"""Create the Stage A-only XAU directional edge attribution protocol lock."""
from __future__ import annotations

import argparse
from pathlib import Path

from quasartrend.research.xau_directional_edge_attribution import (
    RAW_SOURCE_PATH,
    build_context_lock,
    build_xau_directional_edge_attribution_result,
    build_xau_directional_edge_attribution_protocol,
    verify_stage_a_identities,
    write_xau_directional_edge_attribution_protocol,
    write_xau_directional_edge_attribution_result,
)


def main() -> int:
    parser = argparse.ArgumentParser(description="XAU directional attribution: Stage A lock or guarded Stage B result")
    parser.add_argument("--repo-root", type=Path, default=Path("."))
    parser.add_argument("--xm-m1-source", type=Path, default=Path(RAW_SOURCE_PATH))
    parser.add_argument("--output", type=Path, default=Path("exports/xm/phase_xau_directional_edge_attribution_protocol.json"))
    parser.add_argument("--stage-b", action="store_true")
    parser.add_argument("--result-output", type=Path, default=Path("exports/xm/phase_xau_directional_edge_attribution.json"))
    args = parser.parse_args()
    root = args.repo_root.resolve()
    if args.stage_b:
        result = build_xau_directional_edge_attribution_result(repo_root=root, xm_m1_source=args.xm_m1_source, protocol_path=args.output)
        digest = write_xau_directional_edge_attribution_result(result, args.result_output, repo_root=root)
        baseline = result["directional_baseline"]
        print(f"result path: {args.result_output}")
        print(f"result SHA-256: {digest}")
        print(f"closed trades: {result['population_reproduction']['closed_trades']}")
        print(f"total R: {baseline['long']['total_r'] + baseline['short']['total_r']}")
        print(f"expectancy R: {(baseline['long']['total_r'] + baseline['short']['total_r']) / result['population_reproduction']['closed_trades']}")
        print(f"classification: {', '.join(result['classification']['labels'])}")
        for key, value in result["restriction_state"].items(): print(f"{key}: {value}")
        return 0
    verify_stage_a_identities(root)
    context_lock = build_context_lock(repo_root=root, xm_m1_source=args.xm_m1_source)
    protocol = build_xau_directional_edge_attribution_protocol(context_lock)
    digest = write_xau_directional_edge_attribution_protocol(protocol, args.output)
    print(f"protocol path: {args.output}")
    print(f"protocol SHA-256: {digest}")
    print("NEW ATTRIBUTION ECONOMICS INSPECTED BEFORE LOCK: NO")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
