"""Deterministic descriptive failure labels for Phase 7.3.

The labels classify *closed losing outcomes* only.  They are deliberately
outcome diagnostics, never entry selectors or candidate logic.
"""
from __future__ import annotations

import math
from collections.abc import Iterable


FAILURE_MODE_ORDER = ("F1_immediate_stop_failure", "F2_weak_follow_through_then_stop", "F3_material_follow_through_then_loss", "F4_non_stop_strategy_loss")


def _finite(value: object, name: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value):
        raise ValueError(f"{name} must be finite for a closed loss")
    return float(value)


def failure_mode(row: object) -> str | None:
    """Return the exclusive Phase 7.3 label, or ``None`` for a non-loss.

    Stop labels take precedence and have fixed MFE boundaries: < .25R,
    [.25R, 1R), and >= 1R.  A non-stop closed loss is F4.
    """
    realized = _finite(getattr(row, "realized_r", None), "realized_r")
    if realized >= 0.0:
        return None
    stopped = getattr(row, "stop_hit", None)
    if stopped is True:
        mfe = _finite(getattr(row, "mfe_r", None), "mfe_r")
        if mfe < 0.25:
            return FAILURE_MODE_ORDER[0]
        if mfe < 1.0:
            return FAILURE_MODE_ORDER[1]
        return FAILURE_MODE_ORDER[2]
    if stopped is False:
        return FAILURE_MODE_ORDER[3]
    raise ValueError("closed loss stop_hit must be boolean")


def classify_losing_closed(rows: Iterable[object]) -> tuple[tuple[str, object], ...]:
    """Classify every supplied loss once, retaining deterministic input order."""
    result: list[tuple[str, object]] = []
    for row in rows:
        label = failure_mode(row)
        if label is not None:
            result.append((label, row))
    return tuple(result)


def validate_failure_partition(rows: Iterable[object]) -> tuple[tuple[str, object], ...]:
    """Prove the taxonomy is mutually exclusive and exhaustive for closed losses."""
    supplied = tuple(rows)
    classified = classify_losing_closed(supplied)
    labels = tuple(label for label, _ in classified)
    if any(label not in FAILURE_MODE_ORDER for label in labels):
        raise ValueError("unknown failure-mode label")
    # Each input row is visited once and can produce at most one scalar label;
    # make the cardinality condition explicit for callers and tests.
    expected = sum(_finite(getattr(row, "realized_r", None), "realized_r") < 0.0 for row in supplied)
    if len(classified) != expected:
        raise ValueError("failure taxonomy is not exhaustive")
    return classified
