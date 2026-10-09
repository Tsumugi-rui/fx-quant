"""对比：校准未就绪时「允许交易（常数兜底）」vs「禁止交易」。

回答一个具体问题：起步阶段的交易，对最终收益是加分还是减分？
"""
from __future__ import annotations

import concurrent.futures as cf
import statistics
import sys

from pathlib import Path

# 本脚本位于 tools/ 子目录：把项目根加入 sys.path，才能 import fxquant
# （Python 只把**脚本所在目录**放进 sys.path[0]，子目录脚本看不到根目录的包）
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from fxquant.config import EngineConfig
from fxquant.eval_market import make_market
from fxquant.live import LiveConfig, LiveTrader
from fxquant.sim_execution import SimExecution

KINDS = ["flat", "trend", "trend_mod", "trend_strong",
         "revert_mod", "revert_strong", "cycle"]


def run_one(args):
    kind, seed, ticks, require_calib = args
    cfg = EngineConfig(seed=seed)
    cfg.strategy.spread_estimate = 0.0
    cfg.strategy.require_calibration = require_calib
    feed = make_market(kind, seed)
    ex = SimExecution(seed=seed, feed=feed, spread_rate=0.0)
    lc = LiveConfig(max_ticks=ticks, poll_interval=0, verbose=False,
                    max_trades=1000)
    st = LiveTrader(ex, cfg, lc).run()
    return {"kind": kind, "ret": st.last_equity / st.start_equity - 1.0,
            "opens": st.opens, "require": require_calib}


def main():
    seeds = list(range(1, int(sys.argv[1]) + 1)) if len(sys.argv) > 1 else list(range(1, 13))
    ticks = int(sys.argv[2]) if len(sys.argv) > 2 else 1500
    jobs = [(k, s, ticks, rc)
            for rc in (False, True) for k in KINDS for s in seeds]
    with cf.ProcessPoolExecutor(max_workers=10) as pool:
        rows = list(pool.map(run_one, jobs))

    for rc in (False, True):
        label = "允许起步交易（常数兜底）" if not rc else "禁止起步交易（等校准就绪）"
        print(f"\n=== require_calibration={rc} —— {label} ===")
        print(f"{'市场':<15}{'平均收益':>10}{'中位':>9}{'胜率':>7}"
              f"{'t 值':>8}{'开仓':>7}")
        print("-" * 58)
        for k in KINDS:
            vals = [r["ret"] for r in rows if r["kind"] == k and r["require"] == rc]
            opens = [r["opens"] for r in rows if r["kind"] == k and r["require"] == rc]
            mean = statistics.mean(vals)
            sd = statistics.stdev(vals) if len(vals) > 1 else 0.0
            t = mean / (sd / len(vals) ** 0.5) if sd else 0.0
            wins = sum(1 for v in vals if v > 0) / len(vals)
            print(f"{k:<15}{mean:>+10.2%}{statistics.median(vals):>+9.2%}"
                  f"{wins:>7.0%}{t:>+8.2f}{statistics.mean(opens):>7.1f}")


if __name__ == "__main__":
    main()
