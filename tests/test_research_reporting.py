from __future__ import annotations

import json
import os
from pathlib import Path
import subprocess
import sys
from dataclasses import replace

import pytest

from quasartrend.replay import HistoricalBar, Timeframe
from quasartrend.research.pipeline import (
    _merge_streams,
    _window_report,
    baseline_report,
    build_canonical_bundle,
    report_json,
)
from quasartrend.research.models import ResearchConfig, SplitConfig


GOLDEN_15M = Path("tests/golden/tradingview_15m.csv")
GOLDEN_4H = Path("tests/golden/tradingview_4h.csv")


@pytest.fixture(scope="module")
def reporting_bundle():
    if not GOLDEN_15M.exists() or not GOLDEN_4H.exists():
        pytest.skip("golden exports unavailable")
    return build_canonical_bundle(golden_15m=GOLDEN_15M, golden_4h=GOLDEN_4H)


def test_mtf_merge_uses_4h_before_15m_at_equal_finalization() -> None:
    boundary = Timeframe.HOURS_4.duration_ms
    htf = HistoricalBar("BTCUSDT", Timeframe.HOURS_4, 0, 1, 2, 0.5, 1.5)
    ltf = HistoricalBar(
        "BTCUSDT",
        Timeframe.MINUTES_15,
        boundary - Timeframe.MINUTES_15.duration_ms,
        1,
        2,
        0.5,
        1.5,
    )
    assert tuple(bar.timeframe for bar in _merge_streams((ltf,), (htf,))) == (
        Timeframe.HOURS_4,
        Timeframe.MINUTES_15,
    )


def test_mtf_merge_rejects_nonchronological_input_without_sorting() -> None:
    older = HistoricalBar("BTCUSDT", Timeframe.MINUTES_15, 0, 1, 2, 0.5, 1.5)
    newer = HistoricalBar(
        "BTCUSDT", Timeframe.MINUTES_15, 900_000, 1, 2, 0.5, 1.5
    )
    with pytest.raises(ValueError, match="non-increasing"):
        _merge_streams((newer, older), ())


@pytest.mark.parametrize(
    ("field", "value"),
    (
        ("adr_definition", "FAKE_ADR"),
        ("utc_boundary", "America/New_York"),
        ("source_open_feature_convention", "decision_time"),
        ("decision_split_convention", "open_time"),
        ("mae_mfe_convention", "fake/v0"),
    ),
)
def test_research_config_rejects_forged_convention_metadata(field, value) -> None:
    with pytest.raises(ValueError):
        ResearchConfig(**{field: value})


def test_split_config_rejects_overlapping_custom_windows() -> None:
    with pytest.raises(ValueError, match="non-overlapping"):
        SplitConfig(
            development_end="2026-07-20",
            validation_start="2026-07-10",
        )


def test_bundle_rejects_same_resolved_source_for_both_timeframes() -> None:
    with pytest.raises(ValueError, match="distinct"):
        build_canonical_bundle(
            golden_15m=GOLDEN_4H,
            golden_4h=GOLDEN_4H.resolve(),
        )


def test_bundle_rejects_distinct_paths_with_identical_source_bytes(
    tmp_path: Path,
) -> None:
    first = tmp_path / "first.csv"
    second = tmp_path / "second.csv"
    payload = GOLDEN_4H.read_bytes()
    first.write_bytes(payload)
    second.write_bytes(payload)
    with pytest.raises(ValueError, match="content must be distinct"):
        build_canonical_bundle(golden_15m=first, golden_4h=second)


def test_bundle_rejects_formatting_distinct_normalized_duplicate_sources(
    tmp_path: Path,
) -> None:
    first = tmp_path / "first.csv"
    second = tmp_path / "second.csv"
    payload = GOLDEN_4H.read_bytes()
    first.write_bytes(payload)
    second.write_bytes(b"\xef\xbb\xbf" + payload)
    with pytest.raises(ValueError, match="normalized source content must be distinct"):
        build_canonical_bundle(golden_15m=first, golden_4h=second)


