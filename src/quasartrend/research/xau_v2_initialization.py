"""Immutable V2 XAU research initialization boundary.

This module deliberately does not invoke the strategy engine or inspect any
forward prices.  It freezes the V2 research universe and admits economics only
from the identified pre-cutoff historical artifact.
"""
from __future__ import annotations

from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from hashlib import sha256
import json
import math
from pathlib import Path
import subprocess
from typing import Any, Iterable, Mapping

from .provenance import canonical_json
from .xm_gold_historical_validation import (
    FROZEN_PINESCRIPT_SOURCE_SHA256, FROZEN_PRODUCTION_SOURCE_MANIFEST_SHA256,
    FROZEN_PRODUCTION_SOURCE_SHA256, verify_frozen_production_sources,
)


V2_VERSION = "xau-v2-initialization/v1"
CANONICAL_ORIGIN_MAIN_SHA = "c58e18ef545909184267342eff712dd08bf47dda"
HISTORICAL_ARTIFACT_PATH = "exports/xm/phase_xm_gold_historical_validation.json"
HISTORICAL_ARTIFACT_SHA256 = "5e81a9ad805496e0a3d8578821485f9469998cf9d2f37bff2b9b5e4be1670d0d"
DEVELOPMENT_END_EXCLUSIVE_UTC = "2026-03-01T23:00:00Z"
RESERVED_HOLDOUT_START_UTC = "2026-08-28T20:58:00Z"
RESERVED_HOLDOUT_END_EXCLUSIVE_UTC = "2027-03-01T23:00:00Z"
GLOBAL_ELIGIBLE_SETUPS = 1072
EXPECTED_V2_CONFIG_SHA256 = "28ef625ebac6b31e22ce43a07678342439d55a9de0ede3a338a8ec0d3c0e2d46"
STRATEGY_ELIGIBILITY_START_UTC = "2024-03-20T22:15:00Z"
LEDGER_FIRST_SETUP_ORIGIN_UTC = "2024-03-21T19:00:00Z"
LEDGER_FIRST_ENTRY_UTC = "2024-03-22T01:00:00Z"
LEDGER_LAST_CLOSED_EXIT_UTC = "2026-02-27T12:00:00Z"


def _millis(value: str) -> int:
    return int(datetime.fromisoformat(value.replace("Z", "+00:00")).timestamp() * 1000)


DEVELOPMENT_END_EXCLUSIVE_MS = _millis(DEVELOPMENT_END_EXCLUSIVE_UTC)
RESERVED_HOLDOUT_START_MS = _millis(RESERVED_HOLDOUT_START_UTC)
RESERVED_HOLDOUT_END_EXCLUSIVE_MS = _millis(RESERVED_HOLDOUT_END_EXCLUSIVE_UTC)


@dataclass(frozen=True, slots=True)
class ExpandingFold:
    fold_id: str
    train_start: str
    train_end_exclusive: str
    validation_start: str
    validation_end_exclusive: str
    expected_train_originated: int
    expected_train_scored: int
    expected_validation_originated: int
    expected_validation_scored: int
    expected_validation_censored: int

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)


FOLDS = (
    ExpandingFold("F1", STRATEGY_ELIGIBILITY_START_UTC, "2024-08-01T00:00:00Z", "2024-08-01T00:00:00Z", "2024-12-01T00:00:00Z", 152, 152, 143, 143, 0),
    ExpandingFold("F2", STRATEGY_ELIGIBILITY_START_UTC, "2024-12-01T00:00:00Z", "2024-12-01T00:00:00Z", "2025-04-01T00:00:00Z", 295, 295, 139, 138, 1),
    ExpandingFold("F3", STRATEGY_ELIGIBILITY_START_UTC, "2025-04-01T00:00:00Z", "2025-04-01T00:00:00Z", "2025-08-01T00:00:00Z", 434, 433, 147, 146, 1),
    ExpandingFold("F4", STRATEGY_ELIGIBILITY_START_UTC, "2025-08-01T00:00:00Z", "2025-08-01T00:00:00Z", "2025-12-01T00:00:00Z", 581, 580, 146, 146, 0),
    ExpandingFold("F5", STRATEGY_ELIGIBILITY_START_UTC, "2025-12-01T00:00:00Z", "2025-12-01T00:00:00Z", DEVELOPMENT_END_EXCLUSIVE_UTC, 727, 727, 93, 93, 0),
)


