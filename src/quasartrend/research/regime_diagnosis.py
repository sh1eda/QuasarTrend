"""Development-only, observational Phase 7.2 setup-origin regime diagnosis.

This module has no candidate, validation, or final-OOS behavior.  It uses full
source history only to obtain causal setup-origin feature warm-up, then limits
all outcome joins, bucket edges, and reported economics to development.
"""
from __future__ import annotations

from dataclasses import asdict, dataclass
from hashlib import sha256
import json
import math
from pathlib import Path
from statistics import mean, median, quantiles

from .models import SetupRow, TradeRow
from .pipeline import CanonicalResearchBundle
from .provenance import fingerprint
from .regime_features import (
    FEATURE_DEFINITIONS,
    FEATURE_DEFINITION_FINGERPRINT,
    REGIME_FEATURE_VERSION,
    SETUP_ORIGIN_ANCHOR,
    FeatureDefinitionArtifact,
    SetupRegimeFeatureRow,
    build_setup_regime_feature_rows,
    feature_definition_artifact,
    validate_regime_feature_artifact,
    validate_regime_feature_selectors,
)
from .splits import ChronologicalWindow


DIAGNOSIS_SCHEMA_VERSION = "phase7.2-regime-setup-origin-diagnosis/v1"
FROZEN_FEATURE_ARTIFACT_SHA256 = "32b58f6a478ecdb3cd857900a343048ea79261ab2e9af13a2defda6799784822"
EXPECTED_FEATURE_ARTIFACT_FINGERPRINT = "e5e66c4b1898d450d153e9efd19584dafc9f6548a39dead891d89c0a3d9a4402"
EXPECTED_DIAGNOSIS_EVIDENCE_FINGERPRINT = "4845771118d51a80cbfa4296c86dc3c819294f2dcf3e91fa3a6a175fec94773f"
DEVELOPMENT_WINDOW = ChronologicalWindow("development", "2026-05-15", "2026-07-09")
INACCESSIBLE_ROLES = ("validation", "final_oos")
NUMERIC_FEATURES = (
    "hema_fast_slope_atr_8", "directional_efficiency_8", "directional_efficiency_16",
    "directional_efficiency_32", "hema_flip_count_16", "kalman_flip_count_16",
    "combined_flip_count_16", "kalman_persistence_bars", "atr_adr_ratio",
)
CATEGORICAL_FEATURES = ("hema_kalman_aligned", "htf_hema_aligned")
_EXPECTED_PROVENANCE = {
    "manifest_id": "812d4449ce31615324f021533ce8d4492f9f49ffc98bd1879e823add7991b39c",
    "dataset_fingerprint": "96fb832135bef85123bcca522e6a7836aed58531e73158870c68c030ec9e1982",
    "strategy_fingerprint": "bb8fdc3cda4c39b43a09d8fb6a95a05077d35a01e14e0718a6af75ef21f1f0e6",
    "replay_fingerprint": "54452d8b4a309209586684b6ed356d4b7344f560d2025f1f0c4ad2d001e80312",
    "backtest_fingerprint": "596a0f6010107a8a3d9f3a4cace7cbb828110af3b1d9d37c58558cc3bb2b40d3",
    "research_fingerprint": "54e5635d41f26fd935e3ebd1e93a865c014566c683bda0f9838809fe7c929f33",
    "split_fingerprint": "0d807d81cf3ef72d5fa84a40666c0f2f360da059dcb46188cdad33397aebef5a",
}
_EXPECTED_SOURCE_COUNTS = (("15m", 10452), ("4h", 8480))
_EXPECTED_SOURCE_SCOPE = (
    "Full source-history SetupRow HEMA-flip snapshots for causal warm-up only; "
    "definitions and feature reconstruction access no TradeRow or outcome field. "
    "The full dataset fingerprint is an opaque immutable baseline-provenance binding."
)
_EXPECTED_SOURCE_ARTIFACTS = (
    {"declared_symbol": "BTCUSDT", "timeframe": "15m", "raw_input_sha256": "f5d251aa0e04616b8b74e222b8cb99b7eb9f37c74125815c58453c963727a657", "normalized_content_sha256": "6d50aef93fc0e213f05ae1d141493c4802d0c8dd333ee423be80266a1f7b382f", "row_count": 10452, "date_range": ("2026-05-01", "2026-08-17"), "parser_id": "tradingview-dual-ohlc-csv/v1", "identity_status": "declared_unverified"},
    {"declared_symbol": "BTCUSDT", "timeframe": "4h", "raw_input_sha256": "e463dc26298df500b39ac618e58a59fe1644cb8515ffcbd39181477a2990082b", "normalized_content_sha256": "90726fa09988a4acec01ace0e592903ee93dbfd7540852331feeef95df28b46e", "row_count": 8480, "date_range": ("2022-10-04", "2026-08-17"), "parser_id": "tradingview-dual-ohlc-csv/v1", "identity_status": "declared_unverified"},
)


@dataclass(frozen=True, slots=True)
class NumericSummary:
    count: int
    missing: int
    minimum: float | None
    maximum: float | None
    mean: float | None
    median: float | None


@dataclass(frozen=True, slots=True)
class Correlation:
    outcome: str
    paired_count: int
    pearson_r: float | None
    interpretation: str = "observational_noncausal"


