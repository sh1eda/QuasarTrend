import math
import csv
from dataclasses import asdict
from enum import Enum
from pathlib import Path
from typing import Any

import pytest

from quasartrend.indicators import HemaTrend, KalmanStep, run_hema_batch, run_kalman_batch
from quasartrend.indicators.golden import (
    NUMERIC_COLUMNS,
    _parse_exact_field,
    cold_start_convergence,
    compare_seeded_export,
    load_tradingview_export,
    seed_indicators_from_export,
)
from quasartrend.indicators.pine import dumps_checkpoint, loads_checkpoint


EXPORTS = sorted((Path(__file__).parent / "golden").glob("*.csv"))
EXPORT_ROW_COUNTS = {
    "tradingview_15m": 10_452,
    "tradingview_4h": 8_480,
}
SEEDED_EXCLUDED_CANDLES = {
    "tradingview_15m": 45,
    "tradingview_4h": 1,
}
COLD_START_ACCEPTANCE = {
    "tradingview_15m": (0, 10_452),
    "tradingview_4h": (507, 7_973),
}


def _rewrite_export(
    tmp_path: Path,
    source: Path,
    rewrite: Any,
    *,
    filename: str | None = None,
) -> Path:
    with source.open(newline="", encoding="utf-8-sig") as handle:
        reader = csv.reader(handle)
        header = next(reader)
        rows = list(reader)
    rewrite(header, rows)
    path = tmp_path / (source.name if filename is None else filename)
    with path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.writer(handle)
        writer.writerow(header)
        writer.writerows(rows)
    return path


