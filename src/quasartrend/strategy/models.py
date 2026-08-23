"""Immutable domain types for the deterministic Phase 2 strategy engine."""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
import math


class Direction(str, Enum):
    """A tradeable directional state supplied by the indicator layer."""

    LONG = "long"
    SHORT = "short"


class StrategyStatus(str, Enum):
    WARMING_UP = "warming_up"
    FLAT = "flat"
    PENDING_LONG = "pending_long"
    PENDING_SHORT = "pending_short"
    OPEN_LONG = "open_long"
    OPEN_SHORT = "open_short"
    DATA_BLOCKED = "data_blocked"


class ReadinessState(str, Enum):
    READY = "ready"
    WARMING_UP = "warming_up"
    DATA_BLOCKED = "data_blocked"


class ConfirmationMode(str, Enum):
    STATEFUL_EITHER_ORDER = "stateful_either_order"
    SAME_CANDLE = "same_candle"
    HEMA_THEN_KALMAN_EVENT = "hema_then_kalman_event"


class BiasReversalBehavior(str, Enum):
    EXIT = "exit"
    HOLD_UNTIL_LTF_EXIT = "hold_until_ltf_exit"


class EventType(str, Enum):
    HTF_BIAS_CHANGED = "htf_bias_changed"
    HEMA_FLIP_DETECTED = "hema_flip_detected"
    KALMAN_TRANSITION_DETECTED = "kalman_transition_detected"
    SETUP_ARMED = "setup_armed"
    SETUP_CANCELLED = "setup_cancelled"
    TRADE_OPENED = "trade_opened"
    TRADE_CLOSED = "trade_closed"
    STOP_HIT = "stop_hit"
    DECISION_REJECTED = "decision_rejected"


class ReasonCode(str, Enum):
    NO_HTF_BIAS = "no_htf_bias"
    STRATEGY_NOT_READY = "strategy_not_ready"
    REQUIRED_DATA_UNAVAILABLE = "required_data_unavailable"
    INVALID_ATR = "invalid_atr"
    HEMA_FLIP_WRONG_DIRECTION = "hema_flip_wrong_direction"
    HEMA_FLIP_BEFORE_BIAS_EPOCH = "hema_flip_before_bias_epoch"
    KALMAN_NOT_CONFIRMED = "kalman_not_confirmed"
    PENDING_SETUP_ARMED = "pending_setup_armed"
    PENDING_SETUP_CANCELLED_BY_HEMA = "pending_setup_cancelled_by_hema"
    PENDING_SETUP_CANCELLED_BY_BIAS = "pending_setup_cancelled_by_bias"
    PENDING_SETUP_CANCELLED_BY_READINESS = "pending_setup_cancelled_by_readiness"
    POSITION_ALREADY_OPEN = "position_already_open"
    ENTRY_ACCEPTED = "entry_accepted"
    EXIT_STOP = "exit_stop"
    EXIT_HTF_REVERSAL = "exit_htf_reversal"
    EXIT_HEMA_FLIP = "exit_hema_flip"
    NO_SAME_BAR_REVERSAL = "no_same_bar_reversal"
    STALE_BIAS_EPOCH = "stale_bias_epoch"
    NO_FRESH_HEMA_FLIP = "no_fresh_hema_flip"
    DUPLICATE_TIMESTAMP = "duplicate_timestamp"
    HTF_BIAS_CHANGED = "htf_bias_changed"
    HEMA_FLIP_DETECTED = "hema_flip_detected"
    KALMAN_TRANSITION_DETECTED = "kalman_transition_detected"


class OutOfOrderTimestampError(ValueError):
    """Raised when a caller supplies a bar older than the state cursor."""


@dataclass(frozen=True, slots=True)
class StrategyConfig:
    """Fixed configuration owned by a :class:`StrategyEngine`."""

    atr_multiplier: float = 1.0
    confirmation_mode: ConfirmationMode = ConfirmationMode.STATEFUL_EITHER_ORDER
    bias_reversal_behavior: BiasReversalBehavior = BiasReversalBehavior.EXIT

    def __post_init__(self) -> None:
        if not math.isfinite(self.atr_multiplier) or self.atr_multiplier <= 0.0:
            raise ValueError("atr_multiplier must be finite and positive")


@dataclass(frozen=True, slots=True)
class StrategyBar:
    """One finalized 15m domain bar and its finalized indicator snapshots.

    ``hema_flip`` and ``kalman_transition`` are explicit indicator events.  A
    direction change without the corresponding event is deliberately not an
    entry trigger.
    """

    symbol: str
    timestamp: int
    open: float
    high: float
    low: float
    close: float
    htf_bias: Direction | None
    hema_direction: Direction | None
    kalman_direction: Direction | None
    atr: float | None
    strategy_ready: bool = True
    hema_flip: Direction | None = None
    kalman_transition: Direction | None = None

    def __post_init__(self) -> None:
        if not self.symbol:
            raise ValueError("symbol must be non-empty")
        if isinstance(self.timestamp, bool) or not isinstance(self.timestamp, int):
            raise TypeError("timestamp must be an integer")
        for name in (
            "htf_bias",
            "hema_direction",
            "kalman_direction",
            "hema_flip",
            "kalman_transition",
        ):
            value = getattr(self, name)
            if value is not None and not isinstance(value, Direction):
                raise TypeError(f"{name} must be a Direction or None")
        if not isinstance(self.strategy_ready, bool):
            raise TypeError("strategy_ready must be a bool")
        if self.hema_flip is not None and self.hema_flip is not self.hema_direction:
            raise ValueError("hema_flip must match hema_direction on the same bar")
        if (
            self.kalman_transition is not None
            and self.kalman_transition is not self.kalman_direction
        ):
            raise ValueError(
                "kalman_transition must match kalman_direction on the same bar"
            )
        for name in ("open", "high", "low", "close"):
            if not math.isfinite(getattr(self, name)):
                raise ValueError(f"{name} must be finite")
        if self.high < self.low:
            raise ValueError("high must be greater than or equal to low")


