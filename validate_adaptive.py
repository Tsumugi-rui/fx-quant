"""策略有效性验证：在「已知存在结构」的合成数据上检验自适应机制。

=== 为什么需要这个测试 ===
上一个冒烟测试用的是零漂移随机游走，那里没有任何可预测结构，
策略正确地不赚钱 —— 这验证了「不会凭空造出 alpha」。

但要证明自适应机制**真的能用**，需要在可控的、已知存在 
edge 的环境里测试，看它能否识别出来。这里构造两个场景：

  场景 A：强均值回归（价格围绕锚点振荡）
  场景 B：强趋势（价格带漂移持续上行/下行）

若自适应权重能在场景 A 里抬升 reversion、在场景 B 里抬升 trend，
说明在线学习机制工作正常。

注意：这两个场景是**通用统计结构**（Ornstein-Uhlenbeck 过程与
几何布朗运动带漂移），是量化领域的标准测试模型，
不是对目标游戏内部实现的任何推测。
"""

from __future__ import annotations

import math
import random
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from fxquant import Candle, MarketSeries
from fxquant.config import PAIR_IDS, PAIRS


class MeanRevertingSource:
    """Ornstein-Uhlenbeck 均值回归过程（标准统计模型）。

    x_{t+1} = x_t + theta * (mu - x_t) + sigma * eps
    价格围绕 mu 回归，偏离越远回归力越强 —— 这是均值回归因子的理想猎物。
    """

    def __init__(self, seed: int = 1, theta: float = 0.06,
                 sigma: float = 0.0016, warmup: int = 80) -> None:
        self._rng = random.Random(seed)
        self._theta = theta
        self._sigma = sigma
        self.series: dict[str, MarketSeries] = {}
        for spec in PAIRS:
            mu = spec.initial
            value = mu
            candles: list[Candle] = []
            for _ in range(warmup):
                open_ = value
                value = open_ + theta * (mu - open_) + sigma * open_ * self._rng.gauss(0, 1)
                wick = open_ * sigma * self._rng.random()
                candles.append(Candle(open_, max(open_, value) + wick,
                                      min(open_, value) - wick, value))
            self.series[spec.id] = MarketSeries(spec.id, candles, value)

    def bars(self, pair): return self.series[pair].closed_candles()
    def price(self, pair): return self.series[pair].price

    def advance(self):
        out = {}
        for pair, series in self.series.items():
            mu = next(p.initial for p in PAIRS if p.id == pair)
            open_ = series.price
            close = open_ + self._theta * (mu - open_) + \
                self._sigma * open_ * self._rng.gauss(0, 1)
            wick = open_ * self._sigma * self._rng.random()
            series.candles.append(Candle(open_, max(open_, close) + wick,
                                         min(open_, close) - wick, close))
            series.price = close
            out[pair] = close
        return out


class TrendingSource:
    """带强漂移的几何布朗运动（标准统计模型）。

    x_{t+1} = x_t * (1 + mu + sigma * eps)
    持续同向漂移 —— 趋势因子的理想猎物。
    """

    def __init__(self, seed: int = 2, drift: float = 0.0006,
                 sigma: float = 0.0012, warmup: int = 80) -> None:
        self._rng = random.Random(seed)
        self._drift = drift
        self._sigma = sigma
        self.series: dict[str, MarketSeries] = {}
        for i, spec in enumerate(PAIRS):
            # 奇偶货币对给相反方向的漂移，制造多空机会
            direction = 1 if i % 2 == 0 else -1
            value = spec.initial
            candles: list[Candle] = []
            for _ in range(warmup):
                open_ = value
                value = open_ * (1 + direction * drift + sigma * self._rng.gauss(0, 1))
                wick = open_ * sigma * self._rng.random()
                candles.append(Candle(open_, max(open_, value) + wick,
                                      min(open_, value) - wick, value))
            self.series[spec.id] = MarketSeries(spec.id, candles, value)
            self.series[spec.id]._dir = direction  # type: ignore[attr-defined]

    def bars(self, pair): return self.series[pair].closed_candles()
    def price(self, pair): return self.series[pair].price

    def advance(self):
        out = {}
        for pair, series in self.series.items():
            direction = getattr(series, "_dir", 1)
            open_ = series.price
            close = open_ * (1 + direction * self._drift +
                             self._sigma * self._rng.gauss(0, 1))
            wick = open_ * self._sigma * self._rng.random()
            series.candles.append(Candle(open_, max(open_, close) + wick,
                                         min(open_, close) - wick, close))
            series.price = close
            out[pair] = close
        return out


def run_probe(name: str, market, ticks: int = 5000) -> None:
    from fxquant import EngineConfig, QuantEngine, analyze

    cfg = EngineConfig(seed=7)
    engine = QuantEngine(cfg, market=market)
    result = engine.run(max_ticks=ticks)
    perf = analyze(result.broker, result.equity_trail, result.ticks)

    print(f"\n{'=' * 60}")
    print(f"场景：{name}")
    print(f"{'=' * 60}")
    print(f"  总收益        {perf.total_return:+.2%}")
    print(f"  最大回撤      {perf.max_drawdown:.2%}")
    print(f"  交易笔数      {perf.trades}   胜率 {perf.win_rate:.1%}"
          f"   盈亏比 {perf.profit_factor:.2f}")
    print(f"  期望值/笔     ${perf.expectancy:+.2f}")
    print(f"  手续费/毛利   {perf.fee_ratio:.2%}")
    print("  因子权重与显著性：")
    counts = engine.signals.weighter.sample_counts()
    tstats = engine.signals.weighter.t_stats()
    for fname, w in sorted(result.final_weights.items(), key=lambda x: -x[1]):
        t = tstats.get(fname, 0.0)
        flag = "显著" if t >= 2.0 else "未显著"
        print(f"    {fname:<11} 权重={w:.3f}  样本={counts[fname]:<4} "
              f"t={t:>5.2f} ({flag})")


def main() -> None:
    run_probe("均值回归（OU 过程）", MeanRevertingSource(seed=1))
    run_probe("强趋势（带漂移 GBM）", TrendingSource(seed=2))


if __name__ == "__main__":
    main()
