from __future__ import annotations

from dataclasses import replace
from functools import lru_cache
from pathlib import Path
import subprocess
import sys

import pytest

from quasartrend.research.edge_attribution import (
    build_edge_attribution_report, edge_attribution_json,
    validate_edge_attribution_report, write_edge_attribution_report,
)
from quasartrend.research.pipeline import build_canonical_bundle
from quasartrend.research.regime_features import build_setup_regime_feature_rows, feature_definition_artifact


EXPECTED_ENTRY_CONTEXT_LABELS = {
    "direction": ("LONG", "SHORT"),
    "htf_bias_direction": ("LONG", "SHORT", "missing"),
    "setup_path": ("immediate_opened", "armed_opened"),
    "setup_age": ("same_decision", "one_bar", "two_to_four", "five_plus", "missing_or_invalid"),
    "kalman_transition_age": ("same_decision", "one_bar", "two_to_four", "five_plus", "missing_or_invalid"),
    "utc_six_hour_bucket": ("0", "1", "2", "3", "missing"),
    "utc_weekday": ("0", "1", "2", "3", "4", "5", "6", "missing"),
    "kalman_persistence_bars": ("Q1", "Q2", "Q3", "Q4", "missing"),
    "hema_fast_slope_atr_8": ("Q1", "Q2", "Q3", "Q4", "missing"),
    "directional_efficiency_8": ("Q1", "Q2", "Q3", "Q4", "missing"),
    "directional_efficiency_16": ("Q1", "Q2", "Q3", "Q4", "missing"),
    "directional_efficiency_32": ("Q1", "Q2", "Q3", "Q4", "missing"),
    "hema_flip_count_16": ("Q1", "Q2", "Q3", "Q4", "missing"),
    "kalman_flip_count_16": ("Q1", "Q2", "Q3", "Q4", "missing"),
    "combined_flip_count_16": ("Q1", "Q2", "Q3", "Q4", "missing"),
    "atr_adr_ratio": ("Q1", "Q2", "Q3", "Q4", "missing"),
    "hema_kalman_aligned": ("false", "true", "missing"),
    "adr_extension": ("lt_0.25", "gte_0.25_lt_0.5", "gte_0.5_lte_1", "gt_1", "missing"),
    "atr_extension": ("Q1", "Q2", "Q3", "Q4", "missing"),
    "stop_adr_ratio": ("Q1", "Q2", "Q3", "Q4", "missing"),
    "stop_atr_ratio": ("all_nonmissing", "missing"),
    "atr_at_entry": ("all_nonmissing", "missing"),
    "adr": ("all_nonmissing", "missing"),
}


@lru_cache(maxsize=1)
def _inputs():
    bundle = build_canonical_bundle(golden_15m=Path("tests/golden/tradingview_15m.csv"), golden_4h=Path("tests/golden/tradingview_4h.csv"))
    artifact = feature_definition_artifact(bundle)
    return bundle, artifact, build_setup_regime_feature_rows(bundle)


def test_canonical_population_bridges_tails_and_frozen_windows() -> None:
    bundle, artifact, rows = _inputs()
    report = build_edge_attribution_report(bundle, artifact, rows)
    assert report["population"] == {"observed_setups": 523, "noneligible_setups": 263, "eligible_setups": 260, "opened_trades": 192, "closed_trades": 191, "censored_trades": 1}
    assert report["attribution"]["direction"]["partition"] == "mutually_exclusive_exhaustive"
    assert report["contribution_bridge"]["direction"]["reconciles"]
    assert report["contribution_bridge"]["opened_path"]["opened_reconciles"]
    assert report["contribution_bridge"]["exit_path"]["closed_reconciles"]
    assert all(item["tail_attribution"]["ge_2r"]["count"] >= item["tail_attribution"]["ge_3r"]["count"] >= item["tail_attribution"]["ge_5r"]["count"] for item in report["attribution"]["direction"]["buckets"])
    windows = report["chronology"]["canonical_baseline_windows"]
    assert [(row["role"], row["eligible_setups"], row["closed_trades"]) for row in windows] == [("development", 139, 103), ("validation", 43, 25), ("final_oos", 39, 30)]
    for window in windows:
        assert all(window["frozen_reconciliation"].values())
        for metric in ("total_r", "expectancy_r", "r_per_setup", "profit_factor", "stop_rate", "win_rate"):
            assert window[metric] == pytest.approx(window["frozen_metrics"][metric], abs=1e-12)
    assert report["winner_anatomy"]["groups"][-2]["count"] == 5
    assert report["winner_anatomy"]["groups"][-1]["count"] == 10
    assert report["attribution"]["setup_age"]["population_accounting"]["universe_count"] == 192
    assert report["attribution"]["duration"]["population_accounting"] ["excluded"] == 1
    assert report["raw_numeric_summaries"]["atr_at_entry"]["count"] == 192
    long_ge2 = report["attribution"]["direction"]["buckets"][0]["tail_attribution"]["ge_2r"]
    assert long_ge2["share_of_all_positive_r"] == pytest.approx(.32320970031723056)
    positive_r = report["baseline_economics"]["positive_r"]
    for count in (5, 10):
        memberships = [bucket["tail_attribution"][f"global_top_{count}_winner_membership"] for bucket in report["attribution"]["direction"]["buckets"]]
        assert all(item["denominator_label"] == "all_positive_r" and item["denominator_r"] == positive_r for item in memberships)
        total = sum(item["member_total_r"] for item in memberships)
        assert total == pytest.approx(report["concentration"][f"top_{count}_winners_total_r"], abs=1e-12)
        assert sum(item["share_of_all_positive_r"] for item in memberships) == pytest.approx(total / positive_r, abs=1e-12)


