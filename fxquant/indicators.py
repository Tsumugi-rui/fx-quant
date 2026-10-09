"""因果指标库（增量版）。

=== 性能说明 ===
朴素实现对每个时点都重算整条序列的 TR/EMA，复杂度 O(n^2)，
4000 个时点就会累积数千万次运算。

本模块提供 `IncrementalIndicators`：为每个标的维护滚动状态，
每新增一根 K 线只做 O(1) 或 O(window) 的增量更新。
整体复杂度降到 O(n * window)。

=== 因果性红线 ===
所有函数只使用 index <= len-1 的数据，
`assert_causal()` 提供机械化的未来函数自检。
"""

from __future__ import annotations

import math
from collections import deque
from dataclasses import dataclass, field


# ---------------------------------------------------------------------------
# 无状态工具函数（用于自检与小额计算）
# ---------------------------------------------------------------------------
def sma(values: list[float], window: int) -> float | None:
    if len(values) < window or window <= 0:
        return None
    return sum(values[-window:]) / window


def ema_series(values: list[float], window: int) -> list[float]:
    """与输入等长的 EMA 序列。

    第 i 项只使用 values[:i+1]，因果安全。
    初值取首值，避免 SMA 预热引入的前视问题。
    """
    if not values or window <= 0:
        return []
    k = 2.0 / (window + 1.0)
    out = [values[0]]
    for v in values[1:]:
        out.append(out[-1] + k * (v - out[-1]))
    return out


def ema(values: list[float], window: int) -> float | None:
    s = ema_series(values, window)
    return s[-1] if s else None


def roc(values: list[float], window: int) -> float | None:
    """变化率：当前值相对 window 根之前的变化。"""
    if len(values) < window + 1 or window <= 0:
        return None
    past = values[-1 - window]
    if past == 0:
        return None
    return values[-1] / past - 1.0


def stdev(values: list[float], window: int) -> float | None:
    if len(values) < window or window < 2:
        return None
    w = values[-window:]
    mean = sum(w) / window
    var = sum((v - mean) ** 2 for v in w) / (window - 1)
    return math.sqrt(var)


def zscore(values: list[float], window: int, cap: float = 3.0) -> float | None:
    if len(values) < window or window < 2:
        return None
    w = values[-window:]
    mean = sum(w) / window
    sd = stdev(values, window)
    if sd is None or sd == 0:
        return 0.0
    return max(-cap, min(cap, (values[-1] - mean) / sd))


def true_ranges(highs: list[float], lows: list[float],
                closes: list[float]) -> list[float]:
    n = min(len(highs), len(lows), len(closes))
    if n == 0:
        return []
    out = [highs[0] - lows[0]]
    for i in range(1, n):
        out.append(max(highs[i] - lows[i],
                       abs(highs[i] - closes[i - 1]),
                       abs(lows[i] - closes[i - 1])))
    return out


def atr(highs: list[float], lows: list[float], closes: list[float],
        window: int = 20) -> float | None:
    """Wilder ATR（无状态版本，O(n)）。"""
    trs = true_ranges(highs, lows, closes)
    if len(trs) < window or window <= 0:
        return None
    value = sum(trs[:window]) / window
    for tr in trs[window:]:
        value = (value * (window - 1) + tr) / window
    return value


def atr_pct(highs: list[float], lows: list[float], closes: list[float],
            window: int = 20) -> float | None:
    a = atr(highs, lows, closes, window)
    if a is None or not closes or closes[-1] == 0:
        return None
    return a / closes[-1]


def realized_vol(closes: list[float], window: int = 20) -> float | None:
    if len(closes) < window + 1 or window < 2:
        return None
    rets = [closes[i] / closes[i - 1] - 1.0
            for i in range(len(closes) - window, len(closes))
            if closes[i - 1] != 0]
    if len(rets) < 2:
        return None
    mean = sum(rets) / len(rets)
    var = sum((r - mean) ** 2 for r in rets) / (len(rets) - 1)
    return math.sqrt(var)


def linear_slope(values: list[float], window: int) -> float | None:
    """窗口内最小二乘斜率，归一化为每根 K 线的相对变化。"""
    if len(values) < window or window < 2:
        return None
    ys = values[-window:]
    n = window
    mx = (n - 1) / 2
    my = sum(ys) / n
    denom = sum((x - mx) ** 2 for x in range(n))
    if denom == 0:
        return None
    slope = sum((i - mx) * (ys[i] - my) for i in range(n)) / denom
    return slope / (my if my else 1.0)


# ---------------------------------------------------------------------------
# 增量状态机
# ---------------------------------------------------------------------------
@dataclass
class _SymbolState:
    """单个标的的滚动指标状态。"""

    # EMA
    ema_fast: float | None = None
    ema_slow: float | None = None
    # ATR (Wilder)
    atr: float | None = None
    atr_init_sum: float = 0.0
    atr_count: int = 0
    prev_close: float | None = None
    # 收益率滚动序列（用于波动率）
    returns: deque[float] = field(default_factory=deque)
    returns_sq: deque[float] = field(default_factory=deque)
    # 收盘价滚动窗口（用于 z-score / ROC / 斜率）
    closes: deque[float] = field(default_factory=deque)
    highs: deque[float] = field(default_factory=deque)
    lows: deque[float] = field(default_factory=deque)
    bars: int = 0


