#!/usr/bin/env python3
"""Bounded OFFLINE recovery evidence, optionally followed by read-only MT5 audit.

Never instantiates a capture service or requests broker history/orders. Synthetic
tests exercise a fake API and cannot establish native history synchronization.
"""
from __future__ import annotations

import argparse
from datetime import UTC, datetime
from hashlib import sha256
import importlib
import importlib.metadata
import json
from pathlib import Path
import platform
import subprocess
import sys
import xml.etree.ElementTree as ET


SELECTION = (
    "tests/test_forward_capture_integrity.py", "tests/test_forward_mt5.py",
    "tests/test_forward_xm.py", "tests/test_replay.py", "tests/test_strategy_engine.py",
    "tests/test_persistence.py", "tests/test_checkpoints.py",
    "tests/test_batch_incremental.py", "tests/test_xauusd_golden.py",
    "tests/test_tradingview_golden.py", "tests/test_forward_authorization_blockers.py",
)
EXPECTED_TESTS = 263
PYTEST_CHILD = """
from pathlib import Path
import sys
import quasartrend.forward.mt5 as forward
root = (Path(sys.argv[1]) / 'src').resolve()
for name, module in tuple(sys.modules.items()):
    if name == 'quasartrend' or name.startswith('quasartrend.'):
        source = getattr(module, '__file__', None)
        if source and not Path(source).resolve().is_relative_to(root):
            raise RuntimeError('pytest child imported a different source tree')
if forward.FORWARD_CAPTURE_INTEGRITY_AUTHORIZED is not False:
    raise RuntimeError('pytest child capture gate enabled')
import pytest
raise SystemExit(pytest.main(sys.argv[2:]))
"""


def digest(path: Path) -> str:
    return sha256(path.read_bytes()).hexdigest()


def git(repo: Path, *args: str) -> str:
    return subprocess.run(
        ["git", *args], cwd=repo, check=True, capture_output=True,
        text=True, timeout=15,
    ).stdout.strip()


def package_version(name: str) -> str | None:
    try:
        return importlib.metadata.version(name)
    except importlib.metadata.PackageNotFoundError:
        return None


def source_identity(repo: Path) -> dict:
    """Verify actual imports, not merely the requested checkout's filenames."""
    forward = importlib.import_module("quasartrend.forward.mt5")
    frozen = forward.verify_frozen_production_sources(repo)
    if len(frozen) != 22 or forward.FORWARD_CAPTURE_INTEGRITY_AUTHORIZED is not False:
        raise ValueError("frozen source count or disabled capture gate mismatch")
    imported = {}
    for name, module in tuple(sys.modules.items()):
        if name == "quasartrend" or name.startswith("quasartrend."):
            source = getattr(module, "__file__", None)
            if source:
                path = Path(source).resolve()
                relative = path.relative_to((repo / "src").resolve())
                imported[name] = {"path": "src/" + relative.as_posix(), "sha256": digest(path)}
    paths = set(repo.glob("src/quasartrend/**/*.py"))
    paths.update(repo / item for item in SELECTION)
    paths.update(repo / item for item in frozen)
    paths.update((repo / "pyproject.toml", Path(__file__).resolve()))
    paths.update(path for path in (repo / "conftest.py", repo / "tests/conftest.py") if path.exists())
    manifest = {}
    for path in sorted(paths):
        data = path.read_bytes()
        if b"\r" in data:
            raise ValueError("source LF requirement failed")
        manifest[path.relative_to(repo).as_posix()] = sha256(data).hexdigest()
    return {
        "head": git(repo, "rev-parse", "HEAD"),
        "branch": git(repo, "branch", "--show-current"),
        "dirty_paths": git(repo, "status", "--porcelain=v1", "--untracked-files=all").splitlines(),
        "frozen_22": frozen, "source_sha256": manifest,
        "imported_runtime": imported, "source_lf_verified": True,
        "capture_authorized": False,
    }


def junit_result(path: Path, returncode: int) -> dict:
    root = ET.parse(path).getroot()
    cases = list(root.iter("testcase"))
    failed = sum(case.find("failure") is not None or case.find("error") is not None for case in cases)
    skipped = sum(case.find("skipped") is not None for case in cases)
    return {
        "returncode": returncode, "tests": len(cases), "failed": failed,
        "skipped": skipped, "expected_tests": EXPECTED_TESTS,
        "selection_complete": returncode == 0 and len(cases) == EXPECTED_TESTS and not failed and not skipped,
        "junit_sha256": digest(path),
    }


