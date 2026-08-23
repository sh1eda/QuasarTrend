"""Incremental port of the supplied HEMA Trend PineScript."""

from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Any, Iterable

from .models import Candle, HemaRelation, Timestamp, TrendDirection
from .moving_averages import PineEMA
from .pine import NA, checkpoint_float, crossover, crossunder, is_na, pine_gt, pine_lt, pine_round


CHECKPOINT_VERSION = 1


@dataclass(frozen=True, slots=True)
class HEMAResult:
    half_ema: float
    full_ema: float
    difference: float
    value: float


@dataclass(slots=True)
class HEMA:
    """One call site of the Pine ``f_hema`` function."""

    length: int
    half_ema: PineEMA | None = None
    full_ema: PineEMA | None = None
    final_ema: PineEMA | None = None

    def __post_init__(self) -> None:
        if isinstance(self.length, bool) or not isinstance(self.length, int) or self.length <= 0:
            raise ValueError("HEMA length must be a positive integer")
        if self.half_ema is None:
            self.half_ema = PineEMA(self.half_length)
        if self.full_ema is None:
            self.full_ema = PineEMA(self.length)
        if self.final_ema is None:
            self.final_ema = PineEMA(self.sqrt_length)
        if (
            self.half_ema.length != self.half_length
            or self.full_ema.length != self.length
            or self.final_ema.length != self.sqrt_length
        ):
            raise ValueError("HEMA EMA call-site lengths do not match configuration")

    @property
    def half_length(self) -> int:
        rounded = pine_round(self.length / 2.0)
        assert isinstance(rounded, int)
        return max(1, rounded)

    @property
    def sqrt_length(self) -> int:
        rounded = pine_round(math.sqrt(self.length))
        assert isinstance(rounded, int)
        return max(1, rounded)

    def update(self, source: float) -> HEMAResult:
        assert self.half_ema is not None and self.full_ema is not None and self.final_ema is not None
        half_value = self.half_ema.update(source)
        full_value = self.full_ema.update(source)
        difference = 2.0 * half_value - full_value
        final_value = self.final_ema.update(difference)
        return HEMAResult(
            half_ema=half_value,
            full_ema=full_value,
            difference=difference,
            value=final_value,
        )

    def to_checkpoint(self) -> dict[str, Any]:
        assert self.half_ema is not None and self.full_ema is not None and self.final_ema is not None
        return {
            "type": "HEMA",
            "version": CHECKPOINT_VERSION,
            "length": self.length,
            "half_ema": self.half_ema.to_checkpoint(),
            "full_ema": self.full_ema.to_checkpoint(),
            "final_ema": self.final_ema.to_checkpoint(),
        }

    @classmethod
    def from_checkpoint(cls, data: dict[str, Any], *, expected_length: int | None = None) -> HEMA:
        if data.get("type") != "HEMA" or data.get("version") != CHECKPOINT_VERSION:
            raise ValueError("unsupported HEMA checkpoint")
        length = int(data["length"])
        if expected_length is not None and length != expected_length:
            raise ValueError("HEMA checkpoint length does not match configuration")
        probe = cls(length)
        return cls(
            length=length,
            half_ema=PineEMA.from_checkpoint(data["half_ema"], expected_length=probe.half_length),
            full_ema=PineEMA.from_checkpoint(data["full_ema"], expected_length=length),
            final_ema=PineEMA.from_checkpoint(data["final_ema"], expected_length=probe.sqrt_length),
        )


@dataclass(frozen=True, slots=True)
class HemaTrendResult:
    timestamp: Timestamp
    close: float
    fast: HEMAResult
    slow: HEMAResult
    previous_fast: float
    previous_slow: float
    relation: HemaRelation
    visual_direction: TrendDirection | None
    bullish_cross: bool
    bearish_cross: bool
    bullish_condition: bool
    bearish_condition: bool
    gray_condition: bool


