"""Fail-closed XM MT5 forward capture for the frozen V1 replay.

The MetaTrader5 package is intentionally imported only by :func:`load_mt5`.
This module has no ``order_send`` path: execution evidence is useful now, but
order lifecycle authorization remains a separate future gate.
"""
from __future__ import annotations

from dataclasses import asdict, dataclass, is_dataclass
from datetime import UTC, datetime
from hashlib import sha256
from copy import deepcopy
import importlib
import json
import math
from pathlib import Path
import time
from typing import Any, Callable, Iterable, Mapping

from quasartrend.replay import HistoricalBar, ReplayEngine, ReplayState
from quasartrend.research.xm_gold_historical_validation import verify_frozen_production_sources
from .capture import CaptureBlocked, CaptureMachine, DURATIONS, WARMUP_FINALIZED_BARS, historical
from .durable import EvidenceLock, Fault, JsonlJournal, atomic_write, no_fault

AUTHORIZED_DEMO_SERVERS = frozenset(("XMGlobal-MT5 9", "XMGlobal-MT5 18"))
BROKER_COMPANY = "XM Global Limited"
BROKER_POLICY_VERSION = "xm-demo-broker-policy/v2"
SYMBOL = "GOLD"
DEMO_EXECUTION_VOLUME_POLICY = "SYMBOL_VOLUME_MIN"
EXPECTED_SWAP_MODE_POINTS_RAW = 1
ORDER_BLOCKER = "DEMO ORDER SUBMISSION BLOCKED — ORDER LIFECYCLE AUTHORIZATION NOT GRANTED"
IDENTITY_BLOCKER = "XM DEMO AUDIT: BLOCKED — ACCOUNT IDENTITY NOT PROVEN"
CAPTURE_INTEGRITY_BLOCKER = "XM FORWARD CAPTURE: BLOCKED — PASSIVE CAPTURE AUTHORIZATION NOT GRANTED"
# This authorizes passive evidence capture only. The CLI remains audit-only by
# default, every unresolved data gap still fails closed, and execution remains
# independently hard-blocked with no order-submission path.
FORWARD_CAPTURE_INTEGRITY_AUTHORIZED = True
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
    return sha256(b"".join(name.encode() + b"\0" + Path(__file__).with_name(name).read_bytes() for name in ("mt5.py", "capture.py", "durable.py"))).hexdigest()


@dataclass(frozen=True, slots=True)
class CapabilityAudit:
    server: str | None
    environment_id: str | None
    account_identity_proven: bool
    demo_proven: bool
    broker_identity_proven: bool
    server_authorized: bool
    symbol_compatible: bool
    capture_capability_proven: bool
    execution_permissions_proven: bool
    execution_permission_blocker: str | None
    execution_mode: str
    audit_allowed: bool
    audit_blocker: str | None
    capture_allowed: bool
    capture_blocker: str | None
    execution_allowed: bool
    execution_blocker: str | None
    snapshot: Mapping[str, Any]


def _exact_number(value: Any, expected: float) -> bool:
    return not isinstance(value, bool) and isinstance(value, (int, float)) and math.isfinite(float(value)) and float(value) == expected


def _exact_integer(value: Any, expected: int) -> bool:
    return not isinstance(value, bool) and isinstance(value, int) and value == expected


def _valid_login(value: Any) -> bool:
    return not isinstance(value, bool) and isinstance(value, int) and value > 0


def _first_failed(checks: Iterable[tuple[bool, str]]) -> str | None:
    return next((message for passed, message in checks if not passed), None)


