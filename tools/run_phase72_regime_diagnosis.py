"""Run the finalized, development-only Phase 7.2 Stage 1 diagnosis."""
from __future__ import annotations

import argparse
from pathlib import Path

from quasartrend.research.pipeline import build_canonical_bundle
from quasartrend.research.regime_diagnosis import (
    build_diagnosis_evidence,
    finalize_diagnosis,
    write_diagnosis,
)
from quasartrend.research.regime_features import (
    build_setup_regime_feature_rows,
    feature_definition_artifact,
    feature_definition_json,
)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--golden-15m", type=Path, required=True)
    parser.add_argument("--golden-4h", type=Path, required=True)
    parser.add_argument(
        "--feature-definitions", type=Path,
        default=Path("exports/phase7_2/phase72_regime_feature_definitions.json"),
    )
    parser.add_argument(
        "--output", type=Path,
        default=Path("exports/phase7_2/phase72_regime_diagnosis.json"),
    )
    parser.add_argument("--hypothesis-conclusion", required=True,
                        choices=("SUPPORTED", "NOT_SUPPORTED", "INCONCLUSIVE"))
    parser.add_argument("--overwrite", action="store_true")
    args = parser.parse_args()
    bundle = build_canonical_bundle(golden_15m=args.golden_15m, golden_4h=args.golden_4h)
    artifact = feature_definition_artifact(bundle)
    if args.feature_definitions.read_bytes() != feature_definition_json(artifact):
        raise ValueError("feature-definition bytes do not match the exact canonical artifact")
    evidence = build_diagnosis_evidence(bundle, artifact, build_setup_regime_feature_rows(bundle))
    write_diagnosis(finalize_diagnosis(evidence, args.hypothesis_conclusion), args.output, overwrite=args.overwrite)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
