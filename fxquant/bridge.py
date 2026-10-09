"""HTTP 桥接执行器：Python 引擎 <-> 浏览器游戏接口。

=== 为什么需要桥接 ===
游戏只在 `document.modelContext` 存在时才注册交易接口，
这是宿主环境注入的 API，普通浏览器页面里没有。
Python 无法直接调用浏览器内的 JS 函数，因此需要一层桥。

=== 架构 ===
    Python 引擎
        |  HTTP POST /rpc  {"tool": "...", "params": {...}}
        v
    本地桥接服务（本文件，标准库 http.server）
        ^
        |  浏览器插件轮询 /poll，取任务，执行游戏接口，POST /result 回填
        |
    Tampermonkey 脚本（browser_bridge.user.js）

选择「浏览器主动轮询」而不是「服务主动推送」，原因是：
HTTP 服务无法主动调用浏览器，只能等浏览器来取任务。
轮询间隔设得足够短（200ms），对 1.5 秒一跳的游戏行情完全够用。

=== 安全 ===
- 只监听 127.0.0.1，不对外暴露
- 使用一次性 token 校验，防止其它页面误连
- 所有请求带超时，避免引擎挂死
"""

from __future__ import annotations

import json
import threading
import time
import urllib.error
import urllib.request
import uuid
from collections import deque
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from .execution import (
    CloseResult,
    Execution,
    ExecutionError,
    GameState,
    OrderResult,
)

DEFAULT_HOST = "127.0.0.1"
DEFAULT_PORT = 8765
DEFAULT_TIMEOUT = 8.0


class _BridgeState:
    """桥接服务与浏览器之间共享的任务队列。"""

    def __init__(self) -> None:
        self.lock = threading.Lock()
        self.pending: deque[dict] = deque()       # 待浏览器执行的任务
        self.results: dict[str, dict] = {}        # 已回填的结果
        self.events: deque[dict] = deque(maxlen=200)
        self.last_poll: float = 0.0
        self.connected: bool = False
        self.token: str = uuid.uuid4().hex[:16]

    def submit(self, task: dict) -> str:
        task_id = uuid.uuid4().hex
        task["id"] = task_id
        with self.lock:
            self.pending.append(task)
        return task_id

    def take_task(self) -> dict | None:
        with self.lock:
            self.last_poll = time.time()
            self.connected = True
            if self.pending:
                return self.pending.popleft()
        return None

    def put_result(self, task_id: str, payload: dict) -> None:
        with self.lock:
            self.results[task_id] = payload

    def wait_result(self, task_id: str, timeout: float) -> dict | None:
        deadline = time.time() + timeout
        while time.time() < deadline:
            with self.lock:
                if task_id in self.results:
                    return self.results.pop(task_id)
            time.sleep(0.02)
        return None

    def log(self, message: str) -> None:
        with self.lock:
            self.events.append({"t": time.time(), "msg": message})

    def is_connected(self, stale_after: float = 3.0) -> bool:
        with self.lock:
            return self.connected and (time.time() - self.last_poll) < stale_after


