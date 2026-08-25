"""Frozen Phase 7.2 setup-origin candidate gates; no trade simulation or final OOS."""
from __future__ import annotations

from dataclasses import asdict, dataclass
import json
import math
from pathlib import Path
from statistics import mean, median

from .metrics import calculate_metrics
from .models import SetupRow, TradeRow
from .pipeline import CanonicalResearchBundle
from .provenance import fingerprint
from .regime_diagnosis import DEVELOPMENT_WINDOW, DiagnosisEvidence, validate_diagnosis_evidence
from .regime_features import (FeatureDefinitionArtifact, SetupRegimeFeatureRow,
    build_setup_regime_feature_rows, feature_definition_artifact,
    validate_regime_feature_artifact, validate_regime_feature_selectors)
from .splits import ChronologicalWindow

DEVELOPMENT_SCHEMA_VERSION = "phase7.2-regime-setup-origin-development/v1"
VALIDATION_SCHEMA_VERSION = "phase7.2-regime-setup-origin-validation/v1"
SEMANTIC_VERSION = "phase7.2-regime-setup-origin-candidates/v1"
SETUP_ORIGIN_ANCHOR = "setup_origin"
FROZEN_FEATURE_ARTIFACT_SHA256 = "32b58f6a478ecdb3cd857900a343048ea79261ab2e9af13a2defda6799784822"
FROZEN_DIAGNOSIS_SHA256 = "b69ea74f6c12cdbc5cced5f48a3b23344ccb3456ed31d5cfd998133a127f4b97"
EXPECTED_DIAGNOSIS_EVIDENCE_FINGERPRINT = "4845771118d51a80cbfa4296c86dc3c819294f2dcf3e91fa3a6a175fec94773f"
REVIEW_ATTESTATION = "LUNA_NO_UNRESOLVED_BLOCKER_HIGH"
FINAL_OOS_BASELINE_CLOSED_FLOOR = 50
KNOWN_FINAL_OOS_BASELINE_CLOSED = 30
DEVELOPMENT_WINDOWS = (
    ChronologicalWindow("development_1", "2026-05-15", "2026-05-28"),
    ChronologicalWindow("development_2", "2026-05-29", "2026-06-11"),
    ChronologicalWindow("development_3", "2026-06-12", "2026-06-25"),
    ChronologicalWindow("development_4", "2026-06-26", "2026-07-09"),
)
VALIDATION_WINDOWS = (
    ChronologicalWindow("validation_1", "2026-07-10", "2026-07-14"),
    ChronologicalWindow("validation_2", "2026-07-15", "2026-07-19"),
    ChronologicalWindow("validation_3", "2026-07-20", "2026-07-24"),
    ChronologicalWindow("validation_4", "2026-07-25", "2026-07-28"),
)

@dataclass(frozen=True, slots=True)
class CandidateSpec:
    candidate_id: str
    feature: str
    operator: str
    thresholds: tuple[float, ...]
    missing_behavior: str
    anchor: str = SETUP_ORIGIN_ANCHOR
    decision_point: str = "canonical SetupRow.decision_timestamp at HEMA-flip setup origin"
    effect: str = "accept/reject the canonical setup only; no entry/exit simulation"

    def accepts(self, value: object) -> bool:
        if self.candidate_id == "KALMAN_PERSISTENCE_OUTSIDE_DEVELOPMENT_IQR":
            return isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(float(value)) and (value <= 16 or value > 59)
        if self.candidate_id == "ATR_ADR_OUTSIDE_DEVELOPMENT_IQR":
            return isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(float(value)) and (value <= .06127766764569597 or value > .11783780470509574)
        if self.candidate_id == "HEMA_KALMAN_ALIGNED_AT_SETUP":
            return value is True
        raise ValueError("unknown frozen candidate")