def test_deterministic_json_missing_buckets_overwrite_and_provenance_rejection(tmp_path) -> None:
    bundle, artifact, rows = _inputs()
    report = build_edge_attribution_report(bundle, artifact, rows)
    first = edge_attribution_json(report)
    assert first == edge_attribution_json(report) and first.endswith(b"\n") and b"NaN" not in first and b"Infinity" not in first
    assert any(bucket["label"] == "missing" for bucket in report["attribution"]["duration"]["buckets"])
    output = tmp_path / "x.json"
    write_edge_attribution_report(bundle, artifact, rows, report, output)
    assert output.read_bytes() == first
    with pytest.raises(FileExistsError): write_edge_attribution_report(bundle, artifact, rows, report, output)
    with pytest.raises(ValueError): write_edge_attribution_report(bundle, artifact, rows, {**report, "schema_version": "forged"}, tmp_path / "forged.json")
    validate_edge_attribution_report(bundle, artifact, rows, report)
    with pytest.raises(ValueError): validate_edge_attribution_report(bundle, artifact, rows, {**report, "schema_version": "forged"})
    with pytest.raises(ValueError): build_edge_attribution_report(bundle, replace(artifact, dataset_fingerprint="forged"), rows)


def test_loss_partitions_are_exhaustive_and_negative_shares_reconcile() -> None:
    bundle, artifact, rows = _inputs()
    report = build_edge_attribution_report(bundle, artifact, rows)
    failure = report["failure_modes"]
    assert sum(item["count"] for item in failure["buckets"]) == failure["losses"]
    assert sum(item["negative_r_magnitude_share"] for item in failure["buckets"]) == pytest.approx(1.0, abs=1e-12)
    exits = report["attribution"]["exit_path"]["buckets"]
    assert sum(item["closed_trades"] for item in exits) == 191


def test_dimension_population_accounting_is_authoritative_for_missing_observations() -> None:
    bundle, artifact, rows = _inputs()
    report = build_edge_attribution_report(bundle, artifact, rows)
    atr_adr = next(item for item in report["attribution"]["volatility_range"] if item["dimension"] == "atr_adr_ratio")
    missing_bucket = next(item for item in atr_adr["buckets"] if item["label"] == "missing")
    accounting = atr_adr["population_accounting"]
    assert missing_bucket["eligible_setups"] == 36
    assert "missing" not in missing_bucket
    assert accounting["missing"] == 36
    assert accounting["included"] + accounting["missing"] + accounting["excluded"] == accounting["universe_count"]


def test_failure_and_winner_entry_contexts_use_stable_coarse_labels() -> None:
    bundle, artifact, rows = _inputs()
    report = build_edge_attribution_report(bundle, artifact, rows)

    cohorts = [
        *report["failure_modes"]["buckets"],
        *(item for item in report["winner_anatomy"]["groups"] if item["label"] in {"profitable", "top_5"}),
    ]
    for cohort in cohorts:
        context = cohort["entry_context"]
        assert tuple(context) == tuple(EXPECTED_ENTRY_CONTEXT_LABELS)
        for field, expected_labels in EXPECTED_ENTRY_CONTEXT_LABELS.items():
            assert tuple(context[field]) == expected_labels
            assert set(context[field]) == set(expected_labels)
            assert sum(context[field].values()) == cohort["count"]


def test_chronology_cross_tabs_include_failure_and_winner_diagnostics_with_reconciliation() -> None:
    bundle, artifact, rows = _inputs()
    report = build_edge_attribution_report(bundle, artifact, rows)
    chronology = report["chronology"]
    dimensions = {row["dimension"] for row in chronology["dimension_cross_tabs"]}
    assert {"failure_mode", "winner_group"} <= dimensions
    reconciliation = chronology["cross_tab_reconciliation"]
    assert all(
        item["observation_count_reconciles"]
        and item["closed_trades_reconciles"]
        and item["total_r_reconciles"]
        for item in reconciliation["mutually_exclusive_main_dimensions"]
    )
    assert all(
        item["losing_closed_trades_reconciles"]
        and item["negative_r_magnitude_reconciles"]
        for item in reconciliation["failure_modes"]
    )
    assert reconciliation["winner_groups"] == {
        "overlapping_diagnostic_groups": True,
        "bridge_reconciliation_applicable": False,
        "explicitly_non_bridge": True,
    }


def test_runner_is_deterministic_and_refuses_overwrite(tmp_path) -> None:
    target = tmp_path / "phase73.json"
    command = (sys.executable, "tools/run_phase73_edge_attribution.py", "--output", str(target))
    assert subprocess.run(command, cwd=Path.cwd(), check=False).returncode == 0
    first = target.read_bytes()
    assert subprocess.run(command, cwd=Path.cwd(), check=False).returncode != 0
    other = tmp_path / "other.json"
    assert subprocess.run((sys.executable, "tools/run_phase73_edge_attribution.py", "--output", str(other)), cwd=Path.cwd(), check=False).returncode == 0
    assert first == other.read_bytes()
