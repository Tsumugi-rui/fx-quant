"""经纪商引擎：账户、持仓、盈亏、爆仓与贷款的结算逻辑。

严格对照 app.js：
  positionPnl(pos) = notional * side * (price / entry - 1)
  account()        = { used, floating, equity = cash + used + floating - debt }
  closePosition()  = cash += margin + pnl
  riskRatio(pos)   = max(0, -pnl / margin)
  爆仓             : riskRatio >= 0.80
"""

from __future__ import annotations

from dataclasses import dataclass, field
from itertools import count

from .config import (
    LIQUIDATION_RATIO,
    MAX_HISTORY,
    loan_daily_rate,
    trade_fee,
)

# 利息结算周期：每 20 个时点视为一个"计息日"
LOAN_TICKS_PER_DAY: int = 20

_id_counter = count(1)


@dataclass
class Position:
    id: str
    pair: str
    side: int                 # 1 = 做多, -1 = 做空
    entry: float
    margin: float
    leverage: int
    notional: float
    fee: float
    opened_tick: int
    # 风控运行时字段
    peak_pnl: float = 0.0
    stop_price: float | None = None
    take_price: float | None = None
    trailing_active: bool = False
    borrowed: bool = False
    atr_at_open: float | None = None
    signal_score: float = 0.0
    confidence: float = 0.0

    def pnl(self, price: float) -> float:
        return self.notional * self.side * (price / self.entry - 1.0)

    def risk_ratio(self, price: float) -> float:
        """亏损占保证金比例，>= 0.8 即爆仓。"""
        return max(0.0, -self.pnl(price) / self.margin)

    def return_pct(self, price: float) -> float:
        """相对保证金的收益率。"""
        return self.pnl(price) / self.margin


@dataclass
class TradeRecord:
    pair: str
    side: int
    pnl: float
    fee: float
    liquidated: bool
    opened_tick: int
    closed_tick: int
    reason: str
    entry: float
    exit_price: float
    margin: float
    leverage: int


