"""Frozen, staged Family 1 directional-asymmetry walk-forward research.

This module intentionally contains no strategy implementation.  It applies the
three predeclared direction policies to the immutable setup-context and closed
trade ledgers admitted by :mod:`xau_v2_initialization`.
"""
from __future__ import annotations

from dataclasses import asdict, dataclass, field
from hashlib import sha256
import csv
from datetime import UTC, datetime
import io
import json
import math
from pathlib import Path
import subprocess
from types import MappingProxyType
from typing import Any, Callable, Iterable, Mapping, Sequence

from .provenance import canonical_json
from .xau_v2_initialization import (
    DEVELOPMENT_END_EXCLUSIVE_MS,
    FOLDS,
    GLOBAL_ELIGIBLE_SETUPS,
    HISTORICAL_ARTIFACT_PATH,
    HISTORICAL_ARTIFACT_SHA256,
    STRATEGY_ELIGIBILITY_START_UTC,
    V2ResearchConfig,
    admit_development_economic_ledger,
    assert_economic_range_allowed,
    enumerate_candidates,
    validate_fold_population,
    verify_v1_identity,
)


FAMILY = "directional_asymmetry"
SCHEMA_VERSION = "xau-v2-family-1-directional-asymmetry/v1"
PREOPTIMIZATION_COMMIT_SHA = "e83186040f7568bab5ed49a70cb63610a6403d68"
V2_PROTOCOL_PATH = "exports/xm/v2_walk_forward_protocol.json"
V2_PROTOCOL_SHA256 = "f27fa8a0c7d68f1edba5879dd2d4054a9ecf1dce04246e038c55f32a0cabba20"
CONTEXT_ARTIFACT_PATH = "exports/xm/phase_xau_directional_edge_attribution.json"
CONTEXT_ARTIFACT_SHA256 = "747402a144eaab959b7dc2d6432c0c894f5bc2104833bcd98851cc1188030e3d"
LOCK_DIRECTORY = "exports/xm/v2_family_1_directional_asymmetry_locks"
RESULT_PATH = "exports/xm/v2_family_1_directional_asymmetry.json"
CSV_PATH = "exports/xm/v2_family_1_directional_asymmetry_trades.csv"
CANDIDATES = ("both", "long_only", "short_only")
MINIMUM_TRAINING_CLOSED = 50
MS_PER_DAY = 86_400_000
SMALL_SAMPLE_WARNING = "SMALL-SAMPLE / DESCRIPTIVE ONLY"
IMPLEMENTATION_HASH_KEYS = (
    "family1_module_sha256",
    "family1_runner_sha256",
    "xau_v2_initialization_sha256",
    "provenance_sha256",
)
CLASSIFICATION_RULE = {
    "promising": {
        "walk_forward_fold_count": 5,
        "same_non_both_policy_selected_at_least_folds": 4,
        "selected_expectancy_r": "> matched_both",
        "selected_profit_factor": "> matched_both",
        "selected_total_r": "> matched_both",
        "selected_closed_trades": ">= 50% matched_both",
        "selected_maximum_drawdown_r": "<= 1.25x matched_both",
        "positive_incremental_r_folds": ">= 3",
        "every_selected_fold_expectancy_r": "> -0.50",
        "largest_single_fold_positive_incremental_r": "<= 60% of total positive incremental R",
    },
    "not_justified": [
        "selected expectancy_r <= matched_both",
        "selected profit_factor <= matched_both",
        "selected total_r <= matched_both",
        "non-both policy selected in fewer than 3 folds",
        "selected closed trades < 50% matched_both",
    ],
    "otherwise": "FAMILY 1 — DIRECTIONAL ASYMMETRY: INCONCLUSIVE",
}
_FROZEN_FAMILY_CANDIDATES = tuple(candidate for candidate in enumerate_candidates(V2ResearchConfig()) if candidate.family == FAMILY)
if tuple(candidate.value for candidate in _FROZEN_FAMILY_CANDIDATES) != CANDIDATES:
    raise RuntimeError("frozen Family 1 candidate inventory differs from the predeclared directional order")
_CANDIDATE_IDS = {str(candidate.value): candidate.candidate_id for candidate in _FROZEN_FAMILY_CANDIDATES}
_ADMISSION_TOKEN = object()


@dataclass(frozen=True, slots=True)
class _AdmittedFamily1Inputs:
    """Private real-data capability; only the loader creates production inputs."""
    repo_root: Path
    setups: tuple[Mapping[str, Any], ...]
    trades: tuple[Mapping[str, Any], ...]
    identities: Mapping[str, Any]
    setup_rows_sha256: str
    trade_rows_sha256: str
    _admission_token: object = field(repr=False)


def _freeze_rows(value: Any) -> Any:
    """Deep-copy immutable admitted data: mappings never retain source references."""
    if isinstance(value, Mapping):
        return MappingProxyType({key: _freeze_rows(nested) for key, nested in value.items()})
    if isinstance(value, list) or isinstance(value, tuple):
        return tuple(_freeze_rows(nested) for nested in value)
    return value


def _plain_rows(value: Any) -> Any:
    """Recover canonical JSON-compatible values from a frozen admitted tree."""
    if isinstance(value, Mapping):
        return {key: _plain_rows(nested) for key, nested in value.items()}
    if isinstance(value, tuple) or isinstance(value, list):
        return [_plain_rows(nested) for nested in value]
    return value


def _rows_sha256(rows: Sequence[Mapping[str, Any]]) -> str:
    return sha256(canonical_json(_plain_rows(rows)).encode("utf-8")).hexdigest()


def _millis(value: str) -> int:
    return int(datetime.fromisoformat(value.replace("Z", "+00:00")).timestamp() * 1000)


