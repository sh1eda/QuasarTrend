from __future__ import annotations

import json
from pathlib import Path

import pytest

from quasartrend.replay import HistoricalBar, Timeframe
from quasartrend.research.xm_gold_compatibility import M1_MS, XmM1Bar, _normalize_server_wall_clock, aggregate_m1
import quasartrend.research.xm_gold_historical_validation as validation
from quasartrend.research.xm_gold_historical_validation import (
    CANONICAL_STARTING_SHA,
    EXPECTED_PROTOCOL_SHA256,
    EXPECTED_XM_RAW_SHA256,
    FROZEN_PRODUCTION_SOURCE_MANIFEST_SHA256,
    HISTORICAL_CUTOFF_MS,
    build_xm_gold_historical_validation_protocol,
    expected_protocol_sha256,
    protocol_sha256,
    run_stage_b_guarded,
    verify_canonical_git_provenance,
    verify_compatibility_artifact_identity,
    verify_stage_b_protocol_lock,
    verify_xm_raw_source_identity,
    warmup_allows_strategy_event,
    verify_frozen_production_sources,
    write_xm_gold_historical_validation_protocol,
    xm_gold_historical_validation_protocol_json,
    _economics, _gate, _tail, _chronological, _friction, _segment, _periods, _mfe_diagnostics, _ex_best_period, _run_warmed_replay, xm_gold_historical_validation_json,
)


def test_protocol_is_byte_deterministic_and_contains_predeclared_gate() -> None:
    first = build_xm_gold_historical_validation_protocol()
    second = build_xm_gold_historical_validation_protocol()
    assert xm_gold_historical_validation_protocol_json(first) == xm_gold_historical_validation_protocol_json(second)
    assert xm_gold_historical_validation_protocol_json(first).endswith(b"\n")
    assert protocol_sha256(first) == expected_protocol_sha256()
    assert expected_protocol_sha256() == EXPECTED_PROTOCOL_SHA256
    assert first["historical_validation_boundary"]["end_exclusive_epoch_ms"] == HISTORICAL_CUTOFF_MS
    assert first["canonical_starting_state"]["sha"] == CANONICAL_STARTING_SHA
    assert first["raw_source"]["sha256"] == EXPECTED_XM_RAW_SHA256
    assert first["strategy_freeze_identity"]["production_source_manifest_sha256"] == FROZEN_PRODUCTION_SOURCE_MANIFEST_SHA256
    assert first["metrics"]["closed_trade_population_minimum"] == 250
    assert first["friction"]["cost_levels_r"] == [0.00, 0.02, 0.05, 0.10, 0.15, 0.20, 0.25, 0.30]
    locked = Path("exports/xm/phase_xm_gold_historical_validation_protocol.json")
    assert locked.is_file()
    assert __import__("hashlib").sha256(locked.read_bytes()).hexdigest() == EXPECTED_PROTOCOL_SHA256


def test_frozen_production_source_manifest_reconciles_without_market_access() -> None:
    actual = verify_frozen_production_sources(Path("."))
    assert len(actual) == 22
    assert "src/quasartrend/indicators/golden.py" in actual
    assert "src/quasartrend/backtest/metrics.py" in actual
    assert "references/pinescript/hema_trend.pine" in actual


def test_actual_canonical_git_raw_and_compatibility_identities_without_strategy_evaluation() -> None:
    provenance = verify_canonical_git_provenance(Path("."))
    assert provenance["head"] == CANONICAL_STARTING_SHA
    assert provenance["branch"] == "main"
    assert provenance["local_main"] == CANONICAL_STARTING_SHA
    assert provenance["origin_main"] == CANONICAL_STARTING_SHA
    assert provenance["tag_type"] == "tag"
    assert provenance["tag_target"] == CANONICAL_STARTING_SHA
    assert provenance["tracked_index_clean"] is True
    assert provenance["tracked_worktree_clean"] is True
    raw = verify_xm_raw_source_identity(Path("exports/xm/XM_GOLD_M1_raw.csv"))
    assert raw == {"sha256": EXPECTED_XM_RAW_SHA256, "row_count": 1_000_000}
    assert verify_compatibility_artifact_identity(Path("exports/xm/phase_xm_gold_compatibility.json"))