@dataclass(frozen=True, slots=True)
class V2ResearchConfig:
    """V2-only candidate declaration; it does not alter ``StrategyConfig``."""

    version: str = V2_VERSION
    baseline_origin_main_sha: str = CANONICAL_ORIGIN_MAIN_SHA
    development_end_exclusive_utc: str = DEVELOPMENT_END_EXCLUSIVE_UTC
    directional_candidates: tuple[str, ...] = ("both", "long_only", "short_only")
    sessions_utc: tuple[tuple[str, int, int], ...] = (
        ("all_utc", 0, 24), ("asia", 0, 8), ("london", 7, 16),
        ("new_york", 12, 21), ("london_new_york_overlap", 12, 16),
    )
    adr_max_current_daily_range_over_prior_14d_adr: tuple[float | None, ...] = (
        None, 0.75, 1.0, 1.25,
    )
    minimum_alignment_duration_finalized_m15_bars: tuple[int, ...] = (0, 2, 4, 8)
    confirmation_delay_finalized_m15_bars: tuple[int, ...] = (0, 1, 2)
    atr_stop_multipliers: tuple[float, ...] = (0.75, 1.0, 1.25, 1.5)
    fixed_profit_target_r: tuple[float | None, ...] = (None, 2.0, 3.0)
    trailing_variants: tuple[str, ...] = (
        "none", "activate_1r_trail_1r", "activate_2r_trail_1r",
    )
    stale_duration_bars: tuple[str | int, ...] = ("none", 32, 64, 128)
    armed_setup_expiry_bars: tuple[str | int, ...] = ("none", 1, 2, 4, 8)

    def fingerprint(self) -> str:
        return sha256(canonical_json(asdict(self)).encode()).hexdigest()


@dataclass(frozen=True, slots=True)
class Candidate:
    family: str
    candidate_id: str
    value: Any
    economic_rationale: str


def enumerate_candidates(config: V2ResearchConfig = V2ResearchConfig()) -> tuple[Candidate, ...]:
    """Enumerate sequential families, never their Cartesian product."""
    families: tuple[tuple[str, Iterable[Any], str], ...] = (
        ("directional_asymmetry", config.directional_candidates, "Directional exposure can differ while preserving the frozen setup semantics."),
        ("sessions_utc", config.sessions_utc, "Liquidity and scheduled-market structure vary by UTC session."),
        ("adr_max_current_daily_range_over_prior_14d_adr", config.adr_max_current_daily_range_over_prior_14d_adr, "A broad monotonic maximum-ADR gate may reject entries after the daily move is already extended."),
        ("minimum_alignment_duration_finalized_m15_bars", config.minimum_alignment_duration_finalized_m15_bars, "Existing HEMA/Kalman/Supertrend alignment duration may distinguish established causal state from a fresh weak alignment."),
        ("confirmation_delay_finalized_m15_bars", config.confirmation_delay_finalized_m15_bars, "A small fixed delay on completed M15 bars may reject transient alignment without future-bar access."),
        ("atr_stop_multiplier", config.atr_stop_multipliers, "A broad volatility-normalized stop neighborhood tests risk containment independently of entry filters."),
        ("fixed_profit_target_r", config.fixed_profit_target_r, "Simple fixed-R profit management is evaluated separately from stop and entry changes."),
        ("trailing_variant", config.trailing_variants, "A small set of deterministic R-based trails is evaluated separately from other exit changes."),
        ("maximum_trade_duration_finalized_m15_bars", config.stale_duration_bars, "A prospectively long-held open trade may need a fixed causal time stop."),
        ("armed_setup_expiry_finalized_m15_bars", config.armed_setup_expiry_bars, "Armed-entry expiry is a separate entry subexperiment, never conflated with trade duration."),
    )
    result: list[Candidate] = []
    for family, values, rationale in families:
        for ordinal, value in enumerate(values, 1):
            encoded = canonical_json(value)
            result.append(Candidate(family, f"{family}:{ordinal}:{encoded}", value, rationale))
    return tuple(result)