def _finite_number(value: object, field: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value):
        raise ValueError(f"{field} must be a finite number")
    return float(value)


def _direction_allowed(candidate: str, direction: str) -> bool:
    if candidate not in CANDIDATES:
        raise ValueError(f"unknown Family 1 candidate: {candidate}")
    return candidate == "both" or (candidate == "long_only" and direction == "long") or (candidate == "short_only" and direction == "short")


def _lock_implementation_identities(identities: Mapping[str, Any] | None, *, require_bound: bool) -> dict[str, str]:
    """Return a stable real binding or an explicit synthetic/unbound marker."""
    if identities is None:
        if require_bound:
            raise ValueError("enforced Family 1 staging requires exact implementation-hash identities")
        return {"binding": "synthetic/unbound"}
    if identities == {"binding": "synthetic/unbound"}:
        if require_bound:
            raise ValueError("enforced Family 1 staging requires exact implementation-hash identities")
        return {"binding": "synthetic/unbound"}
    missing = [key for key in IMPLEMENTATION_HASH_KEYS if not isinstance(identities.get(key), str) or len(str(identities[key])) != 64]
    if missing:
        raise ValueError("enforced Family 1 staging requires exact implementation-hash identities")
    return {key: str(identities[key]) for key in IMPLEMENTATION_HASH_KEYS}


def candidate_enumeration() -> list[dict[str, str]]:
    """Return the exact pre-optimization inventory IDs and order, not aliases."""
    return [asdict(candidate) for candidate in _FROZEN_FAMILY_CANDIDATES]


def _direction_sample_diagnostics(metrics: Mapping[str, Any], *, candidate: str, direction: str) -> dict[str, Any]:
    closed = int(metrics["closed"])
    return {
        "candidate_id": _CANDIDATE_IDS[candidate],
        "direction": direction,
        "closed": closed,
        "oos_direction_sample_warning": SMALL_SAMPLE_WARNING if closed < 30 else None,
    }


def _validation_direction_diagnostics(
    setups: Iterable[Mapping[str, Any]], trades: Iterable[Mapping[str, Any]], *, start_ms: int,
    end_exclusive_ms: int, candidate: str, fold_id: str,
) -> dict[str, dict[str, Any]]:
    """Report only policy-included direction cells; each receives an explicit OOS warning."""
    result: dict[str, dict[str, Any]] = {}
    for direction, directional_candidate in (("long", "long_only"), ("short", "short_only")):
        if _direction_allowed(candidate, direction):
            metrics = metrics_for_interval(setups, trades, start_ms=start_ms, end_exclusive_ms=end_exclusive_ms, candidate=directional_candidate, fold_id=fold_id)
            result[direction] = _direction_sample_diagnostics(metrics, candidate=candidate, direction=direction)
    return result


def _direction_r_contributions(
    trades: Iterable[Mapping[str, Any]], *, start_ms: int, end_exclusive_ms: int, candidate: str,
) -> dict[str, dict[str, Any]]:
    """Per-direction selected-policy R, retaining excluded directions as explicit N/A."""
    result: dict[str, dict[str, Any]] = {}
    for direction in ("long", "short"):
        included = _direction_allowed(candidate, direction)
        selected = [row for row in trades if start_ms <= int(row["setup_origin_timestamp"]) < end_exclusive_ms and int(row["exit_timestamp"]) < end_exclusive_ms and row["direction"] == direction]
        result[direction] = {"included_by_policy": included, "total_r": None if not included else sum(_finite_number(row["r"], "trade r") for row in selected)}
    return result


def _type7(values: Sequence[float], percentile: float) -> float | None:
    if not values:
        return None
    ordered = sorted(values)
    position = (len(ordered) - 1) * percentile
    lower = int(position)
    upper = min(lower + 1, len(ordered) - 1)
    return ordered[lower] + (ordered[upper] - ordered[lower]) * (position - lower)


def _ordered_trades(rows: Iterable[Mapping[str, Any]]) -> list[Mapping[str, Any]]:
    return sorted(rows, key=lambda row: (row["exit_timestamp"], row["trade_id"]))


