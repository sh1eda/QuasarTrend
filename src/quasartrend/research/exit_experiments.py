"""Phase 7.1 Stage 2 development-only counterfactual exit reporting.

This module deliberately has no validation or final-OOS runner.  It maps the
frozen Phase 7 development population to the isolated counterfactual simulator
without changing entries, replay, backtest, or production execution.
"""
from __future__ import annotations

from dataclasses import asdict
import json
import math
from pathlib import Path
from statistics import mean, median
from typing import Iterable

from quasartrend.replay import HistoricalBar, Timeframe

from .exit_counterfactuals import (
    BAR_MS,
    EXIT_COUNTERFACTUAL_CONVENTION_VERSION,
    CandidateSpec,
    CounterfactualTradeInput,
    CounterfactualTradeResult,
    STAGE2_CANDIDATE_SPECS,
    simulate_counterfactual,
)
from .pipeline import CanonicalResearchBundle
from .provenance import fingerprint
from .splits import ChronologicalWindow


EXIT_DEVELOPMENT_SCHEMA_VERSION = "phase7.1-stage2-exit-development/v1"
_EXPECTED_BINDINGS = {
    "manifest_id": "812d4449ce31615324f021533ce8d4492f9f49ffc98bd1879e823add7991b39c",
    "dataset_fingerprint": "96fb832135bef85123bcca522e6a7836aed58531e73158870c68c030ec9e1982",
    "strategy_fingerprint": "bb8fdc3cda4c39b43a09d8fb6a95a05077d35a01e14e0718a6af75ef21f1f0e6",
    "replay_fingerprint": "54452d8b4a309209586684b6ed356d4b7344f560d2025f1f0c4ad2d001e80312",
    "backtest_fingerprint": "596a0f6010107a8a3d9f3a4cace7cbb828110af3b1d9d37c58558cc3bb2b40d3",
    "research_fingerprint": "54e5635d41f26fd935e3ebd1e93a865c014566c683bda0f9838809fe7c929f33",
    "split_fingerprint": "0d807d81cf3ef72d5fa84a40666c0f2f360da059dcb46188cdad33397aebef5a",
}
DEVELOPMENT_WINDOW = ChronologicalWindow("development", "2026-05-15", "2026-07-09")
DEVELOPMENT_SUBWINDOWS = (
    ChronologicalWindow("development_1", "2026-05-15", "2026-05-28"),
    ChronologicalWindow("development_2", "2026-05-29", "2026-06-11"),
    ChronologicalWindow("development_3", "2026-06-12", "2026-06-25"),
    ChronologicalWindow("development_4", "2026-06-26", "2026-07-09"),
)
# This is intentionally a literal rather than a derived import-time default:
# rebinding the public candidate table is detected before economics run.
_EXPECTED_SEMANTIC_FINGERPRINT = "7ee4265689fb5e7b5de42a3aa0c3606f0d19f93e8117d0bf4e5002b5d23a1f45"


def _finite(value: object, name: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value):
        raise ValueError(f"{name} must be finite")
    return float(value)


def _semantic_fingerprint() -> str:
    payload = {
        "counterfactual_convention": EXIT_COUNTERFACTUAL_CONVENTION_VERSION,
        "decision_timing": "active_stop_then_finalized_canonical_exit_then_favorable_price_level/v1",
        "intrabar_policy": "adverse_stop_first_and_canonical_first_on_shared_finalized_bar/v1",
        "accounting": "pro_rata_entry_fee_per_component_each_exit_fee_original_risk_quantity/v1",
        "specs": tuple(asdict(spec) for spec in STAGE2_CANDIDATE_SPECS),
    }
    return fingerprint(payload)


