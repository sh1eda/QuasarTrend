#!/usr/bin/env python3
"""Six finite native history observations. Never a completeness/closure certificate.

Run only against an already running, explicitly identified Windows terminal. The
caller must enforce an external process timeout. No retry, service, or capture.
"""
from __future__ import annotations

import argparse
from datetime import UTC, datetime, timedelta
from hashlib import sha256
import importlib
import json
import math
from pathlib import Path
import platform
import subprocess
import sys
import time

from quasartrend.forward import mt5 as forward


def utc(value: datetime) -> str:
    return value.astimezone(UTC).isoformat()


def canonical(value: object) -> bytes:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False).encode()


def requests(as_of: datetime) -> list[dict]:
    if as_of.tzinfo is None or as_of.utcoffset() != timedelta(0):
        raise ValueError("UTC as-of required")
    end = as_of.replace(minute=(as_of.minute // 15) * 15, second=0, microsecond=0)
    saturday = end.replace(hour=0, minute=0, second=0) - timedelta(days=(end.weekday() - 5) % 7)
    if saturday + timedelta(days=2) > end:
        saturday -= timedelta(days=7)
    plans = []
    for label, start, finish in (("recent", end-timedelta(hours=12), end), ("weekend_candidate", saturday, saturday+timedelta(days=2))):
        for timeframe in ("M15", "H4"):
            plans.append(dict(label=label, kind="rates", timeframe=timeframe, start_utc=utc(start), end_utc=utc(finish)))
    plans.extend((dict(label="recent", kind="ticks", start_utc=utc(end-timedelta(minutes=15)), end_utc=utc(end)), dict(label="weekend_candidate", kind="ticks", start_utc=utc(saturday+timedelta(hours=12)), end_utc=utc(saturday+timedelta(hours=12, minutes=15)))))
    return plans


def existing_terminal(path: Path, pid: int) -> None:
    if platform.system() != "Windows" or pid <= 0 or not path.is_file() or path.name.lower() != "terminal64.exe":
        raise ValueError("existing Windows terminal required")
    # PID is an integer, never command text. Resolve the actual running image.
    result = subprocess.run(["powershell.exe", "-NoProfile", "-NonInteractive", "-Command", f"(Get-Process -Id {pid} -ErrorAction Stop).Path"], capture_output=True, text=True, timeout=10, check=True)
    if Path(result.stdout.strip()).resolve() != path.resolve():
        raise ValueError("running terminal image mismatch")


def status(api) -> int | None:
    error = api.last_error()
    code = error[0] if isinstance(error, (tuple, list)) and error else None
    return code if isinstance(code, int) and not isinstance(code, bool) else None


def audit(api, terminal: Path) -> dict:
    value = forward.audit_capabilities(api, execution_mode="none")
    snapshot = dict(value.snapshot)
    snapshot["symbol"] = {k: v for k, v in snapshot["symbol"].items() if k not in {"description", "bank"}}
    return dict(snapshot=snapshot, environment_pseudonym=value.environment_id,
                audit_allowed=value.audit_allowed, capture_allowed=value.capture_allowed,
                execution_allowed=value.execution_allowed,
                target_server9_demo_proven=value.audit_allowed and value.server == "XMGlobal-MT5 9",
                terminal_path_matches=Path(str(getattr(api.terminal_info(), "path", ""))).resolve() == terminal.resolve().parent)


def gate(value: dict) -> bool:
    return (value["target_server9_demo_proven"] is True and value["terminal_path_matches"] is True
            and value["capture_allowed"] is False and value["execution_allowed"] is False)


def payload(rows) -> dict:
    names = rows.dtype.names
    if not names or "time" not in names:
        raise ValueError("native structured dtype with time required")
    normalized = []
    for row in rows:
        result = {}
        for name in names:
            item = row[name].item()
            if isinstance(item, float) and not math.isfinite(item):
                raise ValueError("nonfinite native value")
            if not isinstance(item, (int, float, str, bool)):
                raise ValueError("unsupported native value")
            result[name] = item
        normalized.append(result)
    clocks = {name: {"minimum": min((row[name] for row in normalized), default=None), "maximum": max((row[name] for row in normalized), default=None)} for name in ("time", "time_msc") if name in names}
    return dict(count=len(rows), dtype=rows.dtype.descr, timestamp_bounds=clocks,
                normalized_payload_sha256=sha256(canonical(normalized)).hexdigest(),
                normalization="ordered rows; field names sorted; JSON compact UTF-8; finite numeric values")


def probe(api, terminal: Path, output: Path, as_of: datetime) -> dict:
    """Caller creates a fresh external output directory and verifies process first."""
    report = dict(schema="xm-native-history-observation/v1", execution=False, capture=False,
                  closure_certificate=False, completeness="UNRESOLVED", started_utc=utc(datetime.now(UTC)),
                  as_of_utc=utc(as_of), observations=[], status="UNRESOLVED")
    def persist():
        temporary = output / "evidence.pending"
        temporary.write_bytes(canonical(report) + b"\n")
        temporary.replace(output / "evidence.json")
    persist()
    try:
        if forward.FORWARD_CAPTURE_INTEGRITY_AUTHORIZED is not False:
            raise ValueError("capture gate enabled")
        report["initialize_returned"] = bool(api.initialize(path=str(terminal.resolve()), timeout=15_000))
        report["initialize_status_code"] = status(api)
        persist()
        if not report["initialize_returned"] or report["initialize_status_code"] != 1:
            raise ValueError("initialization unresolved")
        report["before"] = audit(api, terminal)
        report["python_api_surface"] = {name: callable(getattr(api, name, None)) for name in ("copy_rates_range", "copy_ticks_range", "symbol_info", "symbol_info_tick", "terminal_info", "account_info", "symbol_info_session_trade", "symbol_info_session_quote", "series_info_integer", "symbol_is_synchronized")}
        persist()
        if not gate(report["before"]):
            raise ValueError("read-only target gate failed")
        for plan in requests(as_of):
            observation = dict(plan, started_utc=utc(datetime.now(UTC)), returned="NOT_OBSERVED", acquisition="UNRESOLVED")
            report["observations"].append(observation)
            persist()
            call_started = time.monotonic()
            try:
                start, end = (datetime.fromisoformat(plan[key]) for key in ("start_utc", "end_utc"))
                if plan["kind"] == "rates":
                    rows = api.copy_rates_range("GOLD", getattr(api, "TIMEFRAME_"+plan["timeframe"]), start, end)
                else:
                    rows = api.copy_ticks_range("GOLD", start, end, api.COPY_TICKS_ALL)
                observation["status_code"] = status(api)  # Must precede any other API call.
                observation["returned"] = "NONE" if rows is None else "EMPTY" if len(rows) == 0 else "RETURNED"
                persist()
                if rows is not None:
                    observation.update(payload(rows))
                if observation["status_code"] == 1 and rows is not None:
                    observation["acquisition"] = "OBSERVED_API_SUCCESS"
            except Exception as error:
                observation["failure_type"] = type(error).__name__
            observation["elapsed_seconds"] = time.monotonic() - call_started
            observation["finished_utc"] = utc(datetime.now(UTC))
            report["after"] = audit(api, terminal)
            report["identity_stable"] = (report["before"]["environment_pseudonym"] == report["after"]["environment_pseudonym"] and gate(report["after"]))
            persist()
            if not report["identity_stable"]:
                raise ValueError("target identity or gate changed")
        if all(row["acquisition"] == "OBSERVED_API_SUCCESS" for row in report["observations"]):
            report["status"] = "SIX_REQUESTS_OBSERVED"
    except Exception as error:
        report["failure_type"] = type(error).__name__
    finally:
        try:
            api.shutdown()
            report["ipc_shutdown"] = "RETURNED"
        except Exception as error:
            report["ipc_shutdown"] = "UNRESOLVED"
            report["shutdown_failure_type"] = type(error).__name__
            report["status"] = "UNRESOLVED"
        report["finished_utc"] = utc(datetime.now(UTC))
        persist()
    return report


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--terminal-path", type=Path, required=True)
    parser.add_argument("--existing-terminal-pid", type=int, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args(argv)
    repo = Path(__file__).resolve().parents[1]
    output = args.output.resolve()
    if output.is_relative_to(repo):
        parser.error("output must be external to repository")
    output.mkdir(parents=True, exist_ok=False)
    try:
        existing_terminal(args.terminal_path, args.existing_terminal_pid)
        forward.verify_frozen_production_sources(repo)
        expected = (repo / "src").resolve()
        for name, module in tuple(sys.modules.items()):
            if name == "quasartrend" or name.startswith("quasartrend."):
                source = getattr(module, "__file__", None)
                if source is None or not Path(source).resolve().is_relative_to(expected):
                    raise ValueError("runtime source provenance mismatch")
        api = importlib.import_module("MetaTrader5")
        report = probe(api, args.terminal_path, output, datetime.now(UTC))
    except Exception as error:
        report = dict(status="UNRESOLVED", failure_type=type(error).__name__, execution=False, capture=False, closure_certificate=False)
        (output / "preflight_failure.json").write_bytes(canonical(report)+b"\n")
    print(json.dumps({key: report[key] for key in ("status", "execution", "capture", "closure_certificate")}))
    return 0 if report["status"] == "SIX_REQUESTS_OBSERVED" else 2


if __name__ == "__main__":
    raise SystemExit(main())
