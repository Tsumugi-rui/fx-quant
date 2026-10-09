"""最终版策略评估报告（多进程）。

用法：
    python final_report.py            # 默认 12 种子、1500 tick
    python final_report.py 24 2000    # 自定义

输出一张"结构强度 → 收益"的梯度表，并给出每档的 t 统计量。
判读标准：
  - flat（无结构）应当 ≈ 0，且不显著 —— 否则就是噪声高估
  - 随结构增强，收益应当单调改善
"""
from __future__ import annotations

import concurrent.futures as cf
import json
import math
import statistics
import sys

from fxquant.config import EngineConfig
from fxquant.eval_market import make_market
from fxquant.live import LiveConfig, LiveTrader
from fxquant.sim_execution import SimExecution

KINDS = ["flat", "trend", "trend_mod", "trend_strong",
         "revert", "revert_mod", "revert_strong", "cycle"]


def run_one(args):
    kind, seed, ticks, spread = args
    cfg = EngineConfig(seed=seed)
    cfg.strategy.spread_estimate = spread
    feed = make_market(kind, seed)
    ex = SimExecution(seed=seed, feed=feed, spread_rate=spread)
    lc = LiveConfig(max_ticks=ticks, poll_interval=0, verbose=False,
                    max_trades=1000)
    st = LiveTrader(ex, cfg, lc).run()
    return {
        "kind": kind,
        "ret": st.last_equity / st.start_equity - 1.0,
        "opens": st.opens,
        "fees": ex.broker.fees_paid,
    }


def main():
    ticks = int(sys.argv[2]) if len(sys.argv) > 2 else 1500
    seeds = list(range(1, (int(sys.argv[1]) if len(sys.argv) > 1 else 12) + 1))

    for spread in (0.0, 0.0004):
        jobs = [(k, s, ticks, spread) for k in KINDS for s in seeds]
        with cf.ProcessPoolExecutor(max_workers=10) as pool:
            rows = list(pool.map(run_one, jobs))

        print(f"\n成本假设：点差 {spread*10000:.1f} bp（另加手续费）"
              f"  |  每档 n={len(seeds)} 种子")
        print(f"{'市场':<14}{'平均收益':>10}{'中位':>9}{'胜率':>7}"
              f"{'t 值':>7}{'开仓':>7}{'手续费$':>9}")
        print("-" * 70)
        for k in KINDS:
            vals = [r["ret"] for r in rows if r["kind"] == k]
            opens = [r["opens"] for r in rows if r["kind"] == k]
            fees = [r["fees"] for r in rows if r["kind"] == k]
            mean = statistics.mean(vals)
            # 单种子时无样本方差，t 值无定义（返回 0 而非崩溃）。
            sd = statistics.stdev(vals) if len(vals) > 1 else 0.0
            t = mean / (sd / math.sqrt(len(vals))) if sd else 0.0
            wins = sum(1 for v in vals if v > 0) / len(vals)
            print(f"{k:<14}{mean:>+10.2%}{statistics.median(vals):>+9.2%}"
                  f"{wins:>7.0%}{t:>+7.2f}{statistics.mean(opens):>7.1f}"
                  f"{statistics.mean(fees):>9.1f}")

    # 落一份 JSON 供报告使用
    with open("final_report.json", "w", encoding="utf-8") as fh:
        json.dump({"ticks": ticks, "seeds": len(seeds)}, fh)
    print("\n完成。")


if __name__ == "__main__":
    main()
