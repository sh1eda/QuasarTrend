"""Read-only XM GOLD / TradingView XAUUSD compatibility research.

This is deliberately an additive research boundary.  It does not alter the
replay, indicator, strategy, or accounting implementations.  XM M1 history is
audited as acquired and only the predeclared March--August 2026 comparison
window is aggregated or supplied to the frozen engines.
"""
from __future__ import annotations

import csv
from collections import Counter, defaultdict
from dataclasses import dataclass
from datetime import UTC, datetime
from hashlib import sha256
import math
from pathlib import Path
from statistics import mean, median
from typing import Any, Iterable, Mapping, Sequence
from zoneinfo import ZoneInfo

from quasartrend.backtest import BacktestConfig, BacktestEngine
from quasartrend.indicators import Candle, HemaTrend
from quasartrend.replay import HistoricalBar, ReplayConfig, ReplayEngine, Timeframe
from quasartrend.strategy import Direction, EventType, StrategyConfig

from .market_transfer import build_market_transfer_baseline, parse_visible_tradingview_export
from .provenance import canonical_json, fingerprint, source_fingerprint
from .xau_robustness import path_risk


SCHEMA_VERSION = "xm-gold-tradingview-compatibility/v1"
XM_RAW_SCHEMA = (
    "time_epoch", "time_utc", "open", "high", "low", "close",
    "tick_volume", "spread", "real_volume",
)
XM_SYMBOL = "GOLD"
TRADINGVIEW_SYMBOL = "XAUUSD"
XM_SERVER_TIMEZONE = "Europe/Nicosia"
M1_MS = 60_000
COMPARISON_START_UTC = "2026-03-01T23:00:00Z"
COMPARISON_END_UTC = "2026-08-25T17:30:00Z"
COMPARISON_START_MS = 1_772_406_000_000
COMPARISON_END_MS = 1_787_679_000_000
SETUP_TOLERANCE_MS = Timeframe.MINUTES_15.duration_ms
EXPECTED_XM_RAW_SHA256 = "3817558149502888bcee711fb803e6085c5d48cbf36d33901d85ac85c0c0d81a"
EXPECTED_XM_RAW_ROWS = 1_000_000
EXPECTED_TRADINGVIEW_15M_SHA256 = "dc3d17a1d7c23b6e69659520b5a5826a11c0c2672aa77987231f54c46a4dd3cd"
EXPECTED_TRADINGVIEW_4H_SHA256 = "e9e5330a9f872d2e7e08bcaa2e14e9feaf0f59d833c423a5f2dc3459e29fed25"
FROZEN_XAU_BASELINE_SHA256 = "e84d976a57dac2aed300a17c1a9b472e47143127031bf495c345bc67993ecd6f"


@dataclass(frozen=True, slots=True)
class XmM1Bar:
    """One unmodified MT5 M1 record, with an explicitly verified UTC stamp."""

    open_time: int
    open: float
    high: float
    low: float
    close: float
    tick_volume: float
    spread_points: float
    real_volume: float
    raw_open_time: int | None = None


@dataclass(frozen=True, slots=True)
class AggregatedBar:
    """A clock-aligned aggregation, retained even when its source is incomplete."""

    bar: HistoricalBar
    source_count: int
    source_contiguous: bool
    complete: bool
    actual_finalized_at: int


@dataclass(frozen=True, slots=True)
class Match:
    reference_index: int
    candidate_index: int
    delta_ms: int


def _utc(timestamp_ms: int) -> str:
    return datetime.fromtimestamp(timestamp_ms / 1000, UTC).isoformat().replace("+00:00", "Z")


def _verified_source_manifest(
    *, xm_path: Path, xm_audit: Mapping[str, Any], tradingview_15m_path: Path,
    tradingview_15m_raw: bytes, tradingview_4h_path: Path, tradingview_4h_raw: bytes,
) -> dict[str, Any]:
    """Record fixed acquisition identities and fail closed on any byte mismatch."""
    xm_sha = str(xm_audit["raw_sha256"])
    tv_15m_sha, tv_4h_sha = sha256(tradingview_15m_raw).hexdigest(), sha256(tradingview_4h_raw).hexdigest()
    if xm_sha != EXPECTED_XM_RAW_SHA256 or int(xm_audit["row_count"]) != EXPECTED_XM_RAW_ROWS:
        raise ValueError("XM source identity mismatch")
    if tv_15m_sha != EXPECTED_TRADINGVIEW_15M_SHA256:
        raise ValueError("TradingView XAUUSD 15m source identity mismatch")
    if tv_4h_sha != EXPECTED_TRADINGVIEW_4H_SHA256:
        raise ValueError("TradingView XAUUSD 4h source identity mismatch")
    return {
        "xm": {"provider": "XM", "server": "XMGlobal-MT5 18", "symbol": "GOLD",
               "path_identity": r"Derivatives\Spot Metals\GOLD", "digits": 2, "point": 0.01,
               "raw_path_label": str(xm_path), "raw_sha256": xm_sha, "raw_sha256_verified": True,
               "row_count": int(xm_audit["row_count"])},
        "tradingview_xauusd": {"symbol": "XAUUSD", "15m": {"raw_path_label": str(tradingview_15m_path),
                                  "raw_sha256": tv_15m_sha, "raw_sha256_verified": True},
                               "4h": {"raw_path_label": str(tradingview_4h_path), "raw_sha256": tv_4h_sha,
                                      "raw_sha256_verified": True}},
    }


def _raw_open_time(row: XmM1Bar) -> int:
    return row.open_time if row.raw_open_time is None else row.raw_open_time


def _normalize_server_wall_clock(raw_timestamp_ms: int) -> int:
    """Convert a mislabeled +00 server wall-clock value through IANA rules.

    The source's epoch and textual +00 field remain immutable provenance.  They
    are not treated as true UTC because XM documents its server clock as Cyprus
    time.  Reject local wall times that IANA identifies as ambiguous/nonexistent
    rather than guessing an offset.
    """
    naive = datetime.fromtimestamp(raw_timestamp_ms / 1000, UTC).replace(tzinfo=None)
    zone = ZoneInfo(XM_SERVER_TIMEZONE)
    candidates: dict[int, datetime] = {}
    for fold in (0, 1):
        local = naive.replace(tzinfo=zone, fold=fold)
        restored = local.astimezone(UTC).astimezone(zone)
        if restored.replace(tzinfo=None) == naive:
            converted = int(local.astimezone(UTC).timestamp() * 1000)
            candidates[converted] = local
    if not candidates:
        raise ValueError("XM server-wall-clock timestamp is nonexistent in Europe/Nicosia")
    if len(candidates) != 1:
        raise ValueError("XM server-wall-clock timestamp is ambiguous in Europe/Nicosia")
    return next(iter(candidates))


