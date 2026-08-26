"""Read-only, deterministic cross-market baseline construction.

This module deliberately does not use the Phase 7 dual-OHLC parser or its BTC
calendar splits: XAUUSD was supplied as a standard TradingView visible export.
"""

from __future__ import annotations

import csv
import json
import math
from collections import Counter, defaultdict
from dataclasses import asdict
from datetime import UTC, datetime
from hashlib import sha256
from io import StringIO
from pathlib import Path
from statistics import mean, median
from typing import Any

from quasartrend.backtest import BacktestConfig, BacktestEngine
from quasartrend.replay import HistoricalBar, ReplayConfig, ReplayEngine, Timeframe
from quasartrend.strategy import Direction, EventType, ReadinessState, StrategyConfig

from .adr import BAR_MS, adr_contexts, daily_ranges, utc_date
from .models import ResearchConfig
from .provenance import canonical_json, fingerprint, source_fingerprint


SCHEMA_VERSION = "xauusd-market-transfer-baseline/v1"
CANONICAL_FREEZE_SHA = "54bf54259685c890e3cfba9b6176804d332e05e7"
VISIBLE_PLOT_PARSER_ID = "tradingview-visible-ohlc-csv/v1"


def parse_visible_tradingview_export(
    raw_input: bytes, *, declared_symbol: str, timeframe: Timeframe,
) -> tuple[HistoricalBar, ...]:
    """Parse one single-OHLC TradingView export without repairing gaps.

    The source has no symbol column. ``declared_symbol`` is therefore caller
    supplied and remains explicitly unverified provenance, not inferred data.
    """

    if not declared_symbol:
        raise ValueError("declared symbol must be non-empty")
    try:
        rows = list(csv.reader(StringIO(raw_input.decode("utf-8-sig"))))
    except UnicodeDecodeError as error:
        raise ValueError("source input must be UTF-8-sig CSV") from error
    if len(rows) < 2:
        raise ValueError("source CSV must contain a header and at least one row")
    header = rows[0]
    positions = {name: [i for i, value in enumerate(header) if value == name] for name in ("time", "open", "high", "low", "close")}
    if any(len(indexes) != 1 for indexes in positions.values()):
        raise ValueError("source CSV requires unique time and single OHLC columns")
    result: list[HistoricalBar] = []
    for number, row in enumerate(rows[1:], start=2):
        if len(row) != len(header):
            raise ValueError(f"source CSV row {number} has wrong column count")
        try:
            seconds = int(row[positions["time"][0]])
            values = {name: float(row[positions[name][0]]) for name in ("open", "high", "low", "close")}
        except ValueError as error:
            raise ValueError(f"source CSV row {number} has invalid time or OHLC") from error
        if not all(math.isfinite(value) for value in values.values()):
            raise ValueError("source OHLC must be finite")
        if values["low"] > min(values["open"], values["close"]) or values["high"] < max(values["open"], values["close"]):
            raise ValueError("source OHLC envelope does not contain open and close")
        result.append(HistoricalBar(declared_symbol, timeframe, seconds * 1000, **values))
    if not result:
        raise ValueError("source CSV contains no rows")
    if any(right.open_time <= left.open_time for left, right in zip(result, result[1:])):
        raise ValueError("source timestamps must be strictly increasing and unique")
    deltas = [right.open_time - left.open_time for left, right in zip(result, result[1:])]
    if Counter(deltas).most_common(1)[0][0] != timeframe.duration_ms:
        raise ValueError("source modal cadence does not match declared timeframe")
    return tuple(result)


def _source_report(raw: bytes, bars: tuple[HistoricalBar, ...]) -> dict[str, Any]:
    deltas = [right.open_time - left.open_time for left, right in zip(bars, bars[1:])]
    mode = Counter(deltas).most_common(1)[0][0] if deltas else None
    first_day = datetime.fromtimestamp(bars[0].open_time / 1000, UTC).date()
    last_day = datetime.fromtimestamp(bars[-1].open_time / 1000, UTC).date()
    return {
        "raw_sha256": sha256(raw).hexdigest(), "normalized_sha256": source_fingerprint(bars),
        "row_count": len(bars), "first_open_timestamp": bars[0].open_time,
        "last_open_timestamp": bars[-1].open_time, "first_utc": _utc(bars[0].open_time),
        "last_utc": _utc(bars[-1].open_time), "modal_cadence_ms": mode,
        "inclusive_calendar_days": (last_day - first_day).days + 1,
        "gap_count": sum(delta != mode for delta in deltas), "duplicate_timestamps": 0,
        "non_monotonic_timestamps": 0, "identity_status": "caller_declared_unverified_no_symbol_column",
        "source_identity_from_internal_csv_metadata": "UNVERIFIED",
    }


