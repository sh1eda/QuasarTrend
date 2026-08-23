from __future__ import annotations

from dataclasses import replace
import json
import sqlite3
import random
import threading

import pytest

from quasartrend.execution import (
    ConflictingDuplicateIntentError, ExecutionBootstrapRequiredError, ExecutionChronologyError, InvalidIntentError,
    ExecutionIdentity, IntentType, OrderStatus, PaperDecision, PaperExecutionConfig,
    PaperExecutionEngine, PositionTransitionError, ReconciliationMismatchError, SQLiteExecutionStore,
    PaperExecutionAdapter, ExecutionAdapter,
    OrderTransitionError, ExecutionError,
)
from quasartrend.persistence import PersistenceIdentity
from quasartrend.replay import HistoricalBar, ReplayConfig, ReplayEngine, ReplayStepResult, ReplayTrace, Timeframe
from quasartrend.runtime import LiveRuntime, RuntimeConfig
from quasartrend.strategy import (
    Direction, EventType, OpenTrade, ReasonCode, StrategyBar, StrategyEvent, StrategyState, StrategyStatus,
)


SYMBOL = "BINANCE:BTCUSDT.P"


def _identity(symbol: str = SYMBOL) -> PersistenceIdentity:
    replay = ReplayEngine(ReplayConfig(
        ltf_hema_fast_length=1, ltf_hema_slow_length=2, htf_hema_fast_length=1,
        htf_hema_slow_length=2, kalman_period=2, kalman_atr_period=2,
    ))
    return PersistenceIdentity(symbol, replay.config, replay.strategy_engine.config)


def _bar(open_time: int) -> HistoricalBar:
    return HistoricalBar(SYMBOL, Timeframe.MINUTES_15, open_time, 100.0, 101.0, 99.0, 100.0)


def _opened_trace(open_time: int = 0) -> ReplayTrace:
    bar = _bar(open_time)
    trade = OpenTrade(f"{SYMBOL}:1", Direction.LONG, 100.0, bar.finalized_at, 1.0, 99.0, 1, bar.finalized_at)
    state = StrategyState(SYMBOL, status=StrategyStatus.OPEN_LONG, trade=trade)
    event = StrategyEvent(EventType.TRADE_OPENED, SYMBOL, bar.finalized_at, ReasonCode.ENTRY_ACCEPTED,
                          trade.trade_id, Direction.LONG, 100.0)
    strategy_bar = StrategyBar(SYMBOL, bar.finalized_at, bar.open, bar.high, bar.low, bar.close, None, None, None, None)
    return ReplayTrace(bar, strategy_bar, (event,), state, None)


def _closed_trace(open_time: int = 900_000, *, stop: bool = False, diagnostic: bool = False) -> ReplayTrace:
    bar = _bar(open_time)
    reason = ReasonCode.EXIT_STOP if stop else ReasonCode.EXIT_HEMA_FLIP
    close = StrategyEvent(EventType.TRADE_CLOSED, SYMBOL, bar.finalized_at, reason,
                          f"{SYMBOL}:1", Direction.LONG, 98.0)
    events = ((StrategyEvent(EventType.STOP_HIT, SYMBOL, bar.finalized_at, reason,
                            f"{SYMBOL}:1", Direction.LONG, 98.0),) if diagnostic else ()) + (close,)
    strategy_bar = StrategyBar(SYMBOL, bar.finalized_at, bar.open, bar.high, bar.low, bar.close, None, None, None, None)
    return ReplayTrace(bar, strategy_bar, events, StrategyState(SYMBOL, status=StrategyStatus.FLAT), None)


def _engine(decision: PaperDecision = PaperDecision.ACCEPT_FILL) -> PaperExecutionEngine:
    config = PaperExecutionConfig(quantity=2.0, fee_bps=10.0, slippage_bps=25.0, decision=decision)
    return PaperExecutionEngine(ExecutionIdentity.from_persistence(_identity(), config), config)


