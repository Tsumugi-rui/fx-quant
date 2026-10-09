"""自适应因子权重（在线学习）。

=== 为什么不用硬编码权重 ===
如果人为规定「趋势因子权重 -0.25，均值回归 +1.0」，
那等价于宣称"我知道这个市场会均值回归"。
这种知识只能来自两种途径：偷看未来走势，或阅读系统内部实现。
两者都是作弊，且策略一旦面对真实市场或对方改版就失效。

=== 正确做法 ===
把每个因子当作一个"预测器"，让系统自己从**已经发生的价格**中
统计各因子近期的方向命中率，再用 softmax 分配权重。

时间线保证（无未来函数）：
  在时点 t 做决策时，参与权重计算的记录全部是
  「在 t - eval_horizon 之前就已发出、且此刻已能用价格验证」的预测。
  也就是说，权重只反映已兑现的历史表现，不含任何 t 之后的信息。
"""

from __future__ import annotations

import math
from collections import deque
from dataclasses import dataclass

FACTOR_NAMES: tuple[str, ...] = ("trend", "momentum", "reversion", "cycle")


@dataclass
class _PendingPrediction:
    """一条等待验证的因子预测。

    `value` 是该因子的原始取值（-1..1），而不是它的符号。
    保留幅度是为了后面能算**信息系数**（见 `_recompute`）。
    """

    factor: str
    pair: str
    value: float
    entry_price: float
    issued_at: int


@dataclass
class _ResolvedPrediction:
    factor: str
    hit: bool
    value: float             # 当时因子的取值
    move: float              # 该持有期内标的的实际收益
    realized: float          # value × move，即该因子这一次贡献的收益


