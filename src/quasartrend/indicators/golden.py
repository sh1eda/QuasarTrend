"""TradingView export auditing and external golden-dataset comparison."""

from __future__ import annotations

import csv
import math
from collections import Counter
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any

from .diagnostics import flatten_result
from .hema import HEMA, HemaTrend
from .kalman import KalmanFilter, KalmanStep
from .models import Candle
from .moving_averages import PineATR, PineEMA, PineRMA
from .pine import NA, is_na


NUMERIC_COLUMNS = (
    "fast.half_ema", "fast.full_ema", "fast.difference", "fast.value",
    "slow.half_ema", "slow.full_ema", "slow.difference", "slow.value",
    "kalman.v1", "kalman.v2", "kalman.v3", "kalman.v4", "kalman.v5",
    "atr", "raw_upper_band", "raw_lower_band", "upper_band", "lower_band",
    "previous_upper_band", "previous_lower_band", "previous_supertrend", "supertrend",
)

EXACT_EXPORTED_COLUMNS = (
    "bullish_cross", "bearish_cross", "direction",
    "bullish_transition", "bearish_transition",
)

DERIVED_EXACT_COLUMNS = (
    "hema_relation", "hema_visual_direction", "kalman_semantic_direction",
)

_NA_TEXT = {"", "na", "nan", "null", "none"}
_SEED_COLUMNS = (*NUMERIC_COLUMNS, "direction")
_EXACT_DOMAINS = {
    "bullish_cross": {0, 1},
    "bearish_cross": {0, 1},
    "bullish_transition": {0, 1},
    "bearish_transition": {0, 1},
    "direction": {-1, 1},
}


@dataclass(frozen=True, slots=True)
class ExportRow:
    time_seconds: int
    timestamp_ms: int
    candle: Candle
    expected: dict[str, str]


@dataclass(frozen=True, slots=True)
class ExportAudit:
    path: Path
    row_count: int
    column_count: int
    columns: tuple[str, ...]
    duplicate_columns: dict[str, tuple[int, ...]]
    timeframe_seconds: int | None
    timeframe_label: str
    first_timestamp_ms: int
    last_timestamp_ms: int
    timestamp_matches_open_time: bool
    continuity_gap_count: int
    source_ohlc_mismatch_count: int
    missing_cell_count: int
    symbol: str | None

    @property
    def source_parity_passes(self) -> bool:
        return (
            self.timestamp_matches_open_time
            and self.continuity_gap_count == 0
            and self.source_ohlc_mismatch_count == 0
            and self.missing_cell_count == 0
        )


@dataclass(frozen=True, slots=True)
class GoldenComparison:
    audit: ExportAudit
    compared_candles: int
    excluded_seed_candles: int
    numeric_mismatches: dict[str, tuple[int, ...]]
    exact_mismatches: dict[str, tuple[int, ...]]

    @property
    def mismatch_count(self) -> int:
        return len({
            index
            for indexes in (*self.numeric_mismatches.values(), *self.exact_mismatches.values())
            for index in indexes
        })

    @property
    def passes(self) -> bool:
        return self.audit.source_parity_passes and self.mismatch_count == 0 and self.compared_candles > 0


@dataclass(frozen=True, slots=True)
class ColdConvergence:
    field_acceptance_start: dict[str, int]
    overall_acceptance_start: int
    acceptance_candles: int


def _indices(header: list[str]) -> dict[str, list[int]]:
    return {name: [index for index, candidate in enumerate(header) if candidate == name] for name in set(header)}


def _timeframe_label(seconds: int | None) -> str:
    return {900: "15m", 14_400: "4H"}.get(seconds, f"{seconds}s" if seconds is not None else "unknown")


def _is_na_cell(value: str) -> bool:
    return value.strip().lower() in _NA_TEXT


def _finite_decimal(value: str, *, context: str) -> Decimal:
    try:
        number = Decimal(value)
    except InvalidOperation as error:
        raise ValueError(f"{context}: expected a parseable finite number") from error
    if not number.is_finite():
        raise ValueError(f"{context}: expected a finite number")
    return number


def _source_float(value: Decimal, *, context: str) -> float:
    try:
        number = float(value)
    except (OverflowError, ValueError) as error:
        raise ValueError(f"{context}: not representable as a finite Python float") from error
    if not math.isfinite(number):
        raise ValueError(f"{context}: not representable as a finite Python float")
    return number


