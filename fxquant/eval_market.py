"""合成市场（评估台专用）。

=== 这是什么 ===
这是我们**自己造的测试台**，用来回答一个纯方法论问题：

    "当市场确实存在某类可预测结构时，策略能否发现并利用它？
     当市场没有结构时，策略能否克制住不乱亏成本？"

它**不是**对目标游戏内部算法的复刻，也不含游戏的任何参数。
价格过程用的是金融学里的通用建模工具：
  - 收益一阶自相关（AR(1)）
  - 周期性漂移（正弦）
参数取时间序列的通用量级，与任何具体市场无关。

=== 结构类型 ===
  phi > 0   正自相关 —— 涨势倾向延续（趋势/动量因子应有效）
  phi < 0   负自相关 —— 超涨倾向回落（反转因子应有效）
  phi = 0   随机游走 —— 无可预测结构（最优策略是"少交易"）
  amplitude > 0  叠加正弦漂移 —— 存在周期性机会

为公平比较，所有市场的**无条件波动率相同**：
          sigma_eps = volatility * sqrt(1 - phi^2)

=== 关于"周期"是否算作弊 ===
不算。周期/季节性是世界性现象，不是某个市场的专利，真实外汇与商品
市场普遍存在。关键在于：**策略并不被告知周期是多少**，每个标的的周期
与相位都是独立随机生成的，策略必须自己从价格序列中把它找出来。
这与"偷看某份源码里写死的周期常数"是完全不同的两件事。

=== 为什么不用游戏的数据 ===
若把游戏的行情生成公式搬进来做回测，等于"用内部实现的知识去优化参数"，
与偷看未来等价，得出的策略在真实市场里毫无价值。本模块的价值恰恰在于
它是**中立的**：策略在这里赚不到钱，说明它靠的是运气而非真实 edge。
"""

from __future__ import annotations

import math
import random

from .config import PAIR_IDS
from .market import Candle, MarketSource, MarketSeries


class AutocorrMarket:
    """合成市场：AR(1) 收益自相关 + 可选周期性漂移。"""

    def __init__(self, phi: float = 0.0, volatility: float = 0.0015,
                 seed: int | None = None, warmup: int = 120,
                 pairs: tuple[str, ...] = PAIR_IDS,
                 news_probability: float = 0.004,
                 news_duration: int = 8,
                 amplitude: float = 0.0,
                 min_period: float = 6.0, max_period: float = 40.0) -> None:
        if not -0.95 < phi < 0.95:
            raise ValueError("phi 需在 (-0.95, 0.95) 内，否则过程不平稳")
        self._phi = phi
        self._vol = volatility
        # 保证无条件波动率恒定，便于各市场公平对比
        self._sigma = volatility * (1.0 - phi * phi) ** 0.5
        self._amplitude = max(0.0, amplitude)
        self._min_period = min_period
        self._max_period = max_period
        self._rng = random.Random(seed)
        self._news_prob = news_probability
        self._news_duration = news_duration
        self._news_left = 0
        self._last_ret: dict[str, float] = {}
        self._t: dict[str, int] = {}
        self._cycles: dict[str, tuple[float, float]] = {}
        self.series: dict[str, MarketSeries] = {}

        from .config import PAIRS as _SPECS
        initial_by_id = {s.id: s.initial for s in _SPECS}
        for pair in pairs:
            value = initial_by_id.get(pair, 1.0)
            self._last_ret[pair] = 0.0
            self._t[pair] = 0
            # 每个标的的周期与相位独立随机生成 —— 策略无从预先得知
            period = self._rng.uniform(min_period, max_period)
            phase = self._rng.uniform(0.0, 2.0 * math.pi)
            self._cycles[pair] = (period, phase)
            candles: list[Candle] = []
            for _ in range(warmup):
                open_ = value
                value = open_ * (1.0 + self._step(pair, 1.0))
                candles.append(Candle(open=open_, high=max(open_, value),
                                      low=min(open_, value), close=value))
            self.series[pair] = MarketSeries(pair=pair, candles=candles,
                                             price=value)

    # -- 内部 ---------------------------------------------------------------
    def _step(self, pair: str, vol_boost: float) -> float:
        """推进一个时点的收益率。"""
        r = self._phi * self._last_ret[pair] + \
            self._rng.gauss(0.0, self._sigma * vol_boost)
        if self._amplitude > 0.0:
            period, phase = self._cycles[pair]
            r += self._amplitude * math.sin(
                2.0 * math.pi * self._t[pair] / period + phase)
        self._last_ret[pair] = r
        self._t[pair] += 1
        return r

    # -- MarketSource -------------------------------------------------------
    def bars(self, pair: str) -> list[Candle]:
        return self.series[pair].closed_candles()

    def price(self, pair: str) -> float:
        return self.series[pair].price

    def news_active(self) -> bool:
        return self._news_left > 0

    def advance(self) -> dict[str, float]:
        if self._news_left > 0:
            self._news_left -= 1
        elif self._rng.random() < self._news_prob:
            self._news_left = self._news_duration
        vol_boost = 1.6 if self._news_left > 0 else 1.0

        out: dict[str, float] = {}
        for pair, series in self.series.items():
            open_ = series.price
            close = open_ * (1.0 + self._step(pair, vol_boost))
            # 游戏报价是单一价格（无高低价），因此 high=low=close。
            # 这与实盘环境一致，也考验策略在 ATR 退化时是否稳健。
            series.candles.append(Candle(open=open_, high=max(open_, close),
                                         low=min(open_, close), close=close))
            series.price = close
            out[pair] = close
        return out


def make_market(kind: str, seed: int | None, volatility: float = 0.0015):
    """按名称构造市场。

    kind:
      'flat'          随机游走（无可预测结构）
      'trend' / 'revert'          弱结构（phi = ±0.22）
      'trend_mod' / 'revert_mod'  中等结构（phi = ±0.60）
      'trend_strong' / 'revert_strong'  强结构（phi = ±0.85）
      'cycle'         纯周期性漂移（无自相关）
      'trend_cycle'   周期性漂移 + 弱趋势

    === 为什么要分成三档强度 ===
    早期评估只用了 phi=±0.22 一档。实测该档下最有效因子的 IC 仅约 0.05，
    而强结构档（phi=0.85）可达 0.24 —— 也就是说弱档本身就"几乎无结构"，
    策略在弱档上赚不到钱是正常现象，并不能说明策略有问题。

    分档的意义在于看**梯度**：随结构增强，收益应当单调改善。
    若强结构档上依然不赚钱，才说明策略真的没有 edge。
    """
    table = {
        "flat": dict(phi=0.0),
        "trend": dict(phi=0.22),
        "trend_mod": dict(phi=0.60),
        "trend_strong": dict(phi=0.85),
        "revert": dict(phi=-0.22),
        "revert_mod": dict(phi=-0.60),
        "revert_strong": dict(phi=-0.85),
        "cycle": dict(phi=0.0, amplitude=0.0006),
        "trend_cycle": dict(phi=0.60, amplitude=0.0006),
    }
    if kind not in table:
        raise ValueError(f"未知市场类型 {kind}")
    return AutocorrMarket(volatility=volatility, seed=seed, **table[kind])


__all__ = ["AutocorrMarket", "make_market"]
