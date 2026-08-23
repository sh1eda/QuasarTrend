"""Incremental port of the supplied Kalman Step Signals PineScript."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Iterable

from .models import Candle, Timestamp, TrendDirection
from .moving_averages import PineATR
from .pine import (
    NA,
    checkpoint_float,
    crossover,
    crossunder,
    is_na,
    nz,
    pine_eq,
    pine_gt,
    pine_lt,
)


CHECKPOINT_VERSION = 1


@dataclass(frozen=True, slots=True)
class KalmanResult:
    previous_close: float
    v1: float
    v2: float
    v3: float
    v4: float
    v5: float


@dataclass(slots=True)
class KalmanFilter:
    """Literal statement-ordered port of the Pine ``kalman`` function."""

    period: int = 21
    alpha: float = 0.01
    beta: float = 0.1
    v1: float = NA
    v2: float = 1.0
    v3: float | None = None
    v4: float = 0.0
    v5: float = NA
    previous_close: float = NA

    def __post_init__(self) -> None:
        if isinstance(self.period, bool) or not isinstance(self.period, int) or self.period <= 0:
            raise ValueError("Kalman period must be a positive integer")
        if self.v3 is None:
            self.v3 = self.alpha * self.period
        expected_v3 = self.alpha * self.period
        if self.v3 != expected_v3:
            raise ValueError("Kalman v3 does not match alpha * period")

    def update(self, close: float) -> KalmanResult:
        previous_close = self.previous_close

        # Preserve Pine statement order exactly. In particular, v2 mutates on
        # the first bar even though v1/v5 and the returned estimate remain na.
        if is_na(self.v1):
            self.v1 = previous_close
        self.v5 = self.v1
        assert self.v3 is not None
        self.v4 = self.v2 / (self.v2 + self.v3)
        self.v1 = self.v5 + self.v4 * (close - self.v5)
        self.v2 = (1.0 - self.v4) * self.v2 + self.beta / self.period

        self.previous_close = float(close)
        return KalmanResult(
            previous_close=previous_close,
            v1=self.v1,
            v2=self.v2,
            v3=self.v3,
            v4=self.v4,
            v5=self.v5,
        )

    def to_checkpoint(self) -> dict[str, Any]:
        return {
            "type": "KalmanFilter",
            "version": CHECKPOINT_VERSION,
            "period": self.period,
            "alpha": self.alpha,
            "beta": self.beta,
            "v1": self.v1,
            "v2": self.v2,
            "v3": self.v3,
            "v4": self.v4,
            "v5": self.v5,
            "previous_close": self.previous_close,
        }

    @classmethod
    def from_checkpoint(
        cls,
        data: dict[str, Any],
        *,
        expected_period: int | None = None,
        expected_alpha: float | None = None,
        expected_beta: float | None = None,
    ) -> KalmanFilter:
        if data.get("type") != "KalmanFilter" or data.get("version") != CHECKPOINT_VERSION:
            raise ValueError("unsupported KalmanFilter checkpoint")
        period = int(data["period"])
        alpha = float(data["alpha"])
        beta = float(data["beta"])
        if expected_period is not None and period != expected_period:
            raise ValueError("Kalman checkpoint period does not match configuration")
        if expected_alpha is not None and alpha != expected_alpha:
            raise ValueError("Kalman checkpoint alpha does not match configuration")
        if expected_beta is not None and beta != expected_beta:
            raise ValueError("Kalman checkpoint beta does not match configuration")
        return cls(
            period=period,
            alpha=alpha,
            beta=beta,
            v1=checkpoint_float(data.get("v1")),
            v2=float(data["v2"]),
            v3=checkpoint_float(data.get("v3")),
            v4=float(data["v4"]),
            v5=checkpoint_float(data.get("v5")),
            previous_close=checkpoint_float(data.get("previous_close")),
        )


@dataclass(frozen=True, slots=True)
class KalmanStepResult:
    timestamp: Timestamp
    close: float
    kalman: KalmanResult
    true_range: float
    atr: float
    previous_atr: float
    raw_upper_band: float
    raw_lower_band: float
    previous_upper_band: float
    previous_lower_band: float
    lower_uses_raw: bool
    upper_uses_raw: bool
    upper_band: float
    lower_band: float
    previous_supertrend: float
    supertrend: float
    previous_direction: float
    direction: int
    semantic_direction: TrendDirection
    bullish_transition: bool
    bearish_transition: bool


@dataclass(slots=True)
class KalmanStep:
    """One isolated Kalman call site plus the custom Pine Supertrend call site."""

    kalman_period: int = 21
    kalman_alpha: float = 0.01
    kalman_beta: float = 0.1
    factor: float = 1.0
    atr_period: int = 7
    kalman: KalmanFilter | None = None
    atr: PineATR | None = None
    previous_k: float = NA
    previous_atr: float = NA
    previous_lower_band: float = NA
    previous_upper_band: float = NA
    previous_supertrend: float = NA
    previous_direction: float = NA

    def __post_init__(self) -> None:
        if isinstance(self.atr_period, bool) or not isinstance(self.atr_period, int) or self.atr_period <= 0:
            raise ValueError("Supertrend ATR period must be a positive integer")
        if self.kalman is None:
            self.kalman = KalmanFilter(self.kalman_period, self.kalman_alpha, self.kalman_beta)
        if self.atr is None:
            self.atr = PineATR(self.atr_period)
        if (
            self.kalman.period != self.kalman_period
            or self.kalman.alpha != self.kalman_alpha
            or self.kalman.beta != self.kalman_beta
        ):
            raise ValueError("Kalman call-site state does not match configuration")
        if self.atr.length != self.atr_period:
            raise ValueError("ATR call-site state does not match configuration")

    def update(self, candle: Candle) -> KalmanStepResult:
        assert self.kalman is not None and self.atr is not None
        kalman = self.kalman.update(candle.close)
        atr_result = self.atr.update(candle)
        k = kalman.v1
        atr = atr_result.atr

        raw_upper_band = k + self.factor * atr
        raw_lower_band = k - self.factor * atr
        previous_lower_band = nz(self.previous_lower_band)
        previous_upper_band = nz(self.previous_upper_band)

        # These are deliberately written in the same left-to-right, lazy form
        # as Pine v6's expressions.
        lower_uses_raw = pine_gt(raw_lower_band, previous_lower_band) or pine_lt(
            self.previous_k, previous_lower_band
        )
        lower_band = raw_lower_band if lower_uses_raw else previous_lower_band
        upper_uses_raw = pine_lt(raw_upper_band, previous_upper_band) or pine_gt(
            self.previous_k, previous_upper_band
        )
        upper_band = raw_upper_band if upper_uses_raw else previous_upper_band

        previous_supertrend = self.previous_supertrend
        if is_na(self.previous_atr):
            direction = 1
        elif pine_eq(previous_supertrend, previous_upper_band):
            direction = -1 if pine_gt(k, upper_band) else 1
        else:
            direction = 1 if pine_lt(k, lower_band) else -1
        supertrend = lower_band if direction == -1 else upper_band

        bullish_transition = crossunder(
            float(direction), 0.0, self.previous_direction, 0.0
        )
        bearish_transition = crossover(
            float(direction), 0.0, self.previous_direction, 0.0
        )
        semantic_direction = TrendDirection.BULLISH if direction < 0 else TrendDirection.BEARISH

        result = KalmanStepResult(
            timestamp=candle.timestamp,
            close=candle.close,
            kalman=kalman,
            true_range=atr_result.true_range,
            atr=atr,
            previous_atr=self.previous_atr,
            raw_upper_band=raw_upper_band,
            raw_lower_band=raw_lower_band,
            previous_upper_band=previous_upper_band,
            previous_lower_band=previous_lower_band,
            lower_uses_raw=lower_uses_raw,
            upper_uses_raw=upper_uses_raw,
            upper_band=upper_band,
            lower_band=lower_band,
            previous_supertrend=previous_supertrend,
            supertrend=supertrend,
            previous_direction=self.previous_direction,
            direction=direction,
            semantic_direction=semantic_direction,
            bullish_transition=bullish_transition,
            bearish_transition=bearish_transition,
        )

        self.previous_k = k
        self.previous_atr = atr
        self.previous_lower_band = lower_band
        self.previous_upper_band = upper_band
        self.previous_supertrend = supertrend
        self.previous_direction = float(direction)
        return result

    def to_checkpoint(self) -> dict[str, Any]:
        assert self.kalman is not None and self.atr is not None
        return {
            "type": "KalmanStep",
            "version": CHECKPOINT_VERSION,
            "kalman_period": self.kalman_period,
            "kalman_alpha": self.kalman_alpha,
            "kalman_beta": self.kalman_beta,
            "factor": self.factor,
            "atr_period": self.atr_period,
            "kalman": self.kalman.to_checkpoint(),
            "atr": self.atr.to_checkpoint(),
            "previous_k": self.previous_k,
            "previous_atr": self.previous_atr,
            "previous_lower_band": self.previous_lower_band,
            "previous_upper_band": self.previous_upper_band,
            "previous_supertrend": self.previous_supertrend,
            "previous_direction": self.previous_direction,
        }

    @classmethod
    def from_checkpoint(
        cls,
        data: dict[str, Any],
        *,
        expected_kalman_period: int | None = None,
        expected_kalman_alpha: float | None = None,
        expected_kalman_beta: float | None = None,
        expected_factor: float | None = None,
        expected_atr_period: int | None = None,
    ) -> KalmanStep:
        if data.get("type") != "KalmanStep" or data.get("version") != CHECKPOINT_VERSION:
            raise ValueError("unsupported KalmanStep checkpoint")
        period = int(data["kalman_period"])
        alpha = float(data["kalman_alpha"])
        beta = float(data["kalman_beta"])
        factor = float(data["factor"])
        atr_period = int(data["atr_period"])
        expected = (
            (expected_kalman_period, period, "period"),
            (expected_kalman_alpha, alpha, "alpha"),
            (expected_kalman_beta, beta, "beta"),
            (expected_factor, factor, "factor"),
            (expected_atr_period, atr_period, "ATR period"),
        )
        for wanted, actual, label in expected:
            if wanted is not None and wanted != actual:
                raise ValueError(f"KalmanStep checkpoint {label} does not match configuration")
        return cls(
            kalman_period=period,
            kalman_alpha=alpha,
            kalman_beta=beta,
            factor=factor,
            atr_period=atr_period,
            kalman=KalmanFilter.from_checkpoint(
                data["kalman"],
                expected_period=period,
                expected_alpha=alpha,
                expected_beta=beta,
            ),
            atr=PineATR.from_checkpoint(data["atr"], expected_length=atr_period),
            previous_k=checkpoint_float(data.get("previous_k")),
            previous_atr=checkpoint_float(data.get("previous_atr")),
            previous_lower_band=checkpoint_float(data.get("previous_lower_band")),
            previous_upper_band=checkpoint_float(data.get("previous_upper_band")),
            previous_supertrend=checkpoint_float(data.get("previous_supertrend")),
            previous_direction=checkpoint_float(data.get("previous_direction")),
        )


def run_kalman_batch(
    candles: Iterable[Candle],
    *,
    kalman_period: int = 21,
    kalman_alpha: float = 0.01,
    kalman_beta: float = 0.1,
    factor: float = 1.0,
    atr_period: int = 7,
) -> list[KalmanStepResult]:
    indicator = KalmanStep(
        kalman_period=kalman_period,
        kalman_alpha=kalman_alpha,
        kalman_beta=kalman_beta,
        factor=factor,
        atr_period=atr_period,
    )
    return [indicator.update(candle) for candle in candles]