def test_protocol_declares_complete_metric_conventions_without_evaluating_market_history() -> None:
    protocol = build_xm_gold_historical_validation_protocol()
    definitions = protocol["metric_definitions"]
    assert {"closed_trade", "r", "expectancy_r", "profit_factor", "profit_factor_no_loss_convention", "win_rate", "stop_rate", "positive_r", "negative_r_magnitude", "median_r"} <= set(definitions)
    assert definitions["r"] == "closed_trade.net_pnl / (abs(entry_price - stop_price) * quantity), using frozen accounting"
    assert "serialize unavailable/null" in definitions["profit_factor_no_loss_convention"]
    assert "infinite PF and passes PF thresholds" in definitions["profit_factor_no_loss_convention"]
    assert protocol["decompositions"]["direction"]["classification"] == ["both_positive", "long_only_edge", "short_only_edge", "neither"]
    assert protocol["decompositions"]["setup_path"]["segments"] == ["immediate_open", "armed_then_opened"]
    assert protocol["decompositions"]["temporal"]["quarter_concentration_denominator"].startswith("sum of positive")
    assert protocol["metrics"]["leave_one_year_or_partial_year_out"]["otherwise"].startswith("report insufficient")
    assert protocol["tail_dependence"]["removal"].startswith("sequentially remove top 1, 3, 5, and 10")
    assert protocol["stop_giveback"]["missing_path_policy"].startswith("a post-entry path crossing")
    assert protocol["path_risk_conventions"]["rolling"].endswith("earliest window")
    assert protocol["friction"]["segment_breakevens"] == ["long", "short", "immediate_open", "armed_then_opened"]
    assert protocol["compatibility_period_reference"]["primary_population_merge"] == "prohibited"
    assert protocol["classification"]["precedence"][0].startswith("FAIL")
    assert "do not downgrade a negative result" in protocol["classification"]["precedence"][0]
    assert protocol["classification"]["precedence"][1].startswith("INCONCLUSIVE")


def test_protocol_lock_is_immutable_and_stage_b_binds_exact_hash(tmp_path: Path) -> None:
    path = tmp_path / "protocol.json"
    protocol = build_xm_gold_historical_validation_protocol()
    locked = write_xm_gold_historical_validation_protocol(protocol, path)
    assert locked == expected_protocol_sha256()
    assert verify_stage_b_protocol_lock(path, locked) == protocol
    with pytest.raises(FileExistsError, match="immutable protocol lock"):
        write_xm_gold_historical_validation_protocol(protocol, path)
    with pytest.raises(ValueError, match="unknown protocol hash"):
        verify_stage_b_protocol_lock(path, "0" * 64)


def test_modified_protocol_fails_closed_before_any_stage_b_evaluator_runs(tmp_path: Path) -> None:
    path = tmp_path / "protocol.json"
    protocol = build_xm_gold_historical_validation_protocol()
    write_xm_gold_historical_validation_protocol(protocol, path)
    modified = json.loads(path.read_bytes())
    modified["metrics"]["closed_trade_population_minimum"] = 249
    path.write_bytes(xm_gold_historical_validation_protocol_json(modified))
    invoked = False

    def evaluator(_protocol: object) -> None:
        nonlocal invoked
        invoked = True

    with pytest.raises(ValueError, match="protocol hash mismatch|protocol bytes differ"):
        run_stage_b_guarded(
            path, expected_protocol_sha256(), evaluator,
            repo_root=Path("."), xm_m1_source=Path("exports/xm/XM_GOLD_M1_raw.csv"),
            compatibility_artifact=Path("exports/xm/phase_xm_gold_compatibility.json"),
        )
    assert invoked is False


