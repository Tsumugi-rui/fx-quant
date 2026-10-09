"""行情数据源（对策略完全黑箱）。

=== 原则 ===
本模块只负责「把可观测的价格序列喂给策略」，不包含任何对游戏内部
价格生成算法的复刻或假设。

为什么这么设计：
  量化策略的全部输入只能是「市场公开可观测的价格历史」。
  任何来自程序内部实现的知识（随机数种子、漂移公式、事件概率等）
  都属于不可合法获得的信息，与"读取未来走势"在性质上等价。
  据此构建的策略在真实市场中无意义，且对方一旦改版即刻失效。

因此本模块提供两类数据源：
  1. `HistoricalCsvSource` —— 用户提供的真实历史价格 CSV（推荐用于研究）
  2. `SynthWalkSource`      —— 通用随机游走，仅用于自测流程是否跑通
                               不承载任何"目标市场特性"，不可据此调参
"""

from __future__ import annotations

import csv
import math
import random
from dataclasses import dataclass, field
from pathlib import Path
from typing import Protocol

from .config import PAIR_IDS, PAIRS


@dataclass
class Candle:
    open: float
    high: float
    low: float
    close: float

    def as_tuple(self) -> tuple[float, float, float, float]:
        return (self.open, self.high, self.low, self.close)


@dataclass
class MarketSeries:
    """单个可交易标的的价格序列。"""

    pair: str
    candles: list[Candle] = field(default_factory=list)
    price: float = 0.0

    def closed_candles(self) -> list[Candle]:
        """返回截至当前时点的全部已收盘 K 线（策略唯一合法数据源）。"""
        return self.candles


class MarketSource(Protocol):
    """行情源协议。策略层只依赖这个接口。"""

    def bars(self, pair: str) -> list[Candle]:
        """已收盘 K 线序列。"""

    def price(self, pair: str) -> float:
        """最新价。"""

    def advance(self) -> dict[str, float]:
        """推进一个时点，返回各标的最新价。数据耗尽时抛出 StopIteration。"""


# ---------------------------------------------------------------------------
# 真实历史数据源（研究首选）
# ---------------------------------------------------------------------------
class HistoricalCsvSource:
    """从 CSV 读取历史价格，逐行推进。

    CSV 格式（表头必需）：
        date,pair,open,high,low,close
    或宽表格式：
        date,EUR/USD,GBP/USD,...   （每列为一个货币对，作为收盘价，开高低自动生成）

    策略只能按行顺序看到已推进的部分，后续行在推进前对策略不可见，
    从机制上杜绝未来函数。
    """

    def __init__(self, path: str | Path, delimiter: str = ",") -> None:
        self.path = Path(path)
        rows, pairs = self._load(delimiter)
        self._pairs = pairs
        self._rows = rows
        self._cursor = 0
        self.series: dict[str, MarketSeries] = {
            p: MarketSeries(pair=p) for p in pairs
        }

    def _load(self, delimiter: str) -> tuple[list[dict[str, Candle]], list[str]]:
        with self.path.open("r", encoding="utf-8-sig", newline="") as fh:
            reader = csv.DictReader(fh, delimiter=delimiter)
            headers = [h.strip() for h in (reader.fieldnames or [])]
            lower = [h.lower() for h in headers]

            if {"pair", "close"}.issubset(set(lower)):
                return self._load_long(reader, headers, lower)
            return self._load_wide(reader, headers)

    @staticmethod
    def _load_long(reader, headers, lower) -> tuple[list[dict[str, Candle]], list[str]]:
        by_date: dict[str, dict[str, Candle]] = {}
        idx = {name: lower.index(name) for name in
               ("date", "pair", "open", "high", "low", "close") if name in lower}
        pairs: list[str] = []
        for row in reader:
            values = [row[h] for h in headers]
            pair = values[idx["pair"]].strip()
            date = values[idx["date"]].strip() if "date" in idx else str(len(by_date))
            close = _f(values[idx["close"]])
            open_ = _f(values[idx["open"]]) if "open" in idx else close
            high = _f(values[idx["high"]]) if "high" in idx else max(open_, close)
            low = _f(values[idx["low"]]) if "low" in idx else min(open_, close)
            if pair not in pairs:
                pairs.append(pair)
            by_date.setdefault(date, {})[pair] = Candle(open_, high, low, close)

        rows = []
        for date in sorted(by_date):
            rows.append(dict(by_date[date]))
        return rows, pairs

    @staticmethod
    def _load_wide(reader, headers) -> tuple[list[dict[str, Candle]], list[str]]:
        date_key = headers[0]
        pairs = [h for h in headers[1:]]
        rows: list[dict[str, Candle]] = []
        for row in reader:
            bar: dict[str, Candle] = {}
            for pair in pairs:
                raw = row.get(pair, "")
                if raw is None or raw.strip() == "":
                    continue
                close = _f(raw)
                bar[pair] = Candle(close, close, close, close)
            if bar:
                rows.append(bar)
        return rows, pairs

    # -- MarketSource -------------------------------------------------------
    def bars(self, pair: str) -> list[Candle]:
        return self.series[pair].closed_candles()

    def price(self, pair: str) -> float:
        return self.series[pair].price

    def advance(self) -> dict[str, float]:
        if self._cursor >= len(self._rows):
            raise StopIteration("历史数据已耗尽")
        row = self._rows[self._cursor]
        self._cursor += 1
        out: dict[str, float] = {}
        for pair, candle in row.items():
            if pair not in self.series:
                continue
            series = self.series[pair]
            series.candles.append(candle)
            series.price = candle.close
            out[pair] = candle.close
        # 未在本行出现的标的沿用旧价
        for pair, series in self.series.items():
            out.setdefault(pair, series.price)
        return out

    @property
    def remaining(self) -> int:
        return max(0, len(self._rows) - self._cursor)


