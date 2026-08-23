from __future__ import annotations

from dataclasses import replace
import json
import sqlite3

import pytest

from quasartrend.backtest import BacktestConfig, BacktestEngine
from quasartrend.persistence import (
    CHECKPOINT_VERSION,
    CheckpointCorruptionError,
    CheckpointVersionError,
    ChronologyRegressionError,
    CodecError,
    ConfigMismatchError,
    PersistenceIdentity,
    PersistenceWriteError,
    SQLiteCheckpointStore,
    SchemaVersionError,
    SymbolMismatchError,
    decode_replay_state,
    encode_replay_state,
)
from quasartrend.replay import HistoricalBar, ReplayConfig, ReplayEngine, ReplayResult, Timeframe
from quasartrend.strategy import (
    Direction,
    OpenTrade,
    ReadinessState,
    StrategyBar,
    StrategyEngine,
    StrategyState,
    StrategyStatus,
)


def _config() -> ReplayConfig:
    return ReplayConfig(
        ltf_hema_fast_length=1, ltf_hema_slow_length=2,
        htf_hema_fast_length=1, htf_hema_slow_length=2,
        kalman_period=2, kalman_alpha=0.05, kalman_beta=0.2,
        kalman_atr_period=2,
    )


def _bar(timeframe: Timeframe, open_time: int, close: float, symbol: str = "BTC") -> HistoricalBar:
    return HistoricalBar(symbol, timeframe, open_time, close, close + 1.0, close - 1.0, close)


def _stream(symbol: str = "BTC") -> tuple[HistoricalBar, ...]:
    return (
        _bar(Timeframe.MINUTES_15, 12_600_000, 100.25, symbol),
        _bar(Timeframe.HOURS_4, 0, 100.75, symbol),
        _bar(Timeframe.MINUTES_15, 27_000_000, 99.5, symbol),
        _bar(Timeframe.HOURS_4, 14_400_000, 110.25, symbol),
        _bar(Timeframe.MINUTES_15, 27_900_000, 100.5, symbol),
        _bar(Timeframe.MINUTES_15, 28_800_000, 101.75, symbol),
        _bar(Timeframe.HOURS_4, 28_800_000, 90.5, symbol),
        _bar(Timeframe.MINUTES_15, 42_300_000, 102.25, symbol),
    )


def _identity(symbol: str = "BTC", config: ReplayConfig | None = None) -> PersistenceIdentity:
    return PersistenceIdentity(symbol, config or _config())


def _state_at(split: int):
    engine = ReplayEngine(_config())
    return engine.run(_stream()[:split]).state


def _row_payload(path) -> str:
    with sqlite3.connect(path) as connection:
        return connection.execute("SELECT payload FROM checkpoints").fetchone()[0]


def _replace_payload(path, payload: str) -> None:
    with sqlite3.connect(path) as connection:
        connection.execute("UPDATE checkpoints SET payload=?", (payload,))


def test_codec_round_trip_is_canonical_and_restores_tuple_cursor() -> None:
    state = _state_at(5)
    payload = encode_replay_state(state, expected_config=_config())
    assert payload == encode_replay_state(state, expected_config=_config())
    restored = decode_replay_state(payload, expected_config=_config())
    assert restored == state
    assert isinstance(restored.chronology_cursor, tuple)
    assert "NaN" not in payload and "Infinity" not in payload


def test_store_reopens_without_constructor_or_load_creation(tmp_path) -> None:
    path = tmp_path / "nested" / "checkpoint.db"
    identity = _identity()
    store = SQLiteCheckpointStore(path)
    assert not path.exists()
    assert store.load_checkpoint(identity) is None
    assert store.delete_checkpoint(identity) is False
    assert not path.exists()
    saved = store.save_checkpoint(identity, _state_at(3))
    assert path.exists() and saved.last_finalized_at == saved.state.chronology_cursor[0]
    recovered = SQLiteCheckpointStore(path).load_checkpoint(identity)
    assert recovered is not None and recovered.state == saved.state


def test_existing_empty_database_is_absent_until_save_initializes_it(tmp_path) -> None:
    path = tmp_path / "empty.db"
    sqlite3.connect(path).close()
    identity = _identity()
    assert SQLiteCheckpointStore(path).load_checkpoint(identity) is None
    SQLiteCheckpointStore(path).save_checkpoint(identity, _state_at(1))
    with sqlite3.connect(path) as connection:
        assert connection.execute("PRAGMA user_version").fetchone()[0] == 1


