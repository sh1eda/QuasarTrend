"""Fake-native observations only; never contacts a terminal or broker."""
from datetime import UTC, datetime
import importlib.util
import json
from pathlib import Path

import numpy as np
import pytest
from test_forward_mt5 import FakeMT5

SPEC = importlib.util.spec_from_file_location("history_probe", Path(__file__).resolve().parents[1] / "tools/probe_xm_forward_history.py")
probe = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(probe)
AS_OF = datetime(2026, 9, 9, 12, tzinfo=UTC)


class Native(FakeMT5):
    def __init__(self, terminal, *, mode="rows", code=1, switch=False, **kwargs):
        super().__init__(server="XMGlobal-MT5 9", **kwargs)
        self.terminal = terminal
        self.mode, self.code, self.switch = mode, code, switch
        self.calls = []
        self.pending_status = False

    def terminal_info(self):
        assert not self.pending_status, "history status must be immediate"
        value = super().terminal_info()
        value.path = str(self.terminal.parent)
        return value

    def last_error(self):
        self.pending_status = False
        return (self.code if self.calls else 1, "SECRET-API-DESCRIPTION")

    def obtain(self, kind, symbol, start, end, selector):
        assert not self.pending_status
        self.calls.append((kind, symbol, start, end, selector))
        if self.switch:
            self.login += 1
        if self.mode == "exception":
            raise RuntimeError("SECRET-API-DESCRIPTION")
        self.pending_status = True
        if self.mode == "none":
            return None
        return np.array([] if self.mode == "empty" else [(int(start.timestamp()), 2000.0)], dtype=[("time", "<i8"), ("bid", "<f8")])

    def copy_rates_range(self, symbol, timeframe, start, end):
        return self.obtain("rates", symbol, start, end, timeframe)

    def copy_ticks_range(self, symbol, start, end, flags):
        return self.obtain("ticks", symbol, start, end, flags)


def run(tmp_path, **kwargs):
    terminal = tmp_path / "terminal64.exe"
    api = Native(terminal, **kwargs)
    result = probe.probe(api, terminal, tmp_path, AS_OF)
    assert api.shutdown_calls == 1
    encoded = (tmp_path / "evidence.json").read_text()
    assert json.loads(encoded) == json.loads(probe.canonical(result))
    assert "SECRET-API-DESCRIPTION" not in encoded
    assert '"login"' not in encoded
    assert result["execution"] is False and result["capture"] is False
    assert result["closure_certificate"] is False and result["completeness"] == "UNRESOLVED"
    return api, result


def test_exact_six_utc_requests_and_completed_weekend():
    plans = probe.requests(AS_OF)
    assert len(plans) == 6
    assert probe.requests(datetime(2026, 9, 9, 12, 29, 59, tzinfo=UTC))[0]["end_utc"] == "2026-09-09T12:15:00+00:00"
    assert [p["timeframe"] for p in plans[:4]] == ["M15", "H4", "M15", "H4"]
    assert plans[0]["start_utc"] == "2026-09-09T00:00:00+00:00"
    assert plans[2]["start_utc"] == "2026-09-05T00:00:00+00:00"
    assert plans[2]["end_utc"] == "2026-09-07T00:00:00+00:00"
    assert plans[5]["start_utc"] == "2026-09-05T12:00:00+00:00"
    assert plans[5]["end_utc"] == "2026-09-05T12:15:00+00:00"
    assert probe.requests(datetime(2026, 9, 6, 23, tzinfo=UTC))[2]["start_utc"] == "2026-08-29T00:00:00+00:00"
    with pytest.raises(ValueError):
        probe.requests(datetime(2026, 9, 9))


@pytest.mark.parametrize("mode,code,returned,acquisition", [("rows", 1, "RETURNED", "OBSERVED_API_SUCCESS"), ("rows", -4, "RETURNED", "UNRESOLVED"), ("empty", 1, "EMPTY", "OBSERVED_API_SUCCESS"), ("empty", -4, "EMPTY", "UNRESOLVED"), ("none", 1, "NONE", "UNRESOLVED"), ("none", -4, "NONE", "UNRESOLVED")])
def test_return_shape_and_api_status_are_independent(tmp_path, mode, code, returned, acquisition):
    api, result = run(tmp_path, mode=mode, code=code)
    assert len(api.calls) == 6
    assert result["identity_stable"] is True
    for item in result["observations"]:
        assert item["returned"] == returned
        assert item["status_code"] == code
        assert item["acquisition"] == acquisition
        if mode != "none":
            assert len(item["normalized_payload_sha256"]) == 64
            assert item["count"] == (0 if mode == "empty" else 1)
    assert result["status"] == ("SIX_REQUESTS_OBSERVED" if acquisition == "OBSERVED_API_SUCCESS" else "UNRESOLVED")


def test_changed_account_stops_after_one_call_preserves_payload(tmp_path):
    api, result = run(tmp_path, switch=True)
    assert len(api.calls) == 1
    assert result["identity_stable"] is False
    assert result["status"] == "UNRESOLVED"
    assert result["observations"][0]["count"] == 1


def test_live_account_blocks_history(tmp_path):
    api, result = run(tmp_path, demo=False)
    assert not api.calls
    assert result["before"]["target_server9_demo_proven"] is False
    assert result["status"] == "UNRESOLVED"


def test_enabled_capture_gate_blocks_even_initialization(tmp_path, monkeypatch):
    monkeypatch.setattr(probe.forward, "FORWARD_CAPTURE_INTEGRITY_AUTHORIZED", True)
    api, result = run(tmp_path)
    assert not api.calls and not api.initialize_calls
    assert result["status"] == "UNRESOLVED"


def test_api_exception_redacted_and_results_preserved(tmp_path):
    api, result = run(tmp_path, mode="exception")
    assert len(api.calls) == 6
    assert result["status"] == "UNRESOLVED"
    assert all(item["failure_type"] == "RuntimeError" and item["returned"] == "NOT_OBSERVED" for item in result["observations"])


def test_payload_hash_and_timestamp_bounds():
    rows = np.array([(2, 2.5), (1, 1.5)], dtype=[("time", "<i8"), ("bid", "<f8")])
    result = probe.payload(rows)
    assert result["timestamp_bounds"]["time"] == {"minimum": 1, "maximum": 2}
    assert result["normalized_payload_sha256"] == probe.sha256(probe.canonical([{"time": 2, "bid": 2.5}, {"time": 1, "bid": 1.5}])).hexdigest()
