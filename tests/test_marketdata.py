from __future__ import annotations

from io import BytesIO
from urllib.error import HTTPError, URLError
from urllib.parse import parse_qs, urlparse

import pytest

from quasartrend.marketdata import (
    BinanceUSDMClient,
    MarketDataMalformedError,
    MarketDataPermanentError,
    MarketDataTransientError,
)
from quasartrend.replay import Timeframe


def _row(open_time: int, timeframe: Timeframe = Timeframe.MINUTES_15) -> list[object]:
    return [open_time, "100", "102", "99", "101", "12.5", open_time + timeframe.duration_ms - 1, "0", 0, "0", "0", "0"]


class _Response:
    def __init__(self, payload: bytes) -> None:
        self.payload = payload

    def read(self) -> bytes:
        return self.payload


def test_binance_kline_maps_domain_symbol_and_exact_request() -> None:
    requests = []

    def transport(request, timeout):  # type: ignore[no-untyped-def]
        requests.append((request, timeout))
        return _Response(b'[[0,"100","102","99","101","12.5",899999,"0",0,"0","0","0"]]')

    client = BinanceUSDMClient({"BINANCE:BTCUSDT.P": "BTCUSDT"}, transport=transport)
    bars = client.fetch_bars(symbol="BINANCE:BTCUSDT.P", timeframe=Timeframe.MINUTES_15, start_open_time=0, end_open_time=0, limit=1)
    assert bars[0].symbol == "BINANCE:BTCUSDT.P"
    assert bars[0].timeframe is Timeframe.MINUTES_15
    assert bars[0].open_time == 0 and bars[0].volume == 12.5
    query = parse_qs(urlparse(requests[0][0].full_url).query)
    assert query == {"symbol": ["BTCUSDT"], "interval": ["15m"], "startTime": ["0"], "endTime": ["0"], "limit": ["1"]}


@pytest.mark.parametrize(
    "payload",
    [
        b'[[0,"100","102","99","101","1",899999,"0",0,"0","0"]]',
        b'[[1,"100","102","99","101","1",900000,"0",0,"0","0","0"]]',
        b'[[0,"100","102","99","101","1",1,"0",0,"0","0","0"]]',
        b'[[0,"100","100","101","101","1",899999,"0",0,"0","0","0"]]',
        b'[[0,"NaN","102","99","101","1",899999,"0",0,"0","0","0"]]',
        b'[[0,"0","102","99","101","1",899999,"0",0,"0","0","0"]]',
        b'{"code":-1121,"msg":"bad symbol"}',
    ],
)
def test_binance_rejects_malformed_payloads(payload: bytes) -> None:
    client = BinanceUSDMClient({"BTC": "BTCUSDT"}, transport=lambda *_: _Response(payload))
    with pytest.raises(MarketDataMalformedError):
        client.fetch_bars(symbol="BTC", timeframe=Timeframe.MINUTES_15, start_open_time=0, end_open_time=0, limit=1)


def test_binance_rejects_duplicate_or_out_of_order_rows_and_bad_requests() -> None:
    payload = ("[" + ",".join(str(_row(value)).replace("'", '"') for value in (900_000, 0)) + "]").encode()
    client = BinanceUSDMClient({"BTC": "BTCUSDT"}, transport=lambda *_: _Response(payload))
    with pytest.raises(MarketDataMalformedError, match="chronological"):
        client.fetch_bars(symbol="BTC", timeframe=Timeframe.MINUTES_15, start_open_time=0, end_open_time=900_000, limit=2)
    with pytest.raises(MarketDataPermanentError, match="configured"):
        client.fetch_bars(symbol="ETH", timeframe=Timeframe.MINUTES_15, start_open_time=0, end_open_time=0, limit=1)
    with pytest.raises(MarketDataPermanentError, match="aligned"):
        client.fetch_bars(symbol="BTC", timeframe=Timeframe.MINUTES_15, start_open_time=1, end_open_time=1, limit=1)


@pytest.mark.parametrize("status", [408, 429, 500, 503])
def test_binance_transient_http_errors_are_classified(status: int) -> None:
    def transport(*_):  # type: ignore[no-untyped-def]
        raise HTTPError("https://example.invalid", status, "error", {"Retry-After": "2"}, BytesIO())

    client = BinanceUSDMClient({"BTC": "BTCUSDT"}, transport=transport)
    with pytest.raises(MarketDataTransientError) as error:
        client.fetch_bars(symbol="BTC", timeframe=Timeframe.MINUTES_15, start_open_time=0, end_open_time=0, limit=1)
    assert error.value.retry_after_seconds == 2.0


@pytest.mark.parametrize("status", [400, 418, 404])
def test_binance_permanent_http_errors_are_not_transient(status: int) -> None:
    def transport(*_):  # type: ignore[no-untyped-def]
        raise HTTPError("https://example.invalid", status, "error", None, BytesIO())

    client = BinanceUSDMClient({"BTC": "BTCUSDT"}, transport=transport)
    with pytest.raises(MarketDataPermanentError):
        client.fetch_bars(symbol="BTC", timeframe=Timeframe.MINUTES_15, start_open_time=0, end_open_time=0, limit=1)


@pytest.mark.parametrize("failure", [TimeoutError(), URLError("offline")])
def test_binance_timeout_and_url_errors_are_transient(failure: Exception) -> None:
    def transport(*_):  # type: ignore[no-untyped-def]
        raise failure

    client = BinanceUSDMClient({"BTC": "BTCUSDT"}, transport=transport)
    with pytest.raises(MarketDataTransientError):
        client.fetch_bars(symbol="BTC", timeframe=Timeframe.MINUTES_15, start_open_time=0, end_open_time=0, limit=1)
