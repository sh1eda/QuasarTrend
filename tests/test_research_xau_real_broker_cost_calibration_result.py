from __future__ import annotations

from hashlib import sha256
from pathlib import Path

import pytest

import quasartrend.research.xau_real_broker_cost_calibration_result as result


def _ledger() -> list[dict[str, object]]:
    return [
        {"entry_timestamp": 120_000, "exit_timestamp": 180_000, "r": 1.0},
        {"entry_timestamp": 240_000, "exit_timestamp": 300_000, "r": -0.5},
    ]


def test_type7_percentiles_and_point_conversion_are_deterministic() -> None:
    assert result.percentile_type7([26, 32, 37, 42], 50) == 34.5
    assert result.spread_distribution_points([26, 32, 37, 42]) == {"count": 4, "min": 26.0, "mean": 34.25, "median": 34.5, "p75": 38.25, "p90": 40.5, "p95": 41.25, "p99": 41.85, "max": 42.0}
    converted = result.points_distribution_with_price([26, 32])
    assert converted["points"]["median"] == 29.0
    assert converted["price"]["median"] == .29
    assert converted["price"]["count"] == 2
    assert converted["point"] == .01


def test_mqlrates_spread_points_reject_fractional_values() -> None:
    result.validate_integral_spread_points(26.0)
    with pytest.raises(ValueError, match="nonnegative integer"):
        result.validate_integral_spread_points(26.5)


def test_prior_minute_lookup_prohibits_same_timestamp_and_preserves_missing(monkeypatch: pytest.MonkeyPatch) -> None:
    bars = {60_000: 26.0, 120_000: 99.0, 180_000: 32.0, 240_000: 37.0}
    assert result.prior_minute_bar_minimum(bars, 120_000) == 26.0
    assert result.prior_minute_bar_minimum(bars, 300_000) == 37.0
    assert result.prior_minute_bar_minimum(bars, 360_000) is None
    monkeypatch.setattr(result, "EXPECTED_ENDPOINT_MATCHED", 4)
    monkeypatch.setattr(result, "EXPECTED_ENDPOINT_MISSING", {"entry": [], "exit": []})
    audit = result.endpoint_audit(_ledger(), bars)
    assert audit["matched"] == 4
    assert audit["missing"] == 0
    assert audit["combined_previous_bar_minimum"]["points"]["max"] == 99.0


def test_endpoint_audit_fails_closed_when_frozen_expected_population_changes(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(result, "EXPECTED_ENDPOINT_MATCHED", 3)
    with pytest.raises(ValueError, match="endpoint no-lookahead audit mismatch"):
        result.endpoint_audit(_ledger(), {60_000: 26.0, 120_000: 32.0, 180_000: 37.0, 240_000: 42.0})


def test_r_helpers_use_initial_price_risk_and_gross_to_net_identity() -> None:
    assert result.cost_r_from_price_distance(.25, 2000.0, 1995.0) == .05
    assert result.net_r_identity(1.2, .05) == 1.15
    with pytest.raises(ValueError, match="nonzero"):
        result.cost_r_from_price_distance(.25, 2000.0, 2000.0)


def test_incomplete_scenarios_are_immutable_and_waiting() -> None:
    scenario = result.unavailable_scenario("S1_observed_core")
    assert scenario["complete"] is False
    assert scenario["aggregate"]["net_total_r"] is None
    assert scenario["cost_components_r"]["spread"] is None
    assert scenario["long_short"]["LONG"]["available"] is False
    assert "cannot substitute" in scenario["unavailable_reason"]


def test_protocol_guard_runs_before_any_input_opening(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    calls: list[str] = []
    guard = {"protocol_commit_sha": "14f54535a2b4bce9ac849a1669495238fe2fd280"}
    monkeypatch.setattr(result, "verify_committed_protocol_before_inputs", lambda _root, _path: calls.append("guard") or guard)
    monkeypatch.setattr(result, "required_hashes", lambda _root: calls.append("hashes") or {})
    monkeypatch.setattr(result, "_closed_ledger_from_historical_result", lambda _path: calls.append("ledger") or _ledger())
    monkeypatch.setattr(result, "frozen_s0_control", lambda _ledger: calls.append("s0") or {"gross_total_r": 2.0})
    monkeypatch.setattr(result, "stream_pre_cutoff_m1_bar_minimum_spreads", lambda _path: calls.append("raw") or ({60_000: 26.0}, [26.0], 1))
    monkeypatch.setattr(result, "endpoint_audit", lambda _ledger, _bars: calls.append("endpoint") or {})
    monkeypatch.setattr(result, "build_waiting_result", lambda **_kwargs: calls.append("result") or {"ok": True})
    assert result.build_result_guarded(tmp_path, tmp_path / "protocol.json") == {"ok": True}
    assert calls == ["guard", "hashes", "ledger", "s0", "raw", "endpoint", "result"]


def test_actual_committed_protocol_guard_binds_full_commit_and_canonical_provenance() -> None:
    verified = result.verify_committed_protocol_before_inputs(Path("."), Path(result.PROTOCOL_PATH))
    assert verified["protocol_commit_sha"] == "14f54535a2b4bce9ac849a1669495238fe2fd280"
    assert verified["protocol_sha256"] == "acd1a6573018adc5819c4f66e418c3f75f8c0e93b99dd41a864141e56106e837"
    assert verified["protocol_parent_sha"] == "af02995705506ab1629f558e6fbdabe13d2d0785"
    assert verified["origin_main_sha"] == "af02995705506ab1629f558e6fbdabe13d2d0785"
    assert verified["waiting_tag_object"] == "a95c26c7cb5458df48bb49bad5791b4e22cba972"
    assert verified["waiting_tag_peeled_target"] == "af02995705506ab1629f558e6fbdabe13d2d0785"


def test_waiting_result_is_deterministic_and_never_has_s1_to_s3_net_metrics() -> None:
    s0 = {"gross_total_r": 70.72001507737389}
    value = result.build_waiting_result(protocol_guard={"protocol_commit_sha": "14f"}, hashes={}, raw_distribution={}, endpoint={}, s0=s0)
    assert value["classification"] == "XAU REAL BROKER COST CALIBRATION: WAITING FOR BROKER COST DATA"
    assert value["scenarios"]["S2_realistic_execution"]["aggregate"]["net_expectancy_r"] is None
    payload = result.result_json(value)
    assert payload == result.result_json(value)
    assert sha256(payload).hexdigest() == result.result_sha256(value)


def test_generated_frozen_result_is_byte_deterministic_and_pinned() -> None:
    payload = Path(result.RESULT_PATH).read_bytes()
    assert sha256(payload).hexdigest() == result.EXPECTED_RESULT_SHA256
