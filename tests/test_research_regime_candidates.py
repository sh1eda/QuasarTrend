from dataclasses import replace
from functools import lru_cache
from pathlib import Path
import pytest
from quasartrend.research.pipeline import build_canonical_bundle
from quasartrend.research.regime_features import feature_definition_artifact,build_setup_regime_feature_rows
from quasartrend.research.regime_diagnosis import build_diagnosis_evidence
import quasartrend.research.regime_candidates as candidates
from quasartrend.research.regime_candidates import (
    CANDIDATE_SEMANTIC_FINGERPRINT, CANDIDATE_SPECS, DEVELOPMENT_WINDOWS, VALIDATION_WINDOWS,
    FINAL_OOS_BASELINE_CLOSED_FLOOR, KNOWN_FINAL_OOS_BASELINE_CLOSED,
    CandidateMetrics, REVIEW_ATTESTATION, _gate, _population, development_json, development_report,
    register_candidate, validate_development_report, validation_report,
    write_development_report, write_validation_report,
)


@lru_cache(maxsize=1)
def _canonical():
 b=build_canonical_bundle(golden_15m=Path('tests/golden/tradingview_15m.csv'),golden_4h=Path('tests/golden/tradingview_4h.csv'))
 a=feature_definition_artifact(b); rows=build_setup_regime_feature_rows(b)
 return b,a,rows,build_diagnosis_evidence(b,a,rows)

def test_frozen_setup_origin_predicates_and_development_report():
 assert CANDIDATE_SPECS[0].accepts(16) and CANDIDATE_SPECS[0].accepts(60) and not CANDIDATE_SPECS[0].accepts(17) and not CANDIDATE_SPECS[0].accepts(None)
 assert CANDIDATE_SPECS[1].accepts(.06127766764569597) and CANDIDATE_SPECS[1].accepts(.11783780470509575) and not CANDIDATE_SPECS[1].accepts(.08)
 assert CANDIDATE_SPECS[2].accepts(True) and not CANDIDATE_SPECS[2].accepts(False) and not CANDIDATE_SPECS[2].accepts(None)
 assert CANDIDATE_SPECS[0].operator=='<= 16 OR > 59' and CANDIDATE_SPECS[0].thresholds==(16.,59.)
 assert CANDIDATE_SPECS[1].thresholds==(.06127766764569597,.11783780470509574)
 assert CANDIDATE_SPECS[2].missing_behavior=='reject when false or missing'
 assert len(DEVELOPMENT_WINDOWS)==4 and CANDIDATE_SEMANTIC_FINGERPRINT=='a107a69ffe37035897442f1d5eba15d658df4ece026d5c692e7bbc7b84992aa8'
 assert tuple((item.role,item.start_date,item.end_date) for item in VALIDATION_WINDOWS)==(
  ('validation_1','2026-07-10','2026-07-14'),('validation_2','2026-07-15','2026-07-19'),
  ('validation_3','2026-07-20','2026-07-24'),('validation_4','2026-07-25','2026-07-28'))
 assert (FINAL_OOS_BASELINE_CLOSED_FLOOR,KNOWN_FINAL_OOS_BASELINE_CLOSED)==(50,30)
 b,a,rows,evidence=_canonical();r=development_report(b,a,evidence,rows)
 assert len(r.results)==3 and r.role=='development' and r.inaccessible_roles==('validation','final_oos')


def test_reports_are_exact_bound_and_final_oos_stays_locked(tmp_path):
 b,a,rows,evidence=_canonical(); report=development_report(b,a,evidence,rows)
 validate_development_report(b,a,evidence,rows,report)
 with pytest.raises(ValueError):
  validate_development_report(b,a,evidence,rows,replace(report,eligible_setups=0))
 # No review registration means validation performs no final-OOS access.
 validation=validation_report(b,a,evidence,rows,report,())
 assert validation.final_oos_accessed is False
 assert validation.final_oos_status=='not_accessed_no_validation_survivor'
 assert validation.production_change is False and validation.registrations==()
 assert development_json(b,a,evidence,rows,report)==development_json(b,a,evidence,rows,report)
 target=tmp_path/'report.json';write_development_report(b,a,evidence,rows,report,target)
 with pytest.raises(FileExistsError): write_development_report(b,a,evidence,rows,report,target)
 with pytest.raises(ValueError): write_development_report(b,a,evidence,rows,replace(report,role='validation'),tmp_path/'forged.json')
 write_validation_report(b,a,evidence,rows,report,(),validation,tmp_path/'validation.json')
 with pytest.raises(ValueError):
  write_validation_report(b,a,evidence,rows,report,(),replace(validation,production_change=True),tmp_path/'forged-validation.json')


