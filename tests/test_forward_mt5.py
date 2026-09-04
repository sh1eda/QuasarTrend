from __future__ import annotations

from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace

import pytest
import quasartrend.forward.mt5 as forward_mt5

from quasartrend.forward.mt5 import (
    FROZEN_V1_COMMIT, FROZEN_V1_MANIFEST_SHA256, IDENTITY_BLOCKER, ORDER_BLOCKER, DemoExecutionAdapter, Family1LongOnlyShadow, JsonlJournal,
    XMForwardService, audit_capabilities, environment_pseudonym,
    holdout_aggregate_economics,
)
from quasartrend.strategy import Direction, EventType, OpenTrade, StrategyStatus


class Record:
    def __init__(self, **values: object) -> None:
        self.__dict__.update(values)


class IndexOnlyRecord:
    """Faithful MT5-like field indexing without attributes or NumPy."""
    def __init__(self, **values: object) -> None: self.values = values
    def __getitem__(self, field: str) -> object: return self.values[field]


class FakeMT5:
    ACCOUNT_TRADE_MODE_DEMO = 0
    SYMBOL_TRADE_MODE_DISABLED = 0
    COPY_TICKS_ALL = 7
    TIMEFRAME_M1, TIMEFRAME_M15, TIMEFRAME_H4 = 1, 15, 240
    def __init__(self, *, demo: bool = True, server: str = "XMGlobal-MT5 18", connected: bool = True, allowed: bool = True, expert: bool | None = True) -> None:
        self.demo, self.server, self.connected, self.sent = demo, server, connected, 0
        self.allowed, self.expert = allowed, expert; self.tick_rows: list[Record] = []; self.initialize_calls: list[dict[str, object]] = []; self.rate_requests: list[int] = []; self.shutdown_calls = 0
        self.rates: dict[int, list[Record]] = {1: [], 15: [], 240: []}
    def terminal_info(self): return Record(build=1, connected=self.connected, trade_allowed=self.allowed, tradeapi_disabled=False, path="secret-path-not-persisted")
    def account_info(self):
        values = {"server": self.server, "login": 123456, "trade_mode": 0 if self.demo else 2, "trade_allowed": self.allowed, "currency": "USD"}
        if self.expert is not None: values["trade_expert"] = self.expert
        return Record(**values)
    def symbol_info(self, name): return Record(name=name, visible=True, trade_mode=1, order_mode=1, filling_mode=1, volume_min=.01, volume_max=50., volume_step=.01, trade_stops_level=0, trade_freeze_level=0, trade_contract_size=100., digits=2, point=.01, trade_tick_size=.01, trade_tick_value=1., currency_base="XAU", currency_profit="USD", currency_margin="USD", swap_mode=1)
    def copy_ticks_from(self, *_): return self.tick_rows
    def copy_rates_from_pos(self, symbol, timeframe, start, count): self.rate_requests.append(count); return self.rates[timeframe]
    def initialize(self, **kwargs): self.initialize_calls.append(kwargs); self.connected = True; return True
    def shutdown(self): self.shutdown_calls += 1
    def orders_get(self, **_): return ()
    def history_deals_get(self, *_, **__): return ()
    def order_send(self, *_): self.sent += 1; raise AssertionError("must not send")
    def version(self): return (5, 0, 1)


def _proven(mt5: FakeMT5):
    audit = audit_capabilities(mt5, execution_mode="demo")
    assert audit.allowed and audit.environment_id == environment_pseudonym("XMGlobal-MT5 18", 123456)
    assert "login" not in audit.snapshot["account"]
    return audit


def test_capability_audit_is_exact_fail_closed_and_never_orders() -> None:
    mt5 = FakeMT5(); _proven(mt5); assert mt5.sent == 0
    for kwargs in ({"demo": False}, {"server": "XMGlobal-MT5 19"}, {"allowed": False}, {"expert": False}, {"expert": None}):
        audit = audit_capabilities(FakeMT5(**kwargs), execution_mode="demo")
        assert not audit.allowed and audit.blocker == IDENTITY_BLOCKER
    assert not audit_capabilities(FakeMT5(), execution_mode="none").allowed
    missing_mode = FakeMT5(); missing_mode.SYMBOL_TRADE_MODE_DISABLED = None
    assert not audit_capabilities(missing_mode, execution_mode="demo").allowed


