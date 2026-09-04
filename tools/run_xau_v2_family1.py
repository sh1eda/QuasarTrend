#!/usr/bin/env python3
"""Run or verify the immutable staged XAU V2 Family 1 experiment."""
from __future__ import annotations

import argparse
from pathlib import Path

from quasartrend.research.xau_v2_family1 import (
    CSV_PATH,
    LOCK_DIRECTORY,
    RESULT_PATH,
    execute_real_family1,
    family1_csv,
    family1_json,
    load_real_family1_inputs,
    verify_real_input_identities,
    write_family1_artifacts,
)


def main() -> int:
    parser = argparse.ArgumentParser(description="Run frozen XAU V2 Family 1 directional-asymmetry walk-forward research")
    parser.add_argument("--repo-root", type=Path, default=Path("."))
    parser.add_argument("--verify-identities-only", action="store_true", help="read hashes and setup-context counts only; do not admit or evaluate economics")
    parser.add_argument("--verify-regenerate", action="store_true", help="rerun and require existing locks/result/CSV to be byte-identical")
    args = parser.parse_args()
    root = args.repo_root.resolve()
    identities = verify_real_input_identities(root)
    if args.verify_identities_only:
        for name, value in identities.items():
            print(f"{name}: {value}")
        return 0
    inputs = load_real_family1_inputs(root)
    result, csv_rows = execute_real_family1(inputs, lock_directory=root / LOCK_DIRECTORY)
    hashes = write_family1_artifacts(result_path=root / RESULT_PATH, csv_path=root / CSV_PATH, result=result, csv_rows=csv_rows)
    if args.verify_regenerate:
        if (root / RESULT_PATH).read_bytes() != family1_json(result) or (root / CSV_PATH).read_bytes() != family1_csv(csv_rows):
            raise ValueError("Family 1 regeneration is not byte-identical")
    print(f"result: {RESULT_PATH}")
    print(f"result SHA-256: {hashes['result_sha256']}")
    print(f"trades CSV: {CSV_PATH}")
    print(f"trades CSV SHA-256: {hashes['csv_sha256']}")
    print(f"verdict: {result['verdict']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