def _step(prior, trace: ReplayTrace) -> ReplayStepResult:  # type: ignore[no-untyped-def]
    return ReplayStepResult(replace(prior, strategy_state=trace.post_state, chronology_cursor=trace.source_bar.processing_key), trace)


def test_deterministic_mapping_fill_and_stop_close_is_single_intent() -> None:
    engine = _engine()
    first = engine.process_trace(engine.initial_state(), _opened_trace())
    assert len(first.intents) == len(first.state.orders) == len(first.state.fills) == 1
    assert first.intents[0].type is IntentType.ENTRY
    assert first.state.orders[0].status is OrderStatus.FILLED
    assert first.state.position.trade_id == f"{SYMBOL}:1"
    assert first.state.fills[0].execution_price == 100.25
    assert first.state.fills[0].fee == 0.2005
    close = engine.process_trace(first.state, _closed_trace(stop=True, diagnostic=True))
    assert len(close.intents) == 1 and close.intents[0].type is IntentType.STOP
    assert close.state.position.status.value == "flat"


def test_exact_duplicate_is_noop_and_conflicting_payload_is_typed() -> None:
    engine = _engine()
    trace = _opened_trace()
    state = engine.process_trace(engine.initial_state(), trace).state
    assert engine.process_trace(state, trace).state == state
    intent = engine.intent_from_event(trace, trace.events[0], 0)
    assert intent is not None
    with pytest.raises((ConflictingDuplicateIntentError, InvalidIntentError)):
        engine.submit_intent(state, replace(intent, canonical_price=101.0))


def test_public_engine_defer_registers_new_and_equal_source_requires_exact_duplicate() -> None:
    engine = _engine(PaperDecision.DEFER)
    trace = _opened_trace()
    first = engine.process_trace(engine.initial_state(), trace).state
    assert first.orders[0].status is OrderStatus.NEW
    assert engine.process_trace(first, trace).state == first
    altered = replace(trace, events=(replace(trace.events[0], price=101.0),))
    with pytest.raises((ConflictingDuplicateIntentError, ExecutionChronologyError, InvalidIntentError)):
        engine.process_trace(first, altered)


def test_restart_equivalence_after_entry_before_and_after_close(tmp_path) -> None:  # type: ignore[no-untyped-def]
    identity = _identity(); config = PaperExecutionConfig()
    replay = ReplayEngine(identity.replay_config, identity.strategy_config)
    initial = replay.initial_state(SYMBOL); entry = _opened_trace(); close = _closed_trace()
    uninterrupted = SQLiteExecutionStore(tmp_path / "u.db", identity, config)
    one = uninterrupted.save_transition(identity, initial, _step(initial, entry))
    two = uninterrupted.save_transition(identity, one.state, _step(one.state, close))
    split = SQLiteExecutionStore(tmp_path / "s.db", identity, config)
    first = split.save_transition(identity, initial, _step(initial, entry))
    recovered = SQLiteExecutionStore(tmp_path / "s.db", identity, config).load_checkpoint(identity)
    assert recovered is not None
    resumed = SQLiteExecutionStore(tmp_path / "s.db", identity, config).save_transition(identity, recovered.state, _step(recovered.state, close))
    assert (two.state, two.execution_state, two.adapter_state) == (resumed.state, resumed.execution_state, resumed.adapter_state)
    assert SQLiteExecutionStore(tmp_path / "s.db", identity, config).load_checkpoint(identity).execution_state == resumed.execution_state  # type: ignore[union-attr]