def _source_integer(value: str, *, context: str) -> int:
    number = _finite_decimal(value, context=context)
    if number != number.to_integral_value():
        raise ValueError(f"{context}: expected an integer")
    return int(number)


def _parse_exact_field(value: str, name: str, *, context: str) -> int:
    if _is_na_cell(value):
        raise ValueError(f"{context}: exact field {name} must not be na")
    number = _finite_decimal(value, context=f"{context}: exact field {name}")
    if number != number.to_integral_value():
        raise ValueError(f"{context}: exact field {name} must be an integer")
    parsed = int(number)
    if parsed not in _EXACT_DOMAINS[name]:
        allowed = ", ".join(str(item) for item in sorted(_EXACT_DOMAINS[name]))
        raise ValueError(f"{context}: exact field {name} must be one of {{{allowed}}}")
    return parsed


def _declared_timeframe_seconds(path: Path) -> int:
    stem = path.stem.lower()
    labels = [("15m", 900), ("4h", 14_400)]
    matches = [seconds for label, seconds in labels if label in stem]
    if len(matches) > 1:
        raise ValueError(f"{path}: ambiguous declared timeframe in filename")
    if not matches:
        raise ValueError(f"{path}: golden export filename must declare exactly one supported timeframe (15m or 4h)")
    return matches[0]


def load_tradingview_export(path: Path) -> tuple[ExportAudit, list[ExportRow]]:
    with path.open(newline="", encoding="utf-8-sig") as handle:
        reader = csv.reader(handle)
        try:
            header = next(reader)
        except StopIteration as error:
            raise ValueError(f"{path}: empty CSV") from error
        raw_rows = list(reader)

    positions = _indices(header)
    required_unique = {"time", "timestamp", *NUMERIC_COLUMNS, *EXACT_EXPORTED_COLUMNS}
    missing = sorted(name for name in required_unique if len(positions.get(name, [])) != 1)
    for name in ("open", "high", "low", "close"):
        if len(positions.get(name, [])) != 2:
            missing.append(f"{name} (expected twice)")
    if missing:
        raise ValueError(f"{path}: missing or ambiguous columns: {', '.join(missing)}")

    rows: list[ExportRow] = []
    source_mismatches = 0
    missing_cells = 0
    for row_number, raw in enumerate(raw_rows, start=2):
        if len(raw) != len(header):
            raise ValueError(f"{path}:{row_number}: expected {len(header)} columns, got {len(raw)}")
        source_positions = (positions["time"][0], positions["timestamp"][0]) + tuple(
            index for name in ("open", "high", "low", "close") for index in positions[name]
        )
        missing_cells += sum(_is_na_cell(raw[index]) for index in source_positions)
        if any(_is_na_cell(raw[index]) for index in source_positions):
            raise ValueError(f"{path}:{row_number}: missing required source time, timestamp, or OHLC value")
        time_seconds = _source_integer(raw[positions["time"][0]], context=f"{path}:{row_number}: time")
        timestamp_ms = _source_integer(raw[positions["timestamp"][0]], context=f"{path}:{row_number}: timestamp")
        ohlc: dict[str, float] = {}
        for name in ("open", "high", "low", "close"):
            first, second = positions[name]
            first_value = _finite_decimal(raw[first], context=f"{path}:{row_number}: {name}")
            second_value = _finite_decimal(raw[second], context=f"{path}:{row_number}: duplicate {name}")
            first_float = _source_float(first_value, context=f"{path}:{row_number}: {name}")
            _source_float(second_value, context=f"{path}:{row_number}: duplicate {name}")
            if first_value != second_value:
                source_mismatches += 1
            ohlc[name] = first_float
        expected = {name: raw[indexes[-1]] for name, indexes in positions.items()}
        for name in EXACT_EXPORTED_COLUMNS:
            _parse_exact_field(expected[name], name, context=f"{path}:{row_number}")
        rows.append(ExportRow(
            time_seconds=time_seconds,
            timestamp_ms=timestamp_ms,
            candle=Candle(
                open=ohlc["open"],
                high=ohlc["high"],
                low=ohlc["low"],
                close=ohlc["close"],
                timestamp=timestamp_ms,
            ),
            expected=expected,
        ))

    if not rows:
        raise ValueError(f"{path}: no data rows")
    deltas = [right.time_seconds - left.time_seconds for left, right in zip(rows, rows[1:])]
    timestamp_deltas = [right.timestamp_ms - left.timestamp_ms for left, right in zip(rows, rows[1:])]
    if any(delta <= 0 for delta in timestamp_deltas):
        raise ValueError(f"{path}: timestamps must be strictly increasing")
    if any(delta <= 0 for delta in deltas):
        raise ValueError(f"{path}: source time cadence must be positive")
    cadence = Counter(deltas).most_common(1)[0][0] if deltas else None
    declared_cadence = _declared_timeframe_seconds(path)
    if cadence != declared_cadence:
        raise ValueError(
            f"{path}: declared {_timeframe_label(declared_cadence)} export has "
            f"{_timeframe_label(cadence)} cadence"
        )
    gap_count = sum(delta != cadence for delta in deltas) if cadence is not None else 0
    audit = ExportAudit(
        path=path,
        row_count=len(rows),
        column_count=len(header),
        columns=tuple(header),
        duplicate_columns={name: tuple(indexes) for name, indexes in positions.items() if len(indexes) > 1},
        timeframe_seconds=cadence,
        timeframe_label=_timeframe_label(cadence),
        first_timestamp_ms=rows[0].timestamp_ms,
        last_timestamp_ms=rows[-1].timestamp_ms,
        timestamp_matches_open_time=all(row.timestamp_ms == row.time_seconds * 1000 for row in rows),
        continuity_gap_count=gap_count,
        source_ohlc_mismatch_count=source_mismatches,
        missing_cell_count=missing_cells,
        symbol=None,
    )
    return audit, rows


