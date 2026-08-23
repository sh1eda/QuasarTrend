"""Combined replay + paper-execution SQLite checkpoint store for Phase 6."""

from __future__ import annotations

import json
from dataclasses import replace
from pathlib import Path
import sqlite3
from urllib.parse import quote

from quasartrend.persistence import PersistenceIdentity, decode_replay_state, encode_replay_state
from quasartrend.replay import ReplayState, ReplayStepResult
from quasartrend.strategy import Direction

from .engine import PaperExecutionEngine
from .paper import PaperExecutionAdapter
from .models import (
    EXECUTION_SCHEMA_VERSION,
    ExecutionBootstrapRequiredError,
    ExecutionChronologyError,
    ExecutionIdentity,
    ExecutionIdentityMismatchError,
    ExecutionPersistenceError,
    ExecutionState,
    ExecutionTransition,
    IntentType,
    OrderStatus,
    OrderTransitionError,
    PaperAdapterSnapshot,
    PaperAdapterState, AdapterEvent, AdapterEventType,
    ExecutionAdapter,
    PaperExecutionConfig,
    PaperFill,
    PaperOrder,
    PaperPosition,
    PositionStatus,
    ReconciliationMismatchError,
    DivergenceClassification, ExecutionDivergence,
    canonical_json,
    position_from_data,
    position_to_data,
)


_CREATE = """
CREATE TABLE execution_checkpoints (
    symbol TEXT NOT NULL,
    config_fingerprint TEXT NOT NULL,
    execution_fingerprint TEXT NOT NULL,
    schema_version INTEGER NOT NULL,
    payload TEXT NOT NULL,
    PRIMARY KEY(symbol)
)
"""


class ExecutionCheckpoint:
    """Recovered combined state; ``state`` satisfies the runtime store contract."""

    __slots__ = ("identity", "state", "execution_state", "adapter_snapshot", "adapter_state", "divergence")

    def __init__(self, identity: PersistenceIdentity, state: ReplayState,
                 execution_state: ExecutionState, adapter_snapshot: PaperAdapterSnapshot,
                 adapter_state: PaperAdapterState | None = None, divergence: ExecutionDivergence | None = None) -> None:
        self.identity = identity
        self.state = state
        self.execution_state = execution_state
        self.adapter_snapshot = adapter_snapshot
        self.adapter_state = adapter_state
        self.divergence = divergence