@dataclass(frozen=True, slots=True)
class OpenTrade:
    trade_id: str
    side: Direction
    entry_price: float
    entry_timestamp: int
    atr_at_entry: float
    stop_price: float
    bias_epoch: int
    setup_origin_timestamp: int

    def __post_init__(self) -> None:
        if not isinstance(self.side, Direction):
            raise TypeError("open trade side must be a Direction")
        if (
            not math.isfinite(self.entry_price)
            or not math.isfinite(self.atr_at_entry)
            or not math.isfinite(self.stop_price)
            or self.atr_at_entry <= 0.0
        ):
            raise ValueError("open trade prices and ATR must be finite, with positive ATR")
        if self.side is Direction.LONG and not self.stop_price < self.entry_price:
            raise ValueError("a long stop must be below entry")
        if self.side is Direction.SHORT and not self.stop_price > self.entry_price:
            raise ValueError("a short stop must be above entry")


@dataclass(frozen=True, slots=True)
class StrategyEvent:
    """A small equality-friendly event record for decisions and diagnostics."""

    type: EventType
    symbol: str
    timestamp: int
    reason: ReasonCode
    trade_id: str | None = None
    side: Direction | None = None
    price: float | None = None
    reasons: tuple[ReasonCode, ...] = ()
    metadata: tuple[tuple[str, str | int | float], ...] = ()


@dataclass(frozen=True, slots=True)
class StrategyState:
    """Serialization-ready state belonging to exactly one symbol."""

    symbol: str
    status: StrategyStatus = StrategyStatus.WARMING_UP
    current_bias: Direction | None = None
    previous_bias: Direction | None = None
    bias_epoch: int = 0
    bias_activation_timestamp: int | None = None
    current_hema: Direction | None = None
    previous_hema: Direction | None = None
    current_kalman: Direction | None = None
    previous_kalman: Direction | None = None
    pending_direction: Direction | None = None
    pending_flip_timestamp: int | None = None
    pending_bias_epoch: int | None = None
    trade: OpenTrade | None = None
    next_trade_sequence: int = 1
    last_timestamp: int | None = None
    readiness: ReadinessState = ReadinessState.WARMING_UP

    @classmethod
    def initial(cls, symbol: str) -> "StrategyState":
        return cls(symbol=symbol)

    def __post_init__(self) -> None:
        if not self.symbol:
            raise ValueError("symbol must be non-empty")
        for name in (
            "current_bias",
            "previous_bias",
            "current_hema",
            "previous_hema",
            "current_kalman",
            "previous_kalman",
            "pending_direction",
        ):
            value = getattr(self, name)
            if value is not None and not isinstance(value, Direction):
                raise TypeError(f"{name} must be a Direction or None")
        if self.bias_epoch < 0 or self.next_trade_sequence < 1:
            raise ValueError("epochs and trade sequences must be non-negative/positive")
        pending = self.pending_direction is not None
        pending_fields_present = (
            self.pending_flip_timestamp is not None
            and self.pending_bias_epoch is not None
        )
        if pending != pending_fields_present:
            raise ValueError("pending setup fields must be all present or all absent")
        if self.trade is None:
            if self.status in (StrategyStatus.OPEN_LONG, StrategyStatus.OPEN_SHORT):
                raise ValueError("open status requires a trade")
        else:
            expected = StrategyStatus.OPEN_LONG if self.trade.side is Direction.LONG else StrategyStatus.OPEN_SHORT
            if self.status is not expected:
                raise ValueError("open trade side and status disagree")
            if pending:
                raise ValueError("an open trade cannot retain a pending setup")
        if pending:
            expected_status = (
                StrategyStatus.PENDING_LONG
                if self.pending_direction is Direction.LONG
                else StrategyStatus.PENDING_SHORT
            )
            if self.status is not expected_status:
                raise ValueError("pending setup direction and status disagree")
        elif self.status in (StrategyStatus.PENDING_LONG, StrategyStatus.PENDING_SHORT):
            raise ValueError("pending status requires a matching pending setup")
        if pending and self.pending_bias_epoch != self.bias_epoch:
            raise ValueError("pending setup must belong to the active bias epoch")


@dataclass(frozen=True, slots=True)
class StrategyResult:
    state: StrategyState
    events: tuple[StrategyEvent, ...]
