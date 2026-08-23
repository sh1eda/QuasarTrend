"""Development-only Phase 7 experiment discipline and canonical results."""
from __future__ import annotations

from dataclasses import replace
from pathlib import Path

import pytest

from quasartrend.research.experiments import (
    ExperimentConfig,
    _accounted_r,
    _adr_fixed_buckets,
    _delay_outcome,
    _development_population,
    _excursion_r,
    _stop_fill,
    development_report,
    experiment_json,
    write_experiment_report,
)
from quasartrend.research.pipeline import build_canonical_bundle
from quasartrend.replay import HistoricalBar, Timeframe
from quasartrend.strategy import Direction


@pytest.fixture(scope="module")
def report():
    bundle = build_canonical_bundle(
        golden_15m=Path("tests/golden/tradingview_15m.csv"),
        golden_4h=Path("tests/golden/tradingview_4h.csv"),
    )
    return development_report(bundle)


def test_experiment_matrix_is_fixed() -> None:
    with pytest.raises(ValueError, match="delays"):
        ExperimentConfig(delays=(0, 1, 2))
    with pytest.raises(ValueError, match="ADR edges"):
        ExperimentConfig(adr_fixed_edges=(0.5,))
    with pytest.raises(ValueError, match="inclusive"):
        ExperimentConfig(quantile_method="exclusive")


def test_report_is_development_only_and_population_is_exact(report) -> None:
    assert (report.role, report.start_date, report.end_date) == (
        "development", "2026-05-15", "2026-07-09"
    )
    assert report.inaccessible_roles == ("validation", "final_oos")
    assert report.eligible_baseline_setups == 139
    assert report.included_closed_baseline_trades == 103
    assert "DEVELOPMENT ONLY" in report.caveat


def test_delay_zero_is_exact_frozen_development_baseline(report) -> None:
    baseline = report.baseline_metrics
    delay_zero = report.delay_experiments[0]
    assert delay_zero.hypothesis_id == "PAIRED_BASELINE_EXIT_CLOCK_N0"
    assert delay_zero.metrics == baseline
    assert delay_zero.entries_taken == 103
    assert delay_zero.baseline_setups_without_open == 36
    assert len(delay_zero.retained_trade_ids) == len(set(delay_zero.retained_trade_ids)) == 103


def test_delay_counterfactuals_report_opportunity_loss(report) -> None:
    assert tuple(item.delay_bars for item in report.delay_experiments) == (0, 1, 2, 4)
    assert tuple(item.entries_taken for item in report.delay_experiments) == (103, 91, 74, 53)
    for item in report.delay_experiments[1:]:
        assert item.baseline_trades_skipped == 103 - item.entries_taken
        assert item.metrics.trade_retention == pytest.approx(item.entries_taken / 103)
        assert item.metrics.opportunity_r_per_setup == pytest.approx(
            item.metrics.total_r / 139
        )
        assert "research-only" in item.semantics


def test_every_feature_bucket_reports_counts_retention_and_economics(report) -> None:
    assert len(report.feature_experiments) == 11
    for experiment in report.feature_experiments:
        total = sum(bucket.metrics.sample_count for bucket in experiment.buckets)
        total += experiment.missing.metrics.sample_count
        assert total == 103
        for bucket in experiment.buckets + (experiment.missing,):
            assert bucket.metrics.linked_setup_retention == pytest.approx(
                bucket.metrics.sample_count / 139
            )
            assert bucket.metrics.trade_retention == pytest.approx(
                bucket.metrics.sample_count / 103
            )
            assert bucket.metrics.opportunity_r_per_setup == pytest.approx(
                bucket.metrics.total_r / 139
            )


