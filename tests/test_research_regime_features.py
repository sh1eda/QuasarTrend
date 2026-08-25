from __future__ import annotations

from dataclasses import replace
import json
import math
from pathlib import Path

import pytest

from quasartrend.research.models import FieldClass
from quasartrend.research.pipeline import build_canonical_bundle
from quasartrend.research.regime_features import (
    FEATURE_DEFINITIONS,
    FEATURE_DEFINITION_FINGERPRINT,
    PHASE7_1_BASE_SHA,
    SETUP_ORIGIN_ANCHOR,
    SetupRegimeFeatureRow,
    build_setup_regime_feature_rows,
    directional_efficiency,
    feature_definition_artifact,
    feature_definition_json,
    flip_counts_16,
    hema_fast_slope_atr,
    validate_regime_feature_artifact,
    validate_regime_feature_selectors,
    write_feature_definition_artifact,
)
from quasartrend.strategy import Direction, StrategyBar
from quasartrend.strategy import EventType, ReasonCode
from quasartrend.replay import ReplayConfig
from quasartrend.strategy import StrategyConfig


GOLDEN_15M = Path("tests/golden/tradingview_15m.csv")
GOLDEN_4H = Path("tests/golden/tradingview_4h.csv")


@pytest.fixture(scope="module")
def bundle():
    return build_canonical_bundle(golden_15m=GOLDEN_15M, golden_4h=GOLDEN_4H)


def _strategy_bar(*, hema_flip: Direction | None = None, kalman_transition: Direction | None = None) -> StrategyBar:
    hema = hema_flip or Direction.LONG
    kalman = kalman_transition or Direction.LONG
    return StrategyBar(
        "BTCUSDT", 0, 100.0, 100.0, 100.0, 100.0,
        Direction.LONG, hema, kalman, 1.0,
        hema_flip=hema_flip, kalman_transition=kalman_transition,
    )


def test_exact_hema_slope_sign_normalization_and_warmup() -> None:
    values = tuple(float(value) for value in range(9))
    times = tuple(index * 900_000 for index in range(9))
    assert hema_fast_slope_atr(values, 7, Direction.LONG, 2.0, open_times=times) is None
    assert hema_fast_slope_atr(values, 8, Direction.LONG, 2.0, open_times=times) == pytest.approx(.5)
    assert hema_fast_slope_atr(values, 8, Direction.SHORT, 2.0, open_times=times) == pytest.approx(-.5)
    assert hema_fast_slope_atr(values, 8, Direction.LONG, 0.0, open_times=times) is None
    with pytest.raises(ValueError, match="align"):
        hema_fast_slope_atr(values, 8, Direction.LONG, 2.0, open_times=times[:-1])


def test_directional_efficiency_is_causal_and_has_fixed_zero_range_behavior() -> None:
    monotonic = (0.0, 1.0, 2.0, 3.0, 4.0)
    oscillating = (0.0, 1.0, 0.0, 1.0, 2.0)
    times = tuple(index * 900_000 for index in range(5))
    assert directional_efficiency(monotonic, 3, 4, open_times=times) is None
    assert directional_efficiency(monotonic, 4, 4, open_times=times) == 1.0
    assert directional_efficiency(oscillating, 4, 4, open_times=times) == .5
    assert directional_efficiency((7.0,) * 5, 4, 4, open_times=times) == 0.0
    # Appending or changing future closes cannot affect a value at index 4.
    assert directional_efficiency(monotonic + (999.0,), 4, 4, open_times=times + (4_500_000,)) == 1.0
    gap = (0, 900_000, 1_800_000, 3_600_000, 4_500_000)
    assert directional_efficiency(monotonic, 4, 4, open_times=gap) is None
    with pytest.raises(ValueError, match="align"):
        directional_efficiency(monotonic, 4, 4, open_times=times[:-1])


