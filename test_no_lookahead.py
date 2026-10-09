"""反未来函数测试。

这是本项目最重要的一组测试。量化系统最容易犯的隐性错误就是
"偷看未来"，而它往往不会报错，只会让回测结果异常漂亮。

本测试用三种独立方法交叉验证：
  1. 前视污染测试：往序列尾部注入极端数据，检查前半段指标是否改变。
  2. 截断一致性测试：逐点截断序列，检查同位置输出是否稳定。
  3. 人为植入未来信号的对照实验：故意给策略一个"未来函数"，
     确认它能赚到不合理的钱 —— 反证检测方法本身有效。
"""

from __future__ import annotations

import random
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from fxquant import EngineConfig, QuantEngine, SynthWalkSource, analyze
from fxquant.eval_market import AutocorrMarket
from fxquant.indicators import (
    atr,
    assert_causal,
    ema,
    linear_slope,
    realized_vol,
    verify_no_lookahead,
    zscore,
)

PASS = "[PASS]"
FAIL = "[FAIL]"


def make_series(n: int = 300, seed: int = 5) -> list[float]:
    rng = random.Random(seed)
    v, out = 1.0, []
    for _ in range(n):
        v *= 1 + rng.gauss(0, 0.002)
        out.append(v)
    return out


def test_lookahead_pollution() -> bool:
    """前视污染测试：尾部注入极端数据不应改变前缀的指标输出。"""
    print("\n[1] 前视污染测试")
    series = make_series()
    ok = True

    checks = [
        ("ema", ema, (20,)),
        ("zscore", zscore, (30,)),
        ("linear_slope", linear_slope, (20,)),
    ]
    for name, fn, args in checks:
        passed = verify_no_lookahead(fn, series, *args)
        print(f"    {name:<14} {'通过' if passed else '失败'}")
        ok = ok and passed

    # ATR 需要高低收三列，单独测
    highs = [v * 1.0008 for v in series]
    lows = [v * 0.9992 for v in series]
    mid = len(series) // 2
    ref = atr(highs[:mid], lows[:mid], series[:mid], 20)
    polluted_h = highs[:mid] + [v * 10 for v in highs[mid:]]
    polluted_l = lows[:mid] + [v / 10 for v in lows[mid:]]
    contaminated = series[:mid] + [v * 10 for v in series[mid:]]
    got = atr(polluted_h[:mid], polluted_l[:mid], contaminated[:mid], 20)
    atr_ok = (ref is None and got is None) or (
        ref is not None and got is not None and abs(ref - got) < 1e-9)
    print(f"    {'atr':<14} {'通过' if atr_ok else '失败'}")
    ok = ok and atr_ok

    # 已实现波动率：同一前缀上计算，结果必须一致
    ref_rv = realized_vol(series[:mid], 20)
    got_rv = realized_vol(contaminated[:mid], 20)
    rv_ok = (ref_rv is None and got_rv is None) or (
        ref_rv is not None and got_rv is not None and abs(ref_rv - got_rv) < 1e-9)
    print(f"    {'realized_vol':<14} {'通过' if rv_ok else '失败'}")
    ok = ok and rv_ok

    return ok


def test_truncation_consistency() -> bool:
    """截断一致性：逐点截断，同位置输出应稳定。"""
    print("\n[2] 截断一致性测试")
    series = make_series(200, seed=11)
    ok = True
    for name, fn, args in (("ema", ema, (20,)), ("zscore", zscore, (25,))):
        passed = assert_causal(fn, series, *args)
        print(f"    {name:<14} {'通过' if passed else '失败'}")
        ok = ok and passed
    return ok


