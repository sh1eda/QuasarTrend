from __future__ import annotations

import json
from pathlib import Path

import pytest

from quasartrend.research import xau_v2_family1 as family1
from quasartrend.research.xau_v2_initialization import FOLDS, RESERVED_HOLDOUT_END_EXCLUSIVE_MS, RESERVED_HOLDOUT_START_MS


def _setup(timestamp: int, direction: str) -> dict[str, object]:
    return {"timestamp": timestamp, "direction": direction}


def _trade(identifier: str, origin: int, exit_timestamp: int, r: float, direction: str) -> dict[str, object]:
    return {"trade_id": identifier, "setup_origin_timestamp": origin, "entry_timestamp": origin + 1, "exit_timestamp": exit_timestamp, "r": r, "direction": direction, "outcome": "closed"}


def _synthetic_staged_inputs() -> tuple[list[dict[str, object]], list[dict[str, object]]]:
    start = family1._millis(FOLDS[0].train_start) + 1
    setups, trades = [], []
    # Both directional policies qualify for the sample threshold.  Long is
    # repeatedly selected by its superior train-only outcomes.
    for index in range(50):
        timestamp = start + index
        setups.extend((_setup(timestamp, "long"), _setup(timestamp + 100, "short")))
        trades.extend((_trade(f"tl{index}", timestamp, timestamp + 2, 1.0, "long"), _trade(f"ts{index}", timestamp + 100, timestamp + 102, -1.0, "short")))
    for fold in FOLDS:
        timestamp = family1._millis(fold.validation_start) + 1
        setups.extend((_setup(timestamp, "long"), _setup(timestamp + 100, "short")))
        trades.extend((_trade(f"{fold.fold_id}l", timestamp, timestamp + 2, 1.0, "long"), _trade(f"{fold.fold_id}s", timestamp + 100, timestamp + 102, -1.0, "short")))
    return setups, trades


def _fake_implementation_identities(letter: str = "a") -> dict[str, str]:
    return {key: letter * 64 for key in family1.IMPLEMENTATION_HASH_KEYS}


def test_candidate_enumeration_and_training_rank_ties_and_minimum() -> None:
    enumeration = family1.candidate_enumeration()
    assert [row["value"] for row in enumeration] == list(family1.CANDIDATES)
    assert [row["candidate_id"] for row in enumeration] == [
        'directional_asymmetry:1:"both"',
        'directional_asymmetry:2:"long_only"',
        'directional_asymmetry:3:"short_only"',
    ]
    base = {"closed": 50, "expectancy_r": 0.2, "profit_factor": 1.1}
    assert family1.rank_training_candidates({candidate: dict(base) for candidate in family1.CANDIDATES}) == "both"
    reduced = {candidate: {**base, "closed": 49} for candidate in family1.CANDIDATES}
    with pytest.raises(ValueError, match="required 50"):
        family1.rank_training_candidates(reduced)
    ranked = {"both": {**base, "expectancy_r": .3}, "long_only": {**base, "expectancy_r": .3, "profit_factor": 1.2}, "short_only": {**base, "expectancy_r": .4}}
    assert family1.rank_training_candidates(ranked) == "short_only"


def test_interval_boundary_censoring_and_metrics() -> None:
    setups = [_setup(10, "long"), _setup(20, "short"), _setup(30, "long")]
    trades = [_trade("a", 10, 12, 2.0, "long"), _trade("b", 20, 25, -1.0, "short"), _trade("c", 30, 40, 3.0, "long")]
    metrics = family1.metrics_for_interval(setups, trades, start_ms=10, end_exclusive_ms=40, candidate="both", fold_id="T")
    assert metrics["eligible_setups"] == 3 and metrics["closed"] == 2 and metrics["originated_closed_trades"] == 3
    assert metrics["total_r"] == 1.0 and metrics["profit_factor"] == 2.0 and metrics["maximum_drawdown_r"] == 1.0
    assert metrics["cross_boundary_censored"] == [{"trade_id": "c", "direction": "long", "setup_origin_timestamp": 30, "exit_timestamp": 40}]
    assert family1.metrics_for_interval(setups, trades, start_ms=10, end_exclusive_ms=40, candidate="long_only", fold_id="T")["r_per_setup"] == 1.0
    with pytest.raises(ValueError, match="reserved holdout"):
        family1.metrics_for_interval([], [], start_ms=RESERVED_HOLDOUT_START_MS, end_exclusive_ms=RESERVED_HOLDOUT_END_EXCLUSIVE_MS, candidate="both", fold_id="H")
    with pytest.raises(ValueError, match="reserved holdout"):
        family1.training_metrics([], [], train_start_ms=RESERVED_HOLDOUT_START_MS, validation_start_ms=RESERVED_HOLDOUT_END_EXCLUSIVE_MS, candidate="both", fold_id="H")


