import math

import pytest

from quasartrend.indicators import Candle, HEMA, HemaRelation, HemaTrend, TrendDirection


def test_hema_default_derived_lengths_and_nested_ema_warm_up() -> None:
    fast = HEMA(20)
    slow = HEMA(40)
    assert (fast.half_length, fast.sqrt_length) == (10, 4)
    assert (slow.half_length, slow.sqrt_length) == (20, 6)
    rows = [fast.update(100.0) for _ in range(23)]

    # ta.ema seeds from an SMA after its length valid inputs. The outer EMA
    # receives no valid difference until the 20-period EMA has initialized.
    assert all(math.isnan(row.half_ema) for row in rows[:9])
    assert rows[9].half_ema == 100.0
    assert math.isnan(rows[9].full_ema)
    assert rows[19].half_ema == rows[19].full_ema == rows[19].difference == 100.0
    assert all(math.isnan(row.value) for row in rows[:22])
    assert rows[22].value == 100.0


def test_hema_positive_half_tie_rounding() -> None:
    hema = HEMA(5)
    assert hema.half_length == 3
    assert hema.sqrt_length == 2


def test_fast_and_slow_call_sites_do_not_share_state() -> None:
    trend = HemaTrend(fast_length=2, slow_length=4)
    assert trend.fast is not trend.slow
    assert trend.fast.half_ema is not trend.slow.half_ema
    trend.update(Candle(10.0, 10.0, 10.0, 10.0))
    assert trend.fast.to_checkpoint() != trend.slow.to_checkpoint()


def test_initial_equality_is_visually_bearish_without_cross_after_warm_up() -> None:
    trend = HemaTrend(fast_length=2, slow_length=3)
    rows = [trend.update(Candle(10.0, 10.0, 10.0, 10.0)) for _ in range(4)]
    assert all(row.relation is HemaRelation.UNAVAILABLE for row in rows[:3])
    result = rows[3]
    assert result.relation is HemaRelation.EQUAL
    assert result.visual_direction is TrendDirection.BEARISH
    assert not result.bullish_cross
    assert not result.bearish_cross
    assert not result.bullish_condition
    assert not result.bearish_condition
    assert not result.gray_condition


def test_hema_crosses_and_candle_classifications() -> None:
    trend = HemaTrend(fast_length=2, slow_length=5)
    rows = [
        trend.update(Candle(close, close + 1, close - 1, close))
        for close in [10.0] * 6 + [20.0] * 3 + [5.0] * 4
    ]
    # Both HEMAs first produce valid values at index 5. The next two moves
    # exercise crossover/crossunder with a valid prior pair, not warm-up na.
    assert rows[5].relation is HemaRelation.EQUAL
    assert rows[6].bullish_cross
    assert rows[6].fast.value == pytest.approx(23.333333333333336)
    assert rows[6].slow.value == pytest.approx(14.444444444444443)
    assert rows[9].bearish_cross
    assert rows[9].fast.value == pytest.approx(0.12345679012345734)
    assert rows[9].slow.value == pytest.approx(13.672839506172838)
    assert [index for index, row in enumerate(rows) if row.bullish_cross] == [6]
    assert [index for index, row in enumerate(rows) if row.bearish_cross] == [9]
    for row in rows:
        if row.bullish_condition:
            assert row.relation is HemaRelation.ABOVE
            assert row.close > row.fast.value
        if row.bearish_condition:
            assert row.relation is HemaRelation.BELOW
            assert row.close < row.fast.value


def test_hema_na_after_initialization_uses_each_ema_call_site_behavior() -> None:
    hema = HEMA(4)
    for _ in range(5):
        before = hema.update(10.0)
    missing = hema.update(math.nan)
    assert missing.half_ema == before.half_ema
    assert missing.full_ema == before.full_ema
    assert missing.difference == before.difference
    assert missing.value == before.value


def test_invalid_hema_length() -> None:
    with pytest.raises(ValueError, match="positive integer"):
        HEMA(0)
