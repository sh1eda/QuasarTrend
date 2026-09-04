"""Fail-closed XM MT5 forward capture for the frozen V1 replay.

The MetaTrader5 package is intentionally imported only by :func:`load_mt5`.
This module has no ``order_send`` path: execution evidence is useful now, but
position-size semantics are deliberately owned by Sol/main.
"""
from __future__ import annotations

from dataclasses import asdict, dataclass, is_dataclass
from datetime import UTC, datetime
from hashlib import sha256
import importlib
import json
import os
from pathlib import Path
import time
from typing import Any, Callable, Iterable, Mapping

from quasartrend.persistence import decode_replay_state, encode_replay_state
from quasartrend.replay import HistoricalBar, ReplayEngine, ReplayState, Timeframe
from quasartrend.research.xm_gold_historical_validation import verify_frozen_production_sources
from quasartrend.strategy import Direction, EventType

SERVER = "XMGlobal-MT5 18"
SYMBOL = "GOLD"
ORDER_BLOCKER = "DEMO ORDER SUBMISSION BLOCKED — POSITION SIZE SEMANTICS REQUIRE SOL/MAIN DECISION"
IDENTITY_BLOCKER = "XM DEMO EXECUTION: BLOCKED — DEMO ACCOUNT IDENTITY NOT PROVEN"
FROZEN_V1_COMMIT = "c58e18ef545909184267342eff712dd08bf47dda"
FROZEN_V1_MANIFEST_SHA256 = "a6b02c8056c9996eb3bcac64a18588251f9a7c741f6c208eacbdc82de15f3e6d"
HOLDOUT_START_MS = 1787950680000  # 2026-08-28T20:58:00Z
HOLDOUT_END_MS = 1803942000000  # 2027-03-01T23:00:00Z


def load_mt5() -> Any:
    """Load the official dependency at runtime, not while importing the package."""
    try:
        return importlib.import_module("MetaTrader5")
    except ImportError as exc:  # pragma: no cover - depends on Windows VDS
        raise RuntimeError("official MetaTrader5 package is required on the MT5 VDS") from exc


def _value(record: Any, name: str, default: Any = None) -> Any:
    value = default
    if isinstance(record, Mapping): value = record.get(name, default)
    elif name in (getattr(getattr(record, "dtype", None), "names", None) or ()):
        try: value = record[name]
        except (KeyError, IndexError, TypeError): value = default
    elif hasattr(record, name): value = getattr(record, name)
    elif hasattr(record, "__getitem__"):
        try: value = record[name]
        except (KeyError, IndexError, TypeError): value = default
    return value.item() if hasattr(value, "item") else value


def _row(record: Any, names: Iterable[str]) -> dict[str, Any]:
    return {name: _json(_value(record, name)) for name in names if _value(record, name) is not None}


