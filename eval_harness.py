"""策略评估台：在多种自建市场上跑多组随机种子，比较策略的泛化表现。

用法：
    python eval_harness.py            # 跑全部市场
    python eval_harness.py trend      # 只跑某个市场

输出每个市场的：平均收益、中位收益、胜率、平均交易次数。
判读标准（重要）：
  - flat   市场上，策略应当**接近保本**（最优是不交易，不可能稳定盈利）
  - trend  市场上，策略应当**明显盈利**（趋势结构被捕捉）
  - revert 市场上，策略应当**明显盈利**（反转结构被捕捉）
若在 trend/revert 上也赚不到钱，说明策略没有真正的 edge；
若在 flat 上大亏，说明策略在无结构时乱交易、白付成本。
"""

from __future__ import annotations

import statistics
import sys

from fxquant.config import EngineConfig
from fxquant.eval_market import make_market
from fxquant.live import LiveConfig, LiveTrader
from fxquant.sim_execution import SimExecution

KINDS = ["flat", "trend", "trend_mod", "trend_strong",
         "revert", "revert_mod", "revert_strong", "cycle"]


def run_once(kind: str, seed: int, cfg: EngineConfig, ticks: int = 1500,
             spread: float = 0.0):
    # 让"成本门槛里假设的点差"与"实际结算用的点差"保持一致。
    # 否则门槛会按一个并不存在的成本去过滤交易（例如假设 8bp 点差，
    # 而回测里实际不收点差），表现为过度保守、几乎不交易。
    cfg.strategy.spread_estimate = spread
    feed = make_market(kind, seed)
    ex = SimExecution(seed=seed, feed=feed, spread_rate=spread)
    lc = LiveConfig(max_ticks=ticks, poll_interval=0, verbose=False,
                    max_trades=1000)
    trader = LiveTrader(ex, cfg, lc)
    stats = trader.run()
    ret = (stats.last_equity / stats.start_equity - 1.0) if stats.start_equity else 0.0
    return {
        "ret": ret,
        "opens": stats.opens,
        "closes": stats.closes,
        "errors": stats.errors,
        "fees": ex.broker.fees_paid,
    }


def evaluate(kind: str, seeds=range(1, 21), ticks: int = 1500,
             config_hook=None, spread: float = 0.0) -> dict:
    rets, opens, fees, wins = [], [], [], 0
    for seed in seeds:
        cfg = EngineConfig(seed=seed)
        if config_hook:
            config_hook(cfg)
        r = run_once(kind, seed, cfg, ticks, spread)
        rets.append(r["ret"])
        opens.append(r["opens"])
        fees.append(r["fees"])
        if r["ret"] > 0:
            wins += 1
    return {
        "kind": kind,
        "n": len(rets),
        "mean": statistics.mean(rets),
        "median": statistics.median(rets),
        "win": wins / len(rets),
        "opens": statistics.mean(opens),
        "fees": statistics.mean(fees),
        "worst": min(rets),
        "best": max(rets),
    }


def report(kinds=KINDS, seeds=range(1, 21), ticks: int = 1500, hook=None,
           spread: float = 0.0):
    print(f"\n点差 = {spread*10000:.1f} bp（往返成本另加手续费）")
    print(f"{'市场':<12} {'平均':>9} {'中位':>9} {'胜率':>7} "
          f"{'开仓':>6} {'手续费':>9} {'最差':>9} {'最好':>9}")
    print("-" * 78)
    rows = []
    for kind in kinds:
        r = evaluate(kind, seeds, ticks, hook, spread)
        rows.append(r)
        print(f"{r['kind']:<12} {r['mean']:>+9.3%} {r['median']:>+9.3%} "
              f"{r['win']:>7.0%} {r['opens']:>6.1f} {r['fees']:>9.1f} "
              f"{r['worst']:>+9.2%} {r['best']:>+9.2%}")
    return rows


if __name__ == "__main__":
    kinds = [a for a in sys.argv[1:] if not a.startswith("--")]
    spread = 0.0
    for a in sys.argv[1:]:
        if a.startswith("--spread="):
            spread = float(a.split("=")[1])
    report(kinds or KINDS, spread=spread)
