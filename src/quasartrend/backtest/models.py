"""Immutable accounting records for Phase 3 event-driven backtesting."""

from __future__ import annotations

from dataclasses import dataclass
import math

from quasartrend.replay import ReplayTrace
from quasartrend.strategy import Direction, StrategyEvent, StrategyState


@dataclass(frozen=True, slots=True)
class BacktestConfig:
    quantity: float = 1.0
    fee_bps: float = 0.0
    slippage_bps: float = 0.0

    def __post_init__(self) -> None:
        if isinstance(self.quantity, bool) or not math.isfinite(self.quantity) or self.quantity <= 0.0:
            raise ValueError("quantity must be finite and positive")
        for name in ("fee_bps", "slippage_bps"):
            value = getattr(self, name)
            if isinstance(value, bool) or not math.isfinite(value) or value < 0.0:
                raise ValueError(f"{name} must be finite and non-negative")


@dataclass(frozen=True, slots=True)
class ClosedTrade:
    """A closed accounting trade; ``trade_return = net_pnl / execution_entry_notional``."""

    trade_id: str
    symbol: str
    side: Direction
    entry_timestamp: int
    exit_timestamp: int
    canonical_entry_price: float
    canonical_exit_price: float
    execution_entry_price: float
    execution_exit_price: float
    quantity: float
    gross_pnl: float
    entry_fee: float
    exit_fee: float
    total_fees: float
    net_pnl: float
    trade_return: float
    exit_reason: str
    setup_origin_timestamp: int | None
    bias_epoch: int | None


@dataclass(frozen=True, slots=True)
class EquityPoint:
    timestamp: int
    trade_id: str
    realized_equity: float


@dataclass(frozen=True, slots=True)
class BacktestMetrics:
    total_closed_trades: int
    winning_trades: int
    losing_trades: int
    flat_trades: int
    win_rate: float | None
    gross_profit: float
    gross_loss: float
    net_profit: float
    total_fees: float
    average_trade_pnl: float | None
    average_winner: float | None
    average_loser: float | None
    profit_factor: float | None
    max_drawdown: float
    max_drawdown_percentage: None
    long_trade_count: int
    short_trade_count: int


@dataclass(frozen=True, slots=True)
class BacktestResult:
    final_strategy_state: StrategyState
    replay_traces: tuple[ReplayTrace, ...]
    strategy_events: tuple[StrategyEvent, ...]
    closed_trades: tuple[ClosedTrade, ...]
    equity_curve: tuple[EquityPoint, ...]
    metrics: BacktestMetrics
    diagnostics: tuple[str, ...] = ()
