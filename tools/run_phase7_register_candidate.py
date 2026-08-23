"""Register the sole Phase 7 candidate from development evidence only."""
from __future__ import annotations

import argparse
from pathlib import Path

from quasartrend.research.candidates import register_development_candidate, write_artifact
from quasartrend.research.experiments import development_report
from quasartrend.research.pipeline import build_canonical_bundle


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--golden-15m", type=Path, required=True)
    parser.add_argument("--golden-4h", type=Path, required=True)
    parser.add_argument("--output", type=Path, default=Path("exports/phase7/phase7_candidate_registration.json"))
    parser.add_argument("--overwrite", action="store_true")
    args = parser.parse_args()
    bundle = build_canonical_bundle(golden_15m=args.golden_15m, golden_4h=args.golden_4h)
    registration = register_development_candidate(bundle, development_report(bundle))
    write_artifact(registration, args.output, overwrite=args.overwrite)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