def _metrics(
    setups: Iterable[Mapping[str, Any]], trades: Iterable[Mapping[str, Any]], *, interval_start_ms: int,
    interval_end_exclusive_ms: int, candidate: str, fold_id: str,
) -> dict[str, Any]:
    """Calculate Family 1 metrics with only the supplied, already-contained rows."""
    selected_setups = [row for row in setups if _direction_allowed(candidate, str(row["direction"]))]
    selected_trades = [row for row in _ordered_trades(trades) if _direction_allowed(candidate, str(row["direction"]))]
    values = [_finite_number(row["r"], "trade r") for row in selected_trades]
    wins = [value for value in values if value > 0]
    losses = [value for value in values if value < 0]
    durations = [int(row["exit_timestamp"]) - int(row["entry_timestamp"]) for row in selected_trades]
    if any(duration < 0 for duration in durations):
        raise ValueError("trade duration cannot be negative")
    cumulative = peak = 0.0
    maximum_drawdown = 0.0
    losing_streak = longest_losing_streak = 0
    curve: list[dict[str, Any]] = []
    for ordinal, (row, value) in enumerate(zip(selected_trades, values), 1):
        cumulative += value
        peak = max(peak, cumulative)
        maximum_drawdown = max(maximum_drawdown, peak - cumulative)
        losing_streak = losing_streak + 1 if value < 0 else 0
        longest_losing_streak = max(longest_losing_streak, losing_streak)
        curve.append({"ordinal": ordinal, "fold_id": fold_id, "candidate_id": candidate, "trade_id": row["trade_id"], "exit_timestamp": row["exit_timestamp"], "r": value, "cumulative_r": cumulative})
    count = len(selected_trades)
    setup_count = len(selected_setups)
    positive = sum(wins)
    negative_magnitude = -sum(losses)
    interval_days = (interval_end_exclusive_ms - interval_start_ms) / MS_PER_DAY
    if interval_days <= 0:
        raise ValueError("metrics interval must be positive")
    return {
        "eligible_setups": setup_count,
        "closed": count,
        "long": sum(row["direction"] == "long" for row in selected_trades),
        "short": sum(row["direction"] == "short" for row in selected_trades),
        "wins": len(wins), "losses": len(losses),
        "total_r": sum(values),
        "expectancy_r": None if not count else sum(values) / count,
        "r_per_setup": None if not setup_count else sum(values) / setup_count,
        # A no-loss PF is deliberately null instead of Infinity; the convention
        # is explicit and keeps canonical JSON finite and comparable.
        "profit_factor": None if not losses else positive / negative_magnitude,
        "profit_factor_no_loss_convention": "null",
        "win_rate": None if not count else len(wins) / count,
        "average_win_r": None if not wins else positive / len(wins),
        "max_win_r": None if not wins else max(wins),
        "average_loss_r": None if not losses else sum(losses) / len(losses),
        "max_loss_r": None if not losses else min(losses),
        "maximum_drawdown_r": maximum_drawdown,
        "longest_losing_streak": longest_losing_streak,
        "duration_ms": {"mean": None if not durations else sum(durations) / count, "median": _type7([float(value) for value in durations], .5), "p90": _type7([float(value) for value in durations], .9), "min": None if not durations else min(durations), "max": None if not durations else max(durations)},
        "interval_days": interval_days,
        "trade_density_closed_per_day": count / interval_days,
        "chronological_cumulative_r": curve,
    }


def metrics_for_interval(
    setups: Iterable[Mapping[str, Any]], trades: Iterable[Mapping[str, Any]], *, start_ms: int,
    end_exclusive_ms: int, candidate: str, fold_id: str,
) -> dict[str, Any]:
    """Select an origin-time population and explicitly censor open boundary trades."""
    assert_economic_range_allowed(start_ms, end_exclusive_ms)
    if end_exclusive_ms <= start_ms:
        raise ValueError("interval must be non-empty and half-open")
    setup_rows = [row for row in setups if start_ms <= int(row["timestamp"]) < end_exclusive_ms]
    originated = [row for row in trades if start_ms <= int(row["setup_origin_timestamp"]) < end_exclusive_ms]
    scored = [row for row in originated if int(row["exit_timestamp"]) < end_exclusive_ms]
    censored = [row for row in originated if int(row["exit_timestamp"]) >= end_exclusive_ms]
    result = _metrics(setup_rows, scored, interval_start_ms=start_ms, interval_end_exclusive_ms=end_exclusive_ms, candidate=candidate, fold_id=fold_id)
    result["cross_boundary_censored"] = [{"trade_id": row["trade_id"], "direction": row["direction"], "setup_origin_timestamp": row["setup_origin_timestamp"], "exit_timestamp": row["exit_timestamp"]} for row in _ordered_trades(censored)]
    result["originated_closed_trades"] = sum(_direction_allowed(candidate, row["direction"]) for row in originated)
    return result


def training_metrics(
    setups: Iterable[Mapping[str, Any]], trades: Iterable[Mapping[str, Any]], *, train_start_ms: int,
    validation_start_ms: int, candidate: str, fold_id: str,
) -> dict[str, Any]:
    """Training has no candidate outcome whose exit is at/after validation start."""
    assert_economic_range_allowed(train_start_ms, validation_start_ms)
    setup_rows = [row for row in setups if train_start_ms <= int(row["timestamp"]) < validation_start_ms]
    originated = [row for row in trades if train_start_ms <= int(row["setup_origin_timestamp"]) < validation_start_ms]
    trade_rows = [row for row in originated if int(row["exit_timestamp"]) < validation_start_ms]
    result = _metrics(setup_rows, trade_rows, interval_start_ms=train_start_ms, interval_end_exclusive_ms=validation_start_ms, candidate=candidate, fold_id=fold_id)
    result["originated_closed_trades"] = sum(_direction_allowed(candidate, str(row["direction"])) for row in originated)
    return result


def _verify_fold_population_counts(*, fold: Any, training_both: Mapping[str, Any], validation_both: Mapping[str, Any] | None = None) -> None:
    """Reproduce frozen both-policy trade populations before accepting a stage."""
    if int(training_both["originated_closed_trades"]) != fold.expected_train_originated or int(training_both["closed"]) != fold.expected_train_scored:
        raise ValueError(f"{fold.fold_id} frozen training originated/scored population mismatch")
    if validation_both is None:
        return
    if (int(validation_both["originated_closed_trades"]) != fold.expected_validation_originated or int(validation_both["closed"]) != fold.expected_validation_scored or len(validation_both["cross_boundary_censored"]) != fold.expected_validation_censored):
        raise ValueError(f"{fold.fold_id} frozen validation originated/scored/censored population mismatch")


def rank_training_candidates(training: Mapping[str, Mapping[str, Any]]) -> str:
    """Apply the frozen min-sample and deterministic ranking rule literally."""
    eligible = [candidate for candidate in CANDIDATES if int(training[candidate]["closed"]) >= MINIMUM_TRAINING_CLOSED]
    if not eligible:
        raise ValueError("no Family 1 candidate has the required 50 training closed trades")
    def key(candidate: str) -> tuple[float, float, int, int]:
        value = training[candidate]
        # Null PF (no losses) is a positive unbounded outcome for ranking only;
        # it is never serialized as Infinity.
        pf = value["profit_factor"]
        return (-float(value["expectancy_r"]), -math.inf if pf is None else -float(pf), -int(value["closed"]), CANDIDATES.index(candidate))
    return min(eligible, key=key)