class SQLiteExecutionStore:
    """Separate Phase 6 schema, atomically advancing replay and paper ledger."""

    def __init__(
        self, path: str | Path, identity: ExecutionIdentity | PersistenceIdentity,
        config: PaperExecutionConfig | None = None, adapter: ExecutionAdapter | None = None,
    ) -> None:
        self.path = Path(path)
        self.execution_identity = (
            ExecutionIdentity.from_persistence(identity, config)
            if isinstance(identity, PersistenceIdentity) else identity
        )
        if not isinstance(self.execution_identity, ExecutionIdentity):
            raise TypeError("identity must be ExecutionIdentity or PersistenceIdentity")
        self.config = config or PaperExecutionConfig(
            quantity=self.execution_identity.quantity, fee_bps=self.execution_identity.fee_bps,
            slippage_bps=self.execution_identity.slippage_bps, decision=self.execution_identity.decision,
        )
        self.engine = PaperExecutionEngine(self.execution_identity, self.config)
        self.adapter: ExecutionAdapter = adapter or PaperExecutionAdapter(self.execution_identity, self.config)
        if not isinstance(self.adapter, ExecutionAdapter) or getattr(self.adapter, "identity", None) != self.execution_identity:
            raise ExecutionIdentityMismatchError("execution adapter must match execution identity")

    def load_checkpoint(self, identity: PersistenceIdentity) -> ExecutionCheckpoint | None:
        self._validate_runtime_identity(identity)
        if not self.path.exists():
            return None
        connection = self._open_existing()
        try:
            if not self._validate_existing(connection):
                return None
            row = connection.execute(
                "SELECT config_fingerprint, execution_fingerprint, schema_version, payload "
                "FROM execution_checkpoints WHERE symbol=?", (identity.symbol,)
            ).fetchone()
            if row is None:
                return None
            return self._decode_checkpoint(identity, row)
        except ExecutionPersistenceError:
            raise
        except sqlite3.Error as exc:
            raise ExecutionPersistenceError("unable to read execution checkpoint database") from exc
        finally:
            connection.close()

    def save_transition(
        self, identity: PersistenceIdentity, prior_state: ReplayState, stepped: ReplayStepResult,
    ) -> ExecutionCheckpoint:
        self._validate_runtime_identity(identity)
        self._validate_transition(identity, prior_state, stepped)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        connection: sqlite3.Connection | None = None
        try:
            connection = sqlite3.connect(self.path, isolation_level=None)
            connection.execute("PRAGMA synchronous=FULL")
            connection.execute("BEGIN IMMEDIATE")
            self._initialize_or_validate(connection)
            row = connection.execute(
                "SELECT config_fingerprint, execution_fingerprint, schema_version, payload "
                "FROM execution_checkpoints WHERE symbol=?", (identity.symbol,)
            ).fetchone()
            existing = None if row is None else self._decode_checkpoint(identity, row)
            if existing is None:
                if not self._is_initial_replay_state(prior_state):
                    raise ExecutionBootstrapRequiredError(
                        "noninitial replay state requires a combined execution ledger checkpoint"
                    )
                execution_state = self.engine.initial_state()
                adapter_state = self.adapter.initial_state()
            elif existing.state == stepped.state:
                # At-least-once replay of an already durable source transition:
                # validate it, then return the exact durable object unchanged.
                self._verify_duplicate_transition(existing, stepped)
                connection.execute("COMMIT")
                return existing
            else:
                if existing.state != prior_state:
                    raise ExecutionChronologyError("transition prior replay state is not the durable state")
                execution_state = existing.execution_state
                adapter_state = existing.adapter_state
            transition, adapter_state = self._transition(execution_state, adapter_state, stepped.trace)
            payload = self._encode_checkpoint(identity, stepped.state, transition, adapter_state)
            connection.execute(
                """INSERT INTO execution_checkpoints (
                       symbol, config_fingerprint, execution_fingerprint, schema_version, payload
                   ) VALUES (?, ?, ?, ?, ?)
                   ON CONFLICT(symbol) DO UPDATE SET
                       config_fingerprint=excluded.config_fingerprint,
                       execution_fingerprint=excluded.execution_fingerprint,
                       schema_version=excluded.schema_version,
                       payload=excluded.payload""",
                (identity.symbol, identity.config_fingerprint, self.execution_identity.fingerprint,
                 EXECUTION_SCHEMA_VERSION, payload),
            )
            connection.execute("COMMIT")
            return ExecutionCheckpoint(identity, stepped.state, transition.state, self.adapter.snapshot(adapter_state), adapter_state, self._classify(stepped.state, transition.state))
        except (ExecutionPersistenceError, ExecutionBootstrapRequiredError, ExecutionChronologyError,
                ExecutionIdentityMismatchError, ReconciliationMismatchError):
            if connection is not None:
                self._rollback(connection)
            raise
        except sqlite3.Error as exc:
            if connection is not None:
                self._rollback(connection)
            raise ExecutionPersistenceError("SQLite execution transition save failed") from exc
        except Exception:
            if connection is not None:
                self._rollback(connection)
            raise
        finally:
            if connection is not None:
                connection.close()

    def execution_state(self, identity: PersistenceIdentity) -> ExecutionState | None:
        checkpoint = self.load_checkpoint(identity)
        return None if checkpoint is None else checkpoint.execution_state

    def adapter_snapshot(self, identity: PersistenceIdentity) -> PaperAdapterSnapshot | None:
        checkpoint = self.load_checkpoint(identity)
        return None if checkpoint is None else checkpoint.adapter_snapshot

    def apply_adapter_event(self, identity: PersistenceIdentity, event: AdapterEvent) -> ExecutionCheckpoint:
        """Atomically persist a later deterministic adapter acknowledgement/fill."""
        self._validate_runtime_identity(identity)
        connection: sqlite3.Connection | None = None
        try:
            connection = sqlite3.connect(self.path, isolation_level=None)
            connection.execute("PRAGMA synchronous=FULL")
            connection.execute("BEGIN IMMEDIATE")
            self._initialize_or_validate(connection)
            row = connection.execute("SELECT config_fingerprint, execution_fingerprint, schema_version, payload FROM execution_checkpoints WHERE symbol=?", (identity.symbol,)).fetchone()
            if row is None:
                raise ExecutionBootstrapRequiredError("adapter event requires a durable combined checkpoint")
            checkpoint = self._decode_checkpoint(identity, row)
            adapter_state = self.adapter.apply_event(checkpoint.adapter_state, event)
            execution_state = self.engine.apply_adapter_event(checkpoint.execution_state, event)
            self._reconcile_pair(execution_state, adapter_state)
            payload = self._encode_checkpoint(identity, checkpoint.state, ExecutionTransition(execution_state, self.adapter.snapshot(adapter_state)), adapter_state)
            changed = connection.execute("UPDATE execution_checkpoints SET payload=? WHERE symbol=? AND payload=?", (payload, identity.symbol, row[3])).rowcount
            if changed != 1:
                raise ExecutionPersistenceError("concurrent adapter transition did not update exactly one checkpoint")
            connection.execute("COMMIT")
            return ExecutionCheckpoint(identity, checkpoint.state, execution_state, self.adapter.snapshot(adapter_state), adapter_state, self._classify(checkpoint.state, execution_state))
        except sqlite3.Error as exc:
            if connection is not None:
                self._rollback(connection)
            raise ExecutionPersistenceError("SQLite adapter-event save failed") from exc
        except Exception:
            if connection is not None:
                self._rollback(connection)
            raise
        finally:
            if connection is not None:
                connection.close()

    def reconcile(self, identity: PersistenceIdentity, observed: PaperAdapterSnapshot | None = None) -> None:
        checkpoint = self.load_checkpoint(identity)
        if checkpoint is None:
            if observed is not None:
                raise ReconciliationMismatchError("observed adapter state exists without a durable execution checkpoint")
            return
        self._reconcile_pair(checkpoint.execution_state, checkpoint.adapter_state)
        self.adapter.reconcile(checkpoint.adapter_state, checkpoint.adapter_snapshot)
        if observed is not None:
            self.adapter.reconcile(checkpoint.adapter_state, observed)
            self.engine.reconcile(checkpoint.execution_state, observed)

    def _validate_runtime_identity(self, identity: PersistenceIdentity) -> None:
        if not isinstance(identity, PersistenceIdentity):
            raise TypeError("runtime identity must be PersistenceIdentity")
        if (identity.symbol != self.execution_identity.symbol
                or identity.config_fingerprint != self.execution_identity.config_fingerprint):
            raise ExecutionIdentityMismatchError("runtime identity does not match execution store identity")

    def _transition(self, execution: ExecutionState, adapter: PaperAdapterState, trace) -> tuple[ExecutionTransition, PaperAdapterState]:  # type: ignore[no-untyped-def]
        key = trace.source_bar.processing_key
        unresolved = [order for order in execution.orders if order.status in (OrderStatus.NEW, OrderStatus.ACCEPTED)]
        if unresolved:
            raise OrderTransitionError("unresolved paper order blocks later replay transition")
        if any(order.status is OrderStatus.REJECTED for order in execution.orders):
            from .models import OrderRejectedError
            raise OrderRejectedError("durable rejected paper order blocks later replay transition")
        intents = []
        for ordinal, event in enumerate(trace.events):
            intent = self.engine.intent_from_event(trace, event, ordinal)
            if intent is not None:
                intents.append(intent)
                execution = self.engine.register_intent(execution, intent)
                order = next(item for item in execution.orders if item.intent.intent_id == intent.intent_id)
                adapter = self.adapter.register_order(adapter, order)
                for adapter_event in self.adapter.events_for_new_order(order):
                    adapter = self.adapter.apply_event(adapter, adapter_event)
                    execution = self.engine.apply_adapter_event(execution, adapter_event)
        execution = replace(execution, last_source_processing_key=key)
        adapter = replace(adapter, replay_cursor=key)
        transition = ExecutionTransition(execution, self.adapter.snapshot(adapter), tuple(intents))
        self._reconcile_pair(execution, adapter)
        return transition, adapter

    def _reconcile_pair(self, execution: ExecutionState, adapter: PaperAdapterState) -> None:
        if (execution.identity.fingerprint != adapter.identity_fingerprint or execution.orders != adapter.orders
                or execution.fills != adapter.fills or execution.position != adapter.position):
            raise ReconciliationMismatchError("execution ledger and independently durable adapter state differ")

    @staticmethod
    def _classify(replay: ReplayState, execution: ExecutionState) -> ExecutionDivergence:
        strategy_trade = replay.strategy_state.trade
        position = execution.position
        if strategy_trade is None and position.status is PositionStatus.FLAT:
            return ExecutionDivergence(DivergenceClassification.HEALTHY_FLAT)
        if (strategy_trade is not None and position.status is PositionStatus.OPEN
                and position.trade_id == strategy_trade.trade_id and position.side is strategy_trade.side):
            return ExecutionDivergence(DivergenceClassification.HEALTHY_OPEN, strategy_trade.trade_id)
        relevant = [order for order in execution.orders if order.intent.trade_id == (strategy_trade.trade_id if strategy_trade else position.trade_id)]
        latest = relevant[-1] if relevant else None
        if strategy_trade is not None and position.status is PositionStatus.FLAT and latest is not None and latest.intent.type is IntentType.ENTRY:
            if latest.status in (OrderStatus.NEW, OrderStatus.ACCEPTED):
                return ExecutionDivergence(DivergenceClassification.PENDING_ENTRY, strategy_trade.trade_id)
            if latest.status is OrderStatus.REJECTED:
                return ExecutionDivergence(DivergenceClassification.REJECTED_ENTRY, strategy_trade.trade_id)
        if strategy_trade is None and position.status is PositionStatus.OPEN and latest is not None and latest.intent.type is not IntentType.ENTRY:
            if latest.status in (OrderStatus.NEW, OrderStatus.ACCEPTED):
                return ExecutionDivergence(DivergenceClassification.PENDING_EXIT, position.trade_id)
            if latest.status is OrderStatus.REJECTED:
                return ExecutionDivergence(DivergenceClassification.REJECTED_EXIT, position.trade_id)
        raise ReconciliationMismatchError("strategy and execution states have an unexplained divergence")

    @staticmethod
    def _validate_transition(identity: PersistenceIdentity, prior: ReplayState, stepped: ReplayStepResult) -> None:
        if prior.symbol != identity.symbol or stepped.state.symbol != identity.symbol:
            raise ExecutionIdentityMismatchError("replay transition symbol mismatches store identity")
        if stepped.trace.source_bar.symbol != identity.symbol or stepped.trace.post_state != stepped.state.strategy_state:
            raise ExecutionChronologyError("replay step does not match its source trace")
        if stepped.state.chronology_cursor != stepped.trace.source_bar.processing_key:
            raise ExecutionChronologyError("replay step cursor must equal source processing key")
        if prior.chronology_cursor is not None and stepped.trace.source_bar.processing_key <= prior.chronology_cursor:
            raise ExecutionChronologyError("replay transition is not strictly chronological")
        from quasartrend.strategy import EventType
        for event in stepped.trace.events:
            if event.type is EventType.TRADE_CLOSED:
                trade = prior.strategy_state.trade
                if (trade is None or stepped.trace.post_state.trade is not None or event.trade_id != trade.trade_id
                        or event.side is not trade.side or event.price is None):
                    raise ExecutionChronologyError("closed event must match prior strategy trade and close post-state")

    @staticmethod
    def _is_initial_replay_state(state: ReplayState) -> bool:
        from quasartrend.strategy import StrategyState
        return state.chronology_cursor is None and state.strategy_state == StrategyState.initial(state.symbol)

    def _verify_duplicate_transition(self, checkpoint: ExecutionCheckpoint, stepped: ReplayStepResult) -> None:
        if checkpoint.state.chronology_cursor != stepped.trace.source_bar.processing_key:
            raise ExecutionChronologyError("duplicate transition source does not match durable cursor")
        # Re-run only against the durable execution state to verify deterministic
        # intent mapping; duplicate intents are strict no-ops.
        verified = self.engine.process_trace(checkpoint.execution_state, stepped.trace)
        if verified.state != checkpoint.execution_state or verified.adapter_snapshot != checkpoint.adapter_snapshot:
            raise ExecutionChronologyError("same-source transition conflicts with durable execution ledger")

    def _encode_checkpoint(self, identity: PersistenceIdentity, state: ReplayState,
                           transition: ExecutionTransition, adapter_state: PaperAdapterState) -> str:
        payload = {
            "adapter_state": self._adapter_state_data(adapter_state),
            "config_fingerprint": identity.config_fingerprint,
            "execution_identity": self.execution_identity.payload | {"fingerprint": self.execution_identity.fingerprint},
            "execution_state": self._state_data(transition.state),
            "replay_state": json.loads(encode_replay_state(state, expected_config=identity.replay_config))["state"],
            "schema_version": EXECUTION_SCHEMA_VERSION,
            "symbol": identity.symbol,
        }
        return canonical_json(payload)

    def _decode_checkpoint(self, identity: PersistenceIdentity, row: tuple[object, ...]) -> ExecutionCheckpoint:
        fingerprint, execution_fingerprint, schema_version, payload = row
        if (fingerprint != identity.config_fingerprint or execution_fingerprint != self.execution_identity.fingerprint
                or schema_version != EXECUTION_SCHEMA_VERSION or not isinstance(payload, str)):
            raise ExecutionIdentityMismatchError("execution checkpoint identity/version mismatch")
        try:
            data = json.loads(payload, object_pairs_hook=self._no_duplicates, parse_constant=self._bad_constant)
        except (TypeError, ValueError, json.JSONDecodeError) as exc:
            raise ExecutionPersistenceError("malformed execution checkpoint JSON") from exc
        if canonical_json(data) != payload or not isinstance(data, dict):
            raise ExecutionPersistenceError("execution checkpoint must use canonical JSON")
        expected = {"adapter_state", "config_fingerprint", "execution_identity", "execution_state", "replay_state", "schema_version", "symbol"}
        if set(data) != expected or data["symbol"] != identity.symbol or data["config_fingerprint"] != identity.config_fingerprint:
            raise ExecutionPersistenceError("execution checkpoint envelope is invalid")
        expected_identity = self.execution_identity.payload | {"fingerprint": self.execution_identity.fingerprint}
        if data["execution_identity"] != expected_identity or data["schema_version"] != EXECUTION_SCHEMA_VERSION:
            raise ExecutionIdentityMismatchError("execution checkpoint execution identity mismatches")
        replay_payload = canonical_json({"checkpoint_version": 1, "state": data["replay_state"]})
        try:
            replay_state = decode_replay_state(replay_payload, expected_config=identity.replay_config)
        except Exception as exc:
            raise ExecutionPersistenceError("execution checkpoint replay state is invalid") from exc
        execution_state = self._state_from_data(data["execution_state"])
        adapter_state = self._adapter_state_from_data(data["adapter_state"])
        snapshot = self.adapter.snapshot(adapter_state)
        self._reconcile_pair(execution_state, adapter_state)
        if replay_state.symbol != execution_state.identity.symbol:
            raise ExecutionIdentityMismatchError("combined replay/execution symbols mismatch")
        if execution_state.last_source_processing_key != replay_state.chronology_cursor or adapter_state.replay_cursor != replay_state.chronology_cursor:
            raise ExecutionChronologyError("combined execution/adapter/replay cursors differ")
        return ExecutionCheckpoint(identity, replay_state, execution_state, snapshot, adapter_state, self._classify(replay_state, execution_state))

    def _state_data(self, state: ExecutionState) -> dict[str, object]:
        return {
            "fills": [self._fill_data(fill) for fill in state.fills],
            "intents": [self._intent_data(intent) for intent in state.intents],
            "last_source_processing_key": None if state.last_source_processing_key is None else list(state.last_source_processing_key),
            "orders": [{"intent_id": order.intent.intent_id, "order_id": order.order_id, "status": order.status.value} for order in state.orders],
            "position": position_to_data(state.position),
        }

    def _state_from_data(self, data: object) -> ExecutionState:
        if not isinstance(data, dict) or set(data) != {"fills", "intents", "last_source_processing_key", "orders", "position"}:
            raise ExecutionPersistenceError("execution state payload fields are invalid")
        if not all(isinstance(data[name], list) for name in ("fills", "intents", "orders")):
            raise ExecutionPersistenceError("execution ledger collections are invalid")
        intents = tuple(self._intent_from_data(item) for item in data["intents"])
        by_id = {item.intent_id: item for item in intents}
        orders: list[PaperOrder] = []
        for item in data["orders"]:
            if not isinstance(item, dict) or set(item) != {"intent_id", "order_id", "status"} or item["intent_id"] not in by_id:
                raise ExecutionPersistenceError("order payload is invalid")
            try:
                orders.append(PaperOrder(item["order_id"], by_id[item["intent_id"]], OrderStatus(item["status"])))
            except (TypeError, ValueError) as exc:
                raise ExecutionPersistenceError("order payload is invalid") from exc
        cursor = data["last_source_processing_key"]
        if cursor is not None:
            if not isinstance(cursor, list) or len(cursor) != 2:
                raise ExecutionPersistenceError("execution chronology cursor is invalid")
            cursor = tuple(cursor)
        return ExecutionState(self.execution_identity, intents, tuple(orders),
                              self._validated_fills(tuple(orders), tuple(self._fill_from_data(item) for item in data["fills"])),
                              position_from_data(data["position"]), cursor)

    def _validated_fills(self, orders: tuple[PaperOrder, ...], fills: tuple[PaperFill, ...]) -> tuple[PaperFill, ...]:
        by_id = {order.order_id: order for order in orders}
        for fill in fills:
            order = by_id.get(fill.order_id)
            if order is None or self.engine._make_fill(order) != fill:
                raise ExecutionPersistenceError("persisted fill differs from canonical paper execution")
        return fills

    @staticmethod
    def _intent_data(intent) -> dict[str, object]:  # type: ignore[no-untyped-def]
        return intent.payload | {"intent_id": intent.intent_id}

    def _intent_from_data(self, data: object):  # type: ignore[no-untyped-def]
        if not isinstance(data, dict) or set(data) != {"canonical_price", "decision_slot_id", "event_ordinal", "identity_fingerprint", "intent_id", "quantity", "side", "source_processing_key", "symbol", "trade_id", "type", "reason"}:
            raise ExecutionPersistenceError("intent payload is invalid")
        key = data["source_processing_key"]
        if not isinstance(key, list) or len(key) != 2:
            raise ExecutionPersistenceError("intent source key is invalid")
        try:
            from .models import ExecutionIntent, stable_id
            intent = ExecutionIntent(data["intent_id"], data["decision_slot_id"], data["identity_fingerprint"], data["symbol"], tuple(key), data["event_ordinal"], IntentType(data["type"]), data["reason"], data["trade_id"], Direction(data["side"]), data["canonical_price"], data["quantity"])
            event_type = "trade_opened" if intent.type is IntentType.ENTRY else "trade_closed"
            slot = stable_id("quasartrend.execution.decision-slot", {"event_ordinal": intent.event_ordinal, "event_type": event_type, "execution_identity": intent.identity_fingerprint, "source_processing_key": list(intent.source_processing_key), "symbol": intent.symbol, "trade_id": intent.trade_id})
            expected = stable_id("quasartrend.execution.intent", {"canonical_price": intent.canonical_price, "decision_slot_id": slot, "identity_fingerprint": intent.identity_fingerprint, "quantity": intent.quantity, "reason": intent.reason, "side": intent.side.value, "symbol": intent.symbol, "trade_id": intent.trade_id, "type": intent.type.value})
            if intent.decision_slot_id != slot or intent.intent_id != expected or intent.identity_fingerprint != self.execution_identity.fingerprint or intent.symbol != self.execution_identity.symbol or intent.quantity != self.config.quantity:
                raise ExecutionPersistenceError("persisted intent identity or configuration is invalid")
            return intent
        except (TypeError, ValueError) as exc:
            raise ExecutionPersistenceError("intent payload is invalid") from exc

    @staticmethod
    def _fill_data(fill: PaperFill) -> dict[str, object]:
        return {"canonical_price": fill.canonical_price, "execution_price": fill.execution_price, "fee": fill.fee,
                "fill_id": fill.fill_id, "intent_id": fill.intent_id, "order_id": fill.order_id,
                "quantity": fill.quantity, "side": fill.side.value, "symbol": fill.symbol,
                "trade_id": fill.trade_id, "type": fill.type.value}

    @staticmethod
    def _fill_from_data(data: object) -> PaperFill:
        fields = {"canonical_price", "execution_price", "fee", "fill_id", "intent_id", "order_id", "quantity", "side", "symbol", "trade_id", "type"}
        if not isinstance(data, dict) or set(data) != fields:
            raise ExecutionPersistenceError("fill payload is invalid")
        try:
            return PaperFill(data["fill_id"], data["order_id"], data["intent_id"], data["symbol"], data["trade_id"], Direction(data["side"]), IntentType(data["type"]), data["quantity"], data["canonical_price"], data["execution_price"], data["fee"])
        except (TypeError, ValueError) as exc:
            raise ExecutionPersistenceError("fill payload is invalid") from exc

    @staticmethod
    def _snapshot_data(snapshot: PaperAdapterSnapshot) -> dict[str, object]:
        return {"fills": [SQLiteExecutionStore._fill_data(fill) for fill in snapshot.fills], "identity_fingerprint": snapshot.identity_fingerprint,
                "orders": [{"intent": SQLiteExecutionStore._intent_data(order.intent), "order_id": order.order_id, "status": order.status.value} for order in snapshot.orders], "position": position_to_data(snapshot.position)}

    def _adapter_state_data(self, state: PaperAdapterState) -> dict[str, object]:
        return self._snapshot_data(self.adapter.snapshot(state)) | {
            "events": [
                {"event_id": event.event_id, "order_id": event.order_id, "type": event.type.value,
                 "fill": None if event.fill is None else self._fill_data(event.fill)}
                for event in state.events
            ],
            "replay_cursor": None if state.replay_cursor is None else list(state.replay_cursor),
        }

    def _adapter_state_from_data(self, data: object) -> PaperAdapterState:
        if not isinstance(data, dict) or set(data) != {"identity_fingerprint", "orders", "fills", "position", "events", "replay_cursor"}:
            raise ExecutionPersistenceError("adapter state payload is invalid")
        # Reuse the execution order/fill decoder, then replay events through the
        # adapter so historical adapter transitions are independently checked.
        if not isinstance(data["orders"], list) or not isinstance(data["fills"], list) or not isinstance(data["events"], list):
            raise ExecutionPersistenceError("adapter state collections are invalid")
        # Adapter orders need their full intent payload, so store it in each row
        # after accepting old rows only as corruption (there is no migration).
        try:
            orders: list[PaperOrder] = []
            for row in data["orders"]:
                if not isinstance(row, dict) or set(row) != {"order_id", "status", "intent"}:
                    raise ExecutionPersistenceError("adapter order payload lacks full intent")
                orders.append(PaperOrder(row["order_id"], self._intent_from_data(row["intent"]), OrderStatus(row["status"])))
            state = PaperAdapterState(
                data["identity_fingerprint"], tuple(replace(order, status=OrderStatus.NEW) for order in orders),
                (), PaperPosition.flat(self.execution_identity.symbol),
            )
            for event_row in data["events"]:
                if not isinstance(event_row, dict) or set(event_row) != {"event_id", "order_id", "type", "fill"}:
                    raise ExecutionPersistenceError("adapter event payload is invalid")
                fill = None if event_row["fill"] is None else self._fill_from_data(event_row["fill"])
                event = AdapterEvent(event_row["event_id"], event_row["order_id"], AdapterEventType(event_row["type"]), fill)
                state = self.adapter.apply_event(state, event)
        except ExecutionPersistenceError:
            raise
        except (TypeError, ValueError) as exc:
            raise ExecutionPersistenceError("adapter enum/state payload is invalid") from exc
        cursor = data["replay_cursor"]
        if cursor is not None and (not isinstance(cursor, list) or len(cursor) != 2):
            raise ExecutionPersistenceError("adapter replay cursor is invalid")
        state = replace(state, replay_cursor=None if cursor is None else tuple(cursor))
        if self.adapter.snapshot(state) != self._snapshot_from_data({k: data[k] for k in ("identity_fingerprint", "orders", "fills", "position")}):
            raise ReconciliationMismatchError("adapter event replay differs from persisted adapter state")
        return state

    def _snapshot_from_data(self, data: object) -> PaperAdapterSnapshot:
        fields = {"fills", "identity_fingerprint", "orders", "position"}
        if not isinstance(data, dict) or set(data) != fields or not isinstance(data["fills"], list) or not isinstance(data["orders"], list):
            raise ExecutionPersistenceError("adapter snapshot payload is invalid")
        try:
            # This method is used only to compare persisted full adapter state.
            orders = tuple(PaperOrder(item["order_id"], self._intent_from_data(item["intent"]), OrderStatus(item["status"])) for item in data["orders"])
            fills = tuple(self._fill_from_data(item) for item in data["fills"])
            return PaperAdapterSnapshot(data["identity_fingerprint"], orders, fills, position_from_data(data["position"]))
        except (TypeError, ValueError) as exc:
            raise ExecutionPersistenceError("adapter snapshot payload is invalid") from exc

    def _open_existing(self) -> sqlite3.Connection:
        try:
            return sqlite3.connect(f"file:{quote(str(self.path.resolve()))}?mode=rw", uri=True, isolation_level=None)
        except sqlite3.Error as exc:
            raise ExecutionPersistenceError("unable to open execution checkpoint database") from exc

    @staticmethod
    def _rollback(connection: sqlite3.Connection) -> None:
        try:
            connection.execute("ROLLBACK")
        except sqlite3.Error:
            pass

    @staticmethod
    def _no_duplicates(pairs):  # type: ignore[no-untyped-def]
        result = {}
        for key, value in pairs:
            if key in result:
                raise ValueError("duplicate JSON key")
            result[key] = value
        return result

    @staticmethod
    def _bad_constant(value: str) -> object:
        raise ValueError(f"non-finite JSON constant: {value}")

    def _initialize_or_validate(self, connection: sqlite3.Connection) -> None:
        version = connection.execute("PRAGMA user_version").fetchone()[0]
        tables = connection.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()
        if version == 0 and not tables:
            connection.execute(_CREATE)
            connection.execute(f"PRAGMA user_version={EXECUTION_SCHEMA_VERSION}")
            return
        if version != EXECUTION_SCHEMA_VERSION:
            raise ExecutionPersistenceError("unsupported execution database schema version")
        self._assert_schema(connection)

    def _validate_existing(self, connection: sqlite3.Connection) -> bool:
        version = connection.execute("PRAGMA user_version").fetchone()[0]
        tables = connection.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()
        if version == 0 and not tables:
            return False
        if version != EXECUTION_SCHEMA_VERSION:
            raise ExecutionPersistenceError("unsupported execution database schema version")
        self._assert_schema(connection)
        return True

    @staticmethod
    def _assert_schema(connection: sqlite3.Connection) -> None:
        expected = (("symbol", "TEXT", 1, 1), ("config_fingerprint", "TEXT", 1, 0),
                    ("execution_fingerprint", "TEXT", 1, 0), ("schema_version", "INTEGER", 1, 0),
                    ("payload", "TEXT", 1, 0))
        actual = tuple((row[1], row[2].upper(), row[3], row[5]) for row in connection.execute("PRAGMA table_info(execution_checkpoints)").fetchall())
        if actual != expected:
            raise ExecutionPersistenceError("execution checkpoint table schema is incompatible")
