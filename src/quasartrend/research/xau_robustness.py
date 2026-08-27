"""Deterministic adversarial diagnostics for the frozen XAUUSD transfer run.

This module deliberately replays the unchanged defaults and never feeds a
diagnostic result back into the strategy, replay, or backtest engines.
"""
from __future__ import annotations

from collections import defaultdict
from hashlib import sha256
import json
import math
from pathlib import Path
from statistics import mean, median
from typing import Any, Iterable, Mapping, Sequence

from quasartrend.backtest import BacktestConfig, BacktestEngine
from quasartrend.replay import ReplayConfig, ReplayEngine, Timeframe
from quasartrend.strategy import Direction, EventType, StrategyConfig

from .adr import BAR_MS, adr_contexts, utc_date
from .market_transfer import _merge, build_market_transfer_baseline, parse_visible_tradingview_export
from .provenance import canonical_json


SCHEMA_VERSION = "xauusd-robustness-oos/v1"
CANONICAL_STARTING_SHA = "bc44c6ab83bed847059d8c32cee837b7a927a82b"
CANONICAL_STARTING_TAG = "xau-market-transfer-pass"
FROZEN_BASELINE_ARTIFACT_SHA256 = "e84d976a57dac2aed300a17c1a9b472e47143127031bf495c345bc67993ecd6f"
PREDECLARED_COSTS_R = (0.0, 0.02, 0.05, 0.10, 0.15, 0.20, 0.25, 0.30)
PREDECLARED_INTERACTION_COSTS_R = (0.05, 0.10, 0.15, 0.20)
PREDECLARED_ROLLING_WINDOWS = (20, 50)
PENDING_SOL_REVIEW = "PENDING_SOL_REVIEW"
FINAL_GATE_OUTCOMES = (
    "XAUUSD ROBUSTNESS / OOS VALIDATION: PASS",
    "XAUUSD ROBUSTNESS / OOS VALIDATION: INCONCLUSIVE — MORE DATA REQUIRED",
    "XAUUSD ROBUSTNESS / OOS VALIDATION: FAIL",
)
ALLOWED_GATE_OUTCOMES = (PENDING_SOL_REVIEW, *FINAL_GATE_OUTCOMES)


def _closed(rows: Iterable[Mapping[str, Any]]) -> list[Mapping[str, Any]]:
    return [row for row in rows if row["outcome"] == "closed"]


def economics(rows: Iterable[Mapping[str, Any]], *, cost_r: float = 0.0) -> dict[str, Any]:
    """Closed-trade economics with a fixed effective round-trip R cost."""
    items = _closed(rows)
    values = [float(row["r"]) - cost_r for row in items]
    wins, losses = [x for x in values if x > 0], [x for x in values if x < 0]
    return {
        "closed_trades": len(items), "total_r": float(sum(values)),
        "expectancy_r": None if not values else float(mean(values)),
        "profit_factor": None if not losses else float(sum(wins) / abs(sum(losses))),
        "win_rate": None if not values else sum(x > 0 for x in values) / len(values),
        "stop_rate": None if not items else sum(bool(row["stop_hit"]) for row in items) / len(items),
        "positive_r": float(sum(wins)), "negative_r_magnitude": float(abs(sum(losses))),
    }


def winner_counts(rows: Iterable[Mapping[str, Any]]) -> dict[str, int]:
    values = [float(row["r"]) for row in _closed(rows)]
    return {"winners_ge_2r": sum(value >= 2 for value in values), "winners_ge_3r": sum(value >= 3 for value in values), "winners_ge_5r": sum(value >= 5 for value in values)}


def tail_baseline(rows: Sequence[Mapping[str, Any]]) -> dict[str, Any]:
    """Tail description, with the same deterministic tie ordering as removal."""
    closed_rows = _closed(rows)
    winners = sorted((row for row in closed_rows if float(row["r"]) > 0), key=lambda row: (-float(row["r"]), int(row["exit_timestamp"]), str(row["trade_id"])))
    positive_r = sum(float(row["r"]) for row in winners)
    net_r = sum(float(row["r"]) for row in closed_rows)
    def top(count: int) -> dict[str, Any]:
        values = [float(row["r"]) for row in winners[:count]]
        total = sum(values)
        return {"r": values, "r_sum": float(total), "positive_r_share": None if positive_r == 0 else total / positive_r, "net_r_share": None if net_r == 0 else total / net_r}
    return {**winner_counts(closed_rows), "maximum_winner_r": None if not winners else float(winners[0]["r"]), "top_5": top(5), "top_10": top(10)}