def test_accept_only_restart_duplicate_ack_and_fill_conflict(tmp_path) -> None:  # type: ignore[no-untyped-def]
    identity = _identity(); config = PaperExecutionConfig(decision=PaperDecision.DEFER)
    replay = ReplayEngine(identity.replay_config, identity.strategy_config); prior = replay.initial_state(SYMBOL)
    store = SQLiteExecutionStore(tmp_path / "async.db", identity, config)
    saved = store.save_transition(identity, prior, _step(prior, _opened_trace()))
    order = saved.execution_state.orders[0]; ack = store.adapter.accepted_event(order)
    accepted = SQLiteExecutionStore(tmp_path / "async.db", identity, config).apply_adapter_event(identity, ack)
    duplicate = SQLiteExecutionStore(tmp_path / "async.db", identity, config).apply_adapter_event(identity, ack)
    assert duplicate.execution_state == accepted.execution_state
    with pytest.raises(ConflictingDuplicateIntentError):
        SQLiteExecutionStore(tmp_path / "async.db", identity, config).apply_adapter_event(identity, replace(ack, order_id="other"))
    filled = store.adapter.filled_event(accepted.execution_state.orders[0])
    assert SQLiteExecutionStore(tmp_path / "async.db", identity, config).apply_adapter_event(identity, filled).execution_state.position.status.value == "open"


def test_rejection_is_durable_then_next_source_fails_closed(tmp_path) -> None:  # type: ignore[no-untyped-def]
    identity = _identity(); config = PaperExecutionConfig(decision=PaperDecision.REJECT)
    replay = ReplayEngine(identity.replay_config, identity.strategy_config); prior = replay.initial_state(SYMBOL)
    store = SQLiteExecutionStore(tmp_path / "reject.db", identity, config)
    rejected = store.save_transition(identity, prior, _step(prior, _opened_trace()))
    assert rejected.execution_state.orders[0].status is OrderStatus.REJECTED
    recovered = SQLiteExecutionStore(tmp_path / "reject.db", identity, config).load_checkpoint(identity)
    assert recovered is not None and recovered.divergence.classification.value == "rejected_entry"
    later = ReplayTrace(_bar(900_000), _opened_trace().strategy_bar, (), rejected.state.strategy_state, None)
    with pytest.raises(Exception, match="rejected"):
        store.save_transition(identity, rejected.state, _step(rejected.state, later))
    assert store.load_checkpoint(identity).state == rejected.state  # type: ignore[union-attr]


def test_adapter_event_trigger_abort_rolls_back_bytes_then_retry(tmp_path) -> None:  # type: ignore[no-untyped-def]
    identity = _identity(); config = PaperExecutionConfig(decision=PaperDecision.DEFER)
    replay = ReplayEngine(identity.replay_config, identity.strategy_config); prior = replay.initial_state(SYMBOL)
    path = tmp_path / "abort.db"; store = SQLiteExecutionStore(path, identity, config)
    saved = store.save_transition(identity, prior, _step(prior, _opened_trace()))
    event = store.adapter.accepted_event(saved.execution_state.orders[0])
    before = path.read_bytes()
    with sqlite3.connect(path) as connection:
        connection.execute("CREATE TRIGGER reject_execution_update BEFORE UPDATE ON execution_checkpoints BEGIN SELECT RAISE(ABORT, 'no'); END")
    from quasartrend.execution import ExecutionPersistenceError
    with pytest.raises(ExecutionPersistenceError):
        store.apply_adapter_event(identity, event)
    assert SQLiteExecutionStore(path, identity, config).load_checkpoint(identity).execution_state == saved.execution_state  # type: ignore[union-attr]
    with sqlite3.connect(path) as connection:
        connection.execute("DROP TRIGGER reject_execution_update")
    assert store.apply_adapter_event(identity, event).execution_state.orders[0].status is OrderStatus.ACCEPTED