def test_flip_window_includes_current_and_excludes_future() -> None:
    bars = tuple(
        _strategy_bar(
            hema_flip=Direction.LONG if index in {0, 15, 16} else None,
            kalman_transition=Direction.SHORT if index in {1, 15, 16} else None,
        )
        for index in range(17)
    )
    times = tuple(index * 900_000 for index in range(17))
    assert flip_counts_16(bars, 14, open_times=times) == (None, None, None)
    assert flip_counts_16(bars, 15, open_times=times) == (2, 2, 4)
    # The future event at index 16 was excluded at index 15.  At index 16,
    # index 0 falls out of the exact current-plus-prior-15-bar window.
    assert flip_counts_16(bars, 16, open_times=times) == (2, 3, 5)
    gapped = times[:8] + (times[8] + 900_000,) + tuple(value + 900_000 for value in times[9:])
    assert flip_counts_16(bars, 16, open_times=gapped) == (None, None, None)


def test_definitions_are_complete_frozen_entry_time_metadata() -> None:
    assert len(FEATURE_DEFINITIONS) == 11
    assert len({definition.name for definition in FEATURE_DEFINITIONS}) == 11
    assert all(definition.field_class is FieldClass.ENTRY_TIME_FEATURE for definition in FEATURE_DEFINITIONS)
    assert all(definition.feature_version == "phase7.2-regime-setup-origin-features/v1" for definition in FEATURE_DEFINITIONS)
    assert all(definition.anchor == SETUP_ORIGIN_ANCHOR for definition in FEATURE_DEFINITIONS)
    for definition in FEATURE_DEFINITIONS:
        assert all(getattr(definition, field) for field in (
            "mathematical_definition", "lookback", "normalization",
            "decision_timestamp", "missing_behavior", "warmup_behavior", "source_state",
        ))
    assert len(FEATURE_DEFINITION_FINGERPRINT) == 64
    assert all(definition.decision_timestamp == "canonical SetupRow.decision_timestamp at the HEMA-flip origin" for definition in FEATURE_DEFINITIONS)
    assert all("gap" in definition.missing_behavior or definition.name in {
        "kalman_persistence_bars", "hema_kalman_aligned", "htf_hema_aligned", "atr_adr_ratio",
    } for definition in FEATURE_DEFINITIONS)


def test_selector_validation_is_bound_to_frozen_definitions(bundle) -> None:
    artifact = feature_definition_artifact(bundle)
    assert validate_regime_feature_selectors(("directional_efficiency_8", "atr_adr_ratio"), artifact) == (
        "directional_efficiency_8", "atr_adr_ratio",
    )
    with pytest.raises(ValueError, match="unique"):
        validate_regime_feature_selectors(("atr_adr_ratio", "atr_adr_ratio"), artifact)
    with pytest.raises(ValueError, match="unknown"):
        validate_regime_feature_selectors(("realized_r",), artifact)
    with pytest.raises(ValueError, match="definition/version/anchor"):
        validate_regime_feature_selectors(("atr_adr_ratio",), replace(artifact, anchor="trade_entry"))
    with pytest.raises(TypeError, match="FeatureDefinitionArtifact"):
        validate_regime_feature_selectors(("atr_adr_ratio",), object())  # type: ignore[arg-type]


def test_golden_population_identity_order_and_entry_time_values(bundle) -> None:
    rows = build_setup_regime_feature_rows(bundle)
    assert len(rows) == len(bundle.dataset.setup_rows) == 523
    assert all(isinstance(row, SetupRegimeFeatureRow) for row in rows)
    assert tuple(row.setup_id for row in rows) == tuple(row.setup_id for row in bundle.dataset.setup_rows)
    assert tuple(row.decision_timestamp for row in rows) == tuple(
        row.decision_timestamp for row in bundle.dataset.setup_rows
    )
    setup_by_id = {setup.setup_id: setup for setup in bundle.dataset.setup_rows}
    assert all(row.kalman_persistence_bars == setup_by_id[row.setup_id].kalman_persistence_bars for row in rows)
    assert any(row.atr_adr_ratio is None for row in rows)
    assert any(row.atr_adr_ratio is not None for row in rows)
    for row in rows:
        for value in (
            row.hema_fast_slope_atr_8, row.directional_efficiency_8,
            row.directional_efficiency_16, row.directional_efficiency_32, row.atr_adr_ratio,
        ):
            assert value is None or math.isfinite(value)
        assert row.combined_flip_count_16 is None or row.combined_flip_count_16 == (
            row.hema_flip_count_16 + row.kalman_flip_count_16
        )


