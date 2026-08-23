import math

import pytest

from quasartrend.indicators import Candle, KalmanFilter, KalmanStep, PineATR, TrendDirection


def test_kalman_v1_through_v5_for_first_four_candles() -> None:
    kalman = KalmanFilter(period=2, alpha=0.1, beta=0.2)
    results = [kalman.update(close) for close in [10.0, 12.0, 11.0, 15.0]]

    expected = [
        # v1, v2, v3, v4, v5
        (math.nan, 0.2666666666666666, 0.2, 0.8333333333333334, math.nan),
        (11.142857142857142, 0.2142857142857143, 0.2, 0.5714285714285714, 10.0),
        (11.068965517241379, 0.20344827586206898, 0.2, 0.5172413793103449, 11.142857142857142),
        (13.051282051282051, 0.20085470085470086, 0.2, 0.5042735042735043, 11.068965517241379),
    ]
    for result, values in zip(results, expected, strict=True):
        actual = (result.v1, result.v2, result.v3, result.v4, result.v5)
        for observed, wanted in zip(actual, values, strict=True):
            if math.isnan(wanted):
                assert math.isnan(observed)
            else:
                assert observed == pytest.approx(wanted, rel=1e-15, abs=1e-15)

    assert math.isnan(results[0].previous_close)
    assert results[1].previous_close == 10.0
    assert results[2].previous_close == 12.0
    assert results[3].previous_close == 11.0


def test_first_bar_mutates_covariance_while_estimate_remains_na() -> None:
    kalman = KalmanFilter()
    result = kalman.update(100.0)
    assert math.isnan(result.v1)
    assert math.isnan(result.v5)
    assert result.v2 != 1.0
    assert result.v4 == pytest.approx(1.0 / 1.21)


def test_second_bar_seeds_previous_close_then_updates_current_close() -> None:
    kalman = KalmanFilter(period=2, alpha=0.1, beta=0.2)
    kalman.update(10.0)
    result = kalman.update(12.0)
    assert result.v5 == 10.0
    assert result.v1 == pytest.approx(10.0 + result.v4 * 2.0)


def test_supertrend_warmup_uses_nz_bands_and_bearish_initial_direction() -> None:
    indicator = KalmanStep(kalman_period=2, kalman_alpha=0.1, kalman_beta=0.2, atr_period=3)
    first = indicator.update(Candle(9.0, 11.0, 9.0, 10.0))
    assert math.isnan(first.kalman.v1)
    assert math.isnan(first.atr)
    assert first.previous_lower_band == 0.0
    assert first.previous_upper_band == 0.0
    assert first.lower_band == 0.0
    assert first.upper_band == 0.0
    assert first.direction == 1
    assert first.semantic_direction is TrendDirection.BEARISH
    assert first.supertrend == 0.0


def _seeded_equality_indicator(previous_supertrend: float) -> KalmanStep:
    kalman = KalmanFilter(
        period=1,
        alpha=0.0,
        beta=0.1,
        v1=10.0,
        v2=1.0,
        v3=0.0,
        v4=0.0,
        v5=10.0,
        previous_close=10.0,
    )
    atr = PineATR(1)
    atr.update(Candle(9.0, 11.0, 9.0, 10.0))
    return KalmanStep(
        kalman_period=1,
        kalman_alpha=0.0,
        kalman_beta=0.1,
        factor=0.0,
        atr_period=1,
        kalman=kalman,
        atr=atr,
        previous_k=10.0,
        previous_atr=2.0,
        previous_lower_band=10.0,
        previous_upper_band=10.0,
        previous_supertrend=previous_supertrend,
        previous_direction=1.0,
    )


def test_supertrend_band_equality_keeps_previous_bands() -> None:
    result = _seeded_equality_indicator(10.0).update(Candle(10.0, 11.0, 9.0, 10.0))
    assert result.raw_lower_band == 10.0
    assert result.raw_upper_band == 10.0
    assert not result.lower_uses_raw
    assert not result.upper_uses_raw
    assert result.lower_band == 10.0
    assert result.upper_band == 10.0


def test_supertrend_previous_supertrend_upper_equality_selects_exact_branch() -> None:
    equal_branch = _seeded_equality_indicator(10.0).update(Candle(10.0, 11.0, 9.0, 10.0))
    else_branch = _seeded_equality_indicator(9.0).update(Candle(10.0, 11.0, 9.0, 10.0))
    assert equal_branch.direction == 1
    assert else_branch.direction == -1
    assert else_branch.bullish_transition


def test_invalid_kalman_and_atr_periods() -> None:
    with pytest.raises(ValueError, match="positive integer"):
        KalmanFilter(period=0)
    with pytest.raises(ValueError, match="positive integer"):
        KalmanStep(atr_period=0)

