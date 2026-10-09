"""量化交易引擎主循环。

=== 反未来函数的时间线纪律 ===
每个时点严格按序执行：
    1. 行情推进一个时点（此刻策略才第一次看到该价格）
    2. 把新 K 线推入增量指标（只影响当前位置及以后）
    3. 结算「已到期」的因子预测（用刚产生的价格验证历史预测）
    4. 登记本时点的因子预测（等待未来验证，此刻不看未来）
    5. 用「含当前时点在内」的因子值合成信号
    6. 风控审核 -> 开仓 / 平仓
    7. 账户结算与快照

策略在任何时刻都无法访问未来价格。
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field

from .broker import Broker
from .config import INITIAL_CASH, PAIR_IDS, EngineConfig
from .market import MarketSource, SynthWalkSource
from .risk import RiskManager
from .strategy import Signal, SignalEngine


@dataclass
class TickLog:
    tick: int
    prices: dict[str, float]
    equity: float
    cash: float
    actions: list[str] = field(default_factory=list)
    weights: dict[str, float] = field(default_factory=dict)
    news: bool = False


@dataclass
class EngineResult:
    equity_trail: list[float]
    broker: Broker
    ticks: int
    logs: list[TickLog] = field(default_factory=list)
    start_equity: float = INITIAL_CASH
    final_weights: dict[str, float] = field(default_factory=dict)
    factor_hit_rates: dict[str, float] = field(default_factory=dict)
    factor_edges: dict[str, float] = field(default_factory=dict)
    factor_t_stats: dict[str, float] = field(default_factory=dict)
    news_events: int = 0


class QuantEngine:
    def __init__(self, config: EngineConfig | None = None,
                 market: MarketSource | None = None) -> None:
        self.config = config or EngineConfig()
        self.market = market or SynthWalkSource(seed=self.config.seed)
        self.broker = Broker(fee_enabled=self.config.fee_enabled)
        self.signals = SignalEngine(self.config)
        self.risk = RiskManager(self.config.risk, self.config.strategy)
        self._logs: list[TickLog] = []
        self._observe_interval = max(1, self.config.eval_horizon // 4)
        self._last_day_bucket = 0
        self._news_events = 0
        self._news_active = False
        # 预热增量指标
        self.signals.warmup(self.market)

    # ------------------------------------------------------------------
    def run(self, max_ticks: int = 3000, progress_every: int = 0,
            realtime: bool = False) -> EngineResult:
        prices = {p: self.market.price(p) for p in PAIR_IDS}
        self.risk.on_new_day(self.broker.equity(prices))

        for tick in range(max_ticks):
            if realtime:
                time.sleep(1.5)

            # 1. 行情推进
            try:
                prices = self.market.advance()
            except StopIteration:
                break

            # 2. 把新 K 线推入增量指标
            for pair in PAIR_IDS:
                series = self.market.series.get(pair)
                if series is None or not series.closed_candles():
                    continue
                bar = series.closed_candles()[-1]
                self.signals.push_bar(pair, bar.high, bar.low, bar.close)

            # 3. 新闻事件（公开信息，仅标记不确定性）
            self._update_news()

            # 4. 每 20 时点计息 + 日切
            day_bucket = tick // 20
            if day_bucket != self._last_day_bucket:
                self._last_day_bucket = day_bucket
                self.broker.maybe_accrue_sim_interest()
                self.risk.on_new_day(self.broker.equity(prices))

            actions: list[str] = []

            # 5. 因子值（学习与决策共用）
            cache = self.signals.factor_values_all(self.market)

            # 6. 自学习：登记 + 结算
            if tick % self._observe_interval == 0:
                self.signals.observe_from_cache(self.market, cache, tick)

            # 7. 持仓风控
            actions += self._manage_positions(prices, tick)

            # 8. 爆仓
            for rec in self.broker.liquidations(prices):
                actions.append(f"LIQUIDATED {rec.pair} pnl={rec.pnl:+.2f}")

            # 9. 开仓
            actions += self._consider_entries(prices, tick, cache)

            # 10. 快照
            eq = self.broker.snapshot_equity(prices)
            self.risk.check_daily_loss(eq)

            if progress_every and tick % progress_every == 0:
                self._logs.append(TickLog(
                    tick=tick, prices=dict(prices), equity=eq,
                    cash=self.broker.cash, actions=actions,
                    weights=self.signals.weighter.weights,
                    news=self._news_active))

        return EngineResult(
            equity_trail=list(self.broker.equity_trail),
            broker=self.broker, ticks=max_ticks, logs=self._logs,
            final_weights=self.signals.weighter.weights,
            factor_hit_rates=self.signals.weighter.hit_rates(),
            factor_edges=self.signals.weighter.realized_edges(),
            factor_t_stats=self.signals.weighter.t_stats(),
            news_events=self._news_events)

    # ------------------------------------------------------------------
    def _update_news(self) -> None:
        """读取行情源对外暴露的「是否有快讯」标记。

        注意：这里只取布尔值，不读取任何方向性偏置。
        快讯是玩家在界面上能实时看到的公开信息。
        """
        marker = getattr(self.market, "news_active", None)
        if callable(marker):
            now = bool(marker())
        else:
            now = bool(marker)
        if now and not self._news_active:
            self._news_events += 1
        self._news_active = now

    def _manage_positions(self, prices: dict[str, float], tick: int) -> list[str]:
        actions: list[str] = []
        for pos in list(self.broker.positions):
            price = prices.get(pos.pair, pos.entry)
            should_close, reason = self.risk.update_trailing(pos, price)
            if not should_close:
                should_close, reason = self.risk.should_force_exit(pos, price, tick)
            if should_close:
                rec = self.broker.close(pos, price, tick, reason)
                actions.append(f"CLOSE {rec.pair} {reason} pnl={rec.pnl:+.2f}")
        return actions

    def _consider_entries(self, prices: dict[str, float], tick: int,
                          cache: dict[str, dict[str, float]]) -> list[str]:
        actions: list[str] = []
        equity = self.broker.equity(prices)
        if self.risk.halted or equity <= 0:
            return actions

        top: Signal | None = self.signals.best_from_cache(
            self.market, cache, news_active=self._news_active)
        if top is None:
            return actions
        if any(p.pair == top.pair for p in self.broker.positions):
            return actions

        price = prices[top.pair]
        decision = self.risk.size_position(
            top, equity, self.broker.cash, len(self.broker.positions), price,
            news_active=self._news_active)
        if not decision.approved:
            return actions

        side = 1 if top.score > 0 else -1
        pos = self.broker.open(
            top.pair, side, decision.margin, decision.leverage, price, tick,
            atr_at_open=top.atr_abs, signal_score=top.score,
            confidence=top.confidence)
        if pos is not None:
            pos.stop_price = decision.stop_price
            pos.take_price = decision.take_price
            actions.append(
                f"OPEN {top.pair} {'LONG' if side == 1 else 'SHORT'} "
                f"score={top.score:+.2f} margin=${pos.margin:.0f} lev={pos.leverage}x")
        return actions
