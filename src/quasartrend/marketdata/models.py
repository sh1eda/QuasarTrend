"""Validated market-data boundary records and errors for Phase 5."""

from __future__ import annotations


class MarketDataError(RuntimeError):
    """Base error for a market-data request or its canonical response."""


class MarketDataPermanentError(MarketDataError):
    """A request cannot succeed unchanged (for example a 4xx response)."""


class MarketDataMalformedError(MarketDataPermanentError):
    """The exchange response is not a valid canonical kline response."""


class MarketDataTransientError(MarketDataError):
    """An idempotent request may succeed when retried."""

    def __init__(self, message: str, *, retry_after_seconds: float | None = None) -> None:
        super().__init__(message)
        if retry_after_seconds is not None and retry_after_seconds < 0:
            raise ValueError("retry_after_seconds must be non-negative")
        self.retry_after_seconds = retry_after_seconds


class MarketDataRetryExhaustedError(MarketDataError):
    """The runtime exhausted its bounded transient-request retry budget."""


class MarketDataGapError(MarketDataError):
    """A required finalized candle is absent from a requested cadence."""
