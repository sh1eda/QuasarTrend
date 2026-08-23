"""Deterministic, exchange-independent paper-execution domain records.

The records in this module deliberately carry no wall-clock or network state.
They are the durable boundary between finalized strategy events and a simulated
order/position ledger.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from enum import Enum
import hashlib
import json
import math
from typing import Any, Protocol, runtime_checkable

from quasartrend.persistence import PersistenceIdentity
from quasartrend.strategy import Direction


EXECUTION_SCHEMA_VERSION = 1
EXECUTION_STATE_VERSION = 1
PAPER_FILL_MODEL_VERSION = "finalized-event-v1"
PAPER_ADAPTER_VERSION = "paper-v1"


class ExecutionError(RuntimeError):
    """Base class for an execution operation that cannot safely proceed."""


class InvalidIntentError(ExecutionError):
    pass


class ConflictingDuplicateIntentError(ExecutionError):
    pass


class OrderTransitionError(ExecutionError):
    pass


class PositionTransitionError(ExecutionError):
    pass


class OrderRejectedError(ExecutionError):
    pass


class AdapterTransientError(ExecutionError):
    pass


class AdapterFailureError(ExecutionError):
    pass


class ExecutionPersistenceError(ExecutionError):
    pass


class ExecutionIdentityMismatchError(ExecutionError):
    pass


class ExecutionBootstrapRequiredError(ExecutionError):
    pass


class ReconciliationMismatchError(ExecutionError):
    pass


class ExecutionChronologyError(ExecutionError):
    pass


class IntentType(str, Enum):
    ENTRY = "entry"
    EXIT = "exit"
    STOP = "stop"


class OrderStatus(str, Enum):
    NEW = "new"
    ACCEPTED = "accepted"
    FILLED = "filled"
    REJECTED = "rejected"


class PositionStatus(str, Enum):
    FLAT = "flat"
    OPEN = "open"


class PaperDecision(str, Enum):
    ACCEPT_FILL = "accept_fill"
    ACCEPT_ONLY = "accept_only"
    REJECT = "reject"
    DEFER = "defer"


class AdapterEventType(str, Enum):
    ACCEPTED = "accepted"
    REJECTED = "rejected"
    FILLED = "filled"


class DivergenceClassification(str, Enum):
    HEALTHY_FLAT = "healthy_flat"
    HEALTHY_OPEN = "healthy_open"
    PENDING_ENTRY = "pending_entry"
    REJECTED_ENTRY = "rejected_entry"
    PENDING_EXIT = "pending_exit"
    REJECTED_EXIT = "rejected_exit"


@dataclass(frozen=True, slots=True)
class ExecutionDivergence:
    classification: DivergenceClassification
    trade_id: str | None = None


def _finite(value: object, name: str, *, positive: bool = False, nonnegative: bool = False) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value):
        raise ValueError(f"{name} must be finite")
    number = float(value)
    if positive and number <= 0.0:
        raise ValueError(f"{name} must be positive")
    if nonnegative and number < 0.0:
        raise ValueError(f"{name} must be non-negative")
    return number


def canonical_json(payload: object) -> str:
    """Canonical strict JSON used for all stable execution identities."""
    return json.dumps(payload, allow_nan=False, sort_keys=True, separators=(",", ":"))


def stable_id(domain: str, payload: object) -> str:
    if not domain:
        raise ValueError("stable identifier domain must be non-empty")
    encoded = canonical_json({"domain": domain, "version": 1, "payload": payload})
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()


@dataclass(frozen=True, slots=True)
class PaperExecutionConfig:
    """Paper-only fill configuration, intentionally distinct from BacktestConfig."""

    quantity: float = 1.0
    fee_bps: float = 0.0
    slippage_bps: float = 0.0
    decision: PaperDecision = PaperDecision.ACCEPT_FILL

    def __post_init__(self) -> None:
        object.__setattr__(self, "quantity", _finite(self.quantity, "quantity", positive=True))
        object.__setattr__(self, "fee_bps", _finite(self.fee_bps, "fee_bps", nonnegative=True))
        object.__setattr__(self, "slippage_bps", _finite(self.slippage_bps, "slippage_bps", nonnegative=True))
        if not isinstance(self.decision, PaperDecision):
            raise TypeError("decision must be a PaperDecision")


@dataclass(frozen=True, slots=True)
class ExecutionIdentity:
    """Full durable identity of one symbol's Phase 6 paper ledger."""

    symbol: str
    config_fingerprint: str
    quantity: float = 1.0
    fee_bps: float = 0.0
    slippage_bps: float = 0.0
    schema_version: int = EXECUTION_SCHEMA_VERSION
    state_version: int = EXECUTION_STATE_VERSION
    fill_model_version: str = PAPER_FILL_MODEL_VERSION
    adapter_version: str = PAPER_ADAPTER_VERSION
    decision: PaperDecision = PaperDecision.ACCEPT_FILL
    fingerprint: str = field(init=False)

    def __post_init__(self) -> None:
        if not isinstance(self.symbol, str) or not self.symbol:
            raise ValueError("symbol must be non-empty")
        if (not isinstance(self.config_fingerprint, str) or len(self.config_fingerprint) != 64
                or any(character not in "0123456789abcdef" for character in self.config_fingerprint)):
            raise ValueError("config_fingerprint must be a SHA-256 hex digest")
        for name in ("schema_version", "state_version"):
            value = getattr(self, name)
            if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
                raise ValueError(f"{name} must be a positive integer")
        if not self.fill_model_version or not self.adapter_version or not isinstance(self.decision, PaperDecision):
            raise ValueError("execution version strings must be non-empty")
        object.__setattr__(self, "quantity", _finite(self.quantity, "quantity", positive=True))
        object.__setattr__(self, "fee_bps", _finite(self.fee_bps, "fee_bps", nonnegative=True))
        object.__setattr__(self, "slippage_bps", _finite(self.slippage_bps, "slippage_bps", nonnegative=True))
        object.__setattr__(self, "fingerprint", stable_id("quasartrend.execution.identity", self.payload))

    @classmethod
    def from_persistence(
        cls, identity: PersistenceIdentity, config: PaperExecutionConfig | None = None
    ) -> "ExecutionIdentity":
        config = config or PaperExecutionConfig()
        return cls(
            identity.symbol, identity.config_fingerprint, config.quantity, config.fee_bps, config.slippage_bps,
            decision=config.decision,
        )

    @property
    def payload(self) -> dict[str, object]:
        return {
            "adapter_version": self.adapter_version,
            "config_fingerprint": self.config_fingerprint,
            "fee_bps": self.fee_bps,
            "fill_model_version": self.fill_model_version,
            "quantity": self.quantity,
            "schema_version": self.schema_version,
            "state_version": self.state_version,
            "slippage_bps": self.slippage_bps,
            "symbol": self.symbol,
            "decision": self.decision.value,
        }

    @property
    def canonical_json(self) -> str:
        return canonical_json(self.payload)


