"""Pure deterministic execution-intent, order, fill, and position transitions."""

from __future__ import annotations

from dataclasses import replace
import math

from quasartrend.replay import ReplayTrace
from quasartrend.strategy import Direction, EventType, ReasonCode, StrategyEvent

from .models import (
    AdapterEvent, AdapterEventType,
    AdapterFailureError,
    ConflictingDuplicateIntentError,
    ExecutionChronologyError,
    ExecutionIdentity,
    ExecutionIdentityMismatchError,
    ExecutionIntent,
    ExecutionState,
    ExecutionTransition,
    IntentType,
    InvalidIntentError,
    OrderRejectedError,
    OrderStatus,
    OrderTransitionError,
    PaperAdapterSnapshot,
    PaperDecision,
    PaperExecutionConfig,
    PaperFill,
    PaperOrder,
    PaperPosition,
    PositionStatus,
    PositionTransitionError,
    stable_id,
)


class PaperExecutionEngine:
    """A stateless transition engine for the finalized-event-v1 paper model."""

    def __init__(self, identity: ExecutionIdentity, config: PaperExecutionConfig | None = None) -> None:
        self.identity = identity
        self.config = config or PaperExecutionConfig(
            quantity=identity.quantity, fee_bps=identity.fee_bps, slippage_bps=identity.slippage_bps,
            decision=identity.decision,
        )
        if (self.config.quantity, self.config.fee_bps, self.config.slippage_bps, self.config.decision) != (
            identity.quantity, identity.fee_bps, identity.slippage_bps, identity.decision,
        ):
            raise ExecutionIdentityMismatchError("paper configuration must match execution identity")

    def initial_state(self) -> ExecutionState:
        return ExecutionState.initial(self.identity)

    def intent_from_event(self, trace: ReplayTrace, event: StrategyEvent, event_ordinal: int) -> ExecutionIntent | None:
        self._validate_trace_event(trace, event, event_ordinal)
        if event.type is EventType.STOP_HIT:
            return None
        if event.type is EventType.TRADE_OPENED:
            kind = IntentType.ENTRY
        elif event.type is EventType.TRADE_CLOSED:
            kind = IntentType.STOP if event.reason is ReasonCode.EXIT_STOP else IntentType.EXIT
        else:
            return None
        if event.trade_id is None or event.side is None or event.price is None:
            raise InvalidIntentError(f"{event.type.value} requires trade_id, side, and price")
        if not math.isfinite(event.price) or event.price <= 0.0:
            raise InvalidIntentError("strategy event price must be finite and positive")
        slot_payload = {
            "event_ordinal": event_ordinal,
            "event_type": event.type.value,
            "execution_identity": self.identity.fingerprint,
            "source_processing_key": list(trace.source_bar.processing_key),
            "symbol": event.symbol,
            "trade_id": event.trade_id,
        }
        decision_slot_id = stable_id("quasartrend.execution.decision-slot", slot_payload)
        payload = {
            "canonical_price": event.price, "decision_slot_id": decision_slot_id,
            "identity_fingerprint": self.identity.fingerprint, "quantity": self.config.quantity,
            "reason": event.reason.value, "side": event.side.value, "symbol": event.symbol,
            "trade_id": event.trade_id, "type": kind.value,
        }
        return ExecutionIntent(
            stable_id("quasartrend.execution.intent", payload), decision_slot_id, self.identity.fingerprint,
            event.symbol, trace.source_bar.processing_key, event_ordinal, kind, event.reason.value,
            event.trade_id, event.side, event.price, self.config.quantity,
        )

    def process_trace(self, state: ExecutionState, trace: ReplayTrace) -> ExecutionTransition:
        self._validate_state(state)
        key = trace.source_bar.processing_key
        if state.last_source_processing_key is not None and key < state.last_source_processing_key:
            raise ExecutionChronologyError("source processing key regresses execution state")
        intents = tuple(
            intent for ordinal, event in enumerate(trace.events)
            if (intent := self.intent_from_event(trace, event, ordinal)) is not None
        )
        if key == state.last_source_processing_key:
            if not intents:
                return ExecutionTransition(state, self.snapshot(state), ())
            if any(next((item for item in state.intents if item.decision_slot_id == intent.decision_slot_id), None) != intent for intent in intents):
                raise ExecutionChronologyError("equal source key is not an exact duplicate decision delivery")
        if len({intent.trade_id for intent in intents if intent.type is IntentType.ENTRY}) and any(
            intent.type is not IntentType.ENTRY for intent in intents
        ):
            # Phase 2 forbids same-bar reversal; allowing such a trace would
            # make an ambiguous same-source position transition durable.
            raise ExecutionChronologyError("entry and close intents cannot share one source bar")
        candidate = state
        for intent in intents:
            candidate = self.submit_intent(candidate, intent)
        # A non-event trace still advances execution chronology, enabling a
        # combined checkpoint to prove it corresponds to the replay cursor.
        if key != state.last_source_processing_key:
            candidate = replace(candidate, last_source_processing_key=key)
        return ExecutionTransition(candidate, self.snapshot(candidate), intents)

    def submit_intent(self, state: ExecutionState, intent: ExecutionIntent) -> ExecutionState:
        self._validate_state(state)
        self._validate_intent(intent)
        existing = next((item for item in state.intents if item.decision_slot_id == intent.decision_slot_id), None)
        if existing is not None:
            if existing != intent:
                raise ConflictingDuplicateIntentError("same decision slot has a different payload")
            return state
        if state.last_source_processing_key is not None and intent.source_processing_key < state.last_source_processing_key:
            raise ExecutionChronologyError("intent source processing key regresses execution state")
        order_id = stable_id("quasartrend.execution.order", {"intent_id": intent.intent_id})
        order = PaperOrder(order_id, intent)
        candidate = replace(state, intents=state.intents + (intent,), orders=state.orders + (order,))
        if self.config.decision is PaperDecision.DEFER:
            return candidate
        if self.config.decision is PaperDecision.REJECT:
            return self.reject(candidate, order_id)
        candidate = self.acknowledge(candidate, order_id)
        if self.config.decision is PaperDecision.ACCEPT_ONLY:
            return candidate
        if self.config.decision is not PaperDecision.ACCEPT_FILL:
            raise AdapterFailureError("unsupported paper adapter decision")
        return self.fill(candidate, order_id)

    def register_intent(self, state: ExecutionState, intent: ExecutionIntent) -> ExecutionState:
        """Create exactly one durable NEW order without choosing an adapter outcome."""
        self._validate_state(state)
        self._validate_intent(intent)
        existing = next((item for item in state.intents if item.decision_slot_id == intent.decision_slot_id), None)
        if existing is not None:
            if existing != intent:
                raise ConflictingDuplicateIntentError("same decision slot has a different payload")
            return state
        order = PaperOrder(stable_id("quasartrend.execution.order", {"intent_id": intent.intent_id}), intent)
        return replace(state, intents=state.intents + (intent,), orders=state.orders + (order,))

    def apply_adapter_event(self, state: ExecutionState, event: AdapterEvent) -> ExecutionState:
        expected_event_id = stable_id("quasartrend.execution.adapter-event", (
            {"order_id": event.order_id, "type": "filled", "fill_id": event.fill.fill_id}
            if event.type is AdapterEventType.FILLED else {"order_id": event.order_id, "type": event.type.value}
        ))
        if event.event_id != expected_event_id:
            raise AdapterFailureError("adapter event deterministic identity does not match payload")
        order = self._order(state, event.order_id)
        if event.type is AdapterEventType.ACCEPTED:
            return self.acknowledge(state, order.order_id)
        if event.type is AdapterEventType.REJECTED:
            return self.reject(state, order.order_id)
        if event.fill != self._make_fill(order):
            raise AdapterFailureError("adapter filled event differs from deterministic paper fill")
        return self.fill(state, order.order_id)

    def acknowledge(self, state: ExecutionState, order_id: str) -> ExecutionState:
        order = self._order(state, order_id)
        if order.status is OrderStatus.ACCEPTED or order.status is OrderStatus.FILLED:
            return state
        if order.status is OrderStatus.REJECTED:
            raise OrderRejectedError("rejected order cannot be acknowledged")
        if order.status is not OrderStatus.NEW:
            raise OrderTransitionError("only a new order may be accepted")
        return self._replace_order(state, replace(order, status=OrderStatus.ACCEPTED))

    def reject(self, state: ExecutionState, order_id: str) -> ExecutionState:
        order = self._order(state, order_id)
        if order.status is OrderStatus.REJECTED:
            return state
        if order.status is not OrderStatus.NEW:
            raise OrderTransitionError("only a new order may be rejected")
        return self._replace_order(state, replace(order, status=OrderStatus.REJECTED))

    def fill(self, state: ExecutionState, order_id: str) -> ExecutionState:
        order = self._order(state, order_id)
        if order.status is OrderStatus.FILLED:
            return state
        if order.status is OrderStatus.REJECTED:
            raise OrderRejectedError("rejected order cannot be filled")
        if order.status is not OrderStatus.ACCEPTED:
            raise OrderTransitionError("only an accepted order may be filled")
        fill = self._make_fill(order)
        if any(existing.fill_id == fill.fill_id for existing in state.fills):
            raise OrderTransitionError("accepted order already has a fill")
        # FILLED and its one complete fill are one immutable state transition;
        # never expose an internally inconsistent filled-without-fill ledger.
        return replace(
            state,
            orders=tuple(
                replace(order, status=OrderStatus.FILLED) if item.order_id == order_id else item
                for item in state.orders
            ),
            fills=state.fills + (fill,),
            position=self._apply_fill(state.position, fill),
        )

    def snapshot(self, state: ExecutionState) -> PaperAdapterSnapshot:
        self._validate_state(state)
        return PaperAdapterSnapshot(
            self.identity.fingerprint,
            state.orders, state.fills, state.position,
        )

    def reconcile(self, state: ExecutionState, observed: PaperAdapterSnapshot) -> None:
        expected = self.snapshot(state)
        if observed != expected:
            raise ReconciliationMismatchError("observed paper adapter snapshot differs from execution ledger")

    def _make_fill(self, order: PaperOrder) -> PaperFill:
        intent = order.intent
        fraction = self.config.slippage_bps / 10_000.0
        if intent.type is IntentType.ENTRY:
            execution_price = intent.canonical_price * (1.0 + fraction if intent.side is Direction.LONG else 1.0 - fraction)
        else:
            execution_price = intent.canonical_price * (1.0 - fraction if intent.side is Direction.LONG else 1.0 + fraction)
        fee = execution_price * intent.quantity * self.config.fee_bps / 10_000.0
        fill_id = stable_id("quasartrend.execution.fill", {"order_id": order.order_id, "fill_ordinal": 0})
        return PaperFill(fill_id, order.order_id, intent.intent_id, intent.symbol, intent.trade_id,
                         intent.side, intent.type, intent.quantity, intent.canonical_price,
                         execution_price, fee)

    @staticmethod
    def _apply_fill(position: PaperPosition, fill: PaperFill) -> PaperPosition:
        if position.symbol != fill.symbol:
            raise PositionTransitionError("fill symbol does not match position symbol")
        if fill.type is IntentType.ENTRY:
            if position.status is not PositionStatus.FLAT:
                raise PositionTransitionError("cannot fill entry while a position is open")
            return PaperPosition(
                fill.symbol, PositionStatus.OPEN, fill.trade_id, fill.side, fill.quantity,
                fill.order_id, fill.fill_id, fill.execution_price,
                stable_id("quasartrend.execution.position", {
                    "symbol": fill.symbol, "trade_id": fill.trade_id,
                }),
            )
        if position.status is not PositionStatus.OPEN:
            raise PositionTransitionError("cannot fill exit while flat")
        if (position.trade_id != fill.trade_id or position.side is not fill.side
                or position.quantity != fill.quantity):
            raise PositionTransitionError("exit/stop fill does not match the open position")
        return PaperPosition.flat(fill.symbol)

    def _validate_state(self, state: ExecutionState) -> None:
        if not isinstance(state, ExecutionState) or state.identity != self.identity:
            raise ExecutionIdentityMismatchError("execution state does not belong to this engine")

    def _validate_intent(self, intent: ExecutionIntent) -> None:
        if intent.identity_fingerprint != self.identity.fingerprint or intent.symbol != self.identity.symbol:
            raise ExecutionIdentityMismatchError("intent does not belong to this execution identity")
        if intent.quantity != self.config.quantity:
            raise InvalidIntentError("intent quantity must match paper execution configuration")
        event_type = "trade_opened" if intent.type is IntentType.ENTRY else "trade_closed"
        slot = stable_id("quasartrend.execution.decision-slot", {
            "event_ordinal": intent.event_ordinal, "event_type": event_type,
            "execution_identity": intent.identity_fingerprint,
            "source_processing_key": list(intent.source_processing_key), "symbol": intent.symbol,
            "trade_id": intent.trade_id,
        })
        full = stable_id("quasartrend.execution.intent", {
            "canonical_price": intent.canonical_price, "decision_slot_id": slot,
            "identity_fingerprint": intent.identity_fingerprint, "quantity": intent.quantity,
            "reason": intent.reason, "side": intent.side.value, "symbol": intent.symbol,
            "trade_id": intent.trade_id, "type": intent.type.value,
        })
        if intent.decision_slot_id != slot or intent.intent_id != full:
            raise InvalidIntentError("intent deterministic identity does not match payload")

    @staticmethod
    def _order(state: ExecutionState, order_id: str) -> PaperOrder:
        order = next((item for item in state.orders if item.order_id == order_id), None)
        if order is None:
            raise OrderTransitionError("unknown order ID")
        return order

    @staticmethod
    def _replace_order(state: ExecutionState, replacement: PaperOrder) -> ExecutionState:
        return replace(state, orders=tuple(replacement if order.order_id == replacement.order_id else order for order in state.orders))

    def _validate_trace_event(self, trace: ReplayTrace, event: StrategyEvent, ordinal: int) -> None:
        source = trace.source_bar
        if isinstance(ordinal, bool) or not isinstance(ordinal, int) or ordinal < 0:
            raise InvalidIntentError("event ordinal must be non-negative")
        if ordinal >= len(trace.events) or trace.events[ordinal] != event:
            raise InvalidIntentError("event ordinal must index the exact trace event")
        if source.symbol != self.identity.symbol or trace.post_state.symbol != source.symbol:
            raise InvalidIntentError("trace source/post-state symbol must match execution identity")
        if event.symbol != source.symbol or event.timestamp != source.finalized_at:
            raise InvalidIntentError("strategy event must belong to its finalized source bar")
        if event.type in (EventType.TRADE_OPENED, EventType.TRADE_CLOSED, EventType.STOP_HIT):
            from quasartrend.replay import Timeframe
            if source.timeframe is not Timeframe.MINUTES_15:
                raise InvalidIntentError("executable lifecycle events require a finalized 15m source")
            strategy_bar = trace.strategy_bar
            if (strategy_bar is None or strategy_bar.symbol != source.symbol
                    or strategy_bar.timestamp != source.finalized_at
                    or (strategy_bar.open, strategy_bar.high, strategy_bar.low, strategy_bar.close)
                    != (source.open, source.high, source.low, source.close)):
                raise InvalidIntentError("lifecycle trace strategy bar must match finalized source OHLC")
            if event.trade_id is None or event.side is None:
                raise InvalidIntentError("trade lifecycle event requires trade_id and side")
            if event.price is None or not math.isfinite(event.price) or event.price <= 0.0:
                raise InvalidIntentError("trade lifecycle event requires a finite positive price")
        if event.type is EventType.TRADE_OPENED:
            trade = trace.post_state.trade
            if (trade is None or trade.trade_id != event.trade_id or trade.side is not event.side
                    or trade.entry_price != event.price or trade.entry_timestamp != event.timestamp):
                raise InvalidIntentError("opened event must match trace post-state trade")


# Imported late to keep the public taxonomy grouped in models without a cycle.
from .models import ReconciliationMismatchError