def test_numpy_structured_mt5_records_are_indexed_without_attribute_access(tmp_path: Path) -> None:
    numpy = pytest.importorskip("numpy")
    mt5 = FakeMT5()
    mt5.tick_rows = numpy.array([(1_000, 100.0, 100.1, 0.0, 1.0, 1.0, 0)], dtype=[("time_msc", "i8"), ("bid", "f8"), ("ask", "f8"), ("last", "f8"), ("volume", "f8"), ("volume_real", "f8"), ("flags", "i4")])
    service = XMForwardService(tmp_path, mt5=mt5, execution_mode="demo", now=lambda: 2.0)
    service.poll_once()
    assert service.ticks.count == 1


def test_index_only_mt5_record_is_supported_without_numpy(tmp_path: Path) -> None:
    mt5 = FakeMT5()
    mt5.tick_rows = [IndexOnlyRecord(time_msc=1_000, bid=100.0, ask=100.1, last=0.0, volume=1.0, volume_real=1.0, flags=0)]
    service = XMForwardService(tmp_path, mt5=mt5, execution_mode="demo", now=lambda: 2.0)
    service.poll_once()
    assert service.ticks.count == 1


def test_jsonl_restart_conflict_and_execution_ambiguity_do_not_submit(tmp_path: Path) -> None:
    provenance = {"source_server": "XMGlobal-MT5 18", "symbol": "GOLD", "environment_id": "a" * 64, "capture_version": "test"}
    journal = JsonlJournal(tmp_path / "x.jsonl", schema="test/v1", provenance=provenance, time_field="timestamp", id_field="id")
    assert journal.append({"id": "one", "timestamp": 1})
    assert not journal.append({"id": "one", "timestamp": 1})
    with pytest.raises(ValueError, match="conflicting"): journal.append({"id": "one", "timestamp": 2})
    assert JsonlJournal(tmp_path / "x.jsonl", schema="test/v1", provenance=provenance, time_field="timestamp", id_field="id").count == 1
    mt5 = FakeMT5(); execution = JsonlJournal(tmp_path / "e.jsonl", schema="execution/v1", provenance=provenance, time_field="request_timestamp", id_field="execution_id")
    result = DemoExecutionAdapter(mt5, execution, _proven(mt5)).submit("signal", ambiguous_ack=True)
    assert result.blocker == ORDER_BLOCKER and mt5.sent == 0 and execution.count == 1
    assert DemoExecutionAdapter(mt5, execution, _proven(mt5)).submit("signal").blocker == ORDER_BLOCKER
    assert execution.count == 1


def test_family1_shadow_admits_long_rejects_short_and_has_no_submission_path(tmp_path: Path) -> None:
    provenance = {"source_server": "XMGlobal-MT5 18", "symbol": "GOLD", "environment_id": "a" * 64, "capture_version": "test"}
    shadow = Family1LongOnlyShadow(JsonlJournal(tmp_path / "shadow.jsonl", schema="shadow/v1", provenance=provenance, time_field="decision_timestamp", id_field="shadow_id"))
    long = {"signal_id": "long", "signal_timestamp": 1, "direction": "long"}
    short = {"signal_id": "short", "signal_timestamp": 2, "direction": "short"}
    assert shadow.record(long) and shadow.record(short) and not shadow.record(short)
    rows = list(shadow.journal._rows.values())
    assert [row["decision"] for row in rows] == ["admit", "reject"]
    assert not hasattr(shadow, "submit") and not hasattr(shadow, "order_send")


def test_service_finalized_native_bars_restart_and_no_shadow_order(tmp_path: Path) -> None:
    now_ms = 1_800_000
    mt5 = FakeMT5(); mt5.tick_rows = [Record(time_msc=1_000, bid=100., ask=100.1, last=0., volume=1., volume_real=1., flags=0)]
    # The final item in each feed is currently forming and must not be admitted.
    def rate(time_s: int): return Record(time=time_s, open=100., high=101., low=99., close=100., tick_volume=1, spread=10, real_volume=1)
    mt5.rates[1] = [rate(60), rate(1800)]
    mt5.rates[15] = [rate(0), rate(1800)]
    mt5.rates[240] = [rate(0)]
    service = XMForwardService(tmp_path, mt5=mt5, execution_mode="demo", now=lambda: now_ms / 1000)
    assert mt5.initialize_calls == [{}]
    processed = service.poll_once()
    assert [bar.timeframe.value for bar in processed] == ["15m"]
    assert service.m1.count == 1 and service.bars.count == 1
    resumed = XMForwardService(tmp_path, mt5=mt5, execution_mode="demo", now=lambda: now_ms / 1000)
    assert resumed.poll_once() == () and resumed.state == service.state
    assert not hasattr(service.shadow, "submit") and mt5.sent == 0
    assert service.health()["stale_quote"]
    assert mt5.rate_requests and all(count == 600 for count in mt5.rate_requests)