def assert_v2_config_frozen(config: V2ResearchConfig) -> None:
    if config.fingerprint() != EXPECTED_V2_CONFIG_SHA256:
        raise ValueError("V2 research configuration differs from the frozen predeclared candidate inventory")


def _range_intersects(start_ms: int, end_exclusive_ms: int, other_start_ms: int, other_end_exclusive_ms: int) -> bool:
    return start_ms < other_end_exclusive_ms and other_start_ms < end_exclusive_ms


def assert_economic_range_allowed(start_ms: int, end_exclusive_ms: int) -> None:
    """Fail closed for malformed ranges, future development, and reserved holdout."""
    if isinstance(start_ms, bool) or isinstance(end_exclusive_ms, bool) or not isinstance(start_ms, int) or not isinstance(end_exclusive_ms, int):
        raise TypeError("economic ranges require integer epoch milliseconds")
    if end_exclusive_ms <= start_ms:
        raise ValueError("economic range must be non-empty and half-open")
    if _range_intersects(start_ms, end_exclusive_ms, RESERVED_HOLDOUT_START_MS, RESERVED_HOLDOUT_END_EXCLUSIVE_MS):
        raise ValueError("economic evaluation intersects the reserved holdout")
    if end_exclusive_ms > DEVELOPMENT_END_EXCLUSIVE_MS:
        raise ValueError("economic evaluation exceeds frozen development end")


def verify_historical_artifact(path: Path) -> Mapping[str, Any]:
    """Verify bytes before parsing the sole admitted historical economics."""
    actual = sha256(path.read_bytes()).hexdigest()
    if actual != HISTORICAL_ARTIFACT_SHA256:
        raise ValueError("historical artifact identity mismatch")
    value = json.loads(path.read_bytes())
    if not isinstance(value, dict):
        raise ValueError("historical artifact must be a JSON object")
    return value


def admit_development_economic_ledger(
    path: Path, config: V2ResearchConfig = V2ResearchConfig(),
) -> tuple[Mapping[str, Any], tuple[Mapping[str, Any], ...]]:
    """The sole V2 economic-data admission boundary.

    Forward quote/signal capture deliberately lives in a separate module and
    cannot pass this frozen historical-artifact identity gate.
    """
    assert_v2_config_frozen(config)
    artifact = verify_historical_artifact(path)
    ledger = artifact.get("closed_trade_ledger")
    if not isinstance(ledger, list):
        raise ValueError("frozen historical artifact lacks closed-trade ledger")
    validate_fold_population(ledger)
    for row in ledger:
        assert_economic_range_allowed(row["setup_origin_timestamp"], row["exit_timestamp"] + 1)
    return artifact, tuple(ledger)


def verify_v1_identity(repo_root: Path) -> dict[str, Any]:
    """Verify current V1 source bytes and that HEAD descends from frozen main."""
    root = repo_root.resolve()
    try:
        def git(*args: str) -> str:
            return subprocess.run(("git", *args), cwd=root, check=True, capture_output=True, text=True).stdout.strip()
        head, main, origin_main = git("rev-parse", "HEAD"), git("rev-parse", "main"), git("rev-parse", "origin/main")
        ancestry = subprocess.run(("git", "merge-base", "--is-ancestor", CANONICAL_ORIGIN_MAIN_SHA, "HEAD"), cwd=root).returncode == 0
    except subprocess.CalledProcessError as error:
        raise ValueError("V1 Git identity cannot be verified") from error
    if main != CANONICAL_ORIGIN_MAIN_SHA or origin_main != CANONICAL_ORIGIN_MAIN_SHA or not ancestry:
        raise ValueError("V1 main/origin-main identity or HEAD ancestry mismatch")
    hashes = verify_frozen_production_sources(root)
    return {"head": head, "main": main, "origin_main": origin_main, "head_descends_from_origin_main": ancestry, "source_hashes": hashes}


