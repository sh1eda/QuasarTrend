from __future__ import annotations

from dataclasses import asdict
from pathlib import Path

import pytest

from quasartrend.research.xau_v2_initialization import (
    CANONICAL_ORIGIN_MAIN_SHA, DEVELOPMENT_END_EXCLUSIVE_MS, FOLDS,
    HISTORICAL_ARTIFACT_PATH, RESERVED_HOLDOUT_END_EXCLUSIVE_MS,
    RESERVED_HOLDOUT_START_MS, V2ResearchConfig, assert_economic_range_allowed,
    build_baseline_walk_forward, enumerate_candidates, protocol_inventory,
    _write_exact_protocol, regenerate_v2_artifacts, validate_fold_population, verify_historical_artifact,
    verify_v1_identity,
)
from quasartrend.strategy import StrategyConfig


def test_v1_is_unchanged_and_v2_is_separate() -> None:
    assert StrategyConfig() == StrategyConfig()
    assert verify_v1_identity(Path("."))["origin_main"] == CANONICAL_ORIGIN_MAIN_SHA
    config = V2ResearchConfig()
    assert config.fingerprint() == V2ResearchConfig().fingerprint()
    assert enumerate_candidates(config) == enumerate_candidates(config)
    assert [candidate.value for candidate in enumerate_candidates(config) if candidate.family == "directional_asymmetry"] == ["both", "long_only", "short_only"]
    assert [candidate.value for candidate in enumerate_candidates(config) if candidate.family == "adr_max_current_daily_range_over_prior_14d_adr"] == [None, 0.75, 1.0, 1.25]
    with pytest.raises(ValueError, match="frozen predeclared"):
        protocol_inventory(V2ResearchConfig(directional_candidates=("long_only",)))


def test_holdout_economics_fails_closed() -> None:
    with pytest.raises(ValueError, match="reserved holdout"):
        assert_economic_range_allowed(RESERVED_HOLDOUT_START_MS, RESERVED_HOLDOUT_END_EXCLUSIVE_MS)
    with pytest.raises(ValueError, match="development end"):
        assert_economic_range_allowed(DEVELOPMENT_END_EXCLUSIVE_MS - 1, DEVELOPMENT_END_EXCLUSIVE_MS + 1)


def test_folds_and_baseline_are_frozen_from_the_identified_ledger() -> None:
    artifact = verify_historical_artifact(Path(HISTORICAL_ARTIFACT_PATH))
    counts = validate_fold_population(artifact["closed_trade_ledger"])
    assert [counts[f"F{index}_validation_originated"] for index in range(1, 6)] == [143, 139, 147, 146, 93]
    assert [counts[f"F{index}_validation_scored"] for index in range(1, 6)] == [143, 138, 146, 146, 93]
    assert all(fold.train_end_exclusive == fold.validation_start for fold in FOLDS)
    baseline = build_baseline_walk_forward(Path(HISTORICAL_ARTIFACT_PATH))
    assert baseline["aggregate"]["total_r"] == artifact["aggregate"]["total_r"]
    assert baseline["combined_oos"]["closed"] == 666
    assert sum(len(fold["cross_boundary_censored"]) for fold in baseline["folds"]) == 2
    assert baseline["folds"][0]["out_of_sample"]["eligible_setups"] is None
    assert "median" in baseline["aggregate"]["duration_ms"]


def test_protocol_and_regeneration_are_deterministic_and_safe(tmp_path: Path) -> None:
    first = protocol_inventory()
    assert first == protocol_inventory()
    paths = regenerate_v2_artifacts(Path("."))
    before = paths["protocol"].read_bytes()
    assert regenerate_v2_artifacts(Path("."))["protocol"].read_bytes() == before
    assert first["candidate_policy"]["next_experiment"] == "directional_asymmetry"
    assert first["next_experiment_specification"]["minimum_training_closed_trades"] == 50
    assert first["next_experiment_specification"]["candidates_in_fixed_tie_order"] == ["both", "long_only", "short_only"]
    assert first["development_data"]["closed_trades"] == 820
    assert any(candidate["value"] == ("asia", 0, 8) for candidate in first["candidate_inventory"])
    guarded = tmp_path / "protocol.json"
    _write_exact_protocol(guarded, {"one": 1})
    with pytest.raises(ValueError, match="refusing"):
        _write_exact_protocol(guarded, {"one": 2})
