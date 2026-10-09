"""诊断：策略的信号到底有没有预测力。

核心问题：开仓信号发出后，价格朝我们判断的方向走的概率是多少？
  - 若 ≈50%，策略没有 edge，任何交易都是负期望
  - 若稳定 >52%，才可能覆盖成本

采用**在线记录 + 到期结算**：信号发出时登记，eval_horizon 轮后用
当时的真实价格验证。全程只用到"已经发生"的价格，是合法的历史统计。
"""

from __future__ import annotations

import sys
from pathlib import Path

# 本脚本位于 tools/ 子目录：把项目根加入 sys.path，才能 import fxquant
# （Python 只把**脚本所在目录**放进 sys.path[0]，子目录脚本看不到根目录的包）
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from fxquant.config import EngineConfig
from fxquant.eval_market import make_market
from fxquant.live import LiveConfig, LiveTrader
from fxquant.sim_execution import SimExecution


def diagnose(kind: str, seeds=range(1, 11), ticks: int = 1500) -> None:
    hits = {}
    buckets = {}

    for seed in seeds:
        cfg = EngineConfig(seed=seed)
        horizon = cfg.eval_horizon
        fee_rate = 0.00075  # 开仓手续费率（5x 杠杆下的名义占比），用于净收益判断
        feed = make_market(kind, seed)
        ex = SimExecution(seed=seed, feed=feed)
        lc = LiveConfig(max_ticks=ticks, poll_interval=0, verbose=False,
                        max_trades=1000)
        trader = LiveTrader(ex, cfg, lc)

        pending = []   # (resolve_tick, pair, score, entry_price)
        orig = trader._consider_entry

        def spy(state, _t=trader, _p=pending, _h=horizon):
            now = _t._tick
            # 结算到期
            for item in list(_p):
                if now >= item[0]:
                    _p.remove(item)
                    _, pair, score, entry = item
                    price = state.prices.get(pair, 0.0)
                    if entry > 0 and price > 0:
                        move = price / entry - 1.0
                        signed = move * (1 if score > 0 else -1)
                        b = round(abs(score), 1)
                        d = buckets.setdefault(b, [0, 0])
                        d[0] += 1
                        if signed > 0:
                            d[1] += 1
                        hits[len(hits)] = signed
            # 登记新信号
            cache = _t.signals.factor_values_all()
            top = _t.signals.best_from_cache(None, cache)
            if top is not None and len(state.positions) == 0:
                px = state.prices.get(top.pair, 0.0)
                if px > 0:
                    _p.append((now + _h, top.pair, top.score, px))
            return orig(state)

        trader._consider_entry = spy
        trader.run()

    vals = list(hits.values())
    n = len(vals)
    if n == 0:
        print(f"\n=== {kind} === 无样本")
        return
    win = sum(1 for v in vals if v > 0)
    mean = sum(vals) / n
    # 净收益（扣除成本）
    net = [v - fee_rate for v in vals]
    print(f"\n=== 市场 {kind} （n={n}）===")
    print(f"  方向命中率      : {win/n:.1%}")
    print(f"  平均毛收益/笔   : {mean*10000:+.2f} bp")
    print(f"  平均净收益/笔   : {sum(net)/n*10000:+.2f} bp  （扣 {fee_rate*10000:.1f}bp 成本）")
    print(f"  分档命中率（按 |score|）:")
    for b in sorted(buckets):
        c, w = buckets[b]
        print(f"    |score|≈{b:.1f} : {w}/{c} = {w/c:.1%}")


if __name__ == "__main__":
    for k in ("flat", "trend", "revert"):
        diagnose(k, seeds=range(1, 11), ticks=1500)