def validate_fold_population(ledger: Iterable[Mapping[str, Any]]) -> dict[str, int]:
    rows = tuple(ledger)
    origins: list[int] = []
    trade_ids: set[str] = set()
    for row in rows:
        trade_id = row.get("trade_id")
        if not isinstance(trade_id, str) or not trade_id or trade_id in trade_ids:
            raise ValueError("frozen ledger trade IDs must be non-empty and unique")
        trade_ids.add(trade_id)
        if row.get("outcome") != "closed" or row.get("direction") not in {"long", "short"}:
            raise ValueError("frozen ledger requires closed long/short trades")
        r_value = row.get("r")
        if isinstance(r_value, bool) or not isinstance(r_value, (int, float)) or not math.isfinite(r_value):
            raise ValueError("frozen ledger R must be finite")
        timestamp = row.get("setup_origin_timestamp")
        if isinstance(timestamp, bool) or not isinstance(timestamp, int):
            raise ValueError("frozen ledger lacks setup-origin UTC timestamps")
        origins.append(timestamp)
        for field in ("setup_origin_timestamp", "entry_timestamp", "exit_timestamp"):
            observed = row.get(field)
            if isinstance(observed, bool) or not isinstance(observed, int) or observed >= DEVELOPMENT_END_EXCLUSIVE_MS:
                raise ValueError("ledger observation reaches frozen development cutoff")
        if not row["setup_origin_timestamp"] <= row["entry_timestamp"] <= row["exit_timestamp"]:
            raise ValueError("ledger timestamps must satisfy setup <= entry <= exit")
    if not origins or min(origins) < _millis(STRATEGY_ELIGIBILITY_START_UTC):
        raise ValueError("ledger setup origin precedes frozen strategy eligibility")
    first_by_entry = min(rows, key=lambda row: (row["entry_timestamp"], row["trade_id"]))
    last_by_exit = max(rows, key=lambda row: (row["exit_timestamp"], row["trade_id"]))
    if (
        min(origins) != _millis(LEDGER_FIRST_SETUP_ORIGIN_UTC)
        or first_by_entry["entry_timestamp"] != _millis(LEDGER_FIRST_ENTRY_UTC)
        or last_by_exit["exit_timestamp"] != _millis(LEDGER_LAST_CLOSED_EXIT_UTC)
    ):
        raise ValueError("ledger coverage differs from the frozen development inventory")
    counts: dict[str, int] = {}
    previous_validation_end: int | None = None
    for fold in FOLDS:
        train_end = _millis(fold.train_end_exclusive)
        validation_start = _millis(fold.validation_start)
        validation_end = _millis(fold.validation_end_exclusive)
        if not train_end == validation_start or (previous_validation_end is not None and validation_start != previous_validation_end):
            raise ValueError("folds must be contiguous with strictly earlier training")
        train_originated = sum(origin < train_end for origin in origins)
        train_scored = sum(row["setup_origin_timestamp"] < train_end and row["exit_timestamp"] < train_end for row in rows)
        validation_originated = sum(validation_start <= origin < validation_end for origin in origins)
        validation_scored = sum(validation_start <= row["setup_origin_timestamp"] < validation_end and row["exit_timestamp"] < validation_end for row in rows)
        validation_censored = validation_originated - validation_scored
        if (
            train_originated != fold.expected_train_originated
            or train_scored != fold.expected_train_scored
            or validation_originated != fold.expected_validation_originated
            or validation_scored != fold.expected_validation_scored
            or validation_censored != fold.expected_validation_censored
        ):
            raise ValueError("frozen fold population accounting mismatch")
        counts[f"{fold.fold_id}_train_originated"] = train_originated
        counts[f"{fold.fold_id}_train_scored"] = train_scored
        counts[f"{fold.fold_id}_validation_originated"] = validation_originated
        counts[f"{fold.fold_id}_validation_scored"] = validation_scored
        counts[f"{fold.fold_id}_validation_censored"] = validation_censored
        previous_validation_end = validation_end
    if (
        sum(counts[f"F{index}_validation_originated"] for index in range(1, 6)) != 668
        or sum(counts[f"F{index}_validation_scored"] for index in range(1, 6)) != 666
        or sum(counts[f"F{index}_validation_censored"] for index in range(1, 6)) != 2
        or len(origins) != 820
    ):
        raise ValueError("combined OOS population accounting mismatch")
    return counts