CANDIDATE_SPECS = (
    CandidateSpec("KALMAN_PERSISTENCE_OUTSIDE_DEVELOPMENT_IQR", "kalman_persistence_bars", "<= 16 OR > 59", (16.0, 59.0), "reject when missing"),
    CandidateSpec("ATR_ADR_OUTSIDE_DEVELOPMENT_IQR", "atr_adr_ratio", "<= 0.06127766764569597 OR > 0.11783780470509574", (.06127766764569597, .11783780470509574), "reject when missing"),
    CandidateSpec("HEMA_KALMAN_ALIGNED_AT_SETUP", "hema_kalman_aligned", "is True", (), "reject when false or missing"),
)
CANDIDATE_SEMANTIC_FINGERPRINT = fingerprint(CANDIDATE_SPECS)

def _assert_frozen_contract() -> None:
    """Reject runtime rebinding of every predeclared candidate decision rule."""
    literal_specs = (
        CandidateSpec("KALMAN_PERSISTENCE_OUTSIDE_DEVELOPMENT_IQR", "kalman_persistence_bars", "<= 16 OR > 59", (16.0, 59.0), "reject when missing"),
        CandidateSpec("ATR_ADR_OUTSIDE_DEVELOPMENT_IQR", "atr_adr_ratio", "<= 0.06127766764569597 OR > 0.11783780470509574", (.06127766764569597, .11783780470509574), "reject when missing"),
        CandidateSpec("HEMA_KALMAN_ALIGNED_AT_SETUP", "hema_kalman_aligned", "is True", (), "reject when false or missing"),
    )
    literal_windows = (
        ChronologicalWindow("development_1", "2026-05-15", "2026-05-28"),
        ChronologicalWindow("development_2", "2026-05-29", "2026-06-11"),
        ChronologicalWindow("development_3", "2026-06-12", "2026-06-25"),
        ChronologicalWindow("development_4", "2026-06-26", "2026-07-09"),
    )
    literal_validation_windows = (
        ChronologicalWindow("validation_1", "2026-07-10", "2026-07-14"),
        ChronologicalWindow("validation_2", "2026-07-15", "2026-07-19"),
        ChronologicalWindow("validation_3", "2026-07-20", "2026-07-24"),
        ChronologicalWindow("validation_4", "2026-07-25", "2026-07-28"),
    )
    if (SEMANTIC_VERSION != "phase7.2-regime-setup-origin-candidates/v1"
            or DEVELOPMENT_SCHEMA_VERSION != "phase7.2-regime-setup-origin-development/v1"
            or VALIDATION_SCHEMA_VERSION != "phase7.2-regime-setup-origin-validation/v1"
            or REVIEW_ATTESTATION != "LUNA_NO_UNRESOLVED_BLOCKER_HIGH"
            or FROZEN_FEATURE_ARTIFACT_SHA256 != "32b58f6a478ecdb3cd857900a343048ea79261ab2e9af13a2defda6799784822"
            or FROZEN_DIAGNOSIS_SHA256 != "b69ea74f6c12cdbc5cced5f48a3b23344ccb3456ed31d5cfd998133a127f4b97"
            or EXPECTED_DIAGNOSIS_EVIDENCE_FINGERPRINT != "4845771118d51a80cbfa4296c86dc3c819294f2dcf3e91fa3a6a175fec94773f"
            or CANDIDATE_SPECS != literal_specs
            or DEVELOPMENT_WINDOWS != literal_windows
            or VALIDATION_WINDOWS != literal_validation_windows
            or CANDIDATE_SEMANTIC_FINGERPRINT != "a107a69ffe37035897442f1d5eba15d658df4ece026d5c692e7bbc7b84992aa8"
            or fingerprint(literal_specs) != "a107a69ffe37035897442f1d5eba15d658df4ece026d5c692e7bbc7b84992aa8"
            or FINAL_OOS_BASELINE_CLOSED_FLOOR != 50
            or KNOWN_FINAL_OOS_BASELINE_CLOSED != 30):
        raise ValueError("frozen candidate contract was mutated")

