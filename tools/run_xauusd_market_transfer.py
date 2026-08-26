"""Generate the frozen XAUUSD market-transfer baseline artifact."""
from __future__ import annotations

import argparse
from pathlib import Path

from quasartrend.research.market_transfer import build_market_transfer_baseline, write_market_transfer_report


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--source-15m", type=Path, default=Path("exports/xauusd_pending/XAUUSD_15m.csv"))
    parser.add_argument("--source-4h", type=Path, default=Path("exports/xauusd_pending/XAUUSD_4h.csv"))
    parser.add_argument("--declared-symbol", default="XAUUSD")
    parser.add_argument("--output", type=Path, default=Path("exports/xauusd/phase_xau_market_transfer_baseline.json"))
    parser.add_argument("--overwrite", action="store_true")
    args = parser.parse_args()
    write_market_transfer_report(build_market_transfer_baseline(source_15m=args.source_15m, source_4h=args.source_4h, declared_symbol=args.declared_symbol), args.output, overwrite=args.overwrite)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