def _assert_frozen_bundle(bundle: CanonicalResearchBundle) -> None:
    dataset = bundle.dataset
    observed = {
        "manifest_id": dataset.manifest_id,
        "dataset_fingerprint": fingerprint(dataset),
        "strategy_fingerprint": fingerprint(bundle.strategy_config),
        "replay_fingerprint": fingerprint(bundle.replay_config),
        "backtest_fingerprint": fingerprint(bundle.backtest_config),
        "research_fingerprint": fingerprint(bundle.research_config),
        "split_fingerprint": fingerprint(bundle.split_config),
    }
    for name, expected in _EXPECTED_BINDINGS.items():
        if observed[name] != expected:
            raise ValueError(f"canonical Phase 7 bundle binding mismatch: {name}")
    stored = {
        "strategy_fingerprint": dataset.strategy_fingerprint,
        "replay_fingerprint": dataset.replay_fingerprint,
        "backtest_fingerprint": dataset.backtest_fingerprint,
        "research_fingerprint": dataset.research_fingerprint,
        "split_fingerprint": dataset.split_fingerprint,
    }
    manifest = dataset.manifest
    for name, value in stored.items():
        if value != observed[name] or getattr(manifest, name) != observed[name]:
            raise ValueError(f"canonical Phase 7 stored/live provenance mismatch: {name}")
    if _semantic_fingerprint() != _EXPECTED_SEMANTIC_FINGERPRINT:
        raise ValueError("Stage 2 candidate semantic fingerprint mismatch")
    if tuple(spec.candidate_id for spec in STAGE2_CANDIDATE_SPECS) != (
        "EXIT_FIXED_1R", "EXIT_FIXED_1_5R", "EXIT_FIXED_2R", "EXIT_FIXED_3R", "EXIT_FIXED_4R",
        "EXIT_PARTIAL50_1_5R_CANONICAL", "EXIT_PARTIAL50_2R_CANONICAL", "EXIT_PARTIAL50_3R_CANONICAL",
        "EXIT_BE_AFTER_1R", "EXIT_BE_AFTER_2R", "EXIT_PARTIAL50_2R_BE_RUNNER", "EXIT_PARTIAL50_3R_BE_RUNNER",
    ):
        raise ValueError("Stage 2 candidate ordering mismatch")


def _inside(window: ChronologicalWindow, timestamp: int) -> bool:
    return window.start_ms <= timestamp <= window.end_ms


def _development_population(bundle: CanonicalResearchBundle) -> tuple[tuple[object, ...], tuple[object, ...]]:
    setups = tuple(
        row for row in bundle.dataset.setup_rows
        if row.eligible_baseline_setup and _inside(DEVELOPMENT_WINDOW, row.decision_timestamp)
    )
    setup_by_id = {row.setup_id: row for row in setups}
    trades = tuple(
        row for row in bundle.dataset.trade_rows
        if row.outcome_state == "closed" and row.setup_id in setup_by_id
        and row.exit_timestamp is not None
        and _inside(DEVELOPMENT_WINDOW, row.decision_timestamp)
        and _inside(DEVELOPMENT_WINDOW, row.exit_timestamp)
    )
    ordered = tuple(sorted(trades, key=lambda row: (row.decision_timestamp, row.source_processing_key, row.trade_id)))
    if trades != ordered or len({row.trade_id for row in ordered}) != len(ordered):
        raise ValueError("development canonical trade order/identity mismatch")
    if len(setups) != 139 or len(ordered) != 103:
        raise ValueError("development population drift; expected 139 eligible setups and 103 closed trades")
    return setups, ordered


def _bars_by_open(bundle: CanonicalResearchBundle) -> dict[int, HistoricalBar]:
    bars = tuple(trace.source_bar for trace in bundle.replay.traces if trace.source_bar.timeframe is Timeframe.MINUTES_15)
    result = {bar.open_time: bar for bar in bars}
    if len(result) != len(bars):
        raise ValueError("replay has duplicate 15m source opens")
    return result


