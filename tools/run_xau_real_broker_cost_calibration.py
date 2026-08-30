"""Create the Stage A-only XAU real broker cost-calibration protocol lock."""
from __future__ import annotations

import argparse
from pathlib import Path

from quasartrend.research.xau_real_broker_cost_calibration import (
    PROTOCOL_PATH,
    build_xau_real_broker_cost_calibration_protocol,
    write_xau_real_broker_cost_calibration_protocol,
)


def main() -> int:
    parser = argparse.ArgumentParser(description="Freeze the XAU broker-cost calibration protocol; no economics are calculated")
    parser.add_argument("--output", type=Path, default=Path(PROTOCOL_PATH))
    args = parser.parse_args()
    protocol = build_xau_real_broker_cost_calibration_protocol()
    digest = write_xau_real_broker_cost_calibration_protocol(protocol, args.output)
    print(f"protocol path: {args.output}")
    print(f"protocol SHA-256: {digest}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
