"""Closed-candle polling runtime over the frozen ReplayEngine."""

from __future__ import annotations

from collections.abc import Callable, Iterator
from dataclasses import dataclass
import math
import time
from typing import Protocol, runtime_checkable

from quasartrend.marketdata import (
    MarketDataClient,
    MarketDataGapError,
    MarketDataMalformedError,
    MarketDataPermanentError,
    MarketDataRetryExhaustedError,
    MarketDataTransientError,
)
from quasartrend.persistence import PersistenceIdentity
from quasartrend.replay import HistoricalBar, ReplayEngine, ReplayState, ReplayTrace, Timeframe
from quasartrend.strategy import StrategyEvent

from .clock import Clock, SystemClock
from .models import RuntimeConfig, RuntimePersistenceError


@runtime_checkable
class CheckpointStore(Protocol):
    def load_checkpoint(self, identity: PersistenceIdentity) -> object | None:
        ...

    def save_checkpoint(self, identity: PersistenceIdentity, state: ReplayState) -> object:
        ...


@dataclass(frozen=True, slots=True)
class RuntimePollResult:
    processed_bars: tuple[HistoricalBar, ...]
    traces: tuple[ReplayTrace, ...]
    events: tuple[StrategyEvent, ...]
    state: ReplayState
    initialized_from_bootstrap: bool = False
    initialized_from_checkpoint: bool = False


