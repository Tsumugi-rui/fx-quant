"""多因子信号引擎（自适应权重 + 增量指标）。

四个因子家族，全部只读「截至当前时点的已收盘 K 线」：
  1. 趋势   trend      : 快慢 EMA 乖离 + 线性斜率
  2. 动量   momentum   : ROC 与动量加速度
  3. 回归   reversion  : 布林 z-score 反向
  4. 波动率 volatility : 仅作信号强度调节，不产生方向

=== 方向权重来自在线学习，不是硬编码 ===
`SignalEngine` 不假定任何因子在目标市场上有效或无效。
它把每个因子当作候选预测器交给 `AdaptiveWeighter`，
后者依据已实现的历史表现动态分配权重。

=== 新闻事件（当下公开信息）===
市场快讯是玩家在游戏里**能实时看到**的公开信息，因此可以作为合法输入。
但本程序只把它当作「不确定性升高」的标记，用于在风控层收紧仓位，
**不解析新闻的方向性影响**（那属于程序内部实现，不可使用）。
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from operator import mul as _mul

from .adaptive import FACTOR_NAMES, AdaptiveWeighter, ScoreCalibrator
from .config import PAIR_IDS, EngineConfig, StrategyConfig
from .indicators import IncrementalIndicators


@dataclass
class Signal:
    pair: str
    score: float
    confidence: float
    factors: dict[str, float] = field(default_factory=dict)
    weights: dict[str, float] = field(default_factory=dict)
    atr_abs: float | None = None
    price: float = 0.0
    news_active: bool = False
    # 由在线校准器给出的「按该 score 方向交易」的预期收益率。
    # 供成本门槛判断"这点优势够不够覆盖交易成本"。
    #
    # 注意它是**有符号**的回归预测（price_return ~ score）：
    #   expected = beta × score
    # 而实际下单方向是 sign(score)，所以本单的预期收益是 |expected|，
    # 且仅当 beta>0（即 expected 与 score 同号）时才有正期望。
    # 这一层换算由 RiskManager.cost_gate_ok 负责。
    #
    # None 表示「校准尚未就绪，未知」—— 与"算出来是 0"是两回事，
    # 成本门槛对二者的处理不同。
    expected_return: float | None = None

    @property
    def direction(self) -> int:
        return 1 if self.score > 0 else (-1 if self.score < 0 else 0)


class FactorSet:
    """基于增量指标快照计算各因子得分，输出 (-1, 1)。"""

    @staticmethod
    def trend(snap: dict, closes: list[float], cfg: StrategyConfig) -> float | None:
        fast, slow = snap.get("ema_fast"), snap.get("ema_slow")
        slope = snap.get("slope")
        if fast is None or slow is None or slope is None or slow == 0:
            return None
        gap_score = _tanh((fast - slow) / slow / 0.0015)
        slope_score = _tanh(slope / 0.0008)
        return 0.65 * gap_score + 0.35 * slope_score

    @staticmethod
    def momentum(closes: list[float], cfg: StrategyConfig) -> float | None:
        w = cfg.roc_window
        if len(closes) < w + 1:
            return None
        past = closes[-1 - w]
        if past == 0:
            return None
        r = closes[-1] / past - 1.0
        w_fast = max(2, w // 3)
        r_fast = (closes[-1] / closes[-1 - w_fast] - 1.0) if len(closes) > w_fast else 0.0
        accel = r - r_fast
        return 0.7 * _tanh(r / cfg.roc_scale) + 0.3 * _tanh(accel / cfg.roc_scale)

    @staticmethod
    def reversion(closes: list[float], cfg: StrategyConfig) -> float | None:
        w = cfg.zscore_window
        if len(closes) < w or w < 2:
            return None
        window = closes[-w:]
        mean = sum(window) / w
        var = sum((v - mean) ** 2 for v in window) / (w - 1)
        sd = math.sqrt(var)
        if sd == 0:
            return 0.0
        z = max(-cfg.zscore_cap, min(cfg.zscore_cap, (closes[-1] - mean) / sd))
        return -_tanh(z / cfg.zscore_cap)

    @staticmethod
    def cycle(closes: list[float], cfg: StrategyConfig) -> float | None:
        """周期/谱因子：估计主导周期与相位，预测下一时点的漂移方向。

        方法（标准的周期图 periodogram）：
          1. 取最近 W 个收益，去均值。
          2. 在正交频率网格（周期 = W/k）上扫描，对每个 k 计算复相关幅度
                 C = Σ r[i]·cos(ωi),  S = Σ r[i]·sin(ωi),  ω = 2πk/W
             功率 P = (C² + S²) / W²。
          3. 取功率最大的频率，计算它解释了总方差的多少。
             若低于 cycle_min_frac，说明没有可信的周期 → 返回 0。
          4. 由 (C, S) 反解振幅与相位，外推一个时点得到预测漂移。

        数学依据：若 r[i] ≈ A·sin(ωi + φ)，则
             C ≈ (A·W/2)·sin φ,   S ≈ (A·W/2)·cos φ
        因此 A = 2√(C²+S²)/W，φ = atan2(C, S)；
        外推到 t=W 时相位前进 2πk（整数倍），故预测值 = A·sin(φ)。

        全部输入只是价格历史，**不预设任何具体周期值**。
        """
        w = cfg.cycle_window
        if len(closes) < w + 1:
            return None
        seg = closes[-w - 1:]
        if seg[0] <= 0:
            return None
        rets = [seg[i + 1] / seg[i] - 1.0 for i in range(w)]
        mu = sum(rets) / w
        rets = [r - mu for r in rets]
        var = sum(r * r for r in rets) / w
        if var <= 1e-18:
            return 0.0

        best_power = 0.0
        best_k = 0
        best_c = 0.0
        best_s = 0.0
        # 在正交频率网格上扫描：周期 = w / k。
        # 这正是离散傅里叶变换的天然格点，可避免"真实周期不在候选表上"
        # 造成的频谱泄漏（那会让本应显著的周期检测不出来）。
        k_min = max(1, int(math.ceil(w / cfg.cycle_max_period)))
        k_max = max(k_min, min(w // 2, int(w / cfg.cycle_min_period)))
        for k in range(k_min, k_max + 1):
            cos_t, sin_t = _cycle_tables(w, k)
            # 用 map + operator.mul 让内层循环留在 C 层（比生成器明显快），
            # 累加顺序不变，结果与逐元素相乘再求和完全一致。
            c = sum(map(_mul, rets, cos_t))
            s = sum(map(_mul, rets, sin_t))
            power = (c * c + s * s) / (w * w)
            if power > best_power:
                best_power, best_k, best_c, best_s = power, k, c, s

        if best_k == 0:
            return 0.0
        # 该频率解释的方差比例
        if best_power / (var + 1e-18) < cfg.cycle_min_frac:
            return 0.0

        # 反解振幅与相位。omega = 2*pi*k/w，故外推到 t=w 时
        # 相位前进 2*pi*k（整数倍），sin(2*pi*k + phi) = sin(phi)。
        amp = 2.0 * math.sqrt(best_c * best_c + best_s * best_s) / w
        phase = math.atan2(best_c, best_s)
        pred = amp * math.sin(phase)

        # 用当期波动率归一化，使该因子的量级与其他因子可比
        # （否则它太小，会被自适应机制当作无效信号而忽略）。
        scale = max(math.sqrt(var), cfg.cycle_scale)
        return _tanh(pred / scale)

    @staticmethod
    def volatility_regime(snap: dict, cfg: StrategyConfig) -> float | None:
        """波动率调节项，取值 [-1, 0]。不参与方向投票。"""
        ap = snap.get("atr_pct")
        rv = snap.get("realized_vol")
        if ap is None and rv is None:
            return None
        penalty = min(1.0, (ap or 0.0) / 0.0012)
        rv_penalty = min(1.0, (rv or 0.0) / 0.0020)
        return -max(penalty, rv_penalty) * 0.5


class SignalEngine:
    """自适应多因子合成器（增量计算）。"""

    def __init__(self, config: EngineConfig) -> None:
        self.config = config
        self.cfg = config.strategy
        self.indicators = IncrementalIndicators(
            max_window=max(self.cfg.zscore_window, self.cfg.roc_window,
                           self.cfg.slope_window, self.cfg.atr_window,
                           self.cfg.cycle_window) + 8)
        self.weighter = AdaptiveWeighter(
            lookback=self.cfg.adapt_lookback,
            min_samples=self.cfg.adapt_min_samples,
            temperature=self.cfg.adapt_temperature,
            weight_floor=self.cfg.weight_floor,
            max_weight=self.cfg.max_weight,
            eval_horizon=config.eval_horizon,
            shrinkage=self.cfg.adapt_shrinkage,
            t_threshold=self.cfg.adapt_t_threshold,
        )
        # 在线校准 score -> 预期收益，供成本门槛使用
        self.calibrator = ScoreCalibrator(
            lookback=self.cfg.calib_lookback,
            min_samples=self.cfg.calib_min_samples,
            eval_horizon=config.eval_horizon,
            min_nonzero=self.cfg.calib_min_nonzero,
        )

    # ------------------------------------------------------------------
    def min_bars(self) -> int:
        return max(self.cfg.ema_slow, self.cfg.zscore_window,
                   self.cfg.atr_window, self.cfg.slope_window) + 2

    def warmup(self, market) -> None:
        """用已有历史 K 线预热增量指标。"""
        for pair in PAIR_IDS:
            series = market.series.get(pair)
            if series is None:
                continue
            for c in series.closed_candles():
                self.indicators.update(
                    pair, c.high, c.low, c.close,
                    self.cfg.ema_fast, self.cfg.ema_slow,
                    self.cfg.atr_window, self.cfg.slope_window)

    def push_bar(self, pair: str, high: float, low: float, close: float) -> None:
        self.indicators.update(
            pair, high, low, close,
            self.cfg.ema_fast, self.cfg.ema_slow,
            self.cfg.atr_window, self.cfg.slope_window)

    # ------------------------------------------------------------------
    def factor_values(self, pair: str) -> dict[str, float] | None:
        snap = self.indicators.snapshot(pair, self.cfg.slope_window)
        if snap is None or snap.get("bars", 0) < self.min_bars():
            return None
        closes = self.indicators.closes_of(pair)

        f_t = FactorSet.trend(snap, closes, self.cfg)
        f_m = FactorSet.momentum(closes, self.cfg)
        f_r = FactorSet.reversion(closes, self.cfg)
        f_c = FactorSet.cycle(closes, self.cfg)
        f_v = FactorSet.volatility_regime(snap, self.cfg)
        if f_t is None or f_m is None or f_r is None:
            return None
        return {
            "trend": f_t, "momentum": f_m, "reversion": f_r,
            # 数据不足 cycle_window 时谱分析无法进行，该因子记 0（不产生信号）
            "cycle": f_c if f_c is not None else 0.0,
            "volatility": f_v if f_v is not None else 0.0,
            "atr": snap.get("atr") or 0.0,
            "close": snap.get("close") or closes[-1],
        }

    def factor_values_all(self, market=None) -> dict[str, dict[str, float]]:
        """计算全部标的的因子值。

        `market` 参数保留仅为兼容回测路径；live 模式不传，
        直接读取增量指标内部已推入的 K 线。
        """
        cache: dict[str, dict[str, float]] = {}
        pairs = PAIR_IDS if market is None else [
            p for p in PAIR_IDS if p in getattr(market, "series", {})]
        for pair in pairs:
            values = self.factor_values(pair)
            if values is not None:
                cache[pair] = values
        return cache

    # ------------------------------------------------------------------
    def _compose(self, pair: str, values: dict[str, float], news_active: bool) -> Signal:
        weights = self.weighter.weights
        weighted = sum(values[n] * weights[n] for n in FACTOR_NAMES)
        norm = sum(abs(weights[n]) for n in FACTOR_NAMES) or 1.0
        score = weighted / norm

        vol_damp = 1.0 + values["volatility"] * 0.55
        score *= vol_damp

        # 新闻期额外降档：不确定性升高时压低信号强度
        if news_active:
            score *= 0.6

        contribs = [values[n] * weights[n] for n in FACTOR_NAMES]
        active = [c for c in contribs if abs(c) > 1e-6]
        agree = (sum(1 for c in active if (c > 0) == (score > 0)) / len(active)) \
            if active else 0.0
        confidence = max(0.0, min(1.0, agree * vol_damp))

        score = max(-self.cfg.score_cap, min(self.cfg.score_cap, score))
        expected = self.calibrator.expected_return(score)
        return Signal(
            pair=pair, score=score, confidence=confidence,
            factors={k: v for k, v in values.items() if k != "atr"},
            weights=dict(weights), atr_abs=values.get("atr") or None,
            price=values.get("close", 0.0), news_active=news_active,
            expected_return=expected)

    def signals_from_cache(self, market, cache: dict[str, dict[str, float]],
                           news_active: bool = False) -> dict[str, Signal]:
        """合成信号。`market` 可为 None（live 模式不需要）。"""
        return {pair: self._compose(pair, values, news_active)
                for pair, values in cache.items()}

    def best_from_cache(self, market, cache, news_active: bool = False) -> Signal | None:
        signals = self.signals_from_cache(market, cache, news_active)
        if not signals:
            return None
        top = max(signals.values(), key=lambda s: abs(s.score))
        if abs(top.score) < self.cfg.entry_score:
            return None
        return top

    # ------------------------------------------------------------------
    def observe_from_cache(self, market, cache: dict[str, dict[str, float]],
                           tick: int) -> None:
        for pair, values in cache.items():
            price = values.get("close") or market.price(pair)
            for name in FACTOR_NAMES:
                # 传因子的**原始取值**而不是它的符号：下游用 IC（协方差）
                # 口径评估预测力，需要保留幅度信息。
                self.weighter.record(name, pair, values[name], price, tick)
            # 同步登记合成信号，供成本门槛的在线校准使用。
            # 含 score=0 的样本一并记录：它们不扭曲回归斜率（分子分母同比例
            # 缩放），却能让校准器更快积累到足够样本、尽早可用。
            sig = self._compose(pair, values, False)
            self.calibrator.record(sig.score, pair, price, tick)
        self.weighter.resolve(tick, market.price)
        self.calibrator.resolve(tick, market.price)


def _tanh(x: float) -> float:
    if x > 20:
        return 1.0
    if x < -20:
        return -1.0
    e2 = math.e ** (2 * x)
    return (e2 - 1) / (e2 + 1)


# ---------------------------------------------------------------------------
# 周期图的三角函数查找表
# ---------------------------------------------------------------------------
# 谱分析要在每个时点、每个标的上扫描整条频率网格，
# 逐个调用 math.cos/math.sin 会成为性能热点。
# 这里把 (窗口长度, 频率序号 k) 对应的 cos/sin 序列缓存下来，
# 内层循环只剩乘加运算。预计算与逐次调用的浮点结果完全一致。
_CYCLE_TABLES: dict[tuple[int, int], tuple[list[float], list[float]]] = {}


def _cycle_tables(window: int, k: int) -> tuple[list[float], list[float]]:
    key = (window, k)
    tbl = _CYCLE_TABLES.get(key)
    if tbl is None:
        omega = 2.0 * math.pi * k / window
        cos_t = [math.cos(omega * i) for i in range(window)]
        sin_t = [math.sin(omega * i) for i in range(window)]
        tbl = (cos_t, sin_t)
        _CYCLE_TABLES[key] = tbl
    return tbl