@dataclass(frozen=True, slots=True)
class CandidateMetrics:
    canonical_eligible_setups: int; retained_setups: int; baseline_closed_trades: int; retained_closed_trades: int
    setup_retention: float; trade_retention: float; total_r: float; expectancy_r: float | None; opportunity_r_per_setup: float | None
    profit_factor: float | None; win_rate: float | None; stop_rate: float | None; mean_r: float | None; median_r: float | None
    mean_mae_r: float | None; mean_mfe_r: float | None; mean_duration_bars: float | None; mean_duration_ms: float | None
    positive_r_total: float; negative_r_total: float; winners_ge_2r: int; winners_ge_3r: int; winners_ge_5r: int
    maximum_r: float | None; positive_r_retention: float | None; tail_retention: tuple[float | None, float | None, float | None]

@dataclass(frozen=True, slots=True)
class CandidateWindow:
    role: str; start_date: str; end_date: str; baseline: CandidateMetrics; candidate: CandidateMetrics; total_r_delta: float

@dataclass(frozen=True, slots=True)
class CandidateResult:
    spec: CandidateSpec; baseline: CandidateMetrics; candidate: CandidateMetrics; rejected_setups: int; missing_setups: int
    windows: tuple[CandidateWindow, ...]; improved_windows: int; degraded_windows: int; unchanged_windows: int
    best_window: str | None; worst_window: str | None; stability_pass: bool; gate_failures: tuple[str, ...]; gate_pass: bool

@dataclass(frozen=True, slots=True)
class DevelopmentReport:
    schema_version: str; anchor: str; role: str; inaccessible_roles: tuple[str, ...]; manifest_id: str; dataset_fingerprint: str
    feature_definition_fingerprint: str; feature_artifact_fingerprint: str; feature_artifact_sha256: str; diagnosis_sha256: str
    diagnosis_evidence_fingerprint: str; candidate_semantic_fingerprint: str; source_scope: str; observed_flips: int
    eligible_setups: int; included_closed_trades: int; results: tuple[CandidateResult, ...]

@dataclass(frozen=True, slots=True)
class CandidateRegistration:
    schema_version: str; candidate_id: str; spec: CandidateSpec; anchor: str; manifest_id: str; dataset_fingerprint: str
    feature_definition_fingerprint: str; diagnosis_sha256: str; diagnosis_evidence_fingerprint: str; development_report_fingerprint: str
    candidate_semantic_fingerprint: str; development_baseline: CandidateMetrics; development_candidate: CandidateMetrics; review_attestation: str

@dataclass(frozen=True, slots=True)
class ValidationReport:
    schema_version: str; anchor: str; role: str; inaccessible_roles: tuple[str, ...]; manifest_id: str; dataset_fingerprint: str
    development_report_fingerprint: str; registrations: tuple[CandidateRegistration, ...]; results: tuple[CandidateResult, ...]
    survivors: tuple[str, ...]; final_oos_accessed: bool; final_oos_status: str; final_oos_closed_trade_floor: int; production_change: bool

def _inside(window: ChronologicalWindow, timestamp: int) -> bool:
    return window.start_ms <= timestamp <= window.end_ms

def _finite(value: object) -> float | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)): return None
    value = float(value)
    return value if math.isfinite(value) else None

def _validate_inputs(bundle: CanonicalResearchBundle, artifact: FeatureDefinitionArtifact, evidence: DiagnosisEvidence, rows: tuple[SetupRegimeFeatureRow, ...]) -> None:
    """Hard prerequisite binding before any outcome population is selected."""
    validate_regime_feature_artifact(artifact); validate_diagnosis_evidence(evidence)
    if artifact != feature_definition_artifact(bundle): raise ValueError("exact canonical feature artifact required")
    if rows != build_setup_regime_feature_rows(bundle): raise ValueError("exact canonical setup-origin rows required")
    if tuple(row.setup_id for row in rows) != tuple(row.setup_id for row in bundle.dataset.setup_rows): raise ValueError("setup-origin identity/order mismatch")
    if fingerprint(evidence) != EXPECTED_DIAGNOSIS_EVIDENCE_FINGERPRINT: raise ValueError("exact frozen diagnosis evidence required")
    if evidence.anchor != SETUP_ORIGIN_ANCHOR or evidence.role != "development": raise ValueError("setup-origin development evidence required")
    if evidence.manifest_id != bundle.dataset.manifest_id or evidence.dataset_fingerprint != fingerprint(bundle.dataset): raise ValueError("evidence bundle binding mismatch")
    if evidence.feature_definition_fingerprint != artifact.definition_fingerprint or evidence.feature_artifact_fingerprint != fingerprint(artifact) or evidence.feature_artifact_sha256 != FROZEN_FEATURE_ARTIFACT_SHA256: raise ValueError("feature evidence binding mismatch")
    validate_regime_feature_selectors(tuple(item.feature for item in CANDIDATE_SPECS), artifact)

