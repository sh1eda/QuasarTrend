from __future__ import annotations

import json
from pathlib import Path

import pytest

import quasartrend.research.xau_directional_edge_attribution as stage_a


CONTEXT = {
    "population": dict(stage_a.EXPECTED_POPULATION),
    "first_eligible_timestamp_ms": stage_a.FIRST_ELIGIBLE_TIMESTAMP_MS,
    "bias_persistence_hours": {"q1": 1.0, "q2": 2.0, "nonmissing": 4, "counts": {"low": 2, "medium": 1, "high": 1, "missing": 0}},
    "atr_at_setup": {"q1": 3.0, "q2": 4.0, "nonmissing": 4, "counts": {"low": 2, "medium": 1, "high": 1, "missing": 0}},
    "broad_market_direction_32": {"counts": {"down": 1, "flat": 1, "up": 2, "missing": 0}},
}


def test_protocol_bytes_and_required_stage_a_fields_are_deterministic() -> None:
    first = stage_a.build_xau_directional_edge_attribution_protocol(CONTEXT)
    second = stage_a.build_xau_directional_edge_attribution_protocol(CONTEXT)
    assert stage_a.xau_directional_edge_attribution_protocol_json(first) == stage_a.xau_directional_edge_attribution_protocol_json(second)
    assert stage_a.xau_directional_edge_attribution_protocol_json(first).endswith(b"\n")
    assert first["canonical_starting_state"]["sha"] == stage_a.CANONICAL_STARTING_SHA
    assert first["canonical_starting_state"]["annotated_tag_object"] == stage_a.CANONICAL_TAG_OBJECT
    assert first["frozen_inputs"]["source_sha256"] == stage_a.EXPECTED_SOURCE_SHA256
    assert {key: first["population"][key] for key in stage_a.EXPECTED_POPULATION} == stage_a.EXPECTED_POPULATION
    assert first["frozen_inputs"]["historical_cutoff"]["strictly_before_utc"] == stage_a.HISTORICAL_CUTOFF_UTC
    assert first["frozen_inputs"]["first_eligible_timestamp"] == {"epoch_ms": 1_710_972_900_000, "utc": "2024-03-20T22:15:00Z"}
    assert first["calendar_and_ordering"]["first_half"] == "first ceil(n/2) ordered closed trades; second floor(n/2) ordered closed trades"
    assert "NON-additive" in first["required_output"]["exit_loss"]
    assert "mean losing R" in first["required_output"]["exit_loss"]
    assert "winners, and losers" in first["required_output"]["holding_duration"]
    assert "top 1/3/5/10" in first["required_output"]["winner_tail"]
    assert "frequency=(pL-pS)*(mL+mS)/2" in first["gap_decompositions"]["exact_symmetric_product_contributions"]
    assert "composition=sum_k" in first["gap_decompositions"]["setup_path"]
    assert "frequency=(p_Ls-p_Ss)" in first["gap_decompositions"]["stop_nonstop"]
    assert "including missing" in first["gap_decompositions"]["regime"]
    assert first["metric_definitions"]["profit_factor"].endswith("strictly negative trades")
    assert "LONG max(0,entry-min(low))" in first["metric_definitions"]["full_exit_bar_mae_r"]
    assert "non-stop total R" in first["required_output"]["stop_nonstop"]
    assert first["distribution_and_null_policy"]["regime_missing"].startswith("retain and report")
    assert set(first["regime_families"]) == {"bias_persistence_hours", "atr_at_setup", "broad_market_direction_32", "quantile_method"}
    assert first["stage_a_economics"].endswith("NO")


def test_type_7_quantiles_tertile_ties_and_missing() -> None:
    assert stage_a.type_7_quantile([0.0, 10.0, 20.0, 30.0], 1 / 3) == 10.0
    assert stage_a.type_7_quantile([0.0, 10.0, 20.0, 30.0], 2 / 3) == 20.0
    assert [stage_a.tertile_bin(value, 10.0, 20.0) for value in (None, 10.0, 10.1, 20.0, 20.1)] == ["missing", "low", "medium", "medium", "high"]
    with pytest.raises(ValueError):
        stage_a.type_7_quantile([], 0.5)


def test_direction_32_requires_exact_contiguous_33_bar_window() -> None:
    contiguous = [(index * stage_a.M15_DURATION_MS, float(index)) for index in range(33)]
    assert stage_a._direction_32(contiguous) == "up"
    assert stage_a._direction_32(contiguous[:-1]) == "missing"
    broken = contiguous.copy(); broken[10] = (broken[10][0] + 1, broken[10][1])
    assert stage_a._direction_32(broken) == "missing"


