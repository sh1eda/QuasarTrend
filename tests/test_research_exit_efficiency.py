from __future__ import annotations

import json
import os
from pathlib import Path
import subprocess
import sys

import pytest

from quasartrend.backtest import BacktestConfig
from quasartrend.replay import HistoricalBar, Timeframe
from quasartrend.research.exit_efficiency import (
    EXIT_EFFICIENCY_SCHEMA_VERSION,
    PHASE7_BASE_SHA,
    exit_efficiency_json,
    exit_efficiency_report,
    giveback_metrics,
    reconstruct_favorable_peak,
    realized_outcome_bucket,
    threshold_reached,
)
from quasartrend.research.pipeline import build_canonical_bundle
from quasartrend.strategy import Direction
from quasartrend.strategy import StrategyConfig


GOLDEN_15M = Path("tests/golden/tradingview_15m.csv")
GOLDEN_4H = Path("tests/golden/tradingview_4h.csv")
BAR_MS = 15 * 60 * 1000


@pytest.fixture(scope="module")
def phase71_report():
    bundle = build_canonical_bundle(golden_15m=GOLDEN_15M, golden_4h=GOLDEN_4H)
    return bundle, exit_efficiency_report(bundle)


def _bar(open_time: int, high: float, low: float) -> HistoricalBar:
    return HistoricalBar("BTCUSDT", Timeframe.MINUTES_15, open_time, 100.0, high, low, 100.0)


def test_golden_population_identity_order_and_reconstruction_parity(phase71_report) -> None:
    bundle, report = phase71_report
    assert report.schema_version == EXIT_EFFICIENCY_SCHEMA_VERSION
    assert report.phase7_base_sha == PHASE7_BASE_SHA
    assert report.population == {
        "closed_trades_analyzed": 191,
        "censored_trades_excluded_from_realized_outcome_analysis": 1,
        "mae_observations": 191,
        "mfe_observations": 191,
    }
    records = report.per_trade
    assert len({item["trade_id"] for item in records}) == 191
    setup_ids = {row.setup_id for row in bundle.dataset.setup_rows}
    assert all(item["setup_id"] in setup_ids for item in records)
    assert tuple(item["trade_id"] for item in records) == tuple(
        row.trade_id for row in sorted(
            (row for row in bundle.dataset.trade_rows if row.outcome_state == "closed"),
            key=lambda row: (row.decision_timestamp, row.source_processing_key, row.trade_id),
        )
    )
    canonical = {row.trade_id: row for row in bundle.dataset.trade_rows}
    assert all(item["mfe_r"] == pytest.approx(canonical[item["trade_id"]].mfe_r, abs=1e-12, rel=1e-12) for item in records)
    assert report.data_quality["mfe_reconstruction_mismatches"] == 0
    assert report.data_quality["post_entry_15m_gaps"] == 0


@pytest.mark.parametrize(
    ("configuration", "binding"),
    (({"strategy_config": StrategyConfig(atr_multiplier=1.5)}, "strategy_fingerprint"),
     ({"backtest_config": BacktestConfig(fee_bps=1.0)}, "backtest_fingerprint")),
)
def test_rejects_noncanonical_strategy_or_backtest_bundle(configuration, binding) -> None:
    bundle = build_canonical_bundle(
        golden_15m=GOLDEN_15M, golden_4h=GOLDEN_4H, **configuration,
    )
    with pytest.raises(ValueError, match=rf"canonical Phase 7 bundle binding mismatch: .*{binding}"):
        exit_efficiency_report(bundle)


def test_long_short_tie_and_exit_bar_peak_reconstruction() -> None:
    long_bars = (_bar(BAR_MS, 101.0, 99.0), _bar(2 * BAR_MS, 103.0, 98.0), _bar(3 * BAR_MS, 102.0, 97.0))
    long = reconstruct_favorable_peak(
        direction=Direction.LONG, entry_price=100.0, risk_per_unit=1.0,
        bars=long_bars, exit_bar_open_timestamp=3 * BAR_MS,
    )
    assert (long.favorable_price, long.mfe_r, long.peak_bar_open_timestamp, long.bars_from_entry_to_peak) == (103.0, 3.0, 2 * BAR_MS, 2)
    assert not long.peak_occurs_on_exit_bar

    short_bars = (_bar(BAR_MS, 102.0, 99.0), _bar(2 * BAR_MS, 104.0, 96.0), _bar(3 * BAR_MS, 105.0, 94.0))
    short = reconstruct_favorable_peak(
        direction=Direction.SHORT, entry_price=100.0, risk_per_unit=2.0,
        bars=short_bars, exit_bar_open_timestamp=3 * BAR_MS,
    )
    assert (short.favorable_price, short.mfe_r, short.peak_bar_open_timestamp, short.bars_from_entry_to_peak) == (94.0, 3.0, 3 * BAR_MS, 3)
    assert short.peak_occurs_on_exit_bar

    tied = reconstruct_favorable_peak(
        direction=Direction.LONG, entry_price=100.0, risk_per_unit=1.0,
        bars=(_bar(BAR_MS, 102.0, 99.0), _bar(2 * BAR_MS, 102.0, 97.0)),
        exit_bar_open_timestamp=2 * BAR_MS,
    )
    assert tied.peak_bar_open_timestamp == BAR_MS
    assert tied.bars_from_entry_to_peak == 1

    # The peak comparison is over clamped favorable excursion, not raw highs/lows:
    # every non-empty zero-MFE bar ties at zero and must choose the earliest bar.
    long_zero_tie = reconstruct_favorable_peak(
        direction=Direction.LONG, entry_price=100.0, risk_per_unit=1.0,
        bars=(_bar(BAR_MS, 99.0, 97.0), _bar(2 * BAR_MS, 100.0, 96.0)),
        exit_bar_open_timestamp=2 * BAR_MS,
    )
    short_zero_tie = reconstruct_favorable_peak(
        direction=Direction.SHORT, entry_price=100.0, risk_per_unit=1.0,
        bars=(_bar(BAR_MS, 102.0, 101.0), _bar(2 * BAR_MS, 103.0, 100.0)),
        exit_bar_open_timestamp=2 * BAR_MS,
    )
    assert (long_zero_tie.mfe_r, long_zero_tie.peak_bar_open_timestamp, long_zero_tie.peak_occurs_on_exit_bar) == (0.0, BAR_MS, False)
    assert (short_zero_tie.mfe_r, short_zero_tie.peak_bar_open_timestamp, short_zero_tie.peak_occurs_on_exit_bar) == (0.0, BAR_MS, False)


