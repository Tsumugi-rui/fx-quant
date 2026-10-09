"""桥接链路端到端测试。

模拟一个「浏览器端」：轮询 /poll 取任务，按游戏接口的语义回填 /result。
用来验证 Python 引擎 <-> HTTP 桥接 <-> 浏览器 这条链路真的能跑通，
而不需要真的打开游戏页面。

覆盖：
  1. 握手与 token 校验（错误 token 必须被拒）
  2. BridgeExecution 读取游戏状态
  3. 开仓 / 平仓 RPC 往返
  4. 超时错误能被正确抛出（模拟浏览器无响应）
  5. DryRunExecution 不产生真实下单意图泄漏
"""

from __future__ import annotations

import json
import threading
import time
import urllib.error
import urllib.request

import sys
from pathlib import Path

# 本脚本位于 tests/ 子目录：把项目根加入 sys.path，才能 import fxquant
# （Python 只把**脚本所在目录**放进 sys.path[0]，子目录脚本看不到根目录的包）
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from fxquant.bridge import BridgeExecution, BridgeServer
from fxquant.execution import DryRunExecution, ExecutionError

PORT = 8799


class FakeBrowser:
    """模拟油猴脚本：轮询任务并执行游戏接口。"""

    def __init__(self, port: int, token: str, respond: bool = True) -> None:
        self.base = f"http://127.0.0.1:{port}"
        self.token = token
        self.respond = respond          # False 模拟浏览器卡死
        self.running = True
        self.executed: list[str] = []
        self.trades: list[dict] = []
        self.next_id = 1
        self._thread = threading.Thread(target=self._loop, daemon=True)

    def start(self) -> None:
        self._thread.start()

    def stop(self) -> None:
        self.running = False

    def _loop(self) -> None:
        while self.running:
            try:
                with urllib.request.urlopen(
                        f"{self.base}/poll?token={self.token}", timeout=1) as r:
                    payload = json.loads(r.read().decode("utf-8"))
            except Exception:
                time.sleep(0.05)
                continue

            task = payload.get("task")
            if not task:
                time.sleep(0.03)
                continue
            if not self.respond:
                continue          # 取到任务但不回填，制造超时

            tool = task["tool"]
            params = task.get("params") or {}
            self.executed.append(tool)
            data = self._handle(tool, params)
            self._post("/result", {"id": task["id"], "ok": True, "data": data})

    def _handle(self, tool: str, params: dict) -> dict:
        if tool == "read_fx_game_state":
            return {
                "mode": "real",
                "cash": 8000.0,
                "equity": 10000.0,
                "date": "2026-03-01",
                "prices": {"EUR/USD": 1.09000, "USD/JPY": 150.000},
                "positions": [
                    {"id": "p1", "pair": "EUR/USD", "side": "long",
                     "margin": 500.0, "leverage": 5, "entry": 1.08000,
                     "pnl": 46.3},
                ],
            }
        if tool == "place_fx_trade":
            pid = f"g{self.next_id}"
            self.next_id += 1
            self.trades.append(params)
            return {"id": pid, "entry": 1.09000, "fee": 0.15,
                    "remainingCash": 7500.0}
        if tool == "close_fx_trade":
            return {"pnl": 12.5, "cash": 8512.5}
        if tool == "advance_real_fx_day":
            return {"date": "2026-03-02"}
        return {}

    def _post(self, path: str, body: dict) -> None:
        req = urllib.request.Request(
            self.base + path,
            data=json.dumps(body).encode("utf-8"),
            headers={"Content-Type": "application/json"},
            method="POST")
        try:
            urllib.request.urlopen(req, timeout=2).read()
        except Exception:
            pass