def test_registration_refuses_forged_development_or_attestation():
 b,a,rows,evidence=_canonical(); report=development_report(b,a,evidence,rows)
 candidate=report.results[0].spec.candidate_id
 with pytest.raises(ValueError):
  register_candidate(b,a,evidence,rows,replace(report,source_scope='forged'),candidate,REVIEW_ATTESTATION)
 with pytest.raises(ValueError):
  register_candidate(b,a,evidence,rows,report,candidate,'forged')
 forged_spec=replace(report.results[0].spec,thresholds=(999.,))
 with pytest.raises(ValueError):
  register_candidate(b,a,evidence,rows,replace(report,results=(replace(report.results[0],spec=forged_spec),)+report.results[1:]),candidate,REVIEW_ATTESTATION)
 with pytest.raises(ValueError):
  register_candidate(b,a,evidence,rows,replace(report,dataset_fingerprint='forged'),candidate,REVIEW_ATTESTATION)
 with pytest.raises(ValueError):
  register_candidate(b,a,evidence,rows,replace(report,results=(replace(report.results[0],candidate=replace(report.results[0].candidate,total_r=0.)),)+report.results[1:]),candidate,REVIEW_ATTESTATION)


def test_chronology_is_a_setup_origin_partition_of_the_full_development_population():
 b,a,rows,evidence=_canonical(); report=development_report(b,a,evidence,rows)
 assert report.eligible_setups==139 and report.included_closed_trades==103
 for result in report.results:
  assert sum(window.baseline.canonical_eligible_setups for window in result.windows)==report.eligible_setups
  assert sum(window.baseline.baseline_closed_trades for window in result.windows)==report.included_closed_trades
  assert sum(window.candidate.retained_setups for window in result.windows)==result.candidate.retained_setups
  assert sum(window.candidate.retained_closed_trades for window in result.windows)==result.candidate.retained_closed_trades


def test_candidate_filters_retain_unchanged_canonical_trade_rows_only():
 b,a,rows,evidence=_canonical(); report=development_report(b,a,evidence,rows)
 setups,closed=_population(b,candidates.DEVELOPMENT_WINDOW)
 setup_by_id={item.setup_id:item for item in setups}; feature_by_id={item.setup_id:item for item in rows}
 canonical_by_id={item.trade_id:item for item in closed}
 for result in report.results:
  retained={item.setup_id for item in setups if result.spec.accepts(getattr(feature_by_id[item.setup_id],result.spec.feature))}
  retained_rows=tuple(item for item in closed if item.setup_id in retained)
  rejected={item.setup_id for item in setups}-retained
  assert len(retained)==result.candidate.retained_setups
  assert len(retained_rows)==result.candidate.retained_closed_trades
  assert all(item.setup_id not in rejected for item in retained_rows)
  assert {item.trade_id for item in retained_rows} <={item.trade_id for item in closed}
  assert all(canonical_by_id[item.trade_id] == item for item in retained_rows)
  # Equality includes every entry, stop, exit, and outcome field; no trade is rebuilt.
  assert all(item.direction is setup_by_id[item.setup_id].direction for item in retained_rows)


def test_retention_counts_and_formulas_are_setup_origin_denominators():
 b,a,rows,evidence=_canonical(); report=development_report(b,a,evidence,rows)
 for result in report.results:
  baseline,candidate=result.baseline,result.candidate
  assert candidate.setup_retention==candidate.retained_setups/baseline.canonical_eligible_setups
  assert candidate.trade_retention==candidate.retained_closed_trades/baseline.baseline_closed_trades
  assert result.rejected_setups+candidate.retained_setups==baseline.canonical_eligible_setups


def _metric(**changes):
 base=CandidateMetrics(100,100,100,100,1.,1.,100.,1.,1.,2.,.5,.5,1.,1.,1.,1.,1.,1.,100.,-50.,10,10,10,5.,1.,(1.,1.,1.))
 return replace(base,**changes)


@pytest.mark.parametrize(('change','failure'),(
 ({'expectancy_r':1.},'expectancy'),({'total_r':100.},'total_r'),
 ({'opportunity_r_per_setup':1.},'opportunity_r_per_setup'),({'profit_factor':1.},'profit_factor'),
 ({'setup_retention':.249},'setup_retention'),({'positive_r_retention':.69},'positive_r_retention'),
 ({'tail_retention':(.7,.69,.7)},'tail_retention'),
))
def test_each_nonchronology_gate_component_is_frozen(change,failure):
 baseline=_metric(); candidate=_metric(**change)
 assert failure in _gate(baseline,candidate,True,True)


def test_chronology_gate_requires_two_improvements_and_no_single_delta_over_75_percent():
 baseline,candidate=_metric(),_metric(expectancy_r=1.1,total_r=110.,opportunity_r_per_setup=1.1)
 assert _gate(baseline,candidate,True,True)==()
 assert 'chronology_stability' in _gate(baseline,candidate,False,True)
 assert _gate(baseline,candidate,False,False)==()


