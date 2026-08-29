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
