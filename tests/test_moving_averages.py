import math

import pytest

from quasartrend.indicators import Candle, PineATR, PineEMA, PineRMA
from quasartrend.indicators.pine import NA, dumps_checkpoint, loads_checkpoint


def test_ema_sma_initialization_recurrence_and_na_after_initialization() -> None:
    ema = PineEMA(3)
    values = [ema.update(value) for value in [NA, 10.0, 12.0, NA, 14.0, 16.0]]
    assert all(math.isnan(values[index]) for index in [0, 1, 2, 3])
    assert values[4:] == pytest.approx([12.0, 14.0])
    assert ema.observations == 4


def test_ema_length_one_and_leading_na() -> None:
    ema = PineEMA(1)
    assert math.isnan(ema.update(NA))
    assert ema.update(4.0) == 4.0
    assert ema.update(9.0) == 9.0


def test_ema_checkpoint_round_trip_preserves_partial_and_initialized_sma_seed_state() -> None:
    partial = PineEMA(3)
    assert math.isnan(partial.update(10.0))
    restored_partial = PineEMA.from_checkpoint(loads_checkpoint(dumps_checkpoint(partial.to_checkpoint())))
    assert restored_partial.seed_values == [10.0]
    assert math.isnan(restored_partial.update(12.0))
    assert restored_partial.update(14.0) == pytest.approx(12.0)

    initialized = PineEMA(3)
    for value in [10.0, 12.0, 14.0]:
        initialized.update(value)
    restored_initialized = PineEMA.from_checkpoint(
        loads_checkpoint(dumps_checkpoint(initialized.to_checkpoint()))
    )
    assert restored_initialized.seed_values == []
    assert restored_initialized.update(16.0) == pytest.approx(initialized.update(16.0))


def test_ema_legacy_initialized_checkpoint_without_seed_values_remains_restorable() -> None:
    restored = PineEMA.from_checkpoint({
        "type": "PineEMA", "version": 1, "length": 3, "value": 12.0, "observations": 3,
    })
    assert restored.seed_values == []
    assert restored.update(16.0) == pytest.approx(14.0)


def test_rma_sma_seed_ignores_na_then_uses_wilder_recurrence() -> None:
    rma = PineRMA(3)
    values = [rma.update(value) for value in [1.0, NA, 2.0, 3.0, NA, 6.0]]
    assert all(math.isnan(values[index]) for index in [0, 1, 2])
    assert values[3] == pytest.approx(2.0)
    assert values[4] == pytest.approx(2.0)
    assert values[5] == pytest.approx(10.0 / 3.0)
    assert rma.observations == 4


def test_rma_length_one_initializes_immediately() -> None:
    rma = PineRMA(1)
    assert rma.update(5.0) == 5.0
    assert rma.update(8.0) == 8.0


def test_atr_true_range_first_bar_gap_and_rma_seed() -> None:
    atr = PineATR(3)
    candles = [
        Candle(9.0, 11.0, 9.0, 10.0),
        Candle(12.0, 13.0, 11.0, 12.0),
        Candle(11.0, 12.0, 8.0, 9.0),
        Candle(10.0, 14.0, 10.0, 13.0),
    ]
    results = [atr.update(candle) for candle in candles]
    assert [result.true_range for result in results] == pytest.approx([2.0, 3.0, 4.0, 5.0])
    assert math.isnan(results[0].atr)
    assert math.isnan(results[1].atr)
    assert results[2].atr == pytest.approx(3.0)
    assert results[3].atr == pytest.approx(11.0 / 3.0)


def test_atr_na_high_low_and_na_close_sequences() -> None:
    atr = PineATR(2)
    first = atr.update(Candle(1.0, 3.0, 1.0, 2.0))
    missing = atr.update(Candle(2.0, NA, NA, NA))
    fallback = atr.update(Candle(4.0, 6.0, 4.0, 5.0))
    assert first.true_range == 2.0
    assert math.isnan(missing.true_range)
    assert math.isnan(missing.atr)
    assert fallback.true_range == 2.0
    assert fallback.atr == pytest.approx(2.0)


@pytest.mark.parametrize("factory", [lambda: PineEMA(0), lambda: PineRMA(-1), lambda: PineATR(0)])
def test_invalid_lengths_are_rejected(factory) -> None:
    with pytest.raises(ValueError, match="positive integer"):
        factory()