@pytest.mark.parametrize(
    "verifier_name",
    [
        "verify_canonical_git_provenance", "verify_frozen_production_sources",
        "verify_xm_raw_source_identity", "verify_compatibility_artifact_identity",
    ],
)
def test_stage_b_guard_never_invokes_evaluator_when_any_required_identity_fails(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, verifier_name: str,
) -> None:
    path = tmp_path / "protocol.json"
    write_xm_gold_historical_validation_protocol(build_xm_gold_historical_validation_protocol(), path)
    invoked = False

    def failing_identity(*_args: object, **_kwargs: object) -> None:
        raise ValueError("synthetic identity mismatch")

    def evaluator(_protocol: object) -> None:
        nonlocal invoked
        invoked = True

    monkeypatch.setattr(validation, verifier_name, failing_identity)
    with pytest.raises(ValueError, match="synthetic identity mismatch"):
        run_stage_b_guarded(
            path, expected_protocol_sha256(), evaluator,
            repo_root=Path("."), xm_m1_source=Path("exports/xm/XM_GOLD_M1_raw.csv"),
            compatibility_artifact=Path("exports/xm/phase_xm_gold_compatibility.json"),
        )
    assert invoked is False


def test_historical_cutoff_and_warmup_gate_exclude_early_or_compatibility_inputs() -> None:
    assert warmup_allows_strategy_event(
        timestamp_ms=HISTORICAL_CUTOFF_MS - 1,
        observed_h4_bars=600,
        observed_ltf_bars=600,
        h4_indicator_state_valid=True,
        ltf_indicator_state_valid=True,
    )
    assert not warmup_allows_strategy_event(
        timestamp_ms=HISTORICAL_CUTOFF_MS - 1,
        observed_h4_bars=599,
        observed_ltf_bars=600,
        h4_indicator_state_valid=True,
        ltf_indicator_state_valid=True,
    )
    assert not warmup_allows_strategy_event(
        timestamp_ms=HISTORICAL_CUTOFF_MS - 1,
        observed_h4_bars=600,
        observed_ltf_bars=599,
        h4_indicator_state_valid=True,
        ltf_indicator_state_valid=True,
    )
    assert not warmup_allows_strategy_event(
        timestamp_ms=HISTORICAL_CUTOFF_MS - 1,
        observed_h4_bars=600,
        observed_ltf_bars=600,
        h4_indicator_state_valid=False,
        ltf_indicator_state_valid=True,
    )
    with pytest.raises(ValueError, match="compatibility-period"):
        warmup_allows_strategy_event(
            timestamp_ms=HISTORICAL_CUTOFF_MS,
            observed_h4_bars=600,
            observed_ltf_bars=600,
            h4_indicator_state_valid=True,
            ltf_indicator_state_valid=True,
        )


def test_timezone_and_aggregation_contracts_are_exercised_only_with_synthetic_bars() -> None:
    # 2026-03-02T00:00 server wall-clock is winter UTC+2; the aggregation uses
    # server buckets before normalizing that bucket to UTC.
    server_start = 1_772_409_600_000
    rows = [
        XmM1Bar(server_start + index * M1_MS, 100.0, 101.0, 99.0, 100.5, 1.0, 0.0, 0.0)
        for index in range(15) if index != 7
    ]
    aggregated = aggregate_m1(rows, timeframe=Timeframe.MINUTES_15)
    assert len(aggregated) == 1
    assert aggregated[0].source_count == 14
    assert aggregated[0].complete is False
    assert aggregated[0].source_contiguous is False
    assert aggregated[0].bar.open_time == _normalize_server_wall_clock(server_start)


def _row(identifier: str, r: float | None, *, entry: int = 1, exit_: int = 2, direction: str = "long", path: str = "immediate_open", stop: bool = False) -> dict[str, object]:
    return {"trade_id": identifier, "outcome": "closed" if r is not None else "censored", "r": r, "entry_timestamp": entry, "exit_timestamp": exit_, "direction": direction, "path": path, "stop_hit": stop}


def test_stage_b_synthetic_economics_tail_chronology_and_friction_conventions() -> None:
    rows = [_row("a", 3, entry=1_700_000_000_000, exit_=10), _row("b", -1, entry=1_700_000_000_001, exit_=20, direction="short", path="armed_then_opened", stop=True), _row("c", 1, entry=1_700_000_000_002, exit_=30)]
    economics = _economics(rows, eligible_setups=4)
    assert economics["total_r"] == 3 and economics["expectancy_r"] == pytest.approx(1)
    assert economics["profit_factor"] == 4 and economics["r_per_setup"] == pytest.approx(.75)
    no_loss = _economics([_row("w", 1)])
    assert no_loss["profit_factor"] is None and no_loss["profit_factor_no_loss_positive_return"] is True
    tail = _tail(rows)
    assert tail["top_1"]["trade_ids"] == ["a"] and tail["top_1"]["removal"]["total_r"] == 0
    chronology = _chronological(rows)
    assert chronology["maximum_drawdown_r"] == 1 and chronology["longest_losing_streak"] == 1
    friction = _friction(rows)
    assert friction["levels"]["0.10"]["total_r"] == pytest.approx(2.7)
    assert friction["segment_breakeven_effective_cost_r"]["short"] == -1


