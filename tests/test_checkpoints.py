import math
from dataclasses import asdict
from enum import Enum
from typing import Any

import pytest

from quasartrend.indicators import Candle, HemaTrend, KalmanStep
from quasartrend.indicators.pine import dumps_checkpoint, loads_checkpoint


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


def _candles() -> list[Candle]:
    return [
        Candle(value - 0.25, value + 1.0, value - 1.0, value, timestamp=index)
        for index, value in enumerate([10.0, 11.0, 9.0, 13.0, 12.0, 15.0, 8.0, 7.0])
    ]


@pytest.mark.parametrize("split", range(9))
def test_hema_checkpoint_resume_matches_uninterrupted_at_every_split(split: int) -> None:
    candles = _candles()
    reference = HemaTrend(fast_length=3, slow_length=5)
    expected = [reference.update(candle) for candle in candles]

    first = HemaTrend(fast_length=3, slow_length=5)
    prefix = [first.update(candle) for candle in candles[:split]]
    restored = HemaTrend.from_checkpoint(
        loads_checkpoint(dumps_checkpoint(first.to_checkpoint())),
        expected_fast_length=3,
        expected_slow_length=5,
    )
    actual = prefix + [restored.update(candle) for candle in candles[split:]]
    assert _normalized([asdict(row) for row in actual]) == _normalized([asdict(row) for row in expected])


@pytest.mark.parametrize("split", range(9))
def test_kalman_checkpoint_resume_matches_uninterrupted_at_every_split(split: int) -> None:
    candles = _candles()
    kwargs = dict(kalman_period=3, kalman_alpha=0.05, kalman_beta=0.2, factor=1.5, atr_period=3)
    reference = KalmanStep(**kwargs)
    expected = [reference.update(candle) for candle in candles]

    first = KalmanStep(**kwargs)
    prefix = [first.update(candle) for candle in candles[:split]]
    restored = KalmanStep.from_checkpoint(
        loads_checkpoint(dumps_checkpoint(first.to_checkpoint())),
        expected_kalman_period=3,
        expected_kalman_alpha=0.05,
        expected_kalman_beta=0.2,
        expected_factor=1.5,
        expected_atr_period=3,
    )
    actual = prefix + [restored.update(candle) for candle in candles[split:]]
    assert _normalized([asdict(row) for row in actual]) == _normalized([asdict(row) for row in expected])


def test_partially_warmed_rma_checkpoint_contains_strict_json() -> None:
    indicator = KalmanStep(atr_period=3)
    indicator.update(Candle(9.0, 11.0, 9.0, 10.0))
    payload = dumps_checkpoint(indicator.to_checkpoint())
    assert "NaN" not in payload
    restored = KalmanStep.from_checkpoint(loads_checkpoint(payload))
    assert restored.atr.rma.seed_values == [2.0]


def test_incompatible_and_unknown_checkpoints_are_rejected() -> None:
    hema_checkpoint = HemaTrend(3, 5).to_checkpoint()
    with pytest.raises(ValueError, match="does not match"):
        HemaTrend.from_checkpoint(hema_checkpoint, expected_fast_length=4)
    hema_checkpoint["version"] = 999
    with pytest.raises(ValueError, match="unsupported"):
        HemaTrend.from_checkpoint(hema_checkpoint)

    kalman_checkpoint = KalmanStep(factor=1.5).to_checkpoint()
    with pytest.raises(ValueError, match="does not match"):
        KalmanStep.from_checkpoint(kalman_checkpoint, expected_factor=2.0)

