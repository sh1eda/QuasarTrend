"""Small Pine v6 compatibility surface required by the supplied indicators."""

from __future__ import annotations

import json
import math
from collections.abc import Mapping, Sequence
from typing import Any


NA = float("nan")


def is_na(value: object) -> bool:
    """Return Pine-like ``na(value)`` for numeric values used by this package."""

    return value is None or (isinstance(value, float) and math.isnan(value))


def nz(value: float, replacement: float = 0.0) -> float:
    """Return ``replacement`` when value is ``na``; otherwise return value."""

    return replacement if is_na(value) else value


def pine_round(value: float) -> int | float:
    """Pine ``math.round``: nearest integer, with ties toward positive infinity."""

    if is_na(value):
        return NA
    if not math.isfinite(value):
        return NA
    return math.floor(value + 0.5)


def pine_gt(left: float, right: float) -> bool:
    return False if is_na(left) or is_na(right) else left > right


def pine_gte(left: float, right: float) -> bool:
    return False if is_na(left) or is_na(right) else left >= right


def pine_lt(left: float, right: float) -> bool:
    return False if is_na(left) or is_na(right) else left < right


def pine_lte(left: float, right: float) -> bool:
    return False if is_na(left) or is_na(right) else left <= right


def pine_eq(left: float, right: float) -> bool:
    return False if is_na(left) or is_na(right) else left == right


def crossover(current_a: float, current_b: float, previous_a: float, previous_b: float) -> bool:
    """Exact numeric form of ``ta.crossover(a, b)``."""

    return pine_gt(current_a, current_b) and pine_lte(previous_a, previous_b)


def crossunder(current_a: float, current_b: float, previous_a: float, previous_b: float) -> bool:
    """Exact numeric form of ``ta.crossunder(a, b)``."""

    return pine_lt(current_a, current_b) and pine_gte(previous_a, previous_b)


def _json_safe(value: Any) -> Any:
    if is_na(value):
        return None
    if isinstance(value, Mapping):
        return {str(key): _json_safe(item) for key, item in value.items()}
    if isinstance(value, tuple):
        return [_json_safe(item) for item in value]
    if isinstance(value, Sequence) and not isinstance(value, (str, bytes, bytearray)):
        return [_json_safe(item) for item in value]
    return value


def dumps_checkpoint(checkpoint: Mapping[str, Any]) -> str:
    """Serialize a checkpoint as strict JSON, representing numeric ``na`` as null."""

    return json.dumps(_json_safe(checkpoint), allow_nan=False, sort_keys=True, separators=(",", ":"))


def loads_checkpoint(payload: str) -> dict[str, Any]:
    value = json.loads(payload)
    if not isinstance(value, dict):
        raise ValueError("checkpoint JSON must contain an object")
    return value


def checkpoint_float(value: object) -> float:
    """Restore a checkpoint float, mapping JSON null back to numeric ``na``."""

    return NA if value is None else float(value)