@dataclass(frozen=True, slots=True)
class BucketMetrics:
    eligible_setups: int
    opened_trades: int
    closed_trades: int
    censored_unresolved: int
    non_open: int
    purged_setup_boundary: int
    purged_entry_exit_boundary: int
    sample_retention: float | None
    total_r: float
    expectancy_r: float | None
    r_per_setup: float | None
    opportunity_r_per_canonical_setup: float | None
    profit_factor: float | None
    win_rate: float | None
    stop_rate: float | None
    mean_r: float | None
    median_r: float | None
    mean_mae_r: float | None
    mean_mfe_r: float | None
    mean_duration_bars: float | None
    mean_duration_ms: float | None
    positive_r_total: float
    negative_total_r: float
    negative_trade_count: int
    stop_loss_count: int
    winners_ge_2r: int
    winners_ge_3r: int
    winners_ge_5r: int
    maximum_r: float | None


@dataclass(frozen=True, slots=True)
class LossConcentration:
    eligible_setup_share: float | None
    closed_trade_share: float | None
    negative_r_magnitude_share: float | None
    negative_trade_share: float | None
    stop_loss_share: float | None
    low_follow_through_count: int
    low_follow_through_share: float | None
    low_follow_through_definition: str = "canonical MFE < 1.0R"


@dataclass(frozen=True, slots=True)
class WinnerConcentration:
    winners_ge_2r_count: int
    winners_ge_2r_share: float | None
    winners_ge_3r_count: int
    winners_ge_3r_share: float | None
    winners_ge_5r_count: int
    winners_ge_5r_share: float | None
    positive_r_share: float | None


@dataclass(frozen=True, slots=True)
class BucketDiagnosis:
    label: str
    lower_bound: float | None
    upper_bound: float | None
    lower_inclusive: bool
    upper_inclusive: bool
    metrics: BucketMetrics
    loss_concentration: LossConcentration
    winner_concentration: WinnerConcentration


@dataclass(frozen=True, slots=True)
class FeatureDiagnosis:
    name: str
    kind: str
    anchor: str
    definition: str
    numeric_summary: NumericSummary | None
    quartile_source: str | None
    quantile_method: str | None
    quantile_edges: tuple[float, ...]
    edge_fingerprint: str | None
    continuous_relationships: tuple[Correlation, ...]
    buckets: tuple[BucketDiagnosis, ...]


@dataclass(frozen=True, slots=True)
class BoundaryAudit:
    """All linked trades touching development by setup or entry, without IDs."""

    setup_out_entry_in: int
    setup_in_entry_out: int
    setup_entry_in_exit_out: int
    setup_entry_in_censored_unresolved: int
    setup_entry_exit_included: int


@dataclass(frozen=True, slots=True)
class DiagnosisEvidence:
    schema_version: str
    stage: str
    role: str
    start_date: str
    end_date: str
    inaccessible_roles: tuple[str, ...]
    anchor: str
    source_scope: str
    manifest_id: str
    dataset_fingerprint: str
    strategy_fingerprint: str
    replay_fingerprint: str
    backtest_fingerprint: str
    research_fingerprint: str
    split_fingerprint: str
    source_artifacts: tuple[object, ...]
    source_counts: tuple[tuple[str, int], ...]
    feature_definition_version: str
    feature_definition_fingerprint: str
    feature_artifact_fingerprint: str
    feature_artifact_sha256: str
    observed_flips: int
    eligible_setups: int
    population_categories: tuple[tuple[str, int], ...]
    boundary_audit: BoundaryAudit
    baseline_metrics: BucketMetrics
    features: tuple[FeatureDiagnosis, ...]


@dataclass(frozen=True, slots=True)
class FinalDiagnosis:
    evidence: DiagnosisEvidence
    conclusion: str


@dataclass(frozen=True, slots=True)
class _Population:
    observed_setups: tuple[SetupRow, ...]
    eligible_setups: tuple[SetupRow, ...]
    feature_by_setup_id: dict[str, SetupRegimeFeatureRow]
    category_by_setup_id: dict[str, str]
    included_trade_by_setup_id: dict[str, TradeRow]
    boundary_audit: BoundaryAudit


def _inside(timestamp: int) -> bool:
    return DEVELOPMENT_WINDOW.start_ms <= timestamp <= DEVELOPMENT_WINDOW.end_ms


def _finite(value: object) -> float | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    result = float(value)
    return result if math.isfinite(result) else None


def _pearson(pairs: tuple[tuple[float, float], ...]) -> float | None:
    if len(pairs) < 2:
        return None
    xs = tuple(pair[0] for pair in pairs)
    ys = tuple(pair[1] for pair in pairs)
    x_mean, y_mean = mean(xs), mean(ys)
    x_scale = sum((value - x_mean) ** 2 for value in xs)
    y_scale = sum((value - y_mean) ** 2 for value in ys)
    if x_scale == 0.0 or y_scale == 0.0:
        return None
    return sum((x - x_mean) * (y - y_mean) for x, y in pairs) / math.sqrt(x_scale * y_scale)


def _safe_ratio(numerator: float | int, denominator: float | int) -> float | None:
    return None if denominator == 0 else float(numerator) / float(denominator)


