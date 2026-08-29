"""Lock or execute the guarded XM GOLD historical-validation study."""
from __future__ import annotations

import argparse
from pathlib import Path

from quasartrend.research.xm_gold_historical_validation import (
    CANONICAL_STARTING_SHA,
    HISTORICAL_CUTOFF_UTC,
    RAW_SOURCE_PATH,
    build_xm_gold_historical_validation_protocol,
    verify_canonical_git_provenance,
    verify_compatibility_artifact_identity,
    verify_frozen_production_sources,
    verify_xm_raw_source_identity,
    write_xm_gold_historical_validation_protocol,
    build_xm_gold_historical_validation_report,
    write_xm_gold_historical_validation_report,
)


def main() -> int:
    parser = argparse.ArgumentParser(description="XM GOLD historical validation: Stage A lock or guarded Stage B")
    parser.add_argument("--repo-root", type=Path, default=Path("."))
    parser.add_argument("--xm-m1-source", type=Path, default=Path(RAW_SOURCE_PATH))
    parser.add_argument("--compatibility-artifact", type=Path, default=Path("exports/xm/phase_xm_gold_compatibility.json"))
    parser.add_argument("--output", type=Path, default=Path("exports/xm/phase_xm_gold_historical_validation_protocol.json"))
    parser.add_argument("--stage-b", action="store_true")
    parser.add_argument("--result-output", type=Path, default=Path("exports/xm/phase_xm_gold_historical_validation.json"))
    args = parser.parse_args()

    if args.stage_b:
        report = build_xm_gold_historical_validation_report(
            repo_root=args.repo_root.resolve(), xm_m1_source=args.xm_m1_source,
            compatibility_artifact=args.compatibility_artifact, protocol_path=args.output,
        )
        result_sha256 = write_xm_gold_historical_validation_report(report, args.result_output)
        print(f"result path: {args.result_output}")
        print(f"result SHA-256: {result_sha256}")
        print(f"closed trades: {report['population']['closed_trades']}")
        print(f"total R: {report['aggregate']['total_r']}")
        print(f"expectancy R: {report['aggregate']['expectancy_r']}")
        print(f"PF: {report['aggregate']['profit_factor']}")
        print(f"XM GOLD HISTORICAL VALIDATION QUANTITATIVE PROVISIONAL: {report['gate']['quantitative_provisional_decision']}")
        print(f"XM GOLD HISTORICAL VALIDATION FINAL: {report['gate']['final_decision']}")
        return 0

    repo_root = args.repo_root.resolve()
    verify_canonical_git_provenance(repo_root)
    verify_frozen_production_sources(repo_root)
    verify_xm_raw_source_identity(args.xm_m1_source)
    verify_compatibility_artifact_identity(args.compatibility_artifact)
    protocol = build_xm_gold_historical_validation_protocol()
    protocol_sha256 = write_xm_gold_historical_validation_protocol(protocol, args.output)
    metrics = protocol["metrics"]
    print(f"protocol path: {args.output}")
    print(f"protocol SHA-256: {protocol_sha256}")
    print(f"canonical starting SHA: {CANONICAL_STARTING_SHA}")
    print(f"raw XM SHA: {protocol['raw_source']['sha256']}")
    print(f"historical cutoff: {HISTORICAL_CUTOFF_UTC}")
    print(f"warm-up rule: {protocol['warmup']['policy_id']} ({protocol['warmup']['minimum_strategy_eligible_observed_bars_per_timeframe']} observed replay-eligible finalized bars per timeframe)")
    print(f"minimum trade count: {metrics['closed_trade_population_minimum']}")
    print(f"expectancy threshold: >= {metrics['aggregate']['expectancy_strong_gte']:+.2f}R")
    print(f"PF threshold: >= {metrics['aggregate']['profit_factor_strong_gte']:.2f}")
    print(f"quarter breadth threshold: >= {metrics['temporal_breadth']['positive_quarter_fraction_gte']:.0%} positive")
    print(f"leave-one-quarter-out threshold: >= {metrics['leave_one_quarter_out']['variant_pass_fraction_gte']:.0%} variants")
    print(f"single-trade dependence rule: {metrics['tail']['single_trade_dependence_failure']}")
    print(f"ex-best-quarter rule: {protocol['ex_best_period']['primary']}")
    print(f"0.10R friction rule: {protocol['friction']['primary_0_10r_requirement']}")
    print(f"no-tuning declaration: {protocol['no_tuning_declaration']}")
    print("XM GOLD HISTORICAL VALIDATION PROTOCOL: LOCKED")
    print("PRE-MARCH-2026 STRATEGY RESULTS ACCESSED BEFORE LOCK: NO")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