def _float(row: ExportRow, name: str) -> float:
    value = row.expected[name]
    return NA if _is_na_cell(value) else float(value)


def _expected_int(row: ExportRow, name: str) -> int | float:
    return _parse_exact_field(row.expected[name], name, context=f"timestamp {row.timestamp_ms}")


def _first_complete_seed_index(rows: list[ExportRow]) -> int:
    for index, row in enumerate(rows):
        if all(not is_na(_float(row, name)) for name in _SEED_COLUMNS):
            return index
    raise ValueError("export does not contain a complete recursive indicator checkpoint")


def _seed_hema(row: ExportRow, prefix: str, length: int) -> HEMA:
    probe = HEMA(length)
    return HEMA(
        length=length,
        half_ema=PineEMA(probe.half_length, value=_float(row, f"{prefix}.half_ema"), observations=probe.half_length),
        full_ema=PineEMA(length, value=_float(row, f"{prefix}.full_ema"), observations=length),
        final_ema=PineEMA(probe.sqrt_length, value=_float(row, f"{prefix}.value"), observations=probe.sqrt_length),
    )


def seed_indicators_from_export(row: ExportRow) -> tuple[HemaTrend, KalmanStep]:
    """Use one exported row as the complete recursive checkpoint for the next row."""

    hema = HemaTrend(
        fast_length=20,
        slow_length=40,
        fast=_seed_hema(row, "fast", 20),
        slow=_seed_hema(row, "slow", 40),
        previous_fast=_float(row, "fast.value"),
        previous_slow=_float(row, "slow.value"),
    )
    kalman_filter = KalmanFilter(
        period=21, alpha=0.01, beta=0.1,
        v1=_float(row, "kalman.v1"), v2=_float(row, "kalman.v2"),
        v3=_float(row, "kalman.v3"), v4=_float(row, "kalman.v4"),
        v5=_float(row, "kalman.v5"), previous_close=row.candle.close,
    )
    atr = PineATR(
        length=7,
        rma=PineRMA(length=7, value=_float(row, "atr"), seed_values=[], observations=7),
        previous_close=row.candle.close,
    )
    kalman = KalmanStep(
        kalman_period=21, kalman_alpha=0.01, kalman_beta=0.1, factor=1.0, atr_period=7,
        kalman=kalman_filter, atr=atr,
        previous_k=_float(row, "kalman.v1"), previous_atr=_float(row, "atr"),
        previous_lower_band=_float(row, "lower_band"),
        previous_upper_band=_float(row, "upper_band"),
        previous_supertrend=_float(row, "supertrend"),
        previous_direction=_float(row, "direction"),
    )
    return hema, kalman


