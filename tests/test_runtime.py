from __future__ import annotations

from dataclasses import dataclass
from types import SimpleNamespace

import pytest

from quasartrend.backtest import BacktestEngine
from quasartrend.marketdata import (
    MarketDataGapError,
    MarketDataMalformedError,
    MarketDataPermanentError,
    MarketDataRetryExhaustedError,
    MarketDataTransientError,
)
from quasartrend.persistence import PersistenceIdentity, SQLiteCheckpointStore
from quasartrend.replay import HistoricalBar, ReplayConfig, ReplayEngine, ReplayResult, Timeframe
from quasartrend.runtime import LiveRuntime, RuntimeConfig, RuntimePersistenceError


SYMBOL = "BINANCE:BTCUSDT.P"


def _bar(timeframe: Timeframe, open_time: int, close: float = 100.0) -> HistoricalBar:
    return HistoricalBar(SYMBOL, timeframe, open_time, close, close + 1.0, close - 1.0, close, 1.0)


def _config(**changes: object) -> RuntimeConfig:
    values: dict[str, object] = dict(
        symbol=SYMBOL, bootstrap_15m=1, bootstrap_4h=1, request_page_size=2,
        max_catch_up_bars=100, retry_attempts=4, retry_base_delay_seconds=0.2,
    )
    values.update(changes)
    return RuntimeConfig(**values)  # type: ignore[arg-type]


def _replay() -> ReplayEngine:
    return ReplayEngine(ReplayConfig(
        ltf_hema_fast_length=1, ltf_hema_slow_length=2,
        htf_hema_fast_length=1, htf_hema_slow_length=2,
        kalman_period=2, kalman_atr_period=2,
    ))


def _identity() -> PersistenceIdentity:
    engine = _replay()
    return PersistenceIdentity(SYMBOL, engine.config, engine.strategy_engine.config)


@dataclass
class _Clock:
    value: int

    def now_ms(self) -> int:
        return self.value


class _Store:
    def __init__(self, state=None, *, fail_on_save: int | None = None) -> None:  # type: ignore[no-untyped-def]
        self.checkpoint = None if state is None else SimpleNamespace(state=state)
        self.saved = []
        self.fail_on_save = fail_on_save

    def load_checkpoint(self, identity):  # type: ignore[no-untyped-def]
        return self.checkpoint

    def save_checkpoint(self, identity, state):  # type: ignore[no-untyped-def]
        if self.fail_on_save is not None and len(self.saved) + 1 == self.fail_on_save:
            raise OSError("disk failure")
        self.saved.append(state)
        self.checkpoint = SimpleNamespace(state=state)


class _Client:
    def __init__(self, bars: tuple[HistoricalBar, ...], *, failures=()) -> None:  # type: ignore[no-untyped-def]
        self.bars = bars
        self.calls = []
        self.failures = list(failures)

    def fetch_bars(self, *, symbol, timeframe, start_open_time, end_open_time, limit):  # type: ignore[no-untyped-def]
        self.calls.append((timeframe, start_open_time, end_open_time, limit))
        if self.failures:
            raise self.failures.pop(0)
        return tuple(bar for bar in self.bars if bar.timeframe is timeframe and start_open_time <= bar.open_time <= end_open_time)


class _OpenReturningClient(_Client):
    def __init__(self, bars: tuple[HistoricalBar, ...]) -> None:
        super().__init__(bars)
        self.returned: list[tuple[HistoricalBar, ...]] = []

    def fetch_bars(self, **kwargs):  # type: ignore[no-untyped-def]
        bars = super().fetch_bars(**kwargs)
        self.returned.append(bars)
        return bars


def test_closed_candle_boundary_before_at_after_and_open_15m_exclusion() -> None:
    bar = _bar(Timeframe.MINUTES_15, 0)
    for now, expected in ((899_999, 0), (900_000, 1), (900_001, 1)):
        runtime = LiveRuntime(_config(), client=_Client((bar,)), replay_engine=_replay(), identity=_identity(), store=_Store(), clock=_Clock(now))
        assert len(runtime.poll_once().processed_bars) == expected


