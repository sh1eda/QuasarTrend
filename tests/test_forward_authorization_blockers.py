"""Synthetic blocker regressions; these are not Windows/XM observations.

Gap shapes exercise plausible closure durations without certifying a calendar.
No fixture may promote empty history or MT5 success status to proof of closure.
"""
from __future__ import annotations

import os
from pathlib import Path
import subprocess
import sys

import pytest

from test_forward_capture_integrity import (
    H4, M15, START, SeriesMT5, evidence, service_factory,
)
from test_forward_mt5 import PRODUCTION_CAPTURE_DEFAULT, Record
from quasartrend.forward.capture import CaptureBlocked, historical
from quasartrend.replay import ReplayEngine


def test_empty_ticks_with_complete_rates_do_not_prove_tick_completeness(service_factory):
    """Characterize diagnostic-only tick absence behind the disabled gate.

    Complete native candle coverage allows the internal strategy replay to
    advance. This is not proof that the empty tick response was exhaustive,
    nor does the implementation classify the absence as a session closure.
    """
    assert PRODUCTION_CAPTURE_DEFAULT is False
    api = SeriesMT5()
    service = service_factory(api=api)
    assert len(service.poll_once()) == 1200
    api.now_ms += M15
    assert len(service.poll_once()) == 1
    assert service.ticks.count == 0
    assert service.machine.pending_gaps == []
    assert service.gaps.count == 0
    assert service.health()["stale_quote"] is True
    assert service.inputs.rows[-1]["ticks"] == []
    assert service.inputs.rows[-1]["requests"]["ticks"]["pages"] == [
        {"cursor_ms": api.now_ms - 60_000, "returned": 0, "status": [1, "Success"]},
    ]
    assert service.state.chronology_cursor == (START + M15, 1)
    assert api.sent == 0


@pytest.mark.parametrize("stream", ["ticks", "m1", "m15", "h4"])
@pytest.mark.parametrize("failure", ["timeout_with_payload", "missing_payload"])
def test_history_failure_after_bootstrap_is_atomic_across_restart(
    service_factory, stream, failure,
):
    api = SeriesMT5()
    api.tick_rows = [Record(time_msc=START - 1, bid=100., ask=100.1)]
    service = service_factory(api=api)
    service.poll_once()
    before = evidence(service)
    api.now_ms += H4
    api.tick_rows.append(Record(time_msc=START + 1, bid=100., ask=100.1))
    original_ticks, original_rates = api.copy_ticks_from, api.copy_rates_range
    status = [1, "Success"]
    api.last_error = lambda: tuple(status)

    def response(name, rows):
        status[:] = [1, "Success"]
        if name == stream:
            if failure == "missing_payload":
                return None
            assert rows, "timeout regression requires a nonempty partial payload"
            status[:] = [-10005, "synthetic synchronization timeout"]
        return rows

    api.copy_ticks_from = lambda *args: response("ticks", original_ticks(*args))
    api.copy_rates_range = lambda symbol, tf, start, end: response(
        {1: "m1", 15: "m15", 240: "h4"}[tf],
        original_rates(symbol, tf, start, end),
    )
    # Repeated incomplete acquisition must not append even the successful
    # earlier streams, move the cursor, or change recursive indicator state.
    for _ in range(2):
        with pytest.raises(RuntimeError, match="history result/status"):
            service.poll_once()
        assert evidence(service) == before
    service.close()
    api.copy_ticks_from, api.copy_rates_range = original_ticks, original_rates
    status[:] = [1, "Success"]
    resumed = service_factory(api=api)
    assert evidence(resumed) == before
    assert len(resumed.poll_once()) == 17
    assert resumed.state == ReplayEngine().run(
        [historical(row) for row in resumed.bars.rows]
    ).state
    assert resumed.poll_once() == ()
    assert resumed.bars.count == 1217 and api.sent == 0


@pytest.mark.parametrize("tf", [15, 240])
def test_successful_partial_warmup_pins_origin_and_retries_after_restart(service_factory, tf):
    api = SeriesMT5()
    original = api.copy_rates_from_pos

    def partial(symbol, timeframe, start, count):
        rows = original(symbol, timeframe, start, count)
        return rows[-201:] if timeframe == tf else rows

    api.copy_rates_from_pos = partial
    service = service_factory(api=api)
    with pytest.raises(CaptureBlocked, match="incomplete_finalized_warmup"):
        service.poll_once()
    assert service.activation_ms == START
    assert service.bars.count == service.signals.count == 0
    service.close()
    api.now_ms += H4
    resumed = service_factory(api=api)
    with pytest.raises(CaptureBlocked, match="incomplete_finalized_warmup"):
        resumed.poll_once()
    assert resumed.activation_ms == START and resumed.state.chronology_cursor is None
    api.copy_rates_from_pos = original
    resumed.poll_once()
    assert resumed.activation_ms == START and resumed.bars.count == 1217
    assert resumed.state == ReplayEngine().run(
        [historical(row) for row in resumed.bars.rows]
    ).state
    assert not resumed.machine.pending_gaps and api.sent == 0


