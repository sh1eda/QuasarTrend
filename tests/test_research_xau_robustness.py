from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from quasartrend.research.xau_robustness import (
    FROZEN_BASELINE_ARTIFACT_SHA256, PENDING_SOL_REVIEW, _distribution, _exit_analysis,
    _record_trade_close,
    breakeven_cost_r, build_xauusd_robustness_report, chronological_months,
    economics, friction_stress, leave_one_month_out, path_risk, tail_baseline, tail_removal,
    write_xauusd_robustness_report, xauusd_robustness_json,
)


SOURCE_15M = Path("exports/xauusd_pending/XAUUSD_15m.csv")
SOURCE_4H = Path("exports/xauusd_pending/XAUUSD_4h.csv")


def row(identifier: str, r: float | None, *, entry: int, exit_: int | None = None, direction: str = "long", path: str = "immediate_open", stopped: bool | None = False, mfe: float | None = 0.0, strict_pre_exit_mfe: float | None = None) -> dict[str, object]:
    strict_pre_exit_mfe = mfe if strict_pre_exit_mfe is None and mfe is not None else strict_pre_exit_mfe
    return {"trade_id": identifier, "outcome": "closed" if r is not None else "censored", "r": r, "entry_timestamp": entry, "exit_timestamp": exit_ if exit_ is not None else entry, "direction": direction, "path": path, "stop_hit": stopped, "mfe_r": mfe, "strict_pre_exit_mfe_r": strict_pre_exit_mfe}


def test_chronological_grouping_lomo_direction_path_and_censored_exclusion() -> None:
    rows = [row("a", 1, entry=1_772_000_000_000, direction="long"), row("b", -1, entry=1_774_700_000_000, direction="short", path="armed_then_opened"), row("c", None, entry=1_774_700_000_001)]
    groups = chronological_months(rows)
    assert list(groups) == ["2026-02", "2026-03"]
    lomo = leave_one_month_out(rows)
    assert lomo["2026-02"]["closed_trades"] == 1
    assert lomo["2026-02"]["total_r"] == -1
    assert economics([rows[2]])["closed_trades"] == 0


def test_tail_ties_are_stable_and_friction_recomputes_signs_and_pf() -> None:
    rows = [row("later", 2, entry=0, exit_=20), row("first", 2, entry=0, exit_=10), row("loss", -1, entry=0, exit_=30)]
    removed = tail_removal(rows, (1,))["1"]
    assert removed["removed_trade_ids"] == ["first"]
    stressed = friction_stress(rows, (0.0, 1.5))["1.50"]
    assert stressed["total_r"] == pytest.approx(-1.5)
    assert stressed["profit_factor"] == pytest.approx(1.0 / 2.5)
    assert breakeven_cost_r(rows) == pytest.approx(1.0)
    baseline = tail_baseline(rows)
    assert baseline["maximum_winner_r"] == 2
    assert baseline["top_5"]["r"] == [2.0, 2.0]
    assert baseline["top_5"]["positive_r_share"] == 1
    assert baseline["top_5"]["net_r_share"] == pytest.approx(4 / 3)


def test_path_risk_has_initial_equity_recovery_streak_and_overlapping_windows() -> None:
    rows = [row("a", 2, entry=0, exit_=1), row("b", -1, entry=0, exit_=2), row("c", -2, entry=0, exit_=3), row("d", 4, entry=0, exit_=4)]
    result = path_risk(rows, (2, 3))
    assert result["initial_equity_r"] == 0
    assert result["maximum_peak_to_trough_drawdown_r"] == 3
    assert result["peak_trade_ordinal"] == 1
    assert result["peak_timestamp"] == 1
    assert result["trough_timestamp"] == 3
    assert result["recovery_timestamp"] == 4
    assert result["recovery_elapsed_ms_from_trough"] == 1
    assert result["longest_losing_streak_trades"] == 2
    assert result["recovery_trades_after_trough"] == 1
    assert result["rolling_windows"]["2"]["worst_total_r"] == -3
    assert result["rolling_windows"]["2"]["overlapping_non_independent"] is True


