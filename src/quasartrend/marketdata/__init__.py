"""Phase 5 market-data boundary."""

from .binance import BinanceUSDMClient
from .client import MarketDataClient
from .models import (
    MarketDataError,
    MarketDataGapError,
    MarketDataMalformedError,
    MarketDataPermanentError,
    MarketDataRetryExhaustedError,
    MarketDataTransientError,
)

__all__ = [
    "BinanceUSDMClient", "MarketDataClient", "MarketDataError", "MarketDataGapError",
    "MarketDataMalformedError", "MarketDataPermanentError", "MarketDataRetryExhaustedError",
    "MarketDataTransientError",
]