def _parse_row(row: Sequence[str], number: int) -> XmM1Bar:
    if len(row) != len(XM_RAW_SCHEMA):
        raise ValueError(f"XM CSV row {number} has wrong column count")
    try:
        epoch_seconds = int(row[0])
        parsed_time = datetime.fromisoformat(row[1])
        prices = tuple(float(value) for value in row[2:6])
        tick_volume, spread, real_volume = (float(value) for value in row[6:9])
    except (TypeError, ValueError) as error:
        raise ValueError(f"XM CSV row {number} has invalid values") from error
    if parsed_time.tzinfo is None or parsed_time.utcoffset() != UTC.utcoffset(None):
        raise ValueError(f"XM CSV row {number} time_utc is not UTC")
    parsed_epoch = int(parsed_time.timestamp())
    if parsed_epoch != epoch_seconds:
        raise ValueError(f"XM CSV row {number} epoch and UTC timestamp disagree")
    values = (*prices, tick_volume, spread, real_volume)
    if any(value != value or value in (float("inf"), float("-inf")) for value in values):
        raise ValueError(f"XM CSV row {number} contains non-finite numeric data")
    opening, high, low, close = prices
    if low > min(opening, close) or high < max(opening, close) or high < low:
        raise ValueError(f"XM CSV row {number} has invalid OHLC geometry")
    if tick_volume < 0 or spread < 0 or real_volume < 0:
        raise ValueError(f"XM CSV row {number} has negative volume or spread")
    raw_open_time = epoch_seconds * 1000
    return XmM1Bar(_normalize_server_wall_clock(raw_open_time), opening, high, low, close, tick_volume, spread, real_volume, raw_open_time)


def parse_xm_m1_csv(raw_input: bytes) -> tuple[XmM1Bar, ...]:
    """Strict small-input parser used by tests; it never fills or shifts bars."""
    try:
        rows = list(csv.reader(raw_input.decode("utf-8-sig").splitlines()))
    except UnicodeDecodeError as error:
        raise ValueError("XM CSV must be UTF-8-sig") from error
    if not rows or tuple(rows[0]) != XM_RAW_SCHEMA:
        raise ValueError("XM CSV schema must exactly match the acquired MT5 export")
    result = tuple(_parse_row(row, number) for number, row in enumerate(rows[1:], 2))
    if not result:
        raise ValueError("XM CSV must contain at least one data row")
    if any(_raw_open_time(right) <= _raw_open_time(left) for left, right in zip(result, result[1:])):
        raise ValueError("XM timestamps must be strictly increasing and unique")
    return result


def _percentile(values: Sequence[float], quantile: float) -> float | None:
    if not values:
        return None
    ordered = sorted(values)
    position = (len(ordered) - 1) * quantile
    lower, upper = int(position), min(int(position) + 1, len(ordered) - 1)
    return float(ordered[lower] + (ordered[upper] - ordered[lower]) * (position - lower))


def _summary(values: Sequence[float]) -> dict[str, float | None]:
    return {
        "minimum": None if not values else float(min(values)),
        "median": None if not values else float(median(values)),
        "mean": None if not values else float(mean(values)),
        "p90": _percentile(values, .90), "p95": _percentile(values, .95),
        "p99": _percentile(values, .99), "maximum": None if not values else float(max(values)),
    }


