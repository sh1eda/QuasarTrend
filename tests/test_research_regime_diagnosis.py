from __future__ import annotations

from dataclasses import replace
import json
from pathlib import Path
from statistics import quantiles
from types import SimpleNamespace

import pytest

from quasartrend.research.pipeline import build_canonical_bundle
from quasartrend.research.provenance import fingerprint
from quasartrend.research.regime_diagnosis import (
    DEVELOPMENT_WINDOW,
    FROZEN_FEATURE_ARTIFACT_SHA256,
    _development_population,
    _bucket,
    _metrics,
    _Population,
    _feature_diagnosis,
    _pearson,
    build_diagnosis_evidence,
    diagnosis_json,
    finalize_diagnosis,
    validate_diagnosis_evidence,
    validate_final_diagnosis,
    write_diagnosis,
)
from quasartrend.research.regime_features import (
    build_setup_regime_feature_rows,
    feature_definition_artifact,
)


GOLDEN_15M = Path("tests/golden/tradingview_15m.csv")
GOLDEN_4H = Path("tests/golden/tradingview_4h.csv")


@pytest.fixture(scope="module")
def inputs():
    bundle = build_canonical_bundle(golden_15m=GOLDEN_15M, golden_4h=GOLDEN_4H)
    artifact = feature_definition_artifact(bundle)
    rows = build_setup_regime_feature_rows(bundle)
    return bundle, artifact, rows


def test_development_evidence_is_deterministic_and_setup_origin_only(inputs) -> None:
    bundle, artifact, rows = inputs
    first = build_diagnosis_evidence(bundle, artifact, rows)
    second = build_diagnosis_evidence(bundle, artifact, rows)
    assert first == second
    assert first.role == "development"
    assert first.inaccessible_roles == ("validation", "final_oos")
    assert first.anchor == "setup_origin"
    assert first.feature_artifact_sha256 == FROZEN_FEATURE_ARTIFACT_SHA256
    assert len(first.features) == 11
    assert all(feature.anchor == "setup_origin" for feature in first.features)
    assert sum(count for _, count in first.population_categories) == first.eligible_setups
    assert all(
        sum(bucket.metrics.eligible_setups for bucket in feature.buckets) == first.eligible_setups
        for feature in first.features
    )


def test_numeric_edges_are_exact_inclusive_development_eligible_setup_edges(inputs) -> None:
    bundle, artifact, rows = inputs
    evidence = build_diagnosis_evidence(bundle, artifact, rows)
    feature = next(item for item in evidence.features if item.name == "directional_efficiency_8")
    eligible = {
        setup.setup_id for setup in bundle.dataset.setup_rows
        if setup.eligible_baseline_setup
        and DEVELOPMENT_WINDOW.start_ms <= setup.decision_timestamp <= DEVELOPMENT_WINDOW.end_ms
    }
    values = tuple(row.directional_efficiency_8 for row in rows if row.setup_id in eligible and row.directional_efficiency_8 is not None)
    assert feature.quantile_method == "inclusive"
    assert feature.quantile_edges == tuple(quantiles(values, n=4, method="inclusive"))
    assert tuple(bucket.label for bucket in feature.buckets[:4]) == ("Q1", "Q2", "Q3", "Q4")
    assert feature.buckets[-1].label == "missing"


def test_population_rejects_duplicate_orphan_and_trade_setup_mismatch(inputs) -> None:
    bundle, _, rows = inputs
    trade = bundle.dataset.trade_rows[0]
    duplicate = replace(bundle, dataset=replace(bundle.dataset, trade_rows=bundle.dataset.trade_rows + (trade,)))
    with pytest.raises(ValueError, match="duplicate canonical trade"):
        _development_population(duplicate, rows)
    orphan = replace(trade, setup_id="not-a-setup")
    with pytest.raises(ValueError, match="orphan"):
        _development_population(replace(bundle, dataset=replace(bundle.dataset, trade_rows=(orphan,))), rows)
    mismatch = replace(trade, symbol="OTHER")
    with pytest.raises(ValueError, match="trade/setup identity mismatch"):
        _development_population(replace(bundle, dataset=replace(bundle.dataset, trade_rows=(mismatch,))), rows)