def test_path_risk_snapshots_maximum_drawdown_peak_before_later_new_high() -> None:
    rows = [row("a", 3, entry=0, exit_=10), row("b", -2, entry=0, exit_=20), row("c", -2, entry=0, exit_=30), row("d", 6, entry=0, exit_=40)]
    result = path_risk(rows)
    assert result["maximum_peak_to_trough_drawdown_r"] == 4
    assert result["peak_trade_ordinal"] == 1
    assert result["peak_timestamp"] == 10
    assert result["peak_cumulative_r"] == 3
    assert result["trough_trade_ordinal"] == 3
    assert result["recovery_trade_ordinal"] == 4
    assert result["recovery_trades_after_peak"] == 3


def test_missing_mfe_gap_is_preserved_as_a_caveat() -> None:
    result = _exit_analysis([row("a", -1, entry=1_772_000_000_000, stopped=True, mfe=None), row("b", -1, entry=1_772_000_000_001, stopped=True, mfe=1), row("c", -1, entry=1_774_700_000_000, direction="short", stopped=True, mfe=3), row("d", 2, entry=2, stopped=False)])
    assert result["stopped_mfe_missing_data_gap_count"] == 1
    assert "lack both MFE" in result["missing_mfe_data_gap_caveat"]
    assert result["aggregate"]["stopped_mfe"]["full_through_exit_bar"] == {"stopped_trade_count": 3, "observed_mfe_count": 2, "missing_mfe_data_gap_count": 1, "mean_mfe_r": 2.0, "median_mfe_r": 2.0}
    assert result["aggregate"]["stopped_mfe"]["strict_pre_exit"] == {"stopped_trade_count": 3, "observed_mfe_count": 2, "missing_mfe_data_gap_count": 1, "mean_mfe_r": 2.0, "median_mfe_r": 2.0}
    assert result["by_direction"]["stopped_mfe"]["short"]["full_through_exit_bar"]["mean_mfe_r"] == 3
    assert result["by_entry_month"]["stopped_mfe"]["2026-02"]["full_through_exit_bar"]["observed_mfe_count"] == 1


def test_stopped_mfe_dual_summary_keeps_full_exit_bar_and_exposes_pre_exit_comparison() -> None:
    result = _exit_analysis([
        row("exit_bar_favorable", -1, entry=1, stopped=True, mfe=3, strict_pre_exit_mfe=1),
        row("unchanged", -1, entry=2, stopped=True, mfe=2, strict_pre_exit_mfe=2),
        row("gap", -1, entry=3, stopped=True, mfe=None),
    ])
    stopped_mfe = result["aggregate"]["stopped_mfe"]
    assert stopped_mfe["full_through_exit_bar"]["mean_mfe_r"] == pytest.approx(2.5)
    assert stopped_mfe["strict_pre_exit"]["mean_mfe_r"] == pytest.approx(1.5)
    assert stopped_mfe["full_through_exit_bar_vs_strict_pre_exit"] == {
        "comparable_observation_count": 2,
        "full_strictly_greater_count": 1,
        "aggregate_full_minus_pre_exit_mfe_r": pytest.approx(2.0),
        "mean_full_minus_pre_exit_mfe_r": pytest.approx(1.0),
    }
    assert "intrabar order" in result["mfe_intrabar_order_caveat"]


def test_duplicate_trade_close_is_rejected_before_ledger_accounting() -> None:
    close_events: dict[str, tuple[int, object]] = {}
    event = SimpleNamespace(trade_id="xauusd-1")
    _record_trade_close(close_events, index=7, event=event)
    with pytest.raises(ValueError, match="duplicate trade close"):
        _record_trade_close(close_events, index=8, event=event)


