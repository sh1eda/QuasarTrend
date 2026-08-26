from __future__ import annotations

import json
from dataclasses import asdict
from pathlib import Path

import pytest

from quasartrend.backtest import BacktestConfig
from quasartrend.replay import ReplayConfig, Timeframe
from quasartrend.research.market_transfer import (
    CANONICAL_FREEZE_SHA,
    SCHEMA_VERSION,
    build_market_transfer_baseline,
    parse_visible_tradingview_export,
    write_market_transfer_report,
)
from quasartrend.research.models import ResearchConfig
from quasartrend.research.provenance import canonical_json, fingerprint
from quasartrend.strategy import StrategyConfig


SOURCE_15M = Path("exports/xauusd_pending/XAUUSD_15m.csv")
SOURCE_4H = Path("exports/xauusd_pending/XAUUSD_4h.csv")


EXPECTED_SOURCES = {
    "15m": {"raw_sha256": "dc3d17a1d7c23b6e69659520b5a5826a11c0c2672aa77987231f54c46a4dd3cd", "row_count": 11_537, "first_utc": "2026-03-01T23:00:00Z", "last_utc": "2026-08-25T17:30:00Z"},
    "4h": {"raw_sha256": "e9e5330a9f872d2e7e08bcaa2e14e9feaf0f59d833c423a5f2dc3459e29fed25", "row_count": 10_281, "first_utc": "2020-01-01T22:00:00Z", "last_utc": "2026-08-25T17:00:00Z"},
}


def test_visible_parser_preserves_symbol_and_refuses_repairs() -> None:
    bars = parse_visible_tradingview_export(SOURCE_15M.read_bytes(), declared_symbol="XAUUSD", timeframe=Timeframe.MINUTES_15)
    assert bars[0].symbol == "XAUUSD"
    assert all(bar.symbol == "XAUUSD" for bar in bars)
    duplicate = b"time,open,high,low,close\n0,1,2,0,1\n0,1,2,0,1\n"
    with pytest.raises(ValueError, match="strictly increasing"):
        parse_visible_tradingview_export(duplicate, declared_symbol="XAUUSD", timeframe=Timeframe.MINUTES_15)


def test_visible_parser_accepts_non_utc_4h_anchor_without_resampling() -> None:
    raw = b"time,open,high,low,close\n7200,1,2,0,1\n21600,1,2,0,1\n36000,1,2,0,1\n"
    bars = parse_visible_tradingview_export(raw, declared_symbol="XAUUSD", timeframe=Timeframe.HOURS_4)
    assert [bar.open_time for bar in bars] == [7_200_000, 21_600_000, 36_000_000]


def test_market_transfer_refuses_to_relabel_filename_declared_xau_sources(report: dict[str, object]) -> None:
    with pytest.raises(ValueError, match="declared symbol must match both source filename stems"):
        build_market_transfer_baseline(source_15m=SOURCE_15M, source_4h=SOURCE_4H, declared_symbol="BTCUSDT")
    assert report["instrument"]["declared_symbol"] == "XAUUSD"


@pytest.fixture(scope="module")
def report() -> dict[str, object]:
    return build_market_transfer_baseline(source_15m=SOURCE_15M, source_4h=SOURCE_4H)


def test_market_transfer_artifact_is_deterministic_and_uses_frozen_defaults(report: dict[str, object]) -> None:
    rebuilt = build_market_transfer_baseline(source_15m=SOURCE_15M, source_4h=SOURCE_4H)
    assert canonical_json(report) == canonical_json(rebuilt)
    assert report["schema_version"] == SCHEMA_VERSION
    assert report["canonical_freeze_sha"] == CANONICAL_FREEZE_SHA
    assert report["ordering"]["strict_finalization_order"] is True
    assert tuple(report["ordering"]["first_processing_key"]) < tuple(report["ordering"]["last_processing_key"])
    configs = report["configurations"]
    assert configs == {"replay": asdict(ReplayConfig()), "strategy": asdict(StrategyConfig()), "backtest": asdict(BacktestConfig()), "research": asdict(ResearchConfig())}
    assert report["fingerprints"]["replay"] == fingerprint(ReplayConfig())
    assert report["fingerprints"]["strategy"] == fingerprint(StrategyConfig())
    assert report["fingerprints"]["backtest"] == fingerprint(BacktestConfig())