def chronological_months(rows: Iterable[Mapping[str, Any]], *, timestamp_key: str = "entry_timestamp") -> dict[str, list[Mapping[str, Any]]]:
    result: dict[str, list[Mapping[str, Any]]] = defaultdict(list)
    for row in rows:
        result[utc_date(int(row[timestamp_key]))[:7]].append(row)
    return {month: result[month] for month in sorted(result)}


def monthly_economics(rows: Iterable[Mapping[str, Any]], *, timestamp_key: str = "entry_timestamp") -> dict[str, dict[str, Any]]:
    return {month: {**economics(items), **winner_counts(items), "opened_trades": len(items), "censored_trades": sum(row["outcome"] != "closed" for row in items)} for month, items in chronological_months(rows, timestamp_key=timestamp_key).items()}


def leave_one_month_out(rows: Sequence[Mapping[str, Any]]) -> dict[str, dict[str, Any]]:
    groups = chronological_months(rows)
    return {month: {**economics([row for row in rows if row not in members]), **winner_counts([row for row in rows if row not in members])} for month, members in groups.items()}


def tail_removal(rows: Sequence[Mapping[str, Any]], removals: Sequence[int] = (1, 3, 5, 10)) -> dict[str, dict[str, Any]]:
    """Remove positive winners by R descending, then stable chronological/trade-id tie order."""
    closed_rows = _closed(rows)
    winners = sorted((row for row in closed_rows if float(row["r"]) > 0), key=lambda row: (-float(row["r"]), int(row["exit_timestamp"]), str(row["trade_id"])))
    positive_r, total_r = sum(float(row["r"]) for row in winners), sum(float(row["r"]) for row in closed_rows)
    output: dict[str, dict[str, Any]] = {}
    for n in removals:
        removed = winners[:n]
        remaining_ids = {str(row["trade_id"]) for row in removed}
        report = economics([row for row in closed_rows if str(row["trade_id"]) not in remaining_ids])
        removed_r = sum(float(row["r"]) for row in removed)
        report.update({"requested_removal_count": n, "actual_removal_count": len(removed), "removed_trade_ids": [str(row["trade_id"]) for row in removed], "removed_r": float(removed_r), "removed_positive_r_share": None if not positive_r else removed_r / positive_r, "removed_net_r_share": None if total_r == 0 else removed_r / total_r})
        output[str(n)] = report
    return output


def friction_stress(rows: Sequence[Mapping[str, Any]], costs: Sequence[float] = PREDECLARED_COSTS_R) -> dict[str, dict[str, Any]]:
    baseline_total = economics(rows)["total_r"]
    output: dict[str, dict[str, Any]] = {}
    for cost in costs:
        result = economics(rows, cost_r=cost)
        result["effective_round_trip_cost_r"] = cost
        result["baseline_net_r_retained"] = None if baseline_total == 0 else result["total_r"] / baseline_total
        output[f"{cost:.2f}"] = result
    return output


def breakeven_cost_r(rows: Iterable[Mapping[str, Any]]) -> float | None:
    values = [float(row["r"]) for row in _closed(rows)]
    return None if not values else float(sum(values) / len(values))