def audit_xm_m1(
    path: Path, *, comparison_start_ms: int = COMPARISON_START_MS,
    comparison_end_ms: int = COMPARISON_END_MS,
) -> tuple[dict[str, Any], tuple[XmM1Bar, ...]]:
    """Stream the immutable raw export and select only M1 needed for comparison.

    ``comparison_end_ms`` is the final *M15 open*; source minutes through the
    end of that candle are selected, but no pre-March source minute is selected
    for aggregation, indicators, setups, trade, or economics work.
    """
    if not path.is_file():
        raise ValueError("XM raw source must be an existing file")
    if comparison_start_ms < COMPARISON_START_MS:
        raise ValueError("pre-March XM rows are prohibited from strategy-analysis selection")
    if comparison_end_ms < comparison_start_ms:
        raise ValueError("comparison end must not precede comparison start")
    selected: list[XmM1Bar] = []
    previous: XmM1Bar | None = None
    gaps: list[dict[str, int]] = []
    spread_values: list[float] = []
    tick_values: list[float] = []
    tick_missing = real_missing = 0
    real_nonzero = 0
    row_count = 0
    first = last = None
    end_exclusive = comparison_end_ms + Timeframe.MINUTES_15.duration_ms
    with path.open("r", encoding="utf-8-sig", newline="") as handle:
        reader = csv.reader(handle)
        try:
            header = tuple(next(reader))
        except StopIteration as error:
            raise ValueError("XM CSV is empty") from error
        if header != XM_RAW_SCHEMA:
            raise ValueError("XM CSV schema must exactly match the acquired MT5 export")
        for number, row in enumerate(reader, 2):
            item = _parse_row(row, number)
            if previous is not None and _raw_open_time(item) <= _raw_open_time(previous):
                raise ValueError("XM timestamps must be strictly increasing and unique")
            if previous is not None and _raw_open_time(item) - _raw_open_time(previous) != M1_MS:
                gaps.append({"after_raw_labeled_open_time": _raw_open_time(previous), "before_raw_labeled_open_time": _raw_open_time(item), "duration_ms": _raw_open_time(item) - _raw_open_time(previous)})
            previous = item
            row_count += 1
            first = item if first is None else first
            last = item
            spread_values.append(item.spread_points)
            tick_values.append(item.tick_volume)
            tick_missing += int(item.tick_volume != item.tick_volume)
            real_missing += int(item.real_volume != item.real_volume)
            real_nonzero += int(item.real_volume != 0)
            if comparison_start_ms <= item.open_time < end_exclusive:
                selected.append(item)
    if first is None or last is None:
        raise ValueError("XM CSV must contain at least one data row")
    # A two-minute missing print is evidence of a short irregular source gap,
    # not evidence of a daily broker maintenance closure.
    daily = [gap for gap in gaps if 30 * 60 * 1000 <= gap["duration_ms"] < 6 * 60 * 60 * 1000]
    session = [gap for gap in gaps if gap["duration_ms"] >= 6 * 60 * 60 * 1000]
    raw_sha256 = sha256(path.read_bytes()).hexdigest()
    report = {
        "raw_sha256": raw_sha256, "row_count": row_count,
        "expected_identity": {"sha256": EXPECTED_XM_RAW_SHA256, "row_count": EXPECTED_XM_RAW_ROWS,
                              "sha256_matches": raw_sha256 == EXPECTED_XM_RAW_SHA256,
                              "row_count_matches": row_count == EXPECTED_XM_RAW_ROWS},
        "raw_labeled_first_utc": _utc(_raw_open_time(first)), "raw_labeled_last_utc": _utc(_raw_open_time(last)),
        "normalized_first_utc": _utc(first.open_time), "normalized_last_utc": _utc(last.open_time),
        "schema": list(XM_RAW_SCHEMA), "monotonicity": "strictly_increasing",
        "duplicate_timestamps": 0, "unique_timestamps": row_count,
        "malformed_timestamps": 0, "nan_ohlc": 0, "invalid_ohlc_geometry": 0,
        "one_minute_grid": {"cadence_ms": M1_MS, "gap_count": len(gaps),
                            "exact_one_minute_transitions": max(0, row_count - 1 - len(gaps))},
        "observed_source_gaps": gaps,
        "gap_evidence": {"delta_frequency_ms": dict(sorted(Counter(gap["duration_ms"] for gap in gaps).items())),
                         "short_irregular_gap_count": sum(M1_MS < gap["duration_ms"] < 30 * 60 * 1000 for gap in gaps),
                         "daily_maintenance_candidate_count": len(daily), "daily_maintenance_definition": "30 minutes through less than 6 hours; shorter irregular gaps excluded",
                         "session_or_weekend_gap_count": len(session), "session_or_weekend_definition": "6 hours or longer"},
        "daily_maintenance_gap_count": len(daily), "session_or_weekend_gap_count": len(session),
        "spread_points": {**_summary(spread_values), "missing_count": 0},
        "tick_volume": {**_summary(tick_values), "missing_count": tick_missing},
        "real_volume": {"missing_count": real_missing, "nonzero_count": real_nonzero,
                        "all_zero": real_nonzero == 0},
        "terminal_history_ceiling_suspected": row_count == EXPECTED_XM_RAW_ROWS,
        "time_semantics": {"epoch_time_utc_equality_verified_per_row": True,
                           "raw_plus00_label_interpreted_as": "XM server wall clock, not true UTC",
                           "server_timezone": XM_SERVER_TIMEZONE,
                           "manual_timezone_shift_applied": False,
                           "documented_server_timezone_conversion_applied": True},
        "comparison_source_selection": {"start_utc": _utc(comparison_start_ms),
            "end_exclusive_utc": _utc(end_exclusive), "selected_m1_rows": len(selected),
            "pre_march_strategy_data_selected": False},
    }
    return report, tuple(selected)


def aggregate_m1(
    rows: Sequence[XmM1Bar], *, timeframe: Timeframe, symbol: str = XM_SYMBOL,
) -> tuple[AggregatedBar, ...]:
    """Aggregate on native XM server-clock boundaries and retain incomplete buckets."""
    if not rows:
        return ()
    if any(_raw_open_time(right) <= _raw_open_time(left) for left, right in zip(rows, rows[1:])):
        raise ValueError("M1 aggregation requires strict unique chronological input")
    duration = timeframe.duration_ms
    buckets: dict[int, list[XmM1Bar]] = defaultdict(list)
    for row in rows:
        raw_open_time = _raw_open_time(row)
        buckets[raw_open_time - (raw_open_time % duration)].append(row)
    expected = duration // M1_MS
    result: list[AggregatedBar] = []
    for bucket in sorted(buckets):
        values = buckets[bucket]
        contiguous = all(_raw_open_time(right) - _raw_open_time(left) == M1_MS for left, right in zip(values, values[1:]))
        complete = len(values) == expected and contiguous and _raw_open_time(values[0]) == bucket and _raw_open_time(values[-1]) == bucket + duration - M1_MS
        normalized_open, normalized_finalized = _normalize_server_wall_clock(bucket), _normalize_server_wall_clock(bucket + duration)
        result.append(AggregatedBar(
            HistoricalBar(symbol, timeframe, normalized_open, values[0].open,
                          max(row.high for row in values), min(row.low for row in values),
                          values[-1].close, sum(row.tick_volume for row in values)),
            len(values), contiguous, complete, normalized_finalized,
        ))
    return tuple(result)


def _m15_replay_input(rows: Sequence[AggregatedBar]) -> tuple[HistoricalBar, ...]:
    """Every observed non-empty native M15 bucket is a replay input, unchanged."""
    return tuple(item.bar for item in rows)


def _h4_replay_input(
    rows: Sequence[AggregatedBar], *, first_m15_open: int, final_m15_cutoff: int,
) -> tuple[tuple[HistoricalBar, ...], dict[str, int]]:
    """Retain native observed H4 buckets except explicit boundary/DST exclusions."""
    included: list[HistoricalBar] = []
    exclusions = Counter[str]()
    for item in rows:
        if item.bar.open_time < first_m15_open:
            exclusions["before_comparison_start"] += 1
        elif item.actual_finalized_at > final_m15_cutoff:
            exclusions["after_final_m15_cutoff"] += 1
        elif item.actual_finalized_at != item.bar.finalized_at:
            # Replay's immutable HistoricalBar uses fixed elapsed durations.
            exclusions["dst_variable_duration_not_representable"] += 1
        else:
            included.append(item.bar)
    return tuple(included), dict(sorted(exclusions.items()))


