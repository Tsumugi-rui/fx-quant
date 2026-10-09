"""导出某市场的全部成交明细，用于定位系统性偏置。

用法：python _diag_trades.py flat 1 1500
"""
import sys

from pathlib import Path

# 本脚本位于 tools/ 子目录：把项目根加入 sys.path，才能 import fxquant
# （Python 只把**脚本所在目录**放进 sys.path[0]，子目录脚本看不到根目录的包）
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from fxquant.config import EngineConfig
from fxquant.eval_market import make_market
from fxquant.live import LiveConfig, LiveTrader
from fxquant.sim_execution import SimExecution

kind = sys.argv[1] if len(sys.argv) > 1 else "flat"
seed = int(sys.argv[2]) if len(sys.argv) > 2 else 1
ticks = int(sys.argv[3]) if len(sys.argv) > 3 else 1500

cfg = EngineConfig(seed=seed)
cfg.strategy.spread_estimate = 0.0
feed = make_market(kind, seed)
ex = SimExecution(seed=seed, feed=feed, spread_rate=0.0)
lc = LiveConfig(max_ticks=ticks, poll_interval=0, verbose=False, max_trades=1000)
t = LiveTrader(ex, cfg, lc)
st = t.run()

print(f"市场={kind} seed={seed} 权益 {st.start_equity:.2f} -> {st.last_equity:.2f} "
      f"({st.last_equity/st.start_equity-1:+.2%})  开仓{st.opens} 平仓{st.closes}")
print(f"手续费合计 {ex.broker.fees_paid:.2f}")
print()
print(f"{'#':>3} {'标的':<8}{'方向':<6}{'持有':>5}{'保证金':>9}{'名义':>10}"
      f"{'入场':>11}{'出场':>11}{'盈亏$':>10}{'占保证金':>9}  原因")
tot = 0.0
for i, tr in enumerate(ex.broker.history, 1):
    side = "多" if tr.side == 1 else "空"
    hold = tr.closed_tick - tr.opened_tick
    rr = tr.pnl / tr.margin
    tot += tr.pnl
    print(f"{i:>3} {tr.pair:<8}{side:<6}{hold:>5}{tr.margin:>9.0f}{tr.margin*tr.leverage:>10.0f}"
          f"{tr.entry:>11.5f}{tr.exit_price:>11.5f}{tr.pnl:>+10.2f}{rr:>+9.2%}  {tr.reason}")
print(f"\n合计盈亏 ${tot:+.2f}")
wins = sum(1 for tr in ex.broker.history if tr.pnl > 0)
print(f"胜 {wins} / 负 {len(ex.broker.history)-wins}")