def test_adr_fixed_boundary_assignment_is_exact(report) -> None:
    original = report.feature_experiments[0]
    # Reuse valid accounting rows from a fresh canonical bundle and change only
    # the entry-time feature under test.
    bundle = build_canonical_bundle(
        golden_15m=Path("tests/golden/tradingview_15m.csv"),
        golden_4h=Path("tests/golden/tradingview_4h.csv"),
    )
    closed = tuple(row for row in bundle.dataset.trade_rows if row.outcome_state == "closed")
    rows = tuple(
        replace(closed[index], trade_id=f"boundary:{index}", adr_extension=value)
        for index, value in enumerate((0.249, 0.25, 0.50, 1.00, 1.001))
    )
    result = _adr_fixed_buckets(rows, 5, original.definition)
    assert tuple(item.metrics.sample_count for item in result.buckets) == (1, 1, 2, 1)
    assert tuple(item.label for item in result.buckets) == (
        "<0.25", "[0.25,0.50)", "[0.50,1.00]", ">1.00"
    )


def test_experiment_report_is_byte_deterministic_and_fail_closed(report, tmp_path) -> None:
    first = experiment_json(report)
    assert first == experiment_json(report)
    assert b'"role":"development"' in first
    assert b'"role":"validation"' not in first
    path = tmp_path / "development.json"
    write_experiment_report(report, path)
    assert path.read_bytes() == first
    with pytest.raises(FileExistsError):
        write_experiment_report(report, path)


def test_local_generated_development_artifact_is_fresh_when_present(report) -> None:
    artifact = Path("exports/phase7/phase7_development_experiments.json")
    if artifact.exists():
        assert artifact.read_bytes() == experiment_json(report)


def test_delayed_stop_gap_priority_matches_frozen_semantics() -> None:
    normal = HistoricalBar("BTCUSDT", Timeframe.MINUTES_15, 0, 101, 103, 98, 100)
    long_gap = replace(normal, open=97, high=99, low=96, close=98)
    short_gap = replace(normal, open=103, high=104, low=102, close=103)
    assert _stop_fill(Direction.LONG, 99, normal) == 99
    assert _stop_fill(Direction.LONG, 99, long_gap) == 97
    assert _stop_fill(Direction.SHORT, 102, short_gap) == 103
    assert _stop_fill(Direction.LONG, 95, normal) is None


def test_delayed_accounting_includes_directional_slippage_and_fees() -> None:
    assert _accounted_r(
        side=Direction.LONG, entry=100, exit=110, risk=10,
        quantity=2, fee_bps=100, slippage_bps=100,
    ) == pytest.approx(.5801)
    assert _accounted_r(
        side=Direction.SHORT, entry=100, exit=90, risk=10,
        quantity=2, fee_bps=100, slippage_bps=100,
    ) == pytest.approx(.6201)


def test_delayed_excursions_clear_both_values_on_a_gap() -> None:
    bars = (HistoricalBar("BTCUSDT", Timeframe.MINUTES_15, 0, 100, 104, 97, 101),)
    assert _excursion_r(
        side=Direction.LONG, entry=100, risk=2, bars=bars, contiguous=True
    ) == (1.5, 2.0)
    assert _excursion_r(
        side=Direction.LONG, entry=100, risk=2, bars=bars, contiguous=False
    ) == (None, None)


def test_delayed_no_stop_uses_original_baseline_exit_clock_and_price() -> None:
    bundle = build_canonical_bundle(
        golden_15m=Path("tests/golden/tradingview_15m.csv"),
        golden_4h=Path("tests/golden/tradingview_4h.csv"),
    )
    _, rows = _development_population(bundle)
    traces = tuple(
        trace for trace in bundle.replay.traces
        if trace.source_bar.timeframe is Timeframe.MINUTES_15
    )
    indexes = {trace.source_bar.finalized_at: index for index, trace in enumerate(traces)}
    fallbacks = []
    for row in rows:
        outcome, _ = _delay_outcome(
            row=row, delay=1, traces=traces, indexes=indexes, bundle=bundle
        )
        if outcome is not None and not outcome.stop_hit:
            fallbacks.append((row, outcome))
    assert fallbacks
    for row, outcome in fallbacks:
        assert outcome.exit_mode == "baseline_exit_clock_and_price"
        assert outcome.exit_timestamp == row.exit_timestamp
        assert outcome.canonical_exit_price == row.canonical_exit_price