def _aggregation_audit(
    rows: Sequence[AggregatedBar], replay_bars: Sequence[HistoricalBar], input_m1_count: int,
    *, exclusions: Mapping[str, int] | None = None,
) -> dict[str, Any]:
    counts = [item.source_count for item in rows]
    source_total = sum(counts)
    included_open_times = {bar.open_time for bar in replay_bars}
    incomplete_included = sum(not item.complete and item.bar.open_time in included_open_times for item in rows)
    return {"generated_count": len(rows), "complete_count": sum(item.complete for item in rows),
            "incomplete_count": sum(not item.complete for item in rows),
            "replay_included_count": len(replay_bars), "incomplete_included_count": incomplete_included,
            "replay_exclusions": dict(exclusions or {}),
            "source_count_minimum": None if not counts else min(counts),
            "source_count_maximum": None if not counts else max(counts),
            "source_count_distribution": dict(sorted(Counter(counts).items())),
            "source_count_sum": source_total, "input_m1_count": input_m1_count,
            "no_gap_fill_proof": {"one_output_bucket_per_observed_server_clock_bucket": True,
                                  "aggregated_source_count_sum_equals_input_m1_count": source_total == input_m1_count,
                                  "synthetic_source_bars_created": 0},
            "dst_variable_elapsed_finalization_count": sum(item.actual_finalized_at != item.bar.finalized_at for item in rows),
            "replay_normalized_sha256": source_fingerprint(tuple(replay_bars))}


def _merge(ltf: Sequence[HistoricalBar], htf: Sequence[HistoricalBar]) -> tuple[HistoricalBar, ...]:
    merged = tuple(sorted((*ltf, *htf), key=lambda bar: bar.processing_key))
    if any(right.processing_key <= left.processing_key for left, right in zip(merged, merged[1:])):
        raise ValueError("replay input must have strict finalization order")
    return merged


def _direction(value: Direction | None) -> str | None:
    return None if value is None else value.value


def _agreement(
    reference: Mapping[int, str | None], candidate: Mapping[int, str | None],
    *, exclude_unavailable: bool = False,
) -> dict[str, Any]:
    common = sorted(set(reference).intersection(candidate))
    pairs = [(reference[key], candidate[key]) for key in common
             if not exclude_unavailable or (reference[key] is not None and candidate[key] is not None)]
    confusion: dict[str, int] = Counter(f"{left or 'none'}->{right or 'none'}" for left, right in pairs)
    equal = sum(left == right for left, right in pairs)
    return {"comparable_count": len(pairs), "agreement_count": equal,
            "agreement_rate": None if not pairs else equal / len(pairs),
            "confusion": dict(sorted(confusion.items()))}


def _slope_direction_at_common_timestamps(
    reference: Mapping[int, float | None], candidate: Mapping[int, float | None],
) -> tuple[dict[int, str | None], dict[int, str | None]]:
    """Prospective descriptive HEMA slope labels, never strategy state.

    Each provider's exact frozen plotted value is compared with its immediately
    preceding *common* comparison timestamp.  Equality is ``flat``; there is
    no tolerance.  A label is unavailable when either required value is not
    finite, and callers exclude it from the agreement denominator.
    """
    common = sorted(set(reference).intersection(candidate))
    left: dict[int, str | None] = {}; right: dict[int, str | None] = {}
    previous_left = previous_right = None
    for timestamp in common:
        current_left, current_right = reference[timestamp], candidate[timestamp]
        def label(current: float | None, previous: float | None) -> str | None:
            if current is None or previous is None:
                return None
            return "up" if current > previous else "down" if current < previous else "flat"
        left[timestamp], right[timestamp] = label(current_left, previous_left), label(current_right, previous_right)
        previous_left, previous_right = current_left, current_right
    return left, right


def _bias_transition_markers(values: Mapping[int, str | None]) -> dict[int, str | None]:
    """Existing frozen H4 bias state transitions, not a newly invented cross."""
    previous: str | None = None
    markers: dict[int, str | None] = {}
    for timestamp in sorted(values):
        current = values[timestamp]
        markers[timestamp] = current if current is not None and previous is not None and current != previous else None
        previous = current
    return markers


def _ledger(ltf: Sequence[HistoricalBar], htf: Sequence[HistoricalBar]) -> dict[str, Any]:
    if not ltf or not htf:
        raise ValueError("compatibility replay requires at least one complete 15m and 4h bar")
    replay = ReplayEngine(ReplayConfig(), StrategyConfig()).run(_merge(ltf, htf))
    backtest = BacktestEngine(BacktestConfig()).run(replay)
    ltf_traces = [trace for trace in replay.traces if trace.source_bar.timeframe is Timeframe.MINUTES_15]
    htf_traces = [trace for trace in replay.traces if trace.source_bar.timeframe is Timeframe.HOURS_4]
    setups: dict[int, dict[str, Any]] = {}
    armed: dict[Direction, int] = {}
    entries: dict[str, dict[str, Any]] = {}
    closes: dict[str, Any] = {}
    for ordinal, trace in enumerate(ltf_traces):
        for event in trace.events:
            if event.type is EventType.HEMA_FLIP_DETECTED and event.side is not None:
                setups[event.timestamp] = {"timestamp": event.timestamp, "direction": event.side.value, "path": "rejected"}
            elif event.type is EventType.SETUP_ARMED and event.side is not None:
                origin = trace.post_state.pending_flip_timestamp
                if origin is None or origin not in setups:
                    raise ValueError("armed setup lacks a known HEMA origin")
                setups[origin]["path"] = "armed_pending"
                armed[event.side] = origin
            elif event.type is EventType.SETUP_CANCELLED and event.side is not None:
                origin = armed.pop(event.side, None)
                if origin is not None:
                    setups[origin]["path"] = "armed_then_cancelled"
            elif event.type is EventType.TRADE_OPENED:
                trade = trace.post_state.trade
                if trade is None or event.trade_id != trade.trade_id:
                    raise ValueError("trade-open event/state mismatch")
                path = "armed_then_opened" if trade.setup_origin_timestamp in setups and setups[trade.setup_origin_timestamp]["path"] == "armed_pending" else "immediate_open"
                setups.setdefault(trade.setup_origin_timestamp, {"timestamp": trade.setup_origin_timestamp, "direction": trade.side.value})["path"] = path
                entries[trade.trade_id] = {"ordinal": ordinal, "trade": trade, "entry_timestamp": event.timestamp, "path": path}
            elif event.type is EventType.TRADE_CLOSED:
                if event.trade_id in closes:
                    raise ValueError("duplicate trade close")
                closes[event.trade_id] = event
    closed = {trade.trade_id: trade for trade in backtest.closed_trades}
    if set(closes) != set(closed):
        raise ValueError("replay/backtest closed trade identity mismatch")
    trades: list[dict[str, Any]] = []
    for trade_id, info in entries.items():
        trade = info["trade"]
        row: dict[str, Any] = {
            "trade_id": trade_id, "setup_timestamp": trade.setup_origin_timestamp,
            "direction": trade.side.value, "path": info["path"], "entry_timestamp": info["entry_timestamp"],
            "entry_price": trade.entry_price, "stop_price": trade.stop_price, "outcome": "censored",
            "exit_timestamp": None, "exit_price": None, "exit_reasons": [], "r": None, "stop_hit": None,
        }
        if trade_id in closed:
            closed_trade, event = closed[trade_id], closes[trade_id]
            risk = abs(trade.entry_price - trade.stop_price)
            reasons = [reason.value for reason in event.reasons]
            row.update({"outcome": "closed", "exit_timestamp": closed_trade.exit_timestamp,
                        "exit_price": closed_trade.canonical_exit_price, "exit_reasons": reasons,
                        "r": closed_trade.net_pnl / (risk * closed_trade.quantity),
                        "stop_hit": "exit_stop" in reasons})
        trades.append(row)
    plotted_hema = HemaTrend()
    plotted_values: dict[int, tuple[float | None, float | None]] = {}
    for bar in ltf:
        result = plotted_hema.update(Candle(bar.open, bar.high, bar.low, bar.close, bar.volume if bar.volume is not None else float("nan"), bar.open_time))
        plotted_values[bar.open_time] = (
            result.fast.value if math.isfinite(result.fast.value) else None,
            result.slow.value if math.isfinite(result.slow.value) else None,
        )
    indicator_ltf = {
        trace.source_bar.open_time: {"hema_direction": _direction(trace.strategy_bar.hema_direction),
                                     "kalman_direction": _direction(trace.strategy_bar.kalman_direction),
                                     "hema_cross": _direction(trace.strategy_bar.hema_flip),
                                     "kalman_transition": _direction(trace.strategy_bar.kalman_transition),
                                     "htf_bias_as_of_m15": _direction(trace.strategy_bar.htf_bias),
                                     "hema_fast_value": plotted_values[trace.source_bar.open_time][0],
                                     "hema_slow_value": plotted_values[trace.source_bar.open_time][1]}
        for trace in ltf_traces if trace.strategy_bar is not None
    }
    indicator_htf = {trace.source_bar.open_time: _direction(trace.htf_bias_after_update) for trace in htf_traces}
    return {"setups": sorted(setups.values(), key=lambda item: item["timestamp"]),
            "trades": sorted(trades, key=lambda item: (item["entry_timestamp"], item["trade_id"])),
            "indicator_ltf": indicator_ltf, "indicator_htf": indicator_htf}


