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
    except (PermissionError, RuntimeError, ValueError) as error:
        print(json.dumps({"status": "BLOCKED", "blocker": str(error)}, sort_keys=True))
        return 2
    try:
        audit = service.audit
        print(json.dumps({
            "status": "PASS" if audit.audit_allowed else "BLOCKED",
            "audit": audit.snapshot,
            "permissions": {
                "audit_allowed": audit.audit_allowed,
                "audit_blocker": audit.audit_blocker,
                "capture_allowed": audit.capture_allowed,
                "capture_blocker": audit.capture_blocker,
                "execution_mode_requested": audit.execution_mode,
                "execution_permissions_proven": audit.execution_permissions_proven,
                "execution_permission_blocker": audit.execution_permission_blocker,
                "execution_allowed": audit.execution_allowed,
                "execution_blocker": audit.execution_blocker,
            },
        }, sort_keys=True))
        if args.mode == "audit": return 0 if audit.audit_allowed else 2
        stopping = False
        def stop(*_: object) -> None:
            nonlocal stopping; stopping = True
        signal.signal(signal.SIGINT, stop); signal.signal(signal.SIGTERM, stop)
        service.run(lambda: stopping, args.interval_seconds)
        print(json.dumps(service.health(), sort_keys=True))
        return 0
    except (PermissionError, RuntimeError, ValueError) as error:
        print(json.dumps({"status": "BLOCKED", "blocker": str(error)}, sort_keys=True))
        return 2
    finally:
        shutdown = getattr(service.mt5, "shutdown", None)
        if callable(shutdown): shutdown()


if __name__ == "__main__":
    raise SystemExit(main())