def test_staging_writes_and_verifies_lock_before_validation(tmp_path: Path) -> None:
    setups, trades = _synthetic_staged_inputs()
    events: list[str] = []
    def writer(path: Path, lock: dict[str, object]) -> str:
        events.append(f"lock:{lock['fold']['fold_id']}")
        return family1.write_fold_lock(path, lock)
    def evaluator(*args: object, **kwargs: object) -> dict[str, object]:
        fold_id = str(kwargs["fold_id"])
        assert f"lock:{fold_id}" in events
        events.append(f"validation:{fold_id}")
        return family1.metrics_for_interval(*args, **kwargs)  # type: ignore[arg-type]
    staged = family1._run_staged_family1(setups=setups, trades=trades, lock_directory=tmp_path, lock_writer=writer, validation_evaluator=evaluator)
    assert [fold["selected_candidate"] for fold in staged["folds"]] == ["long_only"] * 5
    assert events == [item for fold in FOLDS for item in (f"lock:{fold.fold_id}", f"validation:{fold.fold_id}", f"validation:{fold.fold_id}", f"validation:{fold.fold_id}")]
    assert json.loads((tmp_path / "F1.json").read_bytes())["validation_economics_included"] is False
    assert staged["folds"][0]["validation_candidates"]["both"]["oos_direction_diagnostics"]["long"]["oos_direction_sample_warning"] == family1.SMALL_SAMPLE_WARNING


def test_frozen_population_enforcement_is_opt_in_for_synthetic_and_fail_closed(tmp_path: Path) -> None:
    setups, trades = _synthetic_staged_inputs()
    # Synthetic rows intentionally do not reproduce the real F1 152/152
    # population, so enforced staging cannot silently accept them.
    with pytest.raises(ValueError, match="F1 frozen training"):
        family1._run_staged_family1(setups=setups, trades=trades, lock_directory=tmp_path, enforce_frozen_population_counts=True, implementation_identities=_fake_implementation_identities())


def test_missing_or_tampered_lock_stops_before_validation(tmp_path: Path) -> None:
    setups, trades = _synthetic_staged_inputs()
    called = False
    def no_write(_path: Path, _lock: dict[str, object]) -> str:
        return "not-a-real-digest"
    def evaluator(*_args: object, **_kwargs: object) -> dict[str, object]:
        nonlocal called
        called = True
        return {}
    with pytest.raises(FileNotFoundError, match="missing required"):
        family1._run_staged_family1(setups=setups, trades=trades, lock_directory=tmp_path, lock_writer=no_write, validation_evaluator=evaluator)
    assert called is False
    # The public verifier rejects byte-level tampering.
    train = {candidate: {"closed": 50, "expectancy_r": 1.0, "profit_factor": 1.0} for candidate in family1.CANDIDATES}
    lock = family1.build_fold_lock(fold=FOLDS[0], training=train, selected_candidate="both")
    path = tmp_path / "lock.json"; family1.write_fold_lock(path, lock)
    changed = json.loads(path.read_bytes()); changed["selected_candidate"] = "short_only"; path.write_bytes(family1.fold_lock_bytes(changed))
    with pytest.raises(ValueError, match="tampered"):
        family1.verify_fold_lock(path, lock)


def test_implementation_hash_drift_changes_lock_and_stops_before_validation(tmp_path: Path) -> None:
    setups, trades = _synthetic_staged_inputs()
    first_identity = _fake_implementation_identities("a")
    second_identity = _fake_implementation_identities("a"); second_identity["family1_module_sha256"] = "c" * 64
    family1._run_staged_family1(setups=setups, trades=trades, lock_directory=tmp_path, implementation_identities=first_identity)
    called = False
    def evaluator(*_args: object, **_kwargs: object) -> dict[str, object]:
        nonlocal called
        called = True
        return {}
    with pytest.raises(ValueError, match="immutable artifact"):
        family1._run_staged_family1(setups=setups, trades=trades, lock_directory=tmp_path, implementation_identities=second_identity, validation_evaluator=evaluator)
    assert called is False
    with pytest.raises(ValueError, match="implementation-hash"):
        family1._run_staged_family1(setups=setups, trades=trades, lock_directory=tmp_path / "required", enforce_frozen_population_counts=True)


