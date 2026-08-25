"""Write the frozen Phase 7.2 regime-feature definition artifact."""
from __future__ import annotations

import argparse
from pathlib import Path

from quasartrend.research.pipeline import build_canonical_bundle
from quasartrend.research.regime_features import write_feature_definition_artifact


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--golden-15m", type=Path, required=True)
    parser.add_argument("--golden-4h", type=Path, required=True)
    parser.add_argument(
        "--output",
        type=Path,
        default=Path("exports/phase7_2/phase72_regime_feature_definitions.json"),
    )
    parser.add_argument("--overwrite", action="store_true")
    args = parser.parse_args()
    bundle = build_canonical_bundle(
        golden_15m=args.golden_15m,
        golden_4h=args.golden_4h,
    )
    write_feature_definition_artifact(bundle, args.output, overwrite=args.overwrite)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