def test_writer_is_fail_closed_and_protocol_verifier_rejects_tampering(tmp_path: Path) -> None:
    protocol = json.loads(Path("exports/xm/phase_xau_directional_edge_attribution_protocol.json").read_bytes())
    path = tmp_path / "lock.json"
    digest = stage_a.write_xau_directional_edge_attribution_protocol(protocol, path)
    assert digest == stage_a.protocol_sha256(protocol)
    with pytest.raises(FileExistsError):
        stage_a.write_xau_directional_edge_attribution_protocol(protocol, path)
    changed = json.loads(path.read_bytes()); changed["stage_a_economics"] = "YES"
    with pytest.raises(ValueError):
        stage_a.verify_xau_directional_edge_attribution_protocol(changed)


def test_tracked_git_clean_rejects_staged_dirty_state(monkeypatch: pytest.MonkeyPatch) -> None:
    class Result:
        def __init__(self, returncode: int) -> None:
            self.returncode = returncode
    def fake_run(command: tuple[str, ...], **_kwargs: object) -> Result:
        return Result(1 if command == ("git", "diff", "--cached", "--quiet") else 0)
    monkeypatch.setattr(stage_a.subprocess, "run", fake_run)
    assert not stage_a._tracked_git_clean(Path("."))


def test_context_lock_rejects_alternate_raw_path(tmp_path: Path) -> None:
    alternate = tmp_path / "XM_GOLD_M1_raw.csv"
    alternate.write_text("not used", encoding="utf-8")
    with pytest.raises(ValueError, match="canonical raw XM source path"):
        stage_a.build_context_lock(repo_root=Path("."), xm_m1_source=alternate)


def test_actual_artifact_is_pinned_and_exact_semantic_lock() -> None:
    path = Path("exports/xm/phase_xau_directional_edge_attribution_protocol.json")
    payload = path.read_bytes()
    protocol = json.loads(payload)
    assert __import__("hashlib").sha256(payload).hexdigest() == stage_a.EXPECTED_PROTOCOL_SHA256
    stage_a.verify_xau_directional_edge_attribution_protocol(protocol)
    assert protocol == stage_a.build_xau_directional_edge_attribution_protocol(protocol["frozen_inputs"]["context_lock"])


def test_committed_stage_b_result_hash_and_historical_execution_verify() -> None:
    path = Path(stage_a.RESULT_PATH)
    payload = path.read_bytes()
    assert __import__("hashlib").sha256(payload).hexdigest() == "747402a144eaab959b7dc2d6432c0c894f5bc2104833bcd98851cc1188030e3d"
    result = json.loads(payload)
    assert result["metadata"]["execution_head_sha"] == "e74cd3843c047b92ca76e13e05c57f72c5f96209"
    # Verification intentionally permits the execution snapshot to be an
    # ancestor of a later verifier HEAD (the current commit is test-only).
    stage_a.verify_xau_directional_edge_attribution_result(result)


@pytest.mark.parametrize("field", ["canonical_starting_state", "classification", "forbidden_analyses"])
def test_pinned_verifier_rejects_canonical_semantic_tampering(field: str) -> None:
    protocol = json.loads(Path("exports/xm/phase_xau_directional_edge_attribution_protocol.json").read_bytes())
    if field == "canonical_starting_state":
        protocol[field]["sha"] = "0" * 40
    elif field == "classification":
        protocol[field]["labels"] = []
    else:
        protocol[field].append("tampered")
    with pytest.raises(ValueError):
        stage_a.verify_xau_directional_edge_attribution_protocol(protocol)


def test_pinned_verifier_rejects_context_tampering() -> None:
    protocol = json.loads(Path("exports/xm/phase_xau_directional_edge_attribution_protocol.json").read_bytes())
    protocol["frozen_inputs"]["context_lock"]["atr_at_setup"]["q1"] = 0.0
    with pytest.raises(ValueError, match="context-lock"):
        stage_a.verify_xau_directional_edge_attribution_protocol(protocol)


def _closed_row(identifier: str, r: float, *, direction: str = "long", exit_timestamp: int = 10, path: str = "immediate_open", stop: bool = False, reasons: list[str] | None = None) -> dict[str, object]:
    return {"trade_id": identifier, "outcome": "closed", "r": r, "direction": direction,
            "entry_timestamp": 1, "exit_timestamp": exit_timestamp, "path": path, "stop_hit": stop,
            "exit_reasons": reasons or (["exit_stop"] if stop else ["exit_hema_flip"]),
            "holding_duration_minutes": 1.0, "mfe_r": 1.0, "mae_r": .5,
            "bias_persistence_hours_bucket": "low", "atr_at_setup_bucket": "low", "broad_market_direction_32_bucket": "up"}


