"""Generate the deterministic XAUUSD robustness/OOS research artifact."""
from __future__ import annotations

import argparse
from pathlib import Path

from quasartrend.research.xau_robustness import ALLOWED_GATE_OUTCOMES, PENDING_SOL_REVIEW, build_xauusd_robustness_report, write_xauusd_robustness_report


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--source-15m", type=Path, default=Path("exports/xauusd_pending/XAUUSD_15m.csv"))
    parser.add_argument("--source-4h", type=Path, default=Path("exports/xauusd_pending/XAUUSD_4h.csv"))
    parser.add_argument("--declared-symbol", default="XAUUSD")
    parser.add_argument("--output", type=Path, default=Path("exports/xauusd/phase_xau_robustness_oos.json"))
    parser.add_argument("--overwrite", action="store_true")
    parser.add_argument("--gate-outcome", choices=ALLOWED_GATE_OUTCOMES, default=PENDING_SOL_REVIEW)
    args = parser.parse_args()
    report = build_xauusd_robustness_report(source_15m=args.source_15m, source_4h=args.source_4h, declared_symbol=args.declared_symbol, gate_outcome=args.gate_outcome)
    write_xauusd_robustness_report(report, args.output, overwrite=args.overwrite)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