def _utc(timestamp: int) -> str:
    return datetime.fromtimestamp(timestamp / 1000, UTC).isoformat().replace("+00:00", "Z")


def _merge(ltf: tuple[HistoricalBar, ...], htf: tuple[HistoricalBar, ...]) -> tuple[HistoricalBar, ...]:
    merged = tuple(sorted((*ltf, *htf), key=lambda bar: bar.processing_key))
    if any(right.processing_key <= left.processing_key for left, right in zip(merged, merged[1:])):
        raise ValueError("source processing order is not strict")
    return merged


def _median(values: list[float]) -> float | None:
    return None if not values else float(median(values))


def _economics(rows: list[dict[str, Any]], eligible: int | None) -> dict[str, Any]:
    closed = [row for row in rows if row["outcome"] == "closed"]
    r = [row["r"] for row in closed]
    wins = [value for value in r if value > 0]
    losses = [value for value in r if value < 0]
    mae = [row["mae_r"] for row in closed if row["mae_r"] is not None]
    mfe = [row["mfe_r"] for row in closed if row["mfe_r"] is not None]
    durations = [row["duration_ms"] for row in closed]
    return {
        "eligible_setups": eligible, "opened_trades": len(rows), "closed_trades": len(closed),
        "censored_trades": len(rows) - len(closed), "total_r": float(sum(r)),
        "expectancy_r": None if not r else float(mean(r)), "r_per_setup": None if not eligible else float(sum(r) / eligible),
        "profit_factor": None if not losses else float(sum(wins) / abs(sum(losses))),
        "win_rate": None if not r else sum(value > 0 for value in r) / len(r),
        "stop_rate": None if not closed else sum(row["stop_hit"] for row in closed) / len(closed),
        "mean_r": None if not r else float(mean(r)), "median_r": _median(r),
        "mean_mae_r": None if not mae else float(mean(mae)), "median_mae_r": _median(mae),
        "mean_mfe_r": None if not mfe else float(mean(mfe)), "median_mfe_r": _median(mfe),
        "mean_duration_ms": None if not durations else float(mean(durations)), "median_duration_ms": _median(durations),
        "positive_r": float(sum(wins)), "negative_r_magnitude": float(abs(sum(losses))),
    }


def _tail(rows: list[dict[str, Any]]) -> dict[str, Any]:
    values = sorted((row["r"] for row in rows if row["outcome"] == "closed" and row["r"] > 0), reverse=True)
    positive = sum(values)
    top5, top10 = values[:5], values[:10]
    return {"winners_ge_2r": sum(value >= 2 for value in values), "winners_ge_3r": sum(value >= 3 for value in values),
            "winners_ge_5r": sum(value >= 5 for value in values), "maximum_r": None if not values else values[0],
            "top_5_winner_r": top5, "top_10_winner_r": top10,
            "top_5_positive_r_share": None if not positive else sum(top5) / positive,
            "top_10_positive_r_share": None if not positive else sum(top10) / positive}


def _session_report(bars: tuple[HistoricalBar, ...], timeframe: Timeframe) -> dict[str, Any]:
    grouped: dict[str, list[HistoricalBar]] = defaultdict(list)
    for bar in bars: grouped[utc_date(bar.open_time)].append(bar)
    expected = 96 if timeframe is Timeframe.MINUTES_15 else 6
    complete = sum(len(group) == expected for group in grouped.values())
    return {"utc_dates": len(grouped), "complete_utc_sessions": complete,
            "incomplete_utc_sessions": len(grouped) - complete,
            "definition": f"exactly {expected} observed {timeframe.value} bars per UTC date; no gap repair"}


def _adr_audit(statuses: list[str]) -> dict[str, Any]:
    reasons = Counter(status for status in statuses if status != "available")
    return {
        "total": len(statuses), "available": sum(status == "available" for status in statuses),
        "unavailable": sum(status != "available" for status in statuses),
        "unavailable_reason_counts": dict(sorted(reasons.items())),
    }