def test_stage_b_close_order_partitions_metrics_tail_and_exit_membership() -> None:
    rows = [_closed_row("b", 2.0, exit_timestamp=1), _closed_row("a", 2.0, exit_timestamp=1), _closed_row("c", -1.0, direction="short", exit_timestamp=2, stop=True, reasons=["exit_stop", "exit_hema_flip"]), _closed_row("d", 0.0, direction="short", exit_timestamp=3)]
    assert [row["trade_id"] for row in stage_a._ordered_closed(rows)] == ["a", "b", "c", "d"]
    assert [len(part) for part in stage_a._partitions(stage_a._ordered_closed(rows), 3)] == [2, 1, 1]
    metrics = stage_a._metrics(rows)
    assert metrics["profit_factor"] == 4.0 and metrics["win_rate"] == .5
    assert stage_a._metrics([_closed_row("zero", 0.0)])["profit_factor"] is None
    tail = stage_a._tail(rows)
    assert tail["top_1"]["trade_ids"] == ["a"] and tail["top_3"]["remaining_total_r"] == -1.0
    exit_table = stage_a._exit_and_holding(rows)
    assert exit_table["short"]["exit_reason_membership"]["exit_stop"]["count"] == 1
    assert exit_table["short"]["exit_reason_membership"]["exit_hema_flip"]["count"] == 2
    assert exit_table["short"]["exclusive_ordered_reason_combinations"]["exit_stop|exit_hema_flip"]["count"] == 1
    assert stage_a._small_cell(metrics) is True


@pytest.fixture(scope="module")
def stage_b_result() -> dict[str, object]:
    protocol = json.loads(Path("exports/xm/phase_xau_directional_edge_attribution_protocol.json").read_bytes())
    return stage_a._build_xau_directional_edge_attribution_result_unchecked(
        repo_root=Path("."), xm_m1_source=Path(stage_a.RAW_SOURCE_PATH), protocol=protocol,
        guard={"head": stage_a._git_output(Path("."), "rev-parse", "HEAD"), "source_sha256": stage_a.EXPECTED_SOURCE_SHA256},
    )


def test_stage_b_actual_internal_builder_reproduces_frozen_population_and_headlines(stage_b_result: dict[str, object]) -> None:
    result = stage_b_result
    protocol = json.loads(Path("exports/xm/phase_xau_directional_edge_attribution_protocol.json").read_bytes())
    assert result["population_reproduction"]["closed_trades"] == 820
    assert result["population_reproduction"]["headline_reproduction"]["status"] == "PASS"
    assert result["aggregate_baseline"]["closed_trades"] == 820
    assert result["directional_baseline"]["long"]["closed_trades"] == 468
    assert result["directional_baseline"]["short"]["closed_trades"] == 352
    assert result["directional_baseline"]["long"]["total_r"] == stage_a.HISTORICAL_HEADLINES["long"]["total_r"]
    assert result["directional_baseline"]["short"]["total_r"] == stage_a.HISTORICAL_HEADLINES["short"]["total_r"]
    assert result["restriction_state"]["directional_filter_tested"] == "NO"
    assert result["later_period_directional_contrast"]["separate_not_validation"] is True
    assert set(result["classification"]["labels"]) <= set(protocol["classification"]["labels"])


def test_stage_b_result_tables_warnings_and_reconciliations(stage_b_result: dict[str, object]) -> None:
    result = stage_b_result
    assert sum(cell[side]["closed_trades"] for cell in result["chronological"]["quartiles"].values() for side in ("long", "short")) == 820
    assert sum(result["setup_path"][path][side]["opened_trades"] for path in result["setup_path"] for side in ("long", "short")) == 820
    for family, buckets in result["regime"].items():
        assert sum(buckets[bucket][side]["eligible_setups"] for bucket in buckets for side in ("long", "short")) == 1072
    cells = [cell for table in result["calendar"].values() for period in table.values() for cell in period.values()]
    cells += [cell for period in result["chronological"]["quartiles"].values() for cell in period.values()]
    cells += [cell for paths in result["setup_path"].values() for cell in paths.values()]
    cells += [cell for buckets in result["regime"].values() for values in buckets.values() for cell in values.values()]
    assert all(cell["sample_warning"] == ("SMALL-SAMPLE / DESCRIPTIVE ONLY" if cell["closed_trades"] < 30 else None) for cell in cells)
    for direction in ("long", "short"):
        exit_table = result["exit_loss_and_holding"][direction]
        assert sum(value["count"] for value in exit_table["exclusive_ordered_reason_combinations"].values()) == result["directional_baseline"][direction]["closed_trades"]
        assert "p90" in exit_table["stopped_trade_diagnostics"]["mfe_r"]
        assert "maximum" in exit_table["holding_duration_minutes"]["overall"]
        assert "positive_r_share" in result["directional_baseline"][direction]["top_5"]


