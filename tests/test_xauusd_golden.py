"""Parity checks for the supplied XAUUSD visible-plot TradingView exports.

Unlike the BTC diagnostic exports, these source files contain chart OHLC and
visible indicator plots only.  They intentionally cannot validate unexported
recursive internals (for example Kalman v1-v5 or ATR state).
"""

from __future__ import annotations

import csv
import math
from collections import Counter
from dataclasses import asdict, dataclass
from pathlib import Path

import pytest

from quasartrend.indicators import Candle, HemaTrend, KalmanStep
from quasartrend.indicators.pine import dumps_checkpoint, is_na, loads_checkpoint


XAUUSD = "XAUUSD"
GOLDEN_DIRECTORY = Path("exports/xauusd_pending")
EXPORTS = (
    (GOLDEN_DIRECTORY / "XAUUSD_15m.csv", 900),
    (GOLDEN_DIRECTORY / "XAUUSD_4h.csv", 14_400),
)

# These are deliberately the frozen BTC visible-value comparison tolerances
# from compare_seeded_export; XAU receives no market-specific widening.
REL_TOL = 1e-12
ABS_TOL = 1e-9

REQUIRED_COLUMNS = (
    "time", "open", "high", "low", "close", "Up direction", "Down direction",
    "HEMA 20", "HEMA 40", "Bullish Trend", "Bearish Trend",
)


@dataclass(frozen=True, slots=True)
class VisiblePlotRow:
    symbol: str
    time_seconds: int
    candle: Candle
    up_direction: float | None
    down_direction: float | None
    kalman_bullish_transition: float | None
    kalman_bearish_transition: float | None
    hema_fast: float | None
    hema_slow: float | None
    hema_bullish_cross: float | None
    hema_bearish_cross: float | None


def _positions(header: list[str], name: str) -> list[int]:
    return [index for index, candidate in enumerate(header) if candidate == name]


def _visible_value(raw: str, *, context: str) -> float | None:
    if not raw.strip():
        return None
    try:
        value = float(raw)
    except ValueError as error:
        raise ValueError(f"{context}: expected a finite numeric plot value") from error
    if not math.isfinite(value):
        raise ValueError(f"{context}: expected a finite numeric plot value")
    return value


def load_visible_plot_export(
    path: Path, *, declared_symbol: str, timeframe_seconds: int,
) -> list[VisiblePlotRow]:
    """Parse one standard TradingView visible-plot export without BTC assumptions."""

    if not declared_symbol:
        raise ValueError("declared symbol must be non-empty")
    with path.open(newline="", encoding="utf-8-sig") as handle:
        reader = csv.reader(handle)
        try:
            header = next(reader)
        except StopIteration as error:
            raise ValueError(f"{path}: empty CSV") from error
        raw_rows = list(reader)

    positions = {name: _positions(header, name) for name in REQUIRED_COLUMNS}
    missing = [name for name in REQUIRED_COLUMNS if not positions[name]]
    if missing:
        raise ValueError(f"{path}: missing visible-plot columns: {', '.join(missing)}")
    if any(len(positions[name]) != 1 for name in ("time", "open", "high", "low", "close", "Up direction", "Down direction", "HEMA 20", "HEMA 40")):
        raise ValueError(f"{path}: visible-plot source has ambiguous unique columns")
    # The first pair belongs to Kalman Step; the second to HEMA Trend.
    if len(positions["Bullish Trend"]) != 2 or len(positions["Bearish Trend"]) != 2:
        raise ValueError(f"{path}: visible-plot source must contain both transition plot pairs")

    rows: list[VisiblePlotRow] = []
    for number, raw in enumerate(raw_rows, start=2):
        if len(raw) != len(header):
            raise ValueError(f"{path}:{number}: expected {len(header)} columns, got {len(raw)}")
        try:
            time_seconds = int(raw[positions["time"][0]])
        except ValueError as error:
            raise ValueError(f"{path}:{number}: expected an integer time") from error
        ohlc = {
            name: _visible_value(raw[positions[name][0]], context=f"{path}:{number}: {name}")
            for name in ("open", "high", "low", "close")
        }
        if any(value is None for value in ohlc.values()):
            raise ValueError(f"{path}:{number}: source OHLC must not be empty")
        assert all(value is not None for value in ohlc.values())
        if ohlc["low"] > min(ohlc["open"], ohlc["close"]) or ohlc["high"] < max(ohlc["open"], ohlc["close"]):
            raise ValueError(f"{path}:{number}: source OHLC envelope does not contain open and close")
        rows.append(VisiblePlotRow(
            symbol=declared_symbol,
            time_seconds=time_seconds,
            candle=Candle(**ohlc, timestamp=time_seconds * 1000),
            up_direction=_visible_value(raw[positions["Up direction"][0]], context=f"{path}:{number}: Up direction"),
            down_direction=_visible_value(raw[positions["Down direction"][0]], context=f"{path}:{number}: Down direction"),
            kalman_bullish_transition=_visible_value(raw[positions["Bullish Trend"][0]], context=f"{path}:{number}: Kalman bullish transition"),
            kalman_bearish_transition=_visible_value(raw[positions["Bearish Trend"][0]], context=f"{path}:{number}: Kalman bearish transition"),
            hema_fast=_visible_value(raw[positions["HEMA 20"][0]], context=f"{path}:{number}: HEMA 20"),
            hema_slow=_visible_value(raw[positions["HEMA 40"][0]], context=f"{path}:{number}: HEMA 40"),
            hema_bullish_cross=_visible_value(raw[positions["Bullish Trend"][1]], context=f"{path}:{number}: HEMA bullish transition"),
            hema_bearish_cross=_visible_value(raw[positions["Bearish Trend"][1]], context=f"{path}:{number}: HEMA bearish transition"),
        ))

    if not rows:
        raise ValueError(f"{path}: no data rows")
    if any(right.time_seconds - left.time_seconds <= 0 for left, right in zip(rows, rows[1:])):
        raise ValueError(f"{path}: timestamps must be strictly increasing")
    # XAUUSD sessions cross daylight-saving changes and market closures, so a
    # fixed Unix-epoch alignment is not a valid 4H source invariant.  Its
    # predominant consecutive-bar cadence must nevertheless match the file's
    # declared timeframe.
    deltas = [right.time_seconds - left.time_seconds for left, right in zip(rows, rows[1:])]
    if Counter(deltas).most_common(1)[0][0] != timeframe_seconds:
        raise ValueError(f"{path}: source cadence does not match its declared timeframe")
    return rows


