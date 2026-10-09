"""冒烟测试：验证引擎流程跑通，并观察自适应权重的收敛过程。

注意：这里用的是 SynthWalkSource（零漂移通用随机游走），
它的期望收益为零、无可预测结构。因此正确的结果应当是
「策略既不大赚也不大亏」，即不会凭空产生 alpha。

如果在这个基准上出现稳定的高收益，反而说明代码存在未来函数泄露。
"""

import sys
from pathlib import Path

# 本脚本位于 tests/ 子目录：把项目根加入 sys.path，才能 import fxquant
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from fxquant import EngineConfig, QuantEngine, SynthWalkSource, analyze


def main() -> None:
    cfg = EngineConfig(seed=42)
    market = SynthWalkSource(seed=42)
    engine = QuantEngine(cfg, market=market)
    result = engine.run(max_ticks=4000, progress_every=400)

    perf = analyze(result.broker, result.equity_trail, result.ticks)

    print("=" * 56)
    print("绩效概览（零漂移基准环境）")
    print("=" * 56)
    print(f"  起始权益      ${perf.start_equity:,.2f}")
    print(f"  期末权益      ${perf.end_equity:,.2f}")
    print(f"  总收益        {perf.total_return:+.2%}")
    print(f"  最大回撤      {perf.max_drawdown:.2%}")
    print(f"  夏普          {perf.sharpe:+.2f}")
    print(f"  交易笔数      {perf.trades}")
    print(f"  胜率          {perf.win_rate:.1%}")
    print(f"  盈亏比        {perf.profit_factor:.3f}")
    print(f"  期望值/笔     ${perf.expectancy:+.2f}")
    print(f"  已付手续费    ${perf.fees_paid:,.2f}")
    print(f"  手续费/毛利   {perf.fee_ratio:.2%}")
    print(f"  爆仓次数      {perf.liquidations}")

    print()
    print("自适应因子权重（在线学习结果）")
    print("-" * 56)
    counts = engine.signals.weighter.sample_counts()
    hits = result.factor_hit_rates
    edges = result.factor_edges
    for name, w in sorted(result.final_weights.items(), key=lambda x: -x[1]):
        hr = hits.get(name, float("nan"))
        eg = edges.get(name, float("nan"))
        print(f"  {name:<11} 权重={w:.3f}  样本={counts[name]:<5} "
              f"命中率={hr:.3f}  已实现={eg:+.2f}bp")

    print()
    print("权重演化（每 400 时点）")
    print("-" * 56)
    for log in result.logs:
        w = log.weights
        parts = "  ".join(f"{k}={v:.2f}" for k, v in sorted(w.items()))
        print(f"  t={log.tick:<5} equity=${log.equity:>9,.2f}  {parts}")


if __name__ == "__main__":
    main()