def path_risk(rows: Sequence[Mapping[str, Any]], windows: Sequence[int] = PREDECLARED_ROLLING_WINDOWS) -> dict[str, Any]:
    items = sorted(_closed(rows), key=lambda row: (int(row["exit_timestamp"]), str(row["trade_id"])))
    equity, peak, max_drawdown, current_peak_index = 0.0, 0.0, 0.0, 0
    max_drawdown_peak_index, trough_index = 0, 0
    curve = [{"trade_count": 0, "close_timestamp": None, "cumulative_r": 0.0}]
    for index, row in enumerate(items, 1):
        equity += float(row["r"]); curve.append({"trade_count": index, "close_timestamp": int(row["exit_timestamp"]), "cumulative_r": float(equity)})
        if equity > peak:
            peak, current_peak_index = equity, index
        drawdown = peak - equity
        if drawdown > max_drawdown:
            max_drawdown, max_drawdown_peak_index, trough_index = drawdown, current_peak_index, index
    peak_value = curve[max_drawdown_peak_index]["cumulative_r"]
    recovery_index = next((i for i in range(trough_index + 1, len(curve)) if curve[i]["cumulative_r"] >= peak_value), None)
    longest, current = 0, 0
    for row in items:
        current = current + 1 if float(row["r"]) < 0 else 0; longest = max(longest, current)
    rolling: dict[str, Any] = {}
    values = [float(row["r"]) for row in items]
    for window in windows:
        sequences = [(sum(values[i:i + window]), i, i + window) for i in range(max(0, len(values) - window + 1))]
        worst = min(sequences, default=None)
        rolling[str(window)] = {"window": window, "overlapping_non_independent": True, "available": len(values) >= window, "worst_total_r": None if worst is None else float(worst[0]), "start_trade_ordinal": None if worst is None else worst[1] + 1, "end_trade_ordinal": None if worst is None else worst[2]}
    peak_timestamp, trough_timestamp = curve[max_drawdown_peak_index]["close_timestamp"], curve[trough_index]["close_timestamp"]
    recovery_timestamp = None if recovery_index is None else curve[recovery_index]["close_timestamp"]
    return {"chronological_close_order": "exit_timestamp_then_trade_id", "drawdown_tie_policy": "strictly larger drawdowns only; equal peaks retain the earliest peak and equal drawdowns retain the earliest trough", "initial_equity_r": 0.0, "cumulative_curve": curve, "maximum_peak_to_trough_drawdown_r": float(max_drawdown), "peak_trade_ordinal": max_drawdown_peak_index, "peak_timestamp": peak_timestamp, "peak_cumulative_r": peak_value, "trough_trade_ordinal": trough_index, "trough_timestamp": trough_timestamp, "recovery_trade_ordinal": recovery_index, "recovery_timestamp": recovery_timestamp, "recovery_trades_after_trough": None if recovery_index is None else recovery_index - trough_index, "recovery_trades_after_peak": None if recovery_index is None else recovery_index - max_drawdown_peak_index, "recovery_elapsed_ms_from_trough": None if recovery_timestamp is None or trough_timestamp is None else recovery_timestamp - trough_timestamp, "recovery_elapsed_ms_from_peak": None if recovery_timestamp is None or peak_timestamp is None else recovery_timestamp - peak_timestamp, "longest_losing_streak_trades": longest, "rolling_windows": rolling}


def _record_trade_close(close_events: dict[str, tuple[int, Any]], *, index: int, event: Any) -> None:
    if event.trade_id in close_events:
        raise ValueError("duplicate trade close")
    close_events[event.trade_id] = (index, event)