def _validate_inputs(
    bundle: CanonicalResearchBundle,
    artifact: FeatureDefinitionArtifact,
    feature_rows: tuple[SetupRegimeFeatureRow, ...],
) -> None:
    validate_regime_feature_artifact(artifact)
    canonical_artifact = feature_definition_artifact(bundle)
    if artifact != canonical_artifact:
        raise ValueError("diagnosis requires the exact in-memory canonical FeatureDefinitionArtifact")
    if sha256(_artifact_bytes(artifact)).hexdigest() != FROZEN_FEATURE_ARTIFACT_SHA256:
        raise ValueError("frozen feature artifact SHA-256 mismatch")
    if tuple(definition.name for definition in artifact.definitions) != tuple(definition.name for definition in FEATURE_DEFINITIONS):
        raise ValueError("feature definition ordering mismatch")
    validate_regime_feature_selectors(NUMERIC_FEATURES + CATEGORICAL_FEATURES, artifact)
    canonical_rows = build_setup_regime_feature_rows(bundle)
    if feature_rows != canonical_rows:
        raise ValueError("diagnosis requires exact canonical setup-origin feature rows")
    if len({row.setup_id for row in feature_rows}) != len(feature_rows):
        raise ValueError("duplicate setup-origin feature row identity")


def _artifact_bytes(artifact: FeatureDefinitionArtifact) -> bytes:
    return json.dumps(asdict(artifact), sort_keys=True, separators=(",", ":"), allow_nan=False).encode() + b"\n"


def _development_population(
    bundle: CanonicalResearchBundle,
    feature_rows: tuple[SetupRegimeFeatureRow, ...],
) -> _Population:
    setup_by_id = {row.setup_id: row for row in bundle.dataset.setup_rows}
    if len(setup_by_id) != len(bundle.dataset.setup_rows):
        raise ValueError("duplicate canonical setup identity")
    feature_by_setup_id = {row.setup_id: row for row in feature_rows}
    if len(feature_by_setup_id) != len(feature_rows) or set(feature_by_setup_id) != set(setup_by_id):
        raise ValueError("setup-origin feature identities do not exactly match canonical setups")
    trade_by_id: dict[str, TradeRow] = {}
    trade_by_setup_id: dict[str, TradeRow] = {}
    for trade in bundle.dataset.trade_rows:
        if trade.trade_id in trade_by_id:
            raise ValueError("duplicate canonical trade identity")
        if trade.setup_id not in setup_by_id:
            raise ValueError("orphan trade setup identity")
        setup = setup_by_id[trade.setup_id]
        if trade.symbol != setup.symbol or trade.direction is not setup.direction:
            raise ValueError("trade/setup identity mismatch")
        if setup.linked_trade_id != trade.trade_id:
            raise ValueError("trade/setup linked trade identity mismatch")
        if trade.setup_id in trade_by_setup_id:
            raise ValueError("multiple trades linked to one setup identity")
        trade_by_id[trade.trade_id] = trade
        trade_by_setup_id[trade.setup_id] = trade

    ledger = {
        "setup_out_entry_in": 0,
        "setup_in_entry_out": 0,
        "setup_entry_in_exit_out": 0,
        "setup_entry_in_censored_unresolved": 0,
        "setup_entry_exit_included": 0,
    }
    for trade in bundle.dataset.trade_rows:
        setup_inside = _inside(setup_by_id[trade.setup_id].decision_timestamp)
        entry_inside = _inside(trade.decision_timestamp)
        if not (setup_inside or entry_inside):
            continue
        if not setup_inside and entry_inside:
            ledger["setup_out_entry_in"] += 1
        elif setup_inside and not entry_inside:
            ledger["setup_in_entry_out"] += 1
        elif trade.outcome_state == "censored":
            ledger["setup_entry_in_censored_unresolved"] += 1
        elif trade.outcome_state == "closed" and trade.exit_timestamp is not None and _inside(trade.exit_timestamp):
            ledger["setup_entry_exit_included"] += 1
        elif trade.outcome_state == "closed":
            ledger["setup_entry_in_exit_out"] += 1
        else:
            raise ValueError("unsupported boundary-audit trade outcome state")
    if sum(ledger.values()) != sum(
        _inside(setup_by_id[trade.setup_id].decision_timestamp) or _inside(trade.decision_timestamp)
        for trade in bundle.dataset.trade_rows
    ):
        raise AssertionError("development boundary audit must exhaust linked trades that touch development")

    observed = tuple(row for row in bundle.dataset.setup_rows if _inside(row.decision_timestamp))
    eligible = tuple(row for row in observed if row.eligible_baseline_setup)
    categories: dict[str, str] = {}
    included: dict[str, TradeRow] = {}
    for setup in eligible:
        trade = trade_by_setup_id.get(setup.setup_id)
        if trade is None:
            if setup.linked_trade_id is not None:
                raise ValueError("setup links to a missing trade identity")
            categories[setup.setup_id] = "non_open_eligible"
            continue
        entry_inside = _inside(trade.decision_timestamp)
        if entry_inside != _inside(setup.decision_timestamp):
            categories[setup.setup_id] = "purged_setup_boundary"
            continue
        if not entry_inside:
            raise AssertionError("development setup without a development entry must be boundary-purged")
        if trade.outcome_state == "censored":
            categories[setup.setup_id] = "censored_unresolved"
        elif trade.outcome_state == "closed":
            if trade.exit_timestamp is None:
                raise ValueError("closed trade lacks an exit timestamp")
            if _inside(trade.exit_timestamp):
                categories[setup.setup_id] = "included_closed"
                included[setup.setup_id] = trade
            else:
                categories[setup.setup_id] = "purged_entry_exit_boundary"
        else:
            raise ValueError("unsupported trade outcome state")
    if set(categories) != {setup.setup_id for setup in eligible}:
        raise AssertionError("development boundary categories must exhaust eligible setups")
    allowed = {
        "non_open_eligible", "purged_setup_boundary", "censored_unresolved",
        "purged_entry_exit_boundary", "included_closed",
    }
    if set(categories.values()) - allowed:
        raise AssertionError("unknown development boundary category")
    return _Population(
        observed, eligible, feature_by_setup_id, categories, included,
        BoundaryAudit(**ledger),
    )


