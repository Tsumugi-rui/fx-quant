"""因子信息系数（IC）诊断：直接测量各因子与**未来**收益的相关性。

不跑交易循环，只看"信号里到底有没有可提取的信息"。

方法：先生成完整价格路径，再逐时点计算因子值，
最后用 t 时刻的因子值 与 t→t+h 的**未来**收益 求相关系数。

注意：逐时点采样会让样本高度重叠，这会使 IC 的**显著性**被高估，
但 IC 的**点估计**仍是无偏的，用于横向比较各因子/各市场足够了。
"""

from __future__ import annotations

import math
import sys

from fxquant.config import PAIR_IDS, EngineConfig
from fxquant.eval_market import AutocorrMarket
from fxquant.indicators import IncrementalIndicators
from fxquant.strategy import FactorSet

FACTORS = ("trend", "momentum", "reversion", "cycle")
HORIZONS = (1, 3, 6, 12, 24)


def corr(xs, ys):
    n = len(xs)
    if n < 3:
        return float("nan")
    mx, my = sum(xs) / n, sum(ys) / n
    dx = [x - mx for x in xs]
    dy = [y - my for y in ys]
    cov = sum(a * b for a, b in zip(dx, dy))
    vx = sum(a * a for a in dx)
    vy = sum(b * b for b in dy)
    if vx <= 0 or vy <= 0:
        return float("nan")
    return cov / math.sqrt(vx * vy)


def collect(factory, seed, cfg, warm: int, ticks: int):
    """返回 {pair: {factor: [(factor_value, price), ...]}}。

    必须按标的分别保存：不同标的的价格路径互不相关，
    若把 7 个标的的样本混成一条序列，h=1 实际上会变成
    "这个标的的因子 vs 另一个标的的收益"，相关系数必然趋近 0。
    """
    m = factory(seed)
    ind = IncrementalIndicators(max_window=max(
        cfg.zscore_window, cfg.roc_window, cfg.slope_window,
        cfg.atr_window, cfg.cycle_window) + 8)
    samples = {p: {f: [] for f in FACTORS} for p in PAIR_IDS}

    for step in range(warm + ticks):
        m.advance()
        for p in PAIR_IDS:
            c = m.series[p].candles[-1]
            ind.update(p, c.high, c.low, c.close, cfg.ema_fast, cfg.ema_slow,
                       cfg.atr_window, cfg.slope_window)
        if step < warm:
            continue
        for p in PAIR_IDS:
            snap = ind.snapshot(p, cfg.slope_window)
            closes = ind.closes_of(p)
            if snap is None or snap.get("bars", 0) < 200:
                continue
            vals = {
                "trend": FactorSet.trend(snap, closes, cfg),
                "momentum": FactorSet.momentum(closes, cfg),
                "reversion": FactorSet.reversion(closes, cfg),
                "cycle": FactorSet.cycle(closes, cfg),
            }
            price = snap.get("close") or closes[-1]
            for f in FACTORS:
                if vals[f] is not None:
                    samples[p][f].append((vals[f], price))
    return samples


def ics(samples, h):
    """按标的分别配对，再汇总计算 corr(因子_t, 收益_{t→t+h})。"""
    out = {}
    for f in FACTORS:
        xs, ys = [], []
        for p, per in samples.items():
            seq = per.get(f) or []
            for i in range(len(seq) - h):
                v, p0 = seq[i]
                p1 = seq[i + h][1]
                if p0 <= 0 or p1 <= 0:
                    continue
                xs.append(v)
                ys.append(p1 / p0 - 1.0)
        out[f] = corr(xs, ys)
    return out


def main() -> None:
    cfg = EngineConfig().strategy
    ticks = int(sys.argv[1]) if len(sys.argv) > 1 else 1200
    seeds = int(sys.argv[2]) if len(sys.argv) > 2 else 3
    specs = [
        ("flat", lambda s: AutocorrMarket(phi=0.0, seed=s)),
        ("trend .22", lambda s: AutocorrMarket(phi=0.22, seed=s)),
        ("trend .60", lambda s: AutocorrMarket(phi=0.60, seed=s)),
        ("trend .85", lambda s: AutocorrMarket(phi=0.85, seed=s)),
        ("revert -.60", lambda s: AutocorrMarket(phi=-0.60, seed=s)),
        ("cycle", lambda s: AutocorrMarket(phi=0.0, amplitude=0.0006, seed=s)),
    ]
    for name, factory in specs:
        merged = {p: {f: [] for f in FACTORS} for p in PAIR_IDS}
        for seed in range(1, seeds + 1):
            s = collect(factory, seed, cfg, warm=260, ticks=ticks)
            for p in PAIR_IDS:
                for f in FACTORS:
                    merged[p][f].extend(s[p][f])
        n = len(merged[PAIR_IDS[0]]["trend"])
        print(f"\n=== {name}（每条{n}，共{n*len(PAIR_IDS)}）===")
        print(f"{'因子':<12}" + "".join(f"{'h='+str(h):>9}" for h in HORIZONS))
        table = {h: ics(merged, h) for h in HORIZONS}
        for f in FACTORS:
            row = "".join(f"{table[h][f]:>+9.3f}" for h in HORIZONS)
            print(f"{f:<12}{row}")


if __name__ == "__main__":
    main()
