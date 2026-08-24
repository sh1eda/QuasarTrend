"""Observational Phase 7.1 Stage 1 exit-efficiency diagnosis.

This module consumes the immutable canonical Phase 7 bundle.  It does not
simulate an alternative exit or participate in candidate/final-OOS workflows.
"""
from __future__ import annotations

from dataclasses import asdict, dataclass
import json
import math
from pathlib import Path
from statistics import mean, median, quantiles
from typing import Iterable

from quasartrend.replay import HistoricalBar, Timeframe
from quasartrend.strategy import Direction

from .models import MAE_MFE_CONVENTION_VERSION, TradeRow
from .pipeline import CanonicalResearchBundle
from .provenance import fingerprint


EXIT_EFFICIENCY_SCHEMA_VERSION = "phase7.1-exit-efficiency-diagnosis/v1"
PHASE7_BASE_SHA = "843574565e76ba470dccddc567e773e04bb7664c"
DIAGNOSTIC_CONVENTION_VERSION = "post_entry_15m_peak_through_exit_bar/v1"
STRICT_PRE_EXIT_CONVENTION_VERSION = "post_entry_15m_excluding_exit_bar/v1"
THRESHOLDS_R = (0.5, 1.0, 1.5, 2.0, 3.0, 4.0, 5.0)
_PARITY_TOLERANCE = 1e-12
_BAR_MS = 15 * 60 * 1000
_CANONICAL_BUNDLE_BINDINGS = {
    "manifest_id": "812d4449ce31615324f021533ce8d4492f9f49ffc98bd1879e823add7991b39c",
    "dataset_fingerprint": "96fb832135bef85123bcca522e6a7836aed58531e73158870c68c030ec9e1982",
    "strategy_fingerprint": "bb8fdc3cda4c39b43a09d8fb6a95a05077d35a01e14e0718a6af75ef21f1f0e6",
    "replay_fingerprint": "54452d8b4a309209586684b6ed356d4b7344f560d2025f1f0c4ad2d001e80312",
    "backtest_fingerprint": "596a0f6010107a8a3d9f3a4cace7cbb828110af3b1d9d37c58558cc3bb2b40d3",
    "research_fingerprint": "54e5635d41f26fd935e3ebd1e93a865c014566c683bda0f9838809fe7c929f33",
    "split_fingerprint": "0d807d81cf3ef72d5fa84a40666c0f2f360da059dcb46188cdad33397aebef5a",
}


@dataclass(frozen=True, slots=True)
class PeakReconstruction:
    """Bar-level favorable-extreme reconstruction, never an intrabar timestamp."""

    mfe_r: float
    favorable_price: float
    peak_bar_open_timestamp: int | None
    peak_bar_finalized_timestamp: int | None
    bars_from_entry_to_peak: int | None
    peak_occurs_on_exit_bar: bool


@dataclass(frozen=True, slots=True)
class ExitEfficiencyReport:
    schema_version: str
    phase7_base_sha: str
    diagnostic_convention_version: str
    strict_pre_exit_bar_convention_version: str
    mae_mfe_convention_version: str
    provenance: dict[str, object]
    population: dict[str, int]
    methodology: dict[str, object]
    data_quality: dict[str, int]
    aggregate_statistics: dict[str, object]
    distributions: dict[str, dict[str, float | int | None]]
    threshold_reach_diagnostics: tuple[dict[str, object], ...]
    cohorts: tuple[dict[str, object], ...]
    losing_trade_mfe_analysis: dict[str, object]
    profitable_trade_giveback: dict[str, object]
    totals_and_caveat: dict[str, object]
    exit_reason_groups: tuple[dict[str, object], ...]
    per_trade: tuple[dict[str, object], ...]


def _finite(value: float | None, name: str) -> float:
    if value is None or not isinstance(value, (float, int)) or isinstance(value, bool):
        raise ValueError(f"{name} must be finite")
    result = float(value)
    if not math.isfinite(result):
        raise ValueError(f"{name} must be finite")
    return result