def test_public_production_path_rejects_unadmitted_or_reserved_trade_before_staging(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    with pytest.raises(TypeError, match="loader-admitted"):
        family1.execute_real_family1({}, lock_directory=tmp_path)  # type: ignore[arg-type]
    identities = _fake_implementation_identities()
    reserved = _trade("reserved", RESERVED_HOLDOUT_START_MS, RESERVED_HOLDOUT_START_MS + 1, 1.0, "long")
    setups = (family1._freeze_rows(_setup(RESERVED_HOLDOUT_START_MS, "long")),)
    trades = (family1._freeze_rows(reserved),)
    inputs = family1._AdmittedFamily1Inputs(repo_root=Path("."), setups=setups, trades=trades, identities=identities, setup_rows_sha256=family1._rows_sha256(setups), trade_rows_sha256=family1._rows_sha256(trades), _admission_token=family1._ADMISSION_TOKEN)
    monkeypatch.setattr(family1, "verify_real_input_identities", lambda _root: identities)
    staged = False
    def forbidden_stage(**_kwargs: object) -> dict[str, object]:
        nonlocal staged
        staged = True
        return {}
    monkeypatch.setattr(family1, "_run_staged_family1", forbidden_stage)
    with pytest.raises(ValueError, match="reserved holdout"):
        family1.execute_real_family1(inputs, lock_directory=tmp_path)
    assert staged is False


def test_admitted_rows_are_deep_frozen_and_digest_tampering_stops_before_staging(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    setup_source = _setup(10, "long")
    trade_source = _trade("one", 10, 12, 1.0, "long"); trade_source["exit_reasons"] = ["exit_stop"]
    setup = family1._freeze_rows(setup_source)
    trade = family1._freeze_rows(trade_source)
    setup_source["timestamp"] = 11; trade_source["exit_reasons"].append("changed")
    assert setup["timestamp"] == 10 and trade["exit_reasons"] == ("exit_stop",)
    with pytest.raises(TypeError):
        setup["timestamp"] = 12
    with pytest.raises(TypeError):
        trade["exit_reasons"][0] = "changed"
    identities = _fake_implementation_identities()
    inputs = family1._AdmittedFamily1Inputs(repo_root=Path("."), setups=(setup,), trades=(trade,), identities=identities, setup_rows_sha256=family1._rows_sha256((setup,)), trade_rows_sha256=family1._rows_sha256((trade,)), _admission_token=family1._ADMISSION_TOKEN)
    tampered_trade = family1._freeze_rows({**family1._plain_rows(trade), "r": 2.0})
    tampered = family1._AdmittedFamily1Inputs(repo_root=Path("."), setups=(setup,), trades=(tampered_trade,), identities=identities, setup_rows_sha256=inputs.setup_rows_sha256, trade_rows_sha256=inputs.trade_rows_sha256, _admission_token=family1._ADMISSION_TOKEN)
    monkeypatch.setattr(family1, "verify_real_input_identities", lambda _root: identities)
    staged = False
    def forbidden_stage(**_kwargs: object) -> dict[str, object]:
        nonlocal staged
        staged = True
        return {}
    monkeypatch.setattr(family1, "_run_staged_family1", forbidden_stage)
    with pytest.raises(ValueError, match="admission digests"):
        family1.execute_real_family1(tampered, lock_directory=tmp_path)
    assert staged is False


def test_revalidated_context_accepts_loader_frozen_tuple_shape(monkeypatch: pytest.MonkeyPatch) -> None:
    setups, trades = _synthetic_staged_inputs()
    frozen_setups = tuple(family1._freeze_rows(row) for row in setups)
    frozen_trades = tuple(family1._freeze_rows(row) for row in trades)
    monkeypatch.setattr(family1, "validate_fold_population", lambda _rows: {})
    monkeypatch.setattr(family1, "_context_rows", lambda artifact: tuple(artifact["eligible_setup_context_ledger"]))
    inputs = family1._AdmittedFamily1Inputs(
        repo_root=Path("."),
        setups=frozen_setups,
        trades=frozen_trades,
        identities=_fake_implementation_identities(),
        setup_rows_sha256=family1._rows_sha256(frozen_setups),
        trade_rows_sha256=family1._rows_sha256(frozen_trades),
        _admission_token=family1._ADMISSION_TOKEN,
    )
    family1._revalidate_admitted_population(inputs)


def test_deterministic_json_csv_and_immutable_artifact_writes(tmp_path: Path) -> None:
    payload = {"b": [2], "a": 1}
    rows = [{"series": "selected_oos", "fold_id": "F1", "selected_candidate": "long_only", "candidate_id": "long_only", "ordinal": 1, "trade_id": "x", "exit_timestamp": 2, "r": 1.0, "cumulative_r": 1.0}]
    assert family1.family1_json(payload) == b'{"a":1,"b":[2]}\n'
    assert family1.family1_csv(rows) == family1.family1_csv(list(reversed(rows)))
    first = family1.write_family1_artifacts(result_path=tmp_path / "result.json", csv_path=tmp_path / "trades.csv", result=payload, csv_rows=rows)
    assert first == family1.write_family1_artifacts(result_path=tmp_path / "result.json", csv_path=tmp_path / "trades.csv", result=payload, csv_rows=rows)
    with pytest.raises(ValueError, match="immutable"):
        family1.write_family1_artifacts(result_path=tmp_path / "result.json", csv_path=tmp_path / "trades.csv", result={"a": 2}, csv_rows=rows)


def test_result_builder_contains_required_matched_and_secondary_context(tmp_path: Path) -> None:
    setups, trades = _synthetic_staged_inputs()
    staged = family1._run_staged_family1(setups=setups, trades=trades, lock_directory=tmp_path)
    result, csv_rows = family1._build_family1_result(staged=staged, setups=setups, trades=trades, identities={"eligible_setups": 1072})
    assert result["comparison"]["same_fold_populations"] is True
    assert result["selected_oos"]["closed"] == 5
    assert result["matched_v1_both_reference"]["closed"] == 10
    assert result["frozen_820_trade_baseline_secondary_context"]["metrics"]["closed"] == len(trades)
    assert result["folds"][0]["selection_lock"]["path"].endswith("F1.json")
    assert {row["series"] for row in csv_rows} == {"selected_oos", "matched_v1_both"}
    assert result["descriptive_candidate_aggregate_on_matched_oos_populations"]["both"]["oos_direction_diagnostics"]["long"]["oos_direction_sample_warning"] == family1.SMALL_SAMPLE_WARNING
    assert result["classification_rule"]["promising"]["same_non_both_policy_selected_at_least_folds"] == 4
    assert result["classification_rule"]["promising"]["selected_maximum_drawdown_r"] == "<= 1.25x matched_both"
    assert result["economics_basis"]["r"] == "gross R from the frozen historical closed-trade ledger"
    assert "no net-cost conclusion" in result["economics_basis"]["net_cost_conclusion"]
    stability = result["comparison"]["stability"]["per_fold_selected_and_matched_direction_r"]["F1"]
    assert stability["selected"]["long"] == {"included_by_policy": True, "total_r": 1.0}
    assert stability["selected"]["short"] == {"included_by_policy": False, "total_r": None}
    assert stability["matched_both"]["short"] == {"included_by_policy": True, "total_r": -1.0}


def test_classification_predeclared_branches() -> None:
    def fold(candidate: str, selected_r: float = 1.0, both_r: float = 0.0, expectancy: float = .1) -> dict[str, object]:
        candidates = {name: {"total_r": both_r, "expectancy_r": expectancy} for name in family1.CANDIDATES}
        candidates[candidate] = {"total_r": selected_r, "expectancy_r": expectancy}
        return {"selected_candidate": candidate, "validation_candidates": candidates}
    selected = {"expectancy_r": .2, "profit_factor": 1.5, "total_r": 10., "closed": 60, "maximum_drawdown_r": 5.}
    reference = {"expectancy_r": .1, "profit_factor": 1.1, "total_r": 5., "closed": 100, "maximum_drawdown_r": 5.}
    promising_folds = [fold("long_only") for _ in range(5)]
    assert family1.classify_family1(folds=promising_folds, selected_metrics=selected, matched_both_metrics=reference).endswith("PROMISING")
    assert family1.classify_family1(folds=[fold("both") for _ in range(5)], selected_metrics=selected, matched_both_metrics=reference).endswith("NOT JUSTIFIED")
    assert family1.classify_family1(folds=promising_folds, selected_metrics={**selected, "maximum_drawdown_r": 10.}, matched_both_metrics=reference).endswith("INCONCLUSIVE")


def test_real_artifact_identity_and_context_denominators_only() -> None:
    identities = family1.verify_real_input_identities(Path("."))
    assert identities["preoptimization_commit_sha"] == family1.PREOPTIMIZATION_COMMIT_SHA
    assert identities["v1_identity_verified"] is True
    assert not any("head" in key.lower() for key in identities)
    assert all(len(identities[key]) == 64 for key in family1.IMPLEMENTATION_HASH_KEYS)
    assert identities["eligible_setups"] == 1072
    assert (identities["long_setups"], identities["short_setups"]) == (587, 485)