def _canonical_bytes(payload: Mapping[str, Any]) -> bytes:
    return (canonical_json(dict(payload)) + "\n").encode("utf-8")


def _write_identical_or_fail(path: Path, payload: bytes) -> str:
    if path.exists():
        if path.read_bytes() != payload:
            raise ValueError(f"refusing to overwrite immutable artifact with different bytes: {path}")
    else:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(payload)
    return sha256(payload).hexdigest()


def build_fold_lock(*, fold: Any, training: Mapping[str, Mapping[str, Any]], selected_candidate: str, implementation_identities: Mapping[str, Any] | None = None) -> dict[str, Any]:
    if selected_candidate != rank_training_candidates(training):
        raise ValueError("fold lock selected candidate does not match frozen training rank")
    return {
        "schema_version": "xau-v2-family-1-lock/v1",
        "family": FAMILY,
        "preoptimization_commit_sha": PREOPTIMIZATION_COMMIT_SHA,
        "v2_protocol": {"path": V2_PROTOCOL_PATH, "sha256": V2_PROTOCOL_SHA256},
        "historical_artifact": {"path": HISTORICAL_ARTIFACT_PATH, "sha256": HISTORICAL_ARTIFACT_SHA256},
        "setup_context_artifact": {"path": CONTEXT_ARTIFACT_PATH, "sha256": CONTEXT_ARTIFACT_SHA256},
        "implementation_hashes": _lock_implementation_identities(implementation_identities, require_bound=False),
        "fold": asdict(fold),
        "training_information_available_strictly_before_utc": fold.validation_start,
        "minimum_training_closed_trades": MINIMUM_TRAINING_CLOSED,
        "ranking": ["expectancy_r descending", "profit_factor descending", "closed_trades descending", "fixed candidate order both/long_only/short_only"],
        "candidate_order": [{"value": candidate, "candidate_id": _CANDIDATE_IDS[candidate]} for candidate in CANDIDATES],
        "training_candidates": {candidate: training[candidate] for candidate in CANDIDATES},
        "selected_candidate": selected_candidate,
        "validation_economics_included": False,
    }


def fold_lock_bytes(lock: Mapping[str, Any]) -> bytes:
    if lock.get("validation_economics_included") is not False:
        raise ValueError("Family 1 lock must not include validation economics")
    return _canonical_bytes(lock)


def write_fold_lock(path: Path, lock: Mapping[str, Any]) -> str:
    return _write_identical_or_fail(path, fold_lock_bytes(lock))


def verify_fold_lock(path: Path, expected_lock: Mapping[str, Any]) -> str:
    if not path.exists():
        raise FileNotFoundError(f"missing required pre-validation lock: {path}")
    expected = fold_lock_bytes(expected_lock)
    observed = path.read_bytes()
    if observed != expected:
        raise ValueError(f"pre-validation lock is missing, tampered, or differs from frozen training selection: {path}")
    return sha256(observed).hexdigest()


def _context_rows(context_artifact: Mapping[str, Any]) -> tuple[Mapping[str, Any], ...]:
    rows = context_artifact.get("eligible_setup_context_ledger")
    if not isinstance(rows, (list, tuple)) or len(rows) != GLOBAL_ELIGIBLE_SETUPS:
        raise ValueError("setup context artifact does not contain exactly 1072 eligible setups")
    seen: set[tuple[int, str]] = set()
    counts = {"long": 0, "short": 0}
    for row in rows:
        if not isinstance(row, Mapping) or row.get("direction") not in counts:
            raise ValueError("setup context rows require long/short direction")
        timestamp = row.get("timestamp")
        if isinstance(timestamp, bool) or not isinstance(timestamp, int):
            raise ValueError("setup context rows require UTC timestamps")
        key = (timestamp, str(row["direction"]))
        if key in seen:
            raise ValueError("setup context direction/timestamp identities must be unique")
        seen.add(key); counts[str(row["direction"])] += 1
    if counts != {"long": 587, "short": 485}:
        raise ValueError("setup context direction totals differ from the frozen 587/485 inventory")
    return tuple(rows)