class LiveRuntime:
    """State-owning Phase 5 adapter with candle-level durable advancement."""

    def __init__(
        self,
        config: RuntimeConfig,
        *,
        client: MarketDataClient,
        replay_engine: ReplayEngine,
        identity: PersistenceIdentity,
        store: CheckpointStore,
        clock: Clock | None = None,
        sleeper: Callable[[float], None] | None = None,
    ) -> None:
        if not isinstance(identity, PersistenceIdentity):
            raise TypeError("identity must be PersistenceIdentity")
        if identity.symbol != config.symbol:
            raise ValueError("RuntimeConfig symbol must match PersistenceIdentity symbol")
        if identity.replay_config != replay_engine.config:
            raise ValueError("PersistenceIdentity replay configuration must match ReplayEngine")
        if identity.strategy_config != replay_engine.strategy_engine.config:
            raise ValueError("PersistenceIdentity strategy configuration must match ReplayEngine")
        self.config = config
        self.client = client
        self.replay_engine = replay_engine
        self.identity = identity
        self.store = store
        self.clock = clock or SystemClock()
        self.sleeper = sleeper or time.sleep
        self._loaded = False
        self._state: ReplayState | None = None
        self._bootstrap_pending = False

    @property
    def state(self) -> ReplayState | None:
        return self._state

    def poll_once(self) -> RuntimePollResult:
        now_ms = self._now_ms()
        initialized_from_bootstrap = False
        initialized_from_checkpoint = False
        if not self._loaded:
            checkpoint = self.store.load_checkpoint(self.identity)
            self._loaded = True
            if checkpoint is None:
                self._state = self.replay_engine.initial_state(self.config.symbol)
                self._bootstrap_pending = True
                initialized_from_bootstrap = True
            else:
                state = getattr(checkpoint, "state", None)
                if not isinstance(state, ReplayState):
                    raise TypeError("checkpoint store returned an object without ReplayState state")
                self._state = state
                initialized_from_checkpoint = True
        assert self._state is not None

        bars_by_timeframe: list[HistoricalBar] = []
        for timeframe, bootstrap_count in (
            (Timeframe.MINUTES_15, self.config.bootstrap_15m),
            (Timeframe.HOURS_4, self.config.bootstrap_4h),
        ):
            bars_by_timeframe.extend(
                self._bars_for_timeframe(timeframe, now_ms, bootstrap_count, self._bootstrap_pending)
            )
        # The client must already be chronological within a timeframe.  Sorting
        # only merges independent valid feeds using the frozen replay ordering.
        ordered = tuple(sorted(bars_by_timeframe, key=lambda bar: bar.processing_key))
        candidate_state = self._state
        processed: list[HistoricalBar] = []
        traces: list[ReplayTrace] = []
        events: list[StrategyEvent] = []
        for bar in ordered:
            if candidate_state.chronology_cursor is not None and bar.processing_key <= candidate_state.chronology_cursor:
                continue
            stepped = self.replay_engine.step(candidate_state, bar)
            try:
                self.store.save_checkpoint(self.identity, stepped.state)
            except Exception as exc:
                # Do not make the candidate visible: it was never durable.
                self._state = candidate_state
                if processed:
                    # Earlier candles did become durable; their cursor is the
                    # only safe basis for the next poll, not a fresh bootstrap.
                    self._bootstrap_pending = False
                raise RuntimePersistenceError("checkpoint save failed; candle was not accepted") from exc
            candidate_state = stepped.state
            self._state = candidate_state
            processed.append(bar)
            traces.append(stepped.trace)
            events.extend(stepped.trace.events)
        if self._bootstrap_pending and processed:
            self._bootstrap_pending = False
        return RuntimePollResult(
            tuple(processed), tuple(traces), tuple(events), self._state,
            initialized_from_bootstrap, initialized_from_checkpoint,
        )

    def polling_loop(self, stop: Callable[[], bool]) -> Iterator[RuntimePollResult]:
        """Cooperative generator; it never hides fetch/persist failures."""
        while not stop():
            yield self.poll_once()
            if stop():
                return
            self.sleeper(self.config.poll_interval_seconds)

    def _bars_for_timeframe(
        self, timeframe: Timeframe, now_ms: int, bootstrap_count: int, bootstrap: bool
    ) -> tuple[HistoricalBar, ...]:
        current_open = self._current_open(timeframe, now_ms)
        latest = current_open - timeframe.duration_ms if now_ms >= timeframe.duration_ms else None
        if bootstrap:
            if latest is None:
                return self._fetch_complete_range(
                    timeframe, None, None, current_open, max_bars=self.config.max_catch_up_bars
                )
            first = latest - (bootstrap_count - 1) * timeframe.duration_ms
            if first < 0:
                first = 0
            return self._fetch_complete_range(
                timeframe, first, latest, current_open, max_bars=self.config.max_catch_up_bars
            )
        if latest is None:
            return self._fetch_complete_range(
                timeframe, None, None, current_open, max_bars=self.config.max_catch_up_bars
            )
        assert self._state is not None
        first_unconsumed = self._first_unconsumed_open(timeframe, self._state.chronology_cursor)
        # An overlap is intentionally requested when one exists.  It validates
        # at-least-once source retrieval while cursor suppression protects replay.
        overlap = first_unconsumed - timeframe.duration_ms
        if overlap < 0:
            overlap = 0
        if overlap > latest:
            # No new bar is legal.  Fetch the latest already-consumed bar if it
            # exists, exercising duplicate polling without manufacturing a gap.
            overlap = latest
        # The configured catch-up ceiling applies to new candles; the single
        # deliberately re-requested cursor overlap is not new work.
        allowed = self.config.max_catch_up_bars + (1 if overlap < first_unconsumed else 0)
        return self._fetch_complete_range(timeframe, overlap, latest, current_open, max_bars=allowed)

    @staticmethod
    def _current_open(timeframe: Timeframe, now_ms: int) -> int:
        return (now_ms // timeframe.duration_ms) * timeframe.duration_ms

    @staticmethod
    def _first_unconsumed_open(timeframe: Timeframe, cursor: tuple[int, int] | None) -> int:
        if cursor is None:
            return 0
        finalized_at, priority = cursor
        duration = timeframe.duration_ms
        quotient, remainder = divmod(finalized_at, duration)
        candidate_final = finalized_at if remainder == 0 else (quotient + 1) * duration
        if candidate_final == finalized_at and timeframe.priority <= priority:
            candidate_final += duration
        # Candle opens at its finalization time minus one duration.
        return max(0, candidate_final - duration)

    def _fetch_complete_range(
        self,
        timeframe: Timeframe,
        first: int | None,
        last: int | None,
        current_open: int,
        *,
        max_bars: int,
    ) -> tuple[HistoricalBar, ...]:
        duration = timeframe.duration_ms
        if (first is None) != (last is None):
            raise AssertionError("required market-data range must have both bounds or neither")
        expected_count = 0 if first is None else (last - first) // duration + 1
        if first is not None and (last - first) % duration:
            raise MarketDataGapError("runtime requested a non-aligned market-data range")
        if expected_count > max_bars:
            raise MarketDataGapError("market-data catch-up exceeds configured maximum")
        request_start = current_open if first is None else first
        collected: list[HistoricalBar] = []
        page_start = request_start
        while page_start <= current_open:
            page_end = min(current_open, page_start + (self.config.request_page_size - 1) * duration)
            page_limit = (page_end - page_start) // duration + 1
            page = self._fetch_with_retry(timeframe, page_start, page_end, page_limit)
            collected.extend(self._validate_page(
                page, timeframe, page_start, page_end, first, last, current_open,
            ))
            page_start = page_end + duration
        if len(collected) != expected_count:
            raise MarketDataGapError("market-data pages do not cover the requested range")
        return tuple(collected)

    def _fetch_with_retry(
        self, timeframe: Timeframe, start: int, end: int, limit: int
    ) -> tuple[HistoricalBar, ...]:
        for attempt in range(self.config.retry_attempts):
            try:
                return self.client.fetch_bars(
                    symbol=self.config.symbol, timeframe=timeframe,
                    start_open_time=start, end_open_time=end, limit=limit,
                )
            except MarketDataTransientError as exc:
                if attempt + 1 == self.config.retry_attempts:
                    raise MarketDataRetryExhaustedError("market-data transient retry budget exhausted") from exc
                delay = self.config.retry_base_delay_seconds * (2 ** attempt)
                if exc.retry_after_seconds is not None:
                    delay = max(delay, exc.retry_after_seconds)
                self.sleeper(delay)
            except (MarketDataMalformedError, MarketDataPermanentError):
                raise
        raise AssertionError("retry loop should return or raise")

    def _validate_page(
        self, page: tuple[HistoricalBar, ...], timeframe: Timeframe, start: int, end: int,
        required_first: int | None, required_last: int | None, current_open: int,
    ) -> tuple[HistoricalBar, ...]:
        if not isinstance(page, tuple):
            raise MarketDataMalformedError("MarketDataClient must return tuple[HistoricalBar, ...]")
        previous: int | None = None
        for bar in page:
            if not isinstance(bar, HistoricalBar):
                raise MarketDataMalformedError("MarketDataClient returned a non-HistoricalBar")
            if bar.symbol != self.config.symbol:
                raise MarketDataMalformedError("market-data bar symbol differs from runtime symbol")
            if bar.timeframe is not timeframe:
                raise MarketDataMalformedError("market-data bar timeframe differs from request")
            if bar.open_time % timeframe.duration_ms:
                raise MarketDataMalformedError("market-data bar open time is not timeframe-aligned")
            if bar.high < max(bar.open, bar.close) or bar.low > min(bar.open, bar.close):
                raise MarketDataMalformedError("market-data bar OHLC envelope is invalid")
            if not all(math.isfinite(value) for value in (bar.open, bar.high, bar.low, bar.close)):
                raise MarketDataMalformedError("market-data bar OHLC must be finite")
            if not start <= bar.open_time <= end:
                raise MarketDataMalformedError("market-data bar lies outside request")
            if bar.open_time > current_open:
                raise MarketDataMalformedError("market-data response includes a future candle")
            if previous is not None and bar.open_time <= previous:
                raise MarketDataGapError("market-data response has a duplicate or out-of-order candle")
            previous = bar.open_time
        required_opens: tuple[int, ...]
        if required_first is None or required_last is None:
            required_opens = ()
        else:
            lower = max(start, required_first)
            upper = min(end, required_last)
            required_opens = () if lower > upper else tuple(
                range(lower, upper + timeframe.duration_ms, timeframe.duration_ms)
            )
        actual_opens = tuple(bar.open_time for bar in page)
        if actual_opens[:len(required_opens)] != required_opens:
            raise MarketDataGapError("market-data response has a missing, duplicate, or out-of-order finalized candle")
        tail = actual_opens[len(required_opens):]
        if tail not in ((), (current_open,)):
            raise MarketDataMalformedError("market-data response has an unexpected non-finalized candle")
        return tuple(page[:len(required_opens)])

    def _now_ms(self) -> int:
        value = self.clock.now_ms()
        if isinstance(value, bool) or not isinstance(value, int) or value < 0:
            raise ValueError("clock.now_ms() must return a non-negative integer")
        return value