def test_true_restart_prefix_suffix_is_identical_including_htf_indicator_and_backtest(tmp_path) -> None:
    config = _config()
    bars = _stream()
    full = ReplayEngine(config).run(bars)
    prefix = ReplayEngine(config).run(bars[:5])
    path = tmp_path / "recovery.db"
    SQLiteCheckpointStore(path).save_checkpoint(_identity(config=config), prefix.state)
    # New engine/store objects ensure no process-local checkpoint is reused.
    recovered = SQLiteCheckpointStore(path).load_checkpoint(_identity(config=config))
    assert recovered is not None
    suffix = ReplayEngine(config).run(bars[5:], recovered.state)
    resumed = ReplayResult(suffix.state, prefix.traces + suffix.traces)
    assert resumed.state == full.state
    assert resumed.traces == full.traces
    assert BacktestEngine(BacktestConfig(fee_bps=2.0, slippage_bps=1.0)).run(resumed) == BacktestEngine(BacktestConfig(fee_bps=2.0, slippage_bps=1.0)).run(full)


def test_warmup_pending_open_trade_and_sequence_are_preserved(tmp_path) -> None:
    identity = _identity()
    store = SQLiteCheckpointStore(tmp_path / "state.db")
    warmup = _state_at(1)
    store.save_checkpoint(identity, warmup)
    assert SQLiteCheckpointStore(tmp_path / "state.db").load_checkpoint(identity).state == warmup  # type: ignore[union-attr]
    open_state = _state_at(5)
    assert open_state.strategy_state.trade is not None
    store.save_checkpoint(identity, open_state)
    recovered = SQLiteCheckpointStore(tmp_path / "state.db").load_checkpoint(identity)
    assert recovered is not None and recovered.state.strategy_state.trade == open_state.strategy_state.trade
    pending_strategy = StrategyState(
        symbol="BTC", status=StrategyStatus.PENDING_LONG, current_bias=Direction.LONG,
        bias_epoch=1, bias_activation_timestamp=1, pending_direction=Direction.LONG,
        pending_flip_timestamp=2, pending_bias_epoch=1, next_trade_sequence=7,
        readiness=ReadinessState.READY,
    )
    pending_state = replace(open_state, strategy_state=pending_strategy)
    store.save_checkpoint(identity, pending_state)
    restored = SQLiteCheckpointStore(tmp_path / "state.db").load_checkpoint(identity)
    assert restored is not None and restored.state.strategy_state == pending_strategy


def test_duplicate_and_out_of_order_remain_rejected_after_recovery(tmp_path) -> None:
    config = _config()
    state = _state_at(5)
    path = tmp_path / "chronology.db"
    SQLiteCheckpointStore(path).save_checkpoint(_identity(config=config), state)
    restored = SQLiteCheckpointStore(path).load_checkpoint(_identity(config=config))
    assert restored is not None
    engine = ReplayEngine(config)
    with pytest.raises(ValueError, match="duplicate"):
        engine.step(restored.state, _stream()[4])
    with pytest.raises(ValueError, match="strict finalization"):
        engine.step(restored.state, _stream()[2])


def test_identity_is_deterministic_and_semantic_changes_are_rejected(tmp_path) -> None:
    config = _config()
    first = _identity(config=config)
    assert first.canonical_json == _identity(config=config).canonical_json
    assert first.config_fingerprint == _identity(config=config).config_fingerprint
    assert '"schema_version":1' in first.canonical_json
    assert '"checkpoint_version":1' in first.canonical_json
    path = tmp_path / "identity.db"
    SQLiteCheckpointStore(path).save_checkpoint(first, _state_at(2))
    changed_atr = PersistenceIdentity("BTC", config, replace(first.strategy_config, atr_multiplier=2.0))
    changed_replay = PersistenceIdentity("BTC", replace(config, kalman_factor=2.0))
    for changed in (changed_atr, changed_replay):
        with pytest.raises(ConfigMismatchError):
            SQLiteCheckpointStore(path).load_checkpoint(changed)
        with pytest.raises(ConfigMismatchError):
            SQLiteCheckpointStore(path).delete_checkpoint(changed)


