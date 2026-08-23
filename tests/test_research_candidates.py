"""Fingerprint-bound candidate selection and held-out stage gates."""
from __future__ import annotations

from dataclasses import replace
from pathlib import Path

import pytest

from quasartrend.research.candidates import (
    CANDIDATE_ID,
    artifact_json,
    final_oos_report,
    register_development_candidate,
    register_finalist,
    reject_after_validation,
    validation_report,
)
from quasartrend.research.experiments import development_report
from quasartrend.research.pipeline import build_canonical_bundle


@pytest.fixture(scope="module")
def staged():
    bundle = build_canonical_bundle(
        golden_15m=Path("tests/golden/tradingview_15m.csv"),
        golden_4h=Path("tests/golden/tradingview_4h.csv"),
    )
    development = development_report(bundle)
    registration = register_development_candidate(bundle, development)
    return bundle, development, registration


def test_registration_is_exact_and_bound_to_development(staged) -> None:
    _, development, registration = staged
    assert registration.candidate_id == CANDIDATE_ID
    assert (registration.feature, registration.operator, registration.threshold) == (
        "adr_extension", "<", .50
    )
    assert registration.development_candidate.sample_count == 64
    assert registration.development_candidate.total_r == pytest.approx(17.079523850647604)
    assert registration.development_report_fingerprint
    assert registration.development_baseline == development.baseline_metrics


def test_registration_rejects_forged_dataset_binding(staged) -> None:
    bundle, development, _ = staged
    forged = replace(development, dataset_fingerprint="0" * 64)
    with pytest.raises(ValueError, match="exact canonical development"):
        register_development_candidate(bundle, forged)


def test_validation_is_the_only_first_held_out_stage(staged) -> None:
    bundle, _, registration = staged
    result = validation_report(bundle, registration)
    assert result.role == "validation"
    assert (result.start_date, result.end_date) == ("2026-07-10", "2026-07-28")
    assert result.candidate_registration_fingerprint
    assert result.baseline_closed_trades == 25
    assert result.candidate_closed_trades == 20
    assert result.baseline.expectancy_r == pytest.approx(.06126292344611567)
    assert result.candidate.expectancy_r == pytest.approx(-.10682514174726113)
    assert result.baseline.total_r == pytest.approx(1.5315730861528918)
    assert result.candidate.total_r == pytest.approx(-2.1365028349452224)
    assert not result.economically_improved


def test_final_oos_is_locked_without_improving_validation(staged) -> None:
    bundle, _, registration = staged
    validation = validation_report(bundle, registration)
    with pytest.raises(ValueError, match="did not satisfy"):
        register_finalist(bundle, registration, validation)
    # There is no FinalistRegistration instance that can be passed to the
    # final-OOS entry point when validation fails.
    with pytest.raises(TypeError, match="FinalistRegistration"):
        final_oos_report(bundle, object())
    decision = reject_after_validation(bundle, registration, validation)
    assert decision.finalists_registered == 0
    assert not decision.final_oos_unlocked
    assert not decision.final_oos_candidate_outcomes_evaluated
    assert not decision.production_strategy_change_recommended
    assert "validation expectancy degraded" in decision.reasons


def test_forged_validation_boolean_cannot_unlock_final_oos(staged) -> None:
    bundle, _, registration = staged
    canonical = validation_report(bundle, registration)
    forged = replace(canonical, economically_improved=True)
    with pytest.raises(ValueError, match="exact canonical validation"):
        register_finalist(bundle, registration, forged)
    with pytest.raises(ValueError, match="exact canonical validation"):
        reject_after_validation(bundle, registration, forged)


def test_relabelled_non_development_report_cannot_register(staged) -> None:
    bundle, development, _ = staged
    forged = replace(development, start_date="2026-07-10", end_date="2026-07-28")
    with pytest.raises(ValueError, match="exact canonical development"):
        register_development_candidate(bundle, forged)


def test_local_candidate_artifacts_are_fresh_when_present(staged) -> None:
    bundle, _, registration = staged
    validation = validation_report(bundle, registration)
    decision = reject_after_validation(bundle, registration, validation)
    expected = {
        Path("exports/phase7/phase7_candidate_registration.json"): artifact_json(registration),
        Path("exports/phase7/phase7_validation.json"): artifact_json(validation),
        Path("exports/phase7/phase7_candidate_decision.json"): artifact_json(decision),
    }
    for path, content in expected.items():
        if path.exists():
            assert path.read_bytes() == content
