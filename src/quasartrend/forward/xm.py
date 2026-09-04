"""Append-only XM forward evidence journals and a demo-only safety boundary.

No broker API is imported here.  Journal entries are observational evidence;
they cannot contain realised PnL or turn a signal into an order.
"""
from __future__ import annotations

from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from hashlib import sha256
import json
import math
import os
from pathlib import Path
from typing import Any, Mapping


def _canonical(value: Any) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False)


def config_hash(config: Any) -> str:
    """Stable configuration identity independent of mapping insertion order."""
    if hasattr(config, "__dataclass_fields__"):
        config = asdict(config)
    return sha256(_canonical(config).encode("utf-8")).hexdigest()


def _iso_utc_from_msc(value: int) -> str:
    return datetime.fromtimestamp(value / 1000, UTC).isoformat(timespec="milliseconds").replace("+00:00", "Z")


def _parse_utc(value: str) -> int:
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except (TypeError, ValueError) as error:
        raise ValueError("time_utc must be ISO-8601 UTC") from error
    if parsed.tzinfo is None or parsed.utcoffset() is None or parsed.utcoffset().total_seconds() != 0:
        raise ValueError("time_utc must use the UTC offset")
    return int(parsed.timestamp() * 1000)


def _number(value: Any, name: str, *, required: bool = False) -> float | None:
    if value is None and not required:
        return None
    if isinstance(value, bool):
        raise TypeError(f"{name} must be finite numeric")
    try:
        result = float(value)
    except (TypeError, ValueError) as error:
        raise TypeError(f"{name} must be finite numeric") from error
    if not math.isfinite(result):
        raise ValueError(f"{name} must be finite")
    return result


@dataclass(frozen=True, slots=True)
class TickAppendResult:
    appended: bool
    duplicate: bool
    gap_ms: int | None


