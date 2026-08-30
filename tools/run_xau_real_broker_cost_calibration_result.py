"""Generate the protocol-guarded XAU broker-cost-calibration WAITING report."""
from __future__ import annotations

import argparse
from pathlib import Path

from quasartrend.research.xau_real_broker_cost_calibration import PROTOCOL_PATH
from quasartrend.research.xau_real_broker_cost_calibration_result import RESULT_PATH, build_result_guarded, write_result


def main() -> int:
    parser = argparse.ArgumentParser(description="Generate XAU broker-cost WAITING report; no net-cost economics")
    parser.add_argument("--repo-root", type=Path, default=Path("."))
    parser.add_argument("--protocol", type=Path, default=Path(PROTOCOL_PATH))
    parser.add_argument("--output", type=Path, default=Path(RESULT_PATH))
    args = parser.parse_args()
    result = build_result_guarded(args.repo_root, args.protocol)
    digest = write_result(result, args.output)
    print(f"result path: {args.output}")
    print(f"result SHA-256: {digest}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