@dataclass(frozen=True, slots=True)
class ExecutionIntent:
    intent_id: str
    decision_slot_id: str
    identity_fingerprint: str
    symbol: str
    source_processing_key: tuple[int, int]
    event_ordinal: int
    type: IntentType
    reason: str
    trade_id: str
    side: Direction
    canonical_price: float
    quantity: float

    def __post_init__(self) -> None:
        if not self.intent_id or not self.decision_slot_id or not self.identity_fingerprint or not self.symbol or not self.trade_id:
            raise InvalidIntentError("intent identity, symbol, and trade_id must be non-empty")
        if (not isinstance(self.source_processing_key, tuple) or len(self.source_processing_key) != 2
                or any(isinstance(value, bool) or not isinstance(value, int) for value in self.source_processing_key)):
            raise InvalidIntentError("source_processing_key must be an integer pair")
        if isinstance(self.event_ordinal, bool) or not isinstance(self.event_ordinal, int) or self.event_ordinal < 0:
            raise InvalidIntentError("event_ordinal must be a non-negative integer")
        if not isinstance(self.type, IntentType) or not isinstance(self.side, Direction) or not isinstance(self.reason, str) or not self.reason:
            raise InvalidIntentError("intent type and side are required")
        object.__setattr__(self, "canonical_price", _finite(self.canonical_price, "canonical_price", positive=True))
        object.__setattr__(self, "quantity", _finite(self.quantity, "quantity", positive=True))

    @property
    def payload(self) -> dict[str, object]:
        return {
            "canonical_price": self.canonical_price, "event_ordinal": self.event_ordinal,
            "decision_slot_id": self.decision_slot_id, "identity_fingerprint": self.identity_fingerprint, "quantity": self.quantity,
            "side": self.side.value, "source_processing_key": list(self.source_processing_key),
            "symbol": self.symbol, "trade_id": self.trade_id, "type": self.type.value, "reason": self.reason,
        }


@dataclass(frozen=True, slots=True)
class PaperOrder:
    order_id: str
    intent: ExecutionIntent
    status: OrderStatus = OrderStatus.NEW

    def __post_init__(self) -> None:
        if not self.order_id:
            raise InvalidIntentError("order_id must be non-empty")
        if not isinstance(self.status, OrderStatus):
            raise TypeError("status must be an OrderStatus")