def match_setups(
    reference: Sequence[Mapping[str, Any]], candidate: Sequence[Mapping[str, Any]],
    *, tolerance_ms: int = SETUP_TOLERANCE_MS,
) -> dict[str, Any]:
    """Chronological reference-first one-to-one matcher, fixed before economics.

    Candidate selection is exact direction, nearest timestamp, earlier timestamp,
    then original candidate order.  Used candidates can never be reused.
    """
    if tolerance_ms < 0:
        raise ValueError("setup matching tolerance must be non-negative")
    used: set[int] = set(); matches: list[Match] = []
    for reference_index, item in enumerate(reference):
        candidates = [(abs(int(other["timestamp"]) - int(item["timestamp"])), int(other["timestamp"]), candidate_index)
                      for candidate_index, other in enumerate(candidate)
                      if candidate_index not in used and other["direction"] == item["direction"] and abs(int(other["timestamp"]) - int(item["timestamp"])) <= tolerance_ms]
        if candidates:
            _, _, candidate_index = min(candidates)
            used.add(candidate_index)
            matches.append(Match(reference_index, candidate_index, int(candidate[candidate_index]["timestamp"]) - int(item["timestamp"])))
    exact = sum(match.delta_ms == 0 for match in matches)
    near = len(matches) - exact
    reference_unmatched = [index for index in range(len(reference)) if index not in {match.reference_index for match in matches}]
    candidate_unmatched = [index for index in range(len(candidate)) if index not in used]
    direction_mismatches = sum(
        any(abs(int(other["timestamp"]) - int(item["timestamp"])) <= tolerance_ms and other["direction"] != item["direction"] for other in candidate)
        for item in reference
    )
    return {"contract": "reference_chronological_same_direction_nearest_then_earlier_timestamp_then_input_order_one_to_one",
            "tolerance_ms": tolerance_ms, "matches": matches, "exact_timestamp_matches": exact,
            "plus_minus_one_bar_matches": near, "reference_unmatched_indexes": reference_unmatched,
            "candidate_unmatched_indexes": candidate_unmatched, "direction_mismatch_reference_count": direction_mismatches,
            "agreement_rate": None if not reference else len(matches) / len(reference)}


def match_trades(
    reference: Sequence[Mapping[str, Any]], candidate: Sequence[Mapping[str, Any]],
    setup_matches: Sequence[Match], reference_setups: Sequence[Mapping[str, Any]],
    candidate_setups: Sequence[Mapping[str, Any]],
) -> dict[str, Any]:
    """Match trades only through a fixed setup match, direction, and chronology."""
    candidate_by_setup: dict[tuple[int, str], list[int]] = defaultdict(list)
    for index, item in enumerate(candidate):
        candidate_by_setup[(int(item["setup_timestamp"]), str(item["direction"]))].append(index)
    used: set[int] = set(); matches: list[Match] = []
    for reference_index, item in enumerate(reference):
        matching_setup = next((match for match in setup_matches if int(reference_setups[match.reference_index]["timestamp"]) == int(item["setup_timestamp"]) and str(reference_setups[match.reference_index]["direction"]) == str(item["direction"])), None)
        if matching_setup is None:
            continue
        key = (int(candidate_setups[matching_setup.candidate_index]["timestamp"]), str(item["direction"]))
        options = [index for index in candidate_by_setup.get(key, []) if index not in used]
        if options:
            candidate_index = min(options, key=lambda index: (int(candidate[index]["entry_timestamp"]), index))
            used.add(candidate_index)
            matches.append(Match(reference_index, candidate_index, int(candidate[candidate_index]["entry_timestamp"]) - int(item["entry_timestamp"])))
    return {"contract": "matched_setup_then_exact_direction_then_earliest_entry_then_input_order_one_to_one",
            "matches": matches, "reference_unmatched_indexes": [index for index in range(len(reference)) if index not in {match.reference_index for match in matches}],
            "candidate_unmatched_indexes": [index for index in range(len(candidate)) if index not in used]}