# ---------------------------------------------------------------------------
# 通用随机游走（仅供自测流程，不含目标市场特性）
# ---------------------------------------------------------------------------
class SynthWalkSource:
    """纯几何随机游走。

    用途声明：**仅用于验证代码流程是否跑通**（数据是否流通、下单是否成交、
    指标是否计算）。它不模拟任何真实或目标市场的规律，
    因此绝不可基于它的回测结果去调策略参数。

    默认为零漂移，即价格的期望未来值与当前值相同 —— 这是一个
    「没有可预测信号」的基准环境，用来确认策略不会凭空产生 alpha。

    `news_active()` 暴露一个「是否有快讯」的布尔标记。这是策略侧
    唯一能从行情源获取的事件信息，且只表示"不确定性升高"，
    不携带任何方向性内容。
    """

    def __init__(self, pairs: tuple[str, ...] = PAIR_IDS, seed: int | None = None,
                 volatility: float = 0.002, warmup: int = 80,
                 drift: float = 0.0, news_probability: float = 0.004,
                 news_duration: int = 8) -> None:
        self._rng = random.Random(seed)
        self._vol = volatility
        self._drift = drift
        self._news_prob = news_probability
        self._news_duration = news_duration
        self._news_left = 0
        self.series: dict[str, MarketSeries] = {}
        for spec in PAIRS:
            if spec.id not in pairs:
                continue
            value = spec.initial
            candles: list[Candle] = []
            for _ in range(warmup):
                open_ = value
                value = open_ * (1 + self._rng.gauss(self._drift, self._vol))
                wick = open_ * self._vol * self._rng.random()
                candles.append(Candle(
                    open=open_,
                    high=max(open_, value) + wick,
                    low=min(open_, value) - wick,
                    close=value,
                ))
            self.series[spec.id] = MarketSeries(
                pair=spec.id, candles=candles, price=value)

    def bars(self, pair: str) -> list[Candle]:
        return self.series[pair].closed_candles()

    def price(self, pair: str) -> float:
        return self.series[pair].price

    def news_active(self) -> bool:
        """当前是否处于快讯期（公开可观测的事件标记）。"""
        return self._news_left > 0

    def advance(self) -> dict[str, float]:
        # 快讯生命周期（只影响波动幅度，不影响方向）
        if self._news_left > 0:
            self._news_left -= 1
        elif self._rng.random() < self._news_prob:
            self._news_left = self._news_duration

        vol_boost = 2.0 if self._news_left > 0 else 1.0

        out: dict[str, float] = {}
        for pair, series in self.series.items():
            open_ = series.price
            close = open_ * (1 + self._rng.gauss(self._drift, self._vol * vol_boost))
            wick = open_ * self._vol * vol_boost * self._rng.random()
            series.candles.append(Candle(
                open=open_, high=max(open_, close) + wick,
                low=min(open_, close) - wick, close=close))
            series.price = close
            out[pair] = close
        return out


# ---------------------------------------------------------------------------
# 兼容别名
# ---------------------------------------------------------------------------
# 旧代码中的 SimMarket 现指向通用随机游走，避免误用为"目标市场模型"。
SimMarket = SynthWalkSource


def _f(value: str | None) -> float:
    try:
        return float(str(value).strip().replace(",", ""))
    except (TypeError, ValueError):
        return float("nan")
