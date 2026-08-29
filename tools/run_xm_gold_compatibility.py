"""Generate the additive XM GOLD / TradingView XAUUSD compatibility artifact."""
from __future__ import annotations

import argparse
from pathlib import Path

from quasartrend.research.xm_gold_compatibility import (
    build_xm_gold_compatibility_report,
    write_xm_gold_compatibility_report,
)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--xm-m1-source", type=Path, default=Path("exports/xm/XM_GOLD_M1_raw.csv"))
    parser.add_argument("--tradingview-15m-source", type=Path, default=Path("exports/xauusd_pending/XAUUSD_15m.csv"))
    parser.add_argument("--tradingview-4h-source", type=Path, default=Path("exports/xauusd_pending/XAUUSD_4h.csv"))
    parser.add_argument("--output", type=Path, default=Path("exports/xm/phase_xm_gold_compatibility.json"))
    parser.add_argument("--overwrite", action="store_true")
    args = parser.parse_args()
    report = build_xm_gold_compatibility_report(
        xm_m1_source=args.xm_m1_source,
        tradingview_15m_source=args.tradingview_15m_source,
        tradingview_4h_source=args.tradingview_4h_source,
    )
    write_xm_gold_compatibility_report(report, args.output, overwrite=args.overwrite)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