def _numeric_summary(values: tuple[float | None, ...]) -> NumericSummary:
    present = tuple(value for value in values if value is not None)
    return NumericSummary(
        len(present), len(values) - len(present),
        None if not present else min(present), None if not present else max(present),
        None if not present else mean(present), None if not present else median(present),
    )


def _metrics(setup_ids: tuple[str, ...], population: _Population) -> BucketMetrics:
    ids = set(setup_ids)
    categories = tuple(population.category_by_setup_id[setup_id] for setup_id in setup_ids)
    trades = tuple(population.included_trade_by_setup_id[setup_id] for setup_id in setup_ids if setup_id in population.included_trade_by_setup_id)
    outcomes = tuple(_finite(trade.realized_r) for trade in trades)
    if any(value is None for value in outcomes):
        raise ValueError("included closed trade has nonfinite realized R")
    rs = tuple(float(value) for value in outcomes if value is not None)
    maes = tuple(value for trade in trades if (value := _finite(trade.mae_r)) is not None)
    mfes = tuple(value for trade in trades if (value := _finite(trade.mfe_r)) is not None)
    duration_bars = tuple(value for trade in trades if (value := _finite(trade.observed_duration_bars)) is not None)
    duration_ms = tuple(value for trade in trades if (value := _finite(trade.elapsed_duration_ms)) is not None)
    gains = sum(value for value in rs if value > 0.0)
    losses = sum(value for value in rs if value < 0.0)
    total = float(sum(rs))
    all_eligible = len(population.eligible_setups)
    return BucketMetrics(
        eligible_setups=len(setup_ids),
        opened_trades=sum(category in {"included_closed", "censored_unresolved", "purged_entry_exit_boundary"} for category in categories),
        closed_trades=len(trades), censored_unresolved=categories.count("censored_unresolved"),
        non_open=categories.count("non_open_eligible"),
        purged_setup_boundary=categories.count("purged_setup_boundary"),
        purged_entry_exit_boundary=categories.count("purged_entry_exit_boundary"),
        sample_retention=_safe_ratio(len(setup_ids), all_eligible), total_r=total,
        expectancy_r=_safe_ratio(total, len(rs)), r_per_setup=_safe_ratio(total, len(setup_ids)),
        opportunity_r_per_canonical_setup=_safe_ratio(total, all_eligible),
        profit_factor=None if losses == 0.0 else gains / abs(losses),
        win_rate=_safe_ratio(sum(value > 0.0 for value in rs), len(rs)),
        stop_rate=_safe_ratio(sum(trade.stop_hit is True for trade in trades), len(trades)),
        mean_r=None if not rs else mean(rs), median_r=None if not rs else median(rs),
        mean_mae_r=None if not maes else mean(maes), mean_mfe_r=None if not mfes else mean(mfes),
        mean_duration_bars=None if not duration_bars else mean(duration_bars),
        mean_duration_ms=None if not duration_ms else mean(duration_ms),
        positive_r_total=gains, negative_total_r=losses,
        negative_trade_count=sum(value < 0.0 for value in rs),
        stop_loss_count=sum(
            trade.stop_hit is True and float(trade.realized_r) < 0.0
            for trade in trades
        ),
        winners_ge_2r=sum(value >= 2.0 for value in rs),
        winners_ge_3r=sum(value >= 3.0 for value in rs),
        winners_ge_5r=sum(value >= 5.0 for value in rs),
        maximum_r=None if not rs else max(rs),
    )


def _concentrations(
    metrics: BucketMetrics, baseline: BucketMetrics, trades: tuple[TradeRow, ...],
    baseline_trades: tuple[TradeRow, ...],
) -> tuple[LossConcentration, WinnerConcentration]:
    low_follow = sum(_finite(trade.mfe_r) is not None and float(trade.mfe_r) < 1.0 for trade in trades)
    stop_loss_count = sum(
        trade.stop_hit is True and _finite(trade.realized_r) is not None and float(trade.realized_r) < 0.0
        for trade in trades
    )
    all_low_follow = sum(_finite(trade.mfe_r) is not None and float(trade.mfe_r) < 1.0 for trade in baseline_trades)
    all_stop_losses = sum(
        trade.stop_hit is True and _finite(trade.realized_r) is not None and float(trade.realized_r) < 0.0
        for trade in baseline_trades
    )
    baseline_negative_magnitude = abs(baseline.negative_total_r)
    return (
        LossConcentration(
            _safe_ratio(metrics.eligible_setups, baseline.eligible_setups),
            _safe_ratio(metrics.closed_trades, baseline.closed_trades),
            _safe_ratio(abs(metrics.negative_total_r), baseline_negative_magnitude),
            _safe_ratio(metrics.negative_trade_count, baseline.negative_trade_count),
            _safe_ratio(stop_loss_count, all_stop_losses), low_follow, _safe_ratio(low_follow, all_low_follow),
        ),
        WinnerConcentration(
            metrics.winners_ge_2r, _safe_ratio(metrics.winners_ge_2r, baseline.winners_ge_2r),
            metrics.winners_ge_3r, _safe_ratio(metrics.winners_ge_3r, baseline.winners_ge_3r),
            metrics.winners_ge_5r, _safe_ratio(metrics.winners_ge_5r, baseline.winners_ge_5r),
            _safe_ratio(metrics.positive_r_total, baseline.positive_r_total),
        ),
    )


