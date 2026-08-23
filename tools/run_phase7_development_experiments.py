"""Run only the predeclared Phase 7 development-window experiments."""
from __future__ import annotations

import argparse
from pathlib import Path

from quasartrend.research.experiments import development_report, write_experiment_report
from quasartrend.research.pipeline import build_canonical_bundle


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--golden-15m", type=Path, required=True)
    parser.add_argument("--golden-4h", type=Path, required=True)
    parser.add_argument("--declared-symbol", default="BTCUSDT")
    parser.add_argument(
        "--output",
        type=Path,
        default=Path("exports/phase7/phase7_development_experiments.json"),
    )
    parser.add_argument("--overwrite", action="store_true")
    args = parser.parse_args()
    bundle = build_canonical_bundle(
        golden_15m=args.golden_15m,
        golden_4h=args.golden_4h,
        declared_symbol=args.declared_symbol,
    )
    write_experiment_report(
        development_report(bundle), args.output, overwrite=args.overwrite
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