def test_stage_b_decomposition_null_policy_and_hypothesis_contract(stage_b_result: dict[str, object]) -> None:
    result = stage_b_result
    decomposition = result["directional_gap_decompositions"]
    assert decomposition["direct_arithmetic"]["sum"] == pytest.approx(decomposition["baseline_expectancy_gap"], abs=1e-12)
    for value in [decomposition["stop_nonstop"], decomposition["setup_path"], *decomposition["regime"].values()]:
        if value["available"]:
            assert value["sum"] == pytest.approx(decomposition["baseline_expectancy_gap"], abs=1e-12)
    assert "direct_component_shares" in result["classification"]["evidence"]
    assert all(result["classification"]["evidence"]["regime"][name]["rule_triggered"]
               for name in result["classification"]["evidence"]["triggered_regime_families"])
    for hypothesis in result["hypotheses_generated_not_tested"]:
        assert {"label", "observation", "possible_mechanism", "evidence_path", "evidence_values", "plausible_confounders", "future_confirmatory_test", "independent_untouched_data_needed", "discovery_disclaimer"} <= set(hypothesis)
    assert any("missing for" in limitation for limitation in result["limitations"])


def test_one_sided_composition_is_unavailable_and_distribution_missing_is_null() -> None:
    left = [_closed_row("l", 1.0)]; right = []
    decomposition = stage_a._composition_within(left, right, "path", ("immediate_open", "armed_then_opened"))
    assert decomposition["available"] is False and decomposition["sum"] is None
    assert decomposition["buckets"]["immediate_open"]["m_long"] == 1.0
    assert decomposition["buckets"]["immediate_open"]["m_short"] is None
    assert stage_a._distribution([]) == {"count": 0, "mean": None, "median": None, "p25": None, "p75": None, "p90": None, "maximum": None, "sample_warning": stage_a.SMALL_SAMPLE_WARNING}
    assert stage_a._cell(stage_a._metrics([]))["sample_warning"] == "SMALL-SAMPLE / DESCRIPTIVE ONLY"


