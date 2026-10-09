"""fxquant —— 面向《FX 简单!》网页游戏的本地量化交易程序。

模块总览
--------
config       : 可交易标的、公开的交易成本规则、策略与风控参数
market       : 行情数据源（真实 CSV / 通用随机游走），对策略黑箱
broker       : 账户、持仓、盈亏、爆仓结算
indicators   : 因果增量指标库（含反未来函数自检）
adaptive     : 自适应因子权重（在线学习）
strategy     : 多因子信号引擎
risk         : 风控与仓位管理
engine       : 逐时点主循环
performance  : 绩效指标

设计红线（详见 README）
--------
1. 不使用任何来自程序内部实现的价格生成知识（随机公式、事件概率等）。
2. 不使用任何未来价格数据。
3. 因子权重由已实现的历史表现在线决定，不做硬编码的方向性假设。
4. 新闻快讯只作为"不确定性升高"标记用于风控，不解析其方向性影响。
"""

from .adaptive import FACTOR_NAMES, AdaptiveWeighter
from .bridge import BridgeExecution, BridgeServer
from .broker import Broker, Position, TradeRecord
from .config import (
    PAIR_IDS,
    PAIRS,
    EngineConfig,
    RiskConfig,
    StrategyConfig,
)
from .engine import EngineResult, QuantEngine, TickLog
from .execution import (
    CloseResult,
    DryRunExecution,
    Execution,
    ExecutionError,
    GameState,
    OrderResult,
    RemotePosition,
)
from .indicators import (
    IncrementalIndicators,
    assert_causal,
    verify_no_lookahead,
)
from .live import LiveConfig, LiveStats, LiveTrader
from .market import Candle, HistoricalCsvSource, MarketSeries, SynthWalkSource
from .performance import Performance, analyze
from .risk import RiskDecision, RiskManager
from .sim_execution import SimExecution
from .strategy import Signal, SignalEngine

__version__ = "0.2.0"

__all__ = [
    "AdaptiveWeighter", "FACTOR_NAMES",
    "BridgeExecution", "BridgeServer",
    "Broker", "Position", "TradeRecord",
    "EngineConfig", "PAIRS", "PAIR_IDS", "RiskConfig", "StrategyConfig",
    "EngineResult", "QuantEngine", "TickLog",
    "CloseResult", "DryRunExecution", "Execution", "ExecutionError",
    "GameState", "OrderResult", "RemotePosition",
    "IncrementalIndicators", "assert_causal", "verify_no_lookahead",
    "LiveConfig", "LiveStats", "LiveTrader",
    "Candle", "HistoricalCsvSource", "MarketSeries", "SynthWalkSource",
    "Performance", "analyze",
    "RiskDecision", "RiskManager",
    "SimExecution",
    "Signal", "SignalEngine",
]