def _correlation(left: Sequence[float], right: Sequence[float]) -> float | None:
    if len(left) < 2 or len(left) != len(right):
        return None
    left_mean, right_mean = mean(left), mean(right)
    numerator = sum((a - left_mean) * (b - right_mean) for a, b in zip(left, right))
    denominator = (sum((a - left_mean) ** 2 for a in left) * sum((b - right_mean) ** 2 for b in right)) ** .5
    return None if denominator == 0 else float(numerator / denominator)


def ohlc_compatibility(reference: Sequence[HistoricalBar], candidate: Sequence[HistoricalBar]) -> dict[str, Any]:
    ref, other = {bar.open_time: bar for bar in reference}, {bar.open_time: bar for bar in candidate}
    common = sorted(set(ref).intersection(other))
    fields: dict[str, Any] = {}
    for field in ("open", "high", "low", "close"):
        absolute = [abs(float(getattr(ref[key], field)) - float(getattr(other[key], field))) for key in common]
        relative = [value / abs(float(getattr(ref[key], field))) for value, key in zip(absolute, common) if getattr(ref[key], field) != 0]
        fields[field] = {"absolute_price_difference": _summary(absolute), "relative_difference": _summary(relative),
                         "price_correlation": _correlation([float(getattr(ref[key], field)) for key in common], [float(getattr(other[key], field)) for key in common])}
    paired = [(left, right) for left, right in zip(common, common[1:]) if right - left == Timeframe.MINUTES_15.duration_ms]
    ref_returns = [ref[right].close / ref[left].close - 1 for left, right in paired]
    other_returns = [other[right].close / other[left].close - 1 for left, right in paired]
    signs = lambda value: 1 if value > 0 else -1 if value < 0 else 0
    high_low_disagreement = sum(
        ((ref[key].high > ref[previous].close, ref[key].low < ref[previous].close) !=
         (other[key].high > other[previous].close, other[key].low < other[previous].close))
        for previous, key in paired
    )
    return {"aligned_timestamp_count": len(common), "reference_only_timestamp_count": len(set(ref) - set(other)),
            "candidate_only_timestamp_count": len(set(other) - set(ref)), "fields": fields,
            "returns": {"comparable_consecutive_pair_count": len(paired), "return_correlation": _correlation(ref_returns, other_returns),
                        "close_direction_sign_agreement": None if not paired else sum(signs(a) == signs(b) for a, b in zip(ref_returns, other_returns)) / len(paired),
                        "high_low_ordering_material_difference_count": high_low_disagreement}}


def _trade_economics(rows: Sequence[Mapping[str, Any]], eligible: int | None = None) -> dict[str, Any]:
    closed = [item for item in rows if item["outcome"] == "closed"]
    values = [float(item["r"]) for item in closed]
    wins, losses = [value for value in values if value > 0], [value for value in values if value < 0]
    ordered_winners = sorted(
        (item for item in closed if float(item["r"]) > 0),
        key=lambda item: (-float(item["r"]), int(item.get("exit_timestamp") or 0), str(item.get("trade_id", ""))),
    )
    top_five_r = float(sum(float(item["r"]) for item in ordered_winners[:5]))
    return {"opened_trades": len(rows), "closed_trades": len(closed), "censored_trades": len(rows) - len(closed),
            "total_r": float(sum(values)), "expectancy_r": None if not values else float(mean(values)),
            "r_per_setup": None if not eligible else float(sum(values) / eligible),
            "profit_factor": None if not losses else float(sum(wins) / abs(sum(losses))),
            "win_rate": None if not values else sum(value > 0 for value in values) / len(values),
            "stop_rate": None if not closed else sum(bool(item["stop_hit"]) for item in closed) / len(closed),
            "median_r": None if not values else float(median(values)), "maximum_winner_r": None if not wins else float(max(wins)),
            "positive_r_mean": None if not wins else float(mean(wins)), "positive_r_median": None if not wins else float(median(wins)),
            "negative_r_mean": None if not losses else float(mean(losses)), "negative_r_median": None if not losses else float(median(losses)),
            "negative_r_magnitude_mean": None if not losses else float(mean(abs(value) for value in losses)),
            "negative_r_magnitude_median": None if not losses else float(median(abs(value) for value in losses)),
            "top_5_winner_r": top_five_r, "top_5_winner_contribution_denominator": "positive_r",
            "top_5_winner_positive_r_share": None if not wins else top_five_r / sum(wins),
            "winners_ge_2r": sum(value >= 2 for value in wins), "winners_ge_3r": sum(value >= 3 for value in wins), "winners_ge_5r": sum(value >= 5 for value in wins)}


def _provider_trade_report(ledger: Mapping[str, Any]) -> dict[str, Any]:
    setups, trades = ledger["setups"], ledger["trades"]
    paths = ("immediate_open", "armed_then_opened", "armed_then_cancelled")
    eligible = [item for item in setups if item.get("path") in paths]
    monthly: dict[str, list[Mapping[str, Any]]] = defaultdict(list)
    for trade in trades:
        monthly[_utc(int(trade["entry_timestamp"]))[:7]].append(trade)
    return {"observed_setups": len(setups), "eligible_setups": len(eligible),
            "setup_direction": dict(sorted(Counter(str(item["direction"]) for item in setups).items())),
            "setup_paths": dict(sorted(Counter(str(item.get("path", "rejected")) for item in setups).items())),
            "economics": _trade_economics(trades, len(eligible)),
            "by_direction": {side: _trade_economics([item for item in trades if item["direction"] == side], sum(item["direction"] == side and item.get("path") in paths for item in setups)) for side in ("long", "short")},
            "by_path": {path: _trade_economics([item for item in trades if item["path"] == path], sum(item.get("path") == path for item in setups)) for path in paths},
            "monthly": {month: _trade_economics(items) for month, items in sorted(monthly.items())},
            "distribution_and_path": path_risk(trades)}


