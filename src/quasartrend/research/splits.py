"""UTC chronological research splits; no randomization or boundary leakage."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

from .models import TradeRow


def utc_ms(date: str, *, end: bool = False) -> int:
    instant = datetime.fromisoformat(date).replace(tzinfo=UTC)
    return int((instant + (timedelta(days=1) if end else timedelta())).timestamp() * 1000) - (1 if end else 0)


@dataclass(frozen=True, slots=True)
class ChronologicalWindow:
    role: str
    start_date: str
    end_date: str

    @property
    def start_ms(self) -> int:
        return utc_ms(self.start_date)

    @property
    def end_ms(self) -> int:
        return utc_ms(self.end_date, end=True)


@dataclass(frozen=True, slots=True)
class WindowTradePartition:
    """Eligible closed outcomes and explicitly purged boundary observations."""

    window: ChronologicalWindow
    included: tuple[TradeRow, ...]
    purged_boundary_crossing: tuple[TradeRow, ...]
    censored: tuple[TradeRow, ...] = ()
    outside_window: tuple[TradeRow, ...] = ()


CANONICAL_WINDOWS = (
    ChronologicalWindow("development", "2026-05-15", "2026-07-09"),
    ChronologicalWindow("validation", "2026-07-10", "2026-07-28"),
    ChronologicalWindow("final_oos", "2026-07-29", "2026-08-16"),
)


def validate_windows(windows: tuple[ChronologicalWindow, ...] | list[ChronologicalWindow]) -> tuple[ChronologicalWindow, ...]:
    result = tuple(windows)
    prior_end: int | None = None
    roles: set[str] = set()
    for window in result:
        if window.role in roles:
            raise ValueError("duplicate window role")
        roles.add(window.role)
        if window.start_ms > window.end_ms:
            raise ValueError("window start must not be after end")
        if prior_end is not None and window.start_ms <= prior_end:
            raise ValueError("evaluation windows must be strictly non-overlapping and chronological")
        prior_end = window.end_ms
    return result


def split_trades(
    rows: tuple[TradeRow, ...] | list[TradeRow],
    windows: tuple[ChronologicalWindow, ...] | list[ChronologicalWindow],
) -> dict[str, WindowTradePartition]:
    """Compatibility name returning exhaustive partitions (never silent drops)."""
    return partition_trades(rows, windows)


def partition_trades(rows: tuple[TradeRow, ...] | list[TradeRow], windows: tuple[ChronologicalWindow, ...] | list[ChronologicalWindow]) -> dict[str, WindowTradePartition]:
    validated = validate_windows(windows)
    result: dict[str, list[TradeRow]] = {window.role: [] for window in validated}; purged: dict[str, list[TradeRow]] = {window.role: [] for window in validated}; censored: dict[str, list[TradeRow]] = {window.role: [] for window in validated}; outside: dict[str, list[TradeRow]] = {window.role: [] for window in validated}
    prior: int | None = None
    ids: set[str] = set()
    for row in rows:
        key=(row.decision_timestamp,row.source_processing_key,row.trade_id)
        if row.trade_id in ids: raise ValueError("duplicate trade IDs")
        ids.add(row.trade_id)
        if prior is not None and key <= prior:
            raise ValueError("trade rows must be supplied in deterministic chronological order")
        prior = key
        for window in validated:
            # Open/censored or boundary-crossing trade cannot contribute an outcome.
            if row.exit_timestamp is not None and window.start_ms <= row.decision_timestamp <= window.end_ms and window.start_ms <= row.exit_timestamp <= window.end_ms:
                result[window.role].append(row)
            elif row.exit_timestamp is not None and (
                window.start_ms <= row.decision_timestamp <= window.end_ms
                or window.start_ms <= row.exit_timestamp <= window.end_ms
            ):
                purged[window.role].append(row)
            elif row.exit_timestamp is None and window.start_ms <= row.decision_timestamp <= window.end_ms:
                censored[window.role].append(row)
            else:
                outside[window.role].append(row)
    return {window.role: WindowTradePartition(window, tuple(result[window.role]), tuple(purged[window.role]),tuple(censored[window.role]),tuple(outside[window.role]))
            for window in validated}


def walk_forward_windows(*, start_date: str, train_days: int, test_days: int, count: int) -> tuple[tuple[ChronologicalWindow, ChronologicalWindow], ...]:
    if train_days <= 0 or test_days <= 0 or count <= 0:
        raise ValueError("walk-forward sizes must be positive")
    cursor = datetime.fromisoformat(start_date).date()
    result = []
    for index in range(count):
        train_start = cursor
        train_end = train_start + timedelta(days=train_days - 1)
        test_start = train_end + timedelta(days=1)
        test_end = test_start + timedelta(days=test_days - 1)
        result.append((ChronologicalWindow(f"wf_{index}_train", train_start.isoformat(), train_end.isoformat()),
                       ChronologicalWindow(f"wf_{index}_oos", test_start.isoformat(), test_end.isoformat())))
        cursor = test_end + timedelta(days=1)
    validate_windows(tuple(window for pair in result for window in pair))
    return tuple(result)
