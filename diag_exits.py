"""诊断：离场逻辑到底把多少"浮盈"变成了"亏损"。

用户反馈「本来盈利的非要拖到止损」。这个脚本直接量化它：

对每一笔交易记录
  - peak  : 持仓期间出现过的最大浮盈（占保证金比例）
  - final : 平仓时的实际盈亏（占保证金比例）
  - reason: 离场原因

然后统计最关键的一个数：
  **「曾经浮盈 ≥ 阈值，最终却亏损平仓」的笔数占比** —— 这就是"贪"的直接度量。

它统计的是"已实现的价格"，不读任何未来数据，也不碰游戏内部实现。
用法：python diag_exits.py [种子数] [每种子轮数]
"""

from __future__ import annotations

import re
import statistics
import sys

from fxquant import EngineConfig, LiveConfig, LiveTrader, SimExecution

FINAL_RE = re.compile(r"盈亏 \$([+-]?[\d.]+)")


class RecTrader(LiveTrader):
    """记录每笔交易峰值/离场原因的子类，不改变任何交易行为。"""

    def __init__(self, *a, **kw) -> None:
        super().__init__(*a, **kw)
        self.snap: dict = {}
        self.records: list[dict] = []

    def _manage_positions(self, state) -> None:
        self.snap = {p.id: p for p in state.positions}
        super()._manage_positions(state)

    def _close_position(self, position_id: str, pair: str, reason: str) -> None:
        pos = self.snap.get(position_id)
        peak = self._peak_pnl.get(position_id, 0.0)
        super()._close_position(position_id, pair, reason)
        if pos is None or pos.margin <= 0:
            return
        m = FINAL_RE.search(self.stats.log[-1]) if self.stats.log else None
        final = float(m.group(1)) if m else 0.0
        self.records.append({
            "pair": pair,
            "peak_ratio": peak / pos.margin,
            "final_ratio": final / pos.margin,
            "reason": reason,
            "pnl": final,
            "margin": pos.margin,
        })


def run_seed(seed: int, ticks: int) -> list[dict]:
    cfg = EngineConfig(seed=seed)
    live_cfg = LiveConfig(poll_interval=0.0, max_trades=400,
                          max_loss_pct=0.99, max_ticks=ticks,
                          verbose=False, warmup_points=80)
    sim = SimExecution(seed=seed)
    trader = RecTrader(sim, cfg, live_cfg)
    trader.run()
    return trader.records


def main() -> int:
    seeds = int(sys.argv[1]) if len(sys.argv) > 1 else 8
    ticks = int(sys.argv[2]) if len(sys.argv) > 2 else 8000

    all_rec: list[dict] = []
    for s in range(seeds):
        all_rec.extend(run_seed(1000 + s, ticks))

    if not all_rec:
        print("没有产生任何交易。")
        return 0

    print("=" * 62)
    print(f"  离场诊断：{seeds} 个种子 × {ticks} 轮｜共 {len(all_rec)} 笔")
    print("=" * 62)

    peaks = [r["peak_ratio"] for r in all_rec]
    finals = [r["final_ratio"] for r in all_rec]
    wins = sum(1 for r in all_rec if r["pnl"] > 0)
    print(f"  峰值浮盈 中位 {statistics.median(peaks):+.1%}  "
          f"均值 {statistics.fmean(peaks):+.1%}  最大 {max(peaks):+.1%}")
    print(f"  最终盈亏 中位 {statistics.median(finals):+.1%}  "
          f"均值 {statistics.fmean(finals):+.1%}")
    print(f"  盈利笔数 {wins}/{len(all_rec)}（{wins/len(all_rec):.0%}）")
    print(f"  合计盈亏 ${sum(r['pnl'] for r in all_rec):+.2f}")

    print("\n  ── 核心指标：浮盈曾经到过某个高度，最后却亏着走 ──")
    print(f"  {'峰值门槛':<10}{'曾达到':>8}{'其中亏损收场':>14}{'转化率':>10}")
    for thr in (0.05, 0.10, 0.15, 0.20, 0.30):
        hit = [r for r in all_rec if r["peak_ratio"] >= thr]
        lost = [r for r in hit if r["pnl"] <= 0]
        rate = f"{len(lost)/len(hit):.0%}" if hit else "—"
        print(f"  ≥{thr:>5.0%}    {len(hit):>8}{len(lost):>14}{rate:>10}")

    print("\n  ── 离场原因分布 ──")
    by_reason: dict[str, list[dict]] = {}
    for r in all_rec:
        key = r["reason"].split("(")[0]
        by_reason.setdefault(key, []).append(r)
    for key, rows in sorted(by_reason.items(), key=lambda kv: -len(kv[1])):
        tot = sum(x["pnl"] for x in rows)
        w = sum(1 for x in rows if x["pnl"] > 0)
        print(f"  {key:<16}{len(rows):>5} 笔  "
              f"合计 ${tot:>+9.2f}  胜率 {w/len(rows):>4.0%}  "
              f"均峰值 {statistics.fmean(x['peak_ratio'] for x in rows):+.1%}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
