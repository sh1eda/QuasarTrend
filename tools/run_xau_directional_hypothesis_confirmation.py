"""Emit the XAU confirmation WAITING status; this CLI never runs economics."""
from __future__ import annotations

import argparse
from pathlib import Path

from quasartrend.research.xau_directional_hypothesis_confirmation import (
    build_waiting_status,
    confirmation_json,
    execute_confirmatory_economics,
    write_waiting_status,
)


def main() -> int:
    parser = argparse.ArgumentParser(description="Inventory XAU sources and emit confirmation WAITING status")
    parser.add_argument("--repo-root", type=Path, default=Path("."))
    parser.add_argument("--output", type=Path, help="optional deterministic WAITING status artifact path")
    parser.add_argument("--execute-confirmatory-economics", action="store_true", help=argparse.SUPPRESS)
    args = parser.parse_args()
    if args.execute_confirmatory_economics:
        execute_confirmatory_economics()
    status = build_waiting_status(args.repo_root)
    payload = confirmation_json(status)
    if args.output is None:
        print(payload.decode("utf-8"), end="")
    else:
        digest = write_waiting_status(status, args.output)
        print(f"status path: {args.output}")
        print(f"status SHA-256: {digest}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