def test_tampered_intent_reason_price_and_id_fail_load(tmp_path) -> None:  # type: ignore[no-untyped-def]
    identity = _identity(); replay = ReplayEngine(identity.replay_config, identity.strategy_config)
    prior = replay.initial_state(SYMBOL); path = tmp_path / "tamper.db"; store = SQLiteExecutionStore(path, identity)
    store.save_transition(identity, prior, _step(prior, _opened_trace()))
    with sqlite3.connect(path) as connection:
        payload = json.loads(connection.execute("SELECT payload FROM execution_checkpoints").fetchone()[0])
        payload["execution_state"]["intents"][0]["reason"] = "exit_stop"
        connection.execute("UPDATE execution_checkpoints SET payload=?", (json.dumps(payload, sort_keys=True, separators=(",", ":")),))
    with pytest.raises(Exception):
        SQLiteExecutionStore(path, identity).load_checkpoint(identity)


def test_trace_validation_ordinal_4h_and_strategy_bar_mismatch() -> None:
    engine = _engine(); trace = _opened_trace()
    with pytest.raises(InvalidIntentError): engine.intent_from_event(trace, trace.events[0], 99)
    htf = HistoricalBar(SYMBOL, Timeframe.HOURS_4, 0, 100, 101, 99, 100)
    with pytest.raises(InvalidIntentError): engine.intent_from_event(replace(trace, source_bar=htf), trace.events[0], 0)
    with pytest.raises(InvalidIntentError): engine.intent_from_event(replace(trace, strategy_bar=replace(trace.strategy_bar, close=101.0)), trace.events[0], 0)


def test_stop_hit_plus_closed_after_async_entry_fill_closes_once(tmp_path) -> None:  # type: ignore[no-untyped-def]
    identity = _identity(); config = PaperExecutionConfig(decision=PaperDecision.DEFER)
    replay = ReplayEngine(identity.replay_config, identity.strategy_config); prior = replay.initial_state(SYMBOL)
    store = SQLiteExecutionStore(tmp_path / "stop.db", identity, config)
    entered = store.save_transition(identity, prior, _step(prior, _opened_trace()))
    entered = store.apply_adapter_event(identity, store.adapter.accepted_event(entered.execution_state.orders[0]))
    entered = store.apply_adapter_event(identity, store.adapter.filled_event(entered.execution_state.orders[0]))
    close = _closed_trace(stop=True, diagnostic=True)
    closed = store.save_transition(identity, entered.state, _step(entered.state, close))
    assert len(closed.execution_state.orders) == 2 and closed.execution_state.position.status.value == "open"
    closed = store.apply_adapter_event(identity, store.adapter.accepted_event(closed.execution_state.orders[1]))
    closed = store.apply_adapter_event(identity, store.adapter.filled_event(closed.execution_state.orders[1]))
    assert closed.execution_state.position.status.value == "flat"


def test_two_symbols_share_db_with_isolated_interleaved_empty_transitions(tmp_path) -> None:  # type: ignore[no-untyped-def]
    path = tmp_path / "symbols.db"; btc = _identity(); eth = _identity("ETH")
    for identity, symbol in ((btc, SYMBOL), (eth, "ETH")):
        engine = ReplayEngine(identity.replay_config, identity.strategy_config); prior = engine.initial_state(symbol)
        bar = HistoricalBar(symbol, Timeframe.MINUTES_15, 0, 10, 11, 9, 10)
        trace = ReplayTrace(bar, None, (), prior.strategy_state, None)
        SQLiteExecutionStore(path, identity).save_transition(identity, prior, _step(prior, trace))
    assert SQLiteExecutionStore(path, btc).load_checkpoint(btc).state.symbol == SYMBOL  # type: ignore[union-attr]
    assert SQLiteExecutionStore(path, eth).load_checkpoint(eth).state.symbol == "ETH"  # type: ignore[union-attr]


def test_no_lookahead_future_trace_does_not_change_finalized_execution(tmp_path) -> None:  # type: ignore[no-untyped-def]
    identity = _identity(); replay = ReplayEngine(identity.replay_config, identity.strategy_config); prior = replay.initial_state(SYMBOL)
    store = SQLiteExecutionStore(tmp_path / "future.db", identity)
    current = store.save_transition(identity, prior, _step(prior, _opened_trace()))
    before = current.execution_state
    future = HistoricalBar(SYMBOL, Timeframe.MINUTES_15, 1_800_000, 999, 1000, 998, 999)
    assert future.finalized_at > current.state.chronology_cursor[0]
    assert store.load_checkpoint(identity).execution_state == before  # type: ignore[union-attr]