class Broker:
    """账户与撮合结算。"""

    def __init__(self, initial_cash: float = 10_000.0, fee_enabled: bool = True,
                 account_id: str = "fx-sim") -> None:
        self.initial_cash = initial_cash
        self.cash = initial_cash
        self.debt = 0.0
        self.loan_principal = 0.0
        self.loan_rate = 0.0
        self.loan_ticks = 0
        self.fee_enabled = fee_enabled
        self.fees_paid = 0.0
        self.positions: list[Position] = []
        self.history: list[TradeRecord] = []
        self.equity_trail: list[float] = []
        self.account_id = account_id
        self._loan_day_ticks = LOAN_TICKS_PER_DAY

    # -- 账户视图 -----------------------------------------------------------
    @property
    def used_margin(self) -> float:
        return sum(p.margin for p in self.positions)

    def floating_pnl(self, prices: dict[str, float]) -> float:
        return sum(p.pnl(prices.get(p.pair, p.entry)) for p in self.positions)

    def equity(self, prices: dict[str, float]) -> float:
        return self.cash + self.used_margin + self.floating_pnl(prices) - self.debt

    def free_margin(self, prices: dict[str, float]) -> float:
        return self.cash

    # -- 开仓 ---------------------------------------------------------------
    def open(self, pair: str, side: int, margin: float, leverage: int,
             price: float, tick: int, atr_at_open: float | None = None,
             signal_score: float = 0.0, confidence: float = 0.0) -> Position | None:
        fee = trade_fee(margin, leverage) if self.fee_enabled else 0.0
        if margin < 10 or margin + fee > self.cash + 1e-9:
            return None
        notional = margin * leverage
        self.cash -= margin + fee
        self.fees_paid += fee
        pos = Position(
            id=f"p{next(_id_counter)}",
            pair=pair, side=side, entry=price, margin=margin,
            leverage=leverage, notional=notional, fee=fee, opened_tick=tick,
            atr_at_open=atr_at_open, signal_score=signal_score, confidence=confidence,
        )
        self.positions.append(pos)
        return pos

    # -- 平仓 ---------------------------------------------------------------
    def close(self, pos: Position, price: float, tick: int, reason: str,
              liquidated: bool = False) -> TradeRecord:
        pnl = pos.pnl(price)
        self.cash += pos.margin + pnl
        if pos in self.positions:
            self.positions.remove(pos)
        rec = TradeRecord(
            pair=pos.pair, side=pos.side, pnl=pnl, fee=pos.fee,
            liquidated=liquidated, opened_tick=pos.opened_tick, closed_tick=tick,
            reason=reason, entry=pos.entry, exit_price=price,
            margin=pos.margin, leverage=pos.leverage,
        )
        self.history.append(rec)
        if len(self.history) > MAX_HISTORY:
            self.history = self.history[-MAX_HISTORY:]
        return rec

    def close_all(self, prices: dict[str, float], tick: int, reason: str) -> list[TradeRecord]:
        return [self.close(p, prices.get(p.pair, p.entry), tick, reason)
                for p in list(self.positions)]

    # -- 爆仓检查 -----------------------------------------------------------
    def liquidations(self, prices: dict[str, float]) -> list[TradeRecord]:
        """返回本 tick 触发爆仓的成交记录（对应 checkLiquidations）。"""
        blown: list[TradeRecord] = []
        for pos in list(self.positions):
            price = prices.get(pos.pair, pos.entry)
            if pos.risk_ratio(price) >= LIQUIDATION_RATIO:
                blown.append(self.close(pos, price, -1, "liquidation", liquidated=True))
        return blown

    # -- 贷款 ---------------------------------------------------------------
    def borrow(self, amount: float) -> bool:
        if amount < 1 or amount > 200_000 or self.debt > 0:
            return False
        self.debt = amount
        self.loan_principal = amount
        self.loan_rate = loan_daily_rate(amount)
        self.loan_ticks = 0
        self.cash += amount
        return True

    def repay(self) -> bool:
        if self.debt <= 0 or self.cash < self.debt:
            return False
        self.cash -= self.debt
        self.debt = 0.0
        self.loan_principal = 0.0
        self.loan_rate = 0.0
        return True

    def accrue_interest(self) -> None:
        if self.debt <= 0:
            return
        self.debt = round((self.debt + round(self.debt * self.loan_rate, 2)) * 100) / 100

    def maybe_accrue_sim_interest(self) -> bool:
        """模拟模式下每 20 tick 计息一次。"""
        if self.debt <= 0:
            return False
        self.loan_ticks += 1
        if self.loan_ticks >= self._loan_day_ticks:
            self.loan_ticks = 0
            self.accrue_interest()
            return True
        return False

    # -- 权益曲线 -----------------------------------------------------------
    def snapshot_equity(self, prices: dict[str, float]) -> float:
        eq = round(self.equity(prices), 2)
        self.equity_trail.append(eq)
        return eq

    # -- 绩效 ---------------------------------------------------------------
    def stats(self) -> dict[str, float]:
        trades = self.history
        wins = [t for t in trades if t.pnl > 0]
        losses = [t for t in trades if t.pnl <= 0]
        gross_win = sum(t.pnl for t in wins)
        gross_loss = abs(sum(t.pnl for t in losses))
        return {
            "trades": len(trades),
            "wins": len(wins),
            "losses": len(losses),
            "win_rate": len(wins) / len(trades) if trades else 0.0,
            "gross_win": gross_win,
            "gross_loss": gross_loss,
            "profit_factor": (gross_win / gross_loss) if gross_loss > 0 else float("inf"),
            "net_pnl": sum(t.pnl for t in trades),
            "fees_paid": self.fees_paid,
            "liquidations": sum(1 for t in trades if t.liquidated),
        }