@pytest.mark.parametrize(
    "timeframe,now,required_other,expected_target",
    [
        (Timeframe.MINUTES_15, 899_999, (), ()),
        (Timeframe.MINUTES_15, 900_000, (), (0,)),
        (Timeframe.MINUTES_15, 900_001, (), (0,)),
        (Timeframe.HOURS_4, 14_399_999, (_bar(Timeframe.MINUTES_15, 12_600_000),), ()),
        (Timeframe.HOURS_4, 14_400_000, (_bar(Timeframe.MINUTES_15, 13_500_000),), (0,)),
        (Timeframe.HOURS_4, 14_400_001, (_bar(Timeframe.MINUTES_15, 13_500_000),), (0,)),
    ],
)
def test_returned_current_kline_is_optional_and_never_reaches_replay(
    timeframe: Timeframe, now: int, required_other: tuple[HistoricalBar, ...], expected_target: tuple[int, ...],
) -> None:
    duration = timeframe.duration_ms
    current = now // duration * duration
    prior = max(0, current - duration)
    target_bars = (_bar(timeframe, current, 101.0),) if prior == current else (
        _bar(timeframe, prior), _bar(timeframe, current, 101.0)
    )
    client = _OpenReturningClient(target_bars + required_other)
    runtime = LiveRuntime(_config(), client=client, replay_engine=_replay(), identity=_identity(), store=_Store(), clock=_Clock(now))
    result = runtime.poll_once()
    assert any(bar.timeframe is timeframe and bar.open_time == current for response in client.returned for bar in response)
    target_trace_opens = tuple(trace.source_bar.open_time for trace in result.traces if trace.source_bar.timeframe is timeframe)
    assert target_trace_opens == expected_target
    assert all(trace.source_bar.open_time != current for trace in result.traces)


def test_open_4h_excluded_and_shared_boundary_is_htf_then_ltf() -> None:
    htf = _bar(Timeframe.HOURS_4, 0, 99.0)
    prior_ltf = _bar(Timeframe.MINUTES_15, 12_600_000, 99.5)
    ltf = _bar(Timeframe.MINUTES_15, 13_500_000, 100.0)
    before = LiveRuntime(_config(), client=_Client((htf, prior_ltf, ltf)), replay_engine=_replay(), identity=_identity(), store=_Store(), clock=_Clock(14_399_999))
    assert [bar.timeframe for bar in before.poll_once().processed_bars] == [Timeframe.MINUTES_15]
    exact = LiveRuntime(_config(), client=_Client((htf, prior_ltf, ltf)), replay_engine=_replay(), identity=_identity(), store=_Store(), clock=_Clock(14_400_000))
    assert [bar.timeframe for bar in exact.poll_once().processed_bars] == [Timeframe.HOURS_4, Timeframe.MINUTES_15]


def test_repeat_poll_suppresses_overlap_duplicates_and_no_events_repeat() -> None:
    bars = (_bar(Timeframe.MINUTES_15, 0),)
    runtime = LiveRuntime(_config(), client=_Client(bars), replay_engine=_replay(), identity=_identity(), store=_Store(), clock=_Clock(900_000))
    first = runtime.poll_once()
    second = runtime.poll_once()
    assert len(first.processed_bars) == 1
    assert second.processed_bars == () and second.events == ()


def test_bootstrap_stays_pending_after_gap_then_retries_configured_suffix() -> None:
    client = _Client((_bar(Timeframe.MINUTES_15, 0),))
    runtime = LiveRuntime(_config(bootstrap_15m=2), client=client, replay_engine=_replay(), identity=_identity(), store=_Store(), clock=_Clock(1_800_000))
    with pytest.raises(MarketDataGapError):
        runtime.poll_once()
    assert client.calls[:1] == [(Timeframe.MINUTES_15, 0, 900_000, 2)]
    client.bars = (_bar(Timeframe.MINUTES_15, 0), _bar(Timeframe.MINUTES_15, 900_000))
    assert len(runtime.poll_once().processed_bars) == 2
    assert client.calls[1] == (Timeframe.MINUTES_15, 0, 900_000, 2)


