from __future__ import annotations

import ast
from dataclasses import replace
import importlib.util
import json
from pathlib import Path
import sys
from types import SimpleNamespace

import pytest
import quasartrend.forward.mt5 as forward_mt5

PRODUCTION_CAPTURE_DEFAULT = forward_mt5.FORWARD_CAPTURE_INTEGRITY_AUTHORIZED

from quasartrend.forward.mt5 import (
    BROKER_POLICY_VERSION, CAPTURE_INTEGRITY_BLOCKER, FROZEN_V1_COMMIT, FROZEN_V1_MANIFEST_SHA256, IDENTITY_BLOCKER, ORDER_BLOCKER, DemoExecutionAdapter, Family1LongOnlyShadow, JsonlJournal,
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
    def __init__(self, *, demo: bool = True, server: str = "XMGlobal-MT5 18", company: str = "XM Global Limited", connected: object = True, allowed: bool = True, account_allowed: bool | None = None, terminal_allowed: bool | None = None, expert: bool | None = True, tradeapi_disabled: bool = False, visible: bool = True, symbol_name: str | None = None, login: int | None = 123456, account_overrides: dict[str, object] | None = None, spec_overrides: dict[str, object] | None = None) -> None:
        self.demo, self.server, self.connected, self.sent = demo, server, connected, 0
        self.company, self.account_allowed = company, allowed if account_allowed is None else account_allowed
        self.terminal_allowed, self.expert = allowed if terminal_allowed is None else terminal_allowed, expert
        self.tradeapi_disabled, self.visible, self.symbol_name, self.login = tradeapi_disabled, visible, symbol_name, login
        self.account_overrides, self.spec_overrides = account_overrides or {}, spec_overrides or {}
        self.tick_rows: list[Record] = []; self.initialize_calls: list[dict[str, object]] = []; self.rate_requests: list[int] = []; self.shutdown_calls = 0
        self.rates: dict[int, list[Record]] = {1: [], 15: [], 240: []}
    def terminal_info(self): return Record(build=1, connected=self.connected, trade_allowed=self.terminal_allowed, tradeapi_disabled=self.tradeapi_disabled, path="secret-path-not-persisted")
    def account_info(self):
        values = {"server": self.server, "login": self.login, "trade_mode": 0 if self.demo else 2, "trade_allowed": self.account_allowed, "currency": "USD", "company": self.company}
        if self.expert is not None: values["trade_expert"] = self.expert
        values.update(self.account_overrides)
        return Record(**values)
    def symbol_info(self, name):
        values = {"name": self.symbol_name or name, "visible": self.visible, "trade_mode": 1, "order_mode": 1, "filling_mode": 1, "volume_min": .01, "volume_max": 50., "volume_step": .01, "trade_stops_level": 0, "trade_freeze_level": 0, "trade_contract_size": 100., "digits": 2, "point": .01, "trade_tick_size": .01, "trade_tick_value": 1., "trade_tick_value_profit": 1., "trade_tick_value_loss": 1., "currency_base": "USD", "currency_profit": "USD", "currency_margin": "USD", "swap_mode": 1, "swap_long": -96.61, "swap_short": 13.11, "swap_rollover3days": 3, "bank": "XM", "description": "GOLD"}
        values.update(self.spec_overrides)
        return Record(**values)
    def copy_ticks_from(self, symbol, start, count, flags):
        return [row for row in self.tick_rows if forward_mt5._value(row, "time_msc") >= int(start.timestamp() * 1000)][:count]
    def copy_rates_range(self, symbol, timeframe, start, end):
        return [row for row in self.rates[timeframe] if start.timestamp() <= forward_mt5._value(row, "time") <= end.timestamp()]
    def copy_rates_from_pos(self, symbol, timeframe, start, count): self.rate_requests.append(count); return self.rates[timeframe]
    def initialize(self, **kwargs): self.initialize_calls.append(kwargs); self.connected = True; return True
    def shutdown(self): self.shutdown_calls += 1
    def orders_get(self, **_): return ()
    def history_deals_get(self, *_, **__): return ()
    def order_send(self, *_): self.sent += 1; raise AssertionError("must not send")
    def version(self): return (5, 0, 1)
    def last_error(self): return (1, 'Success')


@pytest.fixture(autouse=True)
def _authorize_capture_only_for_runtime_unit_tests(monkeypatch: pytest.MonkeyPatch) -> None:
    """Exercise guarded runtime internals; source-closure authorization is separate."""
    monkeypatch.setattr(forward_mt5, "FORWARD_CAPTURE_INTEGRITY_AUTHORIZED", True)


def _proven(mt5: FakeMT5):
    audit = audit_capabilities(mt5, execution_mode="demo")
    assert audit.audit_allowed and audit.environment_id == environment_pseudonym(mt5.server, 123456)
    assert "login" not in audit.snapshot["account"]
    return audit


def test_capability_audit_accepts_only_explicit_demo_broker_server_and_gold_policy() -> None:
    for server in ("XMGlobal-MT5 9", "XMGlobal-MT5 18"):
        mt5 = FakeMT5(server=server)
        audit = _proven(mt5)
        assert audit.demo_proven and audit.broker_identity_proven and audit.server_authorized and audit.symbol_compatible
        assert audit.capture_capability_proven and audit.capture_allowed and audit.execution_permissions_proven
        assert not audit.execution_allowed and audit.execution_blocker == ORDER_BLOCKER and mt5.sent == 0
        serialized = json.dumps(audit.snapshot, sort_keys=True)
        assert "123456" not in serialized and "login" not in audit.snapshot["account"]

    audit_only = audit_capabilities(FakeMT5(server="XMGlobal-MT5 9"))
    assert audit_only.audit_allowed and audit_only.execution_permissions_proven
    assert audit_only.execution_permission_blocker is None
    assert not audit_only.execution_allowed and audit_only.execution_blocker == ORDER_BLOCKER

    cases = (
        ({"demo": False}, "ACCOUNT TRADE MODE IS NOT DEMO"),
        ({"company": "Wrong Broker"}, "BROKER COMPANY NOT AUTHORIZED"),
        ({"server": "XMGlobal-MT5 19"}, "SERVER NOT EXPLICITLY AUTHORIZED"),
        ({"symbol_name": "SILVER"}, "REQUIRED SYMBOL GOLD NOT AVAILABLE"),
        ({"connected": "False"}, "TERMINAL DISCONNECTED"),
        ({"account_overrides": {"trade_mode": False}}, "ACCOUNT TRADE MODE IS NOT DEMO"),
        ({"spec_overrides": {"trade_contract_size": 1.0}}, "GOLD CONTRACT SIZE MISMATCH"),
        ({"spec_overrides": {"digits": 2.0}}, "GOLD DIGITS MISMATCH"),
        ({"spec_overrides": {"point": 0.1}}, "GOLD POINT MISMATCH"),
        ({"spec_overrides": {"trade_tick_size": 0.1}}, "GOLD TICK SIZE MISMATCH"),
        ({"spec_overrides": {"volume_min": -0.01}}, "GOLD VOLUME MIN INVALID OR MISMATCHED"),
        ({"spec_overrides": {"volume_step": 0.0}}, "GOLD VOLUME STEP INVALID OR MISMATCHED"),
        ({"spec_overrides": {"swap_mode": True}}, "GOLD SWAP MODE IS NOT POINTS"),
        ({"spec_overrides": {"swap_rollover3days": 3.0}}, "GOLD TRIPLE-SWAP DAY IS NOT WEDNESDAY"),
    )
    for kwargs, message in cases:
        audit = audit_capabilities(FakeMT5(**kwargs), execution_mode="demo")
        assert not audit.audit_allowed and audit.audit_blocker and message in audit.audit_blocker


def test_capture_capability_is_independent_of_execution_permissions() -> None:
    for kwargs, message in (
        ({"account_allowed": False}, "ACCOUNT TRADING NOT ALLOWED"),
        ({"terminal_allowed": False}, "TERMINAL TRADING NOT ALLOWED"),
        ({"expert": False}, "EXPERT TRADING NOT ALLOWED"),
        ({"expert": None}, "EXPERT TRADING NOT ALLOWED"),
        ({"tradeapi_disabled": True}, "PYTHON TRADING API DISABLED"),
    ):
        audit = audit_capabilities(FakeMT5(**kwargs), execution_mode="demo")
        assert audit.audit_allowed and audit.capture_capability_proven and audit.capture_allowed
        assert not audit.execution_permissions_proven and not audit.execution_allowed
        assert audit.execution_permission_blocker and message in audit.execution_permission_blocker
        assert audit.execution_blocker == ORDER_BLOCKER

    disconnected = audit_capabilities(FakeMT5(connected=False), execution_mode="demo")
    assert not disconnected.audit_allowed and disconnected.audit_blocker == "XM DEMO AUDIT: BLOCKED — TERMINAL DISCONNECTED"


def test_account_identity_and_production_capture_gate_fail_closed(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    assert PRODUCTION_CAPTURE_DEFAULT is False
    missing = audit_capabilities(FakeMT5(login=None), execution_mode="demo")
    assert not missing.account_identity_proven and missing.audit_blocker == IDENTITY_BLOCKER
    monkeypatch.setattr(forward_mt5, "FORWARD_CAPTURE_INTEGRITY_AUTHORIZED", False)
    audit = audit_capabilities(FakeMT5(server="XMGlobal-MT5 9"))
    assert audit.audit_allowed and not audit.capture_allowed and audit.capture_blocker == CAPTURE_INTEGRITY_BLOCKER
    with pytest.raises(PermissionError, match="REQUIRED HISTORY CLOSURES"):
        XMForwardService(tmp_path, mt5=FakeMT5(server="XMGlobal-MT5 9"))


def test_cli_audit_exit_status_uses_audit_permission_not_capture_or_execution(monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    runner_path = Path(__file__).parents[1] / "tools" / "run_xm_v1_forward.py"
    spec = importlib.util.spec_from_file_location("test_run_xm_v1_forward", runner_path)
    assert spec and spec.loader
    runner = importlib.util.module_from_spec(spec); spec.loader.exec_module(runner)
    mt5 = FakeMT5(server="XMGlobal-MT5 9")
    audit = replace(audit_capabilities(mt5), capture_allowed=False, capture_blocker=CAPTURE_INTEGRITY_BLOCKER)
    monkeypatch.setattr(runner, "XMForwardService", lambda *_args, **_kwargs: SimpleNamespace(audit=audit, mt5=mt5, close=mt5.shutdown))
    monkeypatch.setattr(sys, "argv", [str(runner_path), "--root", str(tmp_path), "--mode", "audit"])
    assert runner.main() == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["status"] == "PASS"
    assert payload["permissions"]["audit_allowed"] is True
    assert payload["permissions"]["capture_allowed"] is False
    assert payload["permissions"]["execution_allowed"] is False

    blocked = audit_capabilities(FakeMT5(company="Wrong Broker"))
    monkeypatch.setattr(runner, "XMForwardService", lambda *_args, **_kwargs: SimpleNamespace(audit=blocked, mt5=mt5, close=mt5.shutdown))
    assert runner.main() == 2
    payload = json.loads(capsys.readouterr().out)
    assert payload["status"] == "BLOCKED" and "BROKER COMPANY" in payload["permissions"]["audit_blocker"]


def test_numpy_structured_mt5_records_are_indexed_without_attribute_access(tmp_path: Path) -> None:
    numpy = pytest.importorskip("numpy")
    mt5 = FakeMT5()
    mt5.tick_rows = numpy.array([(1_000, 100.0, 100.1, 0.0, 1.0, 1.0, 0)], dtype=[("time_msc", "i8"), ("bid", "f8"), ("ask", "f8"), ("last", "f8"), ("volume", "f8"), ("volume_real", "f8"), ("flags", "i4")])
    service = XMForwardService(tmp_path, mt5=mt5, execution_mode="demo", now=lambda: 2.0)
    with pytest.raises(forward_mt5.CaptureBlocked, match="incomplete_finalized_warmup"):
        service.poll_once()
    assert service.ticks.count == 1
    service.close()


def test_index_only_mt5_record_is_supported_without_numpy(tmp_path: Path) -> None:
    mt5 = FakeMT5()
    mt5.tick_rows = [IndexOnlyRecord(time_msc=1_000, bid=100.0, ask=100.1, last=0.0, volume=1.0, volume_real=1.0, flags=0)]
    service = XMForwardService(tmp_path, mt5=mt5, execution_mode="demo", now=lambda: 2.0)
    with pytest.raises(forward_mt5.CaptureBlocked, match="incomplete_finalized_warmup"):
        service.poll_once()
    assert service.ticks.count == 1
    service.close()


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
    tree = ast.parse(Path(forward_mt5.__file__).read_text(encoding="utf-8"))
    assert not any(isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute) and node.func.attr == "order_send" for node in ast.walk(tree))


def test_initialize_path_and_failure_are_fail_closed(tmp_path: Path) -> None:
    mt5 = FakeMT5(); XMForwardService(tmp_path, mt5=mt5, terminal_path="C:/MT5/terminal64.exe", execution_mode="demo")
    assert mt5.initialize_calls == [{"path": "C:/MT5/terminal64.exe"}] and mt5.sent == 0
    failed = FakeMT5(); failed.initialize = lambda **_: False
    with pytest.raises(RuntimeError, match="initialization failed"):
        XMForwardService(tmp_path / "failed", mt5=failed)


def test_run_uses_bounded_exponential_backoff(tmp_path: Path) -> None:
    pauses: list[float] = []
    service = XMForwardService(tmp_path, mt5=FakeMT5(), sleeper=pauses.append, execution_mode="demo")
    def fail() -> tuple[object, ...]: raise RuntimeError("temporary")
    service.poll_once = fail  # type: ignore[method-assign]
    service.run(lambda: len(pauses) >= 3, interval_seconds=1.0)
    assert pauses == [1.0, 2.0, 4.0]


def test_journals_bind_frozen_v1_and_implementation_provenance(tmp_path: Path) -> None:
    service = XMForwardService(tmp_path, mt5=FakeMT5(server="XMGlobal-MT5 9"), execution_mode="demo")
    provenance = service.ticks.provenance
    assert provenance["source_server"] == "XMGlobal-MT5 9"
    assert provenance["broker_policy_version"] == BROKER_POLICY_VERSION
    assert provenance["frozen_v1_commit"] == FROZEN_V1_COMMIT
    assert provenance["frozen_v1_manifest_sha256"] == FROZEN_V1_MANIFEST_SHA256
    assert len(provenance["implementation_sha256"]) == 64


def test_tampered_frozen_source_verifier_fails_closed(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(forward_mt5, "verify_frozen_production_sources", lambda _: (_ for _ in ()).throw(ValueError("drift")))
    with pytest.raises(ValueError, match="drift"):
        XMForwardService(tmp_path, mt5=FakeMT5(), execution_mode="demo")


def test_holdout_aggregates_are_blocked() -> None:
    with pytest.raises(PermissionError, match="holdout"):
        holdout_aggregate_economics(start_ms=1787950680000, end_ms=1803942000000)
    holdout_aggregate_economics(start_ms=1, end_ms=2)