def test_censored_and_boundary_categories_are_explicit_and_excluded_from_realized_metrics(inputs) -> None:
    bundle, artifact, rows = inputs
    eligible = next(
        setup for setup in bundle.dataset.setup_rows
        if setup.eligible_baseline_setup and DEVELOPMENT_WINDOW.start_ms <= setup.decision_timestamp <= DEVELOPMENT_WINDOW.end_ms
    )
    trade = next(row for row in bundle.dataset.trade_rows if row.setup_id == eligible.setup_id)
    censored = replace(trade, outcome_state="censored", exit_timestamp=None)
    forged = replace(bundle, dataset=replace(bundle.dataset, trade_rows=tuple(
        censored if row.trade_id == trade.trade_id else row for row in bundle.dataset.trade_rows
    )))
    population = _development_population(forged, rows)
    assert population.category_by_setup_id[eligible.setup_id] == "censored_unresolved"
    assert eligible.setup_id not in population.included_trade_by_setup_id
    # A full evidence build deliberately rejects the altered canonical binding
    # before it can consume any altered outcomes.
    with pytest.raises(ValueError, match="binding mismatch"):
        build_diagnosis_evidence(forged, artifact, rows)


def test_correlation_known_and_zero_variance_behavior() -> None:
    assert _pearson(((1.0, 1.0), (2.0, 2.0), (3.0, 3.0))) == pytest.approx(1.0)
    assert _pearson(((1.0, 1.0), (1.0, 2.0))) is None
    assert _pearson(((1.0, 1.0),)) is None


def test_synthetic_bucket_metrics_and_negative_stop_loss_concentration() -> None:
    def trade(*, realized_r, stop_hit, mae_r, mfe_r, bars, elapsed):
        return SimpleNamespace(
            realized_r=realized_r, stop_hit=stop_hit, mae_r=mae_r, mfe_r=mfe_r,
            observed_duration_bars=bars, elapsed_duration_ms=elapsed,
        )
    included = {
        "s1": trade(realized_r=2.0, stop_hit=False, mae_r=1.0, mfe_r=3.0, bars=2, elapsed=100),
        "s2": trade(realized_r=-1.0, stop_hit=True, mae_r=2.0, mfe_r=.5, bars=4, elapsed=200),
    }
    population = _Population(
        (), (SimpleNamespace(setup_id="s1"), SimpleNamespace(setup_id="s2"), SimpleNamespace(setup_id="s3")),
        {}, {"s1": "included_closed", "s2": "included_closed", "s3": "censored_unresolved"},
        included, SimpleNamespace(),
    )
    metrics = _metrics(("s1", "s2", "s3"), population)
    assert (metrics.total_r, metrics.expectancy_r, metrics.r_per_setup, metrics.opportunity_r_per_canonical_setup) == pytest.approx((1.0, .5, 1 / 3, 1 / 3))
    assert (metrics.profit_factor, metrics.win_rate, metrics.stop_rate, metrics.mean_r, metrics.median_r) == pytest.approx((2.0, .5, .5, .5, .5))
    assert (metrics.mean_mae_r, metrics.mean_mfe_r, metrics.mean_duration_bars, metrics.mean_duration_ms) == pytest.approx((1.5, 1.75, 3.0, 150.0))
    assert (metrics.positive_r_total, metrics.negative_total_r, metrics.negative_trade_count, metrics.stop_loss_count) == (2.0, -1.0, 1, 1)
    assert (metrics.winners_ge_2r, metrics.winners_ge_3r, metrics.winners_ge_5r, metrics.maximum_r) == (1, 0, 0, 2.0)
    bucket = _bucket("all", ("s1", "s2", "s3"), population, metrics, tuple(included.values()))
    assert bucket.loss_concentration.stop_loss_share == 1.0
    assert bucket.loss_concentration.low_follow_through_count == 1
    assert bucket.winner_concentration.winners_ge_2r_share == 1.0


def test_boundary_audit_covers_setup_entry_crossings(inputs) -> None:
    bundle, _, rows = inputs
    trade = bundle.dataset.trade_rows[0]
    setup = next(row for row in bundle.dataset.setup_rows if row.setup_id == trade.setup_id)
    setup_out = replace(setup, decision_timestamp=DEVELOPMENT_WINDOW.start_ms - 1)
    entry_in = replace(trade, decision_timestamp=DEVELOPMENT_WINDOW.start_ms)
    altered_setups = tuple(setup_out if row.setup_id == setup.setup_id else row for row in bundle.dataset.setup_rows)
    altered_trades = tuple(entry_in if row.trade_id == trade.trade_id else row for row in bundle.dataset.trade_rows)
    population = _development_population(replace(bundle, dataset=replace(bundle.dataset, setup_rows=altered_setups, trade_rows=altered_trades)), rows)
    assert population.boundary_audit.setup_out_entry_in >= 1
    setup_in = replace(setup, decision_timestamp=DEVELOPMENT_WINDOW.start_ms)
    entry_out = replace(trade, decision_timestamp=DEVELOPMENT_WINDOW.end_ms + 1)
    altered_setups = tuple(setup_in if row.setup_id == setup.setup_id else row for row in bundle.dataset.setup_rows)
    altered_trades = tuple(entry_out if row.trade_id == trade.trade_id else row for row in bundle.dataset.trade_rows)
    population = _development_population(replace(bundle, dataset=replace(bundle.dataset, setup_rows=altered_setups, trade_rows=altered_trades)), rows)
    assert population.boundary_audit.setup_in_entry_out >= 1