def test_bootstrap_stays_pending_after_retry_exhaustion_then_recovers() -> None:
    client = _Client((_bar(Timeframe.MINUTES_15, 0),), failures=(MarketDataTransientError("a"), MarketDataTransientError("b")))
    runtime = LiveRuntime(_config(retry_attempts=2), client=client, replay_engine=_replay(), identity=_identity(), store=_Store(), clock=_Clock(900_000), sleeper=lambda _: None)
    with pytest.raises(MarketDataRetryExhaustedError):
        runtime.poll_once()
    assert len(runtime.poll_once().processed_bars) == 1


def test_fresh_bootstrap_matches_direct_replay_and_sqlite_checkpoint(tmp_path) -> None:  # type: ignore[no-untyped-def]
    bars = (
        _bar(Timeframe.HOURS_4, 0, 99.0), _bar(Timeframe.HOURS_4, 14_400_000, 101.0),
        _bar(Timeframe.MINUTES_15, 27_000_000, 100.0), _bar(Timeframe.MINUTES_15, 27_900_000, 102.0),
    )
    replay = _replay()
    identity = PersistenceIdentity(SYMBOL, replay.config, replay.strategy_engine.config)
    runtime = LiveRuntime(_config(bootstrap_15m=2, bootstrap_4h=2), client=_Client(bars), replay_engine=replay, identity=identity, store=SQLiteCheckpointStore(tmp_path / "state.db"), clock=_Clock(28_800_000))
    result = runtime.poll_once()
    expected = replay.run(tuple(sorted(bars, key=lambda bar: bar.processing_key)))
    assert result.initialized_from_bootstrap and result.state == expected.state
    assert SQLiteCheckpointStore(tmp_path / "state.db").load_checkpoint(identity).state == expected.state  # type: ignore[union-attr]


def test_live_replay_and_backtest_equivalence_for_same_finalized_stream() -> None:
    ltf = tuple(_bar(Timeframe.MINUTES_15, index * 900_000, 100.0 + (index % 7) - 3) for index in range(48))
    htf = tuple(_bar(Timeframe.HOURS_4, index * 14_400_000, 100.0 + index) for index in range(3))
    bars = ltf + htf
    replay = _replay()
    runtime = LiveRuntime(_config(bootstrap_15m=48, bootstrap_4h=3), client=_Client(bars), replay_engine=replay, identity=_identity(), store=_Store(), clock=_Clock(43_200_000))
    live = runtime.poll_once()
    direct = replay.run(tuple(sorted(bars, key=lambda bar: bar.processing_key)))
    assert live.state == direct.state
    assert live.traces == direct.traces and live.events == direct.events
    assert BacktestEngine().run_traces(live.traces, live.state.strategy_state) == BacktestEngine().run(direct)


def test_checkpoint_resume_fetches_overlap_but_only_first_suffix_once() -> None:
    replay = _replay()
    prefix_bar = _bar(Timeframe.MINUTES_15, 0)
    prefix = replay.run((prefix_bar,))
    suffix = _bar(Timeframe.MINUTES_15, 900_000)
    client = _Client((prefix_bar, suffix))
    runtime = LiveRuntime(_config(), client=client, replay_engine=replay, identity=_identity(), store=_Store(prefix.state), clock=_Clock(1_800_000))
    result = runtime.poll_once()
    assert result.initialized_from_checkpoint
    assert result.processed_bars == (suffix,)
    assert prefix_bar not in result.processed_bars


def test_restart_exactly_at_shared_boundary_respects_checkpoint_priority_zero_and_one() -> None:
    replay = _replay()
    htf = _bar(Timeframe.HOURS_4, 0)
    ltf_before = _bar(Timeframe.MINUTES_15, 12_600_000)
    ltf_boundary = _bar(Timeframe.MINUTES_15, 13_500_000)
    htf_priority_zero = replay.run((htf,)).state
    priority_zero = LiveRuntime(_config(), client=_Client((htf, ltf_before, ltf_boundary)), replay_engine=replay, identity=_identity(), store=_Store(htf_priority_zero), clock=_Clock(14_400_000)).poll_once()
    assert priority_zero.processed_bars == (ltf_boundary,)
    full_boundary = replay.run((htf, ltf_boundary)).state
    priority_one = LiveRuntime(_config(), client=_Client((htf, ltf_boundary)), replay_engine=replay, identity=_identity(), store=_Store(full_boundary), clock=_Clock(14_400_000)).poll_once()
    assert priority_one.processed_bars == ()