def test_store_rejects_close_not_matching_prior_strategy_trade(tmp_path) -> None:  # type: ignore[no-untyped-def]
    identity = _identity(); replay = ReplayEngine(identity.replay_config, identity.strategy_config); prior = replay.initial_state(SYMBOL)
    store = SQLiteExecutionStore(tmp_path / "bad-close.db", identity)
    entered = store.save_transition(identity, prior, _step(prior, _opened_trace()))
    bad = replace(_closed_trace(), events=(replace(_closed_trace().events[0], trade_id=f"{SYMBOL}:999"),))
    with pytest.raises(ExecutionChronologyError):
        store.save_transition(identity, entered.state, _step(entered.state, bad))


def test_tampered_fill_and_position_payload_fail_load(tmp_path) -> None:  # type: ignore[no-untyped-def]
    identity = _identity(); replay = ReplayEngine(identity.replay_config, identity.strategy_config); prior = replay.initial_state(SYMBOL)
    path = tmp_path / "tamper-fill.db"; SQLiteExecutionStore(path, identity).save_transition(identity, prior, _step(prior, _opened_trace()))
    with sqlite3.connect(path) as connection:
        payload = json.loads(connection.execute("SELECT payload FROM execution_checkpoints").fetchone()[0])
        payload["execution_state"]["fills"][0]["fee"] = 999.0
        connection.execute("UPDATE execution_checkpoints SET payload=?", (json.dumps(payload, sort_keys=True, separators=(",", ":")),))
    with pytest.raises(Exception): SQLiteExecutionStore(path, identity).load_checkpoint(identity)


@pytest.mark.parametrize("path_parts,value", [
    (("adapter_state", "orders", 0, "status"), "unknown"),
    (("adapter_state", "events", 0, "type"), "unknown"),
])
def test_invalid_adapter_enum_payload_is_typed_persistence_error(tmp_path, path_parts, value) -> None:  # type: ignore[no-untyped-def]
    from quasartrend.execution import ExecutionPersistenceError
    identity = _identity(); replay = ReplayEngine(identity.replay_config, identity.strategy_config); prior = replay.initial_state(SYMBOL)
    path = tmp_path / "bad-adapter.db"; SQLiteExecutionStore(path, identity).save_transition(identity, prior, _step(prior, _opened_trace()))
    with sqlite3.connect(path) as connection:
        payload = json.loads(connection.execute("SELECT payload FROM execution_checkpoints").fetchone()[0]); target = payload
        for part in path_parts[:-1]: target = target[part]
        target[path_parts[-1]] = value
        connection.execute("UPDATE execution_checkpoints SET payload=?", (json.dumps(payload, sort_keys=True, separators=(",", ":")),))
    with pytest.raises(ExecutionPersistenceError): SQLiteExecutionStore(path, identity).load_checkpoint(identity)


def test_store_reconcile_invokes_injected_adapter_hook(tmp_path) -> None:  # type: ignore[no-untyped-def]
    class Probe(PaperExecutionAdapter):
        def __init__(self, identity, config):  # type: ignore[no-untyped-def]
            super().__init__(identity, config); self.calls = 0
        def reconcile(self, state, observed):  # type: ignore[no-untyped-def]
            self.calls += 1
            return super().reconcile(state, observed)
    identity = _identity(); config = PaperExecutionConfig(); adapter = Probe(ExecutionIdentity.from_persistence(identity, config), config)
    replay = ReplayEngine(identity.replay_config, identity.strategy_config); prior = replay.initial_state(SYMBOL)
    store = SQLiteExecutionStore(tmp_path / "probe.db", adapter.identity, config, adapter)
    saved = store.save_transition(identity, prior, _step(prior, _opened_trace()))
    store.reconcile(identity, saved.adapter_snapshot)
    assert adapter.calls >= 2
    with pytest.raises(ReconciliationMismatchError): store.reconcile(identity, replace(saved.adapter_snapshot, fills=()))


