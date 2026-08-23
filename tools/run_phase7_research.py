"""Run the deterministic, observational Phase 7 baseline report."""
from __future__ import annotations
import argparse
from pathlib import Path
from quasartrend.research.pipeline import baseline_report, build_canonical_bundle, write_report

def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--golden-15m", type=Path, required=True)
    parser.add_argument("--golden-4h", type=Path, required=True)
    parser.add_argument("--declared-symbol", default="BTCUSDT")
    parser.add_argument("--output", type=Path, default=Path("exports/phase7/phase7_baseline.json"))
    parser.add_argument("--overwrite", action="store_true")
    args = parser.parse_args()
    bundle = build_canonical_bundle(golden_15m=args.golden_15m, golden_4h=args.golden_4h, declared_symbol=args.declared_symbol)
    write_report(baseline_report(bundle), args.output, overwrite=args.overwrite)
    return 0
if __name__ == "__main__": raise SystemExit(main())