def test_stage_b_segments_temporal_loo_and_chronological_ties_are_deterministic() -> None:
    rows = [_row("a", 3, entry=1_700_000_000_000, exit_=10), _row("b", -2, entry=1_700_000_000_001, exit_=20, direction="short", path="armed_then_opened"), _row("c", -2, entry=1_700_000_000_002, exit_=30), _row("d", 6, entry=1_700_000_000_003, exit_=40)]
    setups = [{"direction": "long", "path": "rejected"}, {"direction": "long", "path": "immediate_open"}, {"direction": "short", "path": "armed_then_opened"}]
    direction = _segment(rows, setups, "direction", ("long", "short"))
    assert direction["long"]["eligible_setups"] == 1 and direction["long"]["2r_plus"] == 2
    assert direction["short"]["eligible_setups"] == 1 and direction["edge_classification"] == "long_only_edge"
    paths = _segment(rows, setups, "path", ("immediate_open", "armed_then_opened"))
    assert paths["immediate_open"]["setups"] == 1 and paths["armed_then_opened"]["trades"] == 1
    periods = _periods(rows, "quarter")
    assert len(periods) == 1
    chronology = _chronological(rows)
    assert chronology["maximum_drawdown_r"] == 4 and chronology["peak_trade_index"] == 1
    assert chronology["trough_trade_index"] == 3 and chronology["recovery_trade_index"] == 4
    tie_rows = [_row(str(index), 1, exit_=index) for index in range(1, 22)]
    assert _chronological(tie_rows)["rolling"]["20"]["best_start_trade_ordinal"] == 1


def test_tail_removal_and_gate_failure_causes_are_explicit() -> None:
    rows = [_row(str(index), float(11 - index), exit_=index) for index in range(1, 7)] + [_row("loss", -1, exit_=99)]
    tail = _tail(rows)
    assert tail["top_1"]["removal"]["closed_trades"] == len(rows) - 1
    assert tail["top_3"]["removal"]["closed_trades"] == len(rows) - 3
    assert tail["top_5"]["removal"]["closed_trades"] == len(rows) - 5
    assert tail["top_10"]["removal"]["closed_trades"] == 1
    assert tail["single_trade_dependence_failure"] is False


def test_mfe_and_ex_best_helpers_and_result_json_are_deterministic() -> None:
    stopped = [dict(_row("a", -1, stop=True), mfe_r=2.0, strict_pre_exit_mfe_r=1.0), dict(_row("b", -1, stop=True), mfe_r=.3, strict_pre_exit_mfe_r=.3), dict(_row("gap", -1, stop=True), mfe_r=None, strict_pre_exit_mfe_r=None)]
    mfe = _mfe_diagnostics(stopped)
    assert mfe["full_exit_bar_mfe"]["missing_path_count"] == 1
    assert mfe["full_exit_bar_mfe"]["distribution_buckets"]["2_to_<3"] == 1
    assert mfe["full_vs_strict_difference"]["aggregate_full_minus_strict_r"] == 1
    rows = [_row("jan", 2, entry=1_704_067_200_000), _row("apr", 2, entry=1_711_929_600_000), _row("may", -1, entry=1_714_608_000_000)]
    quarters = _periods(rows, "quarter")
    ex_best = _ex_best_period(rows, quarters, "quarter")
    assert ex_best["best_period"] == "2024-Q1" and ex_best["friction"]["0.10"]["closed_trades"] == 2
    payload = {"schema_version": "synthetic", "mfe": mfe, "ex_best": ex_best}
    assert xm_gold_historical_validation_json(payload) == xm_gold_historical_validation_json(payload)