@dataclass(slots=True)
class HemaTrend:
    """The two independent HEMA call sites and their Pine conditions."""

    fast_length: int = 20
    slow_length: int = 40
    fast: HEMA | None = None
    slow: HEMA | None = None
    previous_fast: float = NA
    previous_slow: float = NA

    def __post_init__(self) -> None:
        if self.fast is None:
            self.fast = HEMA(self.fast_length)
        if self.slow is None:
            self.slow = HEMA(self.slow_length)
        if self.fast.length != self.fast_length or self.slow.length != self.slow_length:
            raise ValueError("HemaTrend call-site lengths do not match configuration")
        if self.fast is self.slow:
            raise ValueError("fast and slow HEMA call sites must not share state")

    def update(self, candle: Candle) -> HemaTrendResult:
        assert self.fast is not None and self.slow is not None
        prior_fast = self.previous_fast
        prior_slow = self.previous_slow
        fast = self.fast.update(candle.close)
        slow = self.slow.update(candle.close)

        if is_na(fast.value) or is_na(slow.value):
            relation = HemaRelation.UNAVAILABLE
            visual_direction = None
        elif pine_gt(fast.value, slow.value):
            relation = HemaRelation.ABOVE
            visual_direction = TrendDirection.BULLISH
        elif pine_lt(fast.value, slow.value):
            relation = HemaRelation.BELOW
            visual_direction = TrendDirection.BEARISH
        else:
            relation = HemaRelation.EQUAL
            # Mirrors: hema1 > hema2 ? bullColor : bearColor.
            visual_direction = TrendDirection.BEARISH

        bullish_cross = crossover(fast.value, slow.value, prior_fast, prior_slow)
        bearish_cross = crossunder(fast.value, slow.value, prior_fast, prior_slow)
        bullish_condition = pine_gt(candle.close, fast.value) and pine_gt(fast.value, slow.value)
        bearish_condition = pine_lt(candle.close, fast.value) and pine_lt(fast.value, slow.value)
        gray_condition = (
            pine_lt(candle.close, fast.value) and pine_gt(fast.value, slow.value)
        ) or (
            pine_gt(candle.close, fast.value) and pine_lt(fast.value, slow.value)
        )

        self.previous_fast = fast.value
        self.previous_slow = slow.value
        return HemaTrendResult(
            timestamp=candle.timestamp,
            close=candle.close,
            fast=fast,
            slow=slow,
            previous_fast=prior_fast,
            previous_slow=prior_slow,
            relation=relation,
            visual_direction=visual_direction,
            bullish_cross=bullish_cross,
            bearish_cross=bearish_cross,
            bullish_condition=bullish_condition,
            bearish_condition=bearish_condition,
            gray_condition=gray_condition,
        )

    def to_checkpoint(self) -> dict[str, Any]:
        assert self.fast is not None and self.slow is not None
        return {
            "type": "HemaTrend",
            "version": CHECKPOINT_VERSION,
            "fast_length": self.fast_length,
            "slow_length": self.slow_length,
            "fast": self.fast.to_checkpoint(),
            "slow": self.slow.to_checkpoint(),
            "previous_fast": self.previous_fast,
            "previous_slow": self.previous_slow,
        }

    @classmethod
    def from_checkpoint(
        cls,
        data: dict[str, Any],
        *,
        expected_fast_length: int | None = None,
        expected_slow_length: int | None = None,
    ) -> HemaTrend:
        if data.get("type") != "HemaTrend" or data.get("version") != CHECKPOINT_VERSION:
            raise ValueError("unsupported HemaTrend checkpoint")
        fast_length = int(data["fast_length"])
        slow_length = int(data["slow_length"])
        if expected_fast_length is not None and fast_length != expected_fast_length:
            raise ValueError("fast HEMA checkpoint length does not match configuration")
        if expected_slow_length is not None and slow_length != expected_slow_length:
            raise ValueError("slow HEMA checkpoint length does not match configuration")
        return cls(
            fast_length=fast_length,
            slow_length=slow_length,
            fast=HEMA.from_checkpoint(data["fast"], expected_length=fast_length),
            slow=HEMA.from_checkpoint(data["slow"], expected_length=slow_length),
            previous_fast=checkpoint_float(data.get("previous_fast")),
            previous_slow=checkpoint_float(data.get("previous_slow")),
        )


def run_hema_batch(candles: Iterable[Candle], *, fast_length: int = 20, slow_length: int = 40) -> list[HemaTrendResult]:
    indicator = HemaTrend(fast_length=fast_length, slow_length=slow_length)
    return [indicator.update(candle) for candle in candles]

