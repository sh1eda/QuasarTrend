from __future__ import annotations

from types import SimpleNamespace

import pytest

from quasartrend.research.failure_modes import FAILURE_MODE_ORDER, failure_mode, validate_failure_partition


def _row(r: float, stop: bool, mfe: float | None = 0.0) -> SimpleNamespace:
    return SimpleNamespace(realized_r=r, stop_hit=stop, mfe_r=mfe)


def test_fixed_failure_taxonomy_is_mutually_exclusive_and_exhaustive() -> None:
    rows = (_row(-1, True, .24), _row(-1, True, .25), _row(-1, True, 1.0), _row(-.2, False), _row(1, False))
    classified = validate_failure_partition(rows)
    assert tuple(label for label, _ in classified) == FAILURE_MODE_ORDER
    assert failure_mode(rows[-1]) is None


def test_stop_loss_requires_finite_mfe_and_boolean_stop_state() -> None:
    with pytest.raises(ValueError, match="mfe_r"):
        failure_mode(_row(-1, True, None))
    with pytest.raises(ValueError, match="stop_hit"):
        failure_mode(SimpleNamespace(realized_r=-1, stop_hit=None, mfe_r=.1))