class AdaptiveWeighter:
    """依据近期已实现预测力，动态分配因子权重。"""

    def __init__(self, lookback: int = 240, min_samples: int = 40,
                 temperature: float = 2.0, weight_floor: float = 0.0,
                 max_weight: float = 1.0, eval_horizon: int = 20,
                 shrinkage: float = 0.75, min_edge: float = 0.0,
                 t_threshold: float = 1.0) -> None:
        self.lookback = lookback
        self.min_samples = min_samples
        self.temperature = max(0.01, temperature)
        self.weight_floor = weight_floor
        self.max_weight = max_weight
        self.eval_horizon = eval_horizon
        # 收缩强度：0 = 完全信任实测表现（易追噪声），1 = 完全归零。
        # 因子在无结构市场中命中率必然接近 50%，实测差异多半是噪声，
        # 必须向 0 收缩，否则权重会剧烈震荡并放大随机亏损。
        self.shrinkage = min(1.0, max(0.0, shrinkage))
        # 最小有效优势：平均已实现收益低于该阈值视为无 edge，权重归零
        self.min_edge = min_edge
        # t 统计量显著性门槛。约 1.0 对应单边 84% 置信水平。
        # 取值不宜过高：IC≈0.05 的信号要在 t=2 上显著需要上千样本，
        # 而回看窗口只有几百，门槛过高会导致因子永远不显著、
        # 权重恒为 0、策略完全不动作（详见 StrategyConfig 的注释）。
        self.t_threshold = max(0.0, t_threshold)

        self._pending: deque[_PendingPrediction] = deque()
        self._resolved: dict[str, deque[_ResolvedPrediction]] = {
            name: deque(maxlen=lookback) for name in FACTOR_NAMES
        }
        # 初始不给任何因子权重：在积累到足够证据之前，策略应当按兵不动。
        # （早期版本初始化为等权，导致策略在"还没学到任何东西"时就按
        #   因子平均方向开仓，白付交易成本；在随机行情里等于主动送钱。）
        self._weights: dict[str, float] = {name: 0.0 for name in FACTOR_NAMES}
        self._diagnostics: dict[str, float] = {
            name: 0.0 for name in FACTOR_NAMES
        }
        # 每单位因子取值对应的预期收益率（回归斜率），仅用于报告
        self._betas: dict[str, float] = {name: 0.0 for name in FACTOR_NAMES}

    # ------------------------------------------------------------------
    # 记录与结算
    # ------------------------------------------------------------------
    def record(self, factor: str, pair: str, value: float,
               entry_price: float, now: int) -> None:
        """登记一条预测，等待未来验证（验证用的价格尚未发生，此处不看）。

        `value` 传因子**原始取值**（不要传符号）。用原始取值而非 ±1，
        才能在下游估计出「因子每变化一个单位，预期收益变化多少」，
        也就是信息系数 —— 这比只数方向对错的有效信息量大得多：
        把 0.9 的强信号和 0.16 的弱信号一视同仁，等于主动丢掉大部分信息。

        **降采样**：只每隔 `eval_horizon` 个时点登记一次。
        原因是若不降采样，相邻时点发出的预测会共享同一段未来价格，
        样本高度重叠且不独立 —— 这会让 t 统计量被严重高估，
        使因子看起来"显著"而实际只是噪声的重复计数。
        降采样后每条预测覆盖互不重叠的未来区间，统计量才可信。
        """
        if factor not in self._resolved:
            return
        if self.eval_horizon > 1 and now % self.eval_horizon != 0:
            return
        self._pending.append(_PendingPrediction(
            factor=factor, pair=pair, value=value,
            entry_price=entry_price, issued_at=now))

    def resolve(self, now: int, price_lookup) -> int:
        """结算所有已到期的预测。

        `price_lookup(pair) -> float` 只返回当前价，因此这里验证的
        是「已经发生的收益」，属于合法的历史统计。

        `_pending` 以登记时间为序（FIFO），因此只需从队首处理
        已到期项即可，无需每轮遍历整个队列。复杂 O(每个到期的项)。
        """
        resolved = 0
        while self._pending:
            item = self._pending[0]
            if now - item.issued_at < self.eval_horizon:
                # 队首还未到期，后续必然更晚，直接停止
                break
            self._pending.popleft()
            price = price_lookup(item.pair)
            if price is None or item.entry_price <= 0:
                continue
            move = price / item.entry_price - 1.0
            if abs(move) < 1e-12:
                continue
            realized = move * item.value
            self._resolved[item.factor].append(_ResolvedPrediction(
                factor=item.factor, hit=(realized > 0), value=item.value,
                move=move, realized=realized))
            resolved += 1
        if resolved:
            self._recompute()
        return resolved

    # ------------------------------------------------------------------
    def _recompute(self) -> None:
        """依据近期表现重算权重。

        核心思路：
          1. 对每个因子估计它与未来收益的**信息系数（IC，皮尔逊相关系数）**，
             并换算成 signed t 统计量，判断这个相关性是否可信。
          2. 只有 |t| 达到门槛才承认该因子的 edge，否则质量分归零。
          3. **保留符号**：IC 为负，说明该因子在当前市场是*反向指标*，
             应当给它负权重（反着用），而不是把它丢掉。
          4. 幅度用 softmax 在因子间竞争，符号由各自的 IC 方向决定。
          5. 若所有因子都无显著 edge，权重全部归零 —— 这意味着
             score=0、不开仓，即"没有可识别的优势就不下注"。

        === 为什么用 IC 而不是"方向命中率" ===
        早期版本把每条预测压缩成 ±1（因子看涨就看涨），只统计方向对错。
        这丢掉了因子的**幅度**信息：取值 0.9 的强信号与 0.16 的弱信号
        被同等对待，估计效率很低，在实际只有 0.05 量级的 IC 上
        几乎不可能达到统计显著（这正是"死锁"的成因之一）。

        IC 口径直接用协方差：
            IC = cov(factor, 未来收益) / (sd(factor)·sd(收益))
        单位与因子缩放无关（趋势因子天生比周期因子"大"也无所谓），
        并且在同样的样本量下 t 统计量更高、更容易识别出真实 edge。
        """
        raw_scores: dict[str, float] = {}
        diagnostics: dict[str, float] = {}
        betas: dict[str, float] = {}

        for name in FACTOR_NAMES:
            history = self._resolved[name]
            n = len(history)
            if n < self.min_samples:
                raw_scores[name] = 0.0
                diagnostics[name] = 0.0
                betas[name] = 0.0
                continue

            values = [r.value for r in history]
            moves = [r.move for r in history]
            mean_v = sum(values) / n
            mean_m = sum(moves) / n
            dv = [v - mean_v for v in values]
            dm = [m - mean_m for m in moves]
            cov = sum(a * b for a, b in zip(dv, dm)) / n
            var_v = sum(a * a for a in dv) / n
            var_m = sum(b * b for b in dm) / n

            if var_v <= 1e-18 or var_m <= 1e-18:
                raw_scores[name] = 0.0
                diagnostics[name] = 0.0
                betas[name] = 0.0
                continue

            corr = cov / math.sqrt(var_v * var_m)
            corr = max(-0.999999, min(0.999999, corr))
            # 相关系数的 t 统计量（保留符号）
            t_signed = corr * math.sqrt((n - 2) / (1.0 - corr * corr))
            # 每单位因子取值对应的预期收益率（bp 口径更直观，见 betas 报告）
            betas[name] = cov / var_v
            diagnostics[name] = t_signed

            if abs(t_signed) < self.t_threshold:
                # 统计上无法区分于零 -> 不承认 edge
                raw_scores[name] = 0.0
            else:
                # 强度用超出阈值的部分，避免刚过线就重仓；
                # 但符号必须保留 —— 负号代表"这个因子在该市场要反着用"。
                strength = min(1.0, (abs(t_signed) - self.t_threshold)
                               / self.t_threshold)
                raw_scores[name] = math.copysign(strength, t_signed)

        self._diagnostics = diagnostics
        self._betas = betas

        # 收缩：把质量分向 0 拉近（抑制追噪声）。注意它不改变符号。
        shrunk = {k: v * (1.0 - self.shrinkage) for k, v in raw_scores.items()}

        max_abs = max((abs(v) for v in shrunk.values()), default=0.0)
        if max_abs < 1e-12:
            # 没有任何因子表现出可信 edge -> 权重全部归零。
            # 这不是"退化为等权"，而是**退出交易**：
            # 等权会让策略在无信号时仍按因子平均方向开仓，纯属白付成本。
            self._weights = {name: 0.0 for name in FACTOR_NAMES}
            return

        # 幅度：softmax 让显著因子之间形成竞争
        magnitudes: dict[str, float] = {}
        for name in FACTOR_NAMES:
            magnitudes[name] = math.exp(
                self.temperature * abs(shrunk[name]) / max_abs)
        total = sum(magnitudes.values())

        # 符号：沿用因子的 edge 方向（可为负）
        weights: dict[str, float] = {}
        for name in FACTOR_NAMES:
            mag = magnitudes[name] / total
            value = shrunk[name]
            sign = 0.0 if abs(value) < 1e-12 else math.copysign(1.0, value)
            weights[name] = mag * sign

        # 上限裁剪（保留符号）
        clipped: dict[str, float] = {}
        for name in FACTOR_NAMES:
            w = weights[name]
            if abs(w) > self.max_weight:
                w = math.copysign(self.max_weight, w)
            clipped[name] = w

        denom = sum(abs(v) for v in clipped.values())
        if denom < 1e-12:
            self._weights = {name: 0.0 for name in FACTOR_NAMES}
        else:
            self._weights = {k: v / denom for k, v in clipped.items()}

    # ------------------------------------------------------------------
    @property
    def weights(self) -> dict[str, float]:
        return dict(self._weights)

    def sample_counts(self) -> dict[str, int]:
        return {name: len(self._resolved[name]) for name in FACTOR_NAMES}

    def hit_rates(self) -> dict[str, float]:
        out: dict[str, float] = {}
        for name in FACTOR_NAMES:
            history = self._resolved[name]
            if not history:
                out[name] = float("nan")
            else:
                out[name] = sum(1 for r in history if r.hit) / len(history)
        return out

    def realized_edges(self) -> dict[str, float]:
        """各因子的预测力（bp 口径）。

        这里报告的是「因子取值每增加 1.0，预期收益变化多少个 bp」，
        即回归斜率 beta × 10000。为负表示该因子在此市场是反向指标。
        """
        return {name: self._betas.get(name, 0.0) * 10000.0
                for name in FACTOR_NAMES}

    def information_coefficients(self) -> dict[str, float]:
        """各因子与未来收益的相关系数（IC）。

        由 t 统计量反解：t = IC·sqrt((n-2)/(1-IC²))。
        仅用于报告与诊断，不参与权重计算。
        """
        out: dict[str, float] = {}
        for name in FACTOR_NAMES:
            n = len(self._resolved[name])
            t = self._diagnostics.get(name, 0.0)
            if n <= 2 or t == 0.0:
                out[name] = float("nan")
                continue
            # 由 t 反解 |IC| = t / sqrt(t² + n - 2)
            out[name] = math.copysign(
                abs(t) / math.sqrt(t * t + n - 2), t)
        return out

    def t_stats(self) -> dict[str, float]:
        """各因子 IC 的 signed t 统计量，用于判断 edge 是否显著。

        负值有意义：表示该因子与未来收益负相关（应反用）。
        """
        return dict(self._diagnostics)