def _bucket(
    label: str, setup_ids: tuple[str, ...], population: _Population, baseline: BucketMetrics,
    baseline_trades: tuple[TradeRow, ...], *, lower: float | None = None, upper: float | None = None,
    lower_inclusive: bool = False, upper_inclusive: bool = True,
) -> BucketDiagnosis:
    metrics = _metrics(setup_ids, population)
    trades = tuple(population.included_trade_by_setup_id[setup_id] for setup_id in setup_ids if setup_id in population.included_trade_by_setup_id)
    loss, winner = _concentrations(metrics, baseline, trades, baseline_trades)
    return BucketDiagnosis(label, lower, upper, lower_inclusive, upper_inclusive, metrics, loss, winner)


def _relationships(
    feature: str, population: _Population,
) -> tuple[Correlation, ...]:
    pairs: dict[str, list[tuple[float, float]]] = {name: [] for name in (
        "realized_r", "stop_hit", "win", "mfe_r", "mae_r", "observed_duration_bars",
    )}
    for setup_id, trade in population.included_trade_by_setup_id.items():
        value = _finite(getattr(population.feature_by_setup_id[setup_id], feature))
        if value is None:
            continue
        realized = _finite(trade.realized_r)
        outcomes = {
            "realized_r": realized,
            "stop_hit": None if trade.stop_hit is None else float(trade.stop_hit),
            "win": None if realized is None else float(realized > 0.0),
            "mfe_r": _finite(trade.mfe_r), "mae_r": _finite(trade.mae_r),
            "observed_duration_bars": _finite(trade.observed_duration_bars),
        }
        for name, outcome in outcomes.items():
            if outcome is not None:
                pairs[name].append((value, outcome))
    return tuple(Correlation(name, len(values), _pearson(tuple(values))) for name, values in pairs.items())


def _feature_diagnosis(
    name: str, kind: str, population: _Population, baseline: BucketMetrics, baseline_trades: tuple[TradeRow, ...], artifact: FeatureDefinitionArtifact,
) -> FeatureDiagnosis:
    definition = next(item for item in artifact.definitions if item.name == name)
    setup_ids = tuple(setup.setup_id for setup in population.eligible_setups)
    values = tuple(getattr(population.feature_by_setup_id[setup_id], name) for setup_id in setup_ids)
    if kind == "categorical":
        if any(value is not None and type(value) is not bool for value in values):
            raise ValueError(f"categorical {name} has a non-boolean value")
        buckets = (
            _bucket("false", tuple(setup_id for setup_id, value in zip(setup_ids, values) if value is False), population, baseline, baseline_trades),
            _bucket("true", tuple(setup_id for setup_id, value in zip(setup_ids, values) if value is True), population, baseline, baseline_trades),
            _bucket("missing", tuple(setup_id for setup_id, value in zip(setup_ids, values) if value is None), population, baseline, baseline_trades),
        )
        if sum(bucket.metrics.eligible_setups for bucket in buckets) != len(setup_ids):
            raise AssertionError("categorical buckets must exhaust development eligible setups")
        return FeatureDiagnosis(name, kind, definition.anchor, definition.mathematical_definition, None, None, None, (), None, (), buckets)
    numeric_values = tuple(_finite(value) for value in values)
    summary = _numeric_summary(numeric_values)
    present = tuple(value for value in numeric_values if value is not None)
    edges = tuple(float(value) for value in quantiles(present, n=4, method="inclusive")) if len(present) >= 2 else ()
    edge_fingerprint = fingerprint({"source": "development_eligible_nonmissing_setups", "method": "inclusive", "edges": edges})
    if edges:
        buckets = tuple(
            _bucket(
                f"Q{index + 1}",
                tuple(
                    setup_id for setup_id, value in zip(setup_ids, numeric_values)
                    if value is not None
                    and (index == 0 or value > edges[index - 1])
                    and (index == len(edges) or value <= edges[index])
                ),
                population, baseline, baseline_trades,
                lower=None if index == 0 else edges[index - 1],
                upper=None if index == len(edges) else edges[index],
                lower_inclusive=False, upper_inclusive=True,
            )
            for index in range(4)
        )
    else:
        buckets = (_bucket("all_nonmissing", tuple(setup_id for setup_id, value in zip(setup_ids, numeric_values) if value is not None), population, baseline, baseline_trades),)
    missing = _bucket("missing", tuple(setup_id for setup_id, value in zip(setup_ids, numeric_values) if value is None), population, baseline, baseline_trades)
    all_buckets = buckets + (missing,)
    if sum(bucket.metrics.eligible_setups for bucket in all_buckets) != len(setup_ids):
        raise AssertionError("feature buckets must exhaust development eligible setups")
    return FeatureDiagnosis(
        name, kind, definition.anchor, definition.mathematical_definition, summary,
        "development_eligible_nonmissing_setups", "inclusive", edges, edge_fingerprint,
        _relationships(name, population), all_buckets,
    )