def protocol_inventory(config: V2ResearchConfig = V2ResearchConfig()) -> dict[str, Any]:
    assert_v2_config_frozen(config)
    return {
        "schema_version": V2_VERSION,
        "canonical_baseline_origin_main_sha": CANONICAL_ORIGIN_MAIN_SHA,
        "historical_artifact": {"path": HISTORICAL_ARTIFACT_PATH, "sha256": HISTORICAL_ARTIFACT_SHA256},
        "development_data": {
            "admitted_economics_source": "frozen V1 historical closed-trade ledger only",
            "strategy_eligibility_start_utc": STRATEGY_ELIGIBILITY_START_UTC,
            "first_setup_origin_utc": LEDGER_FIRST_SETUP_ORIGIN_UTC,
            "first_entry_utc": LEDGER_FIRST_ENTRY_UTC,
            "last_closed_exit_utc": LEDGER_LAST_CLOSED_EXIT_UTC,
            "development_end_exclusive_utc": DEVELOPMENT_END_EXCLUSIVE_UTC,
            "closed_trades": 820,
            "eligible_setups_global": GLOBAL_ELIGIBLE_SETUPS,
            "post_cutoff_strategy_economics": "prohibited",
            "forward_tick_evidence": "capture/provenance use only; not admitted for V2 economics",
        },
        "development_end_exclusive_utc": DEVELOPMENT_END_EXCLUSIVE_UTC,
        "reserved_holdout": {"start": RESERVED_HOLDOUT_START_UTC, "end_exclusive": RESERVED_HOLDOUT_END_EXCLUSIVE_UTC, "economics": "prohibited; raw forward capture inventory only"},
        "folds": [fold.as_dict() for fold in FOLDS],
        "population_accounting": {
            "initial_train_originated": 152,
            "validation_originated": [143, 139, 147, 146, 93],
            "combined_oos_originated": 668,
            "validation_scored_after_boundary_censoring": [143, 138, 146, 146, 93],
            "combined_oos_scored": 666,
            "cross_boundary_censored": 2,
            "all_development_closed": 820,
            "eligible_setups": GLOBAL_ELIGIBLE_SETUPS,
        },
        "v1_config": {"status": "frozen and unchanged", "parameter_selection": "none for V1 baseline", "source_manifest_sha256": FROZEN_PRODUCTION_SOURCE_MANIFEST_SHA256, "production_source_sha256": dict(FROZEN_PRODUCTION_SOURCE_SHA256), "pinescript_source_sha256": dict(FROZEN_PINESCRIPT_SOURCE_SHA256), "configuration": {"ltf_hema": {"fast_length": 20, "slow_length": 40, "timeframe": "15m"}, "htf_hema": {"fast_length": 20, "slow_length": 40, "timeframe": "4h"}, "kalman": {"period": 21, "alpha": 0.01, "beta": 0.1, "factor": 1.0, "atr_period": 7}, "strategy": {"atr_multiplier": 1.0, "confirmation_mode": "stateful_either_order", "bias_reversal_behavior": "exit"}}},
        "fold_semantics": {"membership": "setup_origin_timestamp UTC", "intervals": "half-open", "training": "setup originated before validation start AND outcome closed strictly before validation start", "validation": "setup originated within the validation interval AND outcome closed strictly before validation end", "cross_boundary": "censor from that fold; never reassign to a different OOS fold; it may enter later training only after its exit is chronologically available", "rationale": "expanding training preserves chronological causality while retaining each validation interval untouched until train-only deterministic selection is frozen"},
        "candidate_policy": {"sequential_families_only": True, "cartesian_product": "prohibited", "economic_data_admission": "admit_development_economic_ledger only; exact historical artifact and exact frozen V2 config required", "selection": "deterministic and train-only; all candidates/results retained; OOS untouched until selection is frozen", "next_experiment": "directional_asymmetry", "next_experiment_status": "predeclared; not run; no winner claimed"},
        "next_experiment_specification": {
            "family": "directional_asymmetry",
            "candidates_in_fixed_tie_order": ["both", "long_only", "short_only"],
            "per_fold_training_population": "only setup origins before validation start whose exits closed strictly before validation start",
            "minimum_training_closed_trades": 50,
            "training_rank": ["expectancy_r descending", "profit_factor descending", "closed_trades descending", "fixed candidate tie order ascending"],
            "selection_lock": "serialize the selected candidate and every training result before opening that fold's OOS metrics",
            "per_fold_oos_population": "selected candidate only; setup origin inside validation interval and exit strictly before validation end",
            "report_after_selection_lock": "retain every candidate result plus selected-candidate OOS; mark any OOS direction cell below 30 closed trades small-sample",
            "permanent_direction_removal": "not authorized by one aggregate or by this experiment alone; require cross-fold stability and Sol/main review",
        },
        "v2_config_hash": config.fingerprint(),
        "candidate_inventory": [asdict(candidate) for candidate in enumerate_candidates(config)],
    }


