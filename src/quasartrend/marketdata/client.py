"""The narrow exchange-independent market-data client boundary."""

from __future__ import annotations

from typing import Protocol, runtime_checkable

from quasartrend.replay import HistoricalBar, Timeframe


@runtime_checkable
class MarketDataClient(Protocol):
    """Returns only canonical bars; exchange-specific payloads stay private.

    Implementations normalize retryable transport failures into
    :class:`MarketDataTransientError`; the runtime deliberately does not guess
    whether arbitrary client exceptions are safe to retry.
    """

    def fetch_bars(
        self,
        *,
        symbol: str,
        timeframe: Timeframe,
        start_open_time: int,
        end_open_time: int,
        limit: int,
    ) -> tuple[HistoricalBar, ...]:
        """Fetch bars whose open times fall in the inclusive requested range."""
