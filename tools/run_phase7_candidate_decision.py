"""Record rejection when the registered candidate fails validation."""
from __future__ import annotations

import argparse
from pathlib import Path

from quasartrend.research.candidates import (
    artifact_json,
    register_development_candidate,
    reject_after_validation,
    validation_report,
    write_artifact,
)
from quasartrend.research.experiments import development_report
from quasartrend.research.pipeline import build_canonical_bundle


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--golden-15m", type=Path, required=True)
    parser.add_argument("--golden-4h", type=Path, required=True)
    parser.add_argument("--registration", type=Path, required=True)
    parser.add_argument("--validation", type=Path, required=True)
    parser.add_argument("--output", type=Path, default=Path("exports/phase7/phase7_candidate_decision.json"))
    parser.add_argument("--overwrite", action="store_true")
    args = parser.parse_args()
    bundle = build_canonical_bundle(golden_15m=args.golden_15m, golden_4h=args.golden_4h)
    registration = register_development_candidate(bundle, development_report(bundle))
    validation = validation_report(bundle, registration)
    if args.registration.read_bytes() != artifact_json(registration):
        raise ValueError("registration artifact does not match canonical registration")
    if args.validation.read_bytes() != artifact_json(validation):
        raise ValueError("validation artifact does not match canonical validation")
    write_artifact(reject_after_validation(bundle, registration, validation), args.output, overwrite=args.overwrite)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