def test_exit_bar_pre_exit_zero_and_ratio_math(phase71_report) -> None:
    _, report = phase71_report
    exit_peak = next(item for item in report.per_trade if item["peak_occurs_on_exit_bar"])
    assert exit_peak["mfe_r"] >= exit_peak["strict_pre_exit_bar_mfe_r"]
    assert "peak_on_exit_bar_ohlc_order_ambiguous" in exit_peak["data_quality_flags"]

    zero = reconstruct_favorable_peak(direction=Direction.LONG, entry_price=100.0, risk_per_unit=2.0, bars=(), exit_bar_open_timestamp=None)
    assert zero.mfe_r == 0.0 and zero.peak_bar_open_timestamp is None
    assert giveback_metrics(mfe_r=0.0, realized_r=-1.0) == (1.0, None, None)
    assert json.loads(json.dumps({"capture_ratio": None, "giveback_fraction": None}, allow_nan=False)) == {"capture_ratio": None, "giveback_fraction": None}

    row = next(item for item in report.per_trade if item["mfe_r"] > 0.0)
    assert row["giveback_r"] == pytest.approx(row["mfe_r"] - row["realized_r"])
    assert row["capture_ratio"] == pytest.approx(row["realized_r"] / row["mfe_r"])
    assert row["giveback_fraction"] == pytest.approx(row["giveback_r"] / row["mfe_r"])


def test_threshold_and_outcome_bucket_boundaries(phase71_report) -> None:
    _, report = phase71_report
    by_threshold = {item["threshold_r"]: item for item in report.threshold_reach_diagnostics}
    assert tuple(by_threshold) == (0.5, 1.0, 1.5, 2.0, 3.0, 4.0, 5.0)
    for threshold in by_threshold:
        assert threshold_reached(threshold, threshold)
        assert not threshold_reached(threshold - 1e-9, threshold)
    assert [realized_outcome_bucket(value) for value in (-.1, 0.0, .249999, .25, .999999, 1.0)] == [
        "loss", "near_breakeven_or_small_retained_profit", "near_breakeven_or_small_retained_profit",
        "positive_but_below_1r", "positive_but_below_1r", "1r_or_more",
    ]
    for item in report.threshold_reach_diagnostics:
        buckets = item["realized_r_outcome_distribution"]
        assert sum(bucket["count"] for bucket in buckets) == item["reached_count"]
        assert [bucket["bucket"] for bucket in buckets] == [
            "loss", "near_breakeven_or_small_retained_profit",
            "positive_but_below_1r", "1r_or_more",
        ]


def test_bytes_are_deterministic_nan_safe_and_generated_artifact_is_fresh(phase71_report) -> None:
    _, report = phase71_report
    first = exit_efficiency_json(report)
    assert first == exit_efficiency_json(report)
    assert first.endswith(b"\n")
    assert b"NaN" not in first and b"Infinity" not in first and b"-Infinity" not in first
    payload = json.loads(first)
    assert payload["per_trade"]
    zero_mfe = [row for row in payload["per_trade"] if row["mfe_r"] == 0.0]
    assert zero_mfe
    assert all(row["capture_ratio"] is None and row["giveback_fraction"] is None for row in zero_mfe)
    generated = Path("exports/phase7_1/phase71_exit_efficiency.json")
    assert generated.read_bytes() == first


def test_cli_overwrite_refusal_and_reproducible_output(tmp_path: Path, phase71_report) -> None:
    _, report = phase71_report
    output = tmp_path / "phase71.json"
    command = (
        sys.executable, "tools/run_phase71_exit_diagnosis.py",
        "--golden-15m", str(GOLDEN_15M), "--golden-4h", str(GOLDEN_4H),
        "--output", str(output),
    )
    environment = {**os.environ, "PYTHONPATH": "src"}
    created = subprocess.run(command, env=environment, capture_output=True, check=False)
    assert created.returncode == 0, created.stderr.decode()
    expected = exit_efficiency_json(report)
    assert output.read_bytes() == expected
    refused = subprocess.run(command, env=environment, capture_output=True, check=False)
    assert refused.returncode != 0
    overwritten = subprocess.run((*command, "--overwrite"), env=environment, capture_output=True, check=False)
    assert overwritten.returncode == 0, overwritten.stderr.decode()
    assert output.read_bytes() == expected