def test_initialize_path_and_failure_are_fail_closed(tmp_path: Path) -> None:
    mt5 = FakeMT5(); XMForwardService(tmp_path, mt5=mt5, terminal_path="C:/MT5/terminal64.exe", execution_mode="demo")
    assert mt5.initialize_calls == [{"path": "C:/MT5/terminal64.exe"}] and mt5.sent == 0
    failed = FakeMT5(); failed.initialize = lambda **_: False
    with pytest.raises(RuntimeError, match="initialization failed"):
        XMForwardService(tmp_path / "failed", mt5=failed)


def test_gap_evidence_is_idempotent_across_overlap_and_restart(tmp_path: Path) -> None:
    now_ms = 300_000
    mt5 = FakeMT5()
    def rate(time_s: int): return Record(time=time_s, open=100., high=101., low=99., close=100., tick_volume=1, spread=10, real_volume=1)
    mt5.rates[1] = [rate(60), rate(180)]  # Missing native M1 open at 120.
    service = XMForwardService(tmp_path, mt5=mt5, now=lambda: now_ms / 1000, execution_mode="demo")
    service.poll_once(); assert service.health()["gaps"] == 1
    service.poll_once(); assert service.health()["gaps"] == 1
    restarted = XMForwardService(tmp_path, mt5=mt5, now=lambda: now_ms / 1000, execution_mode="demo")
    assert restarted.health()["gaps"] == 1


def test_resumed_range_allows_internal_gap_but_requires_persisted_overlap(tmp_path: Path) -> None:
    clock = [1_800_000]
    mt5 = FakeMT5()
    def rate(time_s: int): return Record(time=time_s, open=100., high=101., low=99., close=100., tick_volume=1, spread=10, real_volume=1)
    mt5.rates[15] = [rate(0)]
    service = XMForwardService(tmp_path, mt5=mt5, execution_mode="demo", now=lambda: clock[0] / 1000)
    service.poll_once()
    clock[0] = 2_700_000
    mt5.copy_rates_range = lambda symbol, timeframe, start, end: [rate(0), rate(1800)] if timeframe == mt5.TIMEFRAME_M15 else []
    service.poll_once()
    assert service.gap_journals["m15"].count == 1
    mt5.copy_rates_range = lambda symbol, timeframe, start, end: [rate(2700)] if timeframe == mt5.TIMEFRAME_M15 else []
    clock[0] = 3_600_000
    with pytest.raises(RuntimeError, match="persisted overlap"):
        service.poll_once()


def test_resumed_rates_must_reach_observed_new_tick_but_closed_market_overlap_is_ok(tmp_path: Path) -> None:
    clock = [1_800_000]
    mt5 = FakeMT5()
    def rate(time_s: int): return Record(time=time_s, open=100., high=101., low=99., close=100., tick_volume=1, spread=10, real_volume=1)
    mt5.tick_rows = [Record(time_msc=1_000, bid=100., ask=100.1, last=0., volume=1., volume_real=1., flags=0)]
    mt5.rates[15] = [rate(0)]
    service = XMForwardService(tmp_path, mt5=mt5, execution_mode="demo", now=lambda: clock[0] / 1000)
    service.poll_once()
    # No new native tick: an overlap-only response is valid during a closure.
    clock[0] = 2_700_000
    mt5.copy_rates_range = lambda symbol, timeframe, start, end: [rate(0)] if timeframe == mt5.TIMEFRAME_M15 else []
    service.poll_once()
    # A captured tick in the next M15 proves the response must reach 00:30.
    mt5.tick_rows = [Record(time_msc=2_100_000, bid=100., ask=100.1, last=0., volume=1., volume_real=1., flags=0)]
    clock[0] = 3_600_000
    with pytest.raises(RuntimeError, match="observed tick activity"):
        service.poll_once()


def test_run_uses_bounded_exponential_backoff(tmp_path: Path) -> None:
    pauses: list[float] = []
    service = XMForwardService(tmp_path, mt5=FakeMT5(), sleeper=pauses.append, execution_mode="demo")
    def fail() -> tuple[object, ...]: raise RuntimeError("temporary")
    service.poll_once = fail  # type: ignore[method-assign]
    service.run(lambda: len(pauses) >= 3, interval_seconds=1.0)
    assert pauses == [1.0, 2.0, 4.0]