def _trade_input(row: object, bars_by_open: dict[int, HistoricalBar], bundle: CanonicalResearchBundle) -> CounterfactualTradeInput:
    # The object type is intentionally duck-typed here to keep the report layer
    # additive; field validation below makes an incomplete canonical row fail.
    exit_open = getattr(row, "exit_source_open_timestamp")
    expected = getattr(row, "expected_duration_bars")
    if isinstance(exit_open, bool) or not isinstance(exit_open, int) or isinstance(expected, bool) or not isinstance(expected, int) or expected <= 0:
        raise ValueError("closed canonical trade lacks a valid post-entry duration")
    source_open = getattr(row, "source_open_timestamp")
    opens = tuple(source_open + BAR_MS * offset for offset in range(1, expected + 1))
    if not opens or opens[-1] != exit_open:
        raise ValueError(f"canonical trace duration/exit mismatch for {getattr(row, 'trade_id')}")
    missing = tuple(open_time for open_time in opens if open_time not in bars_by_open)
    if missing:
        raise ValueError(f"canonical post-entry trace gap for {getattr(row, 'trade_id')}: {missing[0]}")
    sequence = tuple(bars_by_open[open_time] for open_time in opens)
    quantity = _finite(getattr(row, "quantity"), "canonical quantity")
    canonical_r = _finite(getattr(row, "realized_r"), "canonical realized_r")
    exit_timestamp = getattr(row, "exit_timestamp")
    exit_price = _finite(getattr(row, "canonical_exit_price"), "canonical exit price")
    exit_reason = getattr(row, "exit_primary_reason")
    if not isinstance(exit_timestamp, int) or isinstance(exit_timestamp, bool) or not isinstance(exit_reason, str):
        raise ValueError("closed canonical trade lacks exit identity")
    return CounterfactualTradeInput(
        setup_id=getattr(row, "setup_id"), trade_id=getattr(row, "trade_id"),
        entry_event_id=getattr(row, "entry_event_id"), symbol=getattr(row, "symbol"),
        side=getattr(row, "direction"), entry_timestamp=getattr(row, "decision_timestamp"),
        entry_source_open_timestamp=source_open, entry_price=_finite(getattr(row, "canonical_entry_price"), "entry price"),
        initial_stop=_finite(getattr(row, "canonical_stop_price"), "initial stop"),
        risk_per_unit=_finite(getattr(row, "canonical_risk_per_unit"), "risk per unit"), quantity=quantity,
        canonical_exit_timestamp=exit_timestamp, canonical_exit_source_open_timestamp=exit_open,
        canonical_exit_price=exit_price, canonical_exit_reason=exit_reason,
        canonical_realized_r=canonical_r, post_entry_bars=sequence,
        fee_bps=bundle.backtest_config.fee_bps, slippage_bps=bundle.backtest_config.slippage_bps,
        data_quality_flags=tuple(getattr(row, "data_quality_flags")),
    )


def _top_total(values: tuple[float, ...], count: int) -> float:
    return float(sum(sorted((value for value in values if value > 0.0), reverse=True)[:count]))


def _trade_metrics(
    records: tuple[dict[str, object], ...], *, eligible_setups: int,
) -> dict[str, object]:
    r_values = tuple(float(record["realized_r"]) for record in records)
    positive = tuple(value for value in r_values if value > 0.0)
    loss = tuple(value for value in r_values if value < 0.0)
    gross_profit = sum(positive)
    gross_loss = sum(loss)
    mae = tuple(float(record["mae_r"]) for record in records)
    mfe = tuple(float(record["mfe_r"]) for record in records)
    durations = tuple(int(record["duration_bars"]) for record in records)
    top5 = _top_total(r_values, 5)
    top10 = _top_total(r_values, 10)
    return {
        "eligible_setups": eligible_setups, "opened_trades": len(records), "closed_trades": len(records),
        "expectancy_r": None if not r_values else sum(r_values) / len(r_values), "total_r": float(sum(r_values)),
        "r_per_setup": None if eligible_setups == 0 else float(sum(r_values)) / eligible_setups,
        "profit_factor": None if gross_loss == 0.0 else gross_profit / abs(gross_loss),
        "win_rate": None if not r_values else len(positive) / len(r_values),
        "stop_rate": None if not records else sum(bool(record["stop_hit"]) for record in records) / len(records),
        "mean_r": None if not r_values else mean(r_values), "median_r": None if not r_values else median(r_values),
        "mean_mae_r": None if not mae else mean(mae), "mean_mfe_r": None if not mfe else mean(mfe),
        "mean_duration_bars": None if not durations else mean(durations),
        "median_duration_bars": None if not durations else median(durations),
        "tail_winners": {str(level): sum(value >= level for value in r_values) for level in (2.0, 3.0, 4.0, 5.0)},
        "maximum_realized_r": None if not r_values else max(r_values), "positive_r": float(gross_profit),
        "top5_winner_total_r": top5, "top10_winner_total_r": top10,
        "top5_positive_r_share": None if gross_profit == 0.0 else top5 / gross_profit,
        "top10_positive_r_share": None if gross_profit == 0.0 else top10 / gross_profit,
    }


