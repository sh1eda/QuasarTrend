"""Build the frozen Phase 7.3 descriptive attribution artifact."""
from __future__ import annotations

import argparse
from pathlib import Path

from quasartrend.research.edge_attribution import build_edge_attribution_report, write_edge_attribution_report
from quasartrend.research.pipeline import build_canonical_bundle
from quasartrend.research.regime_features import build_setup_regime_feature_rows, feature_definition_artifact
from quasartrend.research.regime_features import feature_definition_json
from quasartrend.research.edge_attribution import PHASE72_FEATURE_ARTIFACT_SHA256
from hashlib import sha256


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--golden-15m", type=Path, default=Path("tests/golden/tradingview_15m.csv"))
    parser.add_argument("--golden-4h", type=Path, default=Path("tests/golden/tradingview_4h.csv"))
    parser.add_argument("--output", type=Path, default=Path("exports/phase7_3/phase73_edge_attribution.json"))
    parser.add_argument("--feature-definitions", type=Path, default=Path("exports/phase7_2/phase72_regime_feature_definitions.json"))
    parser.add_argument("--overwrite", action="store_true")
    args = parser.parse_args()
    bundle = build_canonical_bundle(golden_15m=args.golden_15m, golden_4h=args.golden_4h)
    artifact = feature_definition_artifact(bundle)
    supplied = args.feature_definitions.read_bytes()
    if sha256(supplied).hexdigest() != PHASE72_FEATURE_ARTIFACT_SHA256 or supplied != feature_definition_json(artifact):
        raise ValueError("checked-in Phase 7.2 feature-definition artifact mismatch")
    report = build_edge_attribution_report(bundle, artifact, build_setup_regime_feature_rows(bundle))
    write_edge_attribution_report(bundle, artifact, build_setup_regime_feature_rows(bundle), report, args.output, overwrite=args.overwrite)


if __name__ == "__main__":
    main()
