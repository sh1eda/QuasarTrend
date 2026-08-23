"""UTC epoch-millisecond clocks used by the deterministic runtime."""

from __future__ import annotations

import time
from typing import Protocol, runtime_checkable


@runtime_checkable
class Clock(Protocol):
    def now_ms(self) -> int:
        """Return an epoch UTC millisecond value."""


class SystemClock:
    def now_ms(self) -> int:
        return time.time_ns() // 1_000_000
