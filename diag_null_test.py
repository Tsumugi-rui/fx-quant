"""判定 flat（无结构）市场上的正收益是真 edge 还是系统性偏置。

做法：
  A) 正常策略：跑 N 个种子，记录每个种子的收益率，做单样本 t 检验。
  B) 随机对照：保留完全相同的风控/平仓逻辑与仓位规模，
     但把"开仓决策"替换为随机时点 + 随机方向。
     若 B 也显著为正，说明偏置来自**仿真层或平仓逻辑**，与信号无关。

任何"策略 alpha"的结论都必须满足：A 显著为正，且 A 明显优于 B。
"""
from __future__ import annotations

import concurrent.futures as cf
import math
import statistics
import sys

from fxquant.config import EngineConfig, PAIR_IDS
from fxquant.eval_market import make_market
from fxquant.live import LiveConfig, LiveTrader
from fxquant.sim_execution import SimExecution

TICKS = int(sys.argv[1]) if len(sys.argv) > 1 else 2000
SEEDS = range(1, int(sys.argv[2]) + 1) if len(sys.argv) > 2 else range(1, 25)


def run_one(kind: str, seed: int, random_entry: bool) -> float:
    cfg = EngineConfig(seed=seed)
    cfg.strategy.spread_estimate = 0.0
    feed = make_market(kind, seed)
    ex = SimExecution(seed=seed, feed=feed, spread_rate=0.0)
    lc = LiveConfig(max_ticks=TICKS, poll_interval=0, verbose=False,
                    max_trades=1000)
    t = LiveTrader(ex, cfg, lc)

    if random_entry:
        import random as _r
        rng = _r.Random(seed * 7919)

        def rand_entry(state):
            # 复刻正常路径的规模与约束，只把"方向与时机"换成随机
            if t.risk.halted or state.equity <= 0:
                return
            if len(state.positions) >= cfg.risk.max_positions:
                return
            if t.stats.opens >= lc.max_trades:
                return
            if rng.random() > 0.06:      # 与正常版相近的开仓频率
                return
            pair = rng.choice(list(PAIR_IDS))
            if state.find_position(pair) is not None:
                return
            price = state.prices.get(pair, 0.0)
            if price <= 0:
                return
            side = rng.choice(["long", "short"])
            margin = min(1200.0, max(10.0, state.cash * 0.12))
            if margin + 1 > state.cash:
                return
            res = t.execution.place(pair, side, margin, cfg.risk.leverage)
            if res.ok:
                t.stats.opens += 1
                t._last_entry[pair] = t._tick

        t._consider_entry = rand_entry

    st = t.run()
    return st.last_equity / st.start_equity - 1.0


def summarize(name: str, vals: list[float]) -> None:
    n = len(vals)
    m = statistics.mean(vals)
    sd = statistics.stdev(vals) if n > 1 else 0.0
    se = sd / math.sqrt(n) if n > 1 else float("inf")
    t = m / se if se and se > 0 else 0.0
    print(f"{name:<22} 均值{m:>+8.3%}  标准差{sd:>7.3%}  "
          f"最小{min(vals):>+8.2%}  最大{max(vals):>+8.2%}  t={t:>+5.2f}  n={n}")


if __name__ == "__main__":
    for kind in ("flat",):
        with cf.ProcessPoolExecutor(max_workers=8) as pool:
            normal = list(pool.map(run_one, [kind] * len(SEEDS), SEEDS,
                                   [False] * len(SEEDS)))
            control = list(pool.map(run_one, [kind] * len(SEEDS), SEEDS,
                                    [True] * len(SEEDS)))
        print(f"\n=== {kind}（ticks={TICKS}）===")
        summarize("A 正常策略", normal)
        summarize("B 随机方向对照", control)