def build_diagnosis_evidence(
    bundle: CanonicalResearchBundle,
    artifact: FeatureDefinitionArtifact,
    feature_rows: tuple[SetupRegimeFeatureRow, ...],
) -> DiagnosisEvidence:
    """Build development-only observational evidence; never choose a conclusion."""
    _validate_inputs(bundle, artifact, feature_rows)
    population = _development_population(bundle, feature_rows)
    all_ids = tuple(setup.setup_id for setup in population.eligible_setups)
    baseline = _metrics(all_ids, population)
    baseline_trades = tuple(population.included_trade_by_setup_id[setup_id] for setup_id in all_ids if setup_id in population.included_trade_by_setup_id)
    categories = tuple((name, sum(value == name for value in population.category_by_setup_id.values())) for name in (
        "non_open_eligible", "purged_setup_boundary", "purged_entry_exit_boundary", "censored_unresolved", "included_closed",
    ))
    if sum(count for _, count in categories) != len(population.eligible_setups):
        raise AssertionError("population categories must exhaust development eligible setups")
    features = tuple(
        _feature_diagnosis(name, "numeric", population, baseline, baseline_trades, artifact)
        for name in NUMERIC_FEATURES
    ) + tuple(
        _feature_diagnosis(name, "categorical", population, baseline, baseline_trades, artifact)
        for name in CATEGORICAL_FEATURES
    )
    evidence = DiagnosisEvidence(
        DIAGNOSIS_SCHEMA_VERSION, "stage_1", DEVELOPMENT_WINDOW.role,
        DEVELOPMENT_WINDOW.start_date, DEVELOPMENT_WINDOW.end_date, INACCESSIBLE_ROLES,
        SETUP_ORIGIN_ANCHOR, artifact.source_scope, artifact.manifest_id,
        artifact.dataset_fingerprint, artifact.strategy_fingerprint, artifact.replay_fingerprint,
        artifact.backtest_fingerprint, artifact.research_fingerprint, artifact.split_fingerprint,
        artifact.source_artifacts, artifact.source_counts, artifact.feature_version,
        artifact.definition_fingerprint, fingerprint(artifact), FROZEN_FEATURE_ARTIFACT_SHA256,
        len(population.observed_setups), len(population.eligible_setups), categories,
        population.boundary_audit, baseline, features,
    )
    return validate_diagnosis_evidence(evidence)


def _same_float(left: float | None, right: float | None) -> bool:
    if left is None or right is None:
        return left is right
    return math.isclose(left, right, rel_tol=1e-12, abs_tol=1e-12)


def _validate_bucket_aggregate(feature: FeatureDiagnosis, baseline: BucketMetrics) -> None:
    metrics = tuple(bucket.metrics for bucket in feature.buckets)
    if sum(item.eligible_setups for item in metrics) != baseline.eligible_setups:
        raise ValueError("diagnosis feature bucket exhaustiveness mismatch")
    additive = (
        "opened_trades", "closed_trades", "censored_unresolved", "non_open",
        "purged_setup_boundary", "purged_entry_exit_boundary", "negative_trade_count",
        "stop_loss_count", "winners_ge_2r", "winners_ge_3r", "winners_ge_5r",
    )
    for name in additive:
        if sum(getattr(item, name) for item in metrics) != getattr(baseline, name):
            raise ValueError(f"diagnosis baseline/bucket mismatch: {name}")
    for name in ("total_r", "positive_r_total", "negative_total_r"):
        if not _same_float(sum(getattr(item, name) for item in metrics), getattr(baseline, name)):
            raise ValueError(f"diagnosis baseline/bucket mismatch: {name}")