def test_canonical_bundle_has_all_equal_boundary_pairs_in_frozen_order(
    reporting_bundle,
) -> None:
    traces = reporting_bundle.replay.traces
    pairs = [
        (left, right)
        for left, right in zip(traces, traces[1:])
        if left.source_bar.finalized_at == right.source_bar.finalized_at
    ]
    assert len(pairs) == 653
    assert all(
        left.source_bar.timeframe is Timeframe.HOURS_4
        and right.source_bar.timeframe is Timeframe.MINUTES_15
        for left, right in pairs
    )


def test_baseline_report_exact_counts_metrics_and_quality(reporting_bundle) -> None:
    report = baseline_report(reporting_bundle)
    quality = dict(report.quality_counts)
    assert (
        report.observed_flips,
        report.eligible_setups,
        report.noneligible_rejected,
        report.armed,
        report.cancelled,
        report.opened_trades,
        report.closed_trades,
        report.censored_trades,
    ) == (523, 260, 263, 158, 68, 192, 191, 1)
    assert report.metrics.total_r == pytest.approx(-29.008453475608636)
    assert report.metrics.expectancy_r == pytest.approx(-0.1518767197675845)
    assert report.metrics.r_per_setup == pytest.approx(-0.11157097490618706)
    assert report.metrics.profit_factor == pytest.approx(0.8072390130075646)
    assert report.metrics.stop_rate == pytest.approx(0.774869109947644)
    assert report.metrics.mae_observation_count == 191
    assert report.metrics.mfe_observation_count == 191
    assert report.metrics.mean_observed_duration_bars is not None
    assert quality == {
        "censored_trades": 1,
        "missing_mae_mfe": 1,
        "post_entry_15m_gap": 0,
        "setup_adr_available": 449,
        "setup_adr_incomplete_prior_session": 0,
        "setup_adr_warmup": 74,
        "setup_incomplete_session_prefix": 0,
        "setup_missing_adr_extension": 74,
        "trade_adr_available": 162,
        "trade_adr_incomplete_prior_session": 0,
        "trade_adr_warmup": 30,
        "trade_incomplete_session_prefix": 0,
        "trade_missing_adr_extension": 30,
    }


def test_baseline_report_exact_window_counts_and_setup_boundary_purge(
    reporting_bundle,
) -> None:
    windows = {item.role: item for item in baseline_report(reporting_bundle).windows}
    development = windows["development"]
    validation = windows["validation"]
    final_oos = windows["final_oos"]
    assert (
        development.eligible_setups,
        development.included_closed,
        development.purged_setup_boundary,
        development.metrics.total_r,
    ) == (139, 103, 1, pytest.approx(-10.166946804635474))
    assert (
        validation.eligible_setups,
        validation.included_closed,
        validation.purged_setup_boundary,
        validation.metrics.total_r,
    ) == (43, 25, 0, pytest.approx(1.5315730861528918))
    assert (
        final_oos.eligible_setups,
        final_oos.included_closed,
        final_oos.purged_setup_boundary,
        final_oos.metrics.total_r,
    ) == (39, 30, 0, pytest.approx(-10.744226219404073))
    assert not final_oos.evidence_floor_pass
    assert any("below floor 50" in reason for reason in final_oos.evidence_floor_reasons)


def test_window_report_counts_reverse_entry_exit_crossing_as_purged(
    reporting_bundle,
) -> None:
    trade = next(
        row for row in reporting_bundle.dataset.trade_rows if row.outcome_state == "closed"
    )
    setup = next(
        row for row in reporting_bundle.dataset.setup_rows if row.setup_id == trade.setup_id
    )
    window_start = 1_780_272_000_000  # 2026-06-01 00:00:00 UTC
    forged_setup = replace(setup, decision_timestamp=window_start - 1)
    reverse_crossing = replace(
        trade,
        decision_timestamp=window_start - 1,
        exit_timestamp=window_start + 900_000,
    )
    report = _window_report(
        role="test",
        start_date="2026-06-01",
        end_date="2026-06-02",
        setups=(forged_setup,),
        trades=(reverse_crossing,),
        research_config=ResearchConfig(),
    )
    assert report.purged_entry_exit_boundary == 1
    assert report.purged_setup_boundary == 0
    assert report.included_closed == 0