def test_warmup_replay_suppresses_all_pre_eligibility_strategy_transitions() -> None:
    htf = tuple(HistoricalBar("GOLD", Timeframe.HOURS_4, index * 14_400_000, 100 + index, 101 + index, 99 + index, 100 + index) for index in range(600))
    first_ltf = 600 * 14_400_000
    ltf = tuple(HistoricalBar("GOLD", Timeframe.MINUTES_15, first_ltf + index * 900_000, 100 + index, 101 + index, 99 + index, 100 + index) for index in range(600))
    replay, warmup = _run_warmed_replay(ltf, htf)
    assert warmup["first_strategy_eligible_timestamp"] == ltf[-1].finalized_at
    assert len(replay.traces) == 1
    assert replay.traces[0].source_bar.finalized_at < HISTORICAL_CUTOFF_MS


def test_stage_b_classifier_synthetic_pass_conditional_inconclusive_and_fail() -> None:
    def report(*, trades: int, total: float, expectancy: float, pf: float, single_fail: bool = False) -> dict[str, object]:
        e = {"closed_trades": trades, "total_r": total, "expectancy_r": expectancy, "profit_factor": pf, "profit_factor_no_loss_positive_return": False}
        q = {str(index): {"total_r": 1.0} for index in range(4)}
        loo = {label: {"total_r": 1.0, "expectancy_r": .2, "profit_factor": 1.2, "profit_factor_no_loss_positive_return": False} for label in q}
        passed = {"total_r": 1.0, "expectancy_r": .2, "profit_factor": 1.2, "profit_factor_no_loss_positive_return": False}
        friction = {"total_r": 1.0, "expectancy_r": .1, "profit_factor": 1.1, "profit_factor_no_loss_positive_return": False}
        return {"aggregate": e, "temporal": {"quarters": q, "leave_one_quarter_out": loo}, "tail": {"single_trade_dependence_failure": single_fail}, "ex_best_period": {"quarter": passed}, "friction": {"levels": {"0.10": friction}}}
    assert _gate(report(trades=250, total=10, expectancy=.2, pf=1.2))["quantitative_provisional_decision"] == "PASS"
    assert _gate(report(trades=250, total=10, expectancy=.05, pf=1.2))["quantitative_provisional_decision"] == "CONDITIONAL"
    assert _gate(report(trades=249, total=10, expectancy=.2, pf=1.2))["quantitative_provisional_decision"] == "INCONCLUSIVE"
    assert _gate(report(trades=249, total=-1, expectancy=-.1, pf=.9))["quantitative_provisional_decision"] == "FAIL"


@pytest.mark.parametrize("mutation", ["pf", "quarters", "loo", "ex_best", "friction"])
def test_each_quantitative_robustness_gate_can_make_an_otherwise_positive_result_conditional(mutation: str) -> None:
    base = {"closed_trades": 250, "total_r": 10.0, "expectancy_r": .2, "profit_factor": 1.2, "profit_factor_no_loss_positive_return": False}
    passed = {"total_r": 1.0, "expectancy_r": .2, "profit_factor": 1.2, "profit_factor_no_loss_positive_return": False}
    quarters = {str(index): {"total_r": 1.0} for index in range(4)}
    loo = {str(index): dict(passed) for index in range(4)}
    report = {"aggregate": base, "temporal": {"quarters": quarters, "leave_one_quarter_out": loo}, "tail": {"single_trade_dependence_failure": False}, "ex_best_period": {"quarter": dict(passed)}, "friction": {"levels": {"0.10": dict(passed)}}}
    if mutation == "pf": report["aggregate"]["profit_factor"] = 1.1
    elif mutation == "quarters": report["temporal"]["quarters"] = {"a": {"total_r": 1.0}, "b": {"total_r": -1.0}, "c": {"total_r": -1.0}, "d": {"total_r": -1.0}}
    elif mutation == "loo":
        report["temporal"]["leave_one_quarter_out"]["0"] = {**passed, "total_r": -1.0}
        report["temporal"]["leave_one_quarter_out"]["1"] = {**passed, "total_r": -1.0}
    elif mutation == "ex_best": report["ex_best_period"]["quarter"] = {**passed, "total_r": -1.0}
    else: report["friction"]["levels"]["0.10"] = {**passed, "total_r": -1.0}
    assert _gate(report)["quantitative_provisional_decision"] == "CONDITIONAL"
