"""诊断：score 与后续收益的关系（大样本）。

不经过 LiveTrader，直接驱动市场，对每个时点、每个标的记录
   (score, 之后 eval_horizon 轮的实际收益)
然后看：score 越大，后续收益是否越高？

判据：
  - 若正相关且单调 → 信号有真实 edge，加大信号可用于择时
  - 若无关 → 信号是噪声，任何门槛都无法筛出盈利机会
"""

from __future__ import annotations

from collections import defaultdict

from fxquant.config import EngineConfig, PAIR_IDS
from fxquant.eval_market import make_market
from fxquant.strategy import SignalEngine


def collect(kind: str, seeds=range(1, 9), ticks: int = 2000):
    rows = []   # (score, forward_return)
    for seed in seeds:
        cfg = EngineConfig(seed=seed)
        eng = SignalEngine(cfg)
        feed = make_market(kind, seed)
        # 用已有历史预热指标
        for pair in PAIR_IDS:
            s = feed.series.get(pair)
            if s:
                for c in s.closed_candles():
                    eng.push_bar(pair, c.high, c.low, c.close)

        pending = defaultdict(list)   # pair -> [(resolve_tick, score, entry)]
        path = defaultdict(dict)      # pair -> {tick: price}
        for t in range(ticks):
            prices = feed.advance()
            for pair in PAIR_IDS:
                px = prices.get(pair, 0.0)
                if px > 0:
                    eng.push_bar(pair, px, px, px)
                    path[pair][t] = px
            cache = eng.factor_values_all()
            for pair, values in cache.items():
                sig = eng._compose(pair, values, False)
                px = prices.get(pair, 0.0)
                if px > 0:
                    pending[pair].append((t + cfg.eval_horizon, sig.score, px))
            # 结算
            for pair, queue in pending.items():
                keep = []
                for (rt, score, entry) in queue:
                    if t >= rt:
                        px = prices.get(pair, 0.0)
                        if px > 0 and entry > 0:
                            fwd = (px / entry - 1.0) * (1 if score > 0 else -1)
                            rows.append((score, fwd))
                    else:
                        keep.append((rt, score, entry))
                pending[pair] = keep
    return rows


def show(kind: str, rows) -> None:
    if not rows:
        print(f"{kind}: 无样本")
        return
    n = len(rows)
    scores = [abs(s) for s, _ in rows]
    fwd = [f for _, f in rows]
    mean_fwd = sum(fwd) / n

    # 相关系数
    ms = sum(scores) / n
    cov = sum((s - ms) * (f - mean_fwd) for s, f in zip(scores, fwd)) / n
    vs = sum((s - ms) ** 2 for s in scores) / n
    vf = sum((f - mean_fwd) ** 2 for f in fwd) / n
    corr = cov / (vs * vf) ** 0.5 if vs > 0 and vf > 0 else 0.0

    print(f"\n=== {kind}  n={n} ===")
    print(f"  |score| 与后续收益相关系数: {corr:+.4f}")
    print(f"  全体平均后续收益: {mean_fwd*10000:+.2f} bp")
    print(f"  {'|score|档':<12}{'样本':>8}{'平均后续bp':>13}{'命中率':>9}")
    edges = [0.0, 0.1, 0.2, 0.3, 0.4, 0.5, 0.6, 1.0]
    for lo, hi in zip(edges, edges[1:]):
        sel = [f for s, f in rows if lo <= s < hi]
        if len(sel) < 20:
            continue
        m = sum(sel) / len(sel)
        w = sum(1 for f in sel if f > 0) / len(sel)
        print(f"  {lo:.1f}-{hi:.1f}{'':<7}{len(sel):>8}{m*10000:>+13.2f}{w:>9.1%}")


if __name__ == "__main__":
    for k in ("flat", "trend", "revert"):
        show(k, collect(k))
