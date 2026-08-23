"""Strict UTC daily-range construction used by Phase 7 features."""

from __future__ import annotations

from collections.abc import Iterable
from datetime import UTC, datetime, timedelta
import math

from quasartrend.replay import HistoricalBar, Timeframe

from .models import AdrContext, AdrStatus


DAY_MS = 86_400_000
BAR_MS = 900_000


def utc_date(open_time: int) -> str:
    return datetime.fromtimestamp(open_time / 1000, tz=UTC).date().isoformat()


def _day_start(date: str) -> int:
    return int(datetime.fromisoformat(date).replace(tzinfo=UTC).timestamp() * 1000)


def validate_canonical_15m_bars(bars: Iterable[HistoricalBar]) -> tuple[HistoricalBar, ...]:
    """Reject rather than repair malformed, duplicate, or non-chronological data."""
    result = tuple(bars)
    prior: int | None = None
    symbol: str | None = None
    for bar in result:
        if bar.timeframe is not Timeframe.MINUTES_15:
            raise ValueError("ADR requires only canonical 15m bars")
        if bar.open_time % BAR_MS:
            raise ValueError("15m bars must be UTC-aligned")
        if symbol is None:
            symbol = bar.symbol
        elif bar.symbol != symbol:
            raise ValueError("research input must contain one symbol")
        if prior is not None and bar.open_time <= prior:
            raise ValueError("research bars must be in strict chronological order without duplicates")
        prior = bar.open_time
        if not all(math.isfinite(value) for value in (bar.open, bar.high, bar.low, bar.close)):
            raise ValueError("OHLC values must be finite")
        if bar.low > min(bar.open, bar.close) or bar.high < max(bar.open, bar.close):
            raise ValueError("OHLC envelope must contain open and close")
    return result


def daily_ranges(bars: Iterable[HistoricalBar]) -> dict[str, float | None]:
    """Return range only for exact 96-bar UTC sessions; bad dates are ``None``."""
    canonical = validate_canonical_15m_bars(bars)
    grouped: dict[str, list[HistoricalBar]] = {}
    for bar in canonical:
        grouped.setdefault(utc_date(bar.open_time), []).append(bar)
    result: dict[str, float | None] = {}
    for date, session in grouped.items():
        start = _day_start(date)
        expected = tuple(start + offset * BAR_MS for offset in range(96))
        times = tuple(bar.open_time for bar in session)
        if times != expected:
            result[date] = None
        else:
            result[date] = max(bar.high for bar in session) - min(bar.low for bar in session)
    return result


def adr_context_for_date(date: str, ranges: dict[str, float | None], lookback_days: int = 14) -> AdrContext:
    if lookback_days != 14:
        raise ValueError("Phase 7 ADR lookback is exactly 14 complete UTC dates")
    current = datetime.fromisoformat(date).date()
    requested = tuple((current - timedelta(days=offset)).isoformat() for offset in range(lookback_days, 0, -1))
    values = tuple(ranges.get(day) for day in requested)
    complete = sum(value is not None for value in values)
    if complete != lookback_days:
        # Warm-up means the requested calendar horizon precedes the first
        # observed session.  A hole *within* an otherwise old enough horizon
        # is an incomplete/missing prior session, never a shortened lookback.
        first_observed = min(ranges, default=None)
        status = AdrStatus.WARMUP if first_observed is None or first_observed > requested[0] else AdrStatus.INCOMPLETE_PRIOR_SESSION
        return AdrContext(date, None, status, complete)
    return AdrContext(date, sum(values) / lookback_days, AdrStatus.AVAILABLE, complete)  # type: ignore[arg-type]


def adr_contexts(bars: Iterable[HistoricalBar]) -> dict[str, AdrContext]:
    canonical = validate_canonical_15m_bars(bars)
    ranges = daily_ranges(canonical)
    return {date: adr_context_for_date(date, ranges) for date in sorted(ranges)}