def test_market_transfer_metrics_and_symbol_derived_trade_ids_reconcile(report: dict[str, object]) -> None:
    populations = report["populations"]
    assert populations["opened_trades"] == populations["closed_trades"] + populations["censored_trades"]
    direction = report["direction"]
    assert populations["total_r"] == pytest.approx(direction["long"]["economics"]["total_r"] + direction["short"]["economics"]["total_r"])
    path = report["setup_path"]
    assert populations["opened_trades"] == path["immediate_open"]["opened_trade_count"] + path["armed_then_opened"]["opened_trade_count"]
    assert populations["eligible_setups"] == sum(item["count"] for item in path.values())
    source = report["source"]
    assert source["15m"]["identity_status"] == "caller_declared_unverified_no_symbol_column"
    assert report["instrument"]["identity_status"] == "filename_and_caller_declared_unverified_no_symbol_column"
    assert all(trade_id.startswith("XAUUSD:") for trade_id in report["trade_identity"]["all_trade_ids"])


def test_market_transfer_source_audit_and_zero_tuning_baseline_golden(report: dict[str, object]) -> None:
    for timeframe, expected in EXPECTED_SOURCES.items():
        source = report["source"][timeframe]
        for field, value in expected.items():
            assert source[field] == value
        assert source["duplicate_timestamps"] == source["non_monotonic_timestamps"] == 0
        assert source["source_identity_from_internal_csv_metadata"] == "UNVERIFIED"
    assert report["instrument"]["source_identity_from_internal_csv_metadata"] == "UNVERIFIED"
    assert report["research_baseline"] == {"classification": "FRICTIONLESS RESEARCH BASELINE", "live_profitability_claim": "not_implied", "quantity": 1.0, "fee_bps": 0.0, "slippage_bps": 0.0}
    assert report["parity_scope"] == {"visible_plot_pine_python": {"status": "NOT_EVALUATED_BY_ARTIFACT_BUILDER", "required_external_verification": "tests/test_xauusd_golden.py", "15m_rows": 11_537, "4h_rows": 10_281}, "full_canonical_internal_pine": {"status": "UNVERIFIED", "reason": "CSV exports omit recursive Kalman, ATR, and band internal state"}}
    populations = report["populations"]
    assert {field: populations[field] for field in ("observed_setups", "eligible_setups", "opened_trades", "closed_trades", "censored_trades")} == {"observed_setups": 541, "eligible_setups": 271, "opened_trades": 202, "closed_trades": 201, "censored_trades": 1}
    assert populations["expectancy_r"] == pytest.approx(0.27107467225310733)
    assert populations["total_r"] == pytest.approx(54.48600912287457)
    assert populations["r_per_setup"] == pytest.approx(0.20105538421724933)
    assert populations["profit_factor"] == pytest.approx(1.4109775792842534)
    assert populations["win_rate"] == pytest.approx(0.27860696517412936)
    assert populations["stop_rate"] == pytest.approx(0.6119402985074627)
    assert populations["positive_r"] == pytest.approx(187.0626066534888)
    assert populations["negative_r_magnitude"] == pytest.approx(132.57659753061424)
    assert populations["median_r"] == -1.0
    for side, expected in {"long": (125, 85, 21.328437283108645, 0.25092279156598407, 1.4032525073412698, 0.5764705882352941, 5), "short": (146, 116, 33.15757183976592, 0.2858411365497062, 1.4161050687806382, 0.6379310344827587, 8)}.items():
        eligible, closed, total_r, expectancy, pf, stop_rate, winners_ge_5r = expected
        actual = report["direction"][side]
        assert actual["economics"]["eligible_setups"] == eligible
        assert actual["economics"]["closed_trades"] == closed
        assert actual["economics"]["total_r"] == pytest.approx(total_r)
        assert actual["economics"]["expectancy_r"] == pytest.approx(expectancy)
        assert actual["economics"]["profit_factor"] == pytest.approx(pf)
        assert actual["economics"]["stop_rate"] == pytest.approx(stop_rate)
        assert actual["tail"]["winners_ge_5r"] == winners_ge_5r
    for path, expected in {"immediate_open": (103, 28.332073767026515, 0.275068677349772, 1.4014991217493407), "armed_then_opened": (98, 26.153935355848056, 0.26687689138620463, 1.4217636569008008)}.items():
        closed, total_r, expectancy, pf = expected
        actual = report["setup_path"][path]["economics"]
        assert actual["closed_trades"] == closed
        assert actual["total_r"] == pytest.approx(total_r)
        assert actual["expectancy_r"] == pytest.approx(expectancy)
        assert actual["profit_factor"] == pytest.approx(pf)
    assert report["setup_path"]["armed_then_cancelled"]["count"] == 69
    assert report["tail"]["winners_ge_2r"] == 28
    assert report["tail"]["winners_ge_3r"] == 23
    assert report["tail"]["winners_ge_5r"] == 13
    assert report["tail"]["maximum_r"] == pytest.approx(18.151834911168546)
    assert report["tail"]["top_5_positive_r_share"] == pytest.approx(0.3360636789388313)
    assert report["tail"]["top_10_positive_r_share"] == pytest.approx(0.5216687706447376)
    anatomy = report["exit_anatomy"]
    assert anatomy["stop_related_exit_count"] == 123
    assert anatomy["stop_related_total_r"] == -123.0
    assert anatomy["strategy_only_exit_count"] == 78
    assert anatomy["strategy_only_total_r"] == pytest.approx(177.48600912287458)
    assert anatomy["mean_stopped_trade_mfe_r"] == pytest.approx(0.8842271833770343)
    assert anatomy["median_stopped_trade_mfe_r"] == pytest.approx(0.4948131984771692)