class TickJournal:
    """A restart-safe JSONL journal whose only mutation is appending one row."""

    schema_version = "xm-forward-tick-journal/v1"

    def __init__(self, path: Path, *, source_server: str, symbol: str, environment_id: str, capture_version: str, max_gap_ms: int | None = None) -> None:
        if not source_server or not symbol or not capture_version:
            raise ValueError("source_server, symbol, and capture_version are required")
        if not isinstance(environment_id, str) or len(environment_id) != 64 or any(character not in "0123456789abcdef" for character in environment_id):
            raise ValueError("environment_id must be a lowercase SHA-256 pseudonymous terminal/account identity")
        if max_gap_ms is not None and (isinstance(max_gap_ms, bool) or max_gap_ms < 0):
            raise ValueError("max_gap_ms must be nonnegative or None")
        self.path, self.source_server, self.symbol, self.environment_id, self.capture_version = Path(path), source_server, symbol, environment_id, capture_version
        self.max_gap_ms = max_gap_ms
        self._identities: set[str] = set()
        self._last_time_msc: int | None = None
        self.gaps: list[dict[str, int]] = []
        self._load_existing()

    def _load_existing(self) -> None:
        if not self.path.exists():
            return
        with self.path.open("r", encoding="utf-8") as handle:
            for ordinal, line in enumerate(handle, 1):
                if not line.endswith("\n"):
                    raise ValueError("tick journal has a partial final row")
                try:
                    row = json.loads(line)
                except json.JSONDecodeError as error:
                    raise ValueError(f"invalid tick journal row {ordinal}") from error
                identity = _canonical(row)
                if identity in self._identities:
                    raise ValueError("existing tick journal contains duplicate full rows")
                self._validate_row(row)
                self._accept_existing(row, identity)

    def _validate_row(self, row: Mapping[str, Any]) -> None:
        required = {"schema_version", "source_server", "symbol", "environment_id", "capture_version", "time_msc", "time_utc", "bid", "ask", "spread"}
        if set(row) - {"schema_version", "source_server", "symbol", "environment_id", "capture_version", "time_msc", "time_utc", "bid", "ask", "spread", "last", "volume", "volume_real", "flags"} or not required <= set(row):
            raise ValueError("tick journal row schema mismatch")
        if row["schema_version"] != self.schema_version or row["source_server"] != self.source_server or row["symbol"] != self.symbol or row["environment_id"] != self.environment_id or row["capture_version"] != self.capture_version:
            raise ValueError("tick journal provenance mismatch")
        if isinstance(row["time_msc"], bool) or not isinstance(row["time_msc"], int) or row["time_msc"] < 0:
            raise ValueError("time_msc must be a nonnegative integer")
        if _parse_utc(row["time_utc"]) != row["time_msc"]:
            raise ValueError("time_utc and time_msc disagree")
        bid, ask, spread = _number(row["bid"], "bid", required=True), _number(row["ask"], "ask", required=True), _number(row["spread"], "spread", required=True)
        if bid < 0 or ask < 0 or spread < 0 or not math.isclose(spread, ask - bid, rel_tol=0.0, abs_tol=1e-12):
            raise ValueError("tick bid/ask/spread mismatch")
        for key in ("last", "volume", "volume_real"):
            if key in row:
                _number(row[key], key)
        if "flags" in row and (isinstance(row["flags"], bool) or not isinstance(row["flags"], int)):
            raise ValueError("flags must be an integer when present")

    def _accept_existing(self, row: Mapping[str, Any], identity: str) -> None:
        timestamp = row["time_msc"]
        if self._last_time_msc is not None and timestamp < self._last_time_msc:
            raise ValueError("tick journal chronological regression")
        self._record_gap(timestamp)
        self._identities.add(identity)
        self._last_time_msc = timestamp

    def _record_gap(self, timestamp: int) -> int | None:
        if self._last_time_msc is None:
            return None
        gap = timestamp - self._last_time_msc
        if self.max_gap_ms is not None and gap > self.max_gap_ms:
            self.gaps.append({"after_time_msc": self._last_time_msc, "before_time_msc": timestamp, "gap_ms": gap})
            return gap
        return None

    def append(self, *, time_msc: int, time_utc: str | None = None, bid: float, ask: float, spread: float | None = None, last: float | None = None, volume: float | None = None, volume_real: float | None = None, flags: int | None = None, source_server: str | None = None, symbol: str | None = None, environment_id: str | None = None, capture_version: str | None = None) -> TickAppendResult:
        if ((source_server is not None and source_server != self.source_server) or (symbol is not None and symbol != self.symbol) or (environment_id is not None and environment_id != self.environment_id) or (capture_version is not None and capture_version != self.capture_version)):
            raise ValueError("tick append provenance differs from this journal")
        if isinstance(time_msc, bool) or not isinstance(time_msc, int) or time_msc < 0:
            raise ValueError("time_msc must be a nonnegative integer")
        row: dict[str, Any] = {"schema_version": self.schema_version, "source_server": self.source_server, "symbol": self.symbol, "environment_id": self.environment_id, "capture_version": self.capture_version, "time_msc": time_msc, "time_utc": time_utc or _iso_utc_from_msc(time_msc), "bid": bid, "ask": ask, "spread": ask - bid if spread is None else spread}
        for key, value in (("last", last), ("volume", volume), ("volume_real", volume_real), ("flags", flags)):
            if value is not None:
                row[key] = value
        self._validate_row(row)
        identity = _canonical(row)
        if identity in self._identities:
            return TickAppendResult(False, True, None)
        if self._last_time_msc is not None and time_msc < self._last_time_msc:
            raise ValueError("tick chronological regression")
        gap = None
        if self._last_time_msc is not None and self.max_gap_ms is not None and time_msc - self._last_time_msc > self.max_gap_ms:
            gap = time_msc - self._last_time_msc
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.path.open("a", encoding="utf-8") as handle:
            handle.write(identity + "\n")
            handle.flush()
            os.fsync(handle.fileno())
        self._identities.add(identity)
        if gap is not None:
            self.gaps.append({"after_time_msc": self._last_time_msc, "before_time_msc": time_msc, "gap_ms": gap})
        self._last_time_msc = time_msc
        return TickAppendResult(True, False, gap)

    def provenance_snapshot(self) -> dict[str, Any]:
        digest = sha256(self.path.read_bytes()).hexdigest() if self.path.exists() else sha256(b"").hexdigest()
        return {"schema_version": self.schema_version, "source_server": self.source_server, "symbol": self.symbol, "environment_id": self.environment_id, "capture_version": self.capture_version, "journal_sha256": digest, "last_time_msc": self._last_time_msc, "last_time_utc": None if self._last_time_msc is None else _iso_utc_from_msc(self._last_time_msc), "gap_count": len(self.gaps)}