def _ledger(source_15m: Path, source_4h: Path, declared_symbol: str) -> tuple[list[dict[str, Any]], dict[int, dict[str, Any]]]:
    raw15, raw4 = source_15m.read_bytes(), source_4h.read_bytes()
    ltf = parse_visible_tradingview_export(raw15, declared_symbol=declared_symbol, timeframe=Timeframe.MINUTES_15)
    htf = parse_visible_tradingview_export(raw4, declared_symbol=declared_symbol, timeframe=Timeframe.HOURS_4)
    replay_htf = tuple(bar for bar in htf if bar.finalized_at <= ltf[-1].finalized_at)
    replay = ReplayEngine(ReplayConfig(), StrategyConfig()).run(_merge(ltf, replay_htf))
    backtest = BacktestEngine(BacktestConfig()).run(replay)
    ltf_traces = [trace for trace in replay.traces if trace.source_bar.timeframe is Timeframe.MINUTES_15]
    setups: dict[int, dict[str, Any]] = {}; armed: dict[Direction, int] = {}; entries: dict[str, dict[str, Any]] = {}; close_events: dict[str, tuple[int, Any]] = {}
    for index, trace in enumerate(ltf_traces):
        for event in trace.events:
            if event.type is EventType.HEMA_FLIP_DETECTED and event.side is not None:
                setups[event.timestamp] = {"direction": event.side.value, "path": "rejected", "timestamp": event.timestamp}
            elif event.type is EventType.SETUP_ARMED and event.side is not None:
                origin = trace.post_state.pending_flip_timestamp
                if origin is None: raise ValueError("armed setup lacks flip origin")
                setups[origin]["path"] = "armed_then_cancelled"; armed[event.side] = origin
            elif event.type is EventType.SETUP_CANCELLED and event.side is not None:
                origin = armed.pop(event.side, None)
                if origin is not None: setups[origin]["path"] = "armed_then_cancelled"
            elif event.type is EventType.TRADE_OPENED:
                trade = trace.post_state.trade
                if trade is None: raise ValueError("opened trade missing state")
                path = "armed_then_opened" if setups.get(trade.setup_origin_timestamp, {}).get("path") == "armed_then_cancelled" else "immediate_open"
                setups.setdefault(trade.setup_origin_timestamp, {"direction": trade.side.value, "timestamp": trade.setup_origin_timestamp})["path"] = path
                entries[trade.trade_id] = {"trade": trade, "index": index, "path": path, "entry_timestamp": event.timestamp}
            elif event.type is EventType.TRADE_CLOSED:
                _record_trade_close(close_events, index=index, event=event)
    closed = {trade.trade_id: trade for trade in backtest.closed_trades}
    if set(close_events) != set(closed): raise ValueError("replay/backtest closed IDs disagree")
    adr = adr_contexts(ltf); rows: list[dict[str, Any]] = []
    for trade_id, info in entries.items():
        trade, start = info["trade"], info["index"]
        row: dict[str, Any] = {"trade_id": trade_id, "direction": trade.side.value, "path": info["path"], "setup_origin_timestamp": trade.setup_origin_timestamp, "entry_timestamp": info["entry_timestamp"], "entry_price": trade.entry_price, "stop_price": trade.stop_price, "risk_per_unit": abs(trade.entry_price-trade.stop_price), "outcome": "censored", "r": None, "stop_hit": None, "mae_r": None, "mfe_r": None, "strict_pre_exit_mfe_r": None, "duration_ms": None, "exit_reasons": [], "exit_timestamp": None, "post_entry_gap": None}
        if trade_id in closed:
            end, event = close_events[trade_id]; seq = [t.source_bar for t in ltf_traces[start + 1:end + 1]]
            expected = (ltf_traces[end].source_bar.open_time-ltf_traces[start].source_bar.open_time)//BAR_MS
            contiguous = len(seq) == expected and all(bar.open_time == ltf_traces[start].source_bar.open_time+(i+1)*BAR_MS for i, bar in enumerate(seq))
            risk = row["risk_per_unit"]; mae_r = mfe_r = strict_pre_exit_mfe_r = None
            if contiguous:
                low, high = min((bar.low for bar in seq), default=trade.entry_price), max((bar.high for bar in seq), default=trade.entry_price)
                mae = max(0., trade.entry_price-low) if trade.side is Direction.LONG else max(0., high-trade.entry_price)
                mfe = max(0., high-trade.entry_price) if trade.side is Direction.LONG else max(0., trade.entry_price-low)
                mae_r, mfe_r = mae/risk, mfe/risk
                pre_exit_low = min((bar.low for bar in seq[:-1]), default=trade.entry_price)
                pre_exit_high = max((bar.high for bar in seq[:-1]), default=trade.entry_price)
                strict_pre_exit_mfe = max(0., pre_exit_high-trade.entry_price) if trade.side is Direction.LONG else max(0., trade.entry_price-pre_exit_low)
                strict_pre_exit_mfe_r = strict_pre_exit_mfe/risk
            closed_trade = closed[trade_id]; reasons = [reason.value for reason in event.reasons]
            row.update({"outcome":"closed", "r":closed_trade.net_pnl/(risk*closed_trade.quantity), "stop_hit":"exit_stop" in reasons, "mae_r":mae_r, "mfe_r":mfe_r, "strict_pre_exit_mfe_r":strict_pre_exit_mfe_r, "duration_ms":closed_trade.exit_timestamp-info["entry_timestamp"], "exit_reasons":reasons, "exit_timestamp":closed_trade.exit_timestamp, "exit_price":closed_trade.canonical_exit_price, "post_entry_gap":not contiguous})
        row["adr_status"] = adr[utc_date(ltf_traces[start].source_bar.open_time)].status.value; rows.append(row)
    return rows, setups