def test_market_transfer_replay_excludes_unfinalized_terminal_4h_bar(report: dict[str, object]) -> None:
    replay_input = report["replay_input"]
    assert replay_input == {"ltf_finalization_cutoff": 1_787_679_900_000, "ltf_finalization_cutoff_utc": "2026-08-25T17:45:00Z", "htf_raw_row_count": 10_281, "htf_replay_included_row_count": 10_280, "htf_replay_excluded_row_count": 1}
    assert report["source"]["4h"]["row_count"] == replay_input["htf_raw_row_count"]
    assert report["ordering"]["last_processing_key"] == (replay_input["ltf_finalization_cutoff"], 1)
    assert report["ordering"]["last_processing_key"][0] <= replay_input["ltf_finalization_cutoff"]


def test_exit_data_gaps_and_adr_availability_are_explicit_and_reconciled(report: dict[str, object]) -> None:
    anatomy = report["exit_anatomy"]
    taxonomy = anatomy["failure_taxonomy"]
    assert anatomy["stopped_mfe_missing_data_gap_count"] == 5
    assert anatomy["stopped_mfe_observation_count"] + anatomy["stopped_mfe_missing_data_gap_count"] == anatomy["stop_related_exit_count"]
    assert taxonomy["F1_stop_mfe_lt_0_25r"] + taxonomy["F2_stop_mfe_0_25_to_lt_1r"] + taxonomy["F3_stop_mfe_ge_1r"] + taxonomy["unclassified_missing_mfe_data_gap"] == anatomy["stop_related_exit_count"]
    assert taxonomy["stopped_loss_reconciled_count"] + taxonomy["F4_nonstop_losing_exit"] == taxonomy["losing_closed_trade_reconciled_count"]
    assert anatomy["stopped_trade_mfe_reach_all_stopped"]["denominator"] == anatomy["stop_related_exit_count"]
    assert anatomy["stopped_trade_mfe_reach_observed_only"]["denominator"] == anatomy["stopped_mfe_observation_count"]
    adr = report["adr_availability"]
    assert adr["setup"]["total"] == report["populations"]["observed_setups"]
    assert adr["trade"]["total"] == report["populations"]["opened_trades"]
    assert adr["setup"]["available"] == adr["trade"]["available"] == 0
    for audit in adr.values():
        assert audit["available"] + audit["unavailable"] == audit["total"]
        assert sum(audit["unavailable_reason_counts"].values()) == audit["unavailable"]
    assert report["source"]["15m"]["inclusive_calendar_days"] > 0
    assert report["source_coverage"]["overlap_inclusive_calendar_days"] > 0
    assert report["source_coverage"]["warmup"]["first_strategy_ready_15m_index"] == 44
    assert report["source_coverage"]["effective_research_start_utc"] == "2026-03-02T10:00:00Z"


