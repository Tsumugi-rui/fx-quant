"""实时交易循环（live 模式）。

引擎的在线版本：持续从执行后端读取状态，计算信号，下单与平仓。

=== 与回测的区别 ===
回测用历史数据一次跑完；live 是等真实的行情推进，
每次循环只处理"此刻"的信息。时间线纪律同样适用：
信号只能基于已经收到的行情，不能预测下一跳。

=== 数据可获得性的诚实说明 ===
游戏接口只提供「当前报价」，不提供逐根 K 线。
因此 live 模式每轮把最新报价作为一个价格点推入指标。
这意味着 live 的因子输入比回测稀疏（少一个维度：高低价），
ATR 会退化为 |相邻报价差| 的均值。
这不是作弊，而是在可用数据下的正常降级 —— 程序不虚构拿不到的数据。

=== 安全开关（实盘必须）===
- max_loss_pct : 累计亏损超过该比例即停机
- max_trades   : 交易笔数上限，防止失控刷单
- dry_run      : 只记录意图不下单
停机后不会自动恢复，需要人工检查。
"""

from __future__ import annotations

import statistics
import time
from dataclasses import dataclass, field
from datetime import datetime

from .adaptive import FACTOR_NAMES
from .config import PAIR_IDS, EngineConfig
from .execution import DryRunExecution, Execution, GameState
from .risk import RiskManager
from .strategy import SignalEngine


@dataclass
class LiveConfig:
    poll_interval: float = 1.5         # 与游戏行情节奏一致
    max_loss_pct: float = 0.20         # 累计亏损停机线
    target_profit_pct: float = 0.0     # 目标盈利率，达标即停机（0=不设）
    max_trades: int = 200              # 交易笔数上限
    dry_run: bool = False              # 只演练不下单
    max_ticks: int = 0                 # 0 表示无限运行
    verbose: bool = True
    warmup_points: int = 80            # 开局预热点数
    max_holding_ticks: int = 240       # 持仓超时轮数（约 6 分钟 @1.5s）
    # 同一货币对两次开仓之间的最小间隔轮数。
    # 用于防止「下单后持仓列表尚未刷新」导致的重复开仓。
    entry_cooldown_ticks: int = 5

    # 「无 edge 不开仓」：学习器尚未承认任何因子的预测力时，不开仓。
    #
    # === 这里曾有一个方向完全反了的 bug ===
    # 早期版本判断的是「因子权重是否退化为等权」：
    #     if max(weights) <= equal_weight_ceiling(0.38): 不开仓
    # 这个判据在当时是合理的（旧版权重初始化就是等权，等权 = 没学到东西）。
    #
    # 但后来权重改成了「无 edge 时全部归零」，并且归一化为 Σ|w| = 1。
    # 于是 4 个因子时"等权"是 0.25 —— 结果是
    # **显著因子越多、权重越分散，就越容易被判成"等权 = 没信号"而禁止开仓**。
    # 实测后果：在结构最强的市场上（4 个因子全部显著，
    # t 值 2.7/8.0/-8.6/2.5），1500 轮里只发出 3 次开仓请求，
    # 且全部被挡下 —— 策略在最该交易的市场上完全不交易。
    #
    # 正确判据是「权重是否全为零」（= 学习器认为没有任何 edge），
    # 与权重是否均匀无关。
    skip_when_no_edge: bool = True
    no_edge_epsilon: float = 1e-9         # 权重绝对值全都 ≤ 此值视为无 edge

    # 决策雷达：每 N **秒**（墙钟时间）打印一次「策略此刻在想什么」。
    #
    # 为什么需要它：`best_from_cache` 在 |score| < entry_score 时直接返回 None，
    # `_consider_entry` 随即静默 return —— 于是长时间不开仓时，
    # 界面上是一片空白，完全看不出「是没信号、还是信号不够强、还是被风控挡了」。
    # 雷达把这些内部状态显式打出来。设为 0 可关闭。
    #
    # 输出内容（不只是"等什么"，还有"为什么"）：
    #   1. 每个持仓：方向、入场→现价、浮盈占保证金比例、峰值、回吐锁是否激活，
    #      以及**当前信号是否仍支持这个方向**（回答"为什么还拿着"）。
    #   2. 开仓扫描：最强标的、做多还是做空、**逐因子贡献分解**
    #      （各项之和恰等于最终 score，回答"为什么看多/看空"）、以及被哪道闸门挡住。
    #   3. 学习器状态：因子权重、校准 β、各因子样本数。
    #
    # 用**秒**而不是轮数：轮数依赖 poll_interval，改一次轮询间隔
    # 就要重算一遍"到底是几分钟报一次"。30 秒就写 30。
    radar_seconds: float = 30.0


