"""Incremental Pine-compatible EMA, RMA, true range, and ATR."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from .models import Candle
from .pine import NA, checkpoint_float, is_na


CHECKPOINT_VERSION = 1


def _validate_length(length: int) -> None:
    if isinstance(length, bool) or not isinstance(length, int) or length <= 0:
        raise ValueError("length must be a positive integer")


@dataclass(slots=True)
class PineEMA:
    """Incremental equivalent of ``ta.ema`` for a fixed simple integer length."""

    length: int
    value: float = NA
    seed_values: list[float] | None = None
    observations: int = 0

    def __post_init__(self) -> None:
        _validate_length(self.length)
        if self.seed_values is None:
            self.seed_values = []

    @property
    def alpha(self) -> float:
        return 2.0 / (self.length + 1.0)

    def update(self, source: float) -> float:
        # Pine's built-in ignores na source values. Once initialized, the prior
        # EMA remains the value for that bar; before initialization it stays na.
        if is_na(source):
            return self.value
        source = float(source)
        self.observations += 1
        if is_na(self.value):
            assert self.seed_values is not None
            self.seed_values.append(source)
            if len(self.seed_values) == self.length:
                self.value = sum(self.seed_values) / self.length
                self.seed_values.clear()
        else:
            self.value = self.alpha * source + (1.0 - self.alpha) * self.value
        return self.value

    def to_checkpoint(self) -> dict[str, Any]:
        return {
            "type": "PineEMA",
            "version": CHECKPOINT_VERSION,
            "length": self.length,
            "value": self.value,
            "seed_values": list(self.seed_values or []),
            "observations": self.observations,
        }

    @classmethod
    def from_checkpoint(cls, data: dict[str, Any], *, expected_length: int | None = None) -> PineEMA:
        if data.get("type") != "PineEMA" or data.get("version") != CHECKPOINT_VERSION:
            raise ValueError("unsupported PineEMA checkpoint")
        length = int(data["length"])
        if expected_length is not None and length != expected_length:
            raise ValueError("PineEMA checkpoint length does not match configuration")
        # Checkpoints written before PineEMA collected its SMA seed do not
        # contain ``seed_values``. They represented an already initialized
        # EMA, so restoring an empty seed is backwards-compatible.
        return cls(
            length=length,
            value=checkpoint_float(data.get("value")),
            seed_values=[float(value) for value in data.get("seed_values", [])],
            observations=int(data["observations"]),
        )


@dataclass(slots=True)
class PineRMA:
    """Incremental equivalent of ``ta.rma`` with SMA seeding."""

    length: int
    value: float = NA
    seed_values: list[float] | None = None
    observations: int = 0

    def __post_init__(self) -> None:
        _validate_length(self.length)
        if self.seed_values is None:
            self.seed_values = []

    @property
    def alpha(self) -> float:
        return 1.0 / self.length

    def update(self, source: float) -> float:
        if is_na(source):
            return self.value
        source = float(source)
        self.observations += 1
        if is_na(self.value):
            assert self.seed_values is not None
            self.seed_values.append(source)
            if len(self.seed_values) == self.length:
                self.value = sum(self.seed_values) / self.length
                self.seed_values.clear()
        else:
            self.value = self.alpha * source + (1.0 - self.alpha) * self.value
        return self.value

    def to_checkpoint(self) -> dict[str, Any]:
        return {
            "type": "PineRMA",
            "version": CHECKPOINT_VERSION,
            "length": self.length,
            "value": self.value,
            "seed_values": list(self.seed_values or []),
            "observations": self.observations,
        }

    @classmethod
    def from_checkpoint(cls, data: dict[str, Any], *, expected_length: int | None = None) -> PineRMA:
        if data.get("type") != "PineRMA" or data.get("version") != CHECKPOINT_VERSION:
            raise ValueError("unsupported PineRMA checkpoint")
        length = int(data["length"])
        if expected_length is not None and length != expected_length:
            raise ValueError("PineRMA checkpoint length does not match configuration")
        return cls(
            length=length,
            value=checkpoint_float(data.get("value")),
            seed_values=[float(value) for value in data.get("seed_values", [])],
            observations=int(data["observations"]),
        )


@dataclass(frozen=True, slots=True)
class ATRResult:
    true_range: float
    atr: float
    previous_close: float


@dataclass(slots=True)
class PineATR:
    """Incremental ``ta.atr(length) == ta.rma(ta.tr(true), length)``."""

    length: int
    rma: PineRMA | None = None
    previous_close: float = NA

    def __post_init__(self) -> None:
        _validate_length(self.length)
        if self.rma is None:
            self.rma = PineRMA(self.length)
        elif self.rma.length != self.length:
            raise ValueError("ATR and RMA lengths must match")

    def update(self, candle: Candle) -> ATRResult:
        previous_close = self.previous_close
        if is_na(candle.high) or is_na(candle.low):
            true_range = NA
        elif is_na(previous_close):
            true_range = candle.high - candle.low
        else:
            true_range = max(
                candle.high - candle.low,
                abs(candle.high - previous_close),
                abs(candle.low - previous_close),
            )
        assert self.rma is not None
        atr = self.rma.update(true_range)
        self.previous_close = float(candle.close)
        return ATRResult(true_range=true_range, atr=atr, previous_close=previous_close)

    def to_checkpoint(self) -> dict[str, Any]:
        assert self.rma is not None
        return {
            "type": "PineATR",
            "version": CHECKPOINT_VERSION,
            "length": self.length,
            "previous_close": self.previous_close,
            "rma": self.rma.to_checkpoint(),
        }

    @classmethod
    def from_checkpoint(cls, data: dict[str, Any], *, expected_length: int | None = None) -> PineATR:
        if data.get("type") != "PineATR" or data.get("version") != CHECKPOINT_VERSION:
            raise ValueError("unsupported PineATR checkpoint")
        length = int(data["length"])
        if expected_length is not None and length != expected_length:
            raise ValueError("PineATR checkpoint length does not match configuration")
        return cls(
            length=length,
            rma=PineRMA.from_checkpoint(data["rma"], expected_length=length),
            previous_close=checkpoint_float(data.get("previous_close")),
        )
