"""扫描盈利保护参数（回吐锁），看哪组配置最能"把浮盈留住"。

在自建评估台（`make_market` 的多档结构市场）上跑，**不用**纯随机游走 ——
按项目约定，随机游走只用于验证流程是否跑通，不可据以调参。

判读标准：
  - flat（无结构）不应因加了保护而变差太多（保护本身要付出手续费成本）
  - trend_* / revert_* / cycle 这些**有结构**的档位应当系统性变好，
    因为它们才是策略真正该赚的钱

用法：python sweep_profit_guard.py [种子数] [轮数]
     python sweep_profit_guard.py 12 1500 --arms=0.005,0.01 --gives=0.2,0.3
"""

from __future__ import annotations

import concurrent.futures as cf
import math
import statistics
import sys

from fxquant.config import EngineConfig
from fxquant.eval_market import make_market
from fxquant.live import LiveConfig, LiveTrader
from fxquant.sim_execution import SimExecution

KINDS = ["flat", "trend", "trend_mod", "trend_strong",
         "revert", "revert_mod", "revert_strong", "cycle"]

# 默认网格：(profit_guard_arm, profit_guard_giveback, hard_stop_ratio)
# arm = 0 表示关闭盈利保护（旧行为，供对照）。
DEFAULT_ARMS = [0.0, 0.01, 0.02, 0.03, 0.05]
DEFAULT_GIVES = [0.4, 0.5, 0.6]
HARD_STOP = 0.35


def build_configs() -> list[tuple[str, float, float, float]]:
    arms = DEFAULT_ARMS
    gives = DEFAULT_GIVES
    for a in sys.argv[1:]:
        if a.startswith("--arms="):
            arms = [float(x) for x in a.split("=", 1)[1].split(",")]
        elif a.startswith("--gives="):
            gives = [float(x) for x in a.split("=", 1)[1].split(",")]
    out = [("关闭保护(旧)", 0.0, 0.5, 0.55)]
    for arm in arms:
        if arm <= 0:
            continue
        for g in gives:
            out.append((f"arm{arm:.1%} give{g:.0%}", arm, g, HARD_STOP))
    return out


def run_one(args):
    kind, seed, ticks, arm, giveback, hard_stop = args
    cfg = EngineConfig(seed=seed)
    cfg.risk.profit_guard_arm = arm
    cfg.risk.profit_guard_giveback = giveback
    cfg.risk.hard_stop_ratio = hard_stop
    feed = make_market(kind, seed)
    ex = SimExecution(seed=seed, feed=feed, spread_rate=0.0)
    lc = LiveConfig(max_ticks=ticks, poll_interval=0, verbose=False,
                    max_trades=1000)
    st = LiveTrader(ex, cfg, lc).run()
    return {
        "kind": kind,
        "ret": st.last_equity / st.start_equity - 1.0 if st.start_equity else 0.0,
        "opens": st.opens,
    }


def main() -> int:
    seeds_n = int(sys.argv[1]) if len(sys.argv) > 1 else 12
    ticks = int(sys.argv[2]) if len(sys.argv) > 2 else 1500
    seeds = list(range(1, seeds_n + 1))
    configs = build_configs()
    base_label = "关闭保护(旧)"

    print(f"盈利保护参数扫描｜{seeds_n} 种子 × {ticks} 轮 × {len(KINDS)} 档"
          f"｜{len(configs)} 组配置")
    print("（点差 0；判读重点是**有结构**的档位是否系统性变好）\n")

    header = f"{'配置':<20}" + "".join(f"{k[:9]:>10}" for k in KINDS)
    header += f"{'结构档均值':>11}{'flat':>9}"
    print(header)
    print("-" * len(header))

    best = None
    all_means: dict[str, dict[str, float]] = {}
    for label, arm, giveback, hard_stop in configs:
        jobs = [(k, s, ticks, arm, giveback, hard_stop)
                for k in KINDS for s in seeds]
        with cf.ProcessPoolExecutor(max_workers=10) as pool:
            rows = list(pool.map(run_one, jobs))

        means = {}
        opens_mean = statistics.mean(r["opens"] for r in rows)
        for k in KINDS:
            vals = [r["ret"] for r in rows if r["kind"] == k]
            means[k] = statistics.mean(vals)
        all_means[label] = means

        structured = [means[k] for k in KINDS if k != "flat"]
        score = statistics.mean(structured)
        line = f"{label:<20}" + "".join(f"{means[k]:>+10.2%}" for k in KINDS)
        line += f"{score:>+11.2%}{means['flat']:>+9.2%}"
        print(line, flush=True)

        # 第二行：交易频率与强趋势档的 t 值。
        # 交易频率很关键 —— 如果"锁盈"靠的是反复进出，开仓数会暴涨，
        # 在真实市场里手续费会把这个优势吃掉。
        ts = [r["ret"] for r in rows if r["kind"] == "trend_strong"]
        sd = statistics.stdev(ts) if len(ts) > 1 else 0.0
        t_ts = statistics.mean(ts) / (sd / math.sqrt(len(ts))) if sd else 0.0
        print(f"{'':<20}开仓/档 {opens_mean:>5.1f}   "
              f"trend_strong t={t_ts:+.2f}", flush=True)

        if best is None or score > best[0]:
            best = (score, label, means)

    base = all_means[base_label]
    print("\n相对旧行为（关闭保护）的变化（正值=改善）：")
    print(f"{'配置':<20}" + "".join(f"{k[:9]:>10}" for k in KINDS)
          + f"{'结构档均值':>11}")
    print("-" * (20 + 10 * len(KINDS) + 11))
    for label, _a, _g, _h in configs:
        if label == base_label:
            continue
        m = all_means[label]
        d = [m[k] - base[k] for k in KINDS]
        line = f"{label:<20}" + "".join(f"{v:>+10.2%}" for v in d)
        struct_d = [m[k] - base[k] for k in KINDS if k != "flat"]
        line += f"{statistics.mean(struct_d):>+11.2%}"
        print(line)

    print(f"\n结构档均值最高：{best[1]}（{best[0]:+.2%}）")
    print(f"  其 flat = {best[2]['flat']:+.2%}"
          f"（旧行为 {base['flat']:+.2%}）")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