def main() -> int:
    print("桥接链路端到端测试")
    print("=" * 62)

    failures: list[str] = []

    with BridgeServer(port=PORT) as server:
        token = server.token
        browser = FakeBrowser(PORT, token)
        browser.start()
        time.sleep(0.3)

        ex = BridgeExecution(server, timeout=2.0)

        # --- 1. 连接状态 ---------------------------------------------------
        if ex.is_available():
            print("[1] 浏览器连接检测              通过")
        else:
            print("[1] 浏览器连接检测              失败")
            failures.append("连接检测")

        # --- 2. 错误 token 必须被拒 -----------------------------------------
        try:
            urllib.request.urlopen(f"http://127.0.0.1:{PORT}/poll?token=WRONG",
                                   timeout=2)
            print("[2] 错误 token 拒绝              失败（居然通过了）")
            failures.append("token 校验")
        except urllib.error.HTTPError as e:
            if e.code == 403:
                print("[2] 错误 token 拒绝              通过")
            else:
                print(f"[2] 错误 token 拒绝              失败（{e.code}）")
                failures.append("token 校验")

        # --- 3. 读取状态 ---------------------------------------------------
        st = ex.state()
        ok3 = (st.mode == "real" and abs(st.equity - 10000.0) < 1e-6
               and len(st.positions) == 1 and st.positions[0].id == "p1"
               and abs(st.prices["USD/JPY"] - 150.0) < 1e-6)
        print(f"[3] 读取游戏状态                "
              f"{'通过' if ok3 else '失败'}  "
              f"(mode={st.mode} equity={st.equity} 持仓={len(st.positions)})")
        if not ok3:
            failures.append("读取状态")

        # --- 4. 开仓 -------------------------------------------------------
        order = ex.place("EUR/USD", "long", 500.0, 5)
        ok4 = (order.ok and order.position_id == "g1"
               and abs(order.entry - 1.09) < 1e-6)
        print(f"[4] 开仓 RPC 往返                "
              f"{'通过' if ok4 else '失败'}  "
              f"(id={order.position_id} entry={order.entry})")
        if not ok4:
            failures.append("开仓")

        # --- 5. 平仓 -------------------------------------------------------
        cl = ex.close("g1")
        ok5 = cl.ok and abs(cl.pnl - 12.5) < 1e-6
        print(f"[5] 平仓 RPC 往返                "
              f"{'通过' if ok5 else '失败'}  (pnl={cl.pnl})")
        if not ok5:
            failures.append("平仓")

        # --- 6. 推进交易日 --------------------------------------------------
        adv = ex.advance()
        ok6 = adv.get("date") == "2026-03-02"
        print(f"[6] 推进交易日                   "
              f"{'通过' if ok6 else '失败'}  (date={adv.get('date')})")
        if not ok6:
            failures.append("推进交易日")

        # --- 7. 超时必须抛 ExecutionError ------------------------------------
        browser.stop()
        time.sleep(0.2)
        try:
            ex2 = BridgeExecution(server, timeout=0.6)
            ex2.state()
            print("[7] 无响应超时报错               失败（未抛异常）")
            failures.append("超时")
        except ExecutionError as e:
            print(f"[7] 无响应超时报错               通过  ({str(e)[:34]}...)")

        # --- 8. 指令序列完整性 -----------------------------------------------
        seq = browser.executed
        expect = ["read_fx_game_state", "place_fx_trade",
                  "close_fx_trade", "advance_real_fx_day"]
        ok8 = seq == expect
        print(f"[8] 指令序列完整                 "
              f"{'通过' if ok8 else '失败'}  ({seq})")
        if not ok8:
            failures.append("指令序列")

        # --- 9. DryRun 不真正下单 ---------------------------------------------
        browser.respond = True
        browser.running = True
        b2 = FakeBrowser(PORT, token)
        b2.start()
        time.sleep(0.3)
        n_before = len(b2.trades)
        dry = DryRunExecution(ex)
        r = dry.place("GBP/USD", "short", 300.0, 5)
        ok9 = r.ok and len(b2.trades) == n_before and len(dry.intents) == 1
        print(f"[9] DryRun 拦截真实下单          "
              f"{'通过' if ok9 else '失败'}  (真实成交数={len(b2.trades)})")
        if not ok9:
            failures.append("DryRun")
        b2.stop()

    print("=" * 62)
    if failures:
        print(f"存在失败项：{', '.join(failures)}")
        return 1
    print("全部通过")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