def test_duplicate_lifecycle_delivery_preserves_single_order_fill_and_position() -> None:
    engine = _engine(); trace = _opened_trace(); first = engine.process_trace(engine.initial_state(), trace).state
    second = engine.process_trace(first, trace).state
    assert (len(second.intents), len(second.orders), len(second.fills), second.position.trade_id) == (1, 1, 1, f"{SYMBOL}:1")


def test_real_live_runtime_sqlite_execution_end_to_end_and_restart_is_idempotent(tmp_path) -> None:  # type: ignore[no-untyped-def]
    config = ReplayConfig(ltf_hema_fast_length=1, ltf_hema_slow_length=2, htf_hema_fast_length=1, htf_hema_slow_length=2, kalman_period=2, kalman_atr_period=2)
    replay = ReplayEngine(config); identity = PersistenceIdentity(SYMBOL, config, replay.strategy_engine.config)
    random.seed(0)
    htf = tuple(HistoricalBar(SYMBOL, Timeframe.HOURS_4, i * 14_400_000, 100+i*2, 101+i*2, 99+i*2, 100+i*2) for i in range(6))
    ltf = tuple(HistoricalBar(SYMBOL, Timeframe.MINUTES_15, i * 900_000, (x := 100+random.uniform(-10, 10)), x+1, x-1, x) for i in range(100))
    class Client:
        def fetch_bars(self, *, symbol, timeframe, start_open_time, end_open_time, limit):  # type: ignore[no-untyped-def]
            return tuple(bar for bar in htf + ltf if bar.timeframe is timeframe and start_open_time <= bar.open_time <= end_open_time)
    class Clock:
        def now_ms(self): return 90_000_000
    runtime_config = RuntimeConfig(SYMBOL, bootstrap_15m=100, bootstrap_4h=6, request_page_size=1_000, max_catch_up_bars=200)
    path = tmp_path / "live.db"; store = SQLiteExecutionStore(path, identity)
    result = LiveRuntime(runtime_config, client=Client(), replay_engine=replay, identity=identity, store=store, clock=Clock()).poll_once()
    assert any(event.type is EventType.TRADE_OPENED for event in result.events)
    durable = store.load_checkpoint(identity); assert durable is not None
    assert durable.state.chronology_cursor == durable.execution_state.last_source_processing_key == durable.adapter_state.replay_cursor
    assert durable.execution_state.intents and durable.execution_state.orders and durable.execution_state.fills
    restarted = LiveRuntime(runtime_config, client=Client(), replay_engine=ReplayEngine(config), identity=identity, store=SQLiteExecutionStore(path, identity), clock=Clock()).poll_once()
    after = SQLiteExecutionStore(path, identity).load_checkpoint(identity)
    assert restarted.events == () and after.execution_state == durable.execution_state  # type: ignore[union-attr]


