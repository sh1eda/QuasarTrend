"""Deterministic PAPER-only execution adapter; it has no network boundary."""
from __future__ import annotations

from dataclasses import replace

from .engine import PaperExecutionEngine
from .models import (
    AdapterEvent, AdapterEventType, AdapterFailureError, ConflictingDuplicateIntentError,
    ExecutionIdentity, OrderStatus, OrderTransitionError, PaperAdapterSnapshot,
    PaperAdapterState, PaperDecision, PaperExecutionConfig, PaperOrder, PaperPosition,
    stable_id,
)


class PaperExecutionAdapter:
    """A simulated adapter. ACCEPT_ONLY and DEFER are test/simulation modes."""

    def __init__(self, identity: ExecutionIdentity, config: PaperExecutionConfig | None = None) -> None:
        self.identity = identity
        self.config = config or PaperExecutionConfig(identity.quantity, identity.fee_bps, identity.slippage_bps, identity.decision)
        self._engine = PaperExecutionEngine(identity, self.config)

    def initial_state(self) -> PaperAdapterState:
        return PaperAdapterState(self.identity.fingerprint, position=PaperPosition.flat(self.identity.symbol))

    def register_order(self, state: PaperAdapterState, order: PaperOrder) -> PaperAdapterState:
        existing = next((item for item in state.orders if item.order_id == order.order_id), None)
        if existing is not None:
            if existing != order:
                raise ConflictingDuplicateIntentError("adapter order ID conflicts")
            return state
        return replace(state, orders=state.orders + (order,))

    def events_for_new_order(self, order: PaperOrder) -> tuple[AdapterEvent, ...]:
        if self.config.decision is PaperDecision.DEFER:
            return ()
        accepted = self.accepted_event(order)
        if self.config.decision is PaperDecision.ACCEPT_ONLY:
            return (accepted,)
        if self.config.decision is PaperDecision.REJECT:
            return (self.rejected_event(order),)
        return (accepted, self.filled_event(replace(order, status=OrderStatus.ACCEPTED)))

    def accepted_event(self, order: PaperOrder) -> AdapterEvent:
        return AdapterEvent(stable_id("quasartrend.execution.adapter-event", {"order_id": order.order_id, "type": "accepted"}), order.order_id, AdapterEventType.ACCEPTED)

    def rejected_event(self, order: PaperOrder) -> AdapterEvent:
        return AdapterEvent(stable_id("quasartrend.execution.adapter-event", {"order_id": order.order_id, "type": "rejected"}), order.order_id, AdapterEventType.REJECTED)

    def filled_event(self, order: PaperOrder) -> AdapterEvent:
        fill = self._engine._make_fill(order)
        return AdapterEvent(stable_id("quasartrend.execution.adapter-event", {"order_id": order.order_id, "type": "filled", "fill_id": fill.fill_id}), order.order_id, AdapterEventType.FILLED, fill)

    def apply_event(self, state: PaperAdapterState, event: AdapterEvent) -> PaperAdapterState:
        existing = next((item for item in state.events if item.event_id == event.event_id), None)
        if existing is not None:
            if existing != event:
                raise ConflictingDuplicateIntentError("adapter event ID conflicts")
            return state
        expected_id = stable_id("quasartrend.execution.adapter-event", (
            {"order_id": event.order_id, "type": "filled", "fill_id": event.fill.fill_id}
            if event.type is AdapterEventType.FILLED else {"order_id": event.order_id, "type": event.type.value}
        ))
        if event.event_id != expected_id:
            raise AdapterFailureError("adapter event ID is not deterministic")
        order = next((item for item in state.orders if item.order_id == event.order_id), None)
        if order is None:
            raise OrderTransitionError("adapter event targets unknown order")
        if event.type is AdapterEventType.ACCEPTED:
            if order.status is not OrderStatus.NEW:
                raise OrderTransitionError("only NEW adapter order can be accepted")
            next_order, position, fills = replace(order, status=OrderStatus.ACCEPTED), state.position, state.fills
        elif event.type is AdapterEventType.REJECTED:
            if order.status is not OrderStatus.NEW:
                raise OrderTransitionError("only NEW adapter order can be rejected")
            next_order, position, fills = replace(order, status=OrderStatus.REJECTED), state.position, state.fills
        else:
            if order.status is not OrderStatus.ACCEPTED or event.fill is None:
                raise OrderTransitionError("only ACCEPTED adapter order can be filled")
            expected = self._engine._make_fill(order)
            if event.fill != expected:
                raise AdapterFailureError("adapter fill differs from deterministic paper model")
            next_order, position, fills = replace(order, status=OrderStatus.FILLED), self._engine._apply_fill(state.position, event.fill), state.fills + (event.fill,)
        return replace(state, orders=tuple(next_order if item.order_id == order.order_id else item for item in state.orders), fills=fills, position=position, events=state.events + (event,))

    def snapshot(self, state: PaperAdapterState) -> PaperAdapterSnapshot:
        return PaperAdapterSnapshot(state.identity_fingerprint, state.orders, state.fills, state.position)

    def reconcile(self, state: PaperAdapterState, observed: PaperAdapterSnapshot) -> None:
        if self.snapshot(state) != observed:
            from .models import ReconciliationMismatchError
            raise ReconciliationMismatchError("observed adapter snapshot differs from durable adapter state")
