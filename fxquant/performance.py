"""绩效分析：把权益曲线与成交记录换算成可读指标。"""

from __future__ import annotations

import math
from dataclasses import dataclass

from .broker import Broker
from .config import BAR_SECONDS, INITIAL_CASH


@dataclass
class Performance:
    start_equity: float
    end_equity: float
    total_return: float
    max_drawdown: float
    max_drawdown_abs: float
    sharpe: float
    sortino: float
    calmar: float
    trades: int
    win_rate: float
    profit_factor: float
    avg_win: float
    avg_loss: float
    expectancy: float
    fees_paid: float
    fee_ratio: float
    liquidations: int
    ticks: int
    duration_days: float
    final_cash: float

    def as_dict(self) -> dict[str, float]:
        return self.__dict__.copy()


def analyze(broker: Broker, equity_trail: list[float], ticks: int,
            start_equity: float = INITIAL_CASH) -> Performance:
    trail = [start_equity] + list(equity_trail)
    end_equity = trail[-1]

    returns = []
    for i in range(1, len(trail)):
        prev = trail[i - 1]
        if prev > 0:
            returns.append(trail[i] / prev - 1.0)

    # 最大回撤
    peak = trail[0]
    max_dd = 0.0
    max_dd_abs = 0.0
    for v in trail:
        if v > peak:
            peak = v
        dd = (peak - v) / peak if peak > 0 else 0.0
        if dd > max_dd:
            max_dd = dd
            max_dd_abs = peak - v

    stats = broker.stats()
    wins = [t.pnl for t in broker.history if t.pnl > 0]
    losses = [t.pnl for t in broker.history if t.pnl <= 0]

    # 每个 tick 视为一个观测点，年化按 1.5 秒/tick 折算
    ticks_per_year = (365 * 24 * 3600) / BAR_SECONDS
    sharpe = _annualized_sharpe(returns, ticks_per_year)
    sortino = _annualized_sortino(returns, ticks_per_year)

    total_return = (end_equity - start_equity) / start_equity if start_equity else 0.0
    calmar = (total_return / max_dd) if max_dd > 0 else float("inf")

    duration_days = ticks * BAR_SECONDS / 86400.0
    gross = abs(sum(t.pnl for t in broker.history))

    return Performance(
        start_equity=start_equity,
        end_equity=end_equity,
        total_return=total_return,
        max_drawdown=max_dd,
        max_drawdown_abs=max_dd_abs,
        sharpe=sharpe,
        sortino=sortino,
        calmar=calmar,
        trades=stats["trades"],
        win_rate=stats["win_rate"],
        profit_factor=stats["profit_factor"],
        avg_win=(sum(wins) / len(wins)) if wins else 0.0,
        avg_loss=(sum(losses) / len(losses)) if losses else 0.0,
        expectancy=(sum(t.pnl for t in broker.history) / len(broker.history))
        if broker.history else 0.0,
        fees_paid=broker.fees_paid,
        fee_ratio=(broker.fees_paid / gross) if gross > 0 else 0.0,
        liquidations=stats["liquidations"],
        ticks=ticks,
        duration_days=duration_days,
        final_cash=broker.cash,
    )


def _mean(xs: list[float]) -> float:
    return sum(xs) / len(xs) if xs else 0.0


def _std(xs: list[float]) -> float:
    if len(xs) < 2:
        return 0.0
    m = _mean(xs)
    return math.sqrt(sum((x - m) ** 2 for x in xs) / (len(xs) - 1))


def _annualized_sharpe(returns: list[float], periods_per_year: float) -> float:
    if len(returns) < 2:
        return 0.0
    sd = _std(returns)
    if sd == 0:
        return 0.0
    return (_mean(returns) / sd) * math.sqrt(periods_per_year)


def _annualized_sortino(returns: list[float], periods_per_year: float) -> float:
    if len(returns) < 2:
        return 0.0
    downside = [r for r in returns if r < 0]
    if not downside:
        return float("inf") if _mean(returns) > 0 else 0.0
    dd = math.sqrt(sum(r ** 2 for r in downside) / len(downside))
    if dd == 0:
        return 0.0
    return (_mean(returns) / dd) * math.sqrt(periods_per_year)