def _actual_expected_exact(actual: dict[str, Any], row: ExportRow) -> tuple[dict[str, Any], dict[str, Any]]:
    expected_fast = _float(row, "fast.value")
    expected_slow = _float(row, "slow.value")
    if is_na(expected_fast) or is_na(expected_slow):
        relation, visual_direction = "unavailable", None
    elif expected_fast > expected_slow:
        relation, visual_direction = "above", "bullish"
    elif expected_fast < expected_slow:
        relation, visual_direction = "below", "bearish"
    else:
        relation, visual_direction = "equal", "bearish"
    direction = _expected_int(row, "direction")
    expected = {
        **{name: _expected_int(row, name) for name in EXACT_EXPORTED_COLUMNS},
        "hema_relation": relation,
        "hema_visual_direction": visual_direction,
        "kalman_semantic_direction": None if is_na(direction) else "bullish" if direction < 0 else "bearish",
    }
    observed = {
        "bullish_cross": int(bool(actual["bullish_cross"])),
        "bearish_cross": int(bool(actual["bearish_cross"])),
        "direction": int(actual["direction"]),
        "bullish_transition": int(bool(actual["bullish_transition"])),
        "bearish_transition": int(bool(actual["bearish_transition"])),
        "hema_relation": actual["relation"],
        "hema_visual_direction": actual["visual_direction"],
        "kalman_semantic_direction": actual["semantic_direction"],
    }
    return observed, expected


def _numeric_matches(actual: Any, expected: float, *, rel_tol: float, abs_tol: float) -> bool:
    if is_na(actual) or is_na(expected):
        return is_na(actual) and is_na(expected)
    return math.isclose(float(actual), expected, rel_tol=rel_tol, abs_tol=abs_tol)


def _exact_matches(actual: Any, expected: Any) -> bool:
    return is_na(actual) and is_na(expected) if is_na(actual) or is_na(expected) else actual == expected


def _compare_rows(
    rows: list[ExportRow], hema: HemaTrend, kalman: KalmanStep, *,
    start_index: int, rel_tol: float, abs_tol: float,
) -> tuple[dict[str, list[int]], dict[str, list[int]]]:
    numeric = {name: [] for name in NUMERIC_COLUMNS}
    exact = {name: [] for name in (*EXACT_EXPORTED_COLUMNS, *DERIVED_EXACT_COLUMNS)}
    for index in range(start_index, len(rows)):
        row = rows[index]
        actual = flatten_result(hema.update(row.candle))
        actual.update(flatten_result(kalman.update(row.candle)))
        for name in NUMERIC_COLUMNS:
            if not _numeric_matches(actual[name], _float(row, name), rel_tol=rel_tol, abs_tol=abs_tol):
                numeric[name].append(index)
        observed_exact, expected_exact = _actual_expected_exact(actual, row)
        for name in exact:
            if not _exact_matches(observed_exact[name], expected_exact[name]):
                exact[name].append(index)
    return numeric, exact


def compare_seeded_export(path: Path, *, rel_tol: float = 1e-12, abs_tol: float = 1e-9) -> GoldenComparison:
    audit, rows = load_tradingview_export(path)
    seed_index = _first_complete_seed_index(rows)
    hema, kalman = seed_indicators_from_export(rows[seed_index])
    numeric, exact = _compare_rows(rows, hema, kalman, start_index=seed_index + 1, rel_tol=rel_tol, abs_tol=abs_tol)
    return GoldenComparison(
        audit=audit, compared_candles=len(rows) - seed_index - 1, excluded_seed_candles=seed_index + 1,
        numeric_mismatches={name: tuple(indexes) for name, indexes in numeric.items()},
        exact_mismatches={name: tuple(indexes) for name, indexes in exact.items()},
    )


def cold_start_convergence(path: Path, *, rel_tol: float = 1e-12, abs_tol: float = 1e-9) -> ColdConvergence:
    _, rows = load_tradingview_export(path)
    numeric, exact = _compare_rows(
        rows, HemaTrend(), KalmanStep(), start_index=0, rel_tol=rel_tol, abs_tol=abs_tol
    )
    starts = {
        name: (max(indexes) + 1 if indexes else 0)
        for name, indexes in {**numeric, **exact}.items()
    }
    overall = max(starts.values(), default=0)
    return ColdConvergence(starts, overall, max(0, len(rows) - overall))