def test_scalar_passing_candidate_cannot_pass_with_failed_validation_stability():
 baseline,candidate=_metric(),_metric(expectancy_r=1.1,total_r=110.,opportunity_r_per_setup=1.1)
 assert _gate(baseline,candidate,False,True)==('chronology_stability',)


def test_contract_and_provenance_tampering_fail_closed(monkeypatch):
 b,a,rows,evidence=_canonical()
 with pytest.raises(ValueError): development_report(b,replace(a,dataset_fingerprint='forged'),evidence,rows)
 with pytest.raises(ValueError): development_report(b,a,replace(evidence,manifest_id='forged'),rows)
 monkeypatch.setattr(candidates,'FINAL_OOS_BASELINE_CLOSED_FLOOR',1)
 with pytest.raises(ValueError): development_report(b,a,evidence,rows)


def test_known_final_oos_floor_rebinding_fails_before_candidate_evaluation(monkeypatch):
 b,a,rows,evidence=_canonical()
 monkeypatch.setattr(candidates,'KNOWN_FINAL_OOS_BASELINE_CLOSED',31)
 with pytest.raises(ValueError): development_report(b,a,evidence,rows)


@pytest.mark.parametrize('constant',('FROZEN_FEATURE_ARTIFACT_SHA256','FROZEN_DIAGNOSIS_SHA256','EXPECTED_DIAGNOSIS_EVIDENCE_FINGERPRINT'))
def test_provenance_constant_rebinding_fails_before_candidate_evaluation(monkeypatch,constant):
 b,a,rows,evidence=_canonical()
 monkeypatch.setattr(candidates,constant,'forged')
 with pytest.raises(ValueError): development_report(b,a,evidence,rows)


def test_frozen_spec_rebinding_fails_closed(monkeypatch):
 b,a,rows,evidence=_canonical()
 monkeypatch.setattr(candidates,'CANDIDATE_SPECS',CANDIDATE_SPECS[:2])
 with pytest.raises(ValueError): development_report(b,a,evidence,rows)


def test_validation_window_rebinding_fails_closed(monkeypatch):
 b,a,rows,evidence=_canonical(); development=development_report(b,a,evidence,rows)
 monkeypatch.setattr(candidates,'VALIDATION_WINDOWS',VALIDATION_WINDOWS[:3])
 with pytest.raises(ValueError): validation_report(b,a,evidence,rows,development,())


def test_registration_and_validation_accept_only_exact_actual_gate_passing_candidates(tmp_path):
 b,a,rows,evidence=_canonical(); development=development_report(b,a,evidence,rows)
 passing=tuple(item for item in development.results if item.gate_pass)
 if not passing:
  assert passing==()
  return
 registrations=tuple(register_candidate(b,a,evidence,rows,development,item.spec.candidate_id,REVIEW_ATTESTATION) for item in passing)
 validation=validation_report(b,a,evidence,rows,development,registrations)
 assert tuple(item.candidate_id for item in registrations)==tuple(item.spec.candidate_id for item in passing)
 assert tuple(item.spec for item in validation.results)==tuple(item.spec for item in registrations)
 assert set(validation.survivors)<={item.candidate_id for item in registrations}
 assert all(len(result.windows)==4 for result in validation.results)
 for result in validation.results:
  assert sum(window.baseline.canonical_eligible_setups for window in result.windows)==43
  assert sum(window.baseline.baseline_closed_trades for window in result.windows)==25
  assert sum(window.candidate.retained_setups for window in result.windows)==result.candidate.retained_setups
  assert sum(window.candidate.retained_closed_trades for window in result.windows)==result.candidate.retained_closed_trades
  assert 'chronology_stability' in result.gate_failures or result.stability_pass
 assert validation.final_oos_accessed is False and validation.production_change is False
 if validation.survivors:
  assert validation.final_oos_status=='locked_baseline_closed_floor_30_lt_50'
 with pytest.raises(ValueError): validation_report(b,a,evidence,rows,development,registrations+registrations[:1])
 with pytest.raises(ValueError):
  validation_report(b,a,evidence,rows,development,(replace(registrations[0],dataset_fingerprint='forged'),))
 with pytest.raises(ValueError):
  validation_report(b,a,evidence,rows,development,(replace(registrations[0],development_report_fingerprint='forged'),))
 with pytest.raises(ValueError):
  validation_report(b,a,evidence,rows,development,(replace(registrations[0],development_candidate=replace(registrations[0].development_candidate,total_r=0.)),))
 with pytest.raises(ValueError):
  validation_report(b,a,evidence,rows,development,(replace(registrations[0],spec=replace(registrations[0].spec,thresholds=(0.,))),))
 with pytest.raises(ValueError):
  validation_report(b,a,evidence,rows,replace(development,manifest_id='forged'),registrations)
 with pytest.raises(ValueError):
  write_validation_report(b,a,evidence,rows,development,registrations,replace(validation,production_change=True),tmp_path/'forged.json')