def test_configuration_replacement_requires_explicit_delete_and_preserves_prior_row(tmp_path) -> None:
    path = tmp_path / "identity-replacement.db"
    first = _identity()
    changed = PersistenceIdentity("BTC", _config(), replace(first.strategy_config, atr_multiplier=2.0))
    state = _state_at(4)
    SQLiteCheckpointStore(path).save_checkpoint(first, state)
    with pytest.raises(ConfigMismatchError):
        SQLiteCheckpointStore(path).save_checkpoint(changed, state)
    assert SQLiteCheckpointStore(path).load_checkpoint(first).state == state  # type: ignore[union-attr]
    assert SQLiteCheckpointStore(path).delete_checkpoint(first) is True
    assert SQLiteCheckpointStore(path).save_checkpoint(changed, state).state == state


def test_symbol_mismatch_and_cursor_regression_are_explicit(tmp_path) -> None:
    path = tmp_path / "safety.db"
    store = SQLiteCheckpointStore(path)
    with pytest.raises(SymbolMismatchError):
        store.save_checkpoint(_identity("ETH"), _state_at(1))
    identity = _identity()
    newer = _state_at(4)
    store.save_checkpoint(identity, newer)
    with pytest.raises(ChronologyRegressionError):
        store.save_checkpoint(identity, _state_at(2))
    assert SQLiteCheckpointStore(path).load_checkpoint(identity).state == newer  # type: ignore[union-attr]


def test_schema_and_checkpoint_version_mismatches_are_never_absence(tmp_path) -> None:
    path = tmp_path / "versions.db"
    identity = _identity()
    SQLiteCheckpointStore(path).save_checkpoint(identity, _state_at(2))
    with sqlite3.connect(path) as connection:
        connection.execute("PRAGMA user_version=2")
    with pytest.raises(SchemaVersionError):
        SQLiteCheckpointStore(path).load_checkpoint(identity)
    path.unlink()
    SQLiteCheckpointStore(path).save_checkpoint(identity, _state_at(2))
    with sqlite3.connect(path) as connection:
        connection.execute("UPDATE checkpoints SET checkpoint_version=99")
    with pytest.raises(CheckpointVersionError):
        SQLiteCheckpointStore(path).load_checkpoint(identity)


def test_nonempty_v0_and_lookalike_schema_are_rejected(tmp_path) -> None:
    v0 = tmp_path / "v0.db"
    with sqlite3.connect(v0) as connection:
        connection.execute("CREATE TABLE unrelated (value INTEGER)")
    with pytest.raises(SchemaVersionError):
        SQLiteCheckpointStore(v0).load_checkpoint(_identity())
    lookalike = tmp_path / "lookalike.db"
    with sqlite3.connect(lookalike) as connection:
        connection.execute("""CREATE TABLE checkpoints (
            symbol TEXT NOT NULL, execution_timeframe TEXT NOT NULL, htf_timeframe TEXT NOT NULL,
            config_fingerprint BLOB NOT NULL, checkpoint_version TEXT NOT NULL, saved_at_ms INTEGER NOT NULL,
            last_finalized_at INTEGER, last_priority INTEGER, payload TEXT NOT NULL,
            PRIMARY KEY(symbol, execution_timeframe)
        )""")
        connection.execute("PRAGMA user_version=1")
    with pytest.raises(SchemaVersionError):
        SQLiteCheckpointStore(lookalike).load_checkpoint(_identity())


@pytest.mark.parametrize("mutator", ["malformed", "missing", "enum", "nested"])
def test_corrupt_payloads_are_rejected(tmp_path, mutator: str) -> None:
    path = tmp_path / f"{mutator}.db"
    identity = _identity()
    SQLiteCheckpointStore(path).save_checkpoint(identity, _state_at(3))
    if mutator == "malformed":
        _replace_payload(path, "{")
    else:
        payload = json.loads(_row_payload(path))
        if mutator == "missing":
            del payload["state"]["strategy_state"]["status"]
        elif mutator == "enum":
            payload["state"]["latest_htf_bias"] = "upward"
        else:
            del payload["state"]["ltf_hema_checkpoint"]
        _replace_payload(path, json.dumps(payload, sort_keys=True, separators=(",", ":")))
    with pytest.raises(CheckpointCorruptionError):
        SQLiteCheckpointStore(path).load_checkpoint(identity)


