"""风控与仓位管理。

职责：
  1. 依据 ATR 与账户权益计算单笔保证金（波动率目标仓位）
  2. 设置止损 / 止盈 / 追踪止损价位
  3. 保证不逼近爆仓线（保留缓冲）
  4. 单日亏损熔断、最大持仓数约束

设计原则：宁可少赚，不可爆仓。爆仓是量化策略的致命伤。
"""

from __future__ import annotations

from dataclasses import dataclass

from .broker import Broker, Position
from .config import (
    LIQUIDATION_RATIO,
    MAX_LEVERAGE,
    MIN_MARGIN,
    RiskConfig,
    trade_fee,
)
from .strategy import Signal


@dataclass
class RiskDecision:
    approved: bool
    margin: float = 0.0
    leverage: int = 0
    stop_price: float | None = None
    take_price: float | None = None
    reason: str = ""


class RiskManager:
    def __init__(self, cfg: RiskConfig,
                 strategy_cfg: "StrategyConfig | None" = None) -> None:
        self.cfg = cfg
        # 成本门槛参数来自策略配置（score→收益的映射、成本倍数、点差估计）。
        # 允许为 None 以兼容旧调用方（此时成本门槛不生效）。
        if strategy_cfg is None:
            from .config import StrategyConfig as _SC
            strategy_cfg = _SC()
        self.strategy_cfg = strategy_cfg
        self.day_start_equity: float | None = None
        self.halted = False
        self.halt_reason = ""

    # -- 熔断 ---------------------------------------------------------------
    def on_new_day(self, equity: float) -> None:
        self.day_start_equity = equity
        self.halted = False
        self.halt_reason = ""

    def check_daily_loss(self, equity: float) -> bool:
        """单日亏损超阈值则熔断，当日内不再开新仓。"""
        if self.day_start_equity is None:
            self.day_start_equity = equity
            return False
        if self.day_start_equity <= 0:
            return False
        drawdown = (self.day_start_equity - equity) / self.day_start_equity
        if drawdown >= self.cfg.daily_loss_limit:
            self.halted = True
            self.halt_reason = f"单日亏损 {drawdown:.1%} 达到熔断线"
            return True
        return False

    # -- 成本感知门槛 -------------------------------------------------------
    def round_trip_cost(self, margin: float, leverage: int) -> float:
        """一笔交易开+平的**总成本**（美元）。

        构成：
          1. 开仓手续费 trade_fee(margin, leverage)
          2. 平仓手续费（同上，界面未区分方向）
          3. 点差成本 ≈ notional × spread_estimate

        全部基于公开信息：手续费规则来自界面提示，
        点差是玩家可见的买卖价差（这里用保守常数估计）。
        """
        notional = margin * leverage
        open_fee = trade_fee(margin, leverage)
        close_fee = open_fee
        spread_cost = notional * self.strategy_cfg.spread_estimate
        return open_fee + close_fee + spread_cost

    def cost_gate_ok(self, margin: float, leverage: int, score: float,
                     expected_return: float | None = None) -> tuple[bool, str]:
        """成本门槛：预期毛收益是否显著超过往返成本。

        预期收益的**首选来源是在线校准**（`ScoreCalibrator`）：
        它用已实现的价格统计出「每单位 score 对应多少收益率」，
        因此能自动反映信号此刻的真实强度。

        早期版本用一个硬编码常数（score=1.0 对应 0.5% 收益）。
        当信号的真实预测力低于该假设时，门槛会放行大量负期望交易 ——
        实测表现为交易频率暴涨、手续费吞掉全部收益。
        因此这里优先采用校准值；仅在没有校准结果时才退回常数。

        === 符号换算是关键（曾写错） ===
        校准给出的 `expected_return` 是**有符号**的回归预测：
            expected = beta × score     （把「价格收益」回归到 score 上）
        但我们实际的下单方向是 `sign(score)`（score>0 做多、<0 做空），
        所以**这一单**的预期收益是：
            sign(score) × expected = sign(score) × beta × score = beta × |score|
        也就是 `|expected|`。

        因此两个判断必须分开：
          · beta ≤ 0（expected 与 score 异号）：信号方向整体是反的，
            任何方向都没有正期望 —— 拒绝。
          · beta > 0：拿 |expected| 去和成本比。

        早期版本直接 `if expected_return <= 0: 拒绝`，等于
        **只要 score<0（做空）就一律拒绝**（beta>0 时 expected<0），
        策略被悄悄阉割成了「只做多」。

        返回 (是否放行, 原因)。
        """
        sc = self.strategy_cfg
        if not sc.cost_gate_enabled:
            return True, "cost_gate_disabled"

        notional = margin * leverage
        if notional <= 0:
            return False, "名义价值非法"

        if expected_return is None:
            # 在线校准尚未就绪（返回 None = 未知，而不是 0）。
            if sc.require_calibration:
                return False, ("成本门槛未过：在线校准尚未就绪，"
                               "暂不下注（require_calibration=True）")
            # 退回命名常数假设：这是"还没学到东西时的保守默认"。
            # 若这里也拒绝，策略可能永远等不到第一笔交易（见配置注释）。
            exp_ratio = abs(score) * sc.score_to_return
            src = "常数假设"
        else:
            if expected_return * score <= 0:
                return False, (
                    "成本门槛未过：在线校准的斜率非正"
                    "（信号方向无预测力），不下注")
            exp_ratio = abs(expected_return)
            src = "在线校准"

        if exp_ratio <= 0:
            return False, (f"成本门槛未过：{src}的预期收益非正"
                           f"（{exp_ratio*10000:+.2f} bp），不下注")

        expected_gross = exp_ratio * notional
        cost = self.round_trip_cost(margin, leverage)
        required = cost * sc.cost_multiple

        if expected_gross < required:
            return False, (
                f"成本门槛未过：预期毛利 ${expected_gross:.2f} "
                f"< 成本 ${cost:.2f} × {sc.cost_multiple:.1f} "
                f"= ${required:.2f}（{src}）")
        return True, "cost_gate_ok"

    def no_edge_warning(self, weights: dict[str, float]) -> bool:
        """因子权重是否全为零（= 系统认为当前没有任何可用信号）。

        **不要**用"权重是否均匀"来判断无信号。
        权重被归一化为 Σ|w| = 1，多个因子同时显著时权重天然分散，
        那是**强信号**；只有全部为零才代表学习器没找到任何 edge。
        （早期版本用"等权"判据，导致显著因子越多越容易被禁止开仓，
         方向完全反了 —— 详见 LiveConfig.skip_when_no_edge 的注释。）
        """
        return max((abs(v) for v in weights.values()), default=0.0) <= 1e-9

    # -- 仓位规模 -----------------------------------------------------------
    def size_position(self, signal: Signal, equity: float, cash: float,
                      open_count: int, price: float,
                      news_active: bool = False) -> RiskDecision:
        """波动率目标仓位：单笔风险固定为权益的 risk_per_trade。

        推导：
          止损距离 d = ATR * atr_stop_multiple
          单位价格变动对 notional 的盈亏比例 ≈ d / price
          允许亏损额 L = equity * risk_per_trade
          notional = L / (d / price) = L * price / d
          margin   = notional / leverage
        再受 max_margin_fraction 与可用现金二次约束。

        新闻期（公开信息显示不确定性升高）额外收紧仓位。
        """
        cfg = self.cfg
        if self.halted:
            return RiskDecision(False, reason=f"已熔断：{self.halt_reason}")
        if open_count >= cfg.max_positions:
            return RiskDecision(False, reason=f"持仓数已达上限 {cfg.max_positions}")
        if signal.atr_abs is None:
            return RiskDecision(False, reason="ATR 不可用，无法标定风险")
        if price <= 0:
            return RiskDecision(False, reason="价格非法")

        # ATR 可能因行情源只提供报价而退化为 0，此时用价格比例兜底
        atr_value = signal.atr_abs if signal.atr_abs > 0 else price * 0.001
        d = atr_value * cfg.atr_stop_multiple            # 止损距离（价格单位）
        if d <= 0:
            return RiskDecision(False, reason="止损距离非法")

        # 止损距离下限保护。
        #
        # 在只有报价、没有高低价的行情源里（例如游戏接口只给当前价），
        # ATR 会退化为「相邻报价差」，可能小到接近 0。
        # 此时按 ATR 反推的仓位会爆炸式放大，必须设置合理下限。
        #
        # 下限取当前价的 0.5%：这是通用的最小风险单位，
        # 保证止损距离不会小到「永远触发不了」而使仓位失控。
        # 注意这是一个保守的风险尺度选择，不是对某个市场波动的假设。
        min_distance = price * 0.005
        d = max(d, min_distance)

        risk_budget_ratio = cfg.risk_per_trade
        if news_active:
            risk_budget_ratio *= 0.5                     # 新闻期风险预算减半

        loss_budget = equity * risk_budget_ratio
        notional = loss_budget * price / d
        margin = notional / cfg.leverage

        # 约束 1：单笔保证金不超过权益的 max_margin_fraction
        margin = min(margin, equity * cfg.max_margin_fraction)

        # 约束 2：可用现金（含手续费）
        fee_ratio = trade_fee(1.0, cfg.leverage) / 1.0
        affordable = cash / (1.0 + fee_ratio)
        margin = min(margin, affordable * 0.95)

        if margin < MIN_MARGIN:
            return RiskDecision(False, reason=f"可用资金不足（需≥${MIN_MARGIN}）")

        leverage = min(cfg.leverage, MAX_LEVERAGE)

        # 成本门槛：预期毛利必须显著超过开+平的往返成本。
        #
        # 放在最后检查，是因为它需要知道最终仓位规模（margin/leverage）
        # 才能算出成本。前面算出的仓位已经是"能开的最大仓位"，
        # 这里再问一句：这点信号强度，值不值得付这个成本？
        gate_ok, gate_reason = self.cost_gate_ok(
            margin, leverage, signal.score,
            expected_return=getattr(signal, "expected_return", None))
        if not gate_ok:
            return RiskDecision(False, reason=gate_reason)

        side = 1 if signal.score > 0 else -1
        stop = price - side * d
        take = price + side * d * (cfg.atr_take_multiple / cfg.atr_stop_multiple)

        return RiskDecision(True, margin=round(margin, 2), leverage=leverage,
                            stop_price=stop, take_price=take, reason="ok")

    # -- 止损止盈与追踪 -----------------------------------------------------
    def profit_guard(self, pnl: float, peak_pnl: float,
                     margin: float) -> str | None:
        """盈利保护（回吐锁）：浮盈曾经可观，就不许它再变回亏损。

        返回离场理由；`None` 表示继续持有。

        === 为什么需要它 ===
        原来的离场阶梯里，向上的一端是"浮盈达保证金 60% 才启动追踪止盈"。
        实测浮盈峰值中位数只有保证金的 3.5% —— 这个门槛从来没被触发过，
        追踪止盈是一段**死代码**。于是乎离场只剩信号反转与超时，
        而这两者都倾向于在价格已经往回走之后才发生，
        表现为「本来盈利，拖到亏损」。

        === 规则 ===
        记 r = 浮盈 / 保证金（自我标定，与杠杆、仓位大小无关）。
        一旦峰值 peak_r ≥ arm，就把离场线设在
            floor = max(0, peak_r × (1 − giveback))
        当 r 跌回 floor 即离场。
        下限钳到 0 意味着**保本**：赚过的单子最差平价走，不会再亏。

        === 为什么不用 ATR ===
        本游戏行情源只提供单一报价，没有高低价，ATR 会退化成
        |相邻报价差| 的均值，用它定止盈距离不可靠。
        保证金比例口径不依赖任何波动率估计，在稀疏数据下同样成立。
        """
        cfg = self.cfg
        if margin <= 0 or cfg.profit_guard_arm <= 0:
            return None
        peak_r = peak_pnl / margin
        if peak_r < cfg.profit_guard_arm:
            return None
        floor_r = peak_r * (1.0 - cfg.profit_guard_giveback)
        if floor_r < 0.0:
            floor_r = 0.0                 # 不低于保本
        r = pnl / margin
        if r <= floor_r:
            return (f"回吐保护(峰值{peak_r:+.1%}→当前{r:+.1%}"
                    f"，锁{floor_r:+.1%})")
        return None

    def update_trailing(self, pos: Position, price: float) -> tuple[bool, str]:
        """检查是否需要因止损/止盈/追踪止损/盈利保护而平仓。

        返回 (should_close, reason)。
        """
        cfg = self.cfg
        pnl = pos.pnl(price)
        if pnl > pos.peak_pnl:
            pos.peak_pnl = pnl

        # 灾难止损 / 硬止盈
        if pos.stop_price is not None:
            if (pos.side == 1 and price <= pos.stop_price) or \
               (pos.side == -1 and price >= pos.stop_price):
                return True, "stop_loss"
        if pos.take_price is not None:
            if (pos.side == 1 and price >= pos.take_price) or \
               (pos.side == -1 and price <= pos.take_price):
                return True, "take_profit"

        # 盈利保护：优先于 ATR 追踪，且不受 ATR 是否可用影响。
        guard = self.profit_guard(pnl, pos.peak_pnl, pos.margin)
        if guard:
            return True, guard

        # 追踪止损：浮盈达到「初始止损距离对应的收益」的 trailing_start 倍后启动
        if pos.atr_at_open and pos.atr_at_open > 0 and pos.entry > 0:
            initial_risk = pos.atr_at_open * cfg.atr_stop_multiple      # 价格距离
            trigger_pnl = initial_risk * pos.notional / pos.entry       # 对应美元盈亏
            if pos.peak_pnl >= trigger_pnl * cfg.trailing_start:
                pos.trailing_active = True

            if pos.trailing_active:
                trail_distance = pos.atr_at_open * cfg.trailing_step
                if pos.side == 1:
                    new_stop = price - trail_distance
                    if pos.stop_price is None or new_stop > pos.stop_price:
                        pos.stop_price = new_stop
                else:
                    new_stop = price + trail_distance
                    if pos.stop_price is None or new_stop < pos.stop_price:
                        pos.stop_price = new_stop

        return False, ""

    def liquidation_buffer_ok(self, pos: Position, price: float) -> bool:
        """该仓位距爆仓线是否仍保有足够缓冲。"""
        ratio = pos.risk_ratio(price)
        return ratio <= (1.0 - self.cfg.min_liquidation_buffer) * LIQUIDATION_RATIO

    def should_force_exit(self, pos: Position, price: float, tick: int) -> tuple[bool, str]:
        """强制退出条件汇总。"""
        if pos.opened_tick >= 0 and (tick - pos.opened_tick) >= self.cfg.max_holding_bars:
            return True, "timeout"
        if not self.liquidation_buffer_ok(pos, price):
            return True, "risk_buffer"
        return False, ""
