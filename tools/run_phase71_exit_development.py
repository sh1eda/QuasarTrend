"""Generate the locked Phase 7.1 Stage 2 development-only exit report."""
from __future__ import annotations

import argparse
from pathlib import Path

from quasartrend.research.exit_experiments import development_report, write_development_report
from quasartrend.research.pipeline import build_canonical_bundle


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--golden-15m", type=Path, required=True)
    parser.add_argument("--golden-4h", type=Path, required=True)
    parser.add_argument("--declared-symbol", default="BTCUSDT")
    parser.add_argument("--output", type=Path, default=Path("exports/phase7_1/phase71_exit_development.json"))
    parser.add_argument("--overwrite", action="store_true")
    args = parser.parse_args()
    bundle = build_canonical_bundle(
        golden_15m=args.golden_15m, golden_4h=args.golden_4h,
        declared_symbol=args.declared_symbol,
    )
    write_development_report(development_report(bundle), args.output, overwrite=args.overwrite)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