def _normalized(value: Any) -> Any:
    if isinstance(value, Enum):
        return value.value
    if isinstance(value, float) and math.isnan(value):
        return None
    if isinstance(value, dict):
        return {key: _normalized(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_normalized(item) for item in value]
    return value


@pytest.mark.skipif(not EXPORTS, reason="external TradingView golden CSV exports are not available")
@pytest.mark.parametrize("path", EXPORTS, ids=lambda path: path.stem)
def test_export_structure_and_source_candles(path: Path) -> None:
    audit, rows = load_tradingview_export(path)
    assert audit.source_parity_passes
    assert audit.row_count == EXPORT_ROW_COUNTS[path.stem]
    assert audit.timeframe_label in {"15m", "4H"}
    assert audit.timeframe_seconds == {"tradingview_15m": 900, "tradingview_4h": 14_400}[path.stem]
    assert audit.duplicate_columns == {
        "open": (1, 6), "high": (2, 7), "low": (3, 8), "close": (4, 9),
    }
    assert len(rows) == audit.row_count


@pytest.mark.parametrize(
    ("name", "value", "expected"),
    [
        ("bullish_cross", "0", 0),
        ("bullish_cross", "1", 1),
        ("bearish_transition", "0", 0),
        ("bearish_transition", "1", 1),
        ("direction", "-1", -1),
        ("direction", "1", 1),
    ],
)
def test_exact_export_field_domain_accepts_only_authoritative_integer_boundaries(
    name: str, value: str, expected: int,
) -> None:
    assert _parse_exact_field(value, name, context="test") == expected


@pytest.mark.parametrize(
    ("name", "value"),
    [
        ("bullish_cross", "0.5"),
        ("bullish_cross", "-1"),
        ("bullish_cross", "2"),
        ("bullish_cross", "Infinity"),
        ("bullish_cross", "na"),
        ("direction", "0"),
        ("direction", "0.5"),
        ("direction", "2"),
    ],
)
def test_exact_export_field_domain_rejects_fractional_nonfinite_na_and_out_of_domain_values(
    name: str, value: str,
) -> None:
    with pytest.raises(ValueError):
        _parse_exact_field(value, name, context="test")


def test_declared_15m_export_rejects_uniform_thirty_minute_cadence(tmp_path: Path) -> None:
    def rewrite(header: list[str], rows: list[list[str]]) -> None:
        time_index = header.index("time")
        timestamp_index = header.index("timestamp")
        first_time = int(rows[0][time_index])
        for index, row in enumerate(rows):
            time_seconds = first_time + index * 1_800
            row[time_index] = str(time_seconds)
            row[timestamp_index] = str(time_seconds * 1_000)

    path = _rewrite_export(tmp_path, Path("tests/golden/tradingview_15m.csv"), rewrite)
    with pytest.raises(ValueError, match="declared 15m export"):
        load_tradingview_export(path)


def test_export_rejects_undeclared_filename_even_with_uniform_thirty_minute_cadence(tmp_path: Path) -> None:
    def rewrite(header: list[str], rows: list[list[str]]) -> None:
        time_index = header.index("time")
        timestamp_index = header.index("timestamp")
        first_time = int(rows[0][time_index])
        for index, row in enumerate(rows):
            time_seconds = first_time + index * 1_800
            row[time_index] = str(time_seconds)
            row[timestamp_index] = str(time_seconds * 1_000)

    path = _rewrite_export(
        tmp_path, Path("tests/golden/tradingview_15m.csv"), rewrite, filename="renamed.csv",
    )
    with pytest.raises(ValueError, match="must declare exactly one supported timeframe"):
        load_tradingview_export(path)


def test_export_rejects_ambiguous_declared_timeframe_filename(tmp_path: Path) -> None:
    path = _rewrite_export(
        tmp_path,
        Path("tests/golden/tradingview_15m.csv"),
        lambda _header, _rows: None,
        filename="tradingview_15m_4h.csv",
    )
    with pytest.raises(ValueError, match="ambiguous declared timeframe"):
        load_tradingview_export(path)


def test_export_rejects_non_increasing_timestamps(tmp_path: Path) -> None:
    path = _rewrite_export(
        tmp_path,
        Path("tests/golden/tradingview_15m.csv"),
        lambda _header, rows: rows.reverse(),
    )
    with pytest.raises(ValueError, match="timestamps must be strictly increasing"):
        load_tradingview_export(path)


def test_export_rejects_fractional_exact_crossover_value(tmp_path: Path) -> None:
    def rewrite(header: list[str], rows: list[list[str]]) -> None:
        rows[0][header.index("bullish_cross")] = "0.5"

    path = _rewrite_export(tmp_path, Path("tests/golden/tradingview_15m.csv"), rewrite)
    with pytest.raises(ValueError, match="bullish_cross must be an integer"):
        load_tradingview_export(path)


def test_export_rejects_duplicate_ohlc_infinities_even_when_they_match(tmp_path: Path) -> None:
    def rewrite(header: list[str], rows: list[list[str]]) -> None:
        for index in [index for index, name in enumerate(header) if name == "open"]:
            rows[0][index] = "Infinity"

    path = _rewrite_export(tmp_path, Path("tests/golden/tradingview_15m.csv"), rewrite)
    with pytest.raises(ValueError, match="open: expected a finite number"):
        load_tradingview_export(path)


def test_export_rejects_duplicate_ohlc_values_that_overflow_python_float(tmp_path: Path) -> None:
    def rewrite(header: list[str], rows: list[list[str]]) -> None:
        for index in [index for index, name in enumerate(header) if name == "open"]:
            rows[0][index] = "1e1000000"

    path = _rewrite_export(tmp_path, Path("tests/golden/tradingview_15m.csv"), rewrite)
    with pytest.raises(ValueError, match="open: not representable as a finite Python float"):
        load_tradingview_export(path)


@pytest.mark.skipif(not EXPORTS, reason="external TradingView golden CSV exports are not available")
@pytest.mark.parametrize("path", EXPORTS, ids=lambda path: path.stem)
def test_seeded_tradingview_parity_for_every_subsequent_candle(path: Path) -> None:
    comparison = compare_seeded_export(path)
    assert comparison.audit.source_parity_passes
    assert comparison.excluded_seed_candles == SEEDED_EXCLUDED_CANDLES[path.stem]
    assert comparison.compared_candles == comparison.audit.row_count - comparison.excluded_seed_candles
    assert set(comparison.numeric_mismatches) == set(NUMERIC_COLUMNS)
    assert comparison.mismatch_count == 0
    assert comparison.passes


@pytest.mark.skipif(not EXPORTS, reason="external TradingView golden CSV exports are not available")
@pytest.mark.parametrize("path", EXPORTS, ids=lambda path: path.stem)
def test_cold_start_convergence_is_measured_not_assumed(path: Path) -> None:
    convergence = cold_start_convergence(path)
    expected_start, expected_candles = COLD_START_ACCEPTANCE[path.stem]
    assert convergence.overall_acceptance_start == expected_start
    assert convergence.acceptance_candles == expected_candles


@pytest.mark.skipif(not EXPORTS, reason="external TradingView golden CSV exports are not available")
@pytest.mark.parametrize("path", EXPORTS, ids=lambda path: path.stem)
def test_same_csv_batch_incremental_and_checkpoint_resume_are_identical(path: Path) -> None:
    _, rows = load_tradingview_export(path)
    candles = [row.candle for row in rows]

    hema_batch = run_hema_batch(candles)
    hema_engine = HemaTrend()
    hema_incremental = [hema_engine.update(candle) for candle in candles]
    assert _normalized([asdict(row) for row in hema_batch]) == _normalized(
        [asdict(row) for row in hema_incremental]
    )

    kalman_batch = run_kalman_batch(candles)
    kalman_engine = KalmanStep()
    kalman_incremental = [kalman_engine.update(candle) for candle in candles]
    assert _normalized([asdict(row) for row in kalman_batch]) == _normalized(
        [asdict(row) for row in kalman_incremental]
    )

    seed_index = compare_seeded_export(path).excluded_seed_candles - 1
    seed_rows = rows[seed_index + 1:]
    seed_hema, seed_kalman = seed_indicators_from_export(rows[seed_index])
    split = 241
    prefix_hema = [seed_hema.update(row.candle) for row in seed_rows[:split]]
    prefix_kalman = [seed_kalman.update(row.candle) for row in seed_rows[:split]]
    restored_hema = HemaTrend.from_checkpoint(
        loads_checkpoint(dumps_checkpoint(seed_hema.to_checkpoint()))
    )
    restored_kalman = KalmanStep.from_checkpoint(
        loads_checkpoint(dumps_checkpoint(seed_kalman.to_checkpoint()))
    )
    resumed_hema = prefix_hema + [restored_hema.update(row.candle) for row in seed_rows[split:]]
    resumed_kalman = prefix_kalman + [restored_kalman.update(row.candle) for row in seed_rows[split:]]

    uninterrupted_hema, uninterrupted_kalman = seed_indicators_from_export(rows[seed_index])
    expected_hema = [uninterrupted_hema.update(row.candle) for row in seed_rows]
    expected_kalman = [uninterrupted_kalman.update(row.candle) for row in seed_rows]
    assert _normalized([asdict(row) for row in resumed_hema]) == _normalized(
        [asdict(row) for row in expected_hema]
    )
    assert _normalized([asdict(row) for row in resumed_kalman]) == _normalized(
        [asdict(row) for row in expected_kalman]
    )
