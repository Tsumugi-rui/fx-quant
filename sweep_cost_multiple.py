"""扫描 cost_multiple：选一个在"无结构不亏、有结构能赚"上最稳健的值。

用法：python _sweep.py
"""
from __future__ import annotations

import concurrent.futures as cf
import math
import statistics

from fxquant.config import EngineConfig
from fxquant.eval_market import make_market
from fxquant.live import LiveConfig, LiveTrader
from fxquant.sim_execution import SimExecution

KINDS = ["flat", "trend", "trend_mod", "revert_mod", "cycle"]
SEEDS = list(range(1, 13))
TICKS = 1500
MULTIPLES = [1.5, 3.0, 6.0, 12.0]


def run_one(kind, seed, mult):
    cfg = EngineConfig(seed=seed)
    cfg.strategy.spread_estimate = 0.0
    cfg.strategy.cost_multiple = mult
    feed = make_market(kind, seed)
    ex = SimExecution(seed=seed, feed=feed, spread_rate=0.0)
    lc = LiveConfig(max_ticks=TICKS, poll_interval=0, verbose=False,
                    max_trades=1000)
    st = LiveTrader(ex, cfg, lc).run()
    return (st.last_equity / st.start_equity - 1.0, st.opens)


def _job(args):
    return run_one(*args)


def main():
    jobs = [(k, s, m) for m in MULTIPLES for k in KINDS for s in SEEDS]
    with cf.ProcessPoolExecutor(max_workers=10) as pool:
        out = list(pool.map(_job, jobs))

    idx = 0
    print(f"{'市场':<12}" + "".join(f"{'x'+str(m):>18}" for m in MULTIPLES))
    print("-" * 86)
    for k in KINDS:
        row = ""
        for m in MULTIPLES:
            vals = [out[idx + i][0] for i in range(len(SEEDS))]
            opens = [out[idx + i][1] for i in range(len(SEEDS))]
            idx += len(SEEDS)
            mean = statistics.mean(vals)
            sd = statistics.stdev(vals)
            t = mean / (sd / math.sqrt(len(vals))) if sd else 0.0
            row += f"{mean:>+9.2%}(t{t:>+4.1f})"
        print(f"{k:<12}{row}")
    print()
    print("括号内为 t 统计量；每格 n=12 个种子。开仓数见下：")
    idx = 0
    for k in KINDS:
        row = ""
        for m in MULTIPLES:
            opens = [out[idx + i][1] for i in range(len(SEEDS))]
            idx += len(SEEDS)
            row += f"{statistics.mean(opens):>18.1f}"
        print(f"{k:<12}{row}")


if __name__ == "__main__":
    main()
