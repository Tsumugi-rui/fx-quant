"""诊断：各因子在不同预测周期下的信息系数（IC）。

IC = corr(因子取值, 之后 h 期的收益)。
  IC > 0  → 因子方向有效（值越大，未来涨越多）
  IC < 0  → 因子是**反向指标**（值越大，未来反而跌）
  |IC| 很小 → 因子无预测力

这是量化研究里判断"因子有没有用"的标准工具。
关键是要找到 IC 最大的那个 h —— 因子有自己的有效周期，
拿错误的周期去评估，会误判它为无效甚至有害。
"""

from __future__ import annotations

from collections import defaultdict

import sys
from pathlib import Path

# 本脚本位于 tools/ 子目录：把项目根加入 sys.path，才能 import fxquant
# （Python 只把**脚本所在目录**放进 sys.path[0]，子目录脚本看不到根目录的包）
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from fxquant.config import EngineConfig, PAIR_IDS
from fxquant.eval_market import make_market
from fxquant.strategy import SignalEngine

FACTORS = ("trend", "momentum", "reversion", "cycle")
HORIZONS = (1, 2, 3, 5, 10, 20, 40)


def collect(kind: str, seeds=range(1, 6), ticks: int = 2000):
    """返回 {(factor, horizon): [(factor_value, forward_return), ...]}"""
    data = defaultdict(list)
    for seed in seeds:
        cfg = EngineConfig(seed=seed)
        eng = SignalEngine(cfg)
        feed = make_market(kind, seed)
        for pair in PAIR_IDS:
            s = feed.series.get(pair)
            if s:
                for c in s.closed_candles():
                    eng.push_bar(pair, c.high, c.low, c.close)

        by_pair = defaultdict(dict)     # pair -> {t: (vals, price)}
        for t in range(ticks):
            prices = feed.advance()
            for pair in PAIR_IDS:
                px = prices.get(pair, 0.0)
                if px > 0:
                    eng.push_bar(pair, px, px, px)
            cache = eng.factor_values_all()
            for pair, vals in cache.items():
                px = prices.get(pair, 0.0)
                if px > 0:
                    by_pair[pair][t] = (vals, px)

        for pair, d in by_pair.items():
            ts = sorted(d)
            for t in ts:
                vals, px = d[t]
                for h in HORIZONS:
                    if t + h in d and px > 0:
                        fwd = d[t + h][1] / px - 1.0
                        for f in FACTORS:
                            data[(f, h)].append((vals[f], fwd))
    return data


def ic(pairs) -> float:
    n = len(pairs)
    if n < 100:
        return float("nan")
    xs = [p[0] for p in pairs]
    ys = [p[1] for p in pairs]
    mx, my = sum(xs) / n, sum(ys) / n
    cov = sum((x - mx) * (y - my) for x, y in pairs) / n
    vx = sum((x - mx) ** 2 for x in xs) / n
    vy = sum((y - my) ** 2 for y in ys) / n
    if vx <= 0 or vy <= 0:
        return float("nan")
    return cov / (vx * vy) ** 0.5


if __name__ == "__main__":
    for kind in ("flat", "trend", "revert", "cycle", "trend_cycle"):
        data = collect(kind)
        print(f"\n=== {kind} ===   列 = 预测周期 h（轮）")
        print(f"{'因子':<11}" + "".join(f"{h:>10}" for h in HORIZONS))
        for f in FACTORS:
            cells = []
            for h in HORIZONS:
                v = ic(data[(f, h)])
                cells.append(f"{v:>+10.4f}" if v == v else f"{'n/a':>10}")
            print(f"{f:<11}" + "".join(cells))
        # 标注每个因子的最佳周期
        print("  最佳周期：", end="")
        for f in FACTORS:
            best = max(HORIZONS, key=lambda h: abs(ic(data[(f, h)]))
                       if ic(data[(f, h)]) == ic(data[(f, h)]) else -1)
            print(f" {f}={best}(IC={ic(data[(f,best)]):+.4f})", end="")
        print()