@pytest.mark.parametrize("closure_slots", [1, 12], ids=["daily-shaped", "weekend-shaped"])
@pytest.mark.parametrize("mixed_activity", [False, True], ids=["empty", "mixed-active"])
def test_calendar_shaped_absence_stays_blocked_until_native_candles_fill(
    service_factory, closure_slots, mixed_activity,
):
    api = SeriesMT5()
    # Preserve a pre-gap tick cursor so resumed retrieval includes the gap.
    api.tick_rows = [Record(time_msc=START - 1, bid=100., ask=100.1)]
    service = service_factory(api=api)
    service.poll_once()
    prior = service.state
    end = START + closure_slots * H4
    for tf, duration in ((1, 60_000), (15, M15), (240, H4)):
        api.omit[tf].update(range(START, end, duration))
    if mixed_activity:
        api.tick_rows.append(Record(time_msc=START + 1, bid=100., ask=100.1))
    api.now_ms = end + H4
    for _ in range(2):
        with pytest.raises(CaptureBlocked):
            service.poll_once()
        reasons = {gap["reason"] for gap in service.machine.pending_gaps}
        assert "unresolved_candle_or_session_closure" in reasons
        assert ("activity_proven_missing_candle" in reasons) == mixed_activity
        assert service.state == prior and service.bars.count == 1200
    blocked = evidence(service)
    service.close()
    resumed = service_factory(api=api)
    assert evidence(resumed) == blocked
    # A successful empty response and restart do not certify inactivity.
    with pytest.raises(CaptureBlocked):
        resumed.poll_once()
    assert resumed.state == prior
    for missing in api.omit.values():
        missing.clear()
    resumed.poll_once()
    expected_count = 1200 + 17 * (closure_slots + 1)
    assert resumed.bars.count == expected_count
    assert not resumed.machine.pending_gaps
    assert resumed.state == ReplayEngine().run(
        [historical(row) for row in resumed.bars.rows]
    ).state
    projection_counts = {name: journal.count for name, journal in resumed.projections.items()}
    for _ in range(2):
        assert resumed.poll_once() == ()
    assert {name: journal.count for name, journal in resumed.projections.items()} == projection_counts
    assert api.sent == 0


def test_failed_reconnect_preserves_evidence_then_requires_same_identity(service_factory):
    api = SeriesMT5()
    service = service_factory(api=api)
    service.poll_once()
    before = evidence(service)
    api.now_ms += H4
    api.connected = False
    original = api.initialize
    api.initialize = lambda **kwargs: False
    with pytest.raises(RuntimeError, match="reconnect failed"):
        service.poll_once()
    assert evidence(service) == before
    api.initialize = original
    api.login += 1
    with pytest.raises(PermissionError, match="environment changed"):
        service.poll_once()
    assert evidence(service) == before
    api.login -= 1
    assert len(service.poll_once()) == 17
    assert service.state == ReplayEngine().run(
        [historical(row) for row in service.bars.rows]
    ).state
    assert service.reconnects == 2 and api.sent == 0


@pytest.mark.parametrize("stream,point", [
    ("inputs", "during_append"),
    ("inputs", "after_fsync"),
    ("bars", "after_fsync"),
    ("checkpoint", "before_checkpoint_replace"),
    ("checkpoint", "after_checkpoint_replace"),
])
def test_abrupt_process_exit_reconstructs_exact_committed_evidence(
    service_factory, tmp_path, stream, point,
):
    """Actually exit a separate process: no exception unwind/service.close.

    This validates process loss on the executing local filesystem. It does not
    emulate host power loss, Windows when run elsewhere, or native MT5 recovery.
    """
    baseline_api = SeriesMT5()
    baseline = service_factory("baseline", baseline_api)
    baseline.poll_once()
    baseline_api.now_ms += H4
    baseline.poll_once()
    expected = evidence(baseline)
    child = r'''
import os
from pathlib import Path
import sys
from test_forward_capture_integrity import H4, SeriesMT5
import quasartrend.forward.mt5 as forward

forward.FORWARD_CAPTURE_INTEGRITY_AUTHORIZED = True
root, stream, point = Path(sys.argv[1]), sys.argv[2], sys.argv[3]
api = SeriesMT5()
service = forward.XMForwardService(root, mt5=api, now=lambda: api.now_ms / 1000)
service.poll_once()
api.now_ms += H4

def fault(actual, path):
    target = "checkpoints" if stream == "checkpoint" else stream
    if actual == point and target in path.parts:
        os._exit(73)

service.fault = service.inputs.fault = fault
for journal in service.projections.values():
    journal.fault = fault
service.poll_once()
raise AssertionError("requested fault boundary was not reached")
'''
    repo = Path(__file__).resolve().parents[1]
    env = os.environ.copy()
    env["PYTHONPATH"] = os.pathsep.join([
        str(repo / "tests"), str(repo / "src"), env.get("PYTHONPATH", ""),
    ])
    result = subprocess.run(
        [sys.executable, "-c", child, str(tmp_path / "crashed"), stream, point],
        cwd=repo, env=env, capture_output=True, text=True, timeout=30,
    )
    assert result.returncode == 73, result.stderr
    api = SeriesMT5()
    api.now_ms += H4
    resumed = service_factory("crashed", api)
    if resumed.inputs.count == 1:
        assert (stream, point) == ("inputs", "during_append")
        resumed.poll_once()
    assert evidence(resumed) == expected
    resumed.close()
    again = service_factory("crashed", api)
    assert evidence(again) == expected
    assert api.sent == 0
