"""In-memory parser and validation for declared TradingView OHLC exports."""
from __future__ import annotations

import csv
from io import StringIO
import math

from quasartrend.replay import HistoricalBar, Timeframe

PARSER_ID = "tradingview-dual-ohlc-csv/v1"


def validate_canonical_source_bars(
    bars: tuple[HistoricalBar, ...] | list[HistoricalBar],
    timeframe: Timeframe,
) -> tuple[HistoricalBar, ...]:
    result = tuple(bars)
    if not result:
        raise ValueError("source stream must not be empty")
    symbol = result[0].symbol
    prior: int | None = None
    for bar in result:
        if bar.timeframe is not timeframe:
            raise ValueError("source stream timeframe mismatch")
        if bar.symbol != symbol:
            raise ValueError("source stream must contain one declared symbol")
        if bar.open_time % timeframe.duration_ms:
            raise ValueError("source bar is not timeframe-aligned")
        if prior is not None and bar.open_time <= prior:
            raise ValueError("source bars must be strictly chronological and unique")
        prior = bar.open_time
        if not all(math.isfinite(value) for value in (bar.open, bar.high, bar.low, bar.close)):
            raise ValueError("source OHLC must be finite")
        if bar.low > min(bar.open, bar.close) or bar.high < max(bar.open, bar.close):
            raise ValueError("source OHLC envelope does not contain open and close")
    return result


def parse_tradingview_export(
    raw_input: bytes,
    *,
    declared_symbol: str,
    timeframe: Timeframe,
    parser_id: str = PARSER_ID,
) -> tuple[HistoricalBar, ...]:
    """Parse the repository's versioned dual-OHLC TradingView CSV format."""
    if parser_id != PARSER_ID:
        raise ValueError("unsupported source parser id")
    if not declared_symbol:
        raise ValueError("declared symbol must be non-empty")
    try:
        rows = list(csv.reader(StringIO(raw_input.decode("utf-8-sig"))))
    except UnicodeDecodeError as error:
        raise ValueError("source input must be UTF-8-sig CSV") from error
    if len(rows) < 2:
        raise ValueError("source CSV must contain a header and at least one row")
    header = rows[0]
    positions = {name: [index for index, value in enumerate(header) if value == name] for name in ("time", "timestamp", "open", "high", "low", "close")}
    if len(positions["time"]) != 1 or len(positions["timestamp"]) != 1:
        raise ValueError("source CSV requires unique time and timestamp columns")
    if any(len(positions[name]) != 2 for name in ("open", "high", "low", "close")):
        raise ValueError("source CSV requires exactly two OHLC columns")
    parsed: list[HistoricalBar] = []
    for number, row in enumerate(rows[1:], start=2):
        if len(row) != len(header):
            raise ValueError(f"source CSV row {number} has wrong column count")
        try:
            time_seconds = int(row[positions["time"][0]])
            timestamp = int(row[positions["timestamp"][0]])
        except ValueError as error:
            raise ValueError(f"source CSV row {number} has invalid timestamp") from error
        if timestamp != time_seconds * 1000:
            raise ValueError("source timestamp must equal time*1000")
        values: dict[str, float] = {}
        for name in ("open", "high", "low", "close"):
            first, second = positions[name]
            try:
                left, right = float(row[first]), float(row[second])
            except ValueError as error:
                raise ValueError(f"source CSV row {number} has invalid {name}") from error
            if not math.isfinite(left) or not math.isfinite(right) or left != right:
                raise ValueError("source duplicate OHLC values must be equal and finite")
            values[name] = left
        parsed.append(HistoricalBar(declared_symbol, timeframe, timestamp, **values))
    return validate_canonical_source_bars(parsed, timeframe)
