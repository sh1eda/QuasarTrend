"""Immutable Phase 3 historical-bar and replay domain records.

Timestamps are epoch milliseconds identifying a candle's *open*.  A bar only
becomes available to replay at :attr:`HistoricalBar.finalized_at`.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
import math

from quasartrend.strategy import Direction, StrategyBar, StrategyEvent, StrategyState


class Timeframe(str, Enum):
    MINUTES_15 = "15m"
    HOURS_4 = "4h"

    @property
    def duration_ms(self) -> int:
        return 900_000 if self is Timeframe.MINUTES_15 else 14_400_000

    @property
    def priority(self) -> int:
        # A newly finalized 4H value is legal for the coincident 15m decision.
        return 0 if self is Timeframe.HOURS_4 else 1


@dataclass(frozen=True, slots=True)
class HistoricalBar:
    """One finalized source OHLC(V) candle; no aggregation is implied."""

    symbol: str
    timeframe: Timeframe
    open_time: int
    open: float
    high: float
    low: float
    close: float
    volume: float | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.symbol, str) or not self.symbol:
            raise ValueError("symbol must be non-empty")
        if not isinstance(self.timeframe, Timeframe):
            raise TypeError("timeframe must be a supported Timeframe")
        if isinstance(self.open_time, bool) or not isinstance(self.open_time, int):
            raise TypeError("open_time must be an integer epoch milliseconds")
        for name in ("open", "high", "low", "close"):
            if not math.isfinite(getattr(self, name)):
                raise ValueError(f"{name} must be finite")
        if self.high < self.low:
            raise ValueError("high must be greater than or equal to low")
        if self.volume is not None and (
            not math.isfinite(self.volume) or self.volume < 0.0
        ):
            raise ValueError("volume must be finite and non-negative when present")

    @property
    def finalized_at(self) -> int:
        return self.open_time + self.timeframe.duration_ms

    @property
    def processing_key(self) -> tuple[int, int]:
        return (self.finalized_at, self.timeframe.priority)


@dataclass(frozen=True, slots=True)
class ReplayConfig:
    """Indicator configuration for the fixed initial 15m/4H replay topology."""

    ltf_hema_fast_length: int = 20
    ltf_hema_slow_length: int = 40
    htf_hema_fast_length: int = 20
    htf_hema_slow_length: int = 40
    kalman_period: int = 21
    kalman_alpha: float = 0.01
    kalman_beta: float = 0.1
    kalman_factor: float = 1.0
    kalman_atr_period: int = 7

    def __post_init__(self) -> None:
        for name in (
            "ltf_hema_fast_length", "ltf_hema_slow_length", "htf_hema_fast_length",
            "htf_hema_slow_length", "kalman_period", "kalman_atr_period",
        ):
            value = getattr(self, name)
            if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
                raise ValueError(f"{name} must be a positive integer")
        for name in ("kalman_alpha", "kalman_beta", "kalman_factor"):
            value = getattr(self, name)
            if isinstance(value, bool) or not math.isfinite(value):
                raise ValueError(f"{name} must be finite")


@dataclass(frozen=True, slots=True)
class ReplayState:
    """Equality-friendly complete incremental replay state.

    Indicator checkpoints are strict JSON strings, which intentionally turn
    Pine ``na`` values into JSON null before dataclass equality is evaluated.
    """

    symbol: str
    strategy_state: StrategyState
    ltf_hema_checkpoint: str
    ltf_kalman_checkpoint: str
    htf_hema_checkpoint: str
    latest_htf_bias: Direction | None = None
    chronology_cursor: tuple[int, int] | None = None

    def __post_init__(self) -> None:
        if not self.symbol:
            raise ValueError("symbol must be non-empty")
        if self.strategy_state.symbol != self.symbol:
            raise ValueError("strategy state symbol must match replay state")
        if self.latest_htf_bias is not None and not isinstance(self.latest_htf_bias, Direction):
            raise TypeError("latest_htf_bias must be a Direction or None")


@dataclass(frozen=True, slots=True)
class ReplayTrace:
    """One source-bar update, including the Phase 2 call for a 15m bar."""

    source_bar: HistoricalBar
    strategy_bar: StrategyBar | None
    events: tuple[StrategyEvent, ...]
    post_state: StrategyState
    htf_bias_after_update: Direction | None


@dataclass(frozen=True, slots=True)
class ReplayStepResult:
    state: ReplayState
    trace: ReplayTrace


@dataclass(frozen=True, slots=True)
class ReplayResult:
    state: ReplayState
    traces: tuple[ReplayTrace, ...] = ()
    diagnostics: tuple[str, ...] = field(default_factory=tuple)

    @property
    def events(self) -> tuple[StrategyEvent, ...]:
        return tuple(event for trace in self.traces for event in trace.events)

    @property
    def final_strategy_state(self) -> StrategyState:
        return self.state.strategy_state
