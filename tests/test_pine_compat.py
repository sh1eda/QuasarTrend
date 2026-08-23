import math

import pytest

from quasartrend.indicators.pine import (
    NA,
    crossover,
    crossunder,
    dumps_checkpoint,
    is_na,
    loads_checkpoint,
    nz,
    pine_eq,
    pine_gt,
    pine_lt,
    pine_round,
)


def test_na_nz_and_numeric_comparisons() -> None:
    assert is_na(NA)
    assert is_na(None)
    assert not is_na(0.0)
    assert nz(NA) == 0.0
    assert nz(NA, 7.5) == 7.5
    assert nz(3.0, 7.5) == 3.0
    assert not pine_gt(NA, 1.0)
    assert not pine_lt(1.0, NA)
    assert not pine_eq(NA, NA)


@pytest.mark.parametrize(
    ("value", "expected"),
    [(1.49, 1), (1.5, 2), (2.5, 3), (0.5, 1), (-1.5, -1), (-1.51, -2)],
)
def test_pine_round_ties_up(value: float, expected: int) -> None:
    assert pine_round(value) == expected


def test_pine_round_propagates_na_and_non_finite_as_na() -> None:
    assert math.isnan(pine_round(NA))
    assert math.isnan(pine_round(float("inf")))


def test_crosses_include_previous_equality_but_not_current_equality() -> None:
    assert crossover(2.0, 1.0, 1.0, 1.0)
    assert crossunder(0.0, 1.0, 1.0, 1.0)
    assert not crossover(1.0, 1.0, 0.0, 1.0)
    assert not crossunder(1.0, 1.0, 2.0, 1.0)
    assert not crossover(2.0, 1.0, NA, 1.0)
    assert not crossunder(0.0, 1.0, 2.0, NA)


def test_checkpoint_json_maps_na_to_null_and_back_to_mapping() -> None:
    payload = dumps_checkpoint({"value": NA, "nested": [1.0, NA]})
    assert payload == '{"nested":[1.0,null],"value":null}'
    assert loads_checkpoint(payload) == {"nested": [1.0, None], "value": None}


def test_checkpoint_json_requires_an_object() -> None:
    with pytest.raises(ValueError, match="object"):
        loads_checkpoint("[]")