def _distribution(rows: Sequence[Mapping[str, Any]], setups: Mapping[int, Mapping[str, Any]]) -> dict[str, Any]:
    setup_groups: dict[str, list[Mapping[str, Any]]] = defaultdict(list)
    for setup in setups.values(): setup_groups[utc_date(int(setup["timestamp"]))[:7]].append(setup)
    trade_groups = chronological_months(rows)
    setup_origin: dict[str, Any] = {}
    for month, items in sorted(setup_groups.items()):
        eligible = [item for item in items if item.get("path") in {"immediate_open", "armed_then_opened", "armed_then_cancelled"}]
        setup_origin[month] = {"observed_setups": len(items), "eligible_setups": len(eligible), "path_counts": {path: sum(item.get("path") == path for item in items) for path in ("rejected", "immediate_open", "armed_then_opened", "armed_then_cancelled")}}
    trade_entry: dict[str, Any] = {}
    for month, opened in sorted(trade_groups.items()):
        closed = _closed(opened)
        directions = {side: {"opened_trades": sum(item["direction"] == side for item in opened), "closed_trades": sum(item["direction"] == side for item in closed), "censored_trades": sum(item["direction"] == side and item["outcome"] != "closed" for item in opened)} for side in ("long", "short")}
        trade_entry[month] = {"opened_trades": len(opened), "closed_trades": len(closed), "censored_trades": len(opened)-len(closed), "direction": directions, "exit_counts": {"stop_related": sum(bool(item["stop_hit"]) for item in closed), "strategy_only": sum(not bool(item["stop_hit"]) for item in closed)}, "tail": winner_counts(closed)}
    cross_month = sum(utc_date(int(row["setup_origin_timestamp"]))[:7] != utc_date(int(row["entry_timestamp"]))[:7] for row in rows)
    if sum(item["observed_setups"] for item in setup_origin.values()) != len(setups): raise ValueError("setup-origin distribution does not reconcile observed setups")
    if sum(item["eligible_setups"] for item in setup_origin.values()) != sum(item.get("path") in {"immediate_open", "armed_then_opened", "armed_then_cancelled"} for item in setups.values()): raise ValueError("setup-origin distribution does not reconcile eligible setups")
    if sum(item["opened_trades"] for item in trade_entry.values()) != len(rows) or sum(item["closed_trades"] for item in trade_entry.values()) != len(_closed(rows)): raise ValueError("entry-month distribution does not reconcile trade population")
    return {"setup_origin_month": {"convention": "setup_origin_finalized_at", "months": setup_origin}, "trade_entry_month": {"convention": "entry_finalized_at", "months": trade_entry}, "cross_month_setup_open_count": cross_month}


def _segment(rows: Sequence[Mapping[str, Any]], key: str) -> dict[str, Any]:
    values = sorted({str(row[key]) for row in rows})
    return {value: {"aggregate": {**economics([row for row in rows if str(row[key]) == value]), **winner_counts([row for row in rows if str(row[key]) == value])}, "monthly": monthly_economics([row for row in rows if str(row[key]) == value])} for value in values}


def _exit_analysis(rows: Sequence[Mapping[str, Any]]) -> dict[str, Any]:
    closed_rows = _closed(rows); stops = [row for row in closed_rows if row["stop_hit"]]; strategy = [row for row in closed_rows if not row["stop_hit"]]
    def mfe_summary(items: Sequence[Mapping[str, Any]], key: str) -> dict[str, Any]:
        values = [float(row[key]) for row in items if row[key] is not None]
        return {"stopped_trade_count": len(items), "observed_mfe_count": len(values), "missing_mfe_data_gap_count": len(items)-len(values), "mean_mfe_r": None if not values else float(mean(values)), "median_mfe_r": None if not values else float(median(values))}
    def stopped_mfe(items: Sequence[Mapping[str, Any]]) -> dict[str, Any]:
        full = mfe_summary(items, "mfe_r")
        strict_pre_exit = mfe_summary(items, "strict_pre_exit_mfe_r")
        comparable = [(float(row["mfe_r"]), float(row["strict_pre_exit_mfe_r"])) for row in items if row["mfe_r"] is not None and row["strict_pre_exit_mfe_r"] is not None]
        differences = [full_value - pre_exit_value for full_value, pre_exit_value in comparable]
        return {
            "full_through_exit_bar": full,
            "strict_pre_exit": strict_pre_exit,
            "full_through_exit_bar_vs_strict_pre_exit": {
                "comparable_observation_count": len(differences),
                "full_strictly_greater_count": sum(difference > 0 for difference in differences),
                "aggregate_full_minus_pre_exit_mfe_r": float(sum(differences)),
                "mean_full_minus_pre_exit_mfe_r": None if not differences else float(mean(differences)),
            },
        }
    def view(items: list[Mapping[str, Any]]) -> dict[str, Any]: return {"count":len(items), **economics(items), **winner_counts(items)}
    by_month = chronological_months(stops)
    by_direction = {side: [row for row in stops if row["direction"] == side] for side in ("long", "short")}
    return {
        "mfe_conventions": {
            "full_through_exit_bar": "Post-entry 15m bars including the exit bar; this preserves the original reported MFE convention.",
            "strict_pre_exit": "Post-entry 15m bars strictly before the exit bar; a conservative diagnostic, not an assertion of actual intrabar MFE.",
        },
        "mfe_intrabar_order_caveat": "OHLC data does not establish intrabar order on the exit bar; strict-pre-exit MFE only excludes that bar for a conservative comparison.",
        "missing_mfe_data_gap_caveat": "Stopped trades with post-entry 15m paths crossing source gaps lack both MFE conventions; summaries use observed values only.",
        "stopped_mfe_missing_data_gap_count": mfe_summary(stops, "mfe_r")["missing_mfe_data_gap_count"],
        "aggregate":{"stop_related":view(stops), "strategy_only":view(strategy), "stopped_mfe":stopped_mfe(stops)},
        "by_entry_month":{"stop_related":monthly_economics(stops), "strategy_only":monthly_economics(strategy), "stopped_mfe":{month:stopped_mfe(items) for month, items in by_month.items()}},
        "by_direction":{"stop_related":_segment(stops,"direction"), "strategy_only":_segment(strategy,"direction"), "stopped_mfe":{side:stopped_mfe(items) for side, items in by_direction.items()}},
    }


