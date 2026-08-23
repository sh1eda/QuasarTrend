"""Configuration and explicit runtime-only errors for Phase 5."""

from __future__ import annotations

from dataclasses import dataclass
import math


@dataclass(frozen=True, slots=True)
class RuntimeConfig:
    """Live ingestion concerns only; replay and strategy configs remain frozen."""

    symbol: str
    poll_interval_seconds: float = 30.0
    bootstrap_15m: int = 600
    bootstrap_4h: int = 600
    request_page_size: int = 1_000
    max_catch_up_bars: int = 10_000
    retry_attempts: int = 4
    retry_base_delay_seconds: float = 0.2

    def __post_init__(self) -> None:
        if not isinstance(self.symbol, str) or not self.symbol:
            raise ValueError("symbol must be non-empty")
        for name in ("poll_interval_seconds", "retry_base_delay_seconds"):
            value = getattr(self, name)
            if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value) or value <= 0:
                raise ValueError(f"{name} must be finite and positive")
        for name in ("bootstrap_15m", "bootstrap_4h", "request_page_size", "max_catch_up_bars", "retry_attempts"):
            value = getattr(self, name)
            if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
                raise ValueError(f"{name} must be a positive integer")
        if self.request_page_size > 1_000:
            raise ValueError("request_page_size must not exceed 1000")
        if self.bootstrap_15m > self.max_catch_up_bars or self.bootstrap_4h > self.max_catch_up_bars:
            raise ValueError("bootstrap counts must not exceed max_catch_up_bars")


class RuntimePersistenceError(RuntimeError):
    """A replayed candle was not accepted because its checkpoint save failed."""