def verify_real_input_identities(repo_root: Path) -> dict[str, Any]:
    """Verify bytes and setup denominators only; this does not evaluate economics."""
    root = repo_root.resolve()
    # This must occur before touching any Family 1 input: it re-verifies the
    # production/Pine source manifest and canonical V1 ancestry fail-closed.
    v1_identity = verify_v1_identity(root)
    protocol = root / V2_PROTOCOL_PATH
    context = root / CONTEXT_ARTIFACT_PATH
    historical = root / HISTORICAL_ARTIFACT_PATH
    for path, expected, label in ((protocol, V2_PROTOCOL_SHA256, "V2 protocol"), (context, CONTEXT_ARTIFACT_SHA256, "setup context artifact"), (historical, HISTORICAL_ARTIFACT_SHA256, "historical artifact")):
        if sha256(path.read_bytes()).hexdigest() != expected:
            raise ValueError(f"{label} identity mismatch")
    committed_protocol = subprocess.run(("git", "show", f"{PREOPTIMIZATION_COMMIT_SHA}:{V2_PROTOCOL_PATH}"), cwd=root, check=True, stdout=subprocess.PIPE).stdout
    if sha256(committed_protocol).hexdigest() != V2_PROTOCOL_SHA256 or committed_protocol != protocol.read_bytes():
        raise ValueError("current V2 protocol is not the exact committed pre-optimization protocol")
    if subprocess.run(("git", "merge-base", "--is-ancestor", PREOPTIMIZATION_COMMIT_SHA, "HEAD"), cwd=root).returncode:
        raise ValueError("pre-optimization protocol commit is not an ancestor of HEAD")
    context_artifact = json.loads(context.read_bytes())
    rows = _context_rows(context_artifact)
    implementation_paths = {
        "family1_module_sha256": root / "src/quasartrend/research/xau_v2_family1.py",
        "family1_runner_sha256": root / "tools/run_xau_v2_family1.py",
        "xau_v2_initialization_sha256": root / "src/quasartrend/research/xau_v2_initialization.py",
        "provenance_sha256": root / "src/quasartrend/research/provenance.py",
    }
    # Initialization and provenance are upstream execution dependencies.  They
    # are required to remain byte-for-byte at the pre-optimization commit.
    for path in (implementation_paths["xau_v2_initialization_sha256"], implementation_paths["provenance_sha256"]):
        relative = path.relative_to(root).as_posix()
        committed = subprocess.run(("git", "show", f"{PREOPTIMIZATION_COMMIT_SHA}:{relative}"), cwd=root, check=True, stdout=subprocess.PIPE).stdout
        if path.read_bytes() != committed:
            raise ValueError(f"{relative} differs from its committed pre-optimization bytes")
    implementation_hashes = {name: sha256(path.read_bytes()).hexdigest() for name, path in implementation_paths.items()}
    return {"preoptimization_commit_sha": PREOPTIMIZATION_COMMIT_SHA, "v1_identity_verified": True, "canonical_v1_origin_main_sha": v1_identity["origin_main"], "v2_protocol_sha256": V2_PROTOCOL_SHA256, "historical_artifact_sha256": HISTORICAL_ARTIFACT_SHA256, "setup_context_artifact_sha256": CONTEXT_ARTIFACT_SHA256, **implementation_hashes, "eligible_setups": len(rows), "long_setups": 587, "short_setups": 485}


def load_real_family1_inputs(repo_root: Path) -> _AdmittedFamily1Inputs:
    """The real-run admission point.  Call only after review of Family 1 code."""
    root = repo_root.resolve()
    identities = verify_real_input_identities(root)
    context_artifact = json.loads((root / CONTEXT_ARTIFACT_PATH).read_bytes())
    setups = _context_rows(context_artifact)
    _, trades = admit_development_economic_ledger(root / HISTORICAL_ARTIFACT_PATH, V2ResearchConfig())
    context_keys = {(int(row["timestamp"]), str(row["direction"])) for row in setups}
    trade_keys = {(int(row["setup_origin_timestamp"]), str(row["direction"])) for row in trades}
    if len(trade_keys) != len(trades) or not trade_keys <= context_keys:
        raise ValueError("admitted trade ledger does not match frozen setup direction/origin identities")
    frozen_setups = tuple(_freeze_rows(row) for row in setups)
    frozen_trades = tuple(_freeze_rows(row) for row in trades)
    return _AdmittedFamily1Inputs(repo_root=root, setups=frozen_setups, trades=frozen_trades, identities=MappingProxyType(dict(identities)), setup_rows_sha256=_rows_sha256(frozen_setups), trade_rows_sha256=_rows_sha256(frozen_trades), _admission_token=_ADMISSION_TOKEN)


def _revalidate_admitted_trade_ranges(trades: Iterable[Mapping[str, Any]]) -> None:
    """Defence in depth before any production metric, baseline, or result build."""
    for row in trades:
        origin, exit_timestamp = row.get("setup_origin_timestamp"), row.get("exit_timestamp")
        if isinstance(origin, bool) or isinstance(exit_timestamp, bool) or not isinstance(origin, int) or not isinstance(exit_timestamp, int):
            raise ValueError("admitted trade lacks integer economic timestamps")
        assert_economic_range_allowed(origin, exit_timestamp + 1)


def _verify_admitted_row_digests(inputs: _AdmittedFamily1Inputs) -> None:
    if _rows_sha256(inputs.setups) != inputs.setup_rows_sha256 or _rows_sha256(inputs.trades) != inputs.trade_rows_sha256:
        raise ValueError("loader-admitted Family 1 rows differ from their canonical admission digests")


def _revalidate_admitted_population(inputs: _AdmittedFamily1Inputs) -> None:
    """Repeat both authoritative ledger/context checks immediately before staging."""
    validate_fold_population(inputs.trades)
    setups = _context_rows({"eligible_setup_context_ledger": list(inputs.setups)})
    context_keys = {(int(row["timestamp"]), str(row["direction"])) for row in setups}
    trade_keys = {(int(row["setup_origin_timestamp"]), str(row["direction"])) for row in inputs.trades}
    if len(trade_keys) != len(inputs.trades) or not trade_keys <= context_keys:
        raise ValueError("revalidated trade ledger does not match setup direction/origin identities")


def execute_real_family1(inputs: _AdmittedFamily1Inputs, *, lock_directory: Path) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    """The sole public production execution path; arbitrary row inputs are rejected."""
    if not isinstance(inputs, _AdmittedFamily1Inputs) or inputs._admission_token is not _ADMISSION_TOKEN:
        raise TypeError("production Family 1 execution requires loader-admitted inputs")
    current_identities = verify_real_input_identities(inputs.repo_root)
    if dict(inputs.identities) != current_identities:
        raise ValueError("loader-admitted Family 1 identities no longer match current verified inputs")
    _verify_admitted_row_digests(inputs)
    _revalidate_admitted_trade_ranges(inputs.trades)
    _revalidate_admitted_population(inputs)
    staged = _run_staged_family1(
        setups=inputs.setups,
        trades=inputs.trades,
        lock_directory=lock_directory,
        enforce_frozen_population_counts=True,
        implementation_identities=current_identities,
    )
    return _build_family1_result(staged=staged, setups=inputs.setups, trades=inputs.trades, identities=current_identities)