def _validate_metric_algebra(metrics: BucketMetrics, baseline_eligible: int) -> None:
    counts = (
        metrics.eligible_setups, metrics.opened_trades, metrics.closed_trades,
        metrics.censored_unresolved, metrics.non_open, metrics.purged_setup_boundary,
        metrics.purged_entry_exit_boundary, metrics.negative_trade_count,
        metrics.stop_loss_count, metrics.winners_ge_2r, metrics.winners_ge_3r,
        metrics.winners_ge_5r,
    )
    if any(not isinstance(value, int) or isinstance(value, bool) or value < 0 for value in counts):
        raise ValueError("diagnosis metric count is invalid")
    if (
        metrics.closed_trades > metrics.opened_trades
        or metrics.opened_trades > metrics.eligible_setups
        or metrics.negative_trade_count > metrics.closed_trades
        or metrics.stop_loss_count > metrics.closed_trades
        or any(value > metrics.closed_trades for value in (
            metrics.winners_ge_2r, metrics.winners_ge_3r, metrics.winners_ge_5r,
        ))
        or not (metrics.winners_ge_5r <= metrics.winners_ge_3r <= metrics.winners_ge_2r)
    ):
        raise ValueError("diagnosis metric count bounds mismatch")
    if sum((metrics.closed_trades, metrics.censored_unresolved, metrics.non_open, metrics.purged_setup_boundary, metrics.purged_entry_exit_boundary)) != metrics.eligible_setups:
        raise ValueError("diagnosis metric category exhaustiveness mismatch")
    expected_retention = _safe_ratio(metrics.eligible_setups, baseline_eligible)
    if not _same_float(metrics.sample_retention, expected_retention):
        raise ValueError("diagnosis metric sample retention mismatch")
    if not _same_float(metrics.expectancy_r, _safe_ratio(metrics.total_r, metrics.closed_trades)):
        raise ValueError("diagnosis metric expectancy mismatch")
    if not _same_float(metrics.r_per_setup, _safe_ratio(metrics.total_r, metrics.eligible_setups)):
        raise ValueError("diagnosis metric R-per-setup mismatch")
    if not _same_float(metrics.opportunity_r_per_canonical_setup, _safe_ratio(metrics.total_r, baseline_eligible)):
        raise ValueError("diagnosis metric opportunity R-per-setup mismatch")
    if not _same_float(metrics.mean_r, metrics.expectancy_r):
        raise ValueError("diagnosis metric mean/expectancy mismatch")
    expected_pf = None if metrics.negative_total_r == 0.0 else metrics.positive_r_total / abs(metrics.negative_total_r)
    if (
        metrics.positive_r_total < 0.0 or metrics.negative_total_r > 0.0
        or not _same_float(metrics.total_r, metrics.positive_r_total + metrics.negative_total_r)
        or not _same_float(metrics.profit_factor, expected_pf)
    ):
        raise ValueError("diagnosis metric profit-factor/totals mismatch")
    if metrics.closed_trades == 0:
        if any(value is not None for value in (metrics.win_rate, metrics.stop_rate, metrics.expectancy_r, metrics.mean_r, metrics.median_r, metrics.maximum_r)):
            raise ValueError("diagnosis empty metric outcome mismatch")
    else:
        for value, name in ((metrics.win_rate, "win rate"), (metrics.stop_rate, "stop rate")):
            if value is None or not 0.0 <= value <= 1.0 or not math.isclose(value * metrics.closed_trades, round(value * metrics.closed_trades), abs_tol=1e-12):
                raise ValueError(f"diagnosis metric {name} mismatch")
        if metrics.stop_loss_count > round(metrics.stop_rate * metrics.closed_trades):
            raise ValueError("diagnosis negative stop-loss count mismatch")


def _validate_concentrations(feature: FeatureDiagnosis, baseline: BucketMetrics) -> None:
    low_follow_total = sum(bucket.loss_concentration.low_follow_through_count for bucket in feature.buckets)
    for bucket in feature.buckets:
        metrics = bucket.metrics
        loss = bucket.loss_concentration
        winner = bucket.winner_concentration
        expected_loss = (
            _safe_ratio(metrics.eligible_setups, baseline.eligible_setups),
            _safe_ratio(metrics.closed_trades, baseline.closed_trades),
            _safe_ratio(abs(metrics.negative_total_r), abs(baseline.negative_total_r)),
            _safe_ratio(metrics.negative_trade_count, baseline.negative_trade_count),
            _safe_ratio(metrics.stop_loss_count, baseline.stop_loss_count),
            _safe_ratio(loss.low_follow_through_count, low_follow_total),
        )
        actual_loss = (
            loss.eligible_setup_share, loss.closed_trade_share, loss.negative_r_magnitude_share,
            loss.negative_trade_share, loss.stop_loss_share, loss.low_follow_through_share,
        )
        if any(not _same_float(actual, expected) for actual, expected in zip(actual_loss, expected_loss)):
            raise ValueError("diagnosis loss concentration mismatch")
        expected_winner = (
            _safe_ratio(metrics.winners_ge_2r, baseline.winners_ge_2r),
            _safe_ratio(metrics.winners_ge_3r, baseline.winners_ge_3r),
            _safe_ratio(metrics.winners_ge_5r, baseline.winners_ge_5r),
            _safe_ratio(metrics.positive_r_total, baseline.positive_r_total),
        )
        actual_winner = (
            winner.winners_ge_2r_share, winner.winners_ge_3r_share,
            winner.winners_ge_5r_share, winner.positive_r_share,
        )
        if any(not _same_float(actual, expected) for actual, expected in zip(actual_winner, expected_winner)):
            raise ValueError("diagnosis winner concentration mismatch")