def _indicator_report(reference: Mapping[str, Any], candidate: Mapping[str, Any]) -> dict[str, Any]:
    ltf_ref, ltf_other = reference["indicator_ltf"], candidate["indicator_ltf"]
    fast_ref, fast_other = _slope_direction_at_common_timestamps(
        {key: value["hema_fast_value"] for key, value in ltf_ref.items()},
        {key: value["hema_fast_value"] for key, value in ltf_other.items()},
    )
    slow_ref, slow_other = _slope_direction_at_common_timestamps(
        {key: value["hema_slow_value"] for key, value in ltf_ref.items()},
        {key: value["hema_slow_value"] for key, value in ltf_other.items()},
    )
    h4_ref = {key: value["htf_bias_as_of_m15"] for key, value in ltf_ref.items()}
    h4_other = {key: value["htf_bias_as_of_m15"] for key, value in ltf_other.items()}
    return {"hema_fast_slope_direction": {"definition": "sign of exact frozen fast HEMA plotted value versus preceding common comparison timestamp; up/down/flat; unavailable excluded; no tolerance; descriptive only, not strategy state", "agreement": _agreement(fast_ref, fast_other, exclude_unavailable=True)},
            "hema_slow_slope_direction": {"definition": "sign of exact frozen slow HEMA plotted value versus preceding common comparison timestamp; up/down/flat; unavailable excluded; no tolerance; descriptive only, not strategy state", "agreement": _agreement(slow_ref, slow_other, exclude_unavailable=True)},
            "hema_trend_state": _agreement({key: value["hema_direction"] for key, value in ltf_ref.items()}, {key: value["hema_direction"] for key, value in ltf_other.items()}),
            "hema_cross_event": _agreement({key: value["hema_cross"] for key, value in ltf_ref.items()}, {key: value["hema_cross"] for key, value in ltf_other.items()}),
            "kalman_plotted_direction": {"definition": "frozen plotted Up/Down direction derived from Kalman pineDirection", "agreement": _agreement({key: value["kalman_direction"] for key, value in ltf_ref.items()}, {key: value["kalman_direction"] for key, value in ltf_other.items()})},
            "supertrend_direction": {"definition": "frozen semantic Supertrend direction derived from the same Kalman pineDirection as plotted Up/Down", "redundant_with": "kalman_plotted_direction", "agreement": _agreement({key: value["kalman_direction"] for key, value in ltf_ref.items()}, {key: value["kalman_direction"] for key, value in ltf_other.items()})},
            "kalman_transition_marker": _agreement({key: value["kalman_transition"] for key, value in ltf_ref.items()}, {key: value["kalman_transition"] for key, value in ltf_other.items()}),
            "h4_bias_as_of_common_m15": {"definition": "each provider's existing frozen latest-finalized H4 bias carried into common M15 decision/finalization timestamps; H4 finalization precedes coincident M15 processing; no H4 timestamp shift or nearest matching", "agreement": _agreement(h4_ref, h4_other)},
            "h4_bias_transition_as_of_common_m15": {"definition": "change in the existing frozen carried H4 bias across common M15 decision/finalization timestamps; no H4 cross marker is inferred", "agreement": _agreement(_bias_transition_markers(h4_ref), _bias_transition_markers(h4_other))}}


def _match_report(reference: Sequence[Mapping[str, Any]], candidate: Sequence[Mapping[str, Any]], matches: Sequence[Match]) -> dict[str, Any]:
    rows = []
    for match in matches:
        left, right = reference[match.reference_index], candidate[match.candidate_index]
        if left["outcome"] != "closed" or right["outcome"] != "closed":
            continue
        rows.append({"entry_time_difference_ms": int(right["entry_timestamp"]) - int(left["entry_timestamp"]),
                     "entry_price_relative_difference": abs(float(right["entry_price"]) - float(left["entry_price"])) / abs(float(left["entry_price"])),
                     "exit_time_difference_ms": int(right["exit_timestamp"]) - int(left["exit_timestamp"]),
                     "exit_reason_agree": tuple(left["exit_reasons"]) == tuple(right["exit_reasons"]),
                     "r_difference": float(right["r"]) - float(left["r"]),
                     "r_sign_agree": (float(left["r"]) > 0) == (float(right["r"]) > 0),
                     "winner_loser_agree": (float(left["r"]) > 0) == (float(right["r"]) > 0)})
    return {"matched_trade_count": len(matches), "matched_closed_trade_count": len(rows),
            "entry_time_difference_ms": _summary([float(item["entry_time_difference_ms"]) for item in rows]),
            "entry_price_relative_difference": _summary([float(item["entry_price_relative_difference"]) for item in rows]),
            "exit_time_difference_ms": _summary([float(item["exit_time_difference_ms"]) for item in rows]),
            "exit_reason_agreement_rate": None if not rows else sum(item["exit_reason_agree"] for item in rows) / len(rows),
            "r_difference": _summary([float(item["r_difference"]) for item in rows]),
            "sign_of_r_agreement_rate": None if not rows else sum(item["r_sign_agree"] for item in rows) / len(rows),
            "winner_loser_agreement_rate": None if not rows else sum(item["winner_loser_agree"] for item in rows) / len(rows)}