def test_window_report_prior_setup_censored_entry_is_setup_purged(
    reporting_bundle,
) -> None:
    trade = reporting_bundle.dataset.trade_rows[-1]
    setup = next(
        row for row in reporting_bundle.dataset.setup_rows if row.setup_id == trade.setup_id
    )
    window_start = 1_780_272_000_000
    forged_setup = replace(setup, decision_timestamp=window_start - 1)
    censored = replace(
        trade,
        outcome_state="censored",
        decision_timestamp=window_start + 900_000,
        exit_timestamp=None,
    )
    report = _window_report(
        role="test",
        start_date="2026-06-01",
        end_date="2026-06-02",
        setups=(forged_setup,),
        trades=(censored,),
        research_config=ResearchConfig(),
    )
    assert report.purged_setup_boundary == 1
    assert report.censored == 0


def test_report_is_self_describing_and_path_independent(reporting_bundle) -> None:
    relative_bytes = report_json(baseline_report(reporting_bundle))
    absolute_bundle = build_canonical_bundle(
        golden_15m=GOLDEN_15M.resolve(), golden_4h=GOLDEN_4H.resolve()
    )
    assert report_json(baseline_report(absolute_bundle)) == relative_bytes
    payload = json.loads(relative_bytes)
    for key in (
        "replay_config",
        "strategy_config",
        "backtest_config",
        "research_config",
        "split_config",
        "quality_counts",
        "windows",
        "source_artifacts",
        "incomplete_source_dates",
        "effective_research_date_range",
    ):
        assert key in payload
    assert payload["source_identity_status"] == "declared_unverified"
    assert payload["merged_source_bar_count"] == 18_932
    assert payload["top_level_population_scope"] == "full_source_history"
    assert dict(payload["effective_population_counts"]) == {
        "observed_setups_after": 5,
        "observed_setups_before": 74,
        "observed_setups_within": 444,
        "trades_after": 3,
        "trades_before": 30,
        "trades_within": 159,
    }
    assert ["15m", "2026-08-17", 84, 96] in payload["incomplete_source_dates"]
    assert ["4h", "2022-10-04", 2, 6] in payload["incomplete_source_dates"]


def test_report_json_is_byte_deterministic_and_rejects_nan(reporting_bundle) -> None:
    report = baseline_report(reporting_bundle)
    assert report_json(report) == report_json(report)
    assert b"NaN" not in report_json(report)
    malformed = json.loads(report_json(report))
    malformed["metrics"]["total_r"] = float("nan")
    with pytest.raises(ValueError):
        json.dumps(malformed, allow_nan=False)


def test_generated_baseline_report_is_fresh(reporting_bundle) -> None:
    generated = Path("exports/phase7/phase7_baseline.json")
    assert generated.read_bytes() == report_json(baseline_report(reporting_bundle))


def test_cli_creates_refuses_overwrite_and_reproduces_bytes(
    tmp_path: Path, reporting_bundle
) -> None:
    output = tmp_path / "baseline.json"
    command = (
        sys.executable,
        "tools/run_phase7_research.py",
        "--golden-15m",
        str(GOLDEN_15M),
        "--golden-4h",
        str(GOLDEN_4H),
        "--output",
        str(output),
    )
    environment = {**os.environ, "PYTHONPATH": "src"}
    created = subprocess.run(command, env=environment, capture_output=True, check=False)
    assert created.returncode == 0, created.stderr.decode()
    expected = report_json(baseline_report(reporting_bundle))
    assert output.read_bytes() == expected
    refused = subprocess.run(command, env=environment, capture_output=True, check=False)
    assert refused.returncode != 0
    overwritten = subprocess.run(
        (*command, "--overwrite"), env=environment, capture_output=True, check=False
    )
    assert overwritten.returncode == 0, overwritten.stderr.decode()
    assert output.read_bytes() == expected
