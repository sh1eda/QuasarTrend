#!/usr/bin/env python3
"""Windows VDS entry point; it deliberately accepts no credentials."""
from __future__ import annotations

import argparse
import json
from pathlib import Path
import signal

from quasartrend.forward.mt5 import XMForwardService


def main() -> int:
    parser = argparse.ArgumentParser(description="Fail-closed XM GOLD frozen-V1 forward capture")
    parser.add_argument("--root", type=Path, required=True, help="evidence directory (not the golden directory)")
    parser.add_argument("--mode", choices=("audit", "capture"), default="audit", help="audit is the safe default")
    parser.add_argument("--execution-mode", choices=("none", "demo"), default="none", help="demo requires broker-side audit too; it still cannot submit orders")
    parser.add_argument("--terminal-path", type=Path, help="optional terminal64.exe path; never a credential")
    parser.add_argument("--repo-root", type=Path, default=Path("."), help="repository root used to verify frozen V1/Pine sources")
    parser.add_argument("--interval-seconds", type=float, default=5.0)
    args = parser.parse_args()
    try:
        service = XMForwardService(args.root, execution_mode=args.execution_mode, terminal_path=args.terminal_path, repo_root=args.repo_root, activate=args.mode == "capture")
    except (PermissionError, RuntimeError, ValueError):
        print("XM DEMO EXECUTION: BLOCKED — DEMO ACCOUNT IDENTITY NOT PROVEN")
        return 2
    try:
        print(json.dumps({"audit": service.audit.snapshot, "demo_proven": service.audit.demo_proven, "allowed": service.audit.allowed, "blocker": service.audit.blocker}, sort_keys=True))
        if args.mode == "audit": return 0
        stopping = False
        def stop(*_: object) -> None:
            nonlocal stopping; stopping = True
        signal.signal(signal.SIGINT, stop); signal.signal(signal.SIGTERM, stop)
        service.run(lambda: stopping, args.interval_seconds)
        print(json.dumps(service.health(), sort_keys=True))
        return 0
    except (PermissionError, RuntimeError, ValueError):
        print("XM DEMO EXECUTION: BLOCKED — DEMO ACCOUNT IDENTITY NOT PROVEN")
        return 2
    finally:
        shutdown = getattr(service.mt5, "shutdown", None)
        if callable(shutdown): shutdown()


if __name__ == "__main__":
    raise SystemExit(main())