def _ambiguities(results: tuple[CounterfactualTradeResult, ...]) -> dict[str, object]:
    categories: dict[str, tuple[str, ...]] = {
        "stop_and_tp": ("stop_and_favorable_target_same_bar_stop_first",),
        "break_even_conflict": (
            "break_even_trigger_and_new_stop_same_bar",
            "break_even_stop_and_canonical_exit_same_bar",
            "break_even_stop_and_favorable_target_same_bar",
        ),
        "candidate_and_canonical": ("canonical_exit_same_bar",),
        "resolved_conservatively_against_candidate": ("resolved_conservatively_against_candidate",),
    }
    values: dict[str, dict[str, object]] = {}
    for name, markers in categories.items():
        ids = tuple(sorted({
            result.trade_id for result in results
            if any(any(marker in flag for marker in markers) for flag in result.intrabar_ambiguity_flags)
        }))
        values[name] = {"count": len(ids), "trade_ids": ids}
    return values


def _canonical_records(rows: tuple[object, ...]) -> tuple[dict[str, object], ...]:
    return tuple({
        "trade_id": getattr(row, "trade_id"), "realized_r": _finite(getattr(row, "realized_r"), "realized_r"),
        "mae_r": _finite(getattr(row, "mae_r"), "mae_r"), "mfe_r": _finite(getattr(row, "mfe_r"), "mfe_r"),
        "duration_bars": getattr(row, "expected_duration_bars"), "stop_hit": getattr(row, "stop_hit"),
        "setup_id": getattr(row, "setup_id"), "decision_timestamp": getattr(row, "decision_timestamp"),
        "exit_timestamp": getattr(row, "exit_timestamp"),
    } for row in rows)


def _candidate_records(results: tuple[CounterfactualTradeResult, ...]) -> tuple[dict[str, object], ...]:
    return tuple({
        "trade_id": result.trade_id, "realized_r": result.combined_realized_r,
        "mae_r": result.diagnostic_mae_r, "mfe_r": result.diagnostic_mfe_r,
        "duration_bars": result.candidate_duration_bars,
        "stop_hit": result.runner_exit_reason in {"stop", "break_even_stop"},
        "setup_id": result.setup_id, "decision_timestamp": result.entry_timestamp,
        "exit_timestamp": result.runner_exit_timestamp,
    } for result in results)


def _window_metrics(
    *, window: ChronologicalWindow, setups: tuple[object, ...], rows: tuple[object, ...],
    results: tuple[CounterfactualTradeResult, ...],
) -> dict[str, object]:
    window_setups = tuple(row for row in setups if _inside(window, getattr(row, "decision_timestamp")))
    row_ids = {
        getattr(row, "trade_id") for row in rows
        if _inside(window, getattr(row, "decision_timestamp"))
        and _inside(window, getattr(row, "exit_timestamp"))
        and _inside(window, next(item for item in setups if getattr(item, "setup_id") == getattr(row, "setup_id")).decision_timestamp)
    }
    baseline_records = tuple(record for record in _canonical_records(rows) if record["trade_id"] in row_ids)
    candidate_records = tuple(record for record in _candidate_records(results) if record["trade_id"] in row_ids)
    baseline = _trade_metrics(baseline_records, eligible_setups=len(window_setups))
    candidate = _trade_metrics(candidate_records, eligible_setups=len(window_setups))
    return {
        "role": window.role, "start_date": window.start_date, "end_date": window.end_date,
        "canonical": baseline, "candidate": candidate,
        "delta_expectancy_r": None if baseline["expectancy_r"] is None or candidate["expectancy_r"] is None else candidate["expectancy_r"] - baseline["expectancy_r"],
        "delta_total_r": candidate["total_r"] - baseline["total_r"],
    }


def _gate(candidate: dict[str, object], canonical: dict[str, object], windows: tuple[dict[str, object], ...]) -> dict[str, object]:
    failures: list[str] = []
    economic_conditions = []
    for label in ("expectancy_r", "r_per_setup", "total_r"):
        economic_conditions.append(candidate[label] > canonical[label])
        if not candidate[label] > canonical[label]:
            failures.append(f"candidate {label} is not strictly greater than canonical")
    profit_factor_ok = candidate["profit_factor"] is not None and canonical["profit_factor"] is not None and candidate["profit_factor"] >= canonical["profit_factor"]
    economic_conditions.append(profit_factor_ok)
    if not profit_factor_ok:
        failures.append("candidate profit factor is worse than canonical")
    if candidate["positive_r"] < 0.70 * canonical["positive_r"]:
        failures.append("candidate positive-R retention is below 70%")
    improved = sum(window["delta_expectancy_r"] is not None and window["delta_expectancy_r"] > 0.0 for window in windows)
    degraded = sum(window["delta_expectancy_r"] is not None and window["delta_expectancy_r"] < 0.0 for window in windows)
    worst = min((window["delta_expectancy_r"] for window in windows if window["delta_expectancy_r"] is not None), default=None)
    best = max((window["delta_expectancy_r"] for window in windows if window["delta_expectancy_r"] is not None), default=None)
    if improved < degraded:
        failures.append("chronological windows improved fewer times than degraded")
    if worst is None or worst < -0.50:
        failures.append("worst chronological expectancy delta is below -0.50R")
    return {"gate_pass": not failures, "failed_reasons": tuple(failures), "windows_improved": improved,
            "windows_degraded": degraded, "worst_expectancy_delta_r": worst, "best_expectancy_delta_r": best,
            "no_win_rate_only": all(economic_conditions),
            "deterministic_leakage_provenance_correct": True}