def _population(bundle: CanonicalResearchBundle, window: ChronologicalWindow) -> tuple[tuple[SetupRow, ...], tuple[TradeRow, ...]]:
    """Window population; closed rows require setup, entry, and exit inside it."""
    by_setup = {item.setup_id: item for item in bundle.dataset.setup_rows}
    if len(by_setup) != len(bundle.dataset.setup_rows): raise ValueError("duplicate setup identity")
    setups = tuple(item for item in bundle.dataset.setup_rows if item.eligible_baseline_setup and _inside(window, item.decision_timestamp))
    ids = {item.setup_id for item in setups}; linked: dict[str, TradeRow] = {}
    for trade in bundle.dataset.trade_rows:
        setup = by_setup.get(trade.setup_id)
        if setup is None or setup.linked_trade_id != trade.trade_id: raise ValueError("orphan/mismatched trade setup identity")
        if trade.setup_id in linked: raise ValueError("multiple trades for setup")
        linked[trade.setup_id] = trade
    closed = tuple(trade for sid, trade in linked.items() if sid in ids and trade.outcome_state == "closed" and trade.exit_timestamp is not None and _inside(window, trade.decision_timestamp) and _inside(window, trade.exit_timestamp))
    return setups, closed

def _setup_origin_partition(
    setups: tuple[SetupRow, ...], closed: tuple[TradeRow, ...], window: ChronologicalWindow,
) -> tuple[tuple[SetupRow, ...], tuple[TradeRow, ...]]:
    """Partition an already-legal development population by setup timestamp only."""
    partitioned_setups = tuple(item for item in setups if _inside(window, item.decision_timestamp))
    ids = {item.setup_id for item in partitioned_setups}
    return partitioned_setups, tuple(item for item in closed if item.setup_id in ids)

def _metrics(setups: tuple[SetupRow, ...], baseline_rows: tuple[TradeRow, ...], kept: set[str]) -> CandidateMetrics:
    rows = tuple(item for item in baseline_rows if item.setup_id in kept); canonical = len(setups); aggregate = calculate_metrics(rows, eligible_setups=canonical)
    rs = tuple(float(item.realized_r) for item in rows if item.realized_r is not None)
    if len(rs) != len(rows): raise ValueError("closed outcome missing realized R")
    maes = tuple(value for item in rows if (value := _finite(item.mae_r)) is not None); mfes = tuple(value for item in rows if (value := _finite(item.mfe_r)) is not None)
    bars = tuple(value for item in rows if (value := _finite(item.observed_duration_bars)) is not None); ms = tuple(value for item in rows if (value := _finite(item.elapsed_duration_ms)) is not None)
    return CandidateMetrics(canonical, len(kept), len(baseline_rows), len(rows), 0. if not canonical else len(kept)/canonical, 0. if not baseline_rows else len(rows)/len(baseline_rows), aggregate.total_r, aggregate.expectancy_r, aggregate.r_per_setup, aggregate.profit_factor, aggregate.win_rate, aggregate.stop_rate, None if not rs else mean(rs), None if not rs else median(rs), None if not maes else mean(maes), None if not mfes else mean(mfes), None if not bars else mean(bars), None if not ms else mean(ms), float(sum(value for value in rs if value > 0)), float(sum(value for value in rs if value < 0)), sum(value >= 2 for value in rs), sum(value >= 3 for value in rs), sum(value >= 5 for value in rs), None if not rs else max(rs), None, (None, None, None))