def test_setup_origin_and_trade_entry_distributions_are_separate_and_reconcile() -> None:
    rows = [row("a", 1, entry=1_774_700_000_000), row("b", None, entry=1_774_700_000_000, direction="short", path="armed_then_opened")]
    rows[0]["setup_origin_timestamp"] = 1_772_000_000_000
    rows[1]["setup_origin_timestamp"] = 1_774_700_000_000
    setups = {1_772_000_000_000: {"timestamp": 1_772_000_000_000, "path": "immediate_open"}, 1_774_700_000_000: {"timestamp": 1_774_700_000_000, "path": "armed_then_opened"}}
    result = _distribution(rows, setups)
    assert result["setup_origin_month"]["months"]["2026-02"]["eligible_setups"] == 1
    assert result["trade_entry_month"]["months"]["2026-03"]["opened_trades"] == 2
    assert result["cross_month_setup_open_count"] == 1


@pytest.fixture(scope="module")
def report() -> dict[str, object]:
    return build_xauusd_robustness_report(source_15m=SOURCE_15M, source_4h=SOURCE_4H)


def test_frozen_population_and_oos_boundary(report: dict[str, object]) -> None:
    frozen = report["frozen_baseline"]
    assert frozen["artifact_sha256"] == FROZEN_BASELINE_ARTIFACT_SHA256
    assert frozen["headline_reconciliation"]["observed_setups"] == 541
    assert frozen["headline_reconciliation"]["closed_trades"] == 201
    assert report["gate_outcome"] == PENDING_SOL_REVIEW
    assert report["oos_design"]["status"].startswith("NO DEFENSIBLE UNTOUCHED OOS")
    assert "strictly after 2026-08-25T17:30Z" in report["oos_design"]["forward_requirement"]
    assert report["stop_giveback"]["stopped_mfe_missing_data_gap_count"] == 5
    stopped_mfe = report["stop_giveback"]["aggregate"]["stopped_mfe"]
    assert stopped_mfe["full_through_exit_bar"]["mean_mfe_r"] == pytest.approx(0.8842271833770343)
    assert stopped_mfe["full_through_exit_bar"]["median_mfe_r"] == pytest.approx(0.4948131984771692)
    assert stopped_mfe["strict_pre_exit"]["mean_mfe_r"] == pytest.approx(0.7916047661812167)
    assert stopped_mfe["strict_pre_exit"]["median_mfe_r"] == pytest.approx(0.3883958580977207)
    comparison = stopped_mfe["full_through_exit_bar_vs_strict_pre_exit"]
    assert comparison["comparable_observation_count"] == 118
    assert comparison["full_strictly_greater_count"] == 34
    assert comparison["aggregate_full_minus_pre_exit_mfe_r"] == pytest.approx(10.929445229106477)
    assert comparison["mean_full_minus_pre_exit_mfe_r"] == pytest.approx(0.0926224171958176)
    distribution = report["data_sufficiency"]["monthly_population_distribution"]
    assert distribution["cross_month_setup_open_count"] == 1
    assert sum(item["observed_setups"] for item in distribution["setup_origin_month"]["months"].values()) == 541
    assert sum(item["opened_trades"] for item in distribution["trade_entry_month"]["months"].values()) == 202
    assert report["chronology"]["cumulative_and_expanding"]["expanding_windows"][-1]["closed_trade_ordinal"] == 201
    assert report["tail_dependence"]["baseline"]["top_5"]["r"]
    assert report["parity_scope"]["visible_plot_pine_python"]["status"] == "NOT_EVALUATED_BY_ARTIFACT_BUILDER"


def test_artifact_json_and_writer_are_deterministic_and_refuse_overwrite(tmp_path: Path, report: dict[str, object]) -> None:
    assert xauusd_robustness_json(report) == xauusd_robustness_json(report)
    assert xauusd_robustness_json(report).endswith(b"\n")
    assert json.loads(xauusd_robustness_json(report))["schema_version"]
    target = tmp_path / "robustness.json"
    write_xauusd_robustness_report(report, target)
    first = target.read_bytes()
    with pytest.raises(FileExistsError):
        write_xauusd_robustness_report(report, target)
    write_xauusd_robustness_report(report, target, overwrite=True)
    assert target.read_bytes() == first