def audit_capabilities(mt5: Any, *, execution_mode: str = "none", symbol: str = SYMBOL) -> CapabilityAudit:
    """Read-only broker-policy audit. It never calls order submission APIs."""
    terminal, account = mt5.terminal_info(), mt5.account_info()
    info = mt5.symbol_info(symbol)
    connection = _value(terminal, "connected") is True
    actual_server = _value(account, "server")
    login = _value(account, "login")
    demo_constant = _value(mt5, "ACCOUNT_TRADE_MODE_DEMO")
    trade_mode = _value(account, "trade_mode")
    account_expert = _value(account, "trade_expert")
    execution_flags = {
        "account_trade_allowed": _value(account, "trade_allowed"),
        "terminal_trade_allowed": _value(terminal, "trade_allowed"),
        "python_trading_api_enabled": _value(terminal, "tradeapi_disabled") is False,
        "account_trade_expert": account_expert,
    }
    disabled_mode = _value(mt5, "SYMBOL_TRADE_MODE_DISABLED")
    symbol_trade_mode = _value(info, "trade_mode")
    symbol_tradeable = (
        not isinstance(disabled_mode, bool) and isinstance(disabled_mode, int)
        and not isinstance(symbol_trade_mode, bool) and isinstance(symbol_trade_mode, int)
        and symbol_trade_mode != disabled_mode
    )
    account_identity = account is not None and _valid_login(login)
    demo = not isinstance(demo_constant, bool) and isinstance(demo_constant, int) and _exact_integer(trade_mode, demo_constant)
    broker_identity = _value(account, "company") == BROKER_COMPANY
    server_authorized = isinstance(actual_server, str) and actual_server in AUTHORIZED_DEMO_SERVERS
    symbol_identity = symbol == SYMBOL and info is not None and _value(info, "name") == SYMBOL
    symbol_visible = symbol_identity and _value(info, "visible") is True
    spec_checks = {
        "digits": _exact_integer(_value(info, "digits"), 2),
        "point": _exact_number(_value(info, "point"), 0.01),
        "trade_tick_size": _exact_number(_value(info, "trade_tick_size"), 0.01),
        "trade_tick_value": _exact_number(_value(info, "trade_tick_value"), 1.0),
        "trade_contract_size": _exact_number(_value(info, "trade_contract_size"), 100.0),
        "volume_min": _exact_number(_value(info, "volume_min"), 0.01),
        "volume_max": _exact_number(_value(info, "volume_max"), 50.0),
        "volume_step": _exact_number(_value(info, "volume_step"), 0.01),
        # The official Python wrapper may omit swap-mode constants.  Raw value
        # 1 is pinned by the admitted server-18 snapshot and supplied server-9
        # evidence; do not infer it from a partial runtime enum table.
        "swap_mode_points": _exact_integer(_value(info, "swap_mode"), EXPECTED_SWAP_MODE_POINTS_RAW),
        "triple_swap_wednesday": _exact_integer(_value(info, "swap_rollover3days"), 3),
        "currency_base": _value(info, "currency_base") == "USD",
        "currency_profit": _value(info, "currency_profit") == "USD",
        "currency_margin": _value(info, "currency_margin") == "USD",
        "bank": _value(info, "bank") == "XM",
        "description": _value(info, "description") == "GOLD",
    }
    symbol_compatible = symbol_visible and all(spec_checks.values())
    audit_checks = (
        (connection, "XM DEMO AUDIT: BLOCKED — TERMINAL DISCONNECTED"),
        (account_identity, IDENTITY_BLOCKER),
        (demo, "XM DEMO AUDIT: BLOCKED — ACCOUNT TRADE MODE IS NOT DEMO"),
        (broker_identity, "XM DEMO AUDIT: BLOCKED — BROKER COMPANY NOT AUTHORIZED"),
        (server_authorized, "XM DEMO AUDIT: BLOCKED — SERVER NOT EXPLICITLY AUTHORIZED"),
        (symbol_identity, "XM DEMO AUDIT: BLOCKED — REQUIRED SYMBOL GOLD NOT AVAILABLE"),
        (symbol_visible, "XM DEMO AUDIT: BLOCKED — REQUIRED SYMBOL GOLD NOT VISIBLE"),
        (spec_checks["digits"], "XM DEMO AUDIT: BLOCKED — GOLD DIGITS MISMATCH"),
        (spec_checks["point"], "XM DEMO AUDIT: BLOCKED — GOLD POINT MISMATCH"),
        (spec_checks["trade_tick_size"], "XM DEMO AUDIT: BLOCKED — GOLD TICK SIZE MISMATCH"),
        (spec_checks["trade_tick_value"], "XM DEMO AUDIT: BLOCKED — GOLD TICK VALUE MISMATCH"),
        (spec_checks["trade_contract_size"], "XM DEMO AUDIT: BLOCKED — GOLD CONTRACT SIZE MISMATCH"),
        (spec_checks["volume_min"], "XM DEMO AUDIT: BLOCKED — GOLD VOLUME MIN INVALID OR MISMATCHED"),
        (spec_checks["volume_max"], "XM DEMO AUDIT: BLOCKED — GOLD VOLUME MAX INVALID OR MISMATCHED"),
        (spec_checks["volume_step"], "XM DEMO AUDIT: BLOCKED — GOLD VOLUME STEP INVALID OR MISMATCHED"),
        (spec_checks["swap_mode_points"], "XM DEMO AUDIT: BLOCKED — GOLD SWAP MODE IS NOT POINTS"),
        (spec_checks["triple_swap_wednesday"], "XM DEMO AUDIT: BLOCKED — GOLD TRIPLE-SWAP DAY IS NOT WEDNESDAY"),
        (spec_checks["currency_base"], "XM DEMO AUDIT: BLOCKED — GOLD BASE CURRENCY MISMATCH"),
        (spec_checks["currency_profit"], "XM DEMO AUDIT: BLOCKED — GOLD PROFIT CURRENCY MISMATCH"),
        (spec_checks["currency_margin"], "XM DEMO AUDIT: BLOCKED — GOLD MARGIN CURRENCY MISMATCH"),
        (spec_checks["bank"], "XM DEMO AUDIT: BLOCKED — GOLD BANK IDENTITY MISMATCH"),
        (spec_checks["description"], "XM DEMO AUDIT: BLOCKED — GOLD DESCRIPTION MISMATCH"),
    )
    audit_blocker = _first_failed(audit_checks)
    audit_allowed = audit_blocker is None
    execution_permissions = audit_allowed and symbol_tradeable and all(value is True for value in execution_flags.values())
    if not audit_allowed:
        execution_permission_blocker = audit_blocker
    elif not symbol_tradeable:
        execution_permission_blocker = "XM DEMO EXECUTION READINESS: BLOCKED — GOLD TRADING DISABLED"
    elif execution_flags["account_trade_allowed"] is not True:
        execution_permission_blocker = "XM DEMO EXECUTION READINESS: BLOCKED — ACCOUNT TRADING NOT ALLOWED"
    elif execution_flags["terminal_trade_allowed"] is not True:
        execution_permission_blocker = "XM DEMO EXECUTION READINESS: BLOCKED — TERMINAL TRADING NOT ALLOWED"
    elif execution_flags["python_trading_api_enabled"] is not True:
        execution_permission_blocker = "XM DEMO EXECUTION READINESS: BLOCKED — PYTHON TRADING API DISABLED"
    elif execution_flags["account_trade_expert"] is not True:
        execution_permission_blocker = "XM DEMO EXECUTION READINESS: BLOCKED — EXPERT TRADING NOT ALLOWED"
    else:
        execution_permission_blocker = None
    execution_blocker = audit_blocker if not audit_allowed else ORDER_BLOCKER
    capture_allowed = audit_allowed and FORWARD_CAPTURE_INTEGRITY_AUTHORIZED
    capture_blocker = audit_blocker if not audit_allowed else None if capture_allowed else CAPTURE_INTEGRITY_BLOCKER
    version = getattr(mt5, "version", lambda: None)()
    snapshot = {
        "schema_version": "xm-mt5-capability-audit/v2", "terminal_connected": connection,
        "policy": {"version": BROKER_POLICY_VERSION, "company": BROKER_COMPANY, "authorized_demo_servers": sorted(AUTHORIZED_DEMO_SERVERS), "symbol": SYMBOL, "swap_mode_points_raw": EXPECTED_SWAP_MODE_POINTS_RAW, "demo_execution_volume_policy": DEMO_EXECUTION_VOLUME_POLICY},
        "mt5_version": _json(version),
        "terminal": _row(terminal, ("build", "connected", "trade_allowed", "tradeapi_disabled", "community_account")),
        "account": _row(account, ("server", "trade_mode", "trade_allowed", "currency", "currency_digits", "margin_mode", "company")),
        "symbol": _row(info, ("name", "visible", "trade_mode", "order_mode", "filling_mode", "volume_min", "volume_max", "volume_step", "trade_stops_level", "trade_freeze_level", "trade_contract_size", "digits", "point", "trade_tick_size", "trade_tick_value", "trade_tick_value_profit", "trade_tick_value_loss", "currency_base", "currency_profit", "currency_margin", "swap_mode", "swap_long", "swap_short", "swap_rollover3days", "bank", "description")),
        "checks": {"account_identity_proven": account_identity, "demo_proven": demo, "broker_identity_proven": broker_identity, "server_authorized": server_authorized, "symbol_identity_proven": symbol_identity, "symbol_visible": symbol_visible, "symbol_spec": spec_checks, "symbol_tradeable": symbol_tradeable, "execution_permissions": execution_flags, "execution_permissions_proven": execution_permissions, "execution_permission_blocker": execution_permission_blocker, "order_submission_authorized": False},
    }
    return CapabilityAudit(
        actual_server,
        environment_pseudonym(actual_server, login) if isinstance(actual_server, str) and account_identity else None,
        account_identity,
        demo,
        broker_identity,
        server_authorized,
        symbol_compatible,
        audit_allowed,
        execution_permissions,
        execution_permission_blocker,
        execution_mode,
        audit_allowed,
        audit_blocker,
        capture_allowed,
        capture_blocker,
        False,
        execution_blocker,
        snapshot,
    )


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
    """One writer, durable observations, deterministic replay and projections.

    Checkpoints are verified caches, never authority for skipping an input.
    Every persistence failure requires closing this instance and recovering.
    """
    def __init__(self, root: Path, *, mt5: Any | None = None, execution_mode: str = "none", terminal_path: str | Path | None = None, repo_root: Path | None = None, activate: bool = True, max_signal_lag_ms: int = 60_000, now: Callable[[], float] = time.time, sleeper: Callable[[float], None] = time.sleep, fault: Fault = no_fault) -> None:
        if type(max_signal_lag_ms) is not int or max_signal_lag_ms <= 0:
            raise ValueError("max_signal_lag_ms must be a positive integer")
        self.mt5, self.root, self.now, self.sleeper = mt5 or load_mt5(), Path(root).resolve(), now, sleeper
        self._lock = None
        self._closed = self._poisoned = False
        self.fault = fault
        try:
            initialized = self.mt5.initialize() if terminal_path is None else self.mt5.initialize(path=str(terminal_path))
            if not initialized:
                raise RuntimeError("XM MT5 initialization failed before capability audit")
            self.repo_root = Path(repo_root) if repo_root is not None else Path(__file__).resolve().parents[3]
            self.frozen_v1_sources = verify_frozen_production_sources(self.repo_root)
            self.audit = audit_capabilities(self.mt5, execution_mode=execution_mode)
            if activate and not self.audit.capture_allowed:
                raise PermissionError(self.audit.capture_blocker or CAPTURE_INTEGRITY_BLOCKER)
            if not self.audit.environment_id:
                raise PermissionError(self.audit.audit_blocker or IDENTITY_BLOCKER)
            self._lock = EvidenceLock(self.root)
            self.execution_mode, self._capture_enabled = execution_mode, activate
            self.max_signal_lag_ms = max_signal_lag_ms
            self.started_at = int(now() * 1000)
            self.reconnects = self.duplicates = self.tick_saturation = 0
            self.last_execution = None
            self._consecutive_failures = 0
            self.base = self.root / "forward" / "xm" / SYMBOL
            p = {"source_server": self.audit.server, "symbol": SYMBOL, "environment_id": self.audit.environment_id,
                 "capture_version": "xm-v1-forward/v2", "broker_policy_version": BROKER_POLICY_VERSION,
                 "implementation_sha256": implementation_hash(), "frozen_v1_commit": FROZEN_V1_COMMIT,
                 "frozen_v1_manifest_sha256": FROZEN_V1_MANIFEST_SHA256,
                 "max_signal_lag_ms": max_signal_lag_ms, "warmup_finalized_bars": WARMUP_FINALIZED_BARS}
            def journal(relative: str, schema: str, timestamp: str, identity: str) -> JsonlJournal:
                return JsonlJournal(self.base / relative, schema=schema, provenance=p, time_field=timestamp, id_field=identity, fault=fault)
            self.inputs = journal("inputs/observations.jsonl", "xm-forward-observations/v2", "observed_at", "observation_id")
            self.ticks = journal("ticks/ticks.jsonl", "xm-forward-ticks/v2", "time_msc", "tick_id")
            self.m1 = journal("m1/bars.jsonl", "xm-forward-bars/v2", "observed_at", "bar_id")
            self.bars = journal("bars/finalized.jsonl", "xm-forward-bars/v2", "finalized_at", "bar_id")
            self.gaps = journal("gaps/integrity.jsonl", "xm-forward-gaps/v2", "observed_at", "gap_id")
            self.gap_journals = {"integrity": self.gaps}
            self.signals = journal("signals/v1.jsonl", "xm-forward-v1-signals/v2", "signal_timestamp", "signal_id")
            self.shadow = Family1LongOnlyShadow(journal("signals/family1_long_only.jsonl", "xm-forward-family1-shadow/v2", "decision_timestamp", "shadow_id"))
            self.execution = journal("execution/evidence.jsonl", "xm-forward-execution/v1", "request_timestamp", "execution_id")
            self.adapter = DemoExecutionAdapter(self.mt5, self.execution, self.audit)
            self.projections = {"ticks": self.ticks, "m1": self.m1, "bars": self.bars, "gaps": self.gaps, "signals": self.signals, "shadow": self.shadow.journal}
            self.checkpoint = self.base / "checkpoints" / "replay.json"
            self.machine: CaptureMachine | None = None
            self._recover()
            self._persist_audit()
        except BaseException:
            self.close()
            raise

    @property
    def activation_ms(self) -> int | None:
        return None if self.machine is None else self.machine.activation_ms

    @property
    def state(self) -> ReplayState:
        return ReplayEngine().initial_state(SYMBOL) if self.machine is None else self.machine.state

    def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        try:
            shutdown = getattr(self.mt5, "shutdown", None)
            if callable(shutdown):
                shutdown()
        finally:
            if self._lock is not None:
                self._lock.close()

    def __enter__(self) -> XMForwardService:
        return self

    def __exit__(self, *_: object) -> None:
        self.close()

    def _persist_audit(self) -> None:
        # Full snapshot revisions are separate immutable audit records. A swap
        # or permission-report change cannot overwrite old evidence or masquerade
        # as a different account (the compact identity binds every journal).
        payload = _canonical({"schema_version": "xm-mt5-capability-evidence/v2", "environment_id": self.audit.environment_id,
                              "snapshot": self.audit.snapshot, "audit_allowed": self.audit.audit_allowed,
                              "capture_allowed": self.audit.capture_allowed, "execution_allowed": False,
                              "implementation_sha256": implementation_hash(), "frozen_v1_sources": self.frozen_v1_sources})
        path = self.base / "audit" / (sha256(payload.encode()).hexdigest() + ".json")
        if path.exists():
            if path.read_bytes() != (payload + "\n").encode():
                raise ValueError("immutable audit evidence corruption")
        else:
            atomic_write(path, payload + "\n")

    def _checkpoint_payload(self, count: int, tip: str) -> dict[str, Any]:
        assert self.machine is not None
        return {"schema_version": "xm-capture-checkpoint/v2", "input_count": count, "input_tip": tip,
                "activation_ms": self.machine.activation_ms, "snapshot": self.machine.snapshot()}

    def _recover(self) -> None:
        saved = None
        if self.checkpoint.exists():
            saved = json.loads(self.checkpoint.read_bytes())
            if (not isinstance(saved, dict) or type(saved.get("input_count")) is not int
                    or not 0 < saved["input_count"] <= self.inputs.count):
                raise ValueError("checkpoint ahead of canonical input or invalid")
        expected: dict[str, list[dict[str, Any]]] = {name: [] for name in self.projections}
        for index, observation in enumerate(self.inputs.rows, 1):
            if self.machine is None:
                self.machine = CaptureMachine(activation_ms=observation["activation_ms"], max_signal_lag_ms=self.max_signal_lag_ms)
            outputs = self.machine.observe(observation)
            for name, rows in outputs.items():
                expected[name].extend(self.projections[name].material(row) for row in rows)
            if saved is not None and saved["input_count"] == index:
                if saved != self._checkpoint_payload(index, self.inputs.digests[index - 1]):
                    raise ValueError("checkpoint does not match deterministic input replay")
        # Validate ALL projections before repairing any one of them. Extra or
        # orphan evidence, even with valid framing, must never be discarded.
        for name, journal in self.projections.items():
            rows = list(journal.rows)
            if rows != expected[name][:len(rows)] or len(rows) > len(expected[name]):
                raise ValueError(f"{name} projection is not an exact canonical input prefix")
        for name, journal in self.projections.items():
            for row in expected[name][journal.count:]:
                journal.append(row)
        if self.machine is not None:
            self._save_checkpoint()

    def _save_checkpoint(self) -> None:
        atomic_write(self.checkpoint, _canonical(self._checkpoint_payload(self.inputs.count, self.inputs.tip)) + "\n", self.fault)

    def _history_status(self, result: Any, name: str) -> list[Any]:
        status = getattr(self.mt5, "last_error", lambda: None)()
        if (result is None or not isinstance(status, (tuple, list)) or len(status) != 2
                or type(status[0]) is not int or status[0] != 1):
            raise RuntimeError(f"MT5 {name} history result/status not successful: {status}")
        # Python success is necessary, but is not treated as proof of closure.
        return _json(status)

    def _rate_rows(self, timeframe: Any, name: str, duration_ms: int, now_ms: int) -> tuple[list[dict[str, Any]], dict[str, Any]]:
        known = {} if self.machine is None else self.machine.known[name]
        previous = None if self.machine is None or name == "m1" else self.machine.latest[name]
        request: dict[str, Any] = {"timeframe": name, "cutoff_ms": now_ms}
        bootstrap = not known or (name != "m1" and sum(row["finalized_at"] <= self.activation_ms for row in known.values()) < WARMUP_FINALIZED_BARS)
        if bootstrap:
            finalized = []
            attempts = []
            cutoff = now_ms if self.activation_ms is None else self.activation_ms
            for count in (601, 1202, 2404, 4808, 9616):
                raw = self.mt5.copy_rates_from_pos(SYMBOL, timeframe, 0, count)
                status = self._history_status(raw, name)
                raw = list(raw)
                finalized = self._normalize_rates(raw, name, duration_ms, now_ms)
                warmup_count = sum(row["finalized_at"] <= cutoff for row in finalized)
                attempts.append({"requested": count, "returned": len(raw), "finalized": len(finalized), "warmup_finalized": warmup_count, "status": status})
                if warmup_count >= WARMUP_FINALIZED_BARS or name == "m1":
                    break
            request.update({"kind": "position_overfetch", "attempts": attempts})
            if name == "m1":
                finalized = finalized[-WARMUP_FINALIZED_BARS:]
            else:
                finalized = ([row for row in finalized if row["finalized_at"] <= cutoff][-WARMUP_FINALIZED_BARS:]
                             + [row for row in finalized if row["finalized_at"] > cutoff])
        else:
            if not callable(getattr(self.mt5, "copy_rates_range", None)):
                raise RuntimeError("MT5 range history is required for deterministic resume")
            start = previous["open_time"] if previous is not None else min(known)
            if name == "m1":
                missing = [] if self.machine is None else self.machine.m1_gaps
                start = max(known) if not missing else max(min(known), min(gap["open_time"] for gap in missing) - duration_ms)
            raw = self.mt5.copy_rates_range(SYMBOL, timeframe, datetime.fromtimestamp(start / 1000, UTC), datetime.fromtimestamp(now_ms / 1000, UTC))
            status = self._history_status(raw, name)
            finalized = self._normalize_rates(list(raw), name, duration_ms, now_ms)
            request.update({"kind": "range", "start_ms": start, "returned_finalized": len(finalized), "status": status})
            if not any(row["open_time"] == start for row in finalized):
                raise RuntimeError(f"resumed {name} range omitted persisted overlap")
        return finalized, request

    @staticmethod
    def _normalize_rates(rates: list[Any], name: str, duration_ms: int, now_ms: int) -> list[dict[str, Any]]:
        finalized: dict[int, dict[str, Any]] = {}
        for rate in rates:
            stamp = _value(rate, "time")
            if type(stamp) is not int or stamp < 0:
                raise ValueError("invalid MT5 bar time")
            open_time = stamp * 1000
            if open_time + duration_ms > now_ms:
                continue
            values = {key: _value(rate, key) for key in ("open", "high", "low", "close", "tick_volume", "spread", "real_volume")}
            for key, value in values.items():
                if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value):
                    raise ValueError(f"invalid finalized bar {key}")
            if (not 0 < values["low"] <= min(values["open"], values["close"]) <= max(values["open"], values["close"]) <= values["high"]
                    or any(values[key] < 0 for key in ("tick_volume", "spread", "real_volume"))):
                raise ValueError("invalid finalized bar OHLC/volume/spread")
            row = {"bar_id": f"{name}:{open_time}", "open_time": open_time, "finalized_at": open_time + duration_ms,
                   "time_utc": utc_iso(open_time), "finalized_utc": utc_iso(open_time + duration_ms), "timeframe": name, **values}
            if open_time in finalized and finalized[open_time] != row:
                raise ValueError("conflicting duplicate finalized rate")
            finalized[open_time] = row
        return [finalized[key] for key in sorted(finalized)]

    def _tick_rows(self, now_ms: int) -> tuple[list[dict[str, Any]], dict[str, Any]]:
        start_ms = self.ticks.last_time if self.ticks.last_time is not None else max(0, now_ms - 60_000)
        rows = []
        cursor_ms = start_ms
        pages = []
        for page_index in range(100):
            page = self.mt5.copy_ticks_from(SYMBOL, datetime.fromtimestamp(cursor_ms / 1000, UTC), 10_000, self.mt5.COPY_TICKS_ALL)
            status = self._history_status(page, "ticks")
            page = list(page)
            stamps = []
            for tick in page:
                msc = _value(tick, "time_msc")
                if type(msc) is not int or msc < cursor_ms:
                    raise ValueError("invalid MT5 tick timestamp or retrieval bound")
                stamps.append(msc)
                if msc > now_ms:
                    continue
                values = _row(tick, ("last", "volume", "volume_real", "flags"))
                bid, ask = _value(tick, "bid"), _value(tick, "ask")
                if any(isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value) for value in (bid, ask, *values.values())) or not 0 < bid <= ask:
                    raise ValueError("invalid MT5 tick quote")
                row = {"time_msc": msc, "time_utc": utc_iso(msc), "bid": bid, "ask": ask, "spread": ask - bid, **values}
                rows.append({"tick_id": sha256(_canonical(row).encode()).hexdigest(), **row})
            pages.append({"cursor_ms": cursor_ms, "returned": len(page), "status": status})
            if len(page) < 10_000 or (stamps and max(stamps) > now_ms):
                break
            newest = max(stamps)
            if newest <= cursor_ms or page_index == 99:
                self.tick_saturation += 1
                raise RuntimeError("MT5 tick pagination saturated; boundary completeness unresolved")
            # Inclusive re-fetch exhausts all distinct ticks at the page boundary.
            # Never advance by +1: that silently loses same-millisecond ticks.
            cursor_ms = newest
        by_id = {row["tick_id"]: row for row in rows}
        return sorted(by_id.values(), key=lambda row: (row["time_msc"], row["tick_id"])), {"start_ms": start_ms, "cutoff_ms": now_ms, "pages": pages}

    def poll_once(self) -> tuple[HistoricalBar, ...]:
        if self._closed or self._poisoned:
            raise RuntimeError("capture service requires restart")
        if not self._capture_enabled:
            raise PermissionError("forward capture is not enabled in audit-only mode")
        now_ms = int(self.now() * 1000)
        if not bool(_value(self.mt5.terminal_info(), "connected", False)):
            self.reconnects += 1
            if not self.mt5.initialize():
                raise RuntimeError("MT5 reconnect failed")
        current_audit = audit_capabilities(self.mt5, execution_mode=self.execution_mode)
        if not current_audit.capture_allowed or current_audit.environment_id != self.audit.environment_id:
            raise PermissionError(current_audit.capture_blocker or current_audit.audit_blocker or "capture environment changed")
        ticks, tick_request = self._tick_rows(now_ms)
        rates, requests = {}, {}
        for name, tf in (("m1", self.mt5.TIMEFRAME_M1), ("m15", self.mt5.TIMEFRAME_M15), ("h4", self.mt5.TIMEFRAME_H4)):
            rates[name], requests[name] = self._rate_rows(tf, name, DURATIONS[name], now_ms)
        completed_ms = int(self.now() * 1000)
        observation = {"observation_id": str(self.inputs.count + 1), "observed_at": completed_ms, "cutoff_ms": now_ms,
                       "activation_ms": now_ms if self.activation_ms is None else self.activation_ms, "ticks": ticks, "rates": rates,
                       "requests": {"ticks": tick_request, "rates": requests}}
        # Validate the transition on a private candidate before committing it.
        # A bad API observation must not permanently poison the canonical WAL.
        candidate = deepcopy(self.machine) if self.machine is not None else CaptureMachine(activation_ms=now_ms, max_signal_lag_ms=self.max_signal_lag_ms)
        outputs = candidate.observe(observation)
        try:
            # The sole commit boundary: no projection or recursive transition
            # may precede this durable, fully framed canonical observation.
            self.inputs.append(observation)
            self.machine = candidate
            for name, rows in outputs.items():
                for row in rows:
                    self.projections[name].append(row)
            self._save_checkpoint()
            self.fault("after_checkpoint_before_next_event", self.checkpoint)
        except BaseException:
            self._poisoned = True
            raise
        if self.machine.pending_gaps:
            first = self.machine.pending_gaps[0]
            raise CaptureBlocked(f"strategy advancement blocked: {first}")
        return tuple(historical(row) for row in outputs["bars"])

    def health(self) -> dict[str, Any]:
        latest_tick = self.ticks.last_time
        return {"service_started_utc": utc_iso(self.started_at), "latest_tick": latest_tick,
                "last_finalized_m15": None if self.machine is None or self.machine.latest["m15"] is None else self.machine.latest["m15"]["finalized_at"],
                "last_finalized_h4": None if self.machine is None or self.machine.latest["h4"] is None else self.machine.latest["h4"]["finalized_at"],
                "counts": {**{name: journal.count for name, journal in self.projections.items()}, "inputs": self.inputs.count, "execution": self.execution.count},
                "gaps": self.gaps.count, "pending_gaps": [] if self.machine is None else self.machine.pending_gaps,
                "reconnects": self.reconnects, "tick_saturation": self.tick_saturation,
                "stale_quote": latest_tick is None or int(self.now() * 1000) - latest_tick > 60_000,
                "missed_stale_signals": 0 if self.machine is None else self.machine.missed_stale_signals,
                "pending": self.state.strategy_state.pending_direction is not None,
                "open_v1_position": self.state.strategy_state.trade is not None, "last_execution": self.last_execution}

    def run(self, stop: Callable[[], bool], interval_seconds: float = 5.0, *, max_polls: int | None = None) -> None:
        if max_polls is not None and (type(max_polls) is not int or max_polls <= 0):
            raise ValueError("max_polls must be a positive integer")
        completed_polls = 0
        while not stop():
            delay = interval_seconds
            try:
                self.poll_once()
                self._consecutive_failures = 0
                completed_polls += 1
            except CaptureBlocked:
                # A data-integrity gap is a STOP condition, not a transient
                # transport failure eligible for retry within this process.
                raise
            except (RuntimeError, PermissionError, ValueError):
                if self._poisoned:
                    raise
                self._consecutive_failures += 1
                if self._consecutive_failures >= 4:
                    raise
                delay = min(interval_seconds * (2 ** (self._consecutive_failures - 1)), 60.0)
            if max_polls is not None and completed_polls >= max_polls:
                break
            if not stop():
                self.sleeper(delay)

def holdout_aggregate_economics(*, start_ms: int, end_ms: int) -> None:
    if any(isinstance(value, bool) or not isinstance(value, int) for value in (start_ms, end_ms)) or start_ms >= end_ms:
        raise ValueError("holdout range must be increasing integer milliseconds")
    if start_ms < HOLDOUT_END_MS and end_ms > HOLDOUT_START_MS:
        raise PermissionError("V1 holdout aggregate economics are blocked for the reserved window")
