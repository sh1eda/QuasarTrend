from __future__ import annotations

from dataclasses import replace
import os
from pathlib import Path
import subprocess
import sys
from types import SimpleNamespace

import pytest

from quasartrend.backtest import BacktestConfig
from quasartrend.research import exit_experiments
from quasartrend.research.exit_counterfactuals import candidate_spec, simulate_counterfactual
from quasartrend.research.exit_experiments import (
    DEVELOPMENT_SUBWINDOWS,
    _ambiguities,
    _bars_by_open,
    _development_population,
    _trade_input,
    development_report,
    development_report_json,
    write_development_report,
)
from quasartrend.research.models import SplitConfig
from quasartrend.research.pipeline import build_canonical_bundle
from quasartrend.strategy import StrategyConfig


GOLDEN_15M = Path("tests/golden/tradingview_15m.csv")
GOLDEN_4H = Path("tests/golden/tradingview_4h.csv")


@pytest.fixture(scope="module")
def bundle():
    return build_canonical_bundle(golden_15m=GOLDEN_15M, golden_4h=GOLDEN_4H)


@pytest.fixture(scope="module")
def report(bundle):
    return development_report(bundle)


def test_development_population_role_order_and_isolation(report) -> None:
    assert report["role"] == "development"
    assert report["inaccessible_roles"] == ("validation", "final_oos")
    assert report["validation_accessed"] is False and report["final_oos_accessed"] is False
    assert report["production_change"] is False
    assert report["population"]["eligible_setups"] == 139
    assert report["population"]["opened_trades"] == report["population"]["closed_trades"] == 103
    assert len(report["candidates"]) == 12
    assert tuple(item["candidate_id"] for item in report["candidates"]) == tuple(
        spec.candidate_id for spec in exit_experiments.STAGE2_CANDIDATE_SPECS
    )
    assert all(len(item["per_trade"]) == 103 and len(item["subwindows"]) == 4 for item in report["candidates"])
    assert tuple(window["start_date"] for window in report["candidates"][0]["subwindows"]) == tuple(
        item.start_date for item in DEVELOPMENT_SUBWINDOWS
    )


@pytest.mark.parametrize(
    ("altered", "binding"),
    (
        (lambda bundle: replace(bundle, strategy_config=StrategyConfig(atr_multiplier=1.5)), "strategy_fingerprint"),
        (lambda bundle: replace(bundle, backtest_config=BacktestConfig(fee_bps=1.0)), "backtest_fingerprint"),
        (lambda bundle: replace(bundle, split_config=SplitConfig(development_end="2026-07-08", validation_start="2026-07-09")), "split_fingerprint"),
        (lambda bundle: replace(bundle, dataset=replace(bundle.dataset, manifest_id="altered")), "manifest_id"),
        (lambda bundle: replace(bundle, dataset=replace(bundle.dataset, setup_rows=bundle.dataset.setup_rows[:-1])), "dataset_fingerprint"),
    ),
)
def test_rejects_altered_live_or_stored_provenance(bundle, altered, binding) -> None:
    with pytest.raises(ValueError, match=rf"binding mismatch: {binding}"):
        development_report(altered(bundle))


def test_rejects_public_spec_rebinding_before_economics(bundle, monkeypatch) -> None:
    monkeypatch.setattr(exit_experiments, "STAGE2_CANDIDATE_SPECS", tuple(reversed(exit_experiments.STAGE2_CANDIDATE_SPECS)))
    with pytest.raises(ValueError, match="semantic fingerprint"):
        development_report(bundle)


def test_trace_mapping_and_canonical_accounting_reconcile(bundle) -> None:
    setups, rows = _development_population(bundle)
    assert len(setups) == 139 and len(rows) == 103
    input_ = _trade_input(rows[0], _bars_by_open(bundle), bundle)
    assert input_.trade_id == rows[0].trade_id
    assert input_.post_entry_bars[-1].open_time == rows[0].exit_source_open_timestamp
    assert len(input_.post_entry_bars) == rows[0].expected_duration_bars
    # The simulator validates canonical R against the same frozen cost basis
    # before it can produce an exit-only counterfactual.
    result = simulate_counterfactual(input_, candidate_spec("EXIT_FIXED_4R"))
    assert result.canonical_realized_r == pytest.approx(rows[0].realized_r, abs=1e-12)
    assert result.delta_r == pytest.approx(result.combined_realized_r - rows[0].realized_r, abs=1e-12)


def test_metrics_tails_windows_ambiguities_and_gate_diagnostics(report) -> None:
    canonical = report["canonical"]["metrics"]
    for candidate in report["candidates"]:
        metrics = candidate["metrics"]
        assert metrics["eligible_setups"] == 139 and metrics["closed_trades"] == 103
        assert set(metrics["tail_winners"]) == {"2.0", "3.0", "4.0", "5.0"}
        assert metrics["positive_r_retention_ratio"] is not None
        audit = candidate["ambiguity_audit"]
        assert set(audit) == {
            "stop_and_tp", "break_even_conflict", "candidate_and_canonical",
            "resolved_conservatively_against_candidate",
        }
        assert all(value["count"] == len(value["trade_ids"]) for value in audit.values())
        gate = candidate["gate"]
        assert gate["windows_improved"] + gate["windows_degraded"] <= 4
        assert isinstance(gate["failed_reasons"], tuple)
        for window in candidate["subwindows"]:
            assert set(window) >= {"canonical", "candidate", "delta_expectancy_r", "delta_total_r"}
            assert window["canonical"]["tail_winners"]["3.0"] >= 0
    assert canonical["eligible_setups"] == 139


def test_ambiguity_audit_excludes_ordinary_later_break_even_stop() -> None:
    ordinary = SimpleNamespace(trade_id="BTC:ordinary", intrabar_ambiguity_flags=("break_even_stop_hit",))
    conflict = SimpleNamespace(
        trade_id="BTC:conflict",
        intrabar_ambiguity_flags=(
            "break_even_trigger_and_new_stop_same_bar",
            "break_even_trigger_and_new_stop_same_bar",
        ),
    )
    audit = _ambiguities((ordinary, conflict))
    assert audit["break_even_conflict"] == {"count": 1, "trade_ids": ("BTC:conflict",)}


def test_bytes_are_independently_deterministic_and_cli_refuses_overwrite(tmp_path: Path, bundle, report) -> None:
    independent = development_report(bundle)
    assert development_report_json(report) == development_report_json(independent)
    output = tmp_path / "development.json"
    write_development_report(report, output)
    assert output.read_bytes() == development_report_json(report)
    with pytest.raises(FileExistsError, match="refusing"):
        write_development_report(report, output)
    command = (
        sys.executable, "tools/run_phase71_exit_development.py", "--golden-15m", str(GOLDEN_15M),
        "--golden-4h", str(GOLDEN_4H), "--output", str(output), "--overwrite",
    )
    completed = subprocess.run(command, env={**os.environ, "PYTHONPATH": "src"}, capture_output=True, check=False)
    assert completed.returncode == 0, completed.stderr.decode()
    assert output.read_bytes() == development_report_json(report)