def development_report(bundle: CanonicalResearchBundle) -> dict[str, object]:
    """Build the sole Stage 2 development artifact; validation/final OOS are absent."""
    _assert_frozen_bundle(bundle)
    setups, rows = _development_population(bundle)
    bars = _bars_by_open(bundle)
    inputs = tuple(_trade_input(row, bars, bundle) for row in rows)
    if len(inputs) != len(rows) or len({item.trade_id for item in inputs}) != len(inputs):
        raise ValueError("development trade/input identity mismatch")
    canonical_records = _canonical_records(rows)
    canonical = _trade_metrics(canonical_records, eligible_setups=len(setups))
    candidates: list[dict[str, object]] = []
    for spec in STAGE2_CANDIDATE_SPECS:
        results = tuple(simulate_counterfactual(input_, spec) for input_ in inputs)
        if len(results) != len(rows) or tuple(result.trade_id for result in results) != tuple(row.trade_id for row in rows):
            raise ValueError("candidate/canonical population mismatch")
        records = _candidate_records(results)
        metrics = _trade_metrics(records, eligible_setups=len(setups))
        metrics["positive_r_retention_ratio"] = None if canonical["positive_r"] == 0.0 else metrics["positive_r"] / canonical["positive_r"]
        if spec.partial_fraction == 0.5:
            retained = sum(
                row.realized_r is not None and row.realized_r >= 2.0
                and result.partial_exit_timestamp is not None and result.runner_exit_timestamp is not None
                and result.runner_exit_timestamp > result.partial_exit_timestamp
                for row, result in zip(rows, results, strict=True)
            )
            metrics["canonical_ge_2r_winners_with_runner_exposure"] = retained
            metrics["runner_exposure_definition"] = "partial TP occurred and 50% runner exited on a later source bar"
        windows = tuple(_window_metrics(window=window, setups=setups, rows=rows, results=results) for window in DEVELOPMENT_SUBWINDOWS)
        candidates.append({
            "candidate_id": spec.candidate_id, "family": spec.family, "parameters": asdict(spec),
            "metrics": metrics, "ambiguity_audit": _ambiguities(results), "subwindows": windows,
            "gate": _gate(metrics, canonical, windows), "per_trade": tuple(asdict(result) for result in results),
        })
    core = {
        "schema_version": EXIT_DEVELOPMENT_SCHEMA_VERSION, "role": "development",
        "inaccessible_roles": ("validation", "final_oos"), "validation_accessed": False,
        "final_oos_accessed": False, "production_change": False,
        "development_window": {"start_date": DEVELOPMENT_WINDOW.start_date, "end_date": DEVELOPMENT_WINDOW.end_date},
        "provenance": {**_EXPECTED_BINDINGS, "candidate_semantic_fingerprint": _semantic_fingerprint()},
        "population": {"eligible_setups": len(setups), "opened_trades": len(rows), "closed_trades": len(rows),
                       "trade_ids": tuple(row.trade_id for row in rows)},
        "canonical": {"metrics": canonical, "per_trade": canonical_records},
        "candidates": tuple(candidates),
    }
    return {**core, "report_fingerprint": fingerprint(core)}


def development_report_json(report: dict[str, object]) -> bytes:
    return json.dumps(report, sort_keys=True, separators=(",", ":"), allow_nan=False).encode("utf-8") + b"\n"


def write_development_report(report: dict[str, object], output: Path, *, overwrite: bool = False) -> None:
    if output.exists() and not overwrite:
        raise FileExistsError("refusing to overwrite existing development report")
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_bytes(development_report_json(report))
