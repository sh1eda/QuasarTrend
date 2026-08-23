"""Stable flattening helpers used by CSV parity diagnostics."""

from __future__ import annotations

from dataclasses import asdict, is_dataclass
from enum import Enum
from typing import Any, Iterable

from .hema import HemaTrendResult
from .kalman import KalmanStepResult


def _flatten(value: Any, prefix: str, target: dict[str, Any]) -> None:
    if isinstance(value, Enum):
        target[prefix] = value.value
    elif is_dataclass(value):
        for key, item in asdict(value).items():
            _flatten(item, f"{prefix}.{key}" if prefix else key, target)
    elif isinstance(value, dict):
        for key, item in value.items():
            _flatten(item, f"{prefix}.{key}" if prefix else str(key), target)
    else:
        target[prefix] = value


def flatten_result(result: HemaTrendResult | KalmanStepResult) -> dict[str, Any]:
    flattened: dict[str, Any] = {}
    _flatten(result, "", flattened)
    return flattened


def flatten_results(results: Iterable[HemaTrendResult | KalmanStepResult]) -> list[dict[str, Any]]:
    return [flatten_result(result) for result in results]