def _percentile_type7(values: list[int], percentile: float) -> float:
    ordered = sorted(values)
    position = (len(ordered) - 1) * percentile / 100.0
    lower = int(position)
    upper = min(lower + 1, len(ordered) - 1)
    return ordered[lower] + (ordered[upper] - ordered[lower]) * (position - lower)


def _closed_metrics(rows: Iterable[Mapping[str, Any]]) -> dict[str, Any]:
    """Small, explicit metric set used only to summarize the frozen ledger."""
    ordered = sorted(rows, key=lambda row: (row["exit_timestamp"], row["trade_id"]))
    values = [float(row["r"]) for row in ordered]
    wins, losses = [value for value in values if value > 0], [value for value in values if value < 0]
    total = sum(values)
    positive, negative = sum(wins), -sum(losses)
    curve: list[dict[str, Any]] = []
    cumulative = peak = 0.0
    maximum_drawdown = 0.0
    longest_losing = current_losing = 0
    durations = []
    long_count = short_count = 0
    for ordinal, (row, value) in enumerate(zip(ordered, values), 1):
        cumulative += value
        peak = max(peak, cumulative)
        maximum_drawdown = max(maximum_drawdown, peak - cumulative)
        if value < 0:
            current_losing += 1
            longest_losing = max(longest_losing, current_losing)
        else:
            current_losing = 0
        durations.append(row["exit_timestamp"] - row["entry_timestamp"])
        if row["direction"] == "long":
            long_count += 1
        else:
            short_count += 1
        curve.append({"ordinal": ordinal, "trade_id": row["trade_id"], "exit_timestamp": row["exit_timestamp"], "cumulative_r": cumulative})
    count = len(values)
    first = ordered[0]["setup_origin_timestamp"] if ordered else None
    last = ordered[-1]["setup_origin_timestamp"] if ordered else None
    span_days = None if first is None or last is None else max((last - first) / 86_400_000, 1.0)
    return {
        "closed": count, "long": long_count, "short": short_count, "wins": len(wins), "losses": len(losses),
        "total_r": total, "expectancy_r": None if not count else total / count,
        "profit_factor": None if not losses else positive / negative,
        "win_rate": None if not count else len(wins) / count,
        "average_win_r": None if not wins else positive / len(wins), "max_win_r": None if not wins else max(wins),
        "average_loss_r": None if not losses else sum(losses) / len(losses), "max_loss_r": None if not losses else min(losses),
        "maximum_drawdown_r": maximum_drawdown, "longest_losing_streak": longest_losing,
        "duration_ms": {"mean": None if not durations else sum(durations) / len(durations), "median": None if not durations else sorted(durations)[(len(durations) - 1) // 2] if len(durations) % 2 else (sorted(durations)[len(durations)//2 - 1] + sorted(durations)[len(durations)//2]) / 2, "p90": None if not durations else _percentile_type7(durations, 90), "min": None if not durations else min(durations), "max": None if not durations else max(durations)},
        "trade_density_closed_per_day": None if span_days is None else count / span_days,
        "chronological_cumulative_r": curve,
    }


def build_baseline_walk_forward(artifact_path: Path) -> dict[str, Any]:
    """Summarize the V1 frozen ledger; no V2 candidate is selected or evaluated."""
    artifact, admitted = admit_development_economic_ledger(artifact_path)
    ledger = list(admitted)
    by_origin = lambda start, end: [row for row in ledger if start <= row["setup_origin_timestamp"] < end]
    strategy_start = min(row["setup_origin_timestamp"] for row in ledger)
    fold_reports = []
    for fold in FOLDS:
        train_end, validate_start, validate_end = (_millis(fold.train_end_exclusive), _millis(fold.validation_start), _millis(fold.validation_end_exclusive))
        train = [row for row in by_origin(strategy_start, train_end) if row["exit_timestamp"] < train_end]
        validation_originated = by_origin(validate_start, validate_end)
        validation = [row for row in validation_originated if row["exit_timestamp"] < validate_end]
        censored = [row for row in validation_originated if row["exit_timestamp"] >= validate_end]
        fold_reports.append({
            "fold": fold.as_dict(),
            "in_sample": {**_closed_metrics(train), "eligible_setups": None, "r_per_setup": None, "eligibility_note": "unavailable: frozen artifact has no setup ledger"},
            "out_of_sample": {**_closed_metrics(validation), "eligible_setups": None, "r_per_setup": None, "eligibility_note": "unavailable: frozen artifact has no setup ledger"},
            "cross_boundary_censored": [{"trade_id": row["trade_id"], "setup_origin_timestamp": row["setup_origin_timestamp"], "exit_timestamp": row["exit_timestamp"]} for row in censored],
        })
    aggregate = dict(artifact["aggregate"])
    # Retain canonical aggregate values byte-for-value rather than recomputing
    # their summation order.  Supplemental fields are derived from that ledger.
    aggregate.update(_closed_metrics(ledger))
    aggregate.update({key: artifact["aggregate"][key] for key in ("total_r", "expectancy_r", "profit_factor", "win_rate")})
    aggregate["eligible_setups"] = GLOBAL_ELIGIBLE_SETUPS
    aggregate["r_per_setup"] = artifact["aggregate"]["r_per_setup"]
    oos = [row for fold in FOLDS for row in by_origin(_millis(fold.validation_start), _millis(fold.validation_end_exclusive)) if row["exit_timestamp"] < _millis(fold.validation_end_exclusive)]
    return {
        "schema_version": "xau-v2-baseline-walk-forward/v1",
        "historical_artifact": {"path": HISTORICAL_ARTIFACT_PATH, "sha256": HISTORICAL_ARTIFACT_SHA256},
        "scope": "V1 baseline summary solely from the frozen pre-cutoff historical ledger",
        "parameter_selection": "none; V1 baseline is not a candidate-selection result",
        "holdout": "not read; no reserved-window economics evaluated",
        "population": {"global_eligible_setups": GLOBAL_ELIGIBLE_SETUPS, "closed_trade_ledger": len(ledger), "per_fold_eligible_setup_and_r_per_setup": "unavailable: frozen artifact has no setup ledger"},
        "aggregate": aggregate,
        "folds": fold_reports,
        "combined_oos": {**_closed_metrics(oos), "originated": 668, "closed_scored_expected": 666, "cross_boundary_censored": 2, "eligible_setups": None, "r_per_setup": None, "eligibility_note": "unavailable: frozen artifact has no setup ledger"},
    }


def forward_capture_metadata() -> dict[str, Any]:
    return {
        "schema_version": "xm-forward-capture-metadata/v1",
        "existing_backfill": {
            "path": "golden/GOLD_ticks.csv", "sha256": "7042dba43df8fa5d4c62036a0dcbe67a6414a6ef5ffddf09a02cb25b6843e5a3",
            "rows": 44_773_688, "approximate_size": "3.1GB", "repository_status": "untracked immutable evidence", "coverage": {"first": "2026-03-17T17:13:33.879Z", "last": "2026-08-31T23:01:48.891Z"},
            "source_server": "XMGlobal-MT5 18", "symbol": "GOLD", "classification": "immutable evidence only; current exporter is a bounded immutable snapshot/backfill, not append-only forward capture",
        },
        "reserved_holdout": {"start": RESERVED_HOLDOUT_START_UTC, "end_exclusive": RESERVED_HOLDOUT_END_EXCLUSIVE_UTC, "price_economics": "not read or evaluated"},
        "current_checkpoint": {
            "append_only_journal": "implemented",
            "journal_provenance": ["source_server", "symbol", "capture_version", "lowercase SHA-256 pseudonymous terminal/account environment_id"],
            "credentials_or_raw_account_id_persisted": False,
            "terminal_scheduler_continuous_ingestion": "unconnected",
            "demo_adapter": "unconnected",
        },
    }


def forward_signal_protocol() -> dict[str, Any]:
    return {
        "schema_version": "xm-forward-signal-protocol/v1",
        "journal_schema": "xm-forward-signal-journal/v1",
        "required": ["source_server", "symbol", "environment_id", "signal_id", "strategy_version", "config_hash", "bar_timestamp", "signal_timestamp", "direction", "theoretical_bid_entry", "stop", "intended_exit_logic", "frozen_state"],
        "environment_id": "required lowercase SHA-256 pseudonymous terminal/account identity; credentials and raw account identifiers prohibited",
        "prohibited": ["outcome", "pnl", "r", "gross_strategy_r", "net_observed_r", "exit_price", "realized"],
        "execution": "demo-only guard; no broker calls or order placement at this checkpoint",
    }


def _write_exact_protocol(path: Path, payload: Mapping[str, Any]) -> None:
    data = (canonical_json(payload) + "\n").encode()
    if path.exists() and path.read_bytes() != data:
        raise ValueError("refusing to overwrite a protocol with different bytes")
    if not path.exists():
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(data)


def regenerate_v2_artifacts(repo_root: Path) -> dict[str, Path]:
    """Deterministically regenerate result artifacts and only create/verify protocols."""
    root = repo_root.resolve()
    verify_v1_identity(root)
    exports = root / "exports/xm"
    protocol = exports / "v2_walk_forward_protocol.json"
    signal_protocol = exports / "xm_forward_signal_protocol.json"
    _write_exact_protocol(protocol, protocol_inventory())
    _write_exact_protocol(signal_protocol, forward_signal_protocol())
    baseline = exports / "v2_baseline_walk_forward.json"
    metadata = exports / "xm_forward_capture_metadata.json"
    baseline.write_text(canonical_json(build_baseline_walk_forward(root / HISTORICAL_ARTIFACT_PATH)) + "\n", encoding="utf-8")
    metadata.write_text(canonical_json(forward_capture_metadata()) + "\n", encoding="utf-8")
    return {"protocol": protocol, "baseline": baseline, "metadata": metadata, "signal_protocol": signal_protocol}