def test_duplicate_inclusive_edges_retain_empty_buckets_and_missing_exhaustion(inputs) -> None:
    _, artifact, _ = inputs
    ids = ("a", "b", "c", "d")
    population = _Population(
        (), tuple(SimpleNamespace(setup_id=item) for item in ids),
        {item: SimpleNamespace(hema_fast_slope_atr_8=(None if item == "d" else 1.0)) for item in ids},
        {item: "non_open_eligible" for item in ids}, {}, SimpleNamespace(),
    )
    baseline = _metrics(ids, population)
    diagnosis = _feature_diagnosis("hema_fast_slope_atr_8", "numeric", population, baseline, (), artifact)
    assert diagnosis.quantile_edges == (1.0, 1.0, 1.0)
    assert tuple(bucket.metrics.eligible_setups for bucket in diagnosis.buckets) == (3, 0, 0, 0, 1)


def test_definition_mutation_and_wrong_rows_are_rejected(inputs) -> None:
    bundle, artifact, rows = inputs
    with pytest.raises(ValueError, match="definition/version/anchor"):
        build_diagnosis_evidence(bundle, replace(artifact, anchor="trade_entry"), rows)
    with pytest.raises(ValueError, match="exact canonical setup-origin"):
        build_diagnosis_evidence(bundle, artifact, rows[:-1])


def test_finalization_json_is_deterministic_nan_safe_and_has_no_nondevelopment_ids(inputs, tmp_path) -> None:
    bundle, artifact, rows = inputs
    final = finalize_diagnosis(build_diagnosis_evidence(bundle, artifact, rows), "INCONCLUSIVE")
    first = diagnosis_json(final)
    assert first == diagnosis_json(final) and first.endswith(b"\n") and b"NaN" not in first
    payload = json.loads(first)
    assert payload["conclusion"] == "INCONCLUSIVE"
    assert payload["evidence"]["inaccessible_roles"] == ["validation", "final_oos"]
    assert "trade_id" not in first.decode() and "setup_id" not in first.decode()
    left = tmp_path / "one" / "diagnosis.json"
    right = tmp_path / "two" / "diagnosis.json"
    write_diagnosis(final, left)
    write_diagnosis(final, right)
    assert left.read_bytes() == right.read_bytes() == first
    with pytest.raises(FileExistsError, match="refusing"):
        write_diagnosis(final, left)
    with pytest.raises(ValueError, match="conclusion"):
        finalize_diagnosis(final.evidence, "MAYBE")


@pytest.mark.parametrize(
    "forged",
    (
        lambda evidence: replace(evidence, role="validation"),
        lambda evidence: replace(evidence, inaccessible_roles=("final_oos",)),
        lambda evidence: replace(evidence, observed_flips=evidence.observed_flips + 1),
        lambda evidence: replace(evidence, population_categories=(("non_open_eligible", 35),) + evidence.population_categories[1:]),
        lambda evidence: replace(evidence, boundary_audit=replace(evidence.boundary_audit, setup_entry_exit_included=102)),
        lambda evidence: replace(evidence, source_scope="forged"),
        lambda evidence: replace(evidence, features=evidence.features[:-1]),
        lambda evidence: replace(evidence, baseline_metrics=replace(evidence.baseline_metrics, total_r=evidence.baseline_metrics.total_r + 1.0)),
    ),
)
def test_evidence_validation_rejects_forged_structure(inputs, forged) -> None:
    bundle, artifact, rows = inputs
    evidence = build_diagnosis_evidence(bundle, artifact, rows)
    with pytest.raises(ValueError):
        validate_diagnosis_evidence(forged(evidence))
    with pytest.raises(ValueError):
        finalize_diagnosis(forged(evidence), "INCONCLUSIVE")


def test_final_diagnosis_rejects_forged_conclusion_bucket_and_concentration(inputs) -> None:
    bundle, artifact, rows = inputs
    evidence = build_diagnosis_evidence(bundle, artifact, rows)
    final = finalize_diagnosis(evidence, "INCONCLUSIVE")
    with pytest.raises(ValueError, match="conclusion"):
        validate_final_diagnosis(replace(final, conclusion="FORGED"))
    with pytest.raises(ValueError, match="conclusion"):
        diagnosis_json(replace(final, conclusion="FORGED"))
    feature = evidence.features[0]
    bucket = feature.buckets[0]
    bad_metric_feature = replace(feature, buckets=(replace(bucket, metrics=replace(bucket.metrics, expectancy_r=0.0)),) + feature.buckets[1:])
    with pytest.raises(ValueError):
        validate_diagnosis_evidence(replace(evidence, features=(bad_metric_feature,) + evidence.features[1:]))
    bad_concentration_feature = replace(feature, buckets=(replace(bucket, loss_concentration=replace(bucket.loss_concentration, eligible_setup_share=0.0)),) + feature.buckets[1:])
    with pytest.raises(ValueError):
        validate_diagnosis_evidence(replace(evidence, features=(bad_concentration_feature,) + evidence.features[1:]))


