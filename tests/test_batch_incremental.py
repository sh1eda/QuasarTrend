import math
from dataclasses import asdict
from enum import Enum
from typing import Any

from quasartrend.indicators import (
    Candle,
    HemaTrend,
    KalmanStep,
    run_hema_batch,
    run_kalman_batch,
)


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


def _rows() -> list[Candle]:
    closes = [100.0, 101.5, 99.0, 103.0, 104.0, 98.5, 97.0, 106.0, 105.0, 109.0]
    return [
        Candle(close - 0.5, close + 1.25, close - 1.5, close, 10.0 + index, index)
        for index, close in enumerate(closes)
    ]


def test_hema_batch_is_the_incremental_loop() -> None:
    candles = _rows()
    batch = run_hema_batch(candles, fast_length=3, slow_length=6)
    incremental_engine = HemaTrend(fast_length=3, slow_length=6)
    incremental = [incremental_engine.update(candle) for candle in candles]
    assert _normalized([asdict(row) for row in batch]) == _normalized([asdict(row) for row in incremental])


def test_kalman_batch_is_the_incremental_loop() -> None:
    candles = _rows()
    batch = run_kalman_batch(
        candles,
        kalman_period=3,
        kalman_alpha=0.05,
        kalman_beta=0.2,
        factor=1.5,
        atr_period=3,
    )
    engine = KalmanStep(
        kalman_period=3,
        kalman_alpha=0.05,
        kalman_beta=0.2,
        factor=1.5,
        atr_period=3,
    )
    incremental = [engine.update(candle) for candle in candles]
    assert _normalized([asdict(row) for row in batch]) == _normalized([asdict(row) for row in incremental])

