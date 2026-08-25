from __future__ import annotations
import argparse
from hashlib import sha256
from pathlib import Path
from quasartrend.research.pipeline import build_canonical_bundle
from quasartrend.research.regime_features import feature_definition_artifact,feature_definition_json,build_setup_regime_feature_rows
from quasartrend.research.regime_diagnosis import build_diagnosis_evidence,finalize_diagnosis,diagnosis_json
from quasartrend.research.regime_candidates import (
 FROZEN_DIAGNOSIS_SHA256,FROZEN_FEATURE_ARTIFACT_SHA256,development_report,write_development_report)
def main():
 p=argparse.ArgumentParser();p.add_argument('--golden-15m',type=Path,required=True);p.add_argument('--golden-4h',type=Path,required=True);p.add_argument('--feature-definitions',type=Path,required=True);p.add_argument('--diagnosis',type=Path,required=True);p.add_argument('--output',type=Path,default=Path('exports/phase7_2/phase72_regime_development.json'));p.add_argument('--overwrite',action='store_true');a=p.parse_args()
 feature_bytes=a.feature_definitions.read_bytes();diagnosis_bytes=a.diagnosis.read_bytes()
 if sha256(feature_bytes).hexdigest()!=FROZEN_FEATURE_ARTIFACT_SHA256 or sha256(diagnosis_bytes).hexdigest()!=FROZEN_DIAGNOSIS_SHA256:raise ValueError('frozen prerequisite SHA-256 mismatch')
 b=build_canonical_bundle(golden_15m=a.golden_15m,golden_4h=a.golden_4h);art=feature_definition_artifact(b);rows=build_setup_regime_feature_rows(b);ev=build_diagnosis_evidence(b,art,rows);expected=diagnosis_json(finalize_diagnosis(ev,'INCONCLUSIVE'))
 if feature_bytes!=feature_definition_json(art) or diagnosis_bytes!=expected:raise ValueError('exact prerequisite artifacts required')
 report=development_report(b,art,ev,rows);write_development_report(b,art,ev,rows,report,a.output,overwrite=a.overwrite);return 0
if __name__=='__main__':raise SystemExit(main())
