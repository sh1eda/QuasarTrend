"""The diagnostic collector must not confuse synthetic evidence with authority."""
from __future__ import annotations

import importlib.util
import json
from pathlib import Path
import subprocess
import sys

import pytest

from test_forward_mt5 import FakeMT5


@pytest.fixture
def collector():
    path = Path(__file__).resolve().parents[1] / "tools/validate_xm_forward_recovery.py"
    spec = importlib.util.spec_from_file_location("recovery_evidence_test", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_existing_output_is_never_overwritten(collector, tmp_path):
    marker = tmp_path / "evidence.json"
    marker.write_bytes(b"preserve")
    with pytest.raises(FileExistsError):
        collector.main(["--output", str(tmp_path)])
    assert marker.read_bytes() == b"preserve"


@pytest.mark.parametrize("count,returncode,extra", [
    (262, 0, ""), (263, 1, ""), (263, 0, "<skipped/>"), (263, 0, "<failure/>"),
])
def test_missing_skipped_or_failed_selection_cannot_be_complete(collector, tmp_path, count, returncode, extra):
    path = tmp_path / "junit.xml"
    path.write_text("<testsuite>" + "<testcase/>" * (count - 1) + f"<testcase>{extra}</testcase></testsuite>")
    assert collector.junit_result(path, returncode)["selection_complete"] is False


def test_wrong_imported_tree_fails_even_with_valid_frozen_manifest(collector, monkeypatch, tmp_path):
    import quasartrend.forward.mt5 as forward
    monkeypatch.setattr(forward, "FORWARD_CAPTURE_INTEGRITY_AUTHORIZED", False)
    monkeypatch.setattr(forward, "verify_frozen_production_sources", lambda repo: {str(i): "a" * 64 for i in range(22)})
    with pytest.raises(ValueError):
        collector.source_identity(tmp_path)


def test_enabled_capture_gate_prevents_evidence_acceptance(collector, monkeypatch):
    import quasartrend.forward.mt5 as forward
    monkeypatch.setattr(forward, "FORWARD_CAPTURE_INTEGRITY_AUTHORIZED", True)
    with pytest.raises(ValueError, match="disabled capture gate"):
        collector.source_identity(Path(__file__).resolve().parents[1])


@pytest.mark.parametrize("changed", [False, True])
def test_source_postcheck_runs_after_timeout_and_invalidates_evidence(collector, monkeypatch, tmp_path, changed):
    calls = []

    def source(repo):
        calls.append(True)
        return {"source": "changed" if changed and len(calls) > 1 else "original"}

    def timeout(*args, **kwargs):
        raise subprocess.TimeoutExpired("synthetic pytest", 30)

    monkeypatch.setattr(collector, "source_identity", source)
    monkeypatch.setattr(collector.subprocess, "run", timeout)
    output = tmp_path / "new"
    assert collector.main(["--output", str(output)]) == 2
    report = json.loads((output / "evidence.json").read_text())
    assert len(calls) == 2
    assert report["source_unchanged"] is (not changed)
    assert report["failure_type"] == "TimeoutExpired"
    assert report["authorization"] == "BLOCKED"
    assert report["native_history_synchronization"] == "NOT_OBSERVED"


def test_optional_terminal_audit_is_metadata_only_and_redacts_identity(collector, monkeypatch, tmp_path):
    import quasartrend.forward.mt5 as forward
    monkeypatch.setattr(forward, "FORWARD_CAPTURE_INTEGRITY_AUTHORIZED", False)
    terminal = tmp_path / "terminal64.exe"
    terminal.write_bytes(b"synthetic executable identity")
    api = FakeMT5(server="XMGlobal-MT5 9")
    original = api.terminal_info

    def terminal_info():
        result = original()
        result.path = str(tmp_path)
        return result

    def forbidden(*args, **kwargs):
        raise AssertionError("metadata audit must not call history or order APIs")

    api.terminal_info = terminal_info
    for name in ("copy_ticks_from", "copy_rates_range", "copy_rates_from_pos", "order_send", "orders_get", "history_deals_get"):
        setattr(api, name, forbidden)
    monkeypatch.setitem(sys.modules, "MetaTrader5", api)
    monkeypatch.setattr(collector.platform, "system", lambda: "Windows")
    result = collector.terminal_audit(terminal)
    assert result["target_server9_demo_proven"] is True
    assert result["capture_allowed"] is result["execution_allowed"] is False
    assert api.initialize_calls == [{"path": str(terminal.resolve()), "timeout": 15_000}]
    assert api.shutdown_calls == 1
    encoded = json.dumps(result)
    assert "123456" not in encoded and "login" not in encoded and str(tmp_path) not in encoded