def _with_retention(candidate: CandidateMetrics, baseline: CandidateMetrics) -> CandidateMetrics:
    positive = None if baseline.positive_r_total == 0 else candidate.positive_r_total / baseline.positive_r_total
    tails = tuple(None if base == 0 else value/base for base, value in zip((baseline.winners_ge_2r, baseline.winners_ge_3r, baseline.winners_ge_5r), (candidate.winners_ge_2r, candidate.winners_ge_3r, candidate.winners_ge_5r)))
    fields = tuple(CandidateMetrics.__dataclass_fields__)
    values = tuple(getattr(candidate, field) for field in fields[:-2])
    return CandidateMetrics(*values, positive, tails)

def _gate(base: CandidateMetrics, candidate: CandidateMetrics, stability: bool, chronology: bool) -> tuple[str, ...]:
    checks = (("expectancy", base.expectancy_r is not None and candidate.expectancy_r is not None and candidate.expectancy_r > base.expectancy_r), ("total_r", candidate.total_r > base.total_r), ("opportunity_r_per_setup", base.opportunity_r_per_setup is not None and candidate.opportunity_r_per_setup is not None and candidate.opportunity_r_per_setup > base.opportunity_r_per_setup), ("profit_factor", base.profit_factor is not None and candidate.profit_factor is not None and candidate.profit_factor >= base.profit_factor), ("setup_retention", candidate.setup_retention >= .25), ("positive_r_retention", candidate.positive_r_retention is not None and candidate.positive_r_retention >= .70), ("tail_retention", all(value is not None and value >= .70 for value in candidate.tail_retention)), ("chronology_stability", not chronology or stability))
    return tuple(name for name, passing in checks if not passing)

def _evaluate(
    setups: tuple[SetupRow, ...], closed: tuple[TradeRow, ...],
    by_id: dict[str, SetupRegimeFeatureRow], spec: CandidateSpec,
    partition_windows: tuple[ChronologicalWindow, ...], chronology: bool,
) -> CandidateResult:
    all_ids = {item.setup_id for item in setups}; kept = {item.setup_id for item in setups if spec.accepts(getattr(by_id[item.setup_id], spec.feature))}
    base = _metrics(setups, closed, all_ids); candidate = _with_retention(_metrics(setups, closed, kept), base); window_results: tuple[CandidateWindow, ...] = ()
    if chronology:
        result = []
        for sub in partition_windows:
            sub_setups, sub_closed = _setup_origin_partition(setups, closed, sub); sub_ids = {item.setup_id for item in sub_setups}; sub_kept = {item.setup_id for item in sub_setups if spec.accepts(getattr(by_id[item.setup_id], spec.feature))}; sub_base = _metrics(sub_setups, sub_closed, sub_ids); sub_candidate = _with_retention(_metrics(sub_setups, sub_closed, sub_kept), sub_base); result.append(CandidateWindow(sub.role, sub.start_date, sub.end_date, sub_base, sub_candidate, sub_candidate.total_r-sub_base.total_r))
        window_results = tuple(result)
        if sum(item.baseline.canonical_eligible_setups for item in window_results) != len(setups): raise AssertionError("chronology windows do not partition eligible setups")
        if sum(item.baseline.baseline_closed_trades for item in window_results) != len(closed): raise AssertionError("chronology windows do not partition closed trades")
        if sum(item.candidate.retained_setups for item in window_results) != candidate.retained_setups: raise AssertionError("chronology windows do not partition retained setups")
        if sum(item.candidate.retained_closed_trades for item in window_results) != candidate.retained_closed_trades: raise AssertionError("chronology windows do not partition retained closed trades")
    improved = sum(item.total_r_delta > 0 for item in window_results); degraded = sum(item.total_r_delta < 0 for item in window_results); positive = tuple(item.total_r_delta for item in window_results if item.total_r_delta > 0)
    stable = chronology and improved >= 2 and bool(positive) and max(positive)/sum(positive) <= .75
    failures = _gate(base, candidate, stable, chronology); missing = sum(getattr(by_id[item.setup_id], spec.feature) is None for item in setups)
    best = None if not window_results else max(window_results, key=lambda item: item.total_r_delta).role
    worst = None if not window_results else min(window_results, key=lambda item: item.total_r_delta).role
    return CandidateResult(spec, base, candidate, len(setups)-len(kept), missing, window_results, improved, degraded, len(window_results)-improved-degraded, best, worst, stable, failures, not failures)