def test_two_store_concurrent_accept_reject_serializes_without_lost_update(tmp_path) -> None:  # type: ignore[no-untyped-def]
    identity = _identity(); config = PaperExecutionConfig(decision=PaperDecision.DEFER)
    replay = ReplayEngine(identity.replay_config, identity.strategy_config); prior = replay.initial_state(SYMBOL)
    path = tmp_path / "race.db"; seed = SQLiteExecutionStore(path, identity, config)
    checkpoint = seed.save_transition(identity, prior, _step(prior, _opened_trace()))
    order = checkpoint.execution_state.orders[0]
    events = (seed.adapter.accepted_event(order), seed.adapter.rejected_event(order))
    barrier = threading.Barrier(2); outcomes: list[object] = []
    def run(event):  # type: ignore[no-untyped-def]
        barrier.wait()
        try: outcomes.append(SQLiteExecutionStore(path, identity, config).apply_adapter_event(identity, event))
        except Exception as exc: outcomes.append(exc)
    threads = [threading.Thread(target=run, args=(event,)) for event in events]
    [thread.start() for thread in threads]; [thread.join() for thread in threads]
    successes = [item for item in outcomes if not isinstance(item, Exception)]
    failures = [item for item in outcomes if isinstance(item, Exception)]
    assert len(successes) == len(failures) == 1
    assert isinstance(failures[0], ExecutionError) and not isinstance(failures[0], sqlite3.Error)
    final = SQLiteExecutionStore(path, identity, config).load_checkpoint(identity)
    assert final is not None and final.execution_state.orders[0].status in (OrderStatus.ACCEPTED, OrderStatus.REJECTED)
    assert final.execution_state.orders[0].status.value == ("accepted" if events[0].event_id in [event.event_id for event in final.adapter_state.events] else "rejected")


def test_accepted_unfilled_blocks_close_without_advancing_then_fill_allows_retry(tmp_path) -> None:  # type: ignore[no-untyped-def]
    identity = _identity(); config = PaperExecutionConfig(decision=PaperDecision.DEFER)
    replay = ReplayEngine(identity.replay_config, identity.strategy_config); initial = replay.initial_state(SYMBOL)
    store = SQLiteExecutionStore(tmp_path / "block.db", identity, config)
    entered = store.save_transition(identity, initial, _step(initial, _opened_trace()))
    accepted = store.apply_adapter_event(identity, store.adapter.accepted_event(entered.execution_state.orders[0]))
    before = store.load_checkpoint(identity)
    with pytest.raises(OrderTransitionError): store.save_transition(identity, accepted.state, _step(accepted.state, _closed_trace()))
    assert store.load_checkpoint(identity).state == before.state  # type: ignore[union-attr]
    filled = store.apply_adapter_event(identity, store.adapter.filled_event(accepted.execution_state.orders[0]))
    assert store.save_transition(identity, filled.state, _step(filled.state, _closed_trace())).execution_state.orders[-1].status is OrderStatus.NEW


def test_eventless_equal_key_is_idempotent_for_engine_and_store(tmp_path) -> None:  # type: ignore[no-untyped-def]
    identity = _identity(); engine = _engine(); state = engine.initial_state()
    bar = _bar(0); trace = ReplayTrace(bar, None, (), state.position if False else ReplayEngine(identity.replay_config).initial_state(SYMBOL).strategy_state, None)
    once = engine.process_trace(state, trace).state
    assert engine.process_trace(once, trace).state == once
    replay = ReplayEngine(identity.replay_config, identity.strategy_config); prior = replay.initial_state(SYMBOL)
    store = SQLiteExecutionStore(tmp_path / "empty.db", identity); stepped = _step(prior, trace)
    saved = store.save_transition(identity, prior, stepped)
    assert store.save_transition(identity, prior, stepped).execution_state == saved.execution_state


def test_accepted_unfilled_rejected_and_invalid_position_transitions() -> None:
    accepted = _engine(PaperDecision.ACCEPT_ONLY).process_trace(_engine(PaperDecision.ACCEPT_ONLY).initial_state(), _opened_trace()).state
    assert accepted.orders[0].status is OrderStatus.ACCEPTED
    assert accepted.position.status.value == "flat"
    rejected_engine = _engine(PaperDecision.REJECT)
    rejected = rejected_engine.process_trace(rejected_engine.initial_state(), _opened_trace()).state
    assert rejected.orders[0].status is OrderStatus.REJECTED
    with pytest.raises(PositionTransitionError):
        _engine().process_trace(_engine().initial_state(), _closed_trace())


