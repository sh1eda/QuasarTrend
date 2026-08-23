"""Leakage-resistant observational research primitives for Phase 7."""

from .adr import adr_context_for_date, adr_contexts, daily_ranges, utc_date, validate_canonical_15m_bars
from .dataset import build_research_dataset, calculate_excursions, source_open_utc_features
from .metrics import calculate_metrics, compare_candidate, evaluate_candidate_evidence
from .models import *  # noqa: F403
from .provenance import canonical_json, event_identity, fingerprint, make_manifest, make_source_artifact, setup_identity
from .splits import CANONICAL_WINDOWS, ChronologicalWindow, WindowTradePartition, partition_trades, split_trades, validate_windows, walk_forward_windows
from .source import PARSER_ID, parse_tradingview_export, validate_canonical_source_bars
from .pipeline import baseline_report, build_canonical_bundle, report_json, write_report
from .experiments import development_report, experiment_json, write_experiment_report
from .candidates import final_oos_report, register_development_candidate, register_finalist, reject_after_validation, validation_report

__all__ = [
    "adr_context_for_date", "adr_contexts", "daily_ranges", "utc_date", "validate_canonical_15m_bars",
    "build_research_dataset", "calculate_excursions", "source_open_utc_features", "calculate_metrics", "compare_candidate", "evaluate_candidate_evidence",
    "canonical_json", "event_identity", "fingerprint", "make_manifest", "make_source_artifact", "setup_identity",
    "CANONICAL_WINDOWS", "ChronologicalWindow", "WindowTradePartition", "partition_trades", "split_trades", "validate_windows", "walk_forward_windows",
    "PARSER_ID", "parse_tradingview_export", "validate_canonical_source_bars",
    "baseline_report", "build_canonical_bundle", "report_json", "write_report",
    "development_report", "experiment_json", "write_experiment_report",
    "final_oos_report", "register_development_candidate", "register_finalist", "reject_after_validation", "validation_report",
]