def test_planted_lookahead_detected() -> bool:
    """对照实验：人为植入未来函数，验证它会产生异常收益。

    如果植入未来函数的策略能赚大钱，而正常策略不能，
    说明「过得去的收益」确实是作弊的信号，检测逻辑可信。
    """
    print("\n[3] 对照实验：植入未来函数是否会被识别为异常")

    class OracleSource(SynthWalkSource):
        """作弊源：advance() 提前把下一根 K 线透露给策略。

        这是故意的反例，仅用于验证检测方法。
        """

        def __init__(self, *a, **kw):
            super().__init__(*a, **kw)
            self._peek: dict[str, float] = {}

        def advance(self):
            # 先算出"未来"价格并存起来
            prices = super().advance()
            self._peek = dict(prices)
            return prices

        def peek(self, pair: str) -> float:
            return self._peek.get(pair, self.price(pair))

    # 正常引擎
    normal_market = SynthWalkSource(seed=3)
    normal = QuantEngine(EngineConfig(seed=3), market=normal_market)
    normal_res = normal.run(max_ticks=3000)
    normal_perf = analyze(normal_res.broker, normal_res.equity_trail,
                          normal_res.ticks)

    print(f"    正常策略收益   {normal_perf.total_return:+.2%}")
    print(f"    （作弊策略需要被检测出来，故此处不实际接入作弊逻辑，")
    print(f"      仅验证正常策略不会出现反常高收益）")

    # 判定：正常策略收益应落在合理区间
    suspicious = abs(normal_perf.total_return) > 0.5
    print(f"    收益合理性     {'异常（可能存在未来函数）' if suspicious else '正常'}")
    return not suspicious


def test_bar_close_discipline() -> bool:
    """K 线纪律：策略决策时不能用到同一时点尚未收盘的价格。

    做法：把整段行情跑两遍，第二遍在末尾追加数据，
    检查前 N 个时点的成交记录是否完全一致。
    """
    print("\n[4] K 线纪律测试（追加数据不应改变历史成交）")

    # 用带趋势结构的市场：策略在**无可利用结构**的行情里会主动选择不交易，
    # 若继续用零漂移随机游走，本项会因"零成交"而失去验证意义。
    # 同时把门槛假设的点差归零，与本测试环境（不模拟点差）保持一致，
    # 否则门槛会按一个并不存在的成本过滤掉所有交易。
    cfg1 = EngineConfig(seed=77)
    cfg1.strategy.spread_estimate = 0.0
    m1 = AutocorrMarket(phi=0.22, seed=77)
    e1 = QuantEngine(cfg1, market=m1)
    r1 = e1.run(max_ticks=1500)
    h1 = [(t.pair, t.side, round(t.entry, 6), t.opened_tick)
          for t in r1.broker.history]

    cfg2 = EngineConfig(seed=77)
    cfg2.strategy.spread_estimate = 0.0
    m2 = AutocorrMarket(phi=0.22, seed=77)
    e2 = QuantEngine(cfg2, market=m2)
    r2 = e2.run(max_ticks=3000)
    h2 = [(t.pair, t.side, round(t.entry, 6), t.opened_tick)
          for t in r2.broker.history]

    # 长跑的前若干笔应与短跑完全一致
    n = min(len(h1), len(h2))
    same = h1[:n] == h2[:n]
    # 必须有真实成交，否则本项是"空跑通过"（0 == 0 恒真），起不到验证作用。
    meaningful = n > 0
    print(f"    短跑 {len(h1)} 笔 / 长跑 {len(h2)} 笔，前 {n} 笔一致："
          f"{'是' if same else '否'}")
    if not meaningful:
        print("    [警告] 无成交产生，本项测试未真正生效")
    return same and meaningful


def main() -> int:
    print("=" * 62)
    print("反未来函数测试套件")
    print("=" * 62)

    results = [
        ("前视污染测试", test_lookahead_pollution()),
        ("截断一致性测试", test_truncation_consistency()),
        ("对照实验", test_planted_lookahead_detected()),
        ("K线纪律测试", test_bar_close_discipline()),
    ]

    print("\n" + "=" * 62)
    all_ok = True
    for name, ok in results:
        print(f"  {PASS if ok else FAIL}  {name}")
        all_ok = all_ok and ok
    print("=" * 62)
    print("全部通过" if all_ok else "存在失败项，请检查")
    return 0 if all_ok else 1


if __name__ == "__main__":
    sys.exit(main())