def reconstruct_favorable_peak(
    *,
    direction: Direction,
    entry_price: float,
    risk_per_unit: float,
    bars: Iterable[HistoricalBar],
    exit_bar_open_timestamp: int | None,
) -> PeakReconstruction:
    """Rebuild bar-level MFE, choosing the earliest equal favorable extreme.

    ``bars`` must already be the post-entry 15m sequence selected by Phase 7.
    An empty sequence has the specified deterministic zero-excursion result.
    """
    if direction not in (Direction.LONG, Direction.SHORT):
        raise TypeError("direction must be a Direction")
    entry = _finite(entry_price, "entry_price")
    risk = _finite(risk_per_unit, "risk_per_unit")
    if risk <= 0:
        raise ValueError("risk_per_unit must be positive")
    sequence = tuple(bars)
    previous_open: int | None = None
    for bar in sequence:
        if not isinstance(bar, HistoricalBar) or bar.timeframe is not Timeframe.MINUTES_15:
            raise TypeError("peak reconstruction requires canonical 15m HistoricalBar values")
        if previous_open is not None and bar.open_time <= previous_open:
            raise ValueError("peak reconstruction bars must be strictly chronological")
        previous_open = bar.open_time
    if not sequence:
        return PeakReconstruction(0.0, entry, None, None, None, False)

    if direction is Direction.LONG:
        excursions = tuple(max(0.0, bar.high - entry) for bar in sequence)
        peak_excursion = max(excursions)
        peak_index = next(index for index, value in enumerate(excursions) if value == peak_excursion)
        favorable_price = sequence[peak_index].high
    else:
        excursions = tuple(max(0.0, entry - bar.low) for bar in sequence)
        peak_excursion = max(excursions)
        peak_index = next(index for index, value in enumerate(excursions) if value == peak_excursion)
        favorable_price = sequence[peak_index].low
    peak = sequence[peak_index]
    return PeakReconstruction(
        mfe_r=peak_excursion / risk,
        favorable_price=favorable_price,
        peak_bar_open_timestamp=peak.open_time,
        peak_bar_finalized_timestamp=peak.finalized_at,
        bars_from_entry_to_peak=peak_index + 1,
        peak_occurs_on_exit_bar=peak.open_time == exit_bar_open_timestamp,
    )


def _summary(values: Iterable[float | None]) -> dict[str, float | int | None]:
    present = tuple(float(value) for value in values if value is not None)
    if not present:
        return {"count": 0, "minimum": None, "p10": None, "p25": None, "p50": None,
                "median": None, "p75": None, "p90": None, "p95": None,
                "maximum": None, "mean": None, "quantile_method": "inclusive"}
    if len(present) == 1:
        qs = (present[0],) * 99
    else:
        qs = tuple(quantiles(present, n=100, method="inclusive"))
    return {
        "count": len(present), "minimum": min(present), "p10": qs[9], "p25": qs[24],
        "p50": qs[49], "median": median(present), "p75": qs[74], "p90": qs[89],
        "p95": qs[94], "maximum": max(present), "mean": mean(present),
        "quantile_method": "inclusive",
    }


def _mean_median(values: Iterable[float | None]) -> dict[str, float | int | None]:
    present = tuple(float(value) for value in values if value is not None)
    return {"count": len(present), "mean": None if not present else mean(present),
            "median": None if not present else median(present)}


def _percentage(count: int, denominator: int) -> float | None:
    return None if denominator == 0 else 100.0 * count / denominator


def threshold_reached(mfe_r: float, threshold_r: float) -> bool:
    """Return the predeclared landmark predicate; equality is a reach."""
    return _finite(mfe_r, "mfe_r") >= _finite(threshold_r, "threshold_r")


def realized_outcome_bucket(realized_r: float) -> str:
    """Return one fixed, exhaustive realized-R outcome bucket."""
    value = _finite(realized_r, "realized_r")
    if value < 0.0:
        return "loss"
    if value < 0.25:
        return "near_breakeven_or_small_retained_profit"
    if value < 1.0:
        return "positive_but_below_1r"
    return "1r_or_more"


def giveback_metrics(*, mfe_r: float, realized_r: float) -> tuple[float, float | None, float | None]:
    """Return un-clipped giveback, capture ratio, and giveback fraction."""
    mfe = _finite(mfe_r, "mfe_r")
    realized = _finite(realized_r, "realized_r")
    if mfe < 0.0:
        raise ValueError("mfe_r must be non-negative")
    giveback = mfe - realized
    if mfe == 0.0:
        return giveback, None, None
    return giveback, realized / mfe, giveback / mfe