def build_xm_gold_compatibility_report(
    *, xm_m1_source: Path, tradingview_15m_source: Path, tradingview_4h_source: Path,
) -> dict[str, Any]:
    """Build the deterministic compatibility artifact; its decision remains Sol-owned."""
    raw_audit, selected_m1 = audit_xm_m1(xm_m1_source)
    xm_m15 = aggregate_m1(selected_m1, timeframe=Timeframe.MINUTES_15)
    xm_h4 = aggregate_m1(selected_m1, timeframe=Timeframe.HOURS_4)
    tv_ltf_raw, tv_htf_raw = tradingview_15m_source.read_bytes(), tradingview_4h_source.read_bytes()
    source_manifest = _verified_source_manifest(
        xm_path=xm_m1_source, xm_audit=raw_audit, tradingview_15m_path=tradingview_15m_source,
        tradingview_15m_raw=tv_ltf_raw, tradingview_4h_path=tradingview_4h_source,
        tradingview_4h_raw=tv_htf_raw,
    )
    tv_ltf = parse_visible_tradingview_export(tv_ltf_raw, declared_symbol=TRADINGVIEW_SYMBOL, timeframe=Timeframe.MINUTES_15)
    tv_htf = parse_visible_tradingview_export(tv_htf_raw, declared_symbol=TRADINGVIEW_SYMBOL, timeframe=Timeframe.HOURS_4)
    tv_ltf = tuple(bar for bar in tv_ltf if COMPARISON_START_MS <= bar.open_time <= COMPARISON_END_MS)
    # The frozen TradingView 4H export predates this compatibility window.  It
    # remains in that provider's independent H4 warm-up only; XM never receives
    # an analogous pre-March bar, and the asymmetry is reported as a caveat.
    tv_htf_replay = tuple(bar for bar in tv_htf if bar.finalized_at <= tv_ltf[-1].finalized_at)
    tv_htf_comparison = tuple(bar for bar in tv_htf_replay if COMPARISON_START_MS <= bar.open_time <= COMPARISON_END_MS)
    xm_ltf_replay = _m15_replay_input(xm_m15)
    xm_htf_replay, h4_exclusions = _h4_replay_input(
        xm_h4, first_m15_open=xm_ltf_replay[0].open_time,
        final_m15_cutoff=xm_ltf_replay[-1].finalized_at,
    )
    tv_ledger, xm_ledger = _ledger(tv_ltf, tv_htf_replay), _ledger(xm_ltf_replay, xm_htf_replay)
    setup_matching = match_setups(tv_ledger["setups"], xm_ledger["setups"])
    trade_matching = match_trades(tv_ledger["trades"], xm_ledger["trades"], setup_matching["matches"], tv_ledger["setups"], xm_ledger["setups"])
    baseline = build_market_transfer_baseline(source_15m=tradingview_15m_source, source_4h=tradingview_4h_source)
    baseline_hash = sha256((canonical_json(baseline) + "\n").encode("utf-8")).hexdigest()
    spread_overlap = [item.spread_points for item in selected_m1]
    return {
        "schema_version": SCHEMA_VERSION, "gate_outcome": "PENDING_SOL_REVIEW",
        "decision_authority": "Sol/main only", "raw_source_immutable": True,
        "source_manifest": source_manifest,
        "pre_march_strategy_analysis": {"prohibited": True, "strategy_source_start_utc": COMPARISON_START_UTC,
            "strategy_source_end_utc": _utc(COMPARISON_END_MS + Timeframe.MINUTES_15.duration_ms), "pre_march_results_serialized": False},
        "frozen_tradingview_baseline": {"expected_artifact_sha256": FROZEN_XAU_BASELINE_SHA256,
            "recomputed_artifact_sha256": baseline_hash, "identity_verified": baseline_hash == FROZEN_XAU_BASELINE_SHA256},
        "xm_raw_audit": raw_audit,
        "aggregation": {"boundary_contract": "XM documented Europe/Nicosia server-clock boundaries (M15 quarter-hour; H4 00:00/04:00/08:00/12:00/16:00/20:00), then normalized to UTC; no manual shift, synthetic bars, interpolation, or gap fill",
            "replay_policy": "all observed non-empty native M15/H4 buckets are supplied unchanged to frozen replay even when source-incomplete; no synthetic repair. H4 excludes only buckets before comparison start, after final M15 cutoff, or with DST-variable elapsed duration not representable by immutable HistoricalBar. H4 finalization has frozen priority before coincident M15 processing.",
            "rejected_prior_policy": "WHOLESALE INCOMPLETE-BUCKET EXCLUSION REJECTED: it erased real provider-native observed bars",
            "m15": _aggregation_audit(xm_m15, xm_ltf_replay, len(selected_m1)),
            "h4": _aggregation_audit(xm_h4, xm_htf_replay, len(selected_m1), exclusions=h4_exclusions)},
        "overlap": {"start_utc": COMPARISON_START_UTC, "end_utc": COMPARISON_END_UTC,
            "m15": {"tradingview_population": len(tv_ltf), "xm_population": len(xm_ltf_replay), "intersection_count": len(set(bar.open_time for bar in tv_ltf) & set(bar.open_time for bar in xm_ltf_replay)), "tradingview_only_count": len(set(bar.open_time for bar in tv_ltf) - set(bar.open_time for bar in xm_ltf_replay)), "xm_only_count": len(set(bar.open_time for bar in xm_ltf_replay) - set(bar.open_time for bar in tv_ltf))},
            "h4": {"tradingview_population": len(tv_htf_comparison), "xm_population": len(xm_htf_replay), "intersection_count": len(set(bar.open_time for bar in tv_htf_comparison) & set(bar.open_time for bar in xm_htf_replay)), "tradingview_only_count": len(set(bar.open_time for bar in tv_htf_comparison) - set(bar.open_time for bar in xm_htf_replay)), "xm_only_count": len(set(bar.open_time for bar in xm_htf_replay) - set(bar.open_time for bar in tv_htf_comparison)), "shifted_or_nearest_timestamp_matching": "PROHIBITED"}},
        "ohlc_compatibility": ohlc_compatibility(tv_ltf, xm_ltf_replay),
        "indicator_compatibility": _indicator_report(tv_ledger, xm_ledger),
        "setup_compatibility": {key: value for key, value in setup_matching.items() if key != "matches"} | {"matched_count": len(setup_matching["matches"])},
        "trade_compatibility": {key: value for key, value in trade_matching.items() if key != "matches"} | _match_report(tv_ledger["trades"], xm_ledger["trades"], trade_matching["matches"]),
        "provider_economics": {"tradingview": _provider_trade_report(tv_ledger), "xm": _provider_trade_report(xm_ledger)},
        "xm_spread_metadata": {"units": {"mt5": "points", "point_price_units": 0.01, "r_cost_model": "NOT_YET_ESTABLISHED"},
            "points": {**_summary(spread_overlap), "fraction_zero": None if not spread_overlap else sum(value == 0 for value in spread_overlap) / len(spread_overlap), "fraction_missing": 0.0},
            "price_units": _summary([value * .01 for value in spread_overlap])},
        "warmup_caveat": "TradingView H4 replay retains available historical H4 warm-up before the comparison window; XM H4 resets at 2026-03-01T23:00:00Z because pre-March XM bars are prohibited from HEMA/Kalman/replay/strategy analysis. This asymmetry is not repaired or shifted.",
    }


def xm_gold_compatibility_json(report: Mapping[str, Any]) -> bytes:
    return canonical_json(dict(report)).encode("utf-8") + b"\n"


def write_xm_gold_compatibility_report(report: Mapping[str, Any], path: Path, *, overwrite: bool = False) -> None:
    if path.exists() and not overwrite:
        raise FileExistsError(f"refusing to overwrite {path}; pass --overwrite")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(xm_gold_compatibility_json(report))