def _make_handler(state: _BridgeState):
    class Handler(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"

        def log_message(self, fmt, *args):    # 静默默认日志
            pass

        def _send(self, code: int, payload: dict) -> None:
            body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
            self.send_response(code)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Access-Control-Allow-Origin", "*")
            self.send_header("Access-Control-Allow-Headers", "Content-Type")
            self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
            self.end_headers()
            self.wfile.write(body)

        def _read_json(self) -> dict:
            length = int(self.headers.get("Content-Length") or 0)
            if length <= 0:
                return {}
            raw = self.rfile.read(length)
            try:
                return json.loads(raw.decode("utf-8"))
            except Exception:
                return {}

        def do_OPTIONS(self):                 # CORS 预检
            self._send(200, {"ok": True})

        def do_GET(self):
            if self.path.startswith("/poll"):
                # 浏览器轮询：取任务（token 校验）
                token = ""
                if "?" in self.path:
                    query = self.path.split("?", 1)[1]
                    for kv in query.split("&"):
                        if kv.startswith("token="):
                            token = kv[6:]
                if token != state.token:
                    self._send(403, {"ok": False, "error": "token 无效"})
                    return
                task = state.take_task()
                self._send(200, {"ok": True, "task": task})
                return

            if self.path.startswith("/handshake"):
                self._send(200, {"ok": True, "token": state.token,
                                 "token_required": True})
                return

            if self.path.startswith("/status"):
                self._send(200, {
                    "ok": True,
                    "connected": state.is_connected(),
                    "queued": len(state.pending),
                    "recent": list(state.events)[-12:],
                })
                return

            self._send(404, {"ok": False, "error": "未知路径"})

        def do_POST(self):
            data = self._read_json()

            if self.path.startswith("/result"):
                task_id = str(data.get("id", ""))
                if not task_id:
                    self._send(400, {"ok": False, "error": "缺少任务 id"})
                    return
                state.put_result(task_id, data)
                self._send(200, {"ok": True})
                return

            if self.path.startswith("/log"):
                state.log(str(data.get("msg", "")))
                self._send(200, {"ok": True})
                return

            self._send(404, {"ok": False, "error": "未知路径"})

    return Handler


class BridgeServer:
    """本地桥接 HTTP 服务。"""

    def __init__(self, host: str = DEFAULT_HOST, port: int = DEFAULT_PORT) -> None:
        self.host = host
        self.port = port
        self.state = _BridgeState()
        self._server: ThreadingHTTPServer | None = None
        self._thread: threading.Thread | None = None

    def start(self) -> None:
        handler = _make_handler(self.state)
        self._server = ThreadingHTTPServer((self.host, self.port), handler)
        self._server.daemon_threads = True
        self._thread = threading.Thread(target=self._server.serve_forever,
                                        daemon=True)
        self._thread.start()

    def stop(self) -> None:
        if self._server is not None:
            self._server.shutdown()
            self._server.server_close()
            self._server = None

    @property
    def token(self) -> str:
        return self.state.token

    def is_connected(self) -> bool:
        return self.state.is_connected()

    def __enter__(self) -> "BridgeServer":
        self.start()
        return self

    def __exit__(self, *exc) -> None:
        self.stop()


class BridgeExecution:
    """通过本地桥接服务调用游戏接口的执行器。"""

    def __init__(self, server: BridgeServer,
                 timeout: float = DEFAULT_TIMEOUT) -> None:
        self.server = server
        self.timeout = timeout

    # -- 底层 RPC -----------------------------------------------------------
    def _rpc(self, tool: str, params: dict | None = None) -> dict:
        task_id = self.server.state.submit({"tool": tool, "params": params or {}})
        result = self.server.state.wait_result(task_id, self.timeout)
        if result is None:
            raise ExecutionError(
                f"游戏接口 {tool} 超时（{self.timeout}s）。"
                f"请确认游戏页面已打开且桥接脚本在运行。")
        if not result.get("ok"):
            raise ExecutionError(f"游戏接口 {tool} 返回错误：{result.get('error')}")
        return result.get("data") or {}

    # -- Execution 协议 ------------------------------------------------------
    def is_available(self) -> bool:
        return self.server.is_connected()

    def describe(self) -> str:
        status = "已连接" if self.is_available() else "未连接"
        return f"游戏桥接 {self.server.host}:{self.server.port}（{status}）"

    def state(self) -> GameState:
        data = self._rpc("read_fx_game_state")
        return GameState.from_payload(data)

    def place(self, pair: str, side: str, margin: float,
              leverage: int) -> OrderResult:
        try:
            data = self._rpc("place_fx_trade", {
                "pair": pair, "side": side,
                "margin": margin, "leverage": leverage,
            })
        except ExecutionError as exc:
            return OrderResult(ok=False, error=str(exc))
        return OrderResult(
            ok=True,
            position_id=str(data.get("id", "")),
            entry=float(data.get("entry", 0) or 0),
            fee=float(data.get("fee", 0) or 0),
            remaining_cash=float(data.get("remainingCash", 0) or 0),
        )

    def close(self, position_id: str) -> CloseResult:
        try:
            data = self._rpc("close_fx_trade", {"id": position_id})
        except ExecutionError as exc:
            return CloseResult(ok=False, error=str(exc))
        return CloseResult(
            ok=True,
            pnl=float(data.get("pnl", 0) or 0),
            cash=float(data.get("cash", 0) or 0),
        )

    def advance(self) -> dict:
        return self._rpc("advance_real_fx_day")