def test_codec_rejects_duplicate_nan_unknown_and_invalid_nested_indicator_fields() -> None:
    payload = json.loads(encode_replay_state(_state_at(2), expected_config=_config()))
    payload["state"]["ltf_kalman_checkpoint"] = "{\"type\":\"KalmanStep\",\"type\":\"KalmanStep\"}"
    with pytest.raises(CodecError):
        decode_replay_state(json.dumps(payload, sort_keys=True, separators=(",", ":")), expected_config=_config())
    with pytest.raises(CodecError):
        decode_replay_state('{"checkpoint_version":1,"state":NaN}', expected_config=_config())
    with pytest.raises(CheckpointVersionError):
        decode_replay_state('{"checkpoint_version":2,"state":{}}')


@pytest.mark.parametrize("mutation", ["previous_direction", "previous_fast", "unknown", "bool_int"])
def test_strict_nested_indicator_and_integer_corruption_is_rejected(tmp_path, mutation: str) -> None:
    path = tmp_path / f"strict-{mutation}.db"
    identity = _identity()
    SQLiteCheckpointStore(path).save_checkpoint(identity, _state_at(4))
    payload = json.loads(_row_payload(path))
    if mutation == "previous_direction":
        nested = json.loads(payload["state"]["ltf_kalman_checkpoint"])
        del nested["previous_direction"]
        payload["state"]["ltf_kalman_checkpoint"] = json.dumps(nested, sort_keys=True, separators=(",", ":"))
    elif mutation == "previous_fast":
        nested = json.loads(payload["state"]["ltf_hema_checkpoint"])
        del nested["previous_fast"]
        payload["state"]["ltf_hema_checkpoint"] = json.dumps(nested, sort_keys=True, separators=(",", ":"))
    elif mutation == "unknown":
        nested = json.loads(payload["state"]["ltf_hema_checkpoint"])
        nested["unexpected"] = 1
        payload["state"]["ltf_hema_checkpoint"] = json.dumps(nested, sort_keys=True, separators=(",", ":"))
    else:
        payload["state"]["strategy_state"]["bias_epoch"] = True
    _replace_payload(path, json.dumps(payload, sort_keys=True, separators=(",", ":")))
    with pytest.raises(CheckpointCorruptionError):
        SQLiteCheckpointStore(path).load_checkpoint(identity)


def test_outer_and_row_metadata_are_strictly_validated(tmp_path) -> None:
    path = tmp_path / "metadata.db"
    identity = _identity()
    SQLiteCheckpointStore(path).save_checkpoint(identity, _state_at(4))
    payload = json.loads(_row_payload(path))
    payload["checkpoint_version"] = True
    _replace_payload(path, json.dumps(payload, sort_keys=True, separators=(",", ":")))
    with pytest.raises(CheckpointVersionError):
        SQLiteCheckpointStore(path).load_checkpoint(identity)
    SQLiteCheckpointStore(path).save_checkpoint(identity, _state_at(4))
    with sqlite3.connect(path) as connection:
        connection.execute("UPDATE checkpoints SET saved_at_ms='not-an-int'")
    with pytest.raises(CheckpointCorruptionError):
        SQLiteCheckpointStore(path).load_checkpoint(identity)


def test_atomic_replacement_and_failed_write_preserves_prior_row(tmp_path) -> None:
    path = tmp_path / "atomic.db"
    identity = _identity()
    store = SQLiteCheckpointStore(path)
    first = _state_at(2)
    second = _state_at(4)
    store.save_checkpoint(identity, first)
    store.save_checkpoint(identity, second)
    assert SQLiteCheckpointStore(path).load_checkpoint(identity).state == second  # type: ignore[union-attr]
    with sqlite3.connect(path) as connection:
        connection.execute("""CREATE TRIGGER fail_checkpoint_update BEFORE UPDATE ON checkpoints
                              BEGIN SELECT RAISE(ABORT, 'test failure'); END""")
    with pytest.raises(PersistenceWriteError):
        store.save_checkpoint(identity, _state_at(5))
    assert SQLiteCheckpointStore(path).load_checkpoint(identity).state == second  # type: ignore[union-attr]