def _price_context(rows: Sequence[Mapping[str, Any]]) -> dict[str, Any]:
    closed_rows = _closed(rows)
    entries = [float(row["entry_price"]) for row in closed_rows]
    risks = [float(row["risk_per_unit"]) / float(row["entry_price"]) * 10_000 for row in closed_rows]
    def distribution(values: list[float]) -> dict[str, Any]:
        return {"count": len(values), "minimum": None if not values else min(values), "median": None if not values else float(median(values)), "maximum": None if not values else max(values)}
    return {"status": "DESCRIPTIVE ONLY; NOT A CANONICAL BROKER COST CONVERSION", "entry_price": distribution(entries), "initial_risk_distance_bps_of_entry": distribution(risks)}


def _cumulative_and_expanding(rows: Sequence[Mapping[str, Any]]) -> dict[str, Any]:
    items = sorted(_closed(rows), key=lambda row: (int(row["exit_timestamp"]), str(row["trade_id"])))
    total = 0.0; checkpoints = []
    for index, row in enumerate(items, 1):
        total += float(row["r"])
        if index % 20 == 0 or index == len(items): checkpoints.append({"closed_trade_ordinal":index, "close_timestamp":row["exit_timestamp"], "cumulative_r":float(total), "cumulative_expectancy_r":float(total/index)})
    ordinals = list(range(20, len(items) + 1, 20))
    if items and (not ordinals or ordinals[-1] != len(items)): ordinals.append(len(items))
    return {"close_order": "exit_timestamp_then_trade_id", "cumulative_checkpoints_every_20_trades":checkpoints, "expanding_windows":[{"closed_trade_ordinal":i, **economics(items[:i])} for i in ordinals]}


def _cost_interactions(rows: Sequence[Mapping[str, Any]]) -> dict[str, Any]:
    """Predeclared cost stress only; no threshold selection is performed."""
    months = chronological_months(rows)
    ex_march = [row for row in rows if utc_date(int(row["entry_timestamp"]))[:7] != "2026-03"]
    output: dict[str, Any] = {}
    for cost in PREDECLARED_INTERACTION_COSTS_R:
        monthly = {month: economics(items, cost_r=cost) for month, items in months.items()}
        directions = {side: economics([row for row in rows if row["direction"] == side], cost_r=cost) for side in ("long", "short")}
        output[f"{cost:.2f}"] = {"effective_round_trip_cost_r": cost, "monthly": monthly, "positive_month_count": sum(item["total_r"] > 0 for item in monthly.values()), "first_chronological_negative_month": next((month for month, item in monthly.items() if item["total_r"] < 0), None), "ex_march": economics(ex_march, cost_r=cost), "direction": {side: {**item, "survives_positive_total_r": item["total_r"] > 0} for side, item in directions.items()}}
    return output


