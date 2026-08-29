"""Create the Stage A-only XAU directional edge attribution protocol lock."""
from __future__ import annotations

import argparse
from pathlib import Path

from quasartrend.research.xau_directional_edge_attribution import (
    RAW_SOURCE_PATH,
    build_context_lock,
    build_xau_directional_edge_attribution_protocol,
    verify_stage_a_identities,
    write_xau_directional_edge_attribution_protocol,
)


def main() -> int:
    parser = argparse.ArgumentParser(description="Stage A-only XAU directional attribution protocol lock")
    parser.add_argument("--repo-root", type=Path, default=Path("."))
    parser.add_argument("--xm-m1-source", type=Path, default=Path(RAW_SOURCE_PATH))
    parser.add_argument("--output", type=Path, default=Path("exports/xm/phase_xau_directional_edge_attribution_protocol.json"))
    args = parser.parse_args()
    root = args.repo_root.resolve()
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