def _run_staged_family1(
    *, setups: Iterable[Mapping[str, Any]], trades: Iterable[Mapping[str, Any]], lock_directory: Path,
    lock_writer: Callable[[Path, Mapping[str, Any]], str] = write_fold_lock,
    validation_evaluator: Callable[..., dict[str, Any]] = metrics_for_interval,
    enforce_frozen_population_counts: bool = False,
    implementation_identities: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """Run F1 lock -> F1 validation -> ... in the only permitted order."""
    setup_rows, trade_rows = tuple(setups), tuple(trades)
    lock_implementation_hashes = _lock_implementation_identities(implementation_identities, require_bound=enforce_frozen_population_counts)
    reports: list[dict[str, Any]] = []
    selected_oos: list[dict[str, Any]] = []
    selected_oos_rows: list[dict[str, Any]] = []
    matched_both_rows: list[dict[str, Any]] = []
    selected_setup_rows: list[dict[str, Any]] = []
    matched_both_setup_rows: list[dict[str, Any]] = []
    for fold in FOLDS:
        train_start, validation_start, validation_end = _millis(fold.train_start), _millis(fold.validation_start), _millis(fold.validation_end_exclusive)
        training = {candidate: training_metrics(setup_rows, trade_rows, train_start_ms=train_start, validation_start_ms=validation_start, candidate=candidate, fold_id=fold.fold_id) for candidate in CANDIDATES}
        if enforce_frozen_population_counts:
            _verify_fold_population_counts(fold=fold, training_both=training["both"])
        selected = rank_training_candidates(training)
        lock = build_fold_lock(fold=fold, training=training, selected_candidate=selected, implementation_identities=lock_implementation_hashes)
        lock_path = lock_directory / f"{fold.fold_id}.json"
        lock_hash = lock_writer(lock_path, lock)
        # Writer success alone is insufficient: a custom writer must leave the
        # exact expected bytes in place before any validation function is called.
        if verify_fold_lock(lock_path, lock) != lock_hash:
            raise ValueError("lock writer returned a digest inconsistent with immutable lock bytes")
        validation = {candidate: validation_evaluator(setup_rows, trade_rows, start_ms=validation_start, end_exclusive_ms=validation_end, candidate=candidate, fold_id=fold.fold_id) for candidate in CANDIDATES}
        if enforce_frozen_population_counts:
            _verify_fold_population_counts(fold=fold, training_both=training["both"], validation_both=validation["both"])
        for candidate in CANDIDATES:
            validation[candidate]["oos_direction_diagnostics"] = _validation_direction_diagnostics(setup_rows, trade_rows, start_ms=validation_start, end_exclusive_ms=validation_end, candidate=candidate, fold_id=fold.fold_id)
            validation[candidate]["direction_r_contributions"] = _direction_r_contributions(trade_rows, start_ms=validation_start, end_exclusive_ms=validation_end, candidate=candidate)
        selected_oos.extend([{**row, "selected_candidate": selected} for row in validation[selected]["chronological_cumulative_r"]])
        originated = [row for row in trade_rows if validation_start <= int(row["setup_origin_timestamp"]) < validation_end]
        selected_oos_rows.extend({**row, "fold_id": fold.fold_id, "selected_candidate": selected, "candidate_id": selected} for row in originated if int(row["exit_timestamp"]) < validation_end and _direction_allowed(selected, str(row["direction"])))
        matched_both_rows.extend({**row, "fold_id": fold.fold_id, "selected_candidate": "both", "candidate_id": "both"} for row in originated if int(row["exit_timestamp"]) < validation_end)
        selected_setup_rows.extend({**row, "fold_id": fold.fold_id, "selected_candidate": selected} for row in setup_rows if validation_start <= int(row["timestamp"]) < validation_end and _direction_allowed(selected, str(row["direction"])))
        matched_both_setup_rows.extend({**row, "fold_id": fold.fold_id, "selected_candidate": "both"} for row in setup_rows if validation_start <= int(row["timestamp"]) < validation_end)
        reports.append({"fold": asdict(fold), "training_candidates": training, "selected_candidate": selected, "selection_lock": {"path": f"{LOCK_DIRECTORY}/{fold.fold_id}.json", "sha256": lock_hash}, "validation_candidates": validation})
    return {"folds": reports, "selected_oos_trades": selected_oos, "selected_oos_rows": selected_oos_rows, "matched_both_rows": matched_both_rows, "selected_setup_rows": selected_setup_rows, "matched_both_setup_rows": matched_both_setup_rows}


def _aggregate_selected(rows: Sequence[Mapping[str, Any]], *, series: str) -> dict[str, Any]:
    trades = [dict(row) for row in rows]
    cumulative = 0.0
    curve = []
    for ordinal, row in enumerate(trades, 1):
        cumulative += float(row["r"])
        curve.append({"ordinal": ordinal, "series": series, "fold_id": row["fold_id"], "candidate_id": row["candidate_id"], "trade_id": row["trade_id"], "exit_timestamp": row["exit_timestamp"], "r": row["r"], "cumulative_r": cumulative})
    return {"closed": len(trades), "total_r": cumulative, "chronological_cumulative_r": curve}


def _combined_metrics(setups: Sequence[Mapping[str, Any]], trades: Sequence[Mapping[str, Any]], *, label: str) -> dict[str, Any]:
    """Metrics for a selected sequence already direction-filtered fold by fold."""
    start = _millis(FOLDS[0].validation_start)
    result = _metrics(setups, trades, interval_start_ms=start, interval_end_exclusive_ms=DEVELOPMENT_END_EXCLUSIVE_MS, candidate="both", fold_id="combined")
    cumulative = 0.0
    curve = []
    for ordinal, row in enumerate(_ordered_trades(trades), 1):
        cumulative += float(row["r"])
        curve.append({"ordinal": ordinal, "series": label, "fold_id": row["fold_id"], "candidate_id": row["candidate_id"], "trade_id": row["trade_id"], "exit_timestamp": row["exit_timestamp"], "r": float(row["r"]), "cumulative_r": cumulative})
    result["chronological_cumulative_r"] = curve
    return result


def _descriptive_candidate_aggregates(
    *, setups: Sequence[Mapping[str, Any]], trades: Sequence[Mapping[str, Any]], folds: Sequence[Mapping[str, Any]],
) -> dict[str, dict[str, Any]]:
    """Aggregate each policy on the same scored F1--F5 populations, not a re-cut interval."""
    setup_rows: list[Mapping[str, Any]] = []
    trade_rows: list[Mapping[str, Any]] = []
    for fold in folds:
        start, end = _millis(fold["fold"]["validation_start"]), _millis(fold["fold"]["validation_end_exclusive"])
        setup_rows.extend(row for row in setups if start <= int(row["timestamp"]) < end)
        trade_rows.extend(row for row in trades if start <= int(row["setup_origin_timestamp"]) < end and int(row["exit_timestamp"]) < end)
    start, end = _millis(FOLDS[0].validation_start), DEVELOPMENT_END_EXCLUSIVE_MS
    result: dict[str, dict[str, Any]] = {}
    for candidate in CANDIDATES:
        metrics = _metrics(setup_rows, trade_rows, interval_start_ms=start, interval_end_exclusive_ms=end, candidate=candidate, fold_id="F1-F5")
        diagnostics: dict[str, dict[str, Any]] = {}
        for direction, directional_candidate in (("long", "long_only"), ("short", "short_only")):
            if _direction_allowed(candidate, direction):
                directional_metrics = _metrics(setup_rows, trade_rows, interval_start_ms=start, interval_end_exclusive_ms=end, candidate=directional_candidate, fold_id="F1-F5")
                diagnostics[direction] = _direction_sample_diagnostics(directional_metrics, candidate=candidate, direction=direction)
        metrics["oos_direction_diagnostics"] = diagnostics
        result[candidate] = metrics
    return result


def _build_family1_result(*, staged: Mapping[str, Any], setups: Sequence[Mapping[str, Any]], trades: Sequence[Mapping[str, Any]], identities: Mapping[str, Any]) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    """Build the deterministic report after all staged folds have completed."""
    folds = staged["folds"]
    selected_metrics = _combined_metrics(staged["selected_setup_rows"], staged["selected_oos_rows"], label="selected_oos")
    matched_metrics = _combined_metrics(staged["matched_both_setup_rows"], staged["matched_both_rows"], label="matched_v1_both")
    classification = classify_family1(folds=folds, selected_metrics=selected_metrics, matched_both_metrics=matched_metrics)
    increments = [{"fold_id": fold["fold"]["fold_id"], "incremental_r_vs_matched_both": fold["validation_candidates"][fold["selected_candidate"]]["total_r"] - fold["validation_candidates"]["both"]["total_r"]} for fold in folds]
    positives = [row["incremental_r_vs_matched_both"] for row in increments if row["incremental_r_vs_matched_both"] > 0]
    direction_contributions = {direction: sum(float(row["r"]) for row in staged["selected_oos_rows"] if row["direction"] == direction) for direction in ("long", "short")}
    csv_rows: list[dict[str, Any]] = []
    for series, rows in (("selected_oos", staged["selected_oos_rows"]), ("matched_v1_both", staged["matched_both_rows"])):
        cumulative = 0.0
        for ordinal, row in enumerate(_ordered_trades(rows), 1):
            cumulative += float(row["r"])
            csv_rows.append({"series": series, "fold_id": row["fold_id"], "selected_candidate": row["selected_candidate"], "candidate_id": row["candidate_id"], "ordinal": ordinal, "trade_id": row["trade_id"], "exit_timestamp": row["exit_timestamp"], "r": float(row["r"]), "cumulative_r": cumulative})
    frozen_baseline = _metrics(setups, trades, interval_start_ms=_millis(STRATEGY_ELIGIBILITY_START_UTC), interval_end_exclusive_ms=DEVELOPMENT_END_EXCLUSIVE_MS, candidate="both", fold_id="development")
    selected_policies = [fold["selected_candidate"] for fold in folds]
    result = {
        "schema_version": SCHEMA_VERSION,
        "family": {"number": 1, "name": FAMILY, "candidates": candidate_enumeration(), "filters_authorized": "direction only", "later_families_or_interactions": "not evaluated"},
        "frozen_inputs": dict(identities),
        "selection_protocol": {"minimum_training_closed_trades": MINIMUM_TRAINING_CLOSED, "ranking": ["expectancy_r descending", "profit_factor descending", "closed_trades descending", "fixed candidate order both/long_only/short_only"], "stage_order": "F1 lock -> F1 validation -> F2 lock -> F2 validation -> F3 lock -> F3 validation -> F4 lock -> F4 validation -> F5 lock -> F5 validation"},
        "folds": folds,
        "descriptive_candidate_aggregate_on_matched_oos_populations": _descriptive_candidate_aggregates(setups=setups, trades=trades, folds=folds),
        "selected_oos": selected_metrics,
        "matched_v1_both_reference": matched_metrics,
        "comparison": {"same_fold_populations": True, "incremental_r_by_fold": increments, "positive_incremental_r_total": sum(positives), "largest_positive_incremental_r_share": None if not positives else max(positives) / sum(positives), "selected_direction_total_r": direction_contributions, "trade_count_ratio_selected_to_matched_both": None if not matched_metrics["closed"] else selected_metrics["closed"] / matched_metrics["closed"], "maximum_drawdown_ratio_selected_to_matched_both": None if not matched_metrics["maximum_drawdown_r"] else selected_metrics["maximum_drawdown_r"] / matched_metrics["maximum_drawdown_r"], "stability": {"selected_candidate_by_fold": selected_policies, "selection_counts": {candidate: selected_policies.count(candidate) for candidate in CANDIDATES}, "positive_incremental_r_folds": [row["fold_id"] for row in increments if row["incremental_r_vs_matched_both"] > 0], "selected_fold_expectancy_r": {fold["fold"]["fold_id"]: fold["validation_candidates"][fold["selected_candidate"]]["expectancy_r"] for fold in folds}, "per_fold_selected_and_matched_direction_r": {fold["fold"]["fold_id"]: {"selected_candidate": fold["selected_candidate"], "selected": fold["validation_candidates"][fold["selected_candidate"]]["direction_r_contributions"], "matched_both": fold["validation_candidates"]["both"]["direction_r_contributions"]} for fold in folds}}},
        "frozen_820_trade_baseline_secondary_context": {"metrics": frozen_baseline, "closed": 820, "eligible_setups": 1072, "source": HISTORICAL_ARTIFACT_PATH, "not_used_as_matched_oos_reference": True},
        "economics_basis": {"r": "gross R from the frozen historical closed-trade ledger", "net_cost_conclusion": "not available; this experiment makes no net-cost conclusion"},
        "limitations": ["All candidate performance is descriptive except the prospectively selected fold sequence.", "R is gross R from the frozen ledger; no net-cost conclusion is available.", "One Family 1 result cannot authorize permanent direction removal or any V2 production change.", "The five lineage-specific provenance failures pinned to older canonical identities remain unchanged and are not weakened."],
        "known_full_suite_lineage_specific_failures": {"count": 5, "status": "unchanged", "reason": "existing phase-specific provenance guards remain pinned to predecessor canonical identities; they were not weakened for Family 1"},
        "classification_rule": CLASSIFICATION_RULE,
        "verdict": classification,
        "decision_authority": "Sol/main only; this artifact does not declare a phase gate or production authorization.",
    }
    return result, csv_rows


def classify_family1(*, folds: Sequence[Mapping[str, Any]], selected_metrics: Mapping[str, Any], matched_both_metrics: Mapping[str, Any]) -> str:
    """Apply the predeclared three-way classification without discretion."""
    selected = [str(fold["selected_candidate"]) for fold in folds]
    non_both = sum(value != "both" for value in selected)
    selected_exp, reference_exp = selected_metrics["expectancy_r"], matched_both_metrics["expectancy_r"]
    selected_pf, reference_pf = selected_metrics["profit_factor"], matched_both_metrics["profit_factor"]
    selected_total, reference_total = selected_metrics["total_r"], matched_both_metrics["total_r"]
    selected_closed, reference_closed = selected_metrics["closed"], matched_both_metrics["closed"]
    if selected_exp is None or reference_exp is None or selected_pf is None or reference_pf is None:
        return "FAMILY 1 — DIRECTIONAL ASYMMETRY: INCONCLUSIVE"
    if selected_exp <= reference_exp or selected_pf <= reference_pf or selected_total <= reference_total or non_both < 3 or selected_closed < .5 * reference_closed:
        return "FAMILY 1 — DIRECTIONAL ASYMMETRY: NOT JUSTIFIED"
    increments = [float(fold["validation_candidates"][fold["selected_candidate"]]["total_r"]) - float(fold["validation_candidates"]["both"]["total_r"]) for fold in folds]
    positive = [value for value in increments if value > 0]
    same_non_both = any(selected.count(candidate) >= 4 for candidate in ("long_only", "short_only"))
    fold_expectancies = [fold["validation_candidates"][fold["selected_candidate"]]["expectancy_r"] for fold in folds]
    dd_ok = selected_metrics["maximum_drawdown_r"] <= 1.25 * matched_both_metrics["maximum_drawdown_r"]
    concentration_ok = bool(positive) and max(positive) <= .6 * sum(positive)
    promising = same_non_both and selected_exp > reference_exp and selected_pf > reference_pf and selected_total > reference_total and selected_closed >= .5 * reference_closed and dd_ok and sum(value > 0 for value in increments) >= 3 and all(value is not None and value > -.5 for value in fold_expectancies) and concentration_ok
    return "FAMILY 1 — DIRECTIONAL ASYMMETRY: PROMISING" if promising else "FAMILY 1 — DIRECTIONAL ASYMMETRY: INCONCLUSIVE"


def family1_json(payload: Mapping[str, Any]) -> bytes:
    return _canonical_bytes(payload)


def family1_csv(rows: Sequence[Mapping[str, Any]]) -> bytes:
    fields = ("series", "fold_id", "selected_candidate", "candidate_id", "ordinal", "trade_id", "exit_timestamp", "r", "cumulative_r")
    buffer = io.StringIO(newline="")
    writer = csv.DictWriter(buffer, fieldnames=fields, lineterminator="\n", extrasaction="ignore")
    writer.writeheader()
    for row in sorted(rows, key=lambda value: (value["series"], value["fold_id"], value["exit_timestamp"], value["trade_id"])):
        writer.writerow({field: row.get(field, "") for field in fields})
    return buffer.getvalue().encode("utf-8")


def write_family1_artifacts(*, result_path: Path, csv_path: Path, result: Mapping[str, Any], csv_rows: Sequence[Mapping[str, Any]]) -> dict[str, str]:
    return {"result_sha256": _write_identical_or_fail(result_path, family1_json(result)), "csv_sha256": _write_identical_or_fail(csv_path, family1_csv(csv_rows))}