def build_xauusd_robustness_report(*, source_15m: Path, source_4h: Path, declared_symbol: str = "XAUUSD", gate_outcome: str = PENDING_SOL_REVIEW) -> dict[str, Any]:
    """Build a deterministic artifact over the exact frozen, frictionless population."""
    if gate_outcome not in ALLOWED_GATE_OUTCOMES: raise ValueError("unrecognized Sol gate outcome")
    baseline = build_market_transfer_baseline(source_15m=source_15m, source_4h=source_4h, declared_symbol=declared_symbol)
    baseline_bytes = (canonical_json(baseline) + "\n").encode("utf-8")
    if sha256(baseline_bytes).hexdigest() != FROZEN_BASELINE_ARTIFACT_SHA256: raise ValueError("frozen baseline artifact SHA does not reconcile")
    rows, setups = _ledger(source_15m, source_4h, declared_symbol)
    closed_rows = _closed(rows)
    if len(setups) != 541 or len(rows) != 202 or len(closed_rows) != 201 or len(rows)-len(closed_rows) != 1: raise ValueError("frozen trade population does not reconcile")
    if not math.isclose(economics(rows)["total_r"], float(baseline["populations"]["total_r"]), abs_tol=1e-12): raise ValueError("ledger headline total R does not reconcile")
    month = monthly_economics(rows); blocks = {"2026-03_to_2026-04": [r for r in rows if utc_date(int(r["entry_timestamp"]))[:7] in {"2026-03","2026-04"}], "2026-05_to_2026-06": [r for r in rows if utc_date(int(r["entry_timestamp"]))[:7] in {"2026-05","2026-06"}], "2026-07_to_2026-08": [r for r in rows if utc_date(int(r["entry_timestamp"]))[:7] in {"2026-07","2026-08"}]}
    for month_name, expected in baseline["chronology"]["months"].items():
        actual = month.get(month_name)
        if actual is None or actual["closed_trades"] != expected["closed_trades"] or not math.isclose(actual["total_r"], expected["total_r"], abs_tol=1e-12):
            raise ValueError("ledger monthly chronology does not reconcile")
    for side in ("long", "short"):
        if not math.isclose(economics([row for row in rows if row["direction"] == side])["total_r"], baseline["direction"][side]["economics"]["total_r"], abs_tol=1e-12):
            raise ValueError("ledger direction does not reconcile")
    for path in ("immediate_open", "armed_then_opened"):
        if not math.isclose(economics([row for row in rows if row["path"] == path])["total_r"], baseline["setup_path"][path]["economics"]["total_r"], abs_tol=1e-12):
            raise ValueError("ledger setup path does not reconcile")
    early, late = sorted(closed_rows, key=lambda r:(int(r["exit_timestamp"]),str(r["trade_id"])))[:100], sorted(closed_rows, key=lambda r:(int(r["exit_timestamp"]),str(r["trade_id"]))) [100:]
    ex_march = [row for row in rows if utc_date(int(row["entry_timestamp"]))[:7] != "2026-03"]
    data_requirement = "same-source/export-method XAUUSD 15m data covering at least 2025-03-01T23:00Z through immediately before current 15m start, with continuous chronology/no repair and sufficient 4H overlap/warmup; preferably more"
    distribution = _distribution(rows, setups)
    if distribution["cross_month_setup_open_count"] != baseline["chronology"]["cross_month_entry_origin_trade_count"]:
        raise ValueError("cross-month setup/open count does not reconcile")
    report = {"schema_version":SCHEMA_VERSION, "gate_outcome":gate_outcome, "gate_authority":"Sol/main only; this artifact does not approve or advance a phase", "canonical_starting_state":{"sha":CANONICAL_STARTING_SHA,"tag":CANONICAL_STARTING_TAG}, "frozen_baseline":{"artifact_sha256":FROZEN_BASELINE_ARTIFACT_SHA256,"reproduced_sha256":sha256(baseline_bytes).hexdigest(),"headline_reconciliation":{"baseline":baseline["populations"],"ledger":economics(rows),"observed_setups":len(setups),"eligible_setups":sum(x.get("path") in {"immediate_open","armed_then_opened","armed_then_cancelled"} for x in setups.values()),"opened_trades":len(rows),"closed_trades":len(closed_rows),"censored_trades":len(rows)-len(closed_rows)}}, "source":{"15m":baseline["source"]["15m"],"4h":baseline["source"]["4h"],"identity_caveat":"SOURCE IDENTITY FROM INTERNAL CSV METADATA: UNVERIFIED; caller/filename declares XAUUSD."}, "configurations":baseline["configurations"],"fingerprints":baseline["fingerprints"],"ordering":baseline["ordering"], "data_sufficiency":{"effective_15m_research_history_calendar_days":baseline["source_coverage"]["overlap_inclusive_calendar_days"],"distribution_by_entry_month":_distribution(rows,setups),"assessment":"Approximately 178 calendar days / 201 closed trades supports descriptive robustness diagnostics but not a defensible untouched OOS conclusion.","additional_historical_xau_15m_data_required":"YES","exact_requirement":data_requirement}, "oos_design":{"status":"NO DEFENSIBLE UNTOUCHED OOS EXISTS WITHIN CURRENT DATA; ADDITIONAL DATA REQUIRED.","current_sample_outcome_inspected":"2026-03-01 through 2026-08-25 monthly outcomes are disclosed in the brief and must not be relabeled untouched OOS.","historical_requirement":data_requirement,"forward_requirement":"A truly forward untouched OOS requires prospectively collected data strictly after 2026-08-25T17:30Z, with split locked before inspection.","provider_caveat":"Source/provider substitutions are a comparability problem."}, "closed_trade_ledger":sorted(rows,key=lambda r:(int(r["entry_timestamp"]),str(r["trade_id"]))), "chronology":{"monthly":month,"cumulative_and_expanding":_cumulative_and_expanding(rows),"rolling_closed_trade_windows":path_risk(rows)["rolling_windows"],"early_half_first_100":economics(early),"late_half_last_101":economics(late),"consecutive_two_month_blocks":{name:economics(items) for name,items in blocks.items()},"march_share_of_net_r":economics([r for r in rows if utc_date(int(r["entry_timestamp"]))[:7]=="2026-03"])["total_r"]/economics(rows)["total_r"],"ex_march":{**economics(ex_march),**winner_counts(ex_march)},"leave_one_month_out":leave_one_month_out(rows)}, "direction":_segment(rows,"direction"),"setup_path":_segment(rows,"path"),"tail_dependence":{"ordering":"R descending, then chronological close timestamp, then trade ID for ties", "baseline":winner_counts(rows),"removal":tail_removal(rows)},"stop_giveback":_exit_analysis(rows),"friction":{"abstraction":"combined normalized effective round-trip R cost; spread, slippage, and commissions cannot be separated under this abstraction","real_broker_cost_conversion":"NOT ESTABLISHED","cost_levels_r":list(PREDECLARED_COSTS_R),"aggregate":friction_stress(rows),"breakeven_effective_cost_per_closed_trade_r":breakeven_cost_r(rows),"breakeven_by_segment":{"long":breakeven_cost_r([r for r in rows if r["direction"]=="long"]),"short":breakeven_cost_r([r for r in rows if r["direction"]=="short"]),"immediate_open":breakeven_cost_r([r for r in rows if r["path"]=="immediate_open"]),"armed_then_opened":breakeven_cost_r([r for r in rows if r["path"]=="armed_then_opened"])},"price_context":"Entry price and initial stop are retained in the ledger; descriptive price/bps conversions are not canonical broker costs.","cost_by_month":{f"{cost:.2f}":{month:economics(items,cost_r=cost) for month,items in chronological_months(rows).items()} for cost in PREDECLARED_INTERACTION_COSTS_R},"cost_by_direction":{f"{cost:.2f}":{side:economics([r for r in rows if r["direction"]==side],cost_r=cost) for side in ("long","short")} for cost in PREDECLARED_INTERACTION_COSTS_R}},"path_risk":path_risk(rows),"caveats":["Friction is diagnostic and does not alter the frozen trade population.","Rolling 20 and 50 closed-trade windows overlap and are non-independent.","No IID/bootstrap diagnostic is included because chronological dependence/regime clustering is not assumed away.","Full canonical internal Pine parity remains UNVERIFIED because exports omit internal fields."]}
    report["frozen_baseline"].update({"direction_reconciliation": baseline["direction"], "setup_path_reconciliation": baseline["setup_path"], "entry_month_reconciliation": baseline["chronology"]["months"]})
    del report["data_sufficiency"]["distribution_by_entry_month"]
    report["data_sufficiency"]["monthly_population_distribution"] = distribution
    report["parity_scope"] = {"visible_plot_pine_python": {**baseline["parity_scope"]["visible_plot_pine_python"], "stage_0_expected_contract": "External Stage-0 test evidence is required; this artifact builder does not run that test."}, "full_canonical_internal_pine": baseline["parity_scope"]["full_canonical_internal_pine"]}
    report["tail_dependence"]["baseline"] = tail_baseline(rows)
    report["friction"]["cost_interactions"] = _cost_interactions(rows)
    report["friction"]["price_context"] = _price_context(rows)
    return report


def xauusd_robustness_json(report: Mapping[str, Any]) -> bytes:
    return json.dumps(report, sort_keys=True, separators=(",", ":"), allow_nan=False).encode("utf-8") + b"\n"


def write_xauusd_robustness_report(report: Mapping[str, Any], path: Path, *, overwrite: bool = False) -> None:
    if path.exists() and not overwrite: raise FileExistsError(f"refusing to overwrite {path}; pass --overwrite")
    path.parent.mkdir(parents=True, exist_ok=True); path.write_bytes(xauusd_robustness_json(report))