def test_delete_returns_explicit_status(tmp_path) -> None:
    path = tmp_path / "delete.db"
    identity = _identity()
    store = SQLiteCheckpointStore(path)
    assert store.delete_checkpoint(identity) is False
    store.save_checkpoint(identity, _state_at(1))
    assert store.delete_checkpoint(identity) is True
    assert store.load_checkpoint(identity) is None


@pytest.mark.parametrize("split", range(1, len(_stream())))
def test_true_store_engine_restart_matches_uninterrupted_for_every_legal_split(tmp_path, split: int) -> None:
    config = _config()
    bars = _stream()
    full = ReplayEngine(config).run(bars)
    prefix = ReplayEngine(config).run(bars[:split])
    path = tmp_path / f"split-{split}.db"
    SQLiteCheckpointStore(path).save_checkpoint(_identity(config=config), prefix.state)
    recovered = SQLiteCheckpointStore(path).load_checkpoint(_identity(config=config))
    assert recovered is not None
    suffix = ReplayEngine(config).run(bars[split:], recovered.state)
    resumed = ReplayResult(suffix.state, prefix.traces + suffix.traces)
    assert resumed == full
    if split == 4:
        assert recovered.state.chronology_cursor == (28_800_000, 0)
        assert bars[split].processing_key == (28_800_000, 1)
        assert suffix.traces[0].source_bar == bars[split]


def test_integer_valued_open_trade_canonicalizes_and_continues_after_restart(tmp_path) -> None:
    config = _config()
    state = _state_at(5)
    original_trade = state.strategy_state.trade
    assert original_trade is not None
    integer_trade = OpenTrade(
        trade_id=original_trade.trade_id, side=original_trade.side,
        entry_price=100, entry_timestamp=original_trade.entry_timestamp,
        atr_at_entry=1, stop_price=101, bias_epoch=original_trade.bias_epoch,
        setup_origin_timestamp=original_trade.setup_origin_timestamp,
    )
    integer_state = replace(state, strategy_state=replace(state.strategy_state, trade=integer_trade))
    path = tmp_path / "integer-trade.db"
    identity = _identity(config=config)
    SQLiteCheckpointStore(path).save_checkpoint(identity, integer_state)
    restored = SQLiteCheckpointStore(path).load_checkpoint(identity)
    assert restored is not None and restored.state == integer_state
    encoded_trade = json.loads(_row_payload(path))["state"]["strategy_state"]["trade"]
    assert all(isinstance(encoded_trade[field], float) for field in ("entry_price", "atr_at_entry", "stop_price"))
    assert ReplayEngine(config).run(_stream()[5:], restored.state) == ReplayEngine(config).run(_stream()[5:], integer_state)


def test_invalid_stop_is_codec_error_and_pending_state_continues_with_sequence(tmp_path) -> None:
    state = _state_at(5)
    path = tmp_path / "pending-continuation.db"
    identity = _identity()
    invalid_payload = json.loads(encode_replay_state(state, expected_config=_config()))
    invalid_payload["state"]["strategy_state"]["trade"]["stop_price"] = -1.0
    with pytest.raises(CodecError):
        decode_replay_state(json.dumps(invalid_payload, sort_keys=True, separators=(",", ":")), expected_config=_config())
    pending = StrategyState(
        symbol="BTC", status=StrategyStatus.PENDING_LONG, current_bias=Direction.LONG,
        bias_epoch=1, bias_activation_timestamp=1, pending_direction=Direction.LONG,
        pending_flip_timestamp=2, pending_bias_epoch=1, next_trade_sequence=7,
        readiness=ReadinessState.READY,
    )
    pending_state = replace(state, strategy_state=pending)
    SQLiteCheckpointStore(path).save_checkpoint(identity, pending_state)
    restored = SQLiteCheckpointStore(path).load_checkpoint(identity)
    assert restored is not None
    confirming = StrategyBar(
        symbol="BTC", timestamp=3, open=100.0, high=101.0, low=99.0, close=100.0,
        htf_bias=Direction.LONG, hema_direction=Direction.LONG, kalman_direction=Direction.LONG,
        atr=1.0,
    )
    expected = StrategyEngine().step(pending, confirming)
    actual = StrategyEngine().step(restored.state.strategy_state, confirming)
    assert actual == expected
    assert actual.state.trade is not None and actual.state.trade.trade_id == "BTC:7"
    assert actual.state.next_trade_sequence == 8