def _json(value: Any) -> Any:
    if is_dataclass(value):
        return _json(asdict(value))
    if isinstance(value, Mapping):
        return {str(key): _json(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_json(item) for item in value]
    if hasattr(value, "item"):
        return value.item()
    return value


def _canonical(row: Mapping[str, Any]) -> str:
    return json.dumps(_json(row), sort_keys=True, separators=(",", ":"), allow_nan=False)


def utc_iso(ms: int) -> str:
    return datetime.fromtimestamp(ms / 1000, UTC).isoformat(timespec="milliseconds").replace("+00:00", "Z")


def environment_pseudonym(server: str, login: Any) -> str:
    """A stable non-reversible identifier; raw login must never reach a journal."""
    return sha256(f"{server}:{login}".encode()).hexdigest()


def implementation_hash() -> str:
    """Bind forward evidence to the exact source implementing its admission rules."""
    return sha256(Path(__file__).read_bytes()).hexdigest()


@dataclass(frozen=True, slots=True)
class CapabilityAudit:
    server: str | None
    environment_id: str | None
    demo_proven: bool
    execution_mode: str
    allowed: bool
    blocker: str | None
    snapshot: Mapping[str, Any]


def audit_capabilities(mt5: Any, *, execution_mode: str = "none", server: str = SERVER, symbol: str = SYMBOL) -> CapabilityAudit:
    """Read-only exact-server/symbol audit. It never calls order submission APIs."""
    terminal, account = mt5.terminal_info(), mt5.account_info()
    info = mt5.symbol_info(symbol)
    connection = bool(_value(terminal, "connected", False))
    actual_server = _value(account, "server")
    login = _value(account, "login")
    demo_constant = _value(mt5, "ACCOUNT_TRADE_MODE_DEMO")
    trade_mode = _value(account, "trade_mode")
    account_expert = _value(account, "trade_expert")
    flags = {
        "account_trade_allowed": _value(account, "trade_allowed"),
        "terminal_trade_allowed": _value(terminal, "trade_allowed"),
        "expert_enabled": _value(terminal, "tradeapi_disabled") is False,
    }
    # Broker-side expert permission is distinct from the terminal API toggle;
    # missing evidence is not permission to trade.
    flags["account_trade_expert"] = account_expert
    disabled_mode = _value(mt5, "SYMBOL_TRADE_MODE_DISABLED")
    symbol_tradeable = disabled_mode is not None and _value(info, "trade_mode") is not None and _value(info, "trade_mode") != disabled_mode
    version = getattr(mt5, "version", lambda: None)()
    snapshot = {
        "schema_version": "xm-mt5-capability-audit/v1", "terminal_connected": connection,
        "mt5_version": _json(version),
        "terminal": _row(terminal, ("build", "connected", "trade_allowed", "tradeapi_disabled", "community_account")),
        "account": _row(account, ("server", "trade_mode", "trade_allowed", "currency", "currency_digits", "margin_mode", "company")),
        "symbol": _row(info, ("name", "visible", "trade_mode", "order_mode", "filling_mode", "volume_min", "volume_max", "volume_step", "trade_stops_level", "trade_freeze_level", "trade_contract_size", "digits", "point", "trade_tick_size", "trade_tick_value", "currency_base", "currency_profit", "currency_margin", "swap_mode")),
        "flags": flags,
    }
    exact = actual_server == server and bool(info) and _value(info, "name") == symbol and bool(_value(info, "visible", False)) and symbol_tradeable
    demo = demo_constant is not None and trade_mode == demo_constant
    flags_ok = all(value is True for value in flags.values())
    proven = connection and exact and demo and flags_ok and login is not None
    mode_ok = execution_mode == "demo"
    blocker = None if proven and mode_ok else IDENTITY_BLOCKER
    if proven and not mode_ok:
        blocker = "XM DEMO EXECUTION: BLOCKED — EXPLICIT --execution-mode demo REQUIRED"
    return CapabilityAudit(actual_server, environment_pseudonym(actual_server, login) if actual_server and login is not None else None, proven, execution_mode, proven and mode_ok, blocker, snapshot)


class JsonlJournal:
    """Strict append-only, fsync-backed evidence journal with exact IDs."""
    def __init__(self, path: Path, *, schema: str, provenance: Mapping[str, Any], time_field: str, id_field: str) -> None:
        self.path, self.schema, self.provenance = Path(path), schema, dict(provenance)
        self.time_field, self.id_field = time_field, id_field
        self._ids: dict[str, str] = {}; self._rows: dict[str, Mapping[str, Any]] = {}; self.last_time: int | None = None
        self._load()

    def _load(self) -> None:
        if not self.path.exists(): return
        with self.path.open(encoding="utf-8") as stream:
            for number, line in enumerate(stream, 1):
                if not line.endswith("\n"): raise ValueError(f"partial journal row {number}")
                try: row = json.loads(line)
                except json.JSONDecodeError as exc: raise ValueError(f"invalid journal row {number}") from exc
                self._validate(row); self._accept(row, _canonical(row))

    def _validate(self, row: Mapping[str, Any]) -> None:
        if row.get("schema_version") != self.schema or any(row.get(k) != v for k, v in self.provenance.items()):
            raise ValueError("journal schema/provenance mismatch")
        stamp, identity = row.get(self.time_field), row.get(self.id_field)
        if isinstance(stamp, bool) or not isinstance(stamp, int) or stamp < 0: raise ValueError("journal timestamp invalid")
        if not isinstance(identity, str) or not identity: raise ValueError("journal identity invalid")

    def _accept(self, row: Mapping[str, Any], encoded: str) -> None:
        stamp, identity = row[self.time_field], row[self.id_field]
        if self.last_time is not None and stamp < self.last_time: raise ValueError("journal chronological regression")
        previous = self._ids.get(identity)
        if previous is not None: raise ValueError("duplicate journal identity")
        self._ids[identity] = encoded; self.last_time = stamp
        self._rows[identity] = dict(row)

    def append(self, row: Mapping[str, Any]) -> bool:
        material = {"schema_version": self.schema, **self.provenance, **_json(row)}
        self._validate(material); encoded, identity = _canonical(material), material[self.id_field]
        previous = self._ids.get(identity)
        if previous is not None:
            if previous == encoded: return False
            raise ValueError("conflicting duplicate journal identity")
        if self.last_time is not None and material[self.time_field] < self.last_time: raise ValueError("journal chronological regression")
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.path.open("a", encoding="utf-8") as stream:
            stream.write(encoded + "\n"); stream.flush(); os.fsync(stream.fileno())
        self._accept(material, encoded); return True

    @property
    def count(self) -> int: return len(self._ids)

    def contains(self, identity: str) -> bool:
        return identity in self._ids

    def latest(self, **criteria: Any) -> Mapping[str, Any] | None:
        found = [row for row in self._rows.values() if all(row.get(k) == v for k, v in criteria.items())]
        return None if not found else max(found, key=lambda row: row[self.time_field])

    @property
    def rows(self) -> tuple[Mapping[str, Any], ...]:
        return tuple(self._rows.values())


@dataclass(frozen=True, slots=True)
class ExecutionResult:
    execution_id: str
    status: str
    blocker: str | None
    reconciled: bool = False


class DemoExecutionAdapter:
    """Evidence/reconciliation boundary. Deliberately incapable of submitting orders."""
    def __init__(self, mt5: Any, execution_journal: JsonlJournal, audit: CapabilityAudit) -> None:
        self.mt5, self.execution_journal, self.audit = mt5, execution_journal, audit

    def reconcile(self, execution_id: str) -> bool:
        # Only read broker history; an ambiguous acknowledgement must never retry.
        orders = getattr(self.mt5, "orders_get", lambda **_: ())(symbol=SYMBOL) or ()
        until = datetime.now(UTC); since = datetime.fromtimestamp(until.timestamp() - 7 * 86_400, UTC)
        deals = getattr(self.mt5, "history_deals_get", lambda *_, **__: ())(since, until, group=SYMBOL) or ()
        return any(execution_id in str(item) for item in (*orders, *deals))

    def submit(self, signal_id: str, *, ambiguous_ack: bool = False, request: Mapping[str, Any] | None = None) -> ExecutionResult:
        execution_id = sha256(f"{signal_id}:v1-demo-execution".encode()).hexdigest()
        # A durable prior attempt owns this signal even if a caller restarts.
        if self.execution_journal.contains(execution_id):
            return ExecutionResult(execution_id, "blocked", ORDER_BLOCKER, False)
        reconciled = self.reconcile(execution_id) if ambiguous_ack else False
        status = "reconciled" if reconciled else "blocked"
        self.execution_journal.append({"execution_id": execution_id, "request_timestamp": int(time.time() * 1000), "signal_id": signal_id, "status": status, "blocker": ORDER_BLOCKER, "requested": _json(request or {}), "observed": {"order_id": None, "deal_id": None, "fill_price": None, "commission": None, "swap": None, "fee": None, "deal_reason": None, "deal_timestamp": None}, "gross_strategy_r": None, "observed_execution_net_r": None})
        return ExecutionResult(execution_id, status, ORDER_BLOCKER, reconciled)


class Family1LongOnlyShadow:
    """Separate signal recorder. Its public surface intentionally has no order method."""
    def __init__(self, journal: JsonlJournal) -> None: self.journal = journal
    def record(self, signal: Mapping[str, Any]) -> bool:
        direction = signal["direction"]
        return self.journal.append({"shadow_id": sha256((signal["signal_id"] + ":family-1-long-only").encode()).hexdigest(), "decision_timestamp": signal["signal_timestamp"], "source_signal_id": signal["signal_id"], "candidate": "Family-1 long_only", "direction": direction, "decision": "admit" if direction == "long" else "reject", "reason": "long_only" if direction == "short" else "v1_long_opportunity", "context": signal})


class XMForwardService:
    """Polling capture using native finalized MT5 M15/H4 candles and frozen ReplayEngine."""
    def __init__(self, root: Path, *, mt5: Any | None = None, execution_mode: str = "none", terminal_path: str | Path | None = None, repo_root: Path | None = None, activate: bool = True, max_signal_lag_ms: int = 60_000, now: Callable[[], float] = time.time, sleeper: Callable[[float], None] = time.sleep) -> None:
        if isinstance(max_signal_lag_ms, bool) or not isinstance(max_signal_lag_ms, int) or max_signal_lag_ms <= 0:
            raise ValueError("max_signal_lag_ms must be a positive integer")
        self.mt5, self.root, self.now, self.sleeper = mt5 or load_mt5(), Path(root), now, sleeper
        initialized = self.mt5.initialize() if terminal_path is None else self.mt5.initialize(path=str(terminal_path))
        if not initialized:
            raise RuntimeError("XM MT5 initialization failed before capability audit")
        self.repo_root = Path(repo_root) if repo_root is not None else Path(__file__).resolve().parents[3]
        # Do not permit a forward run if any frozen V1/Pine source has drifted.
        self.frozen_v1_sources = verify_frozen_production_sources(self.repo_root)
        self.audit = audit_capabilities(self.mt5, execution_mode=execution_mode)
        if activate and not self.audit.allowed:
            raise PermissionError(self.audit.blocker or IDENTITY_BLOCKER)
        if not self.audit.environment_id: raise PermissionError(IDENTITY_BLOCKER)
        # High-volume journals bind compact identities only. The complete
        # verified source map is retained once in immutable audit evidence.
        p = {"source_server": SERVER, "symbol": SYMBOL, "environment_id": self.audit.environment_id, "capture_version": "xm-v1-forward/v1", "implementation_sha256": implementation_hash(), "frozen_v1_commit": FROZEN_V1_COMMIT, "frozen_v1_manifest_sha256": FROZEN_V1_MANIFEST_SHA256}
        base = self.root / "forward" / "xm" / SYMBOL
        self.ticks = JsonlJournal(base / "ticks" / "ticks.jsonl", schema="xm-forward-ticks/v1", provenance=p, time_field="time_msc", id_field="tick_id")
        self.m1 = JsonlJournal(base / "m1" / "bars.jsonl", schema="xm-forward-bars/v1", provenance=p, time_field="open_time", id_field="bar_id")
        self.bars = JsonlJournal(base / "bars" / "finalized.jsonl", schema="xm-forward-bars/v1", provenance=p, time_field="finalized_at", id_field="bar_id")
        self.gap_journals = {name: JsonlJournal(base / "gaps" / f"{name}.jsonl", schema="xm-forward-gap/v1", provenance=p, time_field="before_open", id_field="gap_id") for name in ("ticks", "m1", "m15", "h4")}
        self.signals = JsonlJournal(base / "signals" / "v1.jsonl", schema="xm-forward-v1-signals/v1", provenance=p, time_field="signal_timestamp", id_field="signal_id")
        self.shadow = Family1LongOnlyShadow(JsonlJournal(base / "signals" / "family1_long_only.jsonl", schema="xm-forward-family1-shadow/v1", provenance=p, time_field="decision_timestamp", id_field="shadow_id"))
        # Recover the only allowed split-write window: V1 was durable before
        # shadow. Reconstruct solely from its persisted prospective evidence.
        for signal in self.signals.rows:
            shadow_id = sha256((signal["signal_id"] + ":family-1-long-only").encode()).hexdigest()
            if not self.shadow.journal.contains(shadow_id): self.shadow.record(signal)
        self.execution = JsonlJournal(base / "execution" / "evidence.jsonl", schema="xm-forward-execution/v1", provenance=p, time_field="request_timestamp", id_field="execution_id")
        self.adapter = DemoExecutionAdapter(self.mt5, self.execution, self.audit)
        self.engine, self.state = ReplayEngine(), None
        self.checkpoint = base / "checkpoints" / "replay.json"; self.reconnects = self.duplicates = 0; self.started_at = int(now() * 1000); self.last_execution: str | None = None
        self.stale_quote = True; self._consecutive_failures = 0; self.max_signal_lag_ms = max_signal_lag_ms; self.missed_stale_signals = 0
        self.execution_mode = execution_mode; self.tick_saturation = 0
        self._capture_enabled = activate; self.activation_path = base / "service_activation.json"; self.activation_ms: int | None = None
        self._load_checkpoint()
        self._load_activation()
        self.latest_m15 = self.bars.latest(timeframe="m15")
        latest_h4 = self.bars.latest(timeframe="h4")
        self.latest_h4 = None if latest_h4 is None else {**latest_h4, "bias": None if self.state.latest_htf_bias is None else self.state.latest_htf_bias.value}
        self._persist_audit(base / "audit" / "capability.json")

    def _persist_audit(self, path: Path) -> None:
        payload = _canonical({"schema_version": "xm-mt5-capability-evidence/v1", "environment_id": self.audit.environment_id, "server": self.audit.server, "demo_proven": self.audit.demo_proven, "implementation_sha256": implementation_hash(), "frozen_v1_commit": FROZEN_V1_COMMIT, "frozen_v1_manifest_sha256": FROZEN_V1_MANIFEST_SHA256, "frozen_v1_sources": self.frozen_v1_sources, "snapshot": self.audit.snapshot})
        path.parent.mkdir(parents=True, exist_ok=True)
        if path.exists():
            if path.read_text(encoding="utf-8").rstrip("\n") != payload: raise ValueError("immutable capability snapshot differs from existing evidence")
            return
        temporary = path.with_suffix(".tmp")
        with temporary.open("w", encoding="utf-8") as stream:
            stream.write(payload + "\n"); stream.flush(); os.fsync(stream.fileno())
        os.replace(temporary, path)

    def _load_checkpoint(self) -> None:
        if self.checkpoint.exists(): self.state = decode_replay_state(self.checkpoint.read_text(), expected_config=self.engine.config)
        else: self.state = self.engine.initial_state(SYMBOL)

    def _load_activation(self) -> None:
        if not self.activation_path.exists(): return
        row = json.loads(self.activation_path.read_text(encoding="utf-8"))
        if set(row) != {"schema_version", "activation_ms", "activation_utc"} or row["schema_version"] != "xm-forward-activation/v1" or not isinstance(row["activation_ms"], int) or row["activation_utc"] != utc_iso(row["activation_ms"]):
            raise ValueError("invalid forward service activation evidence")
        self.activation_ms = row["activation_ms"]

    def _ensure_activation(self, now_ms: int) -> int:
        if self.activation_ms is not None: return self.activation_ms
        if not self._capture_enabled: raise PermissionError("forward capture is not enabled in audit-only mode")
        payload = _canonical({"schema_version": "xm-forward-activation/v1", "activation_ms": now_ms, "activation_utc": utc_iso(now_ms)})
        self.activation_path.parent.mkdir(parents=True, exist_ok=True)
        if self.activation_path.exists():
            self._load_activation(); assert self.activation_ms is not None; return self.activation_ms
        temporary = self.activation_path.with_suffix(".tmp")
        with temporary.open("w", encoding="utf-8") as stream:
            stream.write(payload + "\n"); stream.flush(); os.fsync(stream.fileno())
        os.replace(temporary, self.activation_path); self.activation_ms = now_ms
        return now_ms

    def _save_checkpoint(self) -> None:
        self.checkpoint.parent.mkdir(parents=True, exist_ok=True); temporary = self.checkpoint.with_suffix(".tmp")
        with temporary.open("w", encoding="utf-8") as stream:
            stream.write(encode_replay_state(self.state, expected_config=self.engine.config)); stream.flush(); os.fsync(stream.fileno())
        os.replace(temporary, self.checkpoint)

    def _rate_rows(self, timeframe: Any, name: str, duration_ms: int) -> list[Mapping[str, Any]]:
        now_ms = int(self.now() * 1000)
        journal = self.m1 if name == "m1" else self.bars
        previous = journal.latest(timeframe=name)
        if previous is not None and hasattr(self.mt5, "copy_rates_range"):
            start = max(0, int(previous["open_time"]) - duration_ms)
            rates = self.mt5.copy_rates_range(SYMBOL, timeframe, datetime.fromtimestamp(start / 1000, UTC), datetime.fromtimestamp(now_ms / 1000, UTC))
        else:
            rates = self.mt5.copy_rates_from_pos(SYMBOL, timeframe, 0, 600)
        if rates is None:
            raise RuntimeError(f"MT5 {name} rates API returned no data")
        # Wall-clock time cannot prove GOLD traded through a closure. A newer
        # captured native tick can: then a resumed range must at least reach
        # that tick's current/forming source candle, even though only closed
        # candles below are admitted to replay.
        raw_opens = [int(_value(rate, "time")) * 1000 for rate in rates]
        if previous is not None and self.ticks.last_time is not None:
            expected_open = (self.ticks.last_time // duration_ms) * duration_ms
            if expected_open > int(previous["open_time"]) and (not raw_opens or max(raw_opens) < expected_open):
                raise RuntimeError(f"resumed {name} range does not reach observed tick activity")
        finalized = []
        for rate in rates:
            open_time = int(_value(rate, "time")) * 1000
            if open_time + duration_ms > now_ms: continue
            row = {"bar_id": f"{name}:{open_time}", "open_time": open_time, "finalized_at": open_time + duration_ms, "time_utc": utc_iso(open_time), "finalized_utc": utc_iso(open_time + duration_ms), "timeframe": name, "open": float(_value(rate, "open")), "high": float(_value(rate, "high")), "low": float(_value(rate, "low")), "close": float(_value(rate, "close")), "tick_volume": _value(rate, "tick_volume"), "spread": _value(rate, "spread"), "real_volume": _value(rate, "real_volume")}
            finalized.append(row)
        finalized.sort(key=lambda row: row["open_time"])
        # A resumed range must include its persisted overlap. Internal cadence
        # gaps are journaled below because GOLD session closures are legitimate.
        if previous is not None and hasattr(self.mt5, "copy_rates_range"):
            prior_open = int(previous["open_time"])
            if not any(int(row["open_time"]) == prior_open for row in finalized):
                raise RuntimeError(f"resumed {name} range omitted persisted overlap")
        for left, right in zip(finalized, finalized[1:]):
            if right["open_time"] - left["open_time"] > duration_ms:
                self.gap_journals[name].append({"gap_id": f"{name}:{left['open_time']}:{right['open_time']}", "timeframe": name, "after_open": left["open_time"], "before_open": right["open_time"], "gap_ms": right["open_time"] - left["open_time"]})
        return finalized

    def poll_once(self) -> tuple[HistoricalBar, ...]:
        now_ms = int(self.now() * 1000)
        activation_ms = self._ensure_activation(now_ms)
        if not bool(_value(self.mt5.terminal_info(), "connected", False)):
            self.reconnects += 1
            if not self.mt5.initialize(): raise RuntimeError("MT5 reconnect failed")
        current_audit = audit_capabilities(self.mt5, execution_mode=self.execution_mode)
        if not current_audit.allowed or current_audit.environment_id != self.audit.environment_id:
            raise PermissionError(current_audit.blocker or IDENTITY_BLOCKER)
        tick_rows: list[Any] = []
        cursor_ms = self.ticks.last_time or int(self.now() * 1000) - 60_000
        for page_index in range(100):
            page = self.mt5.copy_ticks_from(SYMBOL, datetime.fromtimestamp(max(0, cursor_ms) / 1000, UTC), 10_000, self.mt5.COPY_TICKS_ALL)
            if page is None:
                raise RuntimeError("MT5 ticks API returned no data")
            page = list(page)
            if not page: break
            tick_rows.extend(page)
            timestamps = []
            for tick in page:
                raw_msc = _value(tick, "time_msc")
                timestamps.append(int(raw_msc if raw_msc is not None else int(_value(tick, "time")) * 1000))
            newest = max(timestamps)
            if len(page) < 10_000: break
            if newest <= cursor_ms:
                self.tick_saturation += 1
                self.gap_journals["ticks"].append({"gap_id": f"ticks-saturation:{cursor_ms}", "timeframe": "ticks", "after_open": cursor_ms, "before_open": cursor_ms, "gap_ms": 0, "reason": "full_page_no_time_progress"})
                raise RuntimeError("MT5 tick pagination saturated without time progress")
            if page_index == 99:
                self.tick_saturation += 1
                self.gap_journals["ticks"].append({"gap_id": f"ticks-saturation:page-limit:{cursor_ms}", "timeframe": "ticks", "after_open": cursor_ms, "before_open": cursor_ms, "gap_ms": 0, "reason": "page_limit_reached"})
                raise RuntimeError("MT5 tick pagination page limit reached")
            cursor_ms = newest + 1
        for tick in tick_rows:
            raw_msc = _value(tick, "time_msc")
            msc = int(raw_msc if raw_msc is not None else int(_value(tick, "time")) * 1000)
            row = {"tick_id": sha256(_canonical(_row(tick, ("time_msc", "bid", "ask", "last", "volume", "volume_real", "flags"))).encode()).hexdigest(), "time_msc": msc, "time_utc": utc_iso(msc), "bid": float(_value(tick, "bid")), "ask": float(_value(tick, "ask")), "spread": float(_value(tick, "ask"))-float(_value(tick, "bid")), **_row(tick, ("last", "volume", "volume_real", "flags"))}
            prior_tick = self.ticks.last_time
            if not self.ticks.append(row): self.duplicates += 1
            elif prior_tick is not None and msc - prior_tick > 60_000:
                self.gap_journals["ticks"].append({"gap_id": f"ticks:{prior_tick}:{msc}", "timeframe": "ticks", "after_open": prior_tick, "before_open": msc, "gap_ms": msc - prior_tick})
        m1_tf, m15_tf, h4_tf = self.mt5.TIMEFRAME_M1, self.mt5.TIMEFRAME_M15, self.mt5.TIMEFRAME_H4
        for row in self._rate_rows(m1_tf, "m1", 60_000):
            if not self.m1.append(row): self.duplicates += 1
        captured: list[tuple[dict[str, Any], Timeframe]] = []
        for tf, name, duration, enum in ((m15_tf, "m15", 900_000, Timeframe.MINUTES_15), (h4_tf, "h4", 14_400_000, Timeframe.HOURS_4)):
            for row in self._rate_rows(tf, name, duration):
                captured.append((row, enum))
        ready: list[HistoricalBar] = []
        for row, enum in sorted(captured, key=lambda item: (item[0]["finalized_at"], item[1].priority)):
            if not self.bars.append(row): self.duplicates += 1
            if enum is Timeframe.MINUTES_15: self.latest_m15 = row
            ready.append(HistoricalBar(SYMBOL, enum, row["open_time"], row["open"], row["high"], row["low"], row["close"], row["tick_volume"]))
        processed: list[HistoricalBar] = []
        for bar in sorted(ready, key=lambda item: item.processing_key):
            if self.state.chronology_cursor is not None and bar.processing_key <= self.state.chronology_cursor: continue
            result = self.engine.step(self.state, bar); self.state = result.state; processed.append(bar)
            if bar.timeframe is Timeframe.HOURS_4:
                self.latest_h4 = {"bar_id": f"h4:{bar.open_time}", "open_time": bar.open_time, "finalized_at": bar.finalized_at, "finalized_utc": utc_iso(bar.finalized_at), "bias": None if self.state.latest_htf_bias is None else self.state.latest_htf_bias.value}
            for event in result.trace.events:
                if event.type is EventType.TRADE_OPENED and bar.finalized_at >= activation_ms and 0 <= now_ms - bar.finalized_at <= self.max_signal_lag_ms:
                    trade = self.state.strategy_state.trade; assert trade is not None
                    signal_id = sha256(f"v1:{bar.processing_key}:{trade.trade_id}".encode()).hexdigest()
                    if self.signals.contains(signal_id): continue
                    signal = {"signal_id": signal_id, "signal_timestamp": now_ms, "decision_bar_timestamp": bar.open_time, "strategy_version": "frozen-v1", "config_hash": sha256(_canonical({"replay": asdict(self.engine.config), "strategy": asdict(self.engine.strategy_engine.config)}).encode()).hexdigest(), "direction": trade.side.value, "signal_type": "TradeOpened", "armed_or_immediate": "immediate" if trade.setup_origin_timestamp == bar.finalized_at else "armed", "setup_origin_timestamp": trade.setup_origin_timestamp, "finalized_m15": {"open_time": bar.open_time, "finalized_at": bar.finalized_at, "ohlc": [bar.open, bar.high, bar.low, bar.close]}, "finalized_h4_bias": self.latest_h4, "theoretical_entry": trade.entry_price, "intended_initial_stop": trade.stop_price, "intended_exit_rule": "frozen_v1_bias_reversal_or_hema_or_stop", "state_hash": sha256(_canonical(json.loads(encode_replay_state(self.state, expected_config=self.engine.config))).encode()).hexdigest()}
                    if self.signals.append(signal): self.shadow.record(signal)
                elif event.type is EventType.TRADE_OPENED and bar.finalized_at >= activation_ms and now_ms - bar.finalized_at > self.max_signal_lag_ms:
                    # Catch-up can reconstruct state but must never create a
                    # late prospective opportunity after an outage/restart.
                    self.missed_stale_signals += 1
        self._save_checkpoint(); return tuple(processed)

    def health(self) -> dict[str, Any]:
        latest_tick = self.ticks.last_time
        self.stale_quote = latest_tick is None or int(self.now() * 1000) - latest_tick > 60_000
        gap_count = sum(journal.count for journal in self.gap_journals.values())
        return {"service_started_utc": utc_iso(self.started_at), "latest_tick": latest_tick, "latest_tick_utc": None if latest_tick is None else utc_iso(latest_tick), "latest_m1": self.m1.last_time, "latest_m1_utc": None if self.m1.last_time is None else utc_iso(self.m1.last_time), "last_finalized_m15": None if self.latest_m15 is None else self.latest_m15["finalized_at"], "last_finalized_m15_utc": None if self.latest_m15 is None else self.latest_m15["finalized_utc"], "last_finalized_h4": None if self.latest_h4 is None else self.latest_h4["finalized_at"], "last_finalized_h4_utc": None if self.latest_h4 is None else self.latest_h4["finalized_utc"], "counts": {"ticks": self.ticks.count, "m1": self.m1.count, "bars": self.bars.count, "signals": self.signals.count, "shadow": self.shadow.journal.count, "execution": self.execution.count}, "duplicates": self.duplicates, "gaps": gap_count, "reconnects": self.reconnects, "stale_quote": self.stale_quote, "tick_saturation": self.tick_saturation, "missed_stale_signals": self.missed_stale_signals, "pending": self.state.strategy_state.pending_direction is not None, "open_v1_position": self.state.strategy_state.trade is not None, "last_execution": self.last_execution}

    def run(self, stop: Callable[[], bool], interval_seconds: float = 5.0) -> None:
        while not stop():
            delay = interval_seconds
            try:
                self.poll_once(); self._consecutive_failures = 0
            except Exception:
                self.reconnects += 1; self._consecutive_failures += 1
                if self._consecutive_failures >= 4: raise
                delay = min(interval_seconds * (2 ** (self._consecutive_failures - 1)), 60.0)
            if not stop(): self.sleeper(delay)


def holdout_aggregate_economics(*, start_ms: int, end_ms: int) -> None:
    if any(isinstance(value, bool) or not isinstance(value, int) for value in (start_ms, end_ms)) or start_ms >= end_ms:
        raise ValueError("holdout range must be increasing integer milliseconds")
    if start_ms < HOLDOUT_END_MS and end_ms > HOLDOUT_START_MS:
        raise PermissionError("V1 holdout aggregate economics are blocked for the reserved window")