@dataclass(frozen=True, slots=True)
class PaperFill:
    fill_id: str
    order_id: str
    intent_id: str
    symbol: str
    trade_id: str
    side: Direction
    type: IntentType
    quantity: float
    canonical_price: float
    execution_price: float
    fee: float

    def __post_init__(self) -> None:
        if not all((self.fill_id, self.order_id, self.intent_id, self.symbol, self.trade_id)):
            raise InvalidIntentError("fill identity fields must be non-empty")
        if not isinstance(self.side, Direction) or not isinstance(self.type, IntentType):
            raise InvalidIntentError("fill side and type are required")
        for name, positive in (("quantity", True), ("canonical_price", True), ("execution_price", True), ("fee", False)):
            object.__setattr__(self, name, _finite(getattr(self, name), name, positive=positive, nonnegative=not positive))


@dataclass(frozen=True, slots=True)
class PaperPosition:
    symbol: str
    status: PositionStatus = PositionStatus.FLAT
    trade_id: str | None = None
    side: Direction | None = None
    quantity: float | None = None
    entry_order_id: str | None = None
    entry_fill_id: str | None = None
    entry_execution_price: float | None = None
    position_id: str | None = None

    @classmethod
    def flat(cls, symbol: str) -> "PaperPosition":
        return cls(symbol)

    def __post_init__(self) -> None:
        if not self.symbol or not isinstance(self.status, PositionStatus):
            raise ValueError("position symbol/status is invalid")
        fields = (self.trade_id, self.side, self.quantity, self.entry_order_id, self.entry_fill_id, self.entry_execution_price, self.position_id)
        if self.status is PositionStatus.FLAT:
            if any(value is not None for value in fields):
                raise ValueError("flat position cannot carry an open trade")
        else:
            if not all(value is not None for value in fields) or not isinstance(self.side, Direction):
                raise ValueError("open position requires complete entry data")
            object.__setattr__(self, "quantity", _finite(self.quantity, "quantity", positive=True))
            object.__setattr__(self, "entry_execution_price", _finite(self.entry_execution_price, "entry_execution_price", positive=True))


@dataclass(frozen=True, slots=True)
class ExecutionState:
    """Complete deterministic execution ledger for one execution identity."""

    identity: ExecutionIdentity
    intents: tuple[ExecutionIntent, ...] = ()
    orders: tuple[PaperOrder, ...] = ()
    fills: tuple[PaperFill, ...] = ()
    position: PaperPosition | None = None
    last_source_processing_key: tuple[int, int] | None = None

    @classmethod
    def initial(cls, identity: ExecutionIdentity) -> "ExecutionState":
        return cls(identity=identity, position=PaperPosition.flat(identity.symbol))

    def __post_init__(self) -> None:
        if self.position is None:
            object.__setattr__(self, "position", PaperPosition.flat(self.identity.symbol))
        if self.position.symbol != self.identity.symbol:
            raise ExecutionIdentityMismatchError("position symbol must match execution identity")
        if len({intent.intent_id for intent in self.intents}) != len(self.intents):
            raise ConflictingDuplicateIntentError("execution state contains duplicate intent IDs")
        if len({intent.decision_slot_id for intent in self.intents}) != len(self.intents):
            raise ConflictingDuplicateIntentError("execution state contains duplicate decision slots")
        if len({order.order_id for order in self.orders}) != len(self.orders):
            raise OrderTransitionError("execution state contains duplicate order IDs")
        if len({fill.fill_id for fill in self.fills}) != len(self.fills):
            raise OrderTransitionError("execution state contains duplicate fill IDs")
        intent_by_id = {intent.intent_id: intent for intent in self.intents}
        order_by_id = {order.order_id: order for order in self.orders}
        if len(intent_by_id) != len(order_by_id):
            raise OrderTransitionError("each intent must have exactly one order")
        for order in self.orders:
            if intent_by_id.get(order.intent.intent_id) != order.intent:
                raise OrderTransitionError("order intent is absent or conflicts with the ledger")
            if order.order_id != stable_id("quasartrend.execution.order", {"intent_id": order.intent.intent_id}):
                raise OrderTransitionError("order ID does not match its deterministic intent ID")
        fills_by_order: dict[str, list[PaperFill]] = {}
        for fill in self.fills:
            fills_by_order.setdefault(fill.order_id, []).append(fill)
            order = order_by_id.get(fill.order_id)
            if order is None or order.status is not OrderStatus.FILLED or order.intent.intent_id != fill.intent_id:
                raise OrderTransitionError("fill does not match a filled order")
            expected_fill_id = stable_id("quasartrend.execution.fill", {"order_id": fill.order_id, "fill_ordinal": 0})
            if fill.fill_id != expected_fill_id:
                raise OrderTransitionError("fill ID does not match its deterministic order ID")
        for order in self.orders:
            fill_count = len(fills_by_order.get(order.order_id, ()))
            if (order.status is OrderStatus.FILLED and fill_count != 1) or (
                order.status is not OrderStatus.FILLED and fill_count != 0
            ):
                raise OrderTransitionError("paper model permits exactly one fill only for filled orders")
        if self.last_source_processing_key is not None:
            if (not isinstance(self.last_source_processing_key, tuple) or len(self.last_source_processing_key) != 2
                    or any(isinstance(value, bool) or not isinstance(value, int) for value in self.last_source_processing_key)):
                raise ExecutionChronologyError("last_source_processing_key must be an integer pair")