@pytest.mark.parametrize("mutation", (
    lambda evidence: replace(evidence, baseline_metrics=replace(evidence.baseline_metrics, median_r=(evidence.baseline_metrics.median_r or 0.0) + .125)),
    lambda evidence: replace(evidence, baseline_metrics=replace(evidence.baseline_metrics, mean_mae_r=(evidence.baseline_metrics.mean_mae_r or 0.0) + .125)),
    lambda evidence: replace(evidence, baseline_metrics=replace(evidence.baseline_metrics, mean_mfe_r=(evidence.baseline_metrics.mean_mfe_r or 0.0) + .125)),
    lambda evidence: replace(evidence, baseline_metrics=replace(evidence.baseline_metrics, mean_duration_bars=(evidence.baseline_metrics.mean_duration_bars or 0.0) + .125)),
    lambda evidence: replace(evidence, baseline_metrics=replace(evidence.baseline_metrics, mean_duration_ms=(evidence.baseline_metrics.mean_duration_ms or 0.0) + .125)),
    lambda evidence: replace(evidence, baseline_metrics=replace(evidence.baseline_metrics, maximum_r=(evidence.baseline_metrics.maximum_r or 0.0) + .125)),
))
def test_evidence_fingerprint_rejects_forged_baseline_summary_fields(inputs, mutation) -> None:
    bundle, artifact, rows = inputs
    with pytest.raises(ValueError, match="immutable fingerprint"):
        validate_diagnosis_evidence(mutation(build_diagnosis_evidence(bundle, artifact, rows)))


def test_evidence_fingerprint_rejects_forged_feature_summary_correlation_and_bucket_metadata(inputs) -> None:
    bundle, artifact, rows = inputs
    evidence = build_diagnosis_evidence(bundle, artifact, rows)
    feature = evidence.features[0]
    with pytest.raises(ValueError, match="immutable fingerprint"):
        validate_diagnosis_evidence(replace(evidence, features=(replace(feature, numeric_summary=replace(feature.numeric_summary, median=(feature.numeric_summary.median or 0.0) + .125)),) + evidence.features[1:]))
    with pytest.raises(ValueError, match="immutable fingerprint"):
        validate_diagnosis_evidence(replace(evidence, features=(replace(feature, continuous_relationships=(replace(feature.continuous_relationships[0], pearson_r=0.0),) + feature.continuous_relationships[1:]),) + evidence.features[1:]))
    bucket = feature.buckets[0]
    with pytest.raises(ValueError, match="immutable fingerprint"):
        validate_diagnosis_evidence(replace(evidence, features=(replace(feature, buckets=(replace(bucket, label="FORGED"),) + feature.buckets[1:]),) + evidence.features[1:]))
    with pytest.raises(ValueError, match="immutable fingerprint"):
        validate_diagnosis_evidence(replace(evidence, features=(replace(feature, buckets=(replace(bucket, lower_bound=0.0),) + feature.buckets[1:]),) + evidence.features[1:]))
    forged_edges = tuple(edge + .01 for edge in feature.quantile_edges)
    forged_feature = replace(
        feature, quantile_edges=forged_edges,
        edge_fingerprint=fingerprint({"source": feature.quartile_source, "method": feature.quantile_method, "edges": forged_edges}),
    )
    with pytest.raises(ValueError, match="immutable fingerprint"):
        validate_diagnosis_evidence(replace(evidence, features=(forged_feature,) + evidence.features[1:]))


def test_evidence_fingerprint_rejects_self_consistent_low_follow_through_forgery(inputs) -> None:
    bundle, artifact, rows = inputs
    evidence = build_diagnosis_evidence(bundle, artifact, rows)
    feature = evidence.features[0]
    forged_buckets = tuple(
        replace(bucket, loss_concentration=replace(bucket.loss_concentration, low_follow_through_count=0, low_follow_through_share=None))
        for bucket in feature.buckets
    )
    with pytest.raises(ValueError, match="immutable fingerprint"):
        validate_diagnosis_evidence(replace(evidence, features=(replace(feature, buckets=forged_buckets),) + evidence.features[1:]))