def test_audit_only_does_not_create_prospective_activation(tmp_path: Path) -> None:
    audit = XMForwardService(tmp_path, mt5=FakeMT5(), activate=False)
    assert not audit.activation_path.exists()
    capture = XMForwardService(tmp_path, mt5=FakeMT5(), now=lambda: 10.0, execution_mode="demo")
    capture.poll_once()
    assert capture.activation_ms == 10_000


def test_stale_catchup_event_is_not_prospective_signal(tmp_path: Path) -> None:
    XMForwardService(tmp_path, mt5=FakeMT5(), now=lambda: 100.0, execution_mode="demo").poll_once()  # Persist activation.
    mt5 = FakeMT5()
    mt5.rates[15] = [Record(time=2700, open=100., high=101., low=99., close=100., tick_volume=1, spread=10, real_volume=1)]
    service = XMForwardService(tmp_path, mt5=mt5, now=lambda: 3700.0, execution_mode="demo")
    trade = OpenTrade("GOLD:1", Direction.LONG, 100., 3_600_000, 1., 99., 1, 3_600_000)
    strategy = replace(service.state.strategy_state, status=StrategyStatus.OPEN_LONG, trade=trade)
    service.state = replace(service.state, strategy_state=strategy)
    next_state = replace(service.state, chronology_cursor=(3_600_000, 1))
    service.engine.step = lambda *_: SimpleNamespace(state=next_state, trace=SimpleNamespace(events=(SimpleNamespace(type=EventType.TRADE_OPENED),)))
    service.poll_once()
    assert service.signals.count == 0 and service.health()["missed_stale_signals"] == 1


def test_journals_bind_frozen_v1_and_implementation_provenance(tmp_path: Path) -> None:
    service = XMForwardService(tmp_path, mt5=FakeMT5(), execution_mode="demo")
    provenance = service.ticks.provenance
    assert provenance["frozen_v1_commit"] == FROZEN_V1_COMMIT
    assert provenance["frozen_v1_manifest_sha256"] == FROZEN_V1_MANIFEST_SHA256
    assert len(provenance["implementation_sha256"]) == 64


def test_tampered_frozen_source_verifier_fails_closed(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(forward_mt5, "verify_frozen_production_sources", lambda _: (_ for _ in ()).throw(ValueError("drift")))
    with pytest.raises(ValueError, match="drift"):
        XMForwardService(tmp_path, mt5=FakeMT5(), execution_mode="demo")


def test_h4_is_processed_before_coincident_m15_without_lookahead(tmp_path: Path) -> None:
    now_ms = 14_400_000
    mt5 = FakeMT5()
    def rate(time_s: int): return Record(time=time_s, open=100., high=101., low=99., close=100., tick_volume=1, spread=10, real_volume=1)
    mt5.rates[15] = [rate(13_500)]  # finalizes at the H4 boundary
    mt5.rates[240] = [rate(0)]
    service = XMForwardService(tmp_path, mt5=mt5, now=lambda: now_ms / 1000, execution_mode="demo")
    assert [bar.timeframe.value for bar in service.poll_once()] == ["4h", "15m"]
    assert service.state.chronology_cursor == (now_ms, 1)


def test_two_poll_boundary_journals_h4_by_finalization_not_open_time(tmp_path: Path) -> None:
    clock = [13_500_000]  # 03:45, so M15 open 03:30 has just finalized.
    mt5 = FakeMT5()
    def rate(time_s: int): return Record(time=time_s, open=100., high=101., low=99., close=100., tick_volume=1, spread=10, real_volume=1)
    mt5.rates[15] = [rate(12_600)]
    mt5.rates[240] = []
    service = XMForwardService(tmp_path, mt5=mt5, now=lambda: clock[0] / 1000, execution_mode="demo")
    assert [bar.timeframe.value for bar in service.poll_once()] == ["15m"]
    clock[0] = 14_400_000
    mt5.rates[15] = [rate(12_600), rate(13_500)]
    mt5.rates[240] = [rate(0)]
    assert [bar.timeframe.value for bar in service.poll_once()] == ["4h", "15m"]
    assert service.state.chronology_cursor == (14_400_000, 1)
    assert service.health()["last_finalized_h4"] == 14_400_000


def test_holdout_aggregates_are_blocked() -> None:
    with pytest.raises(PermissionError, match="holdout"):
        holdout_aggregate_economics(start_ms=1787950680000, end_ms=1803942000000)
    holdout_aggregate_economics(start_ms=1, end_ms=2)