def development_report(bundle: CanonicalResearchBundle, artifact: FeatureDefinitionArtifact, evidence: DiagnosisEvidence, rows: tuple[SetupRegimeFeatureRow, ...]) -> DevelopmentReport:
    _assert_frozen_contract(); _validate_inputs(bundle, artifact, evidence, rows); by_id = {item.setup_id: item for item in rows}; setups, closed = _population(bundle, DEVELOPMENT_WINDOW); results = tuple(_evaluate(setups, closed, by_id, spec, DEVELOPMENT_WINDOWS, True) for spec in CANDIDATE_SPECS)
    report = DevelopmentReport(DEVELOPMENT_SCHEMA_VERSION, SETUP_ORIGIN_ANCHOR, "development", ("validation", "final_oos"), bundle.dataset.manifest_id, fingerprint(bundle.dataset), artifact.definition_fingerprint, fingerprint(artifact), FROZEN_FEATURE_ARTIFACT_SHA256, FROZEN_DIAGNOSIS_SHA256, fingerprint(evidence), CANDIDATE_SEMANTIC_FINGERPRINT, evidence.source_scope, evidence.observed_flips, results[0].baseline.canonical_eligible_setups, results[0].baseline.baseline_closed_trades, results)
    if (report.observed_flips, report.eligible_setups, report.included_closed_trades) != (276, 139, 103): raise ValueError("frozen Stage 1 population mismatch")
    return report

def validate_development_report(bundle: CanonicalResearchBundle, artifact: FeatureDefinitionArtifact, evidence: DiagnosisEvidence, rows: tuple[SetupRegimeFeatureRow, ...], report: DevelopmentReport) -> None:
    if report != development_report(bundle, artifact, evidence, rows): raise ValueError("not exact canonical development report")

def register_candidate(bundle: CanonicalResearchBundle, artifact: FeatureDefinitionArtifact, evidence: DiagnosisEvidence, rows: tuple[SetupRegimeFeatureRow, ...], report: DevelopmentReport, candidate_id: str, review_attestation: str) -> CandidateRegistration:
    validate_development_report(bundle, artifact, evidence, rows, report)
    if review_attestation != REVIEW_ATTESTATION: raise ValueError("exact Luna review attestation required")
    result = next((item for item in report.results if item.spec.candidate_id == candidate_id), None)
    if result is None or not result.gate_pass: raise ValueError("candidate did not pass frozen development gate")
    return CandidateRegistration("phase7.2-regime-setup-origin-candidate-registration/v1", candidate_id, result.spec, SETUP_ORIGIN_ANCHOR, report.manifest_id, report.dataset_fingerprint, artifact.definition_fingerprint, FROZEN_DIAGNOSIS_SHA256, fingerprint(evidence), fingerprint(report), CANDIDATE_SEMANTIC_FINGERPRINT, result.baseline, result.candidate, review_attestation)

def _validate_registration(bundle: CanonicalResearchBundle, artifact: FeatureDefinitionArtifact, evidence: DiagnosisEvidence, rows: tuple[SetupRegimeFeatureRow, ...], report: DevelopmentReport, registration: CandidateRegistration) -> None:
    expected = register_candidate(bundle, artifact, evidence, rows, report, registration.candidate_id, registration.review_attestation)
    if registration != expected: raise ValueError("forged candidate registration")