def _outcome_buckets(rows: tuple[dict[str, object], ...]) -> tuple[dict[str, object], ...]:
    labels = ("loss", "near_breakeven_or_small_retained_profit", "positive_but_below_1r", "1r_or_more")
    result = []
    for label in labels:
        count = sum(realized_outcome_bucket(float(row["realized_r"])) == label for row in rows)
        result.append({"bucket": label, "count": count, "percentage": _percentage(count, len(rows))})
    if sum(item["count"] for item in result) != len(rows):
        raise AssertionError("realized-R buckets must be exhaustive")
    return tuple(result)


def _closed_sequence(
    row: TradeRow, bars_by_open: dict[int, HistoricalBar],
) -> tuple[HistoricalBar, ...]:
    if row.exit_source_open_timestamp is None:
        raise ValueError(f"closed trade {row.trade_id} has no exit source-open timestamp")
    expected = row.expected_duration_bars
    if expected is None or expected < 0:
        raise ValueError(f"closed trade {row.trade_id} has invalid expected duration")
    opens = tuple(row.source_open_timestamp + _BAR_MS * offset for offset in range(1, expected + 1))
    missing = tuple(open_time for open_time in opens if open_time not in bars_by_open)
    if missing:
        raise ValueError(
            f"post-entry 15m gap for {row.trade_id}; first missing source-open bar {missing[0]}"
        )
    sequence = tuple(bars_by_open[open_time] for open_time in opens)
    if not sequence or sequence[-1].open_time != row.exit_source_open_timestamp:
        raise ValueError(
            f"post-entry trace for {row.trade_id} does not terminate on its canonical exit bar"
        )
    return sequence