def build_market_transfer_baseline(
    *, source_15m: Path, source_4h: Path, declared_symbol: str = "XAUUSD",
) -> dict[str, Any]:
    """Run unchanged default strategy/replay/backtest semantics on supplied XAU bars."""

    if source_15m.resolve() == source_4h.resolve() or not source_15m.is_file() or not source_4h.is_file():
        raise ValueError("require distinct existing 15m and 4H source paths")
    expected_stems = (f"{declared_symbol}_15m", f"{declared_symbol}_4h")
    actual_stems = (source_15m.stem, source_4h.stem)
    if tuple(stem.casefold() for stem in actual_stems) != tuple(stem.casefold() for stem in expected_stems):
        raise ValueError("declared symbol must match both source filename stems")
    raw15, raw4 = source_15m.read_bytes(), source_4h.read_bytes()
    ltf = parse_visible_tradingview_export(raw15, declared_symbol=declared_symbol, timeframe=Timeframe.MINUTES_15)
    htf = parse_visible_tradingview_export(raw4, declared_symbol=declared_symbol, timeframe=Timeframe.HOURS_4)
    ltf_finalization_cutoff = ltf[-1].finalized_at
    replay_htf = tuple(bar for bar in htf if bar.finalized_at <= ltf_finalization_cutoff)
    if not replay_htf:
        raise ValueError("no finalized 4H bars are available within the supplied 15m range")
    replay_config, strategy_config, backtest_config, research_config = ReplayConfig(), StrategyConfig(), BacktestConfig(), ResearchConfig()
    replay = ReplayEngine(replay_config, strategy_config).run(_merge(ltf, replay_htf))
    backtest = BacktestEngine(backtest_config).run(replay)
    if any(bar.symbol != declared_symbol for bar in (*ltf, *htf)):
        raise ValueError("source identity was not preserved")

    ltf_traces = [trace for trace in replay.traces if trace.source_bar.timeframe is Timeframe.MINUTES_15]
    if any(right.source_bar.processing_key <= left.source_bar.processing_key for left, right in zip(replay.traces, replay.traces[1:])):
        raise ValueError("replay has non-strict processing order")
    trace_by_trade_close: dict[str, tuple[int, Any]] = {}
    setups: dict[int, dict[str, Any]] = {}
    armed: dict[Direction, int] = {}
    entries: dict[str, dict[str, Any]] = {}
    for index, trace in enumerate(ltf_traces):
        for event in trace.events:
            if event.type is EventType.HEMA_FLIP_DETECTED and event.side is not None:
                setups[event.timestamp] = {"direction": event.side, "path": "rejected", "timestamp": event.timestamp}
            elif event.type is EventType.SETUP_ARMED and event.side is not None:
                origin = trace.post_state.pending_flip_timestamp
                if origin is None: raise ValueError("armed setup lacks flip origin")
                setups[origin]["path"] = "armed_then_cancelled"; armed[event.side] = origin
            elif event.type is EventType.SETUP_CANCELLED and event.side is not None:
                origin = armed.pop(event.side, None)
                if origin is not None: setups[origin]["path"] = "armed_then_cancelled"
            elif event.type is EventType.TRADE_OPENED:
                trade = trace.post_state.trade
                if trade is None or event.trade_id != trade.trade_id: raise ValueError("opened trade state mismatch")
                path = "armed_then_opened" if trade.setup_origin_timestamp in setups and setups[trade.setup_origin_timestamp]["path"] == "armed_then_cancelled" else "immediate_open"
                setups.setdefault(trade.setup_origin_timestamp, {"direction": trade.side, "timestamp": trade.setup_origin_timestamp})["path"] = path
                entries[trade.trade_id] = {"trade": trade, "index": index, "path": path, "entry_timestamp": event.timestamp}
            elif event.type is EventType.TRADE_CLOSED:
                if event.trade_id in trace_by_trade_close: raise ValueError("duplicate trade close")
                trace_by_trade_close[event.trade_id] = (index, event)
    closed_by_id = {trade.trade_id: trade for trade in backtest.closed_trades}
    if set(trace_by_trade_close) != set(closed_by_id): raise ValueError("replay/backtest closed trade IDs disagree")
    if any(not trade_id.startswith(f"{declared_symbol}:") for trade_id in entries): raise ValueError("trade IDs do not preserve declared symbol")

    adr = adr_contexts(ltf)
    rows: list[dict[str, Any]] = []
    for trade_id, info in entries.items():
        trade, start = info["trade"], info["index"]
        closed = closed_by_id.get(trade_id)
        row: dict[str, Any] = {"trade_id": trade_id, "direction": trade.side.value, "path": info["path"], "setup_origin_timestamp": trade.setup_origin_timestamp, "entry_timestamp": info["entry_timestamp"], "outcome": "censored", "r": None, "stop_hit": None, "mae_r": None, "mfe_r": None, "duration_ms": None, "exit_reasons": ()}
        if closed is not None:
            end, event = trace_by_trade_close[trade_id]
            bars = [trace.source_bar for trace in ltf_traces[start + 1:end + 1]]
            expected = (ltf_traces[end].source_bar.open_time - ltf_traces[start].source_bar.open_time) // BAR_MS
            contiguous = len(bars) == expected and all(bar.open_time == ltf_traces[start].source_bar.open_time + (i + 1) * BAR_MS for i, bar in enumerate(bars))
            risk = abs(trade.entry_price - trade.stop_price)
            r_value = closed.net_pnl / (risk * closed.quantity)
            mae_r = mfe_r = None
            if contiguous:
                low, high = min((bar.low for bar in bars), default=trade.entry_price), max((bar.high for bar in bars), default=trade.entry_price)
                mae = max(0.0, trade.entry_price - low) if trade.side is Direction.LONG else max(0.0, high - trade.entry_price)
                mfe = max(0.0, high - trade.entry_price) if trade.side is Direction.LONG else max(0.0, trade.entry_price - low)
                mae_r, mfe_r = mae / risk, mfe / risk
            reasons = tuple(reason.value for reason in event.reasons)
            row.update({"outcome": "closed", "r": r_value, "stop_hit": "exit_stop" in reasons, "mae_r": mae_r, "mfe_r": mfe_r,
                        "duration_ms": closed.exit_timestamp - trade.entry_timestamp, "exit_reasons": reasons,
                        "exit_timestamp": closed.exit_timestamp, "post_entry_gap": not contiguous})
        row["adr_status"] = adr[utc_date(ltf_traces[start].source_bar.open_time)].status.value
        rows.append(row)
    if len(rows) != len(entries): raise ValueError("opened trade population does not reconcile")

    eligible = sum(item.get("path") in {"immediate_open", "armed_then_opened", "armed_then_cancelled"} for item in setups.values())
    economics = _economics(rows, eligible)
    closed_rows = [row for row in rows if row["outcome"] == "closed"]
    stop_rows = [row for row in closed_rows if row["stop_hit"]]
    nonstop = [row for row in closed_rows if not row["stop_hit"]]
    f = {"F1_stop_mfe_lt_0_25r": sum(row["mfe_r"] is not None and row["mfe_r"] < .25 for row in stop_rows),
         "F2_stop_mfe_0_25_to_lt_1r": sum(row["mfe_r"] is not None and .25 <= row["mfe_r"] < 1 for row in stop_rows),
         "F3_stop_mfe_ge_1r": sum(row["mfe_r"] is not None and row["mfe_r"] >= 1 for row in stop_rows),
         "F4_nonstop_losing_exit": sum(row["r"] < 0 for row in nonstop)}
    entry_month_rows: dict[str, list[dict[str, Any]]] = defaultdict(list)
    setup_origin_month_rows: dict[str, list[dict[str, Any]]] = defaultdict(list)
    setup_origin_month_setups: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for row in rows:
        entry_month_rows[utc_date(row["entry_timestamp"])[:7]].append(row)
        setup_origin_month_rows[utc_date(row["setup_origin_timestamp"])[:7]].append(row)
    for setup in setups.values():
        setup_origin_month_setups[utc_date(setup["timestamp"])[:7]].append(setup)
    direction = {side: {"economics": _economics([row for row in rows if row["direction"] == side], sum(item.get("path") in {"immediate_open", "armed_then_opened", "armed_then_cancelled"} and item["direction"].value == side for item in setups.values())), "tail": _tail([row for row in rows if row["direction"] == side])} for side in ("long", "short")}
    adr_counts = {
        "setup": _adr_audit([adr[utc_date(timestamp - Timeframe.MINUTES_15.duration_ms)].status.value for timestamp in setups]),
        "trade": _adr_audit([row["adr_status"] for row in rows]),
    }
    path_report = {
        path: {
            "count": sum(item.get("path") == path for item in setups.values()),
            "opened_trade_count": sum(row["path"] == path for row in rows),
            "economics": _economics([row for row in rows if row["path"] == path], sum(item.get("path") == path for item in setups.values())),
            "tail": _tail([row for row in rows if row["path"] == path]),
        }
        for path in ("immediate_open", "armed_then_opened", "armed_then_cancelled")
    }
    # An armed setup can cross a calendar boundary before it opens.  Keep the
    # requested entry-decision P&L view separate from setup-origin opportunity
    # attribution so neither table mixes a trade numerator with a denominator
    # belonging to another month.
    entry_months = {
        month: {
            **_economics(items, None),
            "opened_trade_setups": len(items),
            "tail": _tail(items),
        }
        for month, items in sorted(entry_month_rows.items())
    }
    setup_origin_months = {
        month: {
            "observed_setups": len(month_setups),
            **_economics(
                setup_origin_month_rows.get(month, []),
                sum(item.get("path") in {"immediate_open", "armed_then_opened", "armed_then_cancelled"} for item in month_setups),
            ),
            "tail": _tail(setup_origin_month_rows.get(month, [])),
        }
        for month, month_setups in sorted(setup_origin_month_setups.items())
    }
    month_totals = {month: item["total_r"] for month, item in entry_months.items()}
    stopped_mfe = [row["mfe_r"] for row in stop_rows if row["mfe_r"] is not None]
    stopped_mfe_missing = len(stop_rows) - len(stopped_mfe)
    stop_mfe_counts = {"0.25R": sum(value >= .25 for value in stopped_mfe), "0.5R": sum(value >= .5 for value in stopped_mfe), "1R": sum(value >= 1 for value in stopped_mfe), "2R": sum(value >= 2 for value in stopped_mfe)}
    ready_index = next(
        (i for i, trace in enumerate(ltf_traces) if trace.post_state.readiness is ReadinessState.READY),
        None,
    )
    if economics["opened_trades"] != economics["closed_trades"] + economics["censored_trades"]:
        raise ValueError("trade populations do not reconcile")
    if eligible != sum(item["count"] for item in path_report.values()):
        raise ValueError("setup paths do not reconcile")
    if sum(item["opened_trades"] for item in entry_months.values()) != len(rows):
        raise ValueError("entry-month trade population does not reconcile")
    if sum(item["closed_trades"] for item in setup_origin_months.values()) != len(closed_rows):
        raise ValueError("setup-origin month closed-trade population does not reconcile")
    if sum(item["eligible_setups"] for item in setup_origin_months.values()) != eligible:
        raise ValueError("setup-origin month eligible population does not reconcile")
    if f["F1_stop_mfe_lt_0_25r"] + f["F2_stop_mfe_0_25_to_lt_1r"] + f["F3_stop_mfe_ge_1r"] + stopped_mfe_missing != len(stop_rows):
        raise ValueError("stopped loss taxonomy does not reconcile")
    if sum(f.values()) + stopped_mfe_missing != sum(row["r"] < 0 for row in closed_rows):
        raise ValueError("loss taxonomy does not reconcile to losing closed trades")
    report = {
        "schema_version": SCHEMA_VERSION, "canonical_freeze_sha": CANONICAL_FREEZE_SHA,
        "instrument": {"declared_symbol": declared_symbol, "filename_identity": (source_15m.stem, source_4h.stem), "identity_status": "filename_and_caller_declared_unverified_no_symbol_column", "source_identity_from_internal_csv_metadata": "UNVERIFIED"},
        "trade_identity": {"all_trade_ids": sorted(entries), "symbol_derived_prefix": f"{declared_symbol}:"},
        "source": {"parser_id": VISIBLE_PLOT_PARSER_ID, "15m": _source_report(raw15, ltf), "4h": _source_report(raw4, htf)},
        "fingerprints": {"replay": fingerprint(replay_config), "strategy": fingerprint(strategy_config), "backtest": fingerprint(backtest_config), "research": fingerprint(research_config)},
        "ordering": {"strict_finalization_order": True, "first_processing_key": replay.traces[0].source_bar.processing_key, "last_processing_key": replay.traces[-1].source_bar.processing_key},
        "configurations": {"replay": asdict(replay_config), "strategy": asdict(strategy_config), "backtest": asdict(backtest_config), "research": asdict(research_config)},
        "research_baseline": {"classification": "FRICTIONLESS RESEARCH BASELINE", "live_profitability_claim": "not_implied", "quantity": backtest_config.quantity, "fee_bps": backtest_config.fee_bps, "slippage_bps": backtest_config.slippage_bps},
        "parity_scope": {"visible_plot_pine_python": {"status": "NOT_EVALUATED_BY_ARTIFACT_BUILDER", "required_external_verification": "tests/test_xauusd_golden.py", "15m_rows": len(ltf), "4h_rows": len(htf)}, "full_canonical_internal_pine": {"status": "UNVERIFIED", "reason": "CSV exports omit recursive Kalman, ATR, and band internal state"}},
        "replay_input": {"ltf_finalization_cutoff": ltf_finalization_cutoff, "ltf_finalization_cutoff_utc": _utc(ltf_finalization_cutoff), "htf_raw_row_count": len(htf), "htf_replay_included_row_count": len(replay_htf), "htf_replay_excluded_row_count": len(htf) - len(replay_htf)},
        "source_coverage": {"15m": _session_report(ltf, Timeframe.MINUTES_15), "4h": _session_report(htf, Timeframe.HOURS_4),
            "overlap_start_utc": _utc(max(ltf[0].open_time, htf[0].open_time)), "overlap_end_utc": _utc(min(ltf[-1].open_time, htf[-1].open_time)),
            "overlap_inclusive_calendar_days": (datetime.fromtimestamp(min(ltf[-1].open_time, htf[-1].open_time) / 1000, UTC).date() - datetime.fromtimestamp(max(ltf[0].open_time, htf[0].open_time) / 1000, UTC).date()).days + 1,
            "warmup": {"first_strategy_ready_15m_index": ready_index},
            "effective_research_start_utc": None if ready_index is None else _utc(ltf_traces[ready_index].source_bar.open_time),
            "effective_research_end_utc": _utc(ltf_traces[-1].source_bar.open_time),
        },
        "populations": {"observed_setups": len(setups), **economics}, "economics": economics,
        "setup_path": path_report,
        "direction": direction, "tail": _tail(rows),
        "exit_anatomy": {"stop_related_exit_count": len(stop_rows), "strategy_only_exit_count": len(nonstop), "same_bar_stop_strategy_count": sum(row["stop_hit"] and len(row["exit_reasons"]) > 1 for row in closed_rows), "stop_related_total_r": float(sum(row["r"] for row in stop_rows)), "strategy_only_total_r": float(sum(row["r"] for row in nonstop)), "mean_stopped_trade_mfe_r": None if not stopped_mfe else float(mean(stopped_mfe)), "median_stopped_trade_mfe_r": _median(stopped_mfe), "stopped_mfe_observation_count": len(stopped_mfe), "stopped_mfe_missing_data_gap_count": stopped_mfe_missing, "stopped_trade_mfe_reach_all_stopped": {"denominator": len(stop_rows), "counts": stop_mfe_counts, "percentages": {key: None if not stop_rows else value / len(stop_rows) for key, value in stop_mfe_counts.items()}}, "stopped_trade_mfe_reach_observed_only": {"denominator": len(stopped_mfe), "counts": stop_mfe_counts, "percentages": {key: None if not stopped_mfe else value / len(stopped_mfe) for key, value in stop_mfe_counts.items()}}, "failure_taxonomy": {**f, "unclassified_missing_mfe_data_gap": stopped_mfe_missing, "stopped_loss_reconciled_count": len(stop_rows), "losing_closed_trade_reconciled_count": sum(row["r"] < 0 for row in closed_rows)}},
        "chronology": {"trade_entry_month_convention": "entry_finalized_at", "setup_origin_month_convention": "setup_origin_finalized_at", "months": entry_months, "setup_origin_months": setup_origin_months, "cross_month_entry_origin_trade_count": sum(utc_date(row["entry_timestamp"])[:7] != utc_date(row["setup_origin_timestamp"])[:7] for row in rows), "positive_months": sum(value > 0 for value in month_totals.values()), "negative_months": sum(value < 0 for value in month_totals.values()), "best_month": None if not month_totals else max(month_totals, key=month_totals.get), "worst_month": None if not month_totals else min(month_totals, key=month_totals.get)},
        "adr_availability": adr_counts,
        "frozen_btc_reference": {"eligible_setups": 260, "closed_trades": 191, "expectancy_r": -0.1518767197675845, "total_r": -29.008453475608636, "r_per_setup": -0.11157097490618706, "profit_factor": 0.8072390130075646, "win_rate": .193717, "stop_rate": .774869, "positive_r": 121.480781552, "negative_r_magnitude": 150.489235027, "winners_ge_5r": 9},
    }
    return report


def write_market_transfer_report(report: dict[str, Any], path: Path, *, overwrite: bool = False) -> None:
    if path.exists() and not overwrite: raise FileExistsError(f"refusing to overwrite {path}; pass --overwrite")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(canonical_json(report) + "\n", encoding="utf-8")