def _assert_visible_numeric(actual: float, expected: float | None, *, label: str, index: int) -> None:
    if expected is None:
        assert is_na(actual), f"{label} unexpectedly plotted at row {index}"
    else:
        assert not is_na(actual), f"{label} missing at row {index}"
        assert math.isclose(actual, expected, rel_tol=REL_TOL, abs_tol=ABS_TOL), (
            f"{label} mismatch at row {index}: Python={actual!r}, TradingView={expected!r}"
        )


@pytest.mark.parametrize(("path", "timeframe_seconds"), EXPORTS, ids=("xauusd_15m", "xauusd_4h"))
def test_xauusd_visible_plot_export_parity(path: Path, timeframe_seconds: int) -> None:
    rows = load_visible_plot_export(path, declared_symbol=XAUUSD, timeframe_seconds=timeframe_seconds)
    assert {row.symbol for row in rows} == {XAUUSD}

    hema = HemaTrend()
    kalman = KalmanStep()
    for index, row in enumerate(rows):
        actual_hema = hema.update(row.candle)
        actual_kalman = kalman.update(row.candle)

        _assert_visible_numeric(actual_hema.fast.value, row.hema_fast, label="HEMA 20", index=index)
        _assert_visible_numeric(actual_hema.slow.value, row.hema_slow, label="HEMA 40", index=index)

        assert actual_hema.bullish_cross is (row.hema_bullish_cross is not None), (
            f"HEMA bullish crossover mismatch at row {index}"
        )
        assert actual_hema.bearish_cross is (row.hema_bearish_cross is not None), (
            f"HEMA bearish crossunder mismatch at row {index}"
        )

        if row.up_direction is not None or row.down_direction is not None:
            assert (row.up_direction is None) != (row.down_direction is None), (
                f"TradingView direction ambiguous at row {index}"
            )
            if row.up_direction is not None:
                assert actual_kalman.direction == -1, f"Kalman bullish direction mismatch at row {index}"
                _assert_visible_numeric(actual_kalman.supertrend, row.up_direction, label="Up direction", index=index)
            else:
                assert actual_kalman.direction == 1, f"Kalman bearish direction mismatch at row {index}"
                _assert_visible_numeric(actual_kalman.supertrend, row.down_direction, label="Down direction", index=index)
        # The standard visible export suppresses the line during Kalman/ATR
        # warm-up.  It therefore has no observable direction there, but its
        # transition plots remain independently comparable below.

        assert actual_kalman.bullish_transition is (row.kalman_bullish_transition is not None), (
            f"Kalman bullish transition mismatch at row {index}"
        )
        assert actual_kalman.bearish_transition is (row.kalman_bearish_transition is not None), (
            f"Kalman bearish transition mismatch at row {index}"
        )
        if row.kalman_bullish_transition is not None:
            _assert_visible_numeric(actual_kalman.supertrend, row.kalman_bullish_transition, label="Kalman bullish transition", index=index)
        if row.kalman_bearish_transition is not None:
            _assert_visible_numeric(actual_kalman.supertrend, row.kalman_bearish_transition, label="Kalman bearish transition", index=index)


def _normalized(value: object) -> object:
    if isinstance(value, float) and math.isnan(value):
        return None
    if isinstance(value, dict):
        return {key: _normalized(item) for key, item in value.items()}
    return value


@pytest.mark.parametrize(("path", "timeframe_seconds"), EXPORTS, ids=("xauusd_15m", "xauusd_4h"))
def test_xauusd_visible_plot_recursive_checkpoint_equivalence(path: Path, timeframe_seconds: int) -> None:
    rows = load_visible_plot_export(path, declared_symbol=XAUUSD, timeframe_seconds=timeframe_seconds)
    split = len(rows) // 2

    hema = HemaTrend()
    kalman = KalmanStep()
    for row in rows[:split]:
        hema.update(row.candle)
        kalman.update(row.candle)
    resumed_hema = HemaTrend.from_checkpoint(loads_checkpoint(dumps_checkpoint(hema.to_checkpoint())))
    resumed_kalman = KalmanStep.from_checkpoint(loads_checkpoint(dumps_checkpoint(kalman.to_checkpoint())))

    uninterrupted_hema = HemaTrend()
    uninterrupted_kalman = KalmanStep()
    expected_hema = []
    expected_kalman = []
    for row in rows:
        expected_hema.append(uninterrupted_hema.update(row.candle))
        expected_kalman.append(uninterrupted_kalman.update(row.candle))

    actual_hema = [resumed_hema.update(row.candle) for row in rows[split:]]
    actual_kalman = [resumed_kalman.update(row.candle) for row in rows[split:]]
    assert [_normalized(asdict(item)) for item in actual_hema] == [
        _normalized(asdict(item)) for item in expected_hema[split:]
    ]
    assert [_normalized(asdict(item)) for item in actual_kalman] == [
        _normalized(asdict(item)) for item in expected_kalman[split:]
    ]