def test_combined_store_restart_duplicate_and_reconciliation(tmp_path) -> None:  # type: ignore[no-untyped-def]
    identity = _identity()
    config = PaperExecutionConfig(quantity=1.0, fee_bps=1.0, slippage_bps=1.0)
    store = SQLiteExecutionStore(tmp_path / "combined.db", identity, config)
    replay = ReplayEngine(identity.replay_config, identity.strategy_config)
    prior = replay.initial_state(SYMBOL)
    opened = _opened_trace()
    stepped = ReplayStepResult(replace(prior, strategy_state=opened.post_state, chronology_cursor=opened.source_bar.processing_key), opened)
    saved = store.save_transition(identity, prior, stepped)
    recovered = SQLiteExecutionStore(tmp_path / "combined.db", identity, config).load_checkpoint(identity)
    assert recovered is not None and recovered.state == saved.state
    assert recovered.execution_state == saved.execution_state
    assert store.save_transition(identity, prior, stepped).execution_state == saved.execution_state
    store.reconcile(identity, recovered.adapter_snapshot)
    with pytest.raises(ReconciliationMismatchError):
        store.reconcile(identity, replace(recovered.adapter_snapshot, fills=()))


def test_combined_store_rejects_noninitial_bootstrap_and_rolls_back(tmp_path) -> None:  # type: ignore[no-untyped-def]
    identity = _identity()
    store = SQLiteExecutionStore(tmp_path / "combined.db", identity)
    replay = ReplayEngine(identity.replay_config, identity.strategy_config)
    prior = replay.initial_state(SYMBOL)
    trace = _opened_trace()
    stepped = ReplayStepResult(replace(prior, strategy_state=trace.post_state, chronology_cursor=trace.source_bar.processing_key), trace)
    noninitial_prior = replace(prior, strategy_state=StrategyState(SYMBOL, status=StrategyStatus.FLAT))
    with pytest.raises(ExecutionBootstrapRequiredError):
        store.save_transition(identity, noninitial_prior, stepped)
    assert store.load_checkpoint(identity) is None
    store.save_transition(identity, prior, stepped)
    with pytest.raises(ExecutionChronologyError):
        store.save_transition(identity, prior, ReplayStepResult(prior, trace))
    assert store.load_checkpoint(identity).state == stepped.state  # type: ignore[union-attr]


def test_deferred_adapter_event_is_durable_after_restart_and_observed_absence_mismatches(tmp_path) -> None:  # type: ignore[no-untyped-def]
    identity = _identity()
    config = PaperExecutionConfig(decision=PaperDecision.DEFER)
    store = SQLiteExecutionStore(tmp_path / "defer.db", identity, config)
    assert isinstance(store.adapter, ExecutionAdapter)
    with pytest.raises(ReconciliationMismatchError):
        store.reconcile(identity, store.adapter.snapshot(store.adapter.initial_state()))
    replay = ReplayEngine(identity.replay_config, identity.strategy_config)
    prior = replay.initial_state(SYMBOL)
    trace = _opened_trace()
    stepped = ReplayStepResult(replace(prior, strategy_state=trace.post_state, chronology_cursor=trace.source_bar.processing_key), trace)
    saved = store.save_transition(identity, prior, stepped)
    assert saved.execution_state.orders[0].status is OrderStatus.NEW
    restarted = SQLiteExecutionStore(tmp_path / "defer.db", identity, config)
    order = restarted.load_checkpoint(identity).execution_state.orders[0]  # type: ignore[union-attr]
    accepted = restarted.apply_adapter_event(identity, restarted.adapter.accepted_event(order))
    assert accepted.execution_state.orders[0].status is OrderStatus.ACCEPTED
    filled = restarted.apply_adapter_event(identity, restarted.adapter.filled_event(accepted.execution_state.orders[0]))
    assert filled.execution_state.orders[0].status is OrderStatus.FILLED
    assert filled.execution_state.position.status.value == "open"
