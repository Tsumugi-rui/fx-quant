"""模拟执行后端：在内存里复现游戏的交易结算。

用途：
  1. 单元测试执行层链路，不需要真的开浏览器
  2. 干跑演练，验证引擎到执行层的完整通路
  3. 作为桥接不可用时的降级方案

结算口径与该项目 broker.py 保持一致（同一套公开规则），
因此可用于交叉验证桥接执行器是否正确。
"""

from __future__ import annotations

from .broker import Broker
from .config import PAIR_IDS, PAIRS
from .execution import (
    CloseResult,
    GameState,
    OrderResult,
    RemotePosition,
)
from .market import SynthWalkSource


class SimExecution:
    """内存模拟执行后端。

    `auto_advance=True` 时，每次 `state()` 读取都会自动推进一次行情 ——
    这模拟真实游戏"1.5 秒一跳"的节奏：live 循环每次读状态，
    市场都往前走了一步。设为 False 则行情静止，便于精确测试下单逻辑。
    """

    def __init__(self, seed: int | None = None, mode: str = "sim",
                 feed: SynthWalkSource | None = None,
                 auto_advance: bool = True,
                 spread_rate: float = 0.0) -> None:
        self.feed = feed or SynthWalkSource(seed=seed)
        self.broker = Broker()
        self.mode = mode
        self.auto_advance = auto_advance
        # 买卖价差（相对中间价的比例）。真实交易里玩家买入按较高的 ask、
        # 卖出按较低的 bid，一来一回损失约一个点差。评估台默认 0 是为了
        # 不影响既有单元测试；做策略评估时必须设为真实值，否则会高估收益。
        self.spread_rate = max(0.0, spread_rate)
        self._tick = 0
        self._remote_ids: dict[str, str] = {}     # 本地 id -> 对外 id
        self._reverse: dict[str, str] = {}
        self._next_remote = 1

    # -- 内部 ---------------------------------------------------------------
    def _fill_price(self, pair: str, side: str, opening: bool) -> float:
        """成交价 = 中间价 ± 半个点差。

        买入（long）总是付出更高价，卖出（short）总是得到更低价，
        与真实交易一致。开仓和平仓都会各承受半个点差。
        """
        mid = self.feed.price(pair)
        if self.spread_rate <= 0 or mid <= 0:
            return mid
        half = self.spread_rate / 2.0
        is_buy = (side == "long") == opening
        return mid * (1.0 + half) if is_buy else mid * (1.0 - half)

    # -- Execution 协议 -----------------------------------------------------
    def is_available(self) -> bool:
        return True

    def describe(self) -> str:
        mode = "自动推进" if self.auto_advance else "静止行情"
        return f"内存模拟执行（mode={self.mode}，{mode}）"

    def state(self) -> GameState:
        if self.auto_advance:
            self.feed.advance()
            self._tick += 1
            self.broker.maybe_accrue_sim_interest()
            self._settle_liquidations()

        prices = {p: self.feed.price(p) for p in PAIR_IDS}
        positions = []
        for pos in self.broker.positions:
            positions.append(RemotePosition(
                id=self._remote_ids.get(pos.id, pos.id),
                pair=pos.pair,
                side="long" if pos.side == 1 else "short",
                margin=pos.margin,
                leverage=pos.leverage,
                entry=pos.entry,
                pnl=pos.pnl(prices.get(pos.pair, pos.entry)),
            ))
        return GameState(
            mode=self.mode,
            cash=self.broker.cash,
            equity=self.broker.equity(prices),
            debt=self.broker.debt,
            prices=prices,
            positions=positions,
        )

    def _settle_liquidations(self) -> None:
        """模拟模式的爆仓结算，与游戏规则一致（亏损达保证金 80%）。"""
        prices = {p: self.feed.price(p) for p in PAIR_IDS}
        for rec in self.broker.liquidations(prices):
            # 清理对应的远端 id 映射
            stale = [rid for rid, lid in self._reverse.items()
                     if rid not in self._live_remote_ids()]
            for rid in stale:
                lid = self._reverse.pop(rid, None)
                if lid:
                    self._remote_ids.pop(lid, None)

    def _live_remote_ids(self) -> set[str]:
        return {self._remote_ids[p.id] for p in self.broker.positions
                if p.id in self._remote_ids}

    def place(self, pair: str, side: str, margin: float,
              leverage: int) -> OrderResult:
        if pair not in PAIR_IDS:
            return OrderResult(ok=False, error=f"未知货币对 {pair}")
        if side not in ("long", "short"):
            return OrderResult(ok=False, error=f"未知方向 {side}")
        price = self._fill_price(pair, side, opening=True)
        pos = self.broker.open(
            pair, 1 if side == "long" else -1, margin, leverage,
            price, self._tick)
        if pos is None:
            return OrderResult(ok=False, error="保证金不足或参数非法")
        remote_id = f"sim-{self._next_remote}"
        self._next_remote += 1
        self._remote_ids[pos.id] = remote_id
        self._reverse[remote_id] = pos.id
        return OrderResult(ok=True, position_id=remote_id, entry=price,
                           fee=pos.fee, remaining_cash=self.broker.cash)

    def close(self, position_id: str) -> CloseResult:
        local_id = self._reverse.get(position_id)
        if local_id is None:
            return CloseResult(ok=False, error=f"找不到持仓 {position_id}")
        pos = next((p for p in self.broker.positions if p.id == local_id), None)
        if pos is None:
            return CloseResult(ok=False, error="持仓已不存在")
        side = "long" if pos.side == 1 else "short"
        price = self._fill_price(pos.pair, side, opening=False)
        rec = self.broker.close(pos, price, self._tick, "manual")
        self._remote_ids.pop(local_id, None)
        self._reverse.pop(position_id, None)
        return CloseResult(ok=True, pnl=rec.pnl, cash=self.broker.cash)

    def advance(self) -> dict:
        """推进一个时点。模拟模式下等价于行情前进一步。"""
        prices = self.feed.advance()
        self._tick += 1
        self.broker.maybe_accrue_sim_interest()
        return {"date": f"tick-{self._tick}", "prices": prices,
                "equity": self.broker.equity(prices)}

    # -- 便捷读取（供 live 循环使用）------------------------------------------
    def latest_bar(self, pair: str):
        bars = self.feed.series[pair].closed_candles()
        return bars[-1] if bars else None