def terminal_audit(path: Path) -> dict:
    """Explicit opt-in metadata audit; initialize/shutdown bind the IPC session."""
    if platform.system() != "Windows" or not path.is_file():
        raise ValueError("terminal audit requires Windows and existing explicit terminal file")
    mt5 = importlib.import_module("MetaTrader5")
    forward = importlib.import_module("quasartrend.forward.mt5")
    try:
        if not mt5.initialize(path=str(path.resolve()), timeout=15_000):
            raise RuntimeError("terminal initialization failed")
        audit = forward.audit_capabilities(mt5, execution_mode="none")
        info = mt5.terminal_info()
        actual = Path(str(getattr(info, "path", ""))).resolve()
        if actual != path.resolve().parent:
            raise ValueError("attached terminal identity differs from requested directory")
        # Persist only established account-free snapshot fields; no login,
        # credentials, terminal/data paths, arbitrary last_error or descriptions.
        snapshot = audit.snapshot
        result = {
            "status": "OBSERVED", "terminal_file_sha256": digest(path),
            "environment_pseudonym": audit.environment_id,
            "mt5_version": snapshot["mt5_version"],
            "terminal": snapshot["terminal"], "account": snapshot["account"],
            "symbol": {key: value for key, value in snapshot["symbol"].items() if key not in {"description", "bank"}},
            "checks": snapshot["checks"],
            "target_server9_demo_proven": audit.audit_allowed and audit.server == "XMGlobal-MT5 9",
            "capture_allowed": audit.capture_allowed, "execution_allowed": audit.execution_allowed,
        }
        if result["capture_allowed"] or result["execution_allowed"]:
            raise ValueError("capture or execution gate unexpectedly enabled")
        return result
    finally:
        mt5.shutdown()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, required=True, help="new evidence directory; never overwritten")
    parser.add_argument("--timeout-seconds", type=int, default=600, choices=range(30, 1801), metavar="30..1800")
    parser.add_argument("--audit-terminal-path", type=Path, help="explicit optional Windows terminal64.exe metadata audit")
    args = parser.parse_args(argv)
    repo = Path(__file__).resolve().parents[1]
    # The output must be external to the checkout so it cannot alter recorded
    # dirty-state identity during validation; paths are not printed in evidence.
    output = args.output.resolve()
    if output.is_relative_to(repo):
        parser.error("output must be outside the repository")
    output.mkdir(parents=True, exist_ok=False)
    started = datetime.now(UTC).isoformat()
    report = {
        "schema": "xm-forward-offline-recovery-evidence/v1", "started_utc": started,
        "mode": "OFFLINE_WITH_OPTIONAL_METADATA_AUDIT" if args.audit_terminal_path else "OFFLINE",
        "evidence_status": "INCOMPLETE", "authorization": "BLOCKED",
        "execution": False, "native_history_synchronization": "NOT_OBSERVED",
        "native_terminal_restart_recovery": "NOT_OBSERVED",
        "gold_session_closure_completeness": "UNRESOLVED",
        "terminal_audit": {"status": "NOT_OBSERVED"},
        "runtime": {
            "system": platform.system(), "release": platform.release(),
            "machine": platform.machine(), "python": platform.python_version(),
            "python_implementation": platform.python_implementation(),
            "interpreter_sha256": digest(Path(sys.executable)),
            "packages": {name: package_version(name) for name in ("pytest", "quasartrend", "MetaTrader5", "numpy")},
        },
        "selection": list(SELECTION),
    }
    try:
        # Deliberately do not repair sys.path: a wrong installed import is a
        # failed provenance check, not something this collector may conceal.
        report["stage"] = "source_preflight"
        before = source_identity(repo)
        report["before"] = before
        report["stage"] = "synthetic_tests"
        try:
            completed = subprocess.run(
                [sys.executable, "-c", PYTEST_CHILD, str(repo), *SELECTION, "-q", "--tb=short", f"--junitxml={output / 'junit.xml'}"],
                cwd=repo, capture_output=True, text=True, timeout=args.timeout_seconds,
            )
        finally:
            after = source_identity(repo)
            report["after"] = after
            report["source_unchanged"] = before == after
        # Synthetic pytest output can contain workstation paths; redact known
        # prefixes before saving. No terminal/API error text is ever included.
        combined = completed.stdout + completed.stderr
        for prefix, label in ((str(output), "<OUTPUT>"), (str(repo), "<REPO>"), (str(Path.home()), "<HOME>")):
            combined = combined.replace(prefix, label)
        (output / "pytest.txt").write_text(combined, encoding="utf-8")
        report["synthetic_tests"] = junit_result(output / "junit.xml", completed.returncode)
        if not report["source_unchanged"] or not report["synthetic_tests"]["selection_complete"]:
            raise ValueError("source identity changed or selected synthetic suite incomplete")
        if args.audit_terminal_path:
            report["stage"] = "terminal_metadata_audit"
            report["terminal_audit"] = terminal_audit(args.audit_terminal_path)
            if not report["terminal_audit"]["target_server9_demo_proven"]:
                raise ValueError("target server-9 demo metadata not proven")
        report["evidence_status"] = "COMPLETE_SYNTHETIC_EVIDENCE"
        report["stage"] = "complete"
    except Exception as error:
        # Arbitrary exception messages may contain local paths/API credentials.
        report["failure_type"] = type(error).__name__
        report["evidence_status"] = "INVALID_OR_INCOMPLETE"
    report["finished_utc"] = datetime.now(UTC).isoformat()
    (output / "evidence.json").write_text(json.dumps(report, sort_keys=True, indent=2) + "\n", encoding="utf-8")
    print(json.dumps({key: report[key] for key in ("evidence_status", "authorization", "execution")}))
    return 0 if report["evidence_status"] == "COMPLETE_SYNTHETIC_EVIDENCE" else 2


if __name__ == "__main__":
    raise SystemExit(main())
