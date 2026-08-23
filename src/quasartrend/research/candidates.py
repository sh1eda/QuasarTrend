"""Fingerprint-bound Phase 7 candidate registration and staged evaluation."""
from __future__ import annotations

from dataclasses import asdict, dataclass
import json
from pathlib import Path

from .experiments import DevelopmentExperimentReport, OutcomeMetrics, development_report
from .metrics import calculate_metrics
from .models import ResearchMetrics, SetupRow, TradeRow, validate_entry_feature_selectors
from .pipeline import CanonicalResearchBundle
from .provenance import fingerprint
from .splits import ChronologicalWindow

CANDIDATE_SCHEMA_VERSION = "phase7-candidate-registration/v1"
EVALUATION_SCHEMA_VERSION = "phase7-registered-candidate-evaluation/v1"
CANDIDATE_ID = "ADR_ENTRY_EXTENSION_LT_0_50"


@dataclass(frozen=True, slots=True)
class CandidateRegistration:
    schema_version: str
    candidate_id: str
    manifest_id: str
    dataset_fingerprint: str
    development_report_fingerprint: str
    feature: str
    operator: str
    threshold: float
    missing_behavior: str
    decision_point: str
    semantic_effect: str
    rationale: str
    development_baseline: OutcomeMetrics
    development_candidate: OutcomeMetrics


@dataclass(frozen=True, slots=True)
class WindowCandidateEvaluation:
    schema_version: str
    role: str
    start_date: str
    end_date: str
    candidate_registration_fingerprint: str
    manifest_id: str
    dataset_fingerprint: str
    eligible_baseline_setups: int
    baseline_closed_trades: int
    candidate_closed_trades: int
    rejected_at_entry: int
    missing_at_entry: int
    baseline: OutcomeMetrics
    candidate: OutcomeMetrics
    expectancy_delta: float | None
    total_r_delta: float
    opportunity_r_per_setup_delta: float | None
    profit_factor_delta: float | None
    stop_rate_delta: float | None
    trade_retention: float
    linked_setup_retention: float
    economically_improved: bool


@dataclass(frozen=True, slots=True)
class FinalistRegistration:
    schema_version: str
    candidate: CandidateRegistration
    candidate_registration_fingerprint: str
    validation: WindowCandidateEvaluation
    validation_fingerprint: str
    advancement_rule: str


@dataclass(frozen=True, slots=True)
class CandidateDecision:
    schema_version: str
    candidate_registration_fingerprint: str
    validation_fingerprint: str
    candidates_evaluated: int
    finalists_registered: int
    candidate_id: str
    validation_passed: bool
    final_oos_unlocked: bool
    final_oos_candidate_outcomes_evaluated: bool
    production_strategy_change_recommended: bool
    reasons: tuple[str, ...]


def _metrics(rows: tuple[TradeRow, ...], *, eligible: int, baseline_closed: int) -> OutcomeMetrics:
    value = calculate_metrics(rows, eligible_setups=eligible)
    return OutcomeMetrics(
        eligible, baseline_closed, len(rows),
        0.0 if eligible == 0 else len(rows) / eligible,
        0.0 if baseline_closed == 0 else len(rows) / baseline_closed,
        value.win_rate, value.expectancy_r, value.total_r,
        None if eligible == 0 else value.total_r / eligible,
        value.expectancy_r, value.profit_factor, value.stop_rate,
        value.mean_mae_r, value.mean_mfe_r, value.mean_duration_ms,
    )


def _delta(left: float | None, right: float | None) -> float | None:
    return None if left is None or right is None else right - left


def _population(bundle: CanonicalResearchBundle, window: ChronologicalWindow) -> tuple[tuple[SetupRow, ...], tuple[TradeRow, ...]]:
    inside = lambda timestamp: window.start_ms <= timestamp <= window.end_ms
    setups = tuple(row for row in bundle.dataset.setup_rows if row.eligible_baseline_setup and inside(row.decision_timestamp))
    setup_by_id = {row.setup_id: row for row in bundle.dataset.setup_rows}
    trades = tuple(
        row for row in bundle.dataset.trade_rows
        if row.outcome_state == "closed" and row.exit_timestamp is not None
        and inside(setup_by_id[row.setup_id].decision_timestamp)
        and inside(row.decision_timestamp) and inside(row.exit_timestamp)
    )
    return setups, trades


