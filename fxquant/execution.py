"""执行器抽象层。

本模块定义「交易执行」的统一接口，让引擎与具体后端解耦。

三种实现：
  - SimExecution    : 内存模拟执行，用于单元测试与干跑
  - BridgeExecution : 通过本地 HTTP 桥接到浏览器里的游戏原生接口
  - DryRunExecution : 包装任意执行器，只记录意图不真正下单

设计原则
--------
引擎只依赖 `Execution` 协议。要接入任何新的交易后端，
只需实现 `state()` / `place()` / `close()` / `advance()` 四个方法。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Protocol


# ---------------------------------------------------------------------------
# 数据模型（对应游戏 read_fx_game_state 的返回结构）
# ---------------------------------------------------------------------------
@dataclass
class RemotePosition:
    id: str
    pair: str
    side: str          # 'long' | 'short'
    margin: float
    leverage: int
    entry: float
    pnl: float


@dataclass
class GameState:
    """游戏当前状态的快照。"""

    mode: str                                   # sim | real | battle | challenge
    cash: float
    equity: float
    debt: float = 0.0
    date: str | None = None
    battle_id: str | None = None
    challenge: dict | None = None
    prices: dict[str, float] = field(default_factory=dict)
    positions: list[RemotePosition] = field(default_factory=list)

    @classmethod
    def from_payload(cls, data: dict) -> "GameState":
        positions = []
        for p in data.get("positions") or []:
            positions.append(RemotePosition(
                id=str(p.get("id", "")),
                pair=str(p.get("pair", "")),
                side=str(p.get("side", "long")),
                margin=float(p.get("margin", 0) or 0),
                leverage=int(p.get("leverage", 1) or 1),
                entry=float(p.get("entry", 0) or 0),
                pnl=float(p.get("pnl", 0) or 0),
            ))
        return cls(
            mode=str(data.get("mode", "sim")),
            cash=float(data.get("cash", 0) or 0),
            equity=float(data.get("equity", 0) or 0),
            debt=float(data.get("debt", 0) or 0),
            date=data.get("date"),
            battle_id=data.get("battleId"),
            challenge=data.get("challenge"),
            prices={k: float(v) for k, v in (data.get("prices") or {}).items()},
            positions=positions,
        )

    def find_position(self, pair: str) -> RemotePosition | None:
        for p in self.positions:
            if p.pair == pair:
                return p
        return None


@dataclass
class OrderResult:
    ok: bool
    position_id: str = ""
    entry: float = 0.0
    fee: float = 0.0
    remaining_cash: float = 0.0
    error: str = ""


@dataclass
class CloseResult:
    ok: bool
    pnl: float = 0.0
    cash: float = 0.0
    error: str = ""


class ExecutionError(RuntimeError):
    """执行层异常（通讯失败、接口拒绝等）。"""


# ---------------------------------------------------------------------------
# 协议
# ---------------------------------------------------------------------------
class Execution(Protocol):
    """交易执行后端协议。"""

    def state(self) -> GameState:
        """读取当前游戏状态。"""

    def place(self, pair: str, side: str, margin: float,
              leverage: int) -> OrderResult:
        """开仓。side 为 'long' 或 'short'。"""

    def close(self, position_id: str) -> CloseResult:
        """按当前价格平仓。"""

    def advance(self) -> dict:
        """推进一个交易日（仅 real / battle 模式可用）。"""

    def is_available(self) -> bool:
        """后端是否可用。"""

    def describe(self) -> str:
        """后端描述，用于日志。"""


# ---------------------------------------------------------------------------
# 通用包装：干跑模式
# ---------------------------------------------------------------------------
class DryRunExecution:
    """只记录意图、不真正下单的执行器。

    安全演练用：完整跑通"读状态 -> 算信号 -> 打算下单 -> 管理持仓"的链路，
    但不产生任何真实交易。

    === 为什么需要虚拟持仓表 ===
    干跑不真下单，页面上的持仓列表**不会变化**。
    如果 state() 直接透传底层状态，就会出现：
        开仓 -> 拿到假 ID -> 下轮读到空持仓列表
             -> 引擎认为没持仓 -> 再开一次 -> 无限重复开仓
    这样干跑结果毫无参考价值（100 轮开出 94 笔假单）。

    因此本类维护一份「虚拟持仓」：
      - place() 时把虚拟持仓加进去
      - close() 时从虚拟持仓里移除
      - state() 返回「底层真实状态 + 虚拟持仓」的叠加视图

    这样引擎看到的世界是自洽的，干跑才能真正验证
    策略逻辑与风控行为（而不是只验证下单指令能否发出）。
    """

    def __init__(self, inner: Execution,
                 virtual_equity: float | None = None) -> None:
        self.inner = inner
        self.intents: list[str] = []
        # 虚拟持仓：id -> RemotePosition
        self._virtual: dict[str, RemotePosition] = {}
        # 虚拟持仓占用的保证金合计（用于在权益里扣除，模拟真实占用）
        self._virtual_margin: float = 0.0
        self._seq = 0

    # -- 状态读取 -----------------------------------------------------------
    def state(self) -> GameState:
        """返回「真实状态 + 虚拟持仓」的叠加视图。

        引擎只会看到叠加后的结果，因此它的决策链路与真实交易时一致。
        """
        real = self.inner.state()
        if not self._virtual:
            return real

        prices = dict(real.prices)
        positions = list(real.positions) + [self._refresh(p, prices)
                                           for p in self._virtual.values()]
        # 虚拟持仓占用的保证金从可用现金中扣除
        return GameState(
            mode=real.mode,
            cash=max(0.0, real.cash - self._virtual_margin),
            equity=real.equity,
            debt=real.debt,
            date=real.date,
            battle_id=real.battle_id,
            challenge=real.challenge,
            prices=prices,
            positions=positions,
        )

    def _refresh(self, pos: RemotePosition, prices: dict[str, float]) -> RemotePosition:
        """按最新价格重算虚拟持仓的浮动盈亏。"""
        price = prices.get(pos.pair, pos.entry)
        if pos.entry > 0 and price > 0:
            notional = pos.margin * max(1, pos.leverage)
            side = 1 if pos.side == 'long' else -1
            pos.pnl = notional * side * (price / pos.entry - 1)
        return pos

    # -- 交易操作 -----------------------------------------------------------
    def place(self, pair: str, side: str, margin: float,
              leverage: int) -> OrderResult:
        st = self.inner.state()
        entry = st.prices.get(pair, 0.0)
        self._seq += 1
        pid = f"dry-{self._seq}"

        self._virtual[pid] = RemotePosition(
            id=pid, pair=pair, side=side, margin=margin,
            leverage=max(1, leverage), entry=entry, pnl=0.0)
        self._virtual_margin += margin

        self.intents.append(
            f"[DRY-RUN] 开仓 {pair} {side} margin=${margin:.2f} "
            f"lev={leverage}x @ {entry:.5f}")
        return OrderResult(ok=True, position_id=pid, entry=entry, fee=0.0,
                           remaining_cash=max(0.0, st.cash - self._virtual_margin))

    def close(self, position_id: str) -> CloseResult:
        pos = self._virtual.pop(position_id, None)
        if pos is None:
            # 不在虚拟表里 —— 可能是真实持仓，交给底层处理
            self.intents.append(f"[DRY-RUN] 平仓 {position_id}（非虚拟持仓，忽略）")
            return CloseResult(ok=False, error=f"干跑模式无此虚拟持仓 {position_id}")

        self._virtual_margin = max(0.0, self._virtual_margin - pos.margin)
        self.intents.append(
            f"[DRY-RUN] 平仓 {pos.pair} {pos.side} 盈亏 ${pos.pnl:+.2f}")
        return CloseResult(ok=True, pnl=pos.pnl, cash=self.inner.state().cash)

    def advance(self) -> dict:
        self.intents.append("[DRY-RUN] 推进交易日")
        return {}

    # -- 元信息 -------------------------------------------------------------
    @property
    def virtual_positions(self) -> list[RemotePosition]:
        return list(self._virtual.values())

    def is_available(self) -> bool:
        return self.inner.is_available()

    def describe(self) -> str:
        return f"干跑模式（底层：{self.inner.describe()}）"