def test_reconstruction_fails_closed_on_hema_trace_disagreement(bundle) -> None:
    target = next(trace for trace in bundle.replay.traces if trace.strategy_bar and trace.strategy_bar.hema_direction is Direction.LONG)
    altered_bar = replace(target.strategy_bar, hema_direction=Direction.SHORT, hema_flip=Direction.SHORT)
    altered_trace = replace(target, strategy_bar=altered_bar)
    traces = tuple(altered_trace if trace is target else trace for trace in bundle.replay.traces)
    with pytest.raises(ValueError, match="reconstructed 15m HEMA direction/flip disagrees"):
        build_setup_regime_feature_rows(replace(bundle, replay=replace(bundle.replay, traces=traces)))


@pytest.mark.parametrize(
    ("altered", "binding"),
    (
        (lambda bundle: replace(bundle, strategy_config=StrategyConfig(atr_multiplier=1.5)), "strategy_fingerprint"),
        (lambda bundle: replace(bundle, replay_config=ReplayConfig(ltf_hema_fast_length=21)), "replay_fingerprint"),
    ),
)
def test_noncanonical_live_configuration_is_rejected(bundle, altered, binding) -> None:
    with pytest.raises(ValueError, match=rf"binding mismatch: {binding}"):
        build_setup_regime_feature_rows(altered(bundle))
    with pytest.raises(ValueError, match=rf"binding mismatch: {binding}"):
        feature_definition_artifact(altered(bundle))


def test_noncanonical_source_binding_is_rejected(bundle) -> None:
    altered = replace(bundle, source_counts=(("15m", 1), ("4h", 1)))
    with pytest.raises(ValueError, match="source count binding mismatch"):
        build_setup_regime_feature_rows(altered)
    with pytest.raises(ValueError, match="source count binding mismatch"):
        feature_definition_artifact(altered)


@pytest.mark.parametrize("attribute", ("kalman_direction", "kalman_transition", "atr"))
def test_reconstruction_fails_closed_on_kalman_trace_disagreement(bundle, attribute) -> None:
    target = next(trace for trace in bundle.replay.traces if trace.strategy_bar and trace.strategy_bar.kalman_transition is None)
    if attribute == "kalman_direction":
        value = Direction.SHORT if target.strategy_bar.kalman_direction is Direction.LONG else Direction.LONG
        altered_bar = replace(target.strategy_bar, kalman_direction=value)
    elif attribute == "kalman_transition":
        value = Direction.SHORT if target.strategy_bar.kalman_direction is Direction.SHORT else Direction.LONG
        altered_bar = replace(target.strategy_bar, kalman_direction=value, kalman_transition=value)
    else:
        altered_bar = replace(target.strategy_bar, atr=(target.strategy_bar.atr or 1.0) + 1.0)
    altered_trace = replace(target, strategy_bar=altered_bar)
    traces = tuple(altered_trace if trace is target else trace for trace in bundle.replay.traces)
    with pytest.raises(ValueError, match="reconstructed 15m Kalman direction/transition/ATR disagrees"):
        build_setup_regime_feature_rows(replace(bundle, replay=replace(bundle.replay, traces=traces)))