def register_development_candidate(
    bundle: CanonicalResearchBundle, report: DevelopmentExperimentReport
) -> CandidateRegistration:
    """Register the sole low-complexity candidate selected by Sol from development."""
    canonical_report = development_report(bundle)
    if report != canonical_report:
        raise ValueError("candidate registration requires the exact canonical development report")
    if report.role != "development" or report.inaccessible_roles != ("validation", "final_oos"):
        raise ValueError("candidate registration requires an isolated development report")
    if (
        report.manifest_id != bundle.dataset.manifest_id
        or report.dataset_fingerprint != fingerprint(bundle.dataset)
    ):
        raise ValueError("development report is not bound to this canonical bundle")
    validate_entry_feature_selectors("trade", ("adr_extension",))
    experiment = next((item for item in report.feature_experiments if item.hypothesis_id == "ADR_EXTENSION_FIXED_BUCKETS"), None)
    if experiment is None or tuple(item.label for item in experiment.buckets) != ("<0.25", "[0.25,0.50)", "[0.50,1.00]", ">1.00"):
        raise ValueError("required predeclared ADR experiment is unavailable")
    dev_window = ChronologicalWindow("development", report.start_date, report.end_date)
    setups, baseline_rows = _population(bundle, dev_window)
    candidate_rows = tuple(
        row for row in baseline_rows
        if row.adr_extension is not None and row.adr_extension < .50
    )
    candidate = _metrics(
        candidate_rows, eligible=len(setups), baseline_closed=len(baseline_rows)
    )
    if (
        len(setups) != report.eligible_baseline_setups
        or len(baseline_rows) != report.included_closed_baseline_trades
        or candidate.sample_count
        != sum(item.metrics.sample_count for item in experiment.buckets[:2])
        or abs(candidate.total_r - sum(item.metrics.total_r for item in experiment.buckets[:2])) > 1e-12
    ):
        raise ValueError("development evidence does not match the registered candidate")
    return CandidateRegistration(
        CANDIDATE_SCHEMA_VERSION, CANDIDATE_ID, report.manifest_id,
        report.dataset_fingerprint, fingerprint(report), "adr_extension", "<", .50,
        "reject candidate entry when ADR extension is unavailable",
        "frozen baseline entry decision, using only entry-time values",
        "research-only hard-entry-filter candidate; no production behavior",
        "single interpretable predeclared boundary; development expectancy, total R, R/setup, PF, and retention all improved",
        report.baseline_metrics, candidate,
    )


def _evaluate(bundle: CanonicalResearchBundle, registration: CandidateRegistration, window: ChronologicalWindow) -> WindowCandidateEvaluation:
    if registration.candidate_id != CANDIDATE_ID or registration.feature != "adr_extension" or registration.operator != "<" or registration.threshold != .50:
        raise ValueError("candidate semantics do not match the fixed registration")
    if registration.manifest_id != bundle.dataset.manifest_id or registration.dataset_fingerprint != fingerprint(bundle.dataset):
        raise ValueError("candidate registration is not bound to this dataset")
    setups, baseline_rows = _population(bundle, window)
    candidate_rows = tuple(row for row in baseline_rows if row.adr_extension is not None and row.adr_extension < registration.threshold)
    missing = sum(row.adr_extension is None for row in baseline_rows)
    baseline = _metrics(baseline_rows, eligible=len(setups), baseline_closed=len(baseline_rows))
    candidate = _metrics(candidate_rows, eligible=len(setups), baseline_closed=len(baseline_rows))
    economically_improved = (
        baseline.expectancy_r is not None and candidate.expectancy_r is not None and candidate.expectancy_r > baseline.expectancy_r
        and baseline.opportunity_r_per_setup is not None and candidate.opportunity_r_per_setup is not None and candidate.opportunity_r_per_setup > baseline.opportunity_r_per_setup
        and baseline.profit_factor is not None and candidate.profit_factor is not None and candidate.profit_factor >= baseline.profit_factor
        and candidate.linked_setup_retention >= bundle.research_config.setup_retention_floor
    )
    return WindowCandidateEvaluation(
        EVALUATION_SCHEMA_VERSION, window.role, window.start_date, window.end_date,
        fingerprint(registration), bundle.dataset.manifest_id, fingerprint(bundle.dataset),
        len(setups), len(baseline_rows), len(candidate_rows), len(baseline_rows) - len(candidate_rows), missing,
        baseline, candidate, _delta(baseline.expectancy_r, candidate.expectancy_r),
        candidate.total_r - baseline.total_r,
        _delta(baseline.opportunity_r_per_setup, candidate.opportunity_r_per_setup),
        _delta(baseline.profit_factor, candidate.profit_factor),
        _delta(baseline.stop_rate, candidate.stop_rate),
        candidate.trade_retention, candidate.linked_setup_retention, economically_improved,
    )