def test_result_verifier_serializer_tamper_and_overwrite(stage_b_result: dict[str, object], tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    verifier = stage_a.verify_xau_directional_edge_attribution_result
    verifier(stage_b_result)
    payload = stage_a.xau_directional_edge_attribution_json(stage_b_result)
    assert payload.endswith(b"\n") and payload == stage_a.xau_directional_edge_attribution_json(stage_b_result)
    path = tmp_path / "result.json"
    # The preceding public verifier call is the canonical-replay integration
    # check; isolate writer overwrite mechanics from a second canonical replay.
    monkeypatch.setattr(stage_a, "verify_xau_directional_edge_attribution_result", lambda *_args, **_kwargs: None)
    assert stage_a.write_xau_directional_edge_attribution_result(stage_b_result, path) == __import__("hashlib").sha256(payload).hexdigest()
    with pytest.raises(FileExistsError): stage_a.write_xau_directional_edge_attribution_result(stage_b_result, path)
    changed = json.loads(payload); changed["restriction_state"]["directional_filter_tested"] = "YES"
    with pytest.raises(ValueError, match="restriction"):
        verifier(changed)


def test_stage_b_count_alias_utc_boundaries_and_protocol_threshold_literals() -> None:
    assert stage_a._metrics([])["count"] == stage_a._metrics([])["closed_trades"] == 0
    assert stage_a._calendar_label(1_704_067_200_000, "year") == "2024"  # 2024-01-01T00:00:00Z
    assert stage_a._calendar_label(1_711_929_599_999, "quarter") == "2024-Q1"
    assert stage_a._calendar_label(1_711_929_600_000, "quarter") == "2024-Q2"
    protocol = json.loads(Path("exports/xm/phase_xau_directional_edge_attribution_protocol.json").read_bytes())
    rules = str(protocol["classification"]["rules"])
    assert ">=0.60" in rules and ">=0.50" in rules and ">=0.25" in rules
    assert protocol["distribution_and_null_policy"]["minimum_cell"] == "n < 30 closed trades => SMALL-SAMPLE / DESCRIPTIVE ONLY"


def test_stage_b_context_ledger_and_mfe_mae_reconcile(stage_b_result: dict[str, object]) -> None:
    result = stage_b_result
    contexts = result["eligible_setup_context_ledger"]
    assert len(contexts) == 1072 and len({row["timestamp"] for row in contexts}) == 1072
    assert contexts == sorted(contexts, key=lambda row: row["timestamp"])
    ledger = result["enriched_closed_trade_ledger"]
    assert len(ledger) == 820 and all(row["outcome"] == "closed" for row in ledger)
    assert all(row["mfe_r"] is None or row["mfe_r"] >= 0 for row in ledger)
    assert all(row["mae_r"] is None or row["mae_r"] >= 0 for row in ledger)
    missing = sum(row["mfe_r"] is None or row["mae_r"] is None for row in ledger if row["stop_hit"])
    reported = sum(result["exit_loss_and_holding"][side]["stopped_trade_diagnostics"]["missing_path_count"] for side in ("long", "short"))
    assert missing == reported


def test_stage_b_later_contrast_exact_source_and_short_headline(stage_b_result: dict[str, object]) -> None:
    contrast = stage_b_result["later_period_directional_contrast"]
    assert contrast["source"] == "frozen compatibility provider_economics.xm.by_direction"
    assert contrast["separate_not_validation"] is True and contrast["merge_with_historical"] is False
    frozen = json.loads(Path(stage_a.COMPATIBILITY_RESULT_PATH).read_bytes())["provider_economics"]["xm"]["by_direction"]["short"]
    assert contrast["by_direction"]["short"]["expectancy_r"]["later_compatibility"] == frozen["expectancy_r"]


@pytest.mark.parametrize("section", ["aggregate_baseline", "calendar", "setup_path", "exit_loss_and_holding", "regime", "directional_gap_decompositions", "classification", "hypotheses_generated_not_tested", "later_period_directional_contrast", "enriched_closed_trade_ledger", "eligible_setup_context_ledger"])
def test_stage_b_self_reconciling_verifier_rejects_table_and_ledger_tampering(stage_b_result: dict[str, object], section: str) -> None:
    changed = json.loads(stage_a.xau_directional_edge_attribution_json(stage_b_result))
    if section in {"enriched_closed_trade_ledger", "eligible_setup_context_ledger", "hypotheses_generated_not_tested"}:
        changed[section] = []
    elif section == "classification":
        changed[section]["labels"] = []
    elif section == "aggregate_baseline":
        changed[section]["total_r"] = 0
    elif section == "later_period_directional_contrast":
        changed[section]["source"] = "tampered"
    else:
        changed[section] = {}
    with pytest.raises(ValueError):
        stage_a.verify_xau_directional_edge_attribution_result(changed)


def test_stage_b_public_guard_rejects_dirty_worktree(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(stage_a, "_tracked_git_clean", lambda _root: False)
    with pytest.raises(ValueError, match="clean tracked"):
        stage_a.build_xau_directional_edge_attribution_result(repo_root=Path("."), xm_m1_source=Path(stage_a.RAW_SOURCE_PATH), protocol_path=Path("exports/xm/phase_xau_directional_edge_attribution_protocol.json"))


@pytest.mark.parametrize("field,value", [("entry_price", 0.01), ("mfe_r", 0.25)])
def test_strict_result_verifier_pins_semantic_ledger_to_frozen_replay(stage_b_result: dict[str, object], field: str, value: float) -> None:
    changed = json.loads(stage_a.xau_directional_edge_attribution_json(stage_b_result))
    changed["enriched_closed_trade_ledger"][0][field] = float(changed["enriched_closed_trade_ledger"][0][field]) + value
    # Refresh the dependent diagnostic table: only an independent frozen replay
    # identity check can now distinguish this semantic ledger tampering.
    contexts = stage_a._reconcile_context_ledger(changed["eligible_setup_context_ledger"])
    changed["exit_loss_and_holding"] = stage_a._exit_and_holding(changed["enriched_closed_trade_ledger"])
    with pytest.raises(ValueError, match="frozen replay enriched ledger"):
        stage_a.verify_xau_directional_edge_attribution_result(changed)


@pytest.mark.parametrize("path", [("metadata", "canonical_starting_sha"), ("metadata", "protocol_commit_sha"), ("metadata", "protocol_sha256"), ("metadata", "execution_head_sha"), ("population_reproduction", "warmup"), ("population_reproduction", "aggregation"), ("population_reproduction", "historical_ledger_identity")])
def test_result_verifier_rejects_metadata_and_reproduction_tampering(stage_b_result: dict[str, object], path: tuple[str, str]) -> None:
    changed = json.loads(stage_a.xau_directional_edge_attribution_json(stage_b_result))
    target, key = path
    changed[target][key] = "tampered"
    with pytest.raises(ValueError):
        stage_a.verify_xau_directional_edge_attribution_result(changed)


def test_closed_ledger_stop_and_missing_path_semantics_fail_closed() -> None:
    context = {"timestamp": 1, "direction": "long", "path": "immediate_open", "bias_persistence_hours": 1.0, "atr_at_setup": 1.0, "broad_market_direction_32": "up", "bias_persistence_hours_bucket": "low", "atr_at_setup_bucket": "low", "broad_market_direction_32_bucket": "up"}
    row = _closed_row("x", -1.0, stop=False)
    row.update({"setup_origin_timestamp": 1, **{key: value for key, value in context.items() if key != "timestamp"}})
    rows = [{**row, "trade_id": f"x:{index:04d}", "entry_timestamp": index,
             "exit_timestamp": index + 1} for index in range(820)]
    rows[0]["exit_reasons"] = ["exit_stop"]
    with pytest.raises(ValueError, match="stop membership"):
        stage_a._reconcile_closed_ledger(rows, [context] * 1072)
    rows[0]["stop_hit"] = True; rows[0]["mfe_r"] = None
    with pytest.raises(ValueError, match="MFE/MAE"):
        stage_a._reconcile_closed_ledger(rows, [context] * 1072)


def test_stage_b_guard_rejects_alternate_raw_path_after_mocked_git_checks(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    class Process:
        def __init__(self, stdout: bytes | str = b"") -> None:
            self.returncode = 0; self.stdout = stdout
    protocol = Path("exports/xm/phase_xau_directional_edge_attribution_protocol.json").read_bytes()
    def fake_run(command: tuple[str, ...], **_kwargs: object) -> Process:
        if command[:2] == ("git", "show"): return Process(protocol)
        if command[:2] == ("git", "rev-parse"):
            argument = command[2]
            return Process((stage_a.CANONICAL_TAG_OBJECT if argument == stage_a.CANONICAL_TAG else stage_a.CANONICAL_STARTING_SHA) + "\n")
        return Process()
    monkeypatch.setattr(stage_a, "_tracked_git_clean", lambda _root: True)
    monkeypatch.setattr(stage_a.subprocess, "run", fake_run)
    monkeypatch.setattr(stage_a.historical, "verify_frozen_production_sources", lambda _root: {})
    alternate = tmp_path / "alternate.csv"; alternate.write_text("x", encoding="utf-8")
    with pytest.raises(ValueError, match="canonical raw XM source path"):
        stage_a._verify_stage_b_guard(repo_root=Path("."), protocol_path=Path("exports/xm/phase_xau_directional_edge_attribution_protocol.json"), xm_m1_source=alternate)


def test_full_context_lock_reproduces_structural_identities_without_economics(monkeypatch: pytest.MonkeyPatch) -> None:
    def fail(*_args: object, **_kwargs: object) -> None:
        raise AssertionError("economic path must not be called by Stage A")
    monkeypatch.setattr(stage_a.historical, "_ledger", fail)
    monkeypatch.setattr(stage_a.historical, "_economics", fail)
    monkeypatch.setattr(stage_a.historical.BacktestEngine, "run", fail)
    # Stage A is intentionally generated while HEAD is the canonical starting
    # commit.  Once its separate lock commit exists, replay determinism must
    # remain testable without pretending that HEAD is still the parent commit.
    monkeypatch.setattr(stage_a, "verify_stage_a_identities", lambda _root: {
        "source_sha256": stage_a.EXPECTED_SOURCE_SHA256,
    })
    lock = stage_a.build_context_lock(repo_root=Path("."), xm_m1_source=Path(stage_a.RAW_SOURCE_PATH))
    assert lock["population"] == stage_a.EXPECTED_POPULATION
    assert lock["first_eligible_timestamp_ms"] == stage_a.FIRST_ELIGIBLE_TIMESTAMP_MS