def test_chronology_separates_entry_month_trade_results_from_setup_origin_opportunity(report: dict[str, object]) -> None:
    chronology = report["chronology"]
    entry_months = chronology["months"]
    setup_origin_months = chronology["setup_origin_months"]
    assert chronology["trade_entry_month_convention"] == "entry_finalized_at"
    assert chronology["setup_origin_month_convention"] == "setup_origin_finalized_at"
    assert chronology["cross_month_entry_origin_trade_count"] == 1
    assert sum(month["opened_trades"] for month in entry_months.values()) == report["populations"]["opened_trades"]
    assert sum(month["closed_trades"] for month in entry_months.values()) == report["populations"]["closed_trades"]
    assert sum(month["censored_trades"] for month in entry_months.values()) == report["populations"]["censored_trades"]
    assert all(month["eligible_setups"] is None and month["r_per_setup"] is None for month in entry_months.values())
    assert sum(month["observed_setups"] for month in setup_origin_months.values()) == report["populations"]["observed_setups"]
    assert sum(month["eligible_setups"] for month in setup_origin_months.values()) == report["populations"]["eligible_setups"]
    assert sum(month["opened_trades"] for month in setup_origin_months.values()) == report["populations"]["opened_trades"]
    assert sum(month["closed_trades"] for month in setup_origin_months.values()) == report["populations"]["closed_trades"]
    assert sum(month["censored_trades"] for month in setup_origin_months.values()) == report["populations"]["censored_trades"]
    expected_entry_months = {
        "2026-03": (31, 0.8697236005424415, 26.961431616815688, 2.555316423082368),
        "2026-04": (33, 0.20878758930000355, 6.889990446900117, 1.3132537453154738),
        "2026-05": (34, 0.02582370061970181, 0.8780058210698615, 1.0400796172899704),
        "2026-06": (36, 0.2280610589334398, 8.210198121603833, 1.3246685640296783),
        "2026-07": (35, 0.21591378793452615, 7.556982577708416, 1.3064432392824268),
        "2026-08": (32, 0.12466876683677049, 3.9894005387766556, 1.18649129808754),
    }
    for month, (closed, expectancy, total_r, profit_factor) in expected_entry_months.items():
        actual = entry_months[month]
        assert actual["closed_trades"] == closed
        assert actual["expectancy_r"] == pytest.approx(expectancy)
        assert actual["total_r"] == pytest.approx(total_r)
        assert actual["profit_factor"] == pytest.approx(profit_factor)


def test_writer_is_byte_deterministic_and_refuses_unrequested_overwrite(tmp_path: Path, report: dict[str, object]) -> None:
    output = tmp_path / "baseline.json"
    write_market_transfer_report(report, output)
    first = output.read_bytes()
    with pytest.raises(FileExistsError, match="refusing to overwrite"):
        write_market_transfer_report(report, output)
    write_market_transfer_report(report, output, overwrite=True)
    assert output.read_bytes() == first
    assert json.loads(first)["schema_version"] == SCHEMA_VERSION