def validation_report(bundle: CanonicalResearchBundle, registration: CandidateRegistration) -> WindowCandidateEvaluation:
    cfg = bundle.split_config
    return _evaluate(bundle, registration, ChronologicalWindow("validation", cfg.validation_start, cfg.validation_end))


def register_finalist(
    bundle: CanonicalResearchBundle,
    registration: CandidateRegistration,
    validation: WindowCandidateEvaluation,
) -> FinalistRegistration:
    canonical_validation = validation_report(bundle, registration)
    if validation != canonical_validation:
        raise ValueError("finalist requires the exact canonical validation result")
    if validation.role != "validation" or validation.candidate_registration_fingerprint != fingerprint(registration):
        raise ValueError("finalist requires its own bound validation result")
    if not validation.economically_improved:
        raise ValueError("candidate did not satisfy the predeclared validation advancement rule")
    return FinalistRegistration(
        "phase7-finalist-registration/v1", registration, fingerprint(registration),
        validation, fingerprint(validation),
        "advance only if validation expectancy and opportunity R/setup improve, PF does not decline, and linked setup retention meets ResearchConfig.setup_retention_floor",
    )


def reject_after_validation(
    bundle: CanonicalResearchBundle,
    registration: CandidateRegistration,
    validation: WindowCandidateEvaluation,
) -> CandidateDecision:
    if validation != validation_report(bundle, registration):
        raise ValueError("decision requires the exact canonical validation result")
    if validation.role != "validation" or validation.candidate_registration_fingerprint != fingerprint(registration):
        raise ValueError("decision requires the bound validation result")
    if validation.economically_improved:
        raise ValueError("an improving validation result requires an explicit finalist decision")
    reasons = []
    comparisons = (
        ("expectancy", validation.expectancy_delta),
        ("total R", validation.total_r_delta),
        ("opportunity R/setup", validation.opportunity_r_per_setup_delta),
        ("profit factor", validation.profit_factor_delta),
    )
    reasons.extend(f"validation {name} degraded" for name, value in comparisons if value is not None and value < 0)
    if validation.stop_rate_delta is not None and validation.stop_rate_delta > 0:
        reasons.append("validation stop rate increased")
    if not reasons:
        reasons.append("predeclared validation advancement rule not satisfied")
    return CandidateDecision(
        "phase7-candidate-decision/v1", fingerprint(registration), fingerprint(validation),
        1, 0, registration.candidate_id, False, False, False, False, tuple(reasons),
    )


def final_oos_report(bundle: CanonicalResearchBundle, finalist: FinalistRegistration) -> WindowCandidateEvaluation:
    if not isinstance(finalist, FinalistRegistration):
        raise TypeError("final OOS requires a FinalistRegistration")
    if finalist.candidate_registration_fingerprint != fingerprint(finalist.candidate) or finalist.validation_fingerprint != fingerprint(finalist.validation):
        raise ValueError("finalist registration fingerprint mismatch")
    canonical_validation = validation_report(bundle, finalist.candidate)
    if finalist.validation != canonical_validation or not canonical_validation.economically_improved:
        raise ValueError("exact improving canonical validation is required to unlock final OOS")
    cfg = bundle.split_config
    window = ChronologicalWindow("final_oos", cfg.final_oos_start, cfg.final_oos_end)
    _, baseline_rows = _population(bundle, window)
    if len(baseline_rows) < bundle.research_config.final_oos_closed_trade_floor:
        raise ValueError(
            "final OOS baseline population cannot retain the required closed-trade floor"
        )
    return _evaluate(bundle, finalist.candidate, window)


def artifact_json(value: object) -> bytes:
    return json.dumps(asdict(value), sort_keys=True, separators=(",", ":"), allow_nan=False).encode("utf-8") + b"\n"


def write_artifact(value: object, path: Path, *, overwrite: bool = False) -> None:
    if path.exists() and not overwrite:
        raise FileExistsError(f"refusing to overwrite existing artifact: {path}")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(artifact_json(value))
