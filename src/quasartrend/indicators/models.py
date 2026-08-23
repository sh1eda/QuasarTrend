"""Shared immutable input and output types for indicator calculations."""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from typing import TypeAlias


Timestamp: TypeAlias = int | str | None


class HemaRelation(str, Enum):
    """Relationship between the fast and slow HEMA values."""

    ABOVE = "above"
    BELOW = "below"
    EQUAL = "equal"
    UNAVAILABLE = "unavailable"


class TrendDirection(str, Enum):
    """Semantic direction exposed by an indicator."""

    BULLISH = "bullish"
    BEARISH = "bearish"


@dataclass(frozen=True, slots=True)
class Candle:
    """A single OHLCV observation; timestamp is diagnostic metadata only."""

    open: float
    high: float
    low: float
    close: float
    volume: float = float("nan")
    timestamp: Timestamp = None