def test_downtime_pagination_and_shared_boundary_merge_in_frozen_order() -> None:
    bars = tuple(_bar(Timeframe.MINUTES_15, value) for value in range(0, 28_800_000, 900_000)) + (
        _bar(Timeframe.HOURS_4, 0), _bar(Timeframe.HOURS_4, 14_400_000),
    )
    runtime = LiveRuntime(_config(bootstrap_15m=32, bootstrap_4h=2), client=_Client(bars), replay_engine=_replay(), identity=_identity(), store=_Store(), clock=_Clock(28_800_000))
    result = runtime.poll_once()
    assert len(result.processed_bars) == 34
    keys = [bar.processing_key for bar in result.processed_bars]
    assert keys == sorted(keys)
    shared = [bar.timeframe for bar in result.processed_bars if bar.finalized_at == 14_400_000]
    assert shared == [Timeframe.HOURS_4, Timeframe.MINUTES_15]
    assert len(runtime.client.calls) > 2  # type: ignore[attr-defined]


def test_checkpoint_downtime_uses_multiple_pages_and_rejects_page_holes_or_overlap() -> None:
    replay = _replay()
    prefix = replay.run((_bar(Timeframe.MINUTES_15, 0),))
    bars = tuple(_bar(Timeframe.MINUTES_15, value) for value in range(0, 5_400_000, 900_000))
    client = _Client(bars)
    runtime = LiveRuntime(_config(request_page_size=2), client=client, replay_engine=replay, identity=_identity(), store=_Store(prefix.state), clock=_Clock(5_400_000))
    assert [bar.open_time for bar in runtime.poll_once().processed_bars] == [900_000, 1_800_000, 2_700_000, 3_600_000, 4_500_000]
    assert [call[1:3] for call in client.calls if call[0] is Timeframe.MINUTES_15] == [(0, 900_000), (1_800_000, 2_700_000), (3_600_000, 4_500_000), (5_400_000, 5_400_000)]

    class _OverlapClient(_Client):
        def fetch_bars(self, **kwargs):  # type: ignore[no-untyped-def]
            returned = super().fetch_bars(**kwargs)
            if kwargs["start_open_time"] == 1_800_000:
                return (_bar(Timeframe.MINUTES_15, 0),) + returned
            return returned

    overlap = LiveRuntime(_config(bootstrap_15m=6, request_page_size=2), client=_OverlapClient(bars), replay_engine=_replay(), identity=_identity(), store=_Store(), clock=_Clock(5_400_000))
    with pytest.raises(MarketDataMalformedError, match="outside request"):
        overlap.poll_once()

    class _HoleClient(_Client):
        def fetch_bars(self, **kwargs):  # type: ignore[no-untyped-def]
            returned = super().fetch_bars(**kwargs)
            if kwargs["start_open_time"] == 1_800_000:
                return returned[1:]
            return returned

    hole = LiveRuntime(_config(bootstrap_15m=6, request_page_size=2), client=_HoleClient(bars), replay_engine=_replay(), identity=_identity(), store=_Store(), clock=_Clock(5_400_000))
    with pytest.raises(MarketDataGapError, match="missing"):
        hole.poll_once()