class SignalJournal:
    """Append-only prospective signal evidence; outcome/economic fields are invalid."""

    schema_version = "xm-forward-signal-journal/v1"
    _required = {"schema_version", "source_server", "symbol", "environment_id", "signal_id", "strategy_version", "config_hash", "bar_timestamp", "signal_timestamp", "direction", "theoretical_bid_entry", "stop", "intended_exit_logic", "frozen_state"}
    _prohibited = {"outcome", "pnl", "r", "gross_strategy_r", "net_observed_r", "exit_price", "realized"}

    def __init__(self, path: Path, *, source_server: str, symbol: str, environment_id: str) -> None:
        if not source_server or not symbol:
            raise ValueError("source_server and symbol are required")
        if not isinstance(environment_id, str) or len(environment_id) != 64 or any(character not in "0123456789abcdef" for character in environment_id):
            raise ValueError("environment_id must be a lowercase SHA-256 pseudonymous terminal/account identity")
        self.path = Path(path)
        self.source_server, self.symbol, self.environment_id = source_server, symbol, environment_id
        self._by_id: dict[str, str] = {}
        self._last_timestamp: int | None = None
        self._load_existing()

    def _load_existing(self) -> None:
        if not self.path.exists():
            return
        with self.path.open("r", encoding="utf-8") as handle:
            for ordinal, line in enumerate(handle, 1):
                if not line.endswith("\n"):
                    raise ValueError("signal journal has a partial final row")
                try:
                    row = json.loads(line)
                except json.JSONDecodeError as error:
                    raise ValueError(f"invalid signal journal row {ordinal}") from error
                self._validate(row)
                identity = _canonical(row)
                signal_id = row["signal_id"]
                if signal_id in self._by_id:
                    raise ValueError("existing signal journal contains duplicate signal ID")
                self._accept(row, identity)

    def _validate(self, row: Mapping[str, Any]) -> None:
        if set(row) != self._required or self._prohibited & set(row):
            raise ValueError("signal journal schema forbids outcome/economic fields")
        if row["schema_version"] != self.schema_version or not isinstance(row["signal_id"], str) or not row["signal_id"]:
            raise ValueError("invalid signal journal identity")
        if row["source_server"] != self.source_server or row["symbol"] != self.symbol or row["environment_id"] != self.environment_id:
            raise ValueError("signal journal provenance mismatch")
        for key in ("strategy_version", "config_hash", "direction", "intended_exit_logic"):
            if not isinstance(row[key], str) or not row[key]:
                raise ValueError(f"{key} is required")
        if row["direction"] not in {"long", "short"}:
            raise ValueError("signal direction must be long or short")
        if len(row["config_hash"]) != 64 or any(character not in "0123456789abcdef" for character in row["config_hash"]):
            raise ValueError("config_hash must be a lowercase SHA-256")
        for key in ("bar_timestamp", "signal_timestamp"):
            if isinstance(row[key], bool) or not isinstance(row[key], int):
                raise ValueError(f"{key} must be integer epoch milliseconds")
        if row["signal_timestamp"] < row["bar_timestamp"]:
            raise ValueError("signal timestamp cannot precede its finalized bar")
        _number(row["theoretical_bid_entry"], "theoretical_bid_entry", required=True)
        _number(row["stop"], "stop", required=True)
        if not isinstance(row["frozen_state"], Mapping):
            raise ValueError("frozen_state must be an object")

    def _accept(self, row: Mapping[str, Any], identity: str) -> None:
        timestamp = row["signal_timestamp"]
        if self._last_timestamp is not None and timestamp < self._last_timestamp:
            raise ValueError("signal journal chronological regression")
        self._by_id[row["signal_id"]] = identity
        self._last_timestamp = timestamp

    def append(self, row: Mapping[str, Any]) -> bool:
        material = dict(row)
        self._validate(material)
        identity, signal_id = _canonical(material), material["signal_id"]
        prior = self._by_id.get(signal_id)
        if prior is not None:
            if prior == identity:
                return False
            raise ValueError("conflicting duplicate signal ID")
        if self._last_timestamp is not None and material["signal_timestamp"] < self._last_timestamp:
            raise ValueError("signal chronological regression")
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.path.open("a", encoding="utf-8") as handle:
            handle.write(identity + "\n")
            handle.flush()
            os.fsync(handle.fileno())
        self._accept(material, identity)
        return True


@dataclass(frozen=True, slots=True)
class GrossNetAccounting:
    gross_strategy_r: float
    observed_spread_r: float | None = None
    observed_commission_r: float | None = None
    observed_slippage_r: float | None = None
    observed_swap_r: float | None = None

    def __post_init__(self) -> None:
        for name in ("gross_strategy_r", "observed_spread_r", "observed_commission_r", "observed_slippage_r", "observed_swap_r"):
            value = getattr(self, name)
            if value is not None:
                _number(value, name, required=True)

    @property
    def net_observed_r(self) -> float | None:
        costs = (self.observed_spread_r, self.observed_commission_r, self.observed_slippage_r, self.observed_swap_r)
        return None if any(value is None for value in costs) else self.gross_strategy_r - sum(float(value) for value in costs)


def separate_gross_and_net(*, gross_strategy_r: float, observed_spread_r: float | None, observed_commission_r: float | None, observed_slippage_r: float | None, observed_swap_r: float | None) -> GrossNetAccounting:
    return GrossNetAccounting(gross_strategy_r, observed_spread_r, observed_commission_r, observed_slippage_r, observed_swap_r)


class DemoOnlyExecutionGuard:
    """Explicitly rejects live trading; this checkpoint has no order path."""

    def __init__(self, environment: str) -> None:
        if not isinstance(environment, str):
            raise TypeError("environment must be a string")
        self.environment = environment

    def assert_demo_only(self) -> None:
        if self.environment.strip().upper() not in {"DEMO", "PAPER", "SIMULATION"}:
            raise PermissionError("only explicit DEMO/PAPER/SIMULATION environments are permitted")

    def place_order(self, *_: Any, **__: Any) -> None:
        self.assert_demo_only()
        raise RuntimeError("demo execution adapter is not connected; no order was placed")