def _record_for_closed_trade(row: TradeRow, bars_by_open: dict[int, HistoricalBar]) -> dict[str, object]:
    required = (row.exit_event_id, row.exit_timestamp, row.exit_source_open_timestamp,
                row.exit_finalized_timestamp, row.canonical_exit_price, row.exit_primary_reason,
                row.realized_r, row.mae_r, row.mfe_r, row.observed_duration_bars,
                row.expected_duration_bars, row.elapsed_duration_ms)
    if any(value is None for value in required):
        raise ValueError(f"closed trade {row.trade_id} lacks required canonical outcome data")
    if row.mae_mfe_convention_version != MAE_MFE_CONVENTION_VERSION:
        raise ValueError(f"closed trade {row.trade_id} has a noncanonical MAE/MFE convention")
    sequence = _closed_sequence(row, bars_by_open)
    full_peak = reconstruct_favorable_peak(
        direction=row.direction, entry_price=row.canonical_entry_price,
        risk_per_unit=row.canonical_risk_per_unit, bars=sequence,
        exit_bar_open_timestamp=row.exit_source_open_timestamp,
    )
    pre_exit = reconstruct_favorable_peak(
        direction=row.direction, entry_price=row.canonical_entry_price,
        risk_per_unit=row.canonical_risk_per_unit, bars=sequence[:-1],
        exit_bar_open_timestamp=None,
    )
    frozen_mfe = _finite(row.mfe_r, "canonical mfe_r")
    if not math.isclose(full_peak.mfe_r, frozen_mfe, rel_tol=_PARITY_TOLERANCE, abs_tol=_PARITY_TOLERANCE):
        peak_bar = sequence[(full_peak.bars_from_entry_to_peak or 1) - 1]
        raise ValueError(
            "MFE reconstruction mismatch for "
            f"{row.trade_id}: reconstructed={full_peak.mfe_r!r}, frozen={frozen_mfe!r}, "
            f"peak_bar_open={peak_bar.open_time}, peak_ohlc="
            f"({peak_bar.open!r},{peak_bar.high!r},{peak_bar.low!r},{peak_bar.close!r}), "
            f"risk={row.canonical_risk_per_unit!r}"
        )
    realized = _finite(row.realized_r, "realized_r")
    giveback, capture_ratio, giveback_fraction = giveback_metrics(
        mfe_r=frozen_mfe, realized_r=realized,
    )
    flags = list(row.data_quality_flags)
    if full_peak.peak_occurs_on_exit_bar:
        flags.append("peak_on_exit_bar_ohlc_order_ambiguous")
    if frozen_mfe == 0.0:
        flags.append("zero_mfe_ratio_undefined")
    if len(flags) != len(set(flags)):
        flags = list(dict.fromkeys(flags))
    return {
        "setup_id": row.setup_id,
        "trade_id": row.trade_id,
        "entry_event_id": row.entry_event_id,
        "exit_event_id": row.exit_event_id,
        "symbol": row.symbol,
        "direction": row.direction.value,
        "entry_timestamp": row.decision_timestamp,
        "entry_source_open_timestamp": row.source_open_timestamp,
        "canonical_entry_price": row.canonical_entry_price,
        "canonical_stop_price": row.canonical_stop_price,
        "absolute_stop_distance": row.abs_stop_distance,
        "canonical_risk_per_unit": row.canonical_risk_per_unit,
        "exit_timestamp": row.exit_timestamp,
        "exit_source_open_timestamp": row.exit_source_open_timestamp,
        "canonical_exit_price": row.canonical_exit_price,
        "exit_primary_reason": row.exit_primary_reason,
        "exit_all_reasons": row.exit_all_reasons,
        "realized_r": realized,
        "mae_r": _finite(row.mae_r, "mae_r"),
        "mfe_r": frozen_mfe,
        "observed_duration_bars": row.observed_duration_bars,
        "expected_duration_bars": row.expected_duration_bars,
        "elapsed_duration_ms": row.elapsed_duration_ms,
        "peak_favorable_price": full_peak.favorable_price,
        "peak_favorable_bar_open_timestamp": full_peak.peak_bar_open_timestamp,
        "peak_favorable_bar_finalized_timestamp": full_peak.peak_bar_finalized_timestamp,
        "bars_from_entry_to_peak": full_peak.bars_from_entry_to_peak,
        "peak_occurs_on_exit_bar": full_peak.peak_occurs_on_exit_bar,
        "strict_pre_exit_bar_mfe_r": pre_exit.mfe_r,
        "capture_ratio": capture_ratio,
        "giveback_r": giveback,
        "giveback_fraction": giveback_fraction,
        "data_quality_flags": tuple(flags),
    }


def _validate_canonical_bundle_binding(bundle: CanonicalResearchBundle) -> None:
    """Reject altered Phase 7 semantics before any observational calculation."""
    observed = {
        "manifest_id": bundle.dataset.manifest_id,
        "dataset_fingerprint": fingerprint(bundle.dataset),
        "strategy_fingerprint": bundle.dataset.strategy_fingerprint,
        "replay_fingerprint": bundle.dataset.replay_fingerprint,
        "backtest_fingerprint": bundle.dataset.backtest_fingerprint,
        "research_fingerprint": bundle.dataset.research_fingerprint,
        "split_fingerprint": bundle.dataset.split_fingerprint,
    }
    mismatches = tuple(
        name for name, expected in _CANONICAL_BUNDLE_BINDINGS.items()
        if observed[name] != expected
    )
    if mismatches:
        detail = "; ".join(
            f"{name}: expected={_CANONICAL_BUNDLE_BINDINGS[name]}, observed={observed[name]}"
            for name in mismatches
        )
        raise ValueError(f"canonical Phase 7 bundle binding mismatch: {detail}")


def _cohort(name: str, rows: tuple[dict[str, object], ...]) -> dict[str, object]:
    return {
        "cohort": name,
        "trade_count": len(rows),
        "trade_ids": tuple(str(row["trade_id"]) for row in rows),
        "setup_ids": tuple(str(row["setup_id"]) for row in rows),
        "mean_mfe_r": _mean_median(row["mfe_r"] for row in rows)["mean"],
        "median_mfe_r": _mean_median(row["mfe_r"] for row in rows)["median"],
        "mean_realized_r": _mean_median(row["realized_r"] for row in rows)["mean"],
        "median_realized_r": _mean_median(row["realized_r"] for row in rows)["median"],
        "mean_giveback_r": _mean_median(row["giveback_r"] for row in rows)["mean"],
        "median_giveback_r": _mean_median(row["giveback_r"] for row in rows)["median"],
        "total_realized_r": sum(float(row["realized_r"]) for row in rows),
        "sum_observed_mfe_r": sum(float(row["mfe_r"]) for row in rows),
    }