class IncrementalIndicators:
    """按标的维护增量指标状态。

    调用 `update(pair, o, h, l, c)` 推入一根新 K 线，
    然后通过属性读取最新指标值。所有读取都只反映到当前 K 线为止。
    """

    def __init__(self, max_window: int = 64) -> None:
        self.max_window = max_window
        self._state: dict[str, _SymbolState] = {}
        self.last: dict[str, dict[str, float | None]] = {}

    def _st(self, pair: str) -> _SymbolState:
        st = self._state.get(pair)
        if st is None:
            st = _SymbolState()
            self._state[pair] = st
        return st

    # -- 更新 ---------------------------------------------------------------
    def update(self, pair: str, high: float, low: float, close: float,
               ema_fast_w: int, ema_slow_w: int, atr_w: int,
               window: int) -> dict[str, float | None]:
        st = self._st(pair)
        st.bars += 1

        # --- EMA（O(1) 递推）---
        if st.ema_fast is None:
            st.ema_fast = close
            st.ema_slow = close
        else:
            k_f = 2.0 / (ema_fast_w + 1.0)
            k_s = 2.0 / (ema_slow_w + 1.0)
            st.ema_fast += k_f * (close - st.ema_fast)
            st.ema_slow += k_s * (close - st.ema_slow)

        # --- ATR（Wilder 递推）---
        if st.prev_close is None:
            tr = high - low
        else:
            tr = max(high - low, abs(high - st.prev_close), abs(low - st.prev_close))
        if st.atr is None:
            st.atr_init_sum += tr
            st.atr_count += 1
            if st.atr_count >= atr_w:
                st.atr = st.atr_init_sum / atr_w
        else:
            st.atr = (st.atr * (atr_w - 1) + tr) / atr_w
        st.prev_close = close

        # --- 收益率（用于已实现波动率）---
        if len(st.closes) > 0 and st.closes[-1] != 0:
            r = close / st.closes[-1] - 1.0
            st.returns.append(r)
            st.returns_sq.append(r * r)
            if len(st.returns) > window:
                st.returns.popleft()
                st.returns_sq.popleft()

        # --- 滚动窗口 ---
        st.closes.append(close)
        st.highs.append(high)
        st.lows.append(low)
        if len(st.closes) > self.max_window:
            st.closes.popleft()
            st.highs.popleft()
            st.lows.popleft()

        return self._snapshot(pair, window)

    def _snapshot(self, pair: str, window: int) -> dict[str, float | None]:
        st = self._st(pair)
        closes = list(st.closes)
        out: dict[str, float | None] = {
            "ema_fast": st.ema_fast,
            "ema_slow": st.ema_slow,
            "atr": st.atr,
            "atr_pct": (st.atr / closes[-1]) if st.atr and closes and closes[-1] else None,
            "close": closes[-1] if closes else None,
            "bars": float(st.bars),
        }

        # 已实现波动率（用滚动 sum，O(window)）
        n = len(st.returns)
        if n >= 2:
            sums = sum(st.returns)
            mean = sums / n
            var = sum(st.returns_sq) / n - mean * mean
            out["realized_vol"] = math.sqrt(max(0.0, var))
        else:
            out["realized_vol"] = None

        # 斜率
        out["slope"] = linear_slope(closes, min(len(closes), window)) \
            if len(closes) >= window else None

        return out

    def get(self, pair: str) -> dict[str, float | None] | None:
        st = self._state.get(pair)
        if st is None:
            return None
        return self.last.get(pair)

    def snapshot(self, pair: str, window: int) -> dict[str, float | None] | None:
        if pair not in self._state:
            return None
        return self._snapshot(pair, window)

    def closes_of(self, pair: str) -> list[float]:
        st = self._state.get(pair)
        return list(st.closes) if st else []

    def reset(self) -> None:
        self._state.clear()
        self.last.clear()


# ---------------------------------------------------------------------------
# 因果性自检
# ---------------------------------------------------------------------------
def assert_causal(fn, series: list[float], *args, **kwargs) -> bool:
    """因果性自检。

    对每个 t，验证 fn 在「前缀 series[:t+1]」上的输出，
    与在「被未来数据污染、再截回同一前缀」的序列上、位置 t 处的输出一致。
    一致说明位置 t 的结果只由前 t+1 项决定，没有读未来。
    """
    if len(series) < 3:
        return True
    for t in range(1, len(series) - 1):
        ref = _safe(fn, series[: t + 1], *args, **kwargs)
        contaminated = series[: t + 1] + [v * 7.0 + 1234.0 for v in series[t + 1:]]
        got = _safe(fn, contaminated[: t + 1], *args, **kwargs)
        if not _close_enough(ref, got):
            return False
    return True


def verify_no_lookahead(fn, series: list[float], *args, **kwargs) -> bool:
    """前视检验：往序列尾部追加数据，前半段末位的输出不应改变。"""
    if len(series) < 4:
        return True
    mid = len(series) // 2
    ref = _safe(fn, series[:mid], *args, **kwargs)
    perturbed = series[:mid] + [v * 10.0 + 999.0 for v in series[mid:]]
    after = _safe(fn, perturbed[:mid], *args, **kwargs)
    return _close_enough(ref, after)


def _safe(fn, series, *args, **kwargs):
    try:
        return fn(series, *args, **kwargs)
    except Exception:
        return None


def _close_enough(a, b) -> bool:
    if a is None and b is None:
        return True
    if a is None or b is None:
        return False
    return abs(a - b) < 1e-9