def test_missing_duplicate_out_of_order_and_wrong_client_identity_are_explicit() -> None:
    missing = LiveRuntime(_config(bootstrap_15m=2), client=_Client((_bar(Timeframe.MINUTES_15, 0),)), replay_engine=_replay(), identity=_identity(), store=_Store(), clock=_Clock(1_800_000))
    with pytest.raises(MarketDataGapError):
        missing.poll_once()
    duplicate = _Client((_bar(Timeframe.MINUTES_15, 0), _bar(Timeframe.MINUTES_15, 0)))
    runtime = LiveRuntime(_config(), client=duplicate, replay_engine=_replay(), identity=_identity(), store=_Store(), clock=_Clock(900_000))
    with pytest.raises(MarketDataGapError):
        runtime.poll_once()
    wrong_symbol = HistoricalBar("OTHER", Timeframe.MINUTES_15, 0, 1, 2, 0, 1)
    runtime = LiveRuntime(_config(), client=_Client((wrong_symbol,)), replay_engine=_replay(), identity=_identity(), store=_Store(), clock=_Clock(900_000))
    with pytest.raises(MarketDataMalformedError, match="symbol"):
        runtime.poll_once()
    wrong_timeframe = _bar(Timeframe.HOURS_4, 0)

    class _WrongTimeframeClient(_Client):
        def fetch_bars(self, **kwargs):  # type: ignore[no-untyped-def]
            return (wrong_timeframe,)

    runtime = LiveRuntime(_config(), client=_WrongTimeframeClient(()), replay_engine=_replay(), identity=_identity(), store=_Store(), clock=_Clock(900_000))
    with pytest.raises(MarketDataMalformedError, match="timeframe"):
        runtime.poll_once()


def test_transient_retry_retry_after_exhaustion_and_malformed_no_retry() -> None:
    sleeps: list[float] = []
    runtime = LiveRuntime(_config(), client=_Client((_bar(Timeframe.MINUTES_15, 0),), failures=(MarketDataTransientError("one", retry_after_seconds=1.0),)), replay_engine=_replay(), identity=_identity(), store=_Store(), clock=_Clock(900_000), sleeper=sleeps.append)
    assert len(runtime.poll_once().processed_bars) == 1
    assert sleeps == [1.0]
    exhausted = LiveRuntime(_config(retry_attempts=2), client=_Client((), failures=(MarketDataTransientError("a"), MarketDataTransientError("b"))), replay_engine=_replay(), identity=_identity(), store=_Store(), clock=_Clock(900_000), sleeper=sleeps.append)
    with pytest.raises(MarketDataRetryExhaustedError):
        exhausted.poll_once()
    permanent_client = _Client((), failures=(MarketDataPermanentError("bad"),))
    permanent = LiveRuntime(_config(), client=permanent_client, replay_engine=_replay(), identity=_identity(), store=_Store(), clock=_Clock(900_000))
    with pytest.raises(MarketDataPermanentError):
        permanent.poll_once()
    assert len(permanent_client.calls) == 1
    malformed_client = _Client((), failures=(MarketDataMalformedError("bad"),))
    malformed = LiveRuntime(_config(), client=malformed_client, replay_engine=_replay(), identity=_identity(), store=_Store(), clock=_Clock(900_000))
    with pytest.raises(MarketDataMalformedError):
        malformed.poll_once()
    assert len(malformed_client.calls) == 1


def test_persistence_failure_keeps_last_durable_state_and_loop_stops_cooperatively() -> None:
    bars = (_bar(Timeframe.MINUTES_15, 0), _bar(Timeframe.MINUTES_15, 900_000))
    store = _Store(fail_on_save=2)
    runtime = LiveRuntime(_config(bootstrap_15m=2), client=_Client(bars), replay_engine=_replay(), identity=_identity(), store=store, clock=_Clock(1_800_000))
    with pytest.raises(RuntimePersistenceError):
        runtime.poll_once()
    assert len(store.saved) == 1 and runtime.state == store.saved[-1]
    store.fail_on_save = None
    runtime.clock.value = 2_700_000
    runtime.client.bars = bars + (_bar(Timeframe.MINUTES_15, 1_800_000),)  # type: ignore[attr-defined]
    call_count = len(runtime.client.calls)  # type: ignore[attr-defined]
    assert runtime.poll_once().processed_bars == (_bar(Timeframe.MINUTES_15, 900_000), _bar(Timeframe.MINUTES_15, 1_800_000))
    assert runtime.client.calls[call_count] == (Timeframe.MINUTES_15, 0, 900_000, 2)  # type: ignore[attr-defined]
    stopped = LiveRuntime(_config(), client=_Client(bars), replay_engine=_replay(), identity=_identity(), store=_Store(), clock=_Clock(900_000))
    assert tuple(stopped.polling_loop(lambda: True)) == ()