def exit_efficiency_report(bundle: CanonicalResearchBundle) -> ExitEfficiencyReport:
    """Diagnose exactly the frozen canonical closed-trade population.

    This function has no candidate imports and does not expose outcome values as
    entry features.  Any missing sequence or MFE parity mismatch is fatal.
    """
    if not isinstance(bundle, CanonicalResearchBundle):
        raise TypeError("a CanonicalResearchBundle is required")
    _validate_canonical_bundle_binding(bundle)
    traces = tuple(trace.source_bar for trace in bundle.replay.traces
                   if trace.source_bar.timeframe is Timeframe.MINUTES_15)
    bars_by_open = {bar.open_time: bar for bar in traces}
    if len(bars_by_open) != len(traces):
        raise ValueError("canonical 15m traces have duplicate source-open timestamps")
    ordered_closed_rows = tuple(sorted(
        (row for row in bundle.dataset.trade_rows if row.outcome_state == "closed"),
        key=lambda row: (row.decision_timestamp, row.source_processing_key, row.trade_id),
    ))
    censored = tuple(row for row in bundle.dataset.trade_rows if row.outcome_state == "censored")
    if len(ordered_closed_rows) != 191 or len(censored) != 1 or len(bundle.dataset.trade_rows) != 192:
        raise ValueError(
            "Phase 7.1 requires exactly 191 closed and 1 censored canonical trade "
            f"(observed closed={len(ordered_closed_rows)}, censored={len(censored)}, "
            f"total={len(bundle.dataset.trade_rows)})"
        )
    if len({row.trade_id for row in ordered_closed_rows}) != len(ordered_closed_rows):
        raise ValueError("duplicate canonical closed trade IDs")
    records = tuple(_record_for_closed_trade(row, bars_by_open) for row in ordered_closed_rows)
    if tuple(record["trade_id"] for record in records) != tuple(row.trade_id for row in ordered_closed_rows):
        raise AssertionError("per-trade diagnostic ordering changed")

    total = len(records)
    mfe = tuple(float(row["mfe_r"]) for row in records)
    realized = tuple(float(row["realized_r"]) for row in records)
    mae = tuple(float(row["mae_r"]) for row in records)
    pre_exit = tuple(float(row["strict_pre_exit_bar_mfe_r"]) for row in records)
    giveback = tuple(float(row["giveback_r"]) for row in records)
    capture = tuple(row["capture_ratio"] for row in records)
    giveback_fraction = tuple(row["giveback_fraction"] for row in records)
    durations = tuple(float(row["observed_duration_bars"]) for row in records)
    total_mfe = sum(mfe)
    peak_exit_count = sum(bool(row["peak_occurs_on_exit_bar"]) for row in records)
    negative_giveback = sum(value < 0.0 for value in giveback)
    zero_mfe = sum(value == 0.0 for value in mfe)
    quality = {
        "closed_trades_expected": 191,
        "closed_trades_observed": total,
        "censored_trades_excluded_from_realized_outcome_analysis": len(censored),
        "mae_observation_count": len(mae),
        "mfe_observation_count": len(mfe),
        "missing_canonical_mfe_count": sum(row.mfe_r is None for row in ordered_closed_rows),
        "missing_canonical_mae_count": sum(row.mae_r is None for row in ordered_closed_rows),
        "duplicate_trade_ids": total - len({row["trade_id"] for row in records}),
        "missing_exit_ids": sum(row.exit_event_id is None for row in ordered_closed_rows),
        "post_entry_15m_gaps": sum("post_entry_15m_gap" in row.data_quality_flags for row in ordered_closed_rows),
        "mfe_reconstruction_mismatches": 0,
        "negative_giveback_observations": negative_giveback,
        "zero_mfe_observations": zero_mfe,
        "exit_bar_peak_observations": peak_exit_count,
    }
    if any(quality[key] for key in ("missing_canonical_mfe_count", "missing_canonical_mae_count",
                                    "duplicate_trade_ids", "missing_exit_ids", "post_entry_15m_gaps",
                                    "mfe_reconstruction_mismatches")):
        raise ValueError(f"canonical exit-efficiency data-quality invariant failed: {quality}")

    aggregate_capture = None if total_mfe == 0.0 else sum(realized) / total_mfe
    aggregate_statistics = {
        "closed_trade_count": total,
        "mean_realized_r": mean(realized), "median_realized_r": median(realized),
        "mean_mae_r": mean(mae), "median_mae_r": median(mae),
        "mean_mfe_r": mean(mfe), "median_mfe_r": median(mfe),
        "mean_strict_pre_exit_bar_mfe_r": mean(pre_exit),
        "median_strict_pre_exit_bar_mfe_r": median(pre_exit),
        "mean_giveback_r": mean(giveback), "median_giveback_r": median(giveback),
        "aggregate_capture_ratio_descriptive_sum_realized_over_sum_mfe": aggregate_capture,
        "mean_per_trade_capture_ratio": _mean_median(capture)["mean"],
        "median_per_trade_capture_ratio": _mean_median(capture)["median"],
        "mean_giveback_fraction": _mean_median(giveback_fraction)["mean"],
        "median_giveback_fraction": _mean_median(giveback_fraction)["median"],
        "mean_trade_duration_bars": mean(durations), "median_trade_duration_bars": median(durations),
        "exit_bar_peak_count": peak_exit_count,
        "exit_bar_peak_percentage": _percentage(peak_exit_count, total),
        "mean_canonical_minus_pre_exit_mfe_r": mean(full - strict for full, strict in zip(mfe, pre_exit)),
        "median_canonical_minus_pre_exit_mfe_r": median(full - strict for full, strict in zip(mfe, pre_exit)),
        "canonical_minus_pre_exit_mfe_positive_count": sum(full - strict > 0.0 for full, strict in zip(mfe, pre_exit)),
        "canonical_minus_pre_exit_mfe_positive_percentage": _percentage(sum(full - strict > 0.0 for full, strict in zip(mfe, pre_exit)), total),
    }

    threshold_reports = []
    for threshold in THRESHOLDS_R:
        reached = tuple(row for row in records if threshold_reached(float(row["mfe_r"]), threshold))
        n = len(reached)
        final_positive = sum(float(row["realized_r"]) > 0.0 for row in reached)
        final_loss = sum(float(row["realized_r"]) < 0.0 for row in reached)
        final_below_1 = sum(float(row["realized_r"]) < 1.0 for row in reached)
        retained_threshold = sum(float(row["realized_r"]) >= threshold for row in reached)
        half_giveback = sum(
            row["giveback_fraction"] is not None and float(row["giveback_fraction"]) >= .5
            for row in reached
        )
        threshold_reports.append({
            "threshold_r": threshold, "reached_count": n, "reached_percentage": _percentage(n, total),
            "final_realized_positive_count": final_positive,
            "final_realized_positive_percentage_among_reachers": _percentage(final_positive, n),
            "final_realized_loss_count": final_loss,
            "final_realized_loss_percentage_among_reachers": _percentage(final_loss, n),
            "final_realized_less_than_1r_count": final_below_1,
            "final_realized_less_than_1r_percentage_among_reachers": _percentage(final_below_1, n),
            "final_realized_at_least_threshold_count": retained_threshold,
            "final_realized_at_least_threshold_percentage_among_reachers": _percentage(retained_threshold, n),
            "giveback_at_least_50pct_of_mfe_count": half_giveback,
            "giveback_at_least_50pct_of_mfe_percentage_among_reachers": _percentage(half_giveback, n),
            "mean_realized_r": _mean_median(row["realized_r"] for row in reached)["mean"],
            "median_realized_r": _mean_median(row["realized_r"] for row in reached)["median"],
            "mean_mfe_r": _mean_median(row["mfe_r"] for row in reached)["mean"],
            "median_mfe_r": _mean_median(row["mfe_r"] for row in reached)["median"],
            "mean_giveback_r": _mean_median(row["giveback_r"] for row in reached)["mean"],
            "median_giveback_r": _mean_median(row["giveback_r"] for row in reached)["median"],
            "realized_r_outcome_distribution": _outcome_buckets(reached),
        })

    cohorts = (
        _cohort("A_mfe_at_least_1r_realized_loss", tuple(row for row in records if float(row["mfe_r"]) >= 1.0 and float(row["realized_r"]) < 0.0)),
        _cohort("B_mfe_at_least_2r_realized_loss", tuple(row for row in records if float(row["mfe_r"]) >= 2.0 and float(row["realized_r"]) < 0.0)),
        _cohort("C_mfe_at_least_2r_realized_below_1r", tuple(row for row in records if float(row["mfe_r"]) >= 2.0 and float(row["realized_r"]) < 1.0)),
        _cohort("D_mfe_at_least_3r_giveback_fraction_at_least_50pct", tuple(row for row in records if float(row["mfe_r"]) >= 3.0 and row["giveback_fraction"] is not None and float(row["giveback_fraction"]) >= .5)),
    )
    losing = tuple(row for row in records if float(row["realized_r"]) < 0.0)
    profitable = tuple(row for row in records if float(row["realized_r"]) > 0.0)
    losing_analysis: dict[str, object] = {
        "losing_trade_count": len(losing),
        "mean_losing_trade_mfe_r": _mean_median(row["mfe_r"] for row in losing)["mean"],
        "median_losing_trade_mfe_r": _mean_median(row["mfe_r"] for row in losing)["median"],
        "reached_threshold_percentages": tuple({
            "threshold_r": threshold,
            "count": sum(threshold_reached(float(row["mfe_r"]), threshold) for row in losing),
            "percentage": _percentage(sum(threshold_reached(float(row["mfe_r"]), threshold) for row in losing), len(losing)),
        } for threshold in THRESHOLDS_R),
    }
    profitable_giveback: dict[str, object] = {
        "profitable_trade_count": len(profitable),
        "mean_mfe_r": _mean_median(row["mfe_r"] for row in profitable)["mean"],
        "median_mfe_r": _mean_median(row["mfe_r"] for row in profitable)["median"],
        "mean_realized_r": _mean_median(row["realized_r"] for row in profitable)["mean"],
        "median_realized_r": _mean_median(row["realized_r"] for row in profitable)["median"],
        "mean_giveback_r": _mean_median(row["giveback_r"] for row in profitable)["mean"],
        "median_giveback_r": _mean_median(row["giveback_r"] for row in profitable)["median"],
        "aggregate_capture_ratio": None if sum(float(row["mfe_r"]) for row in profitable) == 0.0 else sum(float(row["realized_r"]) for row in profitable) / sum(float(row["mfe_r"]) for row in profitable),
        "mean_capture_ratio": _mean_median(row["capture_ratio"] for row in profitable)["mean"],
        "median_capture_ratio": _mean_median(row["capture_ratio"] for row in profitable)["median"],
        "giveback_fraction_thresholds": tuple({
            "threshold": threshold,
            "count": sum(row["giveback_fraction"] is not None and float(row["giveback_fraction"]) >= threshold for row in profitable),
            "percentage": _percentage(sum(row["giveback_fraction"] is not None and float(row["giveback_fraction"]) >= threshold for row in profitable), len(profitable)),
        } for threshold in (.25, .5, .75)),
    }
    reason_groups = []
    for reason in sorted({str(row["exit_primary_reason"]) for row in records}):
        group = tuple(row for row in records if row["exit_primary_reason"] == reason)
        reason_groups.append({
            "exit_primary_reason": reason, "trade_count": len(group),
            "mean_realized_r": _mean_median(row["realized_r"] for row in group)["mean"],
            "median_realized_r": _mean_median(row["realized_r"] for row in group)["median"],
            "mean_mfe_r": _mean_median(row["mfe_r"] for row in group)["mean"],
            "median_mfe_r": _mean_median(row["mfe_r"] for row in group)["median"],
            "mean_giveback_r": _mean_median(row["giveback_r"] for row in group)["mean"],
            "median_giveback_r": _mean_median(row["giveback_r"] for row in group)["median"],
            "mean_capture_ratio": _mean_median(row["capture_ratio"] for row in group)["mean"],
        })
    manifest = bundle.dataset.manifest
    provenance = {
        "phase7_base_sha": PHASE7_BASE_SHA,
        "manifest_id": bundle.dataset.manifest_id,
        "dataset_fingerprint": fingerprint(bundle.dataset),
        "strategy_fingerprint": bundle.dataset.strategy_fingerprint,
        "replay_fingerprint": bundle.dataset.replay_fingerprint,
        "backtest_fingerprint": bundle.dataset.backtest_fingerprint,
        "research_fingerprint": bundle.dataset.research_fingerprint,
        "split_fingerprint": bundle.dataset.split_fingerprint,
        "source_artifacts": tuple(asdict(item) for item in manifest.source_artifacts),
        "source_row_counts": bundle.source_counts,
        "canonical_trade_row_count": len(bundle.dataset.trade_rows),
        "canonical_setup_row_count": len(bundle.dataset.setup_rows),
        "declared_symbol": manifest.source_artifacts[0].declared_symbol,
    }
    return ExitEfficiencyReport(
        schema_version=EXIT_EFFICIENCY_SCHEMA_VERSION,
        phase7_base_sha=PHASE7_BASE_SHA,
        diagnostic_convention_version=DIAGNOSTIC_CONVENTION_VERSION,
        strict_pre_exit_bar_convention_version=STRICT_PRE_EXIT_CONVENTION_VERSION,
        mae_mfe_convention_version=MAE_MFE_CONVENTION_VERSION,
        provenance=provenance,
        population={"closed_trades_analyzed": total, "censored_trades_excluded_from_realized_outcome_analysis": len(censored), "mae_observations": len(mae), "mfe_observations": len(mfe)},
        methodology={
            "peak_favorable_excursion_bar": "long uses bar high; short uses bar low; earliest equal extreme is selected; timestamp is bar-level only",
            "exit_bar_ambiguity": "full canonical MFE includes exit-bar OHLC; peak on exit bar has unresolved intrabar ordering ambiguity",
            "strict_pre_exit_bar_mfe": "post-entry 15m bars excluding the exit bar; no preceding bar yields zero excursion from entry price",
            "giveback_r": "mfe_r - realized_r",
            "capture_ratio": "realized_r / mfe_r when mfe_r > 0; null when mfe_r == 0",
            "giveback_fraction": "giveback_r / mfe_r when mfe_r > 0; null when mfe_r == 0",
            "thresholds_r": THRESHOLDS_R,
            "scope": "observational canonical-exit diagnosis only; no candidate exit simulation or final-OOS evaluation",
        },
        data_quality=quality,
        aggregate_statistics=aggregate_statistics,
        distributions={"giveback_r": _summary(giveback), "mfe_r": _summary(mfe), "realized_r": _summary(realized)},
        threshold_reach_diagnostics=tuple(threshold_reports), cohorts=cohorts,
        losing_trade_mfe_analysis=losing_analysis,
        profitable_trade_giveback=profitable_giveback,
        totals_and_caveat={
            "sum_canonical_mfe_r": total_mfe,
            "sum_strict_pre_exit_bar_mfe_r": sum(pre_exit),
            "sum_realized_r": sum(realized),
            "sum_mfe_minus_sum_realized_r": total_mfe - sum(realized),
            "caveat": "Summed MFE is not a realizable strategy return. MFE values occur at different times, cannot generally be captured simultaneously, may include intrabar ambiguity on exit bars, and use future outcome information. This comparison measures observed opportunity/giveback only.",
        },
        exit_reason_groups=tuple(reason_groups), per_trade=records,
    )


def exit_efficiency_json(report: ExitEfficiencyReport) -> bytes:
    """Serialize a path-independent, newline-terminated, NaN-safe artifact."""
    return json.dumps(asdict(report), sort_keys=True, separators=(",", ":"), allow_nan=False).encode() + b"\n"


def write_exit_efficiency_report(
    report: ExitEfficiencyReport, output: Path, *, overwrite: bool = False,
) -> None:
    if output.exists() and not overwrite:
        raise FileExistsError("refusing to overwrite existing exit-efficiency report")
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_bytes(exit_efficiency_json(report))