def validate_diagnosis_evidence(evidence: DiagnosisEvidence) -> DiagnosisEvidence:
    """Validate every frozen structural boundary before final artifact use."""
    if not isinstance(evidence, DiagnosisEvidence):
        raise TypeError("DiagnosisEvidence is required")
    if (
        evidence.schema_version != DIAGNOSIS_SCHEMA_VERSION or evidence.stage != "stage_1"
        or evidence.role != DEVELOPMENT_WINDOW.role
        or (evidence.start_date, evidence.end_date) != (DEVELOPMENT_WINDOW.start_date, DEVELOPMENT_WINDOW.end_date)
        or evidence.inaccessible_roles != INACCESSIBLE_ROLES
        or evidence.anchor != SETUP_ORIGIN_ANCHOR
        or evidence.source_scope != _EXPECTED_SOURCE_SCOPE
        or evidence.feature_definition_version != REGIME_FEATURE_VERSION
        or evidence.feature_definition_fingerprint != FEATURE_DEFINITION_FINGERPRINT
        or evidence.feature_artifact_fingerprint != EXPECTED_FEATURE_ARTIFACT_FINGERPRINT
        or evidence.feature_artifact_sha256 != FROZEN_FEATURE_ARTIFACT_SHA256
    ):
        raise ValueError("diagnosis evidence schema/role/feature binding mismatch")
    observed = {name: getattr(evidence, name) for name in _EXPECTED_PROVENANCE}
    if observed != _EXPECTED_PROVENANCE:
        raise ValueError("diagnosis evidence canonical provenance mismatch")
    if evidence.source_counts != _EXPECTED_SOURCE_COUNTS or tuple(asdict(item) for item in evidence.source_artifacts) != _EXPECTED_SOURCE_ARTIFACTS:
        raise ValueError("diagnosis evidence canonical source binding mismatch")
    expected_categories = (
        ("non_open_eligible", 36), ("purged_setup_boundary", 0),
        ("purged_entry_exit_boundary", 0), ("censored_unresolved", 0),
        ("included_closed", 103),
    )
    if evidence.observed_flips != 276 or evidence.eligible_setups != 139:
        raise ValueError("diagnosis evidence frozen population mismatch")
    if evidence.population_categories != expected_categories:
        raise ValueError("diagnosis evidence population category ordering mismatch")
    if sum(count for _, count in evidence.population_categories) != evidence.eligible_setups:
        raise ValueError("diagnosis evidence population categories do not exhaust eligible setups")
    audit = evidence.boundary_audit
    if audit != BoundaryAudit(1, 0, 0, 0, 103):
        raise ValueError("diagnosis evidence frozen boundary audit mismatch")
    expected = tuple((name, "numeric") for name in NUMERIC_FEATURES) + tuple((name, "categorical") for name in CATEGORICAL_FEATURES)
    if tuple((item.name, item.kind) for item in evidence.features) != expected:
        raise ValueError("diagnosis evidence feature set/order mismatch")
    baseline = evidence.baseline_metrics
    if baseline.eligible_setups != evidence.eligible_setups or baseline.sample_retention != 1.0:
        raise ValueError("diagnosis evidence baseline eligible/setup retention mismatch")
    _validate_metric_algebra(baseline, evidence.eligible_setups)
    for feature in evidence.features:
        if feature.anchor != SETUP_ORIGIN_ANCHOR:
            raise ValueError("diagnosis evidence feature anchor mismatch")
        for bucket in feature.buckets:
            _validate_metric_algebra(bucket.metrics, evidence.eligible_setups)
        _validate_bucket_aggregate(feature, baseline)
        _validate_concentrations(feature, baseline)
        if feature.kind == "numeric":
            if feature.numeric_summary is None or feature.quartile_source != "development_eligible_nonmissing_setups" or feature.quantile_method != "inclusive":
                raise ValueError("diagnosis numeric feature metadata mismatch")
            if feature.numeric_summary.count + feature.numeric_summary.missing != evidence.eligible_setups:
                raise ValueError("diagnosis numeric summary count/missing mismatch")
            if feature.edge_fingerprint != fingerprint({"source": feature.quartile_source, "method": feature.quantile_method, "edges": feature.quantile_edges}):
                raise ValueError("diagnosis numeric edge fingerprint mismatch")
            if feature.numeric_summary.count < 2 and feature.quantile_edges:
                raise ValueError("diagnosis numeric warm-up edges mismatch")
        elif feature.kind == "categorical":
            if feature.numeric_summary is not None or feature.quantile_edges or feature.edge_fingerprint is not None:
                raise ValueError("diagnosis categorical feature metadata mismatch")
            if tuple(bucket.label for bucket in feature.buckets) != ("false", "true", "missing"):
                raise ValueError("diagnosis categorical bucket ordering mismatch")
        else:
            raise ValueError("diagnosis feature kind mismatch")
    # Strict JSON canonicalization also rejects all nonfinite values.  This
    # immutable digest binds every required summary, correlation, bucket
    # metadata, and concentration field beyond individually checked algebra.
    if fingerprint(evidence) != EXPECTED_DIAGNOSIS_EVIDENCE_FINGERPRINT:
        raise ValueError("diagnosis evidence immutable fingerprint mismatch")
    return evidence


def finalize_diagnosis(evidence: DiagnosisEvidence, conclusion: str) -> FinalDiagnosis:
    if not isinstance(evidence, DiagnosisEvidence):
        raise TypeError("DiagnosisEvidence is required")
    validate_diagnosis_evidence(evidence)
    if conclusion not in {"SUPPORTED", "NOT_SUPPORTED", "INCONCLUSIVE"}:
        raise ValueError("conclusion must be SUPPORTED, NOT_SUPPORTED, or INCONCLUSIVE")
    return FinalDiagnosis(evidence, conclusion)


def validate_final_diagnosis(value: FinalDiagnosis) -> FinalDiagnosis:
    """Validate the evidence and the Sol-selected finite conclusion token."""
    if not isinstance(value, FinalDiagnosis):
        raise TypeError("FinalDiagnosis is required")
    validate_diagnosis_evidence(value.evidence)
    if value.conclusion not in {"SUPPORTED", "NOT_SUPPORTED", "INCONCLUSIVE"}:
        raise ValueError("final diagnosis conclusion is invalid")
    return value


def diagnosis_json(value: FinalDiagnosis) -> bytes:
    validate_final_diagnosis(value)
    return json.dumps(asdict(value), sort_keys=True, separators=(",", ":"), allow_nan=False).encode() + b"\n"


def write_diagnosis(value: FinalDiagnosis, path: Path, *, overwrite: bool = False) -> None:
    validate_final_diagnosis(value)
    if path.exists() and not overwrite:
        raise FileExistsError(f"refusing to overwrite existing diagnosis artifact: {path}")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(diagnosis_json(value))