@dataclass
class LiveStats:
    ticks: int = 0
    opens: int = 0
    closes: int = 0
    errors: int = 0
    start_equity: float = 0.0
    last_equity: float = 0.0
    stop_reason: str = ""
    log: list[str] = field(default_factory=list)

    def note(self, message: str) -> None:
        self.log.append(f"[{datetime.now():%H:%M:%S}] {message}")


class LiveTrader:
    """实时交易循环。"""

    def __init__(self, execution: Execution, engine_config: EngineConfig,
                 live_config: LiveConfig | None = None) -> None:
        self.cfg = live_config or LiveConfig()
        self.engine_cfg = engine_config
        self.execution: Execution = (
            DryRunExecution(execution) if self.cfg.dry_run else execution)

        self.signals = SignalEngine(engine_config)
        self.risk = RiskManager(engine_config.risk, engine_config.strategy)
        self.stats = LiveStats()

        self._halted = False
        self._tick = 0
        self._prices: dict[str, float] = {}
        self._peak_pnl: dict[str, float] = {}
        self._seen_at: dict[str, int] = {}
        # 每个货币对上次开仓的轮次，用于重复开仓冷却
        self._last_entry: dict[str, int] = {}
        # 连续因「无信号」而未开仓的轮数，用于限流日志
        self._no_signal_ticks = 0
        # 决策雷达用的最近一次扫描结果
        self._scan_pair: str | None = None
        self._scan_score: float = 0.0
        self._scan_note: str = "尚未开始"
        # 上一次打雷达的墙钟时刻（0.0 表示还没打过，首次立即输出一次）
        self._last_radar_at: float = 0.0

        # 离场统计：每笔的浮盈峰值与留存（都按占保证金的比例记），
        # 以及离场原因计数。
        #
        # 为什么要收集这个：`profit_guard_arm` 该取多少，**取决于盘口的
        # 实际波动尺度**，而这个尺度只有真实游戏才给得出答案
        # （合成市场的波动是我们自己设的，拿它定参数就是自欺）。
        # 收尾时把这些数打出来，玩家跑一段真实行情就知道该调到哪。
        self._margin_of: dict[str, float] = {}
        self._peak_ratios: list[float] = []
        self._retain_ratios: list[float] = []
        self._exit_reasons: dict[str, int] = {}

    # ------------------------------------------------------------------
    def _say(self, message: str) -> None:
        self.stats.note(message)
        if self.cfg.verbose:
            print(f"[{datetime.now():%H:%M:%S}] {message}", flush=True)

    def stop(self) -> None:
        self._halted = True

    # ------------------------------------------------------------------
    def run(self) -> LiveStats:
        self._say(f"执行后端：{self.execution.describe()}")
        if not self.execution.is_available():
            self._say("后端不可用，终止。请检查游戏页面与桥接服务是否就绪。")
            self.stats.stop_reason = "后端不可用"
            return self.stats

        state = self._read_state()
        if state is None:
            self.stats.stop_reason = "读取状态失败"
            return self.stats

        self.stats.start_equity = state.equity
        self.stats.last_equity = state.equity
        self._prices = dict(state.prices)
        self.risk.on_new_day(state.equity)
        self._say(f"初始权益 ${state.equity:,.2f}｜模式 {state.mode}"
                  f"｜持仓 {len(state.positions)}")

        self._warmup(state)

        while not self._halted:
            if self.cfg.max_ticks and self._tick >= self.cfg.max_ticks:
                self.stats.stop_reason = "达到设定的时点数"
                break

            state = self._read_state()
            if state is None:
                time.sleep(self.cfg.poll_interval)
                continue

            self._tick += 1
            self.stats.ticks += 1
            self.stats.last_equity = state.equity
            self._prices = dict(state.prices)

            self._push_bars(state)          # 推进指标
            self._observe()                 # 自学习
            self._manage_positions(state)   # 风控
            self._consider_entry(state)     # 开仓
            self._radar(state)              # 空仓期也能看清在等什么

            if self._check_kill_switch(state):
                break

            time.sleep(self.cfg.poll_interval)

        if not self.stats.stop_reason:
            self.stats.stop_reason = "主动停止"
        self._summarize()
        return self.stats

    # ------------------------------------------------------------------
    def _read_state(self) -> GameState | None:
        try:
            return self.execution.state()
        except Exception as exc:
            self.stats.errors += 1
            self._say(f"读取状态失败：{exc}")
            return None

    def _warmup(self, state: GameState) -> None:
        """用当前报价预热指标，避免开局因样本不足无法交易。"""
        for _ in range(self.cfg.warmup_points):
            for pair, price in state.prices.items():
                if price > 0:
                    self.signals.push_bar(pair, price, price, price)
        self._say(f"指标已预热 {self.cfg.warmup_points} 点")

    def _push_bars(self, state: GameState) -> None:
        """把当前报价作为一个价格点推入指标。

        游戏只提供当前价，没有高低价，因此 high=low=close。
        这是合法输入：它就是此刻的公开报价。
        """
        for pair, price in state.prices.items():
            if price > 0:
                self.signals.push_bar(pair, price, price, price)

    def _observe(self) -> None:
        cache = self.signals.factor_values_all()
        for pair, values in cache.items():
            price = values.get("close", 0.0)
            for name in FACTOR_NAMES:
                # 传原始取值（不是符号）：学习器用 IC 口径，需要幅度信息。
                self.signals.weighter.record(
                    name, pair, values[name], price, self._tick)
            # 同步登记合成信号，供成本门槛的在线校准使用。
            # 缺少这一步会导致校准永远不就绪、门槛把所有交易都挡掉。
            sig = self.signals._compose(pair, values, False)
            self.signals.calibrator.record(sig.score, pair, price, self._tick)
        lookup = lambda p: self._prices.get(p, 0.0)  # noqa: E731
        self.signals.weighter.resolve(self._tick, lookup)
        self.signals.calibrator.resolve(self._tick, lookup)

    # ------------------------------------------------------------------
    def _manage_positions(self, state: GameState) -> None:
        cache = self.signals.factor_values_all()
        weights = self.signals.weighter.weights
        norm = sum(abs(weights[n]) for n in FACTOR_NAMES) or 1.0

        for pos in list(state.positions):
            price = state.prices.get(pos.pair)
            if price is None or pos.entry <= 0:
                continue
            self._margin_of[pos.id] = pos.margin
            side = 1 if pos.side == "long" else -1
            notional = pos.margin * pos.leverage
            pnl = notional * side * (price / pos.entry - 1)
            risk_ratio = max(0.0, -pnl / pos.margin) if pos.margin else 0.0

            peak = max(self._peak_pnl.get(pos.id, 0.0), pnl)
            self._peak_pnl[pos.id] = peak

            # 记录首次见到该持仓的轮次，用于超时退出
            first_seen = self._seen_at.setdefault(pos.id, self._tick)
            age = self._tick - first_seen

            # 1. 灾难止损：浮亏达保证金 hard_stop_ratio 就砍。
            #    兜底手段，不是主要离场路径。
            if risk_ratio >= self.risk.cfg.hard_stop_ratio:
                self._close_position(
                    pos.id, pos.pair,
                    f"止损(亏损{risk_ratio:.0%}保证金)")
                continue

            # 2. 盈利保护（回吐锁）：赚过钱就不许再变亏损单。
            #
            # === 这里原来是「浮盈达保证金 60% 后回撤 25% 才走」===
            # 实测浮盈峰值中位数只有保证金的 3.5%，那个门槛从未被触发，
            # 等于没有止盈 —— 离场只剩信号反转和超时，
            # 结果就是「本来盈利，拖到亏损」。详见 RiskManager.profit_guard。
            guard = self.risk.profit_guard(pnl, peak, pos.margin)
            if guard:
                self._close_position(pos.id, pos.pair, guard)
                continue

            # 3. 超时退出：持仓超过设定轮数仍未触发任何条件，释放保证金
            if age >= self.cfg.max_holding_ticks:
                self._close_position(pos.id, pos.pair, f"持仓超时({age}轮)")
                continue

            # 4. 信号反转：当前信号方向与持仓相反且强度足够时离场
            values = cache.get(pos.pair)
            if values is not None:
                weighted = sum(values[n] * weights[n]
                               for n in FACTOR_NAMES)
                score = weighted / norm
                score *= 1.0 + values["volatility"] * 0.55
                opposite = (score < 0) if side == 1 else (score > 0)
                if opposite and abs(score) >= self.engine_cfg.strategy.entry_score:
                    self._close_position(
                        pos.id, pos.pair, f"信号反转(score={score:+.2f})")

    def _consider_entry(self, state: GameState) -> None:
        # 每一轮都重置扫描结果，保证雷达反映的是「刚刚这一轮」的实况。
        self._scan_pair = None
        self._scan_score = 0.0
        self._scan_note = ""

        if self.risk.halted or state.equity <= 0:
            self._scan_note = "风控已熔断"
            return
        if len(state.positions) >= self.engine_cfg.risk.max_positions:
            self._scan_note = (f"持仓已满（{len(state.positions)}/"
                               f"{self.engine_cfg.risk.max_positions}）")
            return
        if self.stats.opens >= self.cfg.max_trades:
            self._say(f"已达交易笔数上限 {self.cfg.max_trades}，停止开仓")
            self.stats.stop_reason = "达到交易笔数上限"
            self._halted = True
            return

        cache = self.signals.factor_values_all()
        signals = self.signals.signals_from_cache(None, cache)
        if not signals:
            self._scan_note = "指标样本不足，尚未产出信号"
            return

        # 取全市场「最强」的那个信号（不设阈值），这样即使全部不达标，
        # 雷达也能告诉你「最强的一个离门槛还差多少」。
        top = max(signals.values(), key=lambda s: abs(s.score))
        self._scan_pair, self._scan_score = top.pair, top.score
        threshold = self.engine_cfg.strategy.entry_score

        if abs(top.score) < threshold:
            self._scan_note = (f"信号强度未达阈值"
                               f"（|{top.score:.2f}| < {threshold:.2f}）")
            return

        if state.find_position(top.pair) is not None:
            self._scan_note = f"已持有 {top.pair}，不重复开仓"
            return

        # 「无 edge 不开仓」保护。
        #
        # 权重全为零，意味着学习器没能找到任何有统计显著性的预测因子 ——
        # 系统在诚实地说"这个市场现在没有可识别的信号"。
        # 此时开仓是纯赌博：期望毛收益≈0，但成本是确定的，每笔都是负期望。
        #
        # 注意判据是「是否全为零」，不是「是否均匀」——
        # 多个因子同时显著时权重天然分散，那是**强信号**，不是无信号。
        if self.cfg.skip_when_no_edge:
            weights = self.signals.weighter.weights
            max_w = max((abs(v) for v in weights.values()), default=0.0)
            if max_w <= self.cfg.no_edge_epsilon:
                self._no_signal_ticks += 1
                self._scan_note = "无 edge：权重全为零，等学习器确认优势"
                if self._no_signal_ticks % 40 == 1:
                    self._say("暂不开仓：尚无任何因子通过显著性检验"
                              "（权重全为零），等待有效信号")
                return
        self._no_signal_ticks = 0

        # 重复开仓保护。
        #
        # 下单后页面/后端渲染持仓列表存在延迟，若某一轮读到「该货币对无持仓」，
        # 引擎会误判为可以再开一笔，导致对同一货币对连续重复下单。
        # 因此对每个货币对记录上次下单时点，在冷却窗口内禁止再开。
        last = self._last_entry.get(top.pair, -10_000)
        if self._tick - last < self.cfg.entry_cooldown_ticks:
            self._scan_note = f"{top.pair} 在开仓冷却中"
            return

        price = state.prices.get(top.pair, 0.0)
        if price <= 0:
            self._scan_note = f"{top.pair} 无有效报价"
            return

        decision = self.risk.size_position(
            top, state.equity, state.cash, len(state.positions), price)
        if not decision.approved:
            self._scan_note = f"风控否决：{decision.reason}"
            return

        side = "long" if top.score > 0 else "short"
        result = self.execution.place(
            top.pair, side, decision.margin, decision.leverage)
        if result.ok:
            self.stats.opens += 1
            self._last_entry[top.pair] = self._tick
            self._scan_note = "已开仓"
            tag = "干跑" if self.cfg.dry_run else "成交"
            self._say(f"开仓{tag} {top.pair} {side} score={top.score:+.2f} "
                      f"保证金=${decision.margin:.2f} 杠杆={decision.leverage}x "
                      f"入场={result.entry:.5f}")
        else:
            self.stats.errors += 1
            self._scan_note = f"下单被拒：{result.error}"
            self._say(f"开仓失败 {top.pair}：{result.error}")

    # ------------------------------------------------------------------
    @staticmethod
    def _decompose(sig) -> str:
        """把最终 score 拆成各因子的可加贡献，**各项之和恰等于该 score**。

        合成公式（见 `SignalEngine._compose`）：
            score = (Σ 因子值×权重 / Σ|权重|) × (1 + 波动率×0.55)
        把 vol_damp 分摊到每一项，就得到贡献：
            contrib_n = 因子值_n × 权重_n / Σ|权重| × vol_damp
            Σ contrib_n ≡ score
        因此这行不是"事后解释"，而是**决策本身的来源**：
        哪一项在推、哪一项在拖、净额是多少，一目了然。
        """
        weights = sig.weights
        norm = sum(abs(weights[n]) for n in FACTOR_NAMES) or 1.0
        vol_damp = 1.0 + sig.factors.get("volatility", 0.0) * 0.55
        parts = []
        for n in FACTOR_NAMES:
            c = sig.factors.get(n, 0.0) * weights[n] / norm * vol_damp
            if abs(c) < 5e-3:          # 贡献小到看不见，略过以保持可读
                continue
            parts.append((abs(c), f"{n[:4]}{c:+.2f}"))
        if not parts:
            return "各因子贡献均≈0"
        parts.sort(reverse=True)
        return " ".join(s for _, s in parts) + f" → 净 {sig.score:+.2f}"

    def _explain_position(self, pos, state: GameState, sig) -> None:
        """说明「为什么还拿着这笔仓」：方向、盈亏、保护线、信号是否还支持。"""
        price = state.prices.get(pos.pair)
        if price is None or pos.entry <= 0 or pos.margin <= 0:
            return
        side = 1 if pos.side == "long" else -1
        dir_cn = "做多" if side == 1 else "做空"
        notional = pos.margin * pos.leverage
        pnl = notional * side * (price / pos.entry - 1)
        peak = self._peak_pnl.get(pos.id, 0.0)
        r = pnl / pos.margin
        peak_r = peak / pos.margin

        rc = self.risk.cfg
        if peak_r >= rc.profit_guard_arm:
            floor_r = max(0.0, peak_r * (1.0 - rc.profit_guard_giveback))
            lock = f"回吐锁已激活：锁 {floor_r:+.2%} 保证金，跌回即离场"
        else:
            need = rc.profit_guard_arm - peak_r
            lock = (f"回吐锁未激活：浮盈还需再增 {need:.2%} 保证金"
                    f"（达 {rc.profit_guard_arm:.1%}）才开始锁")

        self._say(f"  ▸ 持仓 {pos.pair} {dir_cn} {pos.leverage}x"
                  f"｜入场 {pos.entry:.5f} → 现价 {price:.5f}"
                  f"｜浮盈 ${pnl:+.2f}（{r:+.2%} 保证金，峰值 {peak_r:+.2%}）")
        self._say(f"      {lock}")

        if sig is None:
            self._say("      当前该标的还没算出信号（样本不足），按原计划持有")
            return
        thr = self.engine_cfg.strategy.entry_score
        agree = (sig.score > 0) == (side == 1)
        if agree and abs(sig.score) >= thr:
            verdict = "信号仍支持该方向 → 继续持有"
        elif agree:
            verdict = (f"信号转弱（|{sig.score:.2f}| < {thr:.2f}）"
                       f"→ 不再加仓，交给回吐锁/超时收尾")
        elif abs(sig.score) >= thr:
            verdict = "信号已反向且够强 → 本轮触发反转平仓"
        else:
            verdict = f"信号已反向但强度不足（{sig.score:+.2f}）→ 暂不动"
        rel = "同向" if agree else "已反向"
        self._say(f"      当前 score {sig.score:+.2f}"
                  f"（对{dir_cn}单{rel}）→ {verdict}")
        self._say(f"      因子贡献 {self._decompose(sig)}")

    def _radar(self, state: GameState) -> None:
        """定期汇报「策略此刻在想什么」，让运行期不再是黑箱。

        按**墙钟时间**节流（radar_seconds 秒一次），不按轮数：
        轮数依赖 poll_interval，行情快慢一变口径就漂了。
        首次调用立即输出一次，方便刚启动时就能确认策略活着。
        """
        if self.cfg.radar_seconds <= 0:
            return
        now = time.monotonic()
        if self._last_radar_at and (now - self._last_radar_at) < self.cfg.radar_seconds:
            return
        self._last_radar_at = now

        st = self.engine_cfg.strategy
        rc = self.engine_cfg.risk
        threshold = st.entry_score
        weights = self.signals.weighter.weights
        start = self.stats.start_equity
        chg = (state.equity - start) / start if start else 0.0

        self._say("·" * 54)
        self._say(f"雷达 {self.cfg.radar_seconds:.0f}s｜轮 {self._tick}"
                  f"｜权益 ${state.equity:,.2f}（{chg:+.2%}）"
                  f"｜持仓 {len(state.positions)}/{rc.max_positions}")
        self._say(f"  风控｜止损 亏{rc.hard_stop_ratio:.0%}保证金"
                  f"｜回吐锁 浮盈{rc.profit_guard_arm:.1%}起、最多回吐"
                  f"{rc.profit_guard_giveback:.0%}｜开仓门槛 |score|≥"
                  f"{threshold:.2f}｜成本门槛 {st.cost_multiple:.1f}×")

        # 当前全市场信号（持仓解读与开仓扫描共用一次计算）
        cache = self.signals.factor_values_all()
        signals = self.signals.signals_from_cache(None, cache)

        # ---- 1. 持仓：为什么还拿着 ----
        if state.positions:
            for pos in state.positions:
                self._explain_position(pos, state, signals.get(pos.pair))
        else:
            self._say("  ▸ 无持仓")

        # ---- 2. 开仓扫描：为什么开 / 为什么不开 ----
        if self._scan_pair is None:
            self._say(f"  ▸ 开仓扫描：{self._scan_note or '—'}")
        else:
            direction = "做多" if self._scan_score > 0 else "做空"
            passed = abs(self._scan_score) >= threshold
            self._say(f"  ▸ 开仓扫描 {self._scan_pair} score "
                      f"{self._scan_score:+.2f}（门槛 {threshold:.2f}）"
                      f"→ 方向 {direction}{'' if passed else '（未过阈值）'}")
            sig = signals.get(self._scan_pair)
            if sig is not None:
                self._say(f"      因子贡献 {self._decompose(sig)}")
            self._say(f"      → {self._scan_note or '—'}")

        # ---- 3. 学习器状态：它现在信谁 ----
        held = [f"{n}={weights[n]:+.2f}" for n in FACTOR_NAMES
                if abs(weights[n]) > self.cfg.no_edge_epsilon]
        wpart = " ".join(held) if held else "全零（学习器尚未确认任何 edge）"
        cal = self.signals.calibrator
        if cal.ready:
            calib = f"{cal.beta:+.3e}（非零 {cal.nonzero_count()}）"
        else:
            calib = (f"未就绪（非零 {cal.nonzero_count()}"
                     f"/{st.calib_min_nonzero}）")
        self._say(f"  ▸ 学习器 权重 {wpart}｜校准 β={calib}")

    def _close_position(self, position_id: str, pair: str, reason: str) -> None:
        margin = self._margin_of.get(position_id, 0.0)
        peak = self._peak_pnl.get(position_id, 0.0)
        result = self.execution.close(position_id)
        if result.ok:
            self.stats.closes += 1
            self._peak_pnl.pop(position_id, None)
            self._seen_at.pop(position_id, None)
            self._margin_of.pop(position_id, None)
            # 记一笔「本笔浮盈峰值 / 最终留存」，收尾时汇报真实盈亏尺度。
            self._exit_reasons[reason.split("(")[0]] = (
                self._exit_reasons.get(reason.split("(")[0], 0) + 1)
            if margin > 0:
                self._peak_ratios.append(peak / margin)
                self._retain_ratios.append(result.pnl / margin)
            self._say(f"平仓 {pair}（{reason}）盈亏 ${result.pnl:+.2f}")
        else:
            self.stats.errors += 1
            self._say(f"平仓失败 {pair}：{result.error}")

    # ------------------------------------------------------------------
    def _check_kill_switch(self, state: GameState) -> bool:
        if self.stats.start_equity <= 0:
            return False

        # 目标止盈：达到设定收益率就停机并平掉所有持仓。
        # 先判止盈再判止损 —— 两者同轮触发时以止盈为准（好事优先）。
        if self.cfg.target_profit_pct > 0:
            gain = (state.equity - self.stats.start_equity) / self.stats.start_equity
            if gain >= self.cfg.target_profit_pct:
                self._say(f"达到目标盈利 {gain:+.2%} "
                          f">= {self.cfg.target_profit_pct:.2%}，收工平仓")
                self._flatten_all(state)
                self.stats.stop_reason = "达到目标盈利"
                self._halted = True
                return True

        drawdown = (self.stats.start_equity - state.equity) / self.stats.start_equity
        if drawdown >= self.cfg.max_loss_pct:
            self._say(f"触发亏损停机线：累计回撤 {drawdown:.2%} "
                      f">= {self.cfg.max_loss_pct:.2%}")
            self.stats.stop_reason = "触发亏损停机线"
            self._halted = True
            return True
        return False

    def _flatten_all(self, state: GameState) -> None:
        """平掉全部持仓（停机前落袋）。"""
        for pos in list(state.positions):
            self._close_position(pos.id, pos.pair, "目标达成收工")


    def _summarize(self) -> None:
        s = self.stats
        self._say(f"循环结束：{s.stop_reason}")
        self._say(f"共 {s.ticks} 轮 · 开仓 {s.opens} · 平仓 {s.closes} · "
                  f"错误 {s.errors}")
        if s.start_equity:
            ret = s.last_equity / s.start_equity - 1
            self._say(f"期末权益 ${s.last_equity:,.2f}（{ret:+.2%}）")
        weights = self.signals.weighter.weights
        parts = "  ".join(f"{k}={v:.2f}" for k, v in sorted(weights.items()))
        self._say(f"因子权重：{parts}")

        # 真实盈亏尺度 —— 用来校准盈利保护参数。
        #
        # `--profit-arm` 没有普适最优值：它取决于这个游戏每跳的实际波动。
        # 跑一段真实行情，看这里的两个比例：
        #   峰值中位数  → 一笔单子典型能浮盈到保证金的百分之几
        #   留存中位数  → 实际落袋的是百分之几
        # 若"留存"远小于"峰值"，说明回吐太多，把 --profit-arm 调小、
        # 或把 --profit-giveback 调小（更早落袋）。
        if self._peak_ratios:
            pk = statistics.median(self._peak_ratios)
            rt = statistics.median(self._retain_ratios)
            self._say(f"盈亏尺度：浮盈峰值中位 {pk:+.1%}（均值 "
                      f"{statistics.fmean(self._peak_ratios):+.1%}，最大 "
                      f"{max(self._peak_ratios):+.1%}）｜平仓留存中位 {rt:+.1%}")
            self._say("  ↑ 若留存明显低于峰值，说明回吐过多："
                      "调小 --profit-arm（更早保护）或 --profit-giveback"
                      "（更早落袋）")
        if self._exit_reasons:
            parts = "  ".join(f"{k}×{v}" for k, v in
                              sorted(self._exit_reasons.items(),
                                     key=lambda kv: -kv[1]))
            self._say(f"离场原因：{parts}")