class ScoreCalibrator:
    """在线校准「信号分数 -> 预期收益」的映射。

    === 为什么需要 ===
    成本门槛要回答的问题是："这点信号强度换来的预期收益，
    够不够覆盖交易成本？"

    早期版本用一个**硬编码**的假设（score=1.0 对应 0.5% 收益）。
    但这个假设与实际预测力无关：若真实预测力只有它的四分之一，
    门槛就会放行大量实际为负期望的交易 —— 表现为交易频率暴涨、
    手续费吞掉全部收益。

    这里改为**从已实现的价格中在线估计**：
        beta = cov(score, 未来收益) / var(score)
        预期收益 ≈ beta × score
    于是门槛会自动适配信号的真实强度：强则放宽、弱则收紧。

    === 时间线纪律 ===
    登记时只记下 score 与当时价格；结算时用"已经走到"的价格验证。
    整个流程与 AdaptiveWeighter 遵循同一套规则，不触及未来信息。
    """

    def __init__(self, lookback: int = 300, min_samples: int = 50,
                 eval_horizon: int = 3, prior_beta: float = 0.0,
                 min_nonzero: int = 20) -> None:
        self.lookback = lookback
        self.min_samples = min_samples
        self.eval_horizon = eval_horizon
        # 样本不足时的先验。默认 0 意味着"在证明信号有预测力之前不下注"，
        # 这是最保守的选择。
        self._prior_beta = prior_beta
        self._beta = prior_beta
        # 「就绪」还要求窗口内有足够多的**非零** score。
        #
        # 原因：策略刚起步时权重全为零，score 恒等于 0。
        # 这些 0 样本虽然不改变回归的无偏性（分子分母同比例缩放），
        # 却会让样本数迅速凑够 —— 于是校准器在只有寥寥几个非零 score
        # 的情况下就宣布「就绪」，算出一个由噪声主导的 β，
        # 并据此否决所有交易。必须等真正有信号可言，才算就绪。
        self.min_nonzero = min_nonzero
        self._pending: deque[tuple[int, float, str, float]] = deque()
        self._scores: deque[float] = deque(maxlen=lookback)
        self._returns: deque[float] = deque(maxlen=lookback)

    # ------------------------------------------------------------------
    def record(self, score: float, pair: str, price: float, now: int) -> None:
        """登记一次「若此刻按 score 方向下单」的预测，等待未来验证。

        与因子预测同样做降采样，避免重叠样本高估相关性。
        """
        if self.eval_horizon > 1 and now % self.eval_horizon != 0:
            return
        if price <= 0:
            return
        self._pending.append((now + self.eval_horizon, score, pair, price))

    def resolve(self, now: int, price_lookup) -> int:
        resolved = 0
        while self._pending:
            item = self._pending[0]
            if now < item[0]:
                break
            self._pending.popleft()
            _, score, pair, entry = item
            price = price_lookup(pair)
            if price is None or price <= 0 or entry <= 0:
                continue
            self._scores.append(score)
            self._returns.append(price / entry - 1.0)
            resolved += 1
        if resolved:
            self._recompute()
        return resolved

    def _recompute(self) -> None:
        n = len(self._scores)
        if n < self.min_samples:
            return
        ms = sum(self._scores) / n
        mr = sum(self._returns) / n
        cov = sum((s - ms) * (r - mr)
                  for s, r in zip(self._scores, self._returns)) / n
        var = sum((s - ms) ** 2 for s in self._scores) / n
        if var <= 1e-18:
            return
        self._beta = cov / var

    # ------------------------------------------------------------------
    @property
    def beta(self) -> float:
        """当前估计的「每单位 score 对应的预期收益率」。"""
        return self._beta

    @property
    def ready(self) -> bool:
        """样本量够、且其中有足够多的非零 score，才认为校准可用。"""
        if len(self._scores) < self.min_samples:
            return False
        nonzero = sum(1 for s in self._scores if abs(s) > 1e-9)
        return nonzero >= self.min_nonzero

    def nonzero_count(self) -> int:
        """窗口中非零 score 的样本数，用于诊断。"""
        return sum(1 for s in self._scores if abs(s) > 1e-9)

    def expected_return(self, score: float) -> float | None:
        """按 score 方向交易时的预期收益率（可为负）。

        score>0 时为正表示"看涨方向整体有效"；
        若返回负值，说明当前信号系统在该市场上是反向的，
        调用方应据此拒绝交易。

        === 未就绪时返回 None，而不是 0.0 ===
        这是个踩过的坑：早期版本未就绪时返回先验 0.0，
        下游成本门槛看到"预期收益 = 0"就判定"非正、不下注"，
        于是**永远退回不到常数兜底**，导致策略在校准就绪前完全不交易。
        返回 None 才能让调用方正确区分「不知道」和「算出来是零/负」。
        """
        if not self.ready:
            return None
        return self._beta * score
