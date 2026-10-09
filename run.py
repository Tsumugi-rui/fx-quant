"""fxquant 命令行入口。

用法：
    python run.py backtest --ticks 4000              # 基准环境回测
    python run.py backtest --csv data.csv --ticks 500 # 用真实历史数据回测
    python run.py backtest --ticks 4000 --report out.html  # 生成 HTML 报告
    python run.py selftest                            # 反未来函数自检
"""

from __future__ import annotations

import argparse
import json
import time
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from fxquant import (
    EngineConfig,
    HistoricalCsvSource,
    QuantEngine,
    SynthWalkSource,
    analyze,
)


def cmd_backtest(args: argparse.Namespace) -> int:
    if args.csv:
        market = HistoricalCsvSource(args.csv)
        print(f"数据源：{args.csv}")
    else:
        market = SynthWalkSource(seed=args.seed)
        print(f"数据源：合成随机游走（seed={args.seed}，仅供流程验证）")

    cfg = EngineConfig(seed=args.seed)
    if args.leverage:
        cfg.risk.leverage = args.leverage
    if args.entry_score:
        cfg.strategy.entry_score = args.entry_score
    if args.risk_per_trade:
        cfg.risk.risk_per_trade = args.risk_per_trade

    engine = QuantEngine(cfg, market=market)
    result = engine.run(max_ticks=args.ticks, progress_every=max(1, args.ticks // 10))
    perf = analyze(result.broker, result.equity_trail, result.ticks)

    print()
    print("=" * 58)
    print("回测结果")
    print("=" * 58)
    rows = [
        ("起始权益", f"${perf.start_equity:,.2f}"),
        ("期末权益", f"${perf.end_equity:,.2f}"),
        ("总收益", f"{perf.total_return:+.2%}"),
        ("最大回撤", f"{perf.max_drawdown:.2%}"),
        ("夏普比率", f"{perf.sharpe:+.2f}"),
        ("索提诺比率", f"{perf.sortino:+.2f}"),
        ("交易笔数", f"{perf.trades}"),
        ("胜率", f"{perf.win_rate:.1%}"),
        ("盈亏比", f"{perf.profit_factor:.3f}"),
        ("每笔期望", f"${perf.expectancy:+.2f}"),
        ("已付手续费", f"${perf.fees_paid:,.2f}"),
        ("手续费/毛利", f"{perf.fee_ratio:.2%}"),
        ("爆仓次数", f"{perf.liquidations}"),
        ("新闻事件", f"{result.news_events}"),
    ]
    for k, v in rows:
        print(f"  {k:<14}{v:>18}")

    print()
    print("自适应因子权重（在线学习）")
    print("-" * 58)
    counts = engine.signals.weighter.sample_counts()
    thr = engine.config.strategy.adapt_t_threshold
    ic = engine.signals.weighter.information_coefficients()
    for name, w in sorted(result.final_weights.items(),
                          key=lambda x: -abs(x[1])):
        t = result.factor_t_stats.get(name, 0.0)
        sig = "显著" if abs(t) >= thr else "未显著"
        icv = ic.get(name, float("nan"))
        print(f"  {name:<11} 权重={w:>+6.3f}  样本={counts[name]:<5} "
              f"IC={icv:>+6.3f}  t={t:>+6.2f} ({sig})")

    if args.report:
        write_report(args.report, perf, result,
                     engine.signals.weighter.sample_counts(),
                     engine.signals.weighter.information_coefficients(),
                     engine.config.strategy.adapt_t_threshold)
        print(f"\nHTML 报告已生成：{args.report}")

    if args.json:
        payload = {
            "performance": perf.as_dict(),
            "weights": result.final_weights,
            "t_stats": result.factor_t_stats,
        }
        Path(args.json).write_text(
            json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
        print(f"JSON 结果已写入：{args.json}")

    return 0


def write_report(path: str, perf, result, engine_counts: dict[str, int],
                 engine_ic: dict[str, float] | None = None,
                 threshold: float = 1.0) -> None:
    trail = result.equity_trail
    engine_ic = engine_ic or {}
    if not trail:
        return
    lo, hi = min(trail), max(trail)
    span = (hi - lo) or 1.0
    w, h = 860, 260
    pts = []
    for i, v in enumerate(trail):
        x = 50 + i / max(1, len(trail) - 1) * (w - 100)
        y = 30 + (1 - (v - lo) / span) * (h - 60)
        pts.append(f"{x:.1f},{y:.1f}")
    polyline = " ".join(pts)

    # 红线表示盈利，绿线表示亏损（中国习惯）
    color = "#d94b4b" if perf.total_return >= 0 else "#2f9c76"

    html = f"""<!DOCTYPE html>
<html lang="zh-CN"><head><meta charset="utf-8">
<title>fxquant 回测报告</title>
<style>
  body{{font-family:-apple-system,"Segoe UI","Microsoft YaHei",sans-serif;
       background:#f7faf9;color:#1b3a36;margin:0;padding:32px}}
  .wrap{{max-width:960px;margin:0 auto}}
  h1{{font-size:22px;margin:0 0 4px}}
  .sub{{color:#6b8b83;font-size:13px;margin-bottom:24px}}
  .cards{{display:grid;grid-template-columns:repeat(4,1fr);gap:12px;margin-bottom:24px}}
  .card{{background:#fff;border:1px solid #e2ece8;border-radius:10px;padding:14px 16px}}
  .card .k{{font-size:12px;color:#7d9a92;margin-bottom:6px}}
  .card .v{{font-size:19px;font-weight:600}}
  .panel{{background:#fff;border:1px solid #e2ece8;border-radius:10px;
         padding:18px;margin-bottom:20px}}
  .panel h2{{font-size:15px;margin:0 0 14px;color:#2b524c}}
  table{{width:100%;border-collapse:collapse;font-size:13px}}
  th,td{{text-align:left;padding:8px 6px;border-bottom:1px solid #eef4f1}}
  th{{color:#7d9a92;font-weight:500}}
  .pos{{color:#d94b4b}} .neg{{color:#2f9c76}}
  .warn{{background:#fff8e6;border:1px solid #f0dca8;padding:12px 16px;
        border-radius:8px;font-size:13px;color:#7a5c14;margin-bottom:22px}}
</style></head><body><div class="wrap">
<h1>fxquant 回测报告</h1>
<div class="sub">时点数 {perf.ticks} · 历时 {perf.duration_days:.2f} 天 · 数据源为合成基准环境</div>

<div class="warn"><strong>基准环境声明：</strong>本报告使用合成随机游走数据，
用于验证程序流程与风控逻辑，<strong>不代表任何真实市场的策略表现</strong>。
在无预测结构的环境中，策略收益应在零附近随机波动；若出现异常高收益，
需优先排查是否存在未来函数泄露。</div>

<div class="cards">
  <div class="card"><div class="k">总收益</div>
    <div class="v {'pos' if perf.total_return>=0 else 'neg'}">{perf.total_return:+.2%}</div></div>
  <div class="card"><div class="k">最大回撤</div><div class="v">{perf.max_drawdown:.2%}</div></div>
  <div class="card"><div class="k">胜率</div><div class="v">{perf.win_rate:.1%}</div></div>
  <div class="card"><div class="k">交易笔数</div><div class="v">{perf.trades}</div></div>
</div>

<div class="panel"><h2>权益曲线</h2>
<svg width="{w}" height="{h}" style="width:100%;height:auto">
  <line x1="50" y1="{30 + (h-60)/2}" x2="{w-50}" y2="{30 + (h-60)/2}"
        stroke="#cfe0da" stroke-dasharray="4 4"/>
  <polyline fill="none" stroke="{color}" stroke-width="2" points="{polyline}"/>
  <text x="50" y="20" font-size="11" fill="#7d9a92">最高 ${hi:,.0f}</text>
  <text x="50" y="{h-8}" font-size="11" fill="#7d9a92">最低 ${lo:,.0f}</text>
</svg></div>

<div class="panel"><h2>绩效明细</h2><table>
<tr><th>指标</th><th>数值</th><th>指标</th><th>数值</th></tr>
<tr><td>起始权益</td><td>${perf.start_equity:,.2f}</td>
    <td>期末权益</td><td>${perf.end_equity:,.2f}</td></tr>
<tr><td>夏普比率</td><td>{perf.sharpe:+.2f}</td>
    <td>索提诺比率</td><td>{perf.sortino:+.2f}</td></tr>
<tr><td>盈亏比</td><td>{perf.profit_factor:.3f}</td>
    <td>每笔期望</td><td>${perf.expectancy:+.2f}</td></tr>
<tr><td>已付手续费</td><td>${perf.fees_paid:,.2f}</td>
    <td>手续费/毛利</td><td>{perf.fee_ratio:.2%}</td></tr>
<tr><td>爆仓次数</td><td>{perf.liquidations}</td>
    <td>新闻事件</td><td>{result.news_events}</td></tr>
</table></div>

<div class="panel"><h2>自适应因子权重</h2><table>
<tr><th>因子</th><th>权重</th><th>样本数</th><th>IC</th><th>t 统计量</th><th>显著性</th></tr>
{"".join(
  f"<tr><td>{n}</td><td>{w:+.3f}</td><td>{engine_counts.get(n,0)}</td>"
  f"<td>{engine_ic.get(n, float('nan')):+.3f}</td>"
  f"<td>{result.factor_t_stats.get(n,0):+.2f}</td>"
  f"<td>{'显著' if abs(result.factor_t_stats.get(n,0)) >= threshold else '未显著'}</td></tr>"
  for n, w in sorted(result.final_weights.items(), key=lambda x: -abs(x[1])))}
</table>
<p style="font-size:12px;color:#7d9a92;margin-top:12px">
权重由已实现的历史表现在线决定：正权重表示顺势使用，负权重表示该因子
在此市场是反向指标（反着用）。|t| 未达阈值时该因子不获得权重；
若所有因子都无显著 edge，则权重全为零、策略主动停止开仓 ——
这是系统在"没有可识别优势"时的诚实表现，而不是退化为等权去赌博。</p>
</div>

</div></body></html>"""

    Path(path).write_text(html, encoding="utf-8")


def cmd_selftest(args: argparse.Namespace) -> int:
    import subprocess
    script = Path(__file__).resolve().parent / "test_no_lookahead.py"
    return subprocess.call([sys.executable, str(script)])


def cmd_bridge(args: argparse.Namespace) -> int:
    """只启动桥接服务，等游戏页面连接。"""
    from fxquant import BridgeServer

    server = BridgeServer(port=args.port)
    server.start()
    print("=" * 58)
    print("fxquant 游戏桥接服务已启动")
    print("=" * 58)
    print(f"  监听地址   http://127.0.0.1:{args.port}")
    print(f"  访问令牌   {server.token}")
    print()
    print("接下来：")
    print("  1. 在浏览器安装 browser_bridge.user.js（油猴脚本）")
    print("  2. 打开游戏页面 https://www.bilibili.com/toy/fx-simple/index.html")
    print("  3. 页面右下角出现「fxquant 桥接 · 已连接」即成功")
    print()
    print("按 Ctrl+C 退出")
    try:
        while True:
            time.sleep(1)
            if server.is_connected():
                print("  [已连接] 游戏页面正在通讯", end="\r", flush=True)
    except KeyboardInterrupt:
        print("\n正在关闭…")
    finally:
        server.stop()
    return 0


def cmd_live(args: argparse.Namespace) -> int:
    """实盘交易循环。"""
    from fxquant import (
        BridgeExecution,
        BridgeServer,
        EngineConfig,
        LiveConfig,
        LiveTrader,
        SimExecution,
    )

    cfg = EngineConfig(seed=args.seed)
    if args.leverage:
        cfg.risk.leverage = args.leverage
    if args.entry_score:
        cfg.strategy.entry_score = args.entry_score
    if args.risk_per_trade:
        cfg.risk.risk_per_trade = args.risk_per_trade
    # 成本门槛的两个关键参数可由命令行覆盖：
    #   --spread         实际观察到的点差（决定"每笔要付多少"）
    #   --cost-multiple  要求预期收益超过成本的几倍（决定"要多保守"）
    if getattr(args, "spread", None) is not None:
        cfg.strategy.spread_estimate = args.spread
    if getattr(args, "cost_multiple", None) is not None:
        cfg.strategy.cost_multiple = args.cost_multiple
    # 盈利保护（回吐锁）的两个参数：
    #   --profit-arm       浮盈达保证金的多少就进入保护状态
    #   --profit-giveback  允许回吐峰值的多少比例（下限抬到保本）
    if getattr(args, "profit_arm", None) is not None:
        cfg.risk.profit_guard_arm = args.profit_arm
    if getattr(args, "profit_giveback", None) is not None:
        cfg.risk.profit_guard_giveback = args.profit_giveback

    live_cfg = LiveConfig(
        poll_interval=args.interval,
        max_loss_pct=args.max_loss,
        target_profit_pct=args.target_profit,
        max_trades=args.max_trades,
        dry_run=args.dry_run,
        max_ticks=args.ticks,
        radar_seconds=getattr(args, "radar", 30.0),
    )

    if args.backend == "sim":
        execution = SimExecution(seed=args.seed)
        server = None
    else:
        # 干跑（--dry-run）本身就不会发出任何真实指令，因此**不需要** --yes。
        # 早先的写法是「game 后端一律要求 --yes」，导致
        # `--backend game --dry-run` 直接被挡在门外 ——
        # 而它恰恰是最该被鼓励先跑的一步（只读行情、只记意图）。
        # 只有真正要下单时，才要求显式确认。
        if not args.yes and not args.dry_run:
            print("=" * 58)
            print("警告：即将通过桥接向游戏发送真实交易指令")
            print("=" * 58)
            print("  这会实际改变游戏账户的持仓与余额。")
            print("  建议先用 --dry-run 演练，确认行为符合预期：")
            print(f"    python run.py live --backend game --dry-run"
                  f" --interval {args.interval} --max-trades {args.max_trades}")
            print()
            print("  确认要真实下单，请加 --yes 参数重新运行。")
            return 1
        if args.dry_run:
            print("干跑模式（--dry-run）：只读行情、只记录下单意图，"
                  "不会向游戏发送任何真实指令。")
        else:
            print("真实交易模式（--yes）：将向游戏发送真实下单指令。")
        server = BridgeServer(port=args.port)
        server.start()
        execution = BridgeExecution(server)
        print(f"桥接服务已启动：http://127.0.0.1:{args.port}")
        print("请在浏览器中打开游戏页面并确认桥接脚本已连接…")
        # 等待窗口给到 60 秒：桥接脚本是定时重连的，
        # 若玩家此刻才去开/刷新游戏页面，15 秒往往不够，
        # 会被误判成"脚本没装好"。
        waited = 0.0
        while waited < 60.0:
            if execution.is_available():
                print(f"已连接（等待 {waited:.1f}s）。")
                break
            time.sleep(0.5)
            waited += 0.5
            if abs(waited - round(waited)) < 1e-9 and int(waited) % 15 == 0:
                print(f"  仍在等待游戏页面连接…（已等 {int(waited)}s / 60s）")
        else:
            print("等待超时：60 秒内未检测到游戏页面连接。")
            print("请确认：① 已安装 browser_bridge.user.js；")
            print("        ② 游戏页面已在浏览器中打开；")
            print(f"        ③ 页面里的桥接地址为 http://127.0.0.1:{args.port}")
            server.stop()
            return 1

    trader = LiveTrader(execution, cfg, live_cfg)

    # 启动横幅：把实际生效的关键参数打印出来。
    # 之前踩过坑 —— 早先默认阈值(0.45)偏高，程序一直只读不下单，
    # 却看不出原因。这里明确展示当前阈值。
    print("-" * 58)
    print(f"  开仓阈值 entry_score = {cfg.strategy.entry_score:.2f}"
          + ("   ← 默认值" if args.entry_score is None else "   ← 命令行覆盖"))
    print(f"  杠杆 leverage        = {cfg.risk.leverage}x")
    print(f"  单笔风险 risk/trade  = {cfg.risk.risk_per_trade:.2%}")
    print(f"  最大持仓 max_pos     = {cfg.risk.max_positions}")
    print(f"  止损停机             = {live_cfg.max_loss_pct:.2%}")
    print(f"  目标止盈             = "
          + (f"{live_cfg.target_profit_pct:.2%}" if live_cfg.target_profit_pct > 0 else "不设"))
    print(f"  交易上限 max_trades  = {live_cfg.max_trades}")
    print(f"  点差估计 spread      = {cfg.strategy.spread_estimate*10000:.1f} bp"
          "   ← 游戏只有单一报价时留 0；若观察到买卖双价再填")
    print(f"  成本倍数门槛         = {cfg.strategy.cost_multiple:.1f}x"
          "   ← 预期收益需超过成本的这个倍数才开仓")
    print(f"  盈利保护             = 浮盈≥保证金{cfg.risk.profit_guard_arm:.1%}"
          f" 后回吐{cfg.risk.profit_guard_giveback:.0%}即离场"
          "   ← 赚过的不再变亏损单")
    print(f"  决策雷达             = "
          + (f"每 {live_cfg.radar_seconds:.0f} 秒汇报一次"
             f"（含持仓理由与因子贡献，见下方「雷达」段）"
             if live_cfg.radar_seconds > 0 else "已关闭"))
    print("-" * 58)

    try:
        stats = trader.run()
    except KeyboardInterrupt:
        trader.stop()
        print("\n收到中断信号，正在停止…")
        stats = trader.stats
    finally:
        if server is not None:
            server.stop()

    if Path(args.log).parent != Path("."):
        Path(args.log).parent.mkdir(parents=True, exist_ok=True)
    Path(args.log).write_text("\n".join(stats.log), encoding="utf-8")
    print(f"\n运行日志已写入：{args.log}")
    return 0


def cmd_strategy(args: argparse.Namespace) -> int:
    """打印当前实际生效的策略、因子与全部参数。

    存在的理由：回答「现在到底用的是哪套策略」。
    参数是在代码里逐处生效的，光读源码很容易看漏某一步门槛；
    这里把所有**实际生效值**一次性摊开 —— 参数漂移是踩过的坑
    （例如 cost_multiple 的帮助文本曾长期写着过期的 1.5）。
    """
    import dataclasses

    from fxquant.live import LiveConfig

    cfg = EngineConfig(seed=args.seed)
    s, r = cfg.strategy, cfg.risk
    live = LiveConfig()
    bar = "=" * 68

    print(bar)
    print("  当前生效的策略：自适应多因子 + 在线校准成本门槛")
    print(bar)

    print("\n【决策链】")
    for line in (
        "① 5 个因子各自打分（-1 ~ +1，带符号）",
        "② 在线学习权重：用 IC 口径回头验证预测力，t 检验筛掉不显著的",
        "③ 合成 score = Σ(因子×权重) / Σ|权重|，再乘波动率调节",
        "④ 门槛一（信号）：|score| ≥ entry_score 才进入风控",
        "⑤ 门槛二（成本）：预期收益 ≥ 往返成本 × cost_multiple",
        "⑥ 仓位：ATR 反推，使单笔最大亏损 = 权益 × risk_per_trade",
        "⑦ 离场：盈利保护(回吐锁) / 灾难止损 / 持仓超时 / 信号反转",
    ):
        print(f"    {line}")

    print("\n【因子】")
    for name, desc in (
        ("trend", "快慢 EMA 乖离 + 趋势斜率"),
        ("momentum", "变化率 ROC + 动量加速度"),
        ("reversion", "布林带 z-score 反向（超买看空、超卖看多）"),
        ("cycle", "谱分析：周期图找主导周期，外推一个时点"),
        ("volatility", "波动率状态，只调节强度，不产生方向"),
    ):
        print(f"    {name:<11}{desc}")
    print("    注：每个因子用「正权重还是负权重」由在线学习决定，"
          "不写死在代码里。")

    print("\n【两道门槛的实际取值】")
    print(f"    信号门槛 entry_score   = {s.entry_score:.2f}"
          f"     ← |score| 低于此值不开仓")
    print(f"    成本门槛 cost_multiple = {s.cost_multiple:.1f}x"
          f"    ← 预期收益需超过往返成本的这个倍数")
    print(f"    成本门槛开关           = "
          f"{'开' if s.cost_gate_enabled else '关'}")
    print(f"    点差估计 spread        = {s.spread_estimate*10000:.1f} bp"
          f"    ← 游戏只有单一报价时保持 0")
    print(f"    常数兜底 score_to_return = {s.score_to_return:.3%}"
          f"  ← 仅在校准未就绪时使用")
    print(f"    校准就绪前允许交易      = "
          f"{'否' if s.require_calibration else '是'}"
          f"    ← 否=更保守但有死锁风险，是=用常数兜底保底能交易")

    print("\n【学习器】")
    print(f"    评估期 eval_horizon    = {cfg.eval_horizon} 轮"
          f"    ← 测「因子在 t 时点 vs 之后 "
          f"{cfg.eval_horizon} 轮的收益」")
    print(f"    adapt_lookback        = {s.adapt_lookback}")
    print(f"    adapt_min_samples     = {s.adapt_min_samples}")
    print(f"    adapt_t_threshold     = {s.adapt_t_threshold}"
          f"    ← t 统计量门槛，低于它不承认该因子有 edge")
    print(f"    adapt_shrinkage       = {s.adapt_shrinkage}")
    print(f"    max_weight            = {s.max_weight}")
    print(f"    calib_lookback        = {s.calib_lookback}")
    print(f"    calib_min_samples     = {s.calib_min_samples}")
    print(f"    calib_min_nonzero     = {s.calib_min_nonzero}"
          f"    ← 还要有这么多非零 score 才算校准就绪")

    print("\n【风险（RiskConfig）】")
    print(f"    杠杆 leverage          = {r.leverage}x")
    print(f"    单笔风险 risk_per_trade= {r.risk_per_trade:.2%}"
          f"    ← 每笔最大亏损占权益比例")
    print(f"    最大持仓 max_positions = {r.max_positions}")
    print(f"    ATR 止损倍数           = {r.atr_stop_multiple}")
    print(f"    盈利保护启动门槛       = 浮盈达保证金 {r.profit_guard_arm:.1%}")
    print(f"    回吐容忍               = 峰值的 {r.profit_guard_giveback:.0%}"
          f"（下限抬到保本，赚过的不再亏）")
    print(f"    灾难止损               = 亏损达保证金 {r.hard_stop_ratio:.0%}")
    print(f"    单笔保证金上限         = {r.max_margin_fraction:.0%} 权益")
    print(f"    单日亏损熔断           = {r.daily_loss_limit:.0%}")

    print("\n【实盘安全（LiveConfig 默认值）】")
    print(f"    累计亏损停机 max_loss_pct = {live.max_loss_pct:.0%}")
    print(f"    交易笔数上限 max_trades   = {live.max_trades}")
    print(f"    持仓超时 max_holding_ticks= {live.max_holding_ticks} 轮"
          f"（约 {live.max_holding_ticks * live.poll_interval / 60:.0f} 分钟 @1.5s）")
    print(f"    开仓冷却                  = {live.entry_cooldown_ticks} 轮")
    print(f"    决策雷达间隔 radar_seconds = {live.radar_seconds:.0f} 秒（0=关闭）")

    print("\n【全部参数（原始值）】")
    for title, obj in (
        ("EngineConfig", cfg),
        ("StrategyConfig", s),
        ("RiskConfig", r),
        ("LiveConfig", live),
    ):
        print(f"  [{title}]")
        for f in dataclasses.fields(obj):
            print(f"    {f.name:<26}= {getattr(obj, f.name)!r}")

    print()
    print("想在实盘里看「持仓理由 / 做多做空 / 因子贡献 / 被什么挡住」，"
          "运行 live 后看决策雷达输出；")
    print("或用 --radar N 调整汇报间隔（单位：秒，0 关闭）。")
    return 0


def main() -> int:
    p = argparse.ArgumentParser(prog="fxquant", description="FX 简单! 量化交易程序")
    sub = p.add_subparsers(dest="cmd")

    b = sub.add_parser("backtest", help="运行回测")
    b.add_argument("--ticks", type=int, default=4000, help="回测时点数")
    b.add_argument("--csv", type=str, default=None, help="历史数据 CSV 路径")
    b.add_argument("--seed", type=int, default=42, help="随机种子")
    b.add_argument("--leverage", type=int, default=None, help="覆盖杠杆")
    b.add_argument("--entry-score", type=float, default=None, help="覆盖开仓阈值")
    b.add_argument("--risk-per-trade", type=float, default=None, help="覆盖单笔风险比例")
    b.add_argument("--report", type=str, default=None, help="输出 HTML 报告路径")
    b.add_argument("--json", type=str, default=None, help="输出 JSON 结果路径")
    b.set_defaults(func=cmd_backtest)

    s = sub.add_parser("selftest", help="运行反未来函数自检")
    s.set_defaults(func=cmd_selftest)

    st = sub.add_parser("strategy", help="打印当前生效的策略、因子与全部参数")
    st.add_argument("--seed", type=int, default=42, help="随机种子")
    st.set_defaults(func=cmd_strategy)

    bd = sub.add_parser("bridge", help="启动游戏桥接服务（等浏览器连接）")
    bd.add_argument("--port", type=int, default=8765, help="监听端口")
    bd.set_defaults(func=cmd_bridge)

    lv = sub.add_parser("live", help="运行实时交易循环")
    lv.add_argument("--backend", choices=["sim", "game"], default="sim",
                    help="sim=内存演练，game=接入真实游戏")
    lv.add_argument("--dry-run", action="store_true",
                    help="只记录下单意图，不发送真实指令（game 后端下无需 --yes）")
    lv.add_argument("--yes", action="store_true",
                    help="确认发送真实下单指令（game 后端非干跑时必需）")
    lv.add_argument("--ticks", type=int, default=0, help="运行轮数，0 为无限")
    lv.add_argument("--interval", type=float, default=1.5, help="轮询间隔秒")
    lv.add_argument("--max-loss", type=float, default=0.20, help="亏损停机线")
    lv.add_argument("--target-profit", type=float, default=0.0,
                    help="目标盈利率，达标即平仓停机（0=不设，例如 0.05 表示 5%%）")
    lv.add_argument("--max-trades", type=int, default=200, help="交易笔数上限")
    lv.add_argument("--port", type=int, default=8765, help="桥接端口")
    lv.add_argument("--seed", type=int, default=42, help="随机种子（sim 后端）")
    lv.add_argument("--leverage", type=int, default=None, help="覆盖杠杆")
    lv.add_argument("--entry-score", type=float, default=None, help="覆盖开仓阈值")
    lv.add_argument("--spread", type=float, default=None,
                    help="往返点差估计（如 0.0008 = 8bp），影响成本门槛")
    lv.add_argument("--cost-multiple", type=float, default=None,
                    help="成本倍数门槛，越大越保守"
                         f"（默认 {EngineConfig().strategy.cost_multiple:.1f}）")
    lv.add_argument("--risk-per-trade", type=float, default=None, help="覆盖单笔风险")
    lv.add_argument("--profit-arm", type=float, default=None,
                    help="盈利保护启动门槛：浮盈达保证金的该比例后开始保护"
                         "（默认 0.02）")
    lv.add_argument("--profit-giveback", type=float, default=None,
                    help="允许回吐峰值的比例，越小越早落袋（默认 0.5）")
    lv.add_argument("--radar", type=float, default=30.0,
                    help="决策雷达间隔（秒），每 N 秒汇报持仓理由、做多做空、"
                         "因子贡献与受阻原因，0=关闭")
    lv.add_argument("--log", type=str, default="logs/live.log", help="日志输出路径")
    lv.set_defaults(func=cmd_live)

    args = p.parse_args()
    if not args.cmd:
        p.print_help()
        return 1
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
