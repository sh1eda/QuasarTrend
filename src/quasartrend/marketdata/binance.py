"""Binance USD-M public kline adapter, isolated from the live runtime."""

from __future__ import annotations

from collections.abc import Callable, Mapping
import json
import math
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import Request, urlopen

from quasartrend.replay import HistoricalBar, Timeframe

from .models import MarketDataMalformedError, MarketDataPermanentError, MarketDataTransientError


Transport = Callable[[Request, float], object]


def _retry_after(headers: object) -> float | None:
    if headers is None:
        return None
    try:
        value = headers.get("Retry-After")
    except AttributeError:
        return None
    if value is None:
        return None
    try:
        parsed = float(value)
    except (TypeError, ValueError):
        return None
    return parsed if math.isfinite(parsed) and parsed >= 0 else None


def _integer(value: object, field: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise MarketDataMalformedError(f"Binance kline {field} must be an integer")
    return value


def _finite_number(value: object, field: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (str, int, float)):
        raise MarketDataMalformedError(f"Binance kline {field} must be numeric")
    try:
        number = float(value)
    except (TypeError, ValueError) as exc:
        raise MarketDataMalformedError(f"Binance kline {field} must be numeric") from exc
    if not math.isfinite(number):
        raise MarketDataMalformedError(f"Binance kline {field} must be finite")
    return number


class BinanceUSDMClient:
    """Canonical Binance USD-M ``/fapi/v1/klines`` adapter.

    ``symbol_map`` deliberately maps a QuasarTrend domain symbol to the exact
    exchange symbol.  Returned bars retain the domain symbol.
    """

    endpoint = "/fapi/v1/klines"

    def __init__(
        self,
        symbol_map: Mapping[str, str],
        *,
        base_url: str = "https://fapi.binance.com",
        timeout_seconds: float = 10.0,
        transport: Transport | None = None,
    ) -> None:
        if not isinstance(base_url, str) or not base_url.startswith(("http://", "https://")):
            raise ValueError("base_url must be an HTTP(S) URL")
        if not math.isfinite(timeout_seconds) or timeout_seconds <= 0:
            raise ValueError("timeout_seconds must be finite and positive")
        normalized: dict[str, str] = {}
        for domain, exchange in symbol_map.items():
            if not isinstance(domain, str) or not domain or not isinstance(exchange, str) or not exchange:
                raise ValueError("symbol_map requires non-empty domain and exchange symbols")
            normalized[domain] = exchange
        self._symbol_map = normalized
        self.base_url = base_url.rstrip("/")
        self.timeout_seconds = timeout_seconds
        self._transport = transport or self._urllib_transport

    @staticmethod
    def _urllib_transport(request: Request, timeout: float) -> object:
        return urlopen(request, timeout=timeout)

    def fetch_bars(
        self,
        *,
        symbol: str,
        timeframe: Timeframe,
        start_open_time: int,
        end_open_time: int,
        limit: int,
    ) -> tuple[HistoricalBar, ...]:
        exchange_symbol = self._validate_request(symbol, timeframe, start_open_time, end_open_time, limit)
        query = urlencode({
            "symbol": exchange_symbol,
            "interval": timeframe.value,
            "startTime": start_open_time,
            "endTime": end_open_time,
            "limit": limit,
        })
        request = Request(f"{self.base_url}{self.endpoint}?{query}", method="GET")
        payload = self._request_payload(request)
        return self._canonicalize(payload, symbol, timeframe, start_open_time, end_open_time)

    def _validate_request(
        self, symbol: str, timeframe: Timeframe, start_open_time: int, end_open_time: int, limit: int
    ) -> str:
        if symbol not in self._symbol_map:
            raise MarketDataPermanentError("domain symbol is not configured for Binance")
        if not isinstance(timeframe, Timeframe):
            raise MarketDataPermanentError("unsupported timeframe")
        for name, value in (("start_open_time", start_open_time), ("end_open_time", end_open_time)):
            if isinstance(value, bool) or not isinstance(value, int) or value < 0:
                raise MarketDataPermanentError(f"{name} must be a non-negative integer")
            if value % timeframe.duration_ms:
                raise MarketDataPermanentError(f"{name} must be aligned to timeframe")
        if end_open_time < start_open_time:
            raise MarketDataPermanentError("end_open_time precedes start_open_time")
        if isinstance(limit, bool) or not isinstance(limit, int) or not 1 <= limit <= 1_500:
            raise MarketDataPermanentError("limit must be an integer in Binance's supported range")
        return self._symbol_map[symbol]

    def _request_payload(self, request: Request) -> object:
        try:
            response = self._transport(request, self.timeout_seconds)
            raw = response if isinstance(response, bytes) else response.read()  # type: ignore[union-attr]
        except HTTPError as exc:
            if exc.code in (408, 429) or 500 <= exc.code <= 599:
                raise MarketDataTransientError(
                    f"Binance HTTP {exc.code}", retry_after_seconds=_retry_after(exc.headers)
                ) from exc
            raise MarketDataPermanentError(f"Binance HTTP {exc.code}") from exc
        except (URLError, TimeoutError, ConnectionError, OSError) as exc:
            raise MarketDataTransientError("Binance transport failure") from exc
        except Exception as exc:
            raise MarketDataMalformedError("Binance transport did not return readable bytes") from exc
        if not isinstance(raw, bytes):
            raise MarketDataMalformedError("Binance response must be bytes")
        try:
            return json.loads(raw.decode("utf-8"), parse_constant=lambda value: (_ for _ in ()).throw(ValueError(value)))
        except (UnicodeDecodeError, json.JSONDecodeError, ValueError) as exc:
            raise MarketDataMalformedError("Binance response is not strict JSON") from exc

    @staticmethod
    def _canonicalize(
        payload: object,
        symbol: str,
        timeframe: Timeframe,
        start_open_time: int,
        end_open_time: int,
    ) -> tuple[HistoricalBar, ...]:
        if not isinstance(payload, list):
            raise MarketDataMalformedError("Binance kline payload must be an array")
        bars: list[HistoricalBar] = []
        previous_open: int | None = None
        for row in payload:
            if not isinstance(row, list) or len(row) != 12:
                raise MarketDataMalformedError("Binance kline rows must have exactly 12 fields")
            open_time = _integer(row[0], "open time")
            close_time = _integer(row[6], "close time")
            if open_time < 0 or open_time % timeframe.duration_ms:
                raise MarketDataMalformedError("Binance kline open time is not aligned")
            if close_time != open_time + timeframe.duration_ms - 1:
                raise MarketDataMalformedError("Binance kline close time does not match interval")
            if not start_open_time <= open_time <= end_open_time:
                raise MarketDataMalformedError("Binance kline is outside the requested window")
            if previous_open is not None and open_time <= previous_open:
                raise MarketDataMalformedError("Binance klines must be strictly chronological without duplicates")
            open_ = _finite_number(row[1], "open")
            high = _finite_number(row[2], "high")
            low = _finite_number(row[3], "low")
            close = _finite_number(row[4], "close")
            volume = _finite_number(row[5], "volume")
            if (
                open_ <= 0 or high <= 0 or low <= 0 or close <= 0
                or volume < 0 or high < max(open_, close) or low > min(open_, close) or high < low
            ):
                raise MarketDataMalformedError("Binance kline OHLC envelope is invalid")
            try:
                bars.append(HistoricalBar(symbol, timeframe, open_time, open_, high, low, close, volume))
            except (TypeError, ValueError) as exc:
                raise MarketDataMalformedError("Binance kline cannot form a HistoricalBar") from exc
            previous_open = open_time
        return tuple(bars)
