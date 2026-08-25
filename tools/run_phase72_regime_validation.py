from __future__ import annotations
import argparse,json
from hashlib import sha256
from pathlib import Path
from quasartrend.research.pipeline import build_canonical_bundle
from quasartrend.research.regime_features import feature_definition_artifact,feature_definition_json,build_setup_regime_feature_rows
from quasartrend.research.regime_diagnosis import build_diagnosis_evidence,finalize_diagnosis,diagnosis_json
from quasartrend.research.regime_candidates import (
    FROZEN_DIAGNOSIS_SHA256, FROZEN_FEATURE_ARTIFACT_SHA256, REVIEW_ATTESTATION,
    development_json, development_report, register_candidate,
    validation_report, write_validation_report,
)
def main():
 p=argparse.ArgumentParser();p.add_argument('--golden-15m',type=Path,required=True);p.add_argument('--golden-4h',type=Path,required=True);p.add_argument('--feature-definitions',type=Path,required=True);p.add_argument('--diagnosis',type=Path,required=True);p.add_argument('--development',type=Path,required=True);p.add_argument('--advancing-candidate',action='append',default=[]);p.add_argument('--review-attestation',required=True);p.add_argument('--output',type=Path,default=Path('exports/phase7_2/phase72_regime_validation.json'));p.add_argument('--overwrite',action='store_true');a=p.parse_args()
 feature_bytes=a.feature_definitions.read_bytes();diagnosis_bytes=a.diagnosis.read_bytes()
 if sha256(feature_bytes).hexdigest()!=FROZEN_FEATURE_ARTIFACT_SHA256 or sha256(diagnosis_bytes).hexdigest()!=FROZEN_DIAGNOSIS_SHA256:raise ValueError('frozen prerequisite SHA-256 mismatch')
 b=build_canonical_bundle(golden_15m=a.golden_15m,golden_4h=a.golden_4h);art=feature_definition_artifact(b);rows=build_setup_regime_feature_rows(b);ev=build_diagnosis_evidence(b,art,rows);dev=development_report(b,art,ev,rows)
 if feature_bytes!=feature_definition_json(art) or diagnosis_bytes!=diagnosis_json(finalize_diagnosis(ev,'INCONCLUSIVE')) or a.development.read_bytes()!=development_json(b,art,ev,rows,dev):raise ValueError('exact prerequisite artifacts required')
 if a.review_attestation != REVIEW_ATTESTATION: raise ValueError('exact Luna review attestation required')
 regs=tuple(register_candidate(b,art,ev,rows,dev,x,a.review_attestation) for x in a.advancing_candidate);report=validation_report(b,art,ev,rows,dev,regs);write_validation_report(b,art,ev,rows,dev,regs,report,a.output,overwrite=a.overwrite);return 0
if __name__=='__main__':raise SystemExit(main())