def test_reconstruction_fails_closed_on_indicator_event_trace_disagreement(bundle) -> None:
    target = next(trace for trace in bundle.replay.traces if trace.strategy_bar and trace.strategy_bar.hema_flip is not None)
    events = tuple(event for event in target.events if event.type is not EventType.HEMA_FLIP_DETECTED)
    altered_trace = replace(target, events=events)
    traces = tuple(altered_trace if trace is target else trace for trace in bundle.replay.traces)
    with pytest.raises(ValueError, match="HEMA StrategyBar/ReplayTrace event mismatch"):
        build_setup_regime_feature_rows(replace(bundle, replay=replace(bundle.replay, traces=traces)))


@pytest.mark.parametrize(
    ("field", "value"),
    (
        ("reason", ReasonCode.ENTRY_ACCEPTED),
        ("price", 1.0),
        ("metadata", (("forged", "value"),)),
    ),
)
def test_reconstruction_fails_closed_on_indicator_event_invariant_mutation(bundle, field, value) -> None:
    target = next(trace for trace in bundle.replay.traces if trace.strategy_bar and trace.strategy_bar.kalman_transition is not None)
    event_index = next(index for index, event in enumerate(target.events) if event.type is EventType.KALMAN_TRANSITION_DETECTED)
    events = list(target.events)
    events[event_index] = replace(events[event_index], **{field: value})
    altered_trace = replace(target, events=tuple(events))
    traces = tuple(altered_trace if trace is target else trace for trace in bundle.replay.traces)
    with pytest.raises(ValueError, match="Kalman StrategyBar/ReplayTrace event mismatch"):
        build_setup_regime_feature_rows(replace(bundle, replay=replace(bundle.replay, traces=traces)))


def test_reconstruction_fails_closed_on_forged_15m_htf_bias(bundle) -> None:
    target = next(
        trace for trace in bundle.replay.traces
        if trace.strategy_bar is not None and trace.strategy_bar.htf_bias is not None
    )
    forged = Direction.SHORT if target.strategy_bar.htf_bias is Direction.LONG else Direction.LONG
    altered_trace = replace(
        target,
        strategy_bar=replace(target.strategy_bar, htf_bias=forged),
        htf_bias_after_update=forged,
    )
    traces = tuple(altered_trace if trace is target else trace for trace in bundle.replay.traces)
    with pytest.raises(ValueError, match="reconstructed legal 4H bias disagrees"):
        build_setup_regime_feature_rows(replace(bundle, replay=replace(bundle.replay, traces=traces)))


def test_definition_artifact_is_deterministic_nan_safe_and_path_independent(bundle, tmp_path) -> None:
    artifact = feature_definition_artifact(bundle)
    first = feature_definition_json(artifact)
    assert first == feature_definition_json(artifact)
    assert first.endswith(b"\n") and b"NaN" not in first
    decoded = json.loads(first)
    assert decoded["phase7_1_base_sha"] == PHASE7_1_BASE_SHA
    assert decoded["anchor"] == SETUP_ORIGIN_ANCHOR
    assert "no TradeRow or outcome field" in decoded["source_scope"]
    assert decoded["manifest_id"] == bundle.dataset.manifest_id
    assert decoded["dataset_fingerprint"]
    assert decoded["source_counts"] == [["15m", 10452], ["4h", 8480]]
    assert len(decoded["source_artifacts"]) == 2
    assert validate_regime_feature_artifact(artifact) is artifact
    with pytest.raises(ValueError, match="definition/version/anchor"):
        validate_regime_feature_artifact(replace(artifact, feature_version="stale"))
    left = tmp_path / "left" / "definitions.json"
    right = tmp_path / "right" / "definitions.json"
    write_feature_definition_artifact(artifact, left)
    write_feature_definition_artifact(bundle, right)
    assert left.read_bytes() == right.read_bytes() == first
    with pytest.raises(FileExistsError, match="refusing to overwrite"):
        write_feature_definition_artifact(artifact, left)
    write_feature_definition_artifact(artifact, left, overwrite=True)