@dataclass(frozen=True, slots=True)
class PaperAdapterSnapshot:
    """Durable paper-adapter view used for restart reconciliation."""

    identity_fingerprint: str
    orders: tuple[PaperOrder, ...]
    fills: tuple[PaperFill, ...]
    position: PaperPosition


@dataclass(frozen=True, slots=True)
class AdapterEvent:
    event_id: str
    order_id: str
    type: AdapterEventType
    fill: PaperFill | None = None

    def __post_init__(self) -> None:
        if not self.event_id or not self.order_id or not isinstance(self.type, AdapterEventType):
            raise AdapterFailureError("adapter event identity/type is invalid")
        if (self.type is AdapterEventType.FILLED) != (self.fill is not None):
            raise AdapterFailureError("only a filled adapter event carries a fill")


@dataclass(frozen=True, slots=True)
class PaperAdapterState:
    identity_fingerprint: str
    orders: tuple[PaperOrder, ...] = ()
    fills: tuple[PaperFill, ...] = ()
    position: PaperPosition | None = None
    events: tuple[AdapterEvent, ...] = ()
    replay_cursor: tuple[int, int] | None = None

    def __post_init__(self) -> None:
        if not self.identity_fingerprint or self.position is None:
            raise AdapterFailureError("adapter state identity/position is required")
        if len({order.order_id for order in self.orders}) != len(self.orders):
            raise AdapterFailureError("adapter state contains duplicate orders")
        if len({fill.fill_id for fill in self.fills}) != len(self.fills):
            raise AdapterFailureError("adapter state contains duplicate fills")
        if len({event.event_id for event in self.events}) != len(self.events):
            raise AdapterFailureError("adapter state contains duplicate event IDs")


@runtime_checkable
class ExecutionAdapter(Protocol):
    """Exchange-independent, deterministic adapter boundary (paper only here)."""

    def initial_state(self) -> PaperAdapterState: ...
    def register_order(self, state: PaperAdapterState, order: PaperOrder) -> PaperAdapterState: ...
    def events_for_new_order(self, order: PaperOrder) -> tuple[AdapterEvent, ...]: ...
    def accepted_event(self, order: PaperOrder) -> AdapterEvent: ...
    def rejected_event(self, order: PaperOrder) -> AdapterEvent: ...
    def filled_event(self, order: PaperOrder) -> AdapterEvent: ...
    def apply_event(self, state: PaperAdapterState, event: AdapterEvent) -> PaperAdapterState: ...
    def snapshot(self, state: PaperAdapterState) -> PaperAdapterSnapshot: ...
    def reconcile(self, state: PaperAdapterState, observed: PaperAdapterSnapshot) -> None: ...


@dataclass(frozen=True, slots=True)
class ExecutionTransition:
    state: ExecutionState
    adapter_snapshot: PaperAdapterSnapshot
    intents: tuple[ExecutionIntent, ...] = ()


def position_to_data(position: PaperPosition) -> dict[str, Any]:
    return {
        "entry_execution_price": position.entry_execution_price, "entry_fill_id": position.entry_fill_id,
        "entry_order_id": position.entry_order_id, "quantity": position.quantity,
        "side": None if position.side is None else position.side.value, "status": position.status.value,
        "symbol": position.symbol, "trade_id": position.trade_id, "position_id": position.position_id,
    }


def position_from_data(data: object) -> PaperPosition:
    if not isinstance(data, dict) or set(data) != {"entry_execution_price", "entry_fill_id", "entry_order_id", "quantity", "side", "status", "symbol", "trade_id", "position_id"}:
        raise ExecutionPersistenceError("invalid paper position payload")
    try:
        return PaperPosition(data["symbol"], PositionStatus(data["status"]), data["trade_id"], None if data["side"] is None else Direction(data["side"]), data["quantity"], data["entry_order_id"], data["entry_fill_id"], data["entry_execution_price"], data["position_id"])
    except (TypeError, ValueError) as exc:
        raise ExecutionPersistenceError("invalid paper position payload") from exc