def validation_report(bundle: CanonicalResearchBundle, artifact: FeatureDefinitionArtifact, evidence: DiagnosisEvidence, rows: tuple[SetupRegimeFeatureRow, ...], development: DevelopmentReport, registrations: tuple[CandidateRegistration, ...]) -> ValidationReport:
    _assert_frozen_contract(); validate_development_report(bundle, artifact, evidence, rows, development)
    ids = tuple(item.candidate_id for item in registrations)
    if len(ids) != len(set(ids)): raise ValueError("duplicate registration")
    for registration in registrations: _validate_registration(bundle, artifact, evidence, rows, development, registration)
    window = ChronologicalWindow("validation", bundle.split_config.validation_start, bundle.split_config.validation_end)
    if window != ChronologicalWindow("validation", "2026-07-10", "2026-07-28"):
        raise ValueError("frozen validation window mismatch")
    by_id = {item.setup_id: item for item in rows}; setups, closed = _population(bundle, window)
    if (len(setups), len(closed)) != (43, 25): raise ValueError("frozen validation population mismatch")
    results = tuple(_evaluate(setups, closed, by_id, item.spec, VALIDATION_WINDOWS, True) for item in registrations); survivors = tuple(item.candidate_id for item, result in zip(registrations, results) if result.gate_pass)
    final_status = (
        f"locked_baseline_closed_floor_{KNOWN_FINAL_OOS_BASELINE_CLOSED}_lt_{FINAL_OOS_BASELINE_CLOSED_FLOOR}"
        if survivors else "not_accessed_no_validation_survivor"
    )
    return ValidationReport(VALIDATION_SCHEMA_VERSION, SETUP_ORIGIN_ANCHOR, "validation", ("final_oos",), bundle.dataset.manifest_id, fingerprint(bundle.dataset), fingerprint(development), registrations, results, survivors, False, final_status, FINAL_OOS_BASELINE_CLOSED_FLOOR, False)

def _artifact_json(value: object) -> bytes:
    return json.dumps(asdict(value), sort_keys=True, separators=(",", ":"), allow_nan=False).encode("utf-8") + b"\n"

def development_json(bundle: CanonicalResearchBundle, artifact: FeatureDefinitionArtifact, evidence: DiagnosisEvidence, rows: tuple[SetupRegimeFeatureRow, ...], report: DevelopmentReport) -> bytes:
    """Serialize only a contextually recreated canonical development report."""
    validate_development_report(bundle, artifact, evidence, rows, report)
    return _artifact_json(report)

def validation_json(bundle: CanonicalResearchBundle, artifact: FeatureDefinitionArtifact, evidence: DiagnosisEvidence, rows: tuple[SetupRegimeFeatureRow, ...], development: DevelopmentReport, registrations: tuple[CandidateRegistration, ...], report: ValidationReport) -> bytes:
    """Serialize only a contextually recreated canonical validation report."""
    canonical = validation_report(bundle, artifact, evidence, rows, development, registrations)
    if report != canonical: raise ValueError("not exact canonical validation report")
    return _artifact_json(report)

def _write(bytes_value: bytes, path: Path, *, overwrite: bool) -> None:
    if path.exists() and not overwrite: raise FileExistsError(f"refusing to overwrite existing artifact: {path}")
    path.parent.mkdir(parents=True, exist_ok=True); path.write_bytes(bytes_value)

def write_development_report(bundle: CanonicalResearchBundle, artifact: FeatureDefinitionArtifact, evidence: DiagnosisEvidence, rows: tuple[SetupRegimeFeatureRow, ...], report: DevelopmentReport, path: Path, *, overwrite: bool = False) -> None:
    _write(development_json(bundle, artifact, evidence, rows, report), path, overwrite=overwrite)

def write_validation_report(bundle: CanonicalResearchBundle, artifact: FeatureDefinitionArtifact, evidence: DiagnosisEvidence, rows: tuple[SetupRegimeFeatureRow, ...], development: DevelopmentReport, registrations: tuple[CandidateRegistration, ...], report: ValidationReport, path: Path, *, overwrite: bool = False) -> None:
    _write(validation_json(bundle, artifact, evidence, rows, development, registrations, report), path, overwrite=overwrite)
