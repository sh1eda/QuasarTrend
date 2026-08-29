"""Stage A lock for XM GOLD's predeclared historical-validation protocol.

This module intentionally contains no XM parsing, aggregation, indicator,
replay, strategy, trade, or economic execution.  It only constructs the
immutable protocol that a later Stage B implementation must bind to before it
is permitted to access historical strategy results.
"""
from __future__ import annotations

import json
import csv
from collections import defaultdict
from dataclasses import replace
from datetime import UTC, datetime
from hashlib import sha256
import math
from pathlib import Path
from statistics import mean, median
import subprocess
from typing import Any, Callable, Iterable, Mapping, Sequence, TypeVar

from quasartrend.backtest import BacktestConfig, BacktestEngine
from quasartrend.replay import ReplayConfig, ReplayEngine, ReplayResult, ReplayState, Timeframe
from quasartrend.strategy import Direction, EventType, StrategyConfig, StrategyState
from .provenance import canonical_json
from .xm_gold_compatibility import AggregatedBar, XM_RAW_SCHEMA, XmM1Bar, _parse_row, aggregate_m1


SCHEMA_VERSION = "xm-gold-historical-validation-protocol/v1"
CANONICAL_STARTING_SHA = "446f93cfbad601a7517caac54fb2f2791fc2e5fe"
CANONICAL_TAG = "xm-gold-compatibility-pass"
EXPECTED_XM_RAW_SHA256 = "3817558149502888bcee711fb803e6085c5d48cbf36d33901d85ac85c0c0d81a"
EXPECTED_XM_RAW_ROWS = 1_000_000
EXPECTED_COMPATIBILITY_ARTIFACT_SHA256 = "d9d23efb0a6343d83c06c0fb79d67d245528e4749e834f8f26b06c7bf3c09176"
RAW_SOURCE_PATH = "exports/xm/XM_GOLD_M1_raw.csv"
COMPATIBILITY_ARTIFACT_PATH = "exports/xm/phase_xm_gold_compatibility.json"
HISTORICAL_CUTOFF_UTC = "2026-03-01T23:00:00Z"
HISTORICAL_CUTOFF_MS = 1_772_406_000_000
M1_DURATION_MS = 60_000
M15_DURATION_MS = 900_000
H4_DURATION_MS = 14_400_000

# These are SHA-256 values of the canonical tagged source *contents*, not of
# the Git blob objects.  Stage B must refuse a changed production strategy even
# if its requested protocol hash otherwise looks valid.
FROZEN_PRODUCTION_SOURCE_SHA256 = {
    "src/quasartrend/backtest/__init__.py": "fe9a2d39b96d2ca527c8121fac3a082b4f72912e6a7e334fddca8e714936a5f0",
    "src/quasartrend/backtest/engine.py": "9a6ddea5538c459677f24bbc0579e8a43a6e65b73be4157009abb1a43ca0b69e",
    "src/quasartrend/backtest/metrics.py": "a760cbdb2714493543c1db64fa8c64e27e37be1dda4cba6003b7f213d3ec146a",
    "src/quasartrend/backtest/models.py": "7f3c59b336a25fb09f06801162a2296236b7d2a6b98174a7f0a2ad9afd9fcb48",
    "src/quasartrend/indicators/__init__.py": "6a7d92e3221c089b870166542f65f1dc5977a1a0e85aa23343a7e939524e49e6",
    "src/quasartrend/indicators/diagnostics.py": "2418bed4ce087aa7fabe0044c2aaca7b0f43d2f39b7fe6bce24be2517fc0501a",
    "src/quasartrend/indicators/golden.py": "61bb97f2a03c0d2ea923d51282080d93af4c228efad9525f7350603cddd4453c",
    "src/quasartrend/indicators/hema.py": "7b8320b267db578616ab5cfaee0776ebe75001a1a13c490e02688fdc35021ff7",
    "src/quasartrend/indicators/kalman.py": "394544481b317793a5692aeb4afb18516642fe2e025eb87b51acbc323ff92cd6",
    "src/quasartrend/indicators/models.py": "d17b4bf284da8748f060607c7be74a5095baa87073c5b28df03f6d2b3069e3c6",
    "src/quasartrend/indicators/moving_averages.py": "5c0cac63c288e0dc15b6963f221ea8a3c71b0f76a2ab44fbe2998a70467bbdf2",
    "src/quasartrend/indicators/pine.py": "a39d436726de8e1846b49558797081ec242a533d0cb307be40fed3d75f92daf7",
    "src/quasartrend/replay/__init__.py": "dd25089e1dbbb22eda4565b17224a08bca08087e610480d4c10c45a84ad1421c",
    "src/quasartrend/replay/engine.py": "a6d596f14a93614d037443c2d84f6e1517261caeb37abbddcce30f7d7d1cf86a",
    "src/quasartrend/replay/models.py": "2377ca0b7d3bb1f713d0426e7e41a7c344dfff1a9d928ba71e1c62b6d52dfc9e",
    "src/quasartrend/research/xm_gold_compatibility.py": "e8b3ca32f313320c6846d6ef79f79f217a1b2efe92a487b93bd2e5544fdc2c9c",
    "src/quasartrend/strategy/__init__.py": "287924ce3473dffd6d755e3684aa4aac3fe229735701a7ef73e5105d5e7d5326",
    "src/quasartrend/strategy/engine.py": "719b4f89423b06ee161bbbfd5b207a499ebc80f9c7e4a08d303be7dd5e896f62",
    "src/quasartrend/strategy/models.py": "bb41775c4c60b1661c45e521e494e7d59d70fc06607cd3c69dfd999153998904",
}
FROZEN_PINESCRIPT_SOURCE_SHA256 = {
    "references/pinescript/hema_trend.pine": "298d26ee6114b2f5f05b7031f2c3154a5a5fd92d669c7c932684a87af50e58a4",
    "references/pinescript/kalman_step_signals.pine": "794c4f4ab7311c120fea64ccfada57a6d5135cf1a205b1872b6f9360175f36e4",
    "references/pinescript/parity_export.pine": "9499a74a2590fb3c9117d683ba694bbf03e084db2e07d9e15497260e7bf7291e",
}
FROZEN_PRODUCTION_SOURCE_MANIFEST_SHA256 = "a6b02c8056c9996eb3bcac64a18588251f9a7c741f6c208eacbdc82de15f3e6d"
PREDECLARED_FRICTION_COSTS_R = (0.00, 0.02, 0.05, 0.10, 0.15, 0.20, 0.25, 0.30)
# Centralized pre-lock warm-up constants.  They must not be inferred from,
# or subsequently selected by, historical outcomes.
MINIMUM_INDICATOR_VALID_OBSERVED_BARS = 45
MINIMUM_STRATEGY_ELIGIBLE_OBSERVED_BARS = 600
# This constant is deliberately literal after protocol finalization.  It is
# not stored inside the mutable JSON protocol, avoiding self-referential hash
# semantics and requiring code review for any future change.
EXPECTED_PROTOCOL_SHA256 = "2c690292b3f2a53c0295cb153cf0721044ba3d24c55b56359e32f030c7ee7870"

_T = TypeVar("_T")


def build_xm_gold_historical_validation_protocol() -> dict[str, Any]:
    """Return the complete result-independent Stage A protocol.

    No field is derived from market observations.  In particular, the warm-up
    count follows the existing default runtime bootstrap and measured H4
    cold-start convergence acceptance row with a fixed margin; it is not
    selected from economic outcomes.
    """
    return {
        "schema_version": SCHEMA_VERSION,
        "decision_authority": "Sol/main only; this protocol does not approve or advance a phase",
        "canonical_starting_state": {
            "sha": CANONICAL_STARTING_SHA,
            "annotated_tag": CANONICAL_TAG,
            "expected_tag_target": CANONICAL_STARTING_SHA,
        },
        "strategy_freeze_identity": {
            "identity": "exact frozen QuasarTrend strategy at canonical starting SHA",
            "production_source_sha256": dict(FROZEN_PRODUCTION_SOURCE_SHA256),
            "pinescript_source_sha256": dict(FROZEN_PINESCRIPT_SOURCE_SHA256),
            "production_source_manifest_sha256": FROZEN_PRODUCTION_SOURCE_MANIFEST_SHA256,
            "configuration": {
                "ltf_hema": {"fast_length": 20, "slow_length": 40, "timeframe": "15m"},
                "htf_hema": {"fast_length": 20, "slow_length": 40, "timeframe": "4h"},
                "kalman": {"period": 21, "alpha": 0.01, "beta": 0.1, "factor": 1.0, "atr_period": 7},
                "strategy": {"atr_multiplier": 1.0, "confirmation_mode": "stateful_either_order", "bias_reversal_behavior": "exit"},
            },
            "unchanged_behavior": [
                "15m logic and 4h bias logic", "setup definitions and immediate/armed behavior",
                "cancellation, entry, stop, exit, and R accounting semantics",
                "replay chronology, H4 priority, closed-candle semantics, and indicator initialization",
            ],
        },
        "raw_source": {
            "provider": "XM", "server": "XMGlobal-MT5 18", "instrument": "GOLD",
            "mt5_path": r"Derivatives\Spot Metals\GOLD", "digits": 2, "point": 0.01,
            "path": RAW_SOURCE_PATH, "sha256": EXPECTED_XM_RAW_SHA256, "expected_rows": EXPECTED_XM_RAW_ROWS,
            "raw_labeled_range": {"first": "2023-10-31T00:09:00Z", "last": "2026-08-28T23:57:00Z"},
            "normalized_range_approximate": {"first": "2023-10-30T22:09:00Z", "last": "2026-08-28T20:57:00Z"},
        },
        "compatibility_freeze": {
            "artifact_path": COMPATIBILITY_ARTIFACT_PATH,
            "artifact_sha256": EXPECTED_COMPATIBILITY_ARTIFACT_SHA256,
            "status": "PASS; reference-only and excluded from primary historical economics",
        },
        "timezone": {
            "rule": "XM server wall-clock -> Europe/Nicosia -> UTC",
            "server_timezone": "Europe/Nicosia", "manual_shift": "prohibited",
            "dst_ambiguity_or_nonexistence": "fail_closed",
        },
        "aggregation": {
            "source_timeframe": "1m", "target_timeframes": ["15m", "4h"],
            "boundary": "native XM server-clock boundary before timezone normalization",
            "gap_policy": "no gap filling and no synthetic source or aggregate bars",
            "m15_incomplete_bucket_policy": "retain each observed non-empty native bucket unchanged; report incompleteness",
            "h4_incomplete_bucket_policy": "retain each observed non-empty native bucket only when its finalized timestamp is representable by frozen fixed-duration replay; report exclusions",
            "replay_order": "strict finalized chronology; coincident H4 update has priority before 15m decision",
            "closed_candle_only": True,
        },
        "warmup": {
            "policy_id": "conservative-45-then-600-observed-bars-v1",
            "derivation": "mathematical indicator validity after 45 observed replay-eligible bars per timeframe; strategy/trade gate uses existing default runtime bootstrap of 600 bars per timeframe and exceeds measured H4 cold-start convergence acceptance start row 507 with margin",
            "minimum_indicator_valid_observed_bars_per_timeframe": MINIMUM_INDICATOR_VALID_OBSERVED_BARS,
            "minimum_strategy_eligible_observed_bars_per_timeframe": MINIMUM_STRATEGY_ELIGIBLE_OBSERVED_BARS,
            "indicator_validity_requirement": "after the 45-bar count, HTF bias, LTF HEMA relation, Kalman state, and ATR must be available and finite under frozen semantics",
            "strategy_state_during_warmup": "suppressed/reset; no strategy setup, pending state, open trade, or economic result may carry from warm-up into eligibility",
            "strategy_eligibility": "first M15 decision at or after both timeframes have 600 observed replay-eligible finalized bars and all required indicator state is valid; H4 priority is preserved; no setup or trade may originate before it",
            "trade_eligibility": "setup origin must be at or after strategy eligibility; entries opened from earlier setup origins are excluded",
            "required_stage_b_reporting": ["raw_first_timestamp", "first_m15_bar", "first_h4_bar", "first_indicator_valid_timestamp", "first_strategy_eligible_timestamp", "first_trade_eligible_timestamp"],
        },
        "historical_validation_boundary": {
            "start": "earliest strategy-eligible timestamp after the deterministic warm-up",
            "end_exclusive_utc": HISTORICAL_CUTOFF_UTC,
            "end_exclusive_epoch_ms": HISTORICAL_CUTOFF_MS,
            "compatibility_period_in_primary_metrics": False,
            "censored_trade_policy": "trades not closed strictly before cutoff are censored and excluded from closed-trade economic metrics",
        },
        "metrics": {
            "closed_trade_population_minimum": 250,
            "aggregate": {"total_r_required_gt": 0.0, "expectancy_required_gt": 0.0, "expectancy_strong_gte": 0.10, "profit_factor_required_gt": 1.0, "profit_factor_strong_gte": 1.15},
            "temporal_breadth": {"buckets": ["month", "calendar_quarter", "calendar_year_or_partial_year", "chronological_halves", "rolling_trade_windows"], "positive_quarter_fraction_gte": 0.50, "single_quarter_positive_net_r_concentration_warning_gt": 0.70},
            "leave_one_quarter_out": {"minimum_evaluable_quarters": 4, "variant_pass_fraction_gte": 0.75, "required": {"total_r_gt": 0.0, "expectancy_gt": 0.0, "profit_factor_gt": 1.0}},
            "leave_one_year_or_partial_year_out": {"when": "at least two evaluable calendar-year/partial-year periods", "otherwise": "report insufficient; no threshold substitution", "recompute": ["closed_trades", "total_r", "expectancy_r", "profit_factor"]},
            "chronological_stability": {"trade_partitions": 4, "edge_decay_warning": "final_50_percent_expectancy_lte_0 OR final_50_percent_pf_lte_1"},
            "tail": {"thresholds_r": [2.0, 3.0, 5.0, 10.0], "sequential_removals": [1, 3, 5, 10], "extreme_tail_warning": "remove_top_5_total_r_lte_0", "single_trade_dependence_failure": "remove_top_1_total_r_lte_0"},
            "path_risk": {"rolling_closed_trade_windows": [20, 50], "annualization": "prohibited", "bootstrap": "not_predeclared"},
        },
        "metric_definitions": {
            "closed_trade": "opened frozen-strategy trade with a recorded canonical exit finalized strictly before the historical cutoff",
            "censored_trade": "opened trade not closed strictly before cutoff; report separately and exclude from all closed-trade economics",
            "r": "closed_trade.net_pnl / (abs(entry_price - stop_price) * quantity), using frozen accounting",
            "total_r": "sum of closed-trade R after the stated synthetic cost, if any",
            "expectancy_r": "total R divided by closed-trade count; unavailable when count is zero",
            "profit_factor": "sum of strictly positive closed-trade R divided by absolute sum of strictly negative closed-trade R",
            "profit_factor_no_loss_convention": "serialize unavailable/null when there are no strictly negative closed-trade R values; for gate comparison only, a positive-return no-loss population is treated as infinite PF and passes PF thresholds",
            "win_rate": "count of strictly positive closed-trade R divided by closed-trade count; zero-R trades are not wins",
            "stop_rate": "count of closed trades whose frozen exit reasons include exit_stop divided by closed-trade count",
            "positive_r": "sum of strictly positive closed-trade R",
            "negative_r_magnitude": "absolute sum of strictly negative closed-trade R",
            "median_r": "ordinary median of closed-trade R; unavailable when count is zero",
            "r_per_setup": "total closed-trade R divided by eligible setup count; censored and unopened eligible setups remain in denominator",
        },
        "attribution_and_ordering": {
            "calendar_month_quarter_year_attribution": "UTC trade entry-finalization timestamp",
            "closed_trade_chronological_order": "exit timestamp ascending, then trade_id ascending",
            "evaluable_calendar_period": "any calendar period with at least one closed trade, including partial boundary periods",
            "best_period_tie_break": "earliest chronological period label",
            "top_winner_tie_break": "R descending, then exit timestamp ascending, then trade_id ascending",
            "chronological_quartile_allocation": "contiguous closed-trade order; first remainder buckets receive one additional trade",
        },
        "decompositions": {
            "direction": {
                "segments": ["long", "short"],
                "report": ["eligible_setups", "opened_trades", "closed_trades", "total_r", "expectancy_r", "profit_factor", "win_rate", "stop_rate", "2r_plus", "3r_plus", "5r_plus"],
                "classification": ["both_positive", "long_only_edge", "short_only_edge", "neither"],
                "rule": "both directions are descriptive; no directional filter may be introduced",
            },
            "setup_path": {
                "segments": ["immediate_open", "armed_then_opened"],
                "report": ["setups", "trades", "total_r", "expectancy_r", "profit_factor", "stop_rate", "2r_plus", "3r_plus", "5r_plus"],
                "rule": "materially negative path is reported; no path may be removed or modified",
            },
            "temporal": {
                "calendar": ["month", "calendar_quarter", "calendar_year_or_partial_year"],
                "trade_order_partitions": ["chronological_quartiles", "chronological_halves"],
                "each_partition_report": ["closed_trades", "total_r", "expectancy_r", "profit_factor", "win_rate", "stop_rate"],
                "rolling_windows": [20, 50],
                "quarter_concentration_denominator": "sum of positive total R across evaluable calendar quarters; unavailable if denominator is zero",
            },
        },
        "tail_dependence": {
            "winner_population": "strictly positive closed-trade R sorted with top_winner_tie_break",
            "report": ["2r_plus", "3r_plus", "5r_plus", "10r_plus", "maximum_winner_r", "top_1", "top_3", "top_5", "top_10"],
            "top_n_percentages": ["percentage_of_positive_r", "percentage_of_net_r"],
            "removal": "sequentially remove top 1, 3, 5, and 10 winners and recompute total R, expectancy, and PF",
        },
        "stop_giveback": {
            "population": "closed stopped trades only",
            "full_exit_bar_mfe": "post-entry 15m bars including the exit bar; maximum favorable excursion in R using observed contiguous path",
            "strict_pre_exit_mfe": "post-entry 15m bars strictly before the exit bar; conservative diagnostic only, not asserted intrabar path",
            "report": ["count", "total_stop_r", "mean_mfe_r", "median_mfe_r", "distribution_buckets", "missing_path_count", "full_vs_strict_difference"],
            "distribution_buckets_r": ["<0.25", "0.25_to_<0.5", "0.5_to_<1", "1_to_<2", "2_to_<3", "3_to_<5", "5_plus"],
            "missing_path_policy": "a post-entry path crossing a source gap has null MFE under both conventions; report missing count and compute summaries from observed values only",
        },
        "path_risk_conventions": {
            "cumulative_curve": "closed trades in closed_trade_chronological_order, initial cumulative R = 0",
            "drawdown": "peak-to-trough cumulative-R drawdown; strictly larger drawdown replaces prior, so equal drawdowns retain earliest peak/trough",
            "recovery": "first subsequent closed trade whose cumulative R is at least the selected drawdown peak",
            "streaks": "strictly positive R consecutive winning streak and strictly negative R consecutive losing streak; zero R breaks both",
            "rolling": "overlapping chronological closed-trade windows; equal best/worst totals retain earliest window",
        },
        "friction": {
            "abstraction": "synthetic effective round-trip R cost applied once per closed trade using the frozen XAU robustness convention",
            "real_broker_cost_conversion": "NOT ESTABLISHED",
            "cost_levels_r": list(PREDECLARED_FRICTION_COSTS_R),
            "breakeven": "exact total_R / closed_trades under this abstraction",
            "retained_percentage": "100 * stressed total R / frictionless total R; unavailable when frictionless total R is zero",
            "segment_breakevens": ["long", "short", "immediate_open", "armed_then_opened"],
            "primary_0_10r_requirement": {"total_r_gt": 0.0, "expectancy_gt": 0.0, "profit_factor_gt": 1.0},
        },
        "ex_best_period": {
            "primary": "remove best calendar quarter by total R; require frictionless total R > 0, expectancy > 0, PF > 1",
            "friction_costs_r": [0.05, 0.10, 0.15, 0.20], "secondary": "remove best calendar month and report frictionless diagnostic only",
        },
        "compatibility_period_reference": {
            "period": "2026-03-01T23:00:00Z through the disclosed frozen XM compatibility sample",
            "known": {"closed_trades": 192, "total_r_approx": 70.597728, "expectancy_r_approx": 0.3677, "profit_factor_approx": 1.5691},
            "purpose": "descriptive directionally-consistent/weaker/similar/stronger/structurally-different comparison only",
            "primary_population_merge": "prohibited",
        },
        "full_pass_gate": [
            "at_least_250_closed_historical_trades", "aggregate_total_r_gt_0", "aggregate_expectancy_gte_0_10r", "aggregate_pf_gte_1_15",
            "at_least_50_percent_evaluable_quarters_positive", "at_least_75_percent_loo_quarters_positive_total_r_expectancy_pf",
            "removing_best_trade_does_not_make_total_r_lte_0", "ex_best_quarter_frictionless_total_r_expectancy_pf_positive",
            "at_0_10r_cost_total_r_expectancy_pf_positive", "no_independently_identified_blocker_or_high_methodological_defect", "no_tuning_or_result_selected_method_change",
        ],
        "classification": {
            "pass": "all full_pass_gate requirements", "conditional": "aggregate edge positive but one or more robustness requirements fail without invalidating strategy",
            "inconclusive": "methodological/data limitation including fewer than 250 closed trades",
            "fail": ["aggregate_expectancy_lte_0", "aggregate_pf_lte_1", "aggregate_total_r_lte_0", "single_best_trade_removal_total_r_lte_0", "severe_methodological_invalidity", "leakage", "result_selected_protocol_modification", "frozen_strategy_behavior_changed"],
            "precedence": ["FAIL for any stated FAIL condition, including aggregate expectancy <= 0, PF <= 1, total R <= 0, or single-best-trade-removal total R <= 0; do not downgrade a negative result", "INCONCLUSIVE for fewer than 250 closed trades only when no preceding FAIL condition applies", "PASS only if every full-pass gate passes", "CONDITIONAL for remaining positive aggregate edge with robustness weakness"],
        },
        "no_tuning_declaration": "No XAU-specific tuning, provider-specific parameters, alternative stops/exits, filtering, optimization, parameter sweeps, cherry-picked dates, or result-selected methodological changes are authorized.",
        "protocol_lock": {
            "immutable_after_creation": True,
            "stage_b_requires_exact_protocol_sha256": True,
            "pre_lock_historical_indicator_setup_trade_or_economic_evaluation": "prohibited",
        },
    }


def xm_gold_historical_validation_protocol_json(protocol: Mapping[str, Any]) -> bytes:
    """Serialize a protocol canonically, including exactly one final newline."""
    return (canonical_json(dict(protocol)) + "\n").encode("utf-8")


def protocol_sha256(protocol: Mapping[str, Any]) -> str:
    return sha256(xm_gold_historical_validation_protocol_json(protocol)).hexdigest()


def expected_protocol_sha256() -> str:
    actual = protocol_sha256(build_xm_gold_historical_validation_protocol())
    if actual != EXPECTED_PROTOCOL_SHA256:
        raise ValueError("canonical protocol bytes do not match the pinned Stage A SHA-256")
    return EXPECTED_PROTOCOL_SHA256


def write_xm_gold_historical_validation_protocol(protocol: Mapping[str, Any], path: Path) -> str:
    """Write the one permitted protocol bytes once; never overwrite a lock."""
    expected = build_xm_gold_historical_validation_protocol()
    payload = xm_gold_historical_validation_protocol_json(protocol)
    if payload != xm_gold_historical_validation_protocol_json(expected):
        raise ValueError("refusing to lock a protocol other than the predeclared canonical protocol")
    if sha256(payload).hexdigest() != expected_protocol_sha256():
        raise ValueError("refusing to lock bytes that do not match the pinned Stage A SHA-256")
    if path.exists():
        raise FileExistsError(f"refusing to overwrite immutable protocol lock: {path}")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(payload)
    return sha256(payload).hexdigest()


def verify_stage_b_protocol_lock(path: Path, expected_sha256: str) -> dict[str, Any]:
    """Fail closed unless ``path`` is precisely the locked Stage A protocol."""
    payload = path.read_bytes()
    actual_sha256 = sha256(payload).hexdigest()
    canonical = xm_gold_historical_validation_protocol_json(build_xm_gold_historical_validation_protocol())
    if expected_sha256 != expected_protocol_sha256():
        raise ValueError("unknown protocol hash; Stage B requires the exact Stage A lock hash")
    if actual_sha256 != expected_sha256:
        raise ValueError("protocol hash mismatch; Stage B is not authorized")
    if payload != canonical:
        raise ValueError("protocol bytes differ from the immutable Stage A protocol")
    try:
        parsed = json.loads(payload)
    except json.JSONDecodeError as error:
        raise ValueError("locked protocol is not valid JSON") from error
    if parsed != build_xm_gold_historical_validation_protocol():
        raise ValueError("protocol semantic content differs from the immutable Stage A protocol")
    return parsed


def run_stage_b_guarded(
    path: Path, expected_sha256: str, evaluator: Callable[[Mapping[str, Any]], _T], *,
    repo_root: Path, xm_m1_source: Path, compatibility_artifact: Path,
) -> _T:
    """Authorize Stage B only after exact protocol and all frozen identities verify."""
    protocol = verify_stage_b_protocol_lock(path, expected_sha256)
    verify_canonical_git_provenance(repo_root)
    verify_frozen_production_sources(repo_root)
    verify_xm_raw_source_identity(xm_m1_source)
    verify_compatibility_artifact_identity(compatibility_artifact)
    return evaluator(protocol)


def assert_historical_timestamp(timestamp_ms: int) -> None:
    """Reject any Stage B setup, trade, or result timestamp at/after cutoff."""
    if isinstance(timestamp_ms, bool) or not isinstance(timestamp_ms, int):
        raise TypeError("historical timestamp must be an integer epoch milliseconds")
    if timestamp_ms >= HISTORICAL_CUTOFF_MS:
        raise ValueError("compatibility-period timestamp is excluded from primary historical validation")


def warmup_allows_strategy_event(
    *, timestamp_ms: int, observed_h4_bars: int, observed_ltf_bars: int,
    h4_indicator_state_valid: bool, ltf_indicator_state_valid: bool,
) -> bool:
    """Pure eligibility gate; it does not calculate an indicator or strategy state."""
    assert_historical_timestamp(timestamp_ms)
    warmup = build_xm_gold_historical_validation_protocol()["warmup"]
    indicator_minimum = warmup["minimum_indicator_valid_observed_bars_per_timeframe"]
    strategy_minimum = warmup["minimum_strategy_eligible_observed_bars_per_timeframe"]
    return (
        observed_h4_bars >= indicator_minimum
        and observed_ltf_bars >= indicator_minimum
        and h4_indicator_state_valid
        and ltf_indicator_state_valid
        and observed_h4_bars >= strategy_minimum
        and observed_ltf_bars >= strategy_minimum
    )


def verify_xm_raw_source_identity(path: Path) -> dict[str, int | str]:
    """Hash/count raw bytes only; deliberately no CSV parsing or strategy work."""
    digest = sha256()
    rows = -1
    with path.open("rb") as handle:
        for line in handle:
            digest.update(line)
            rows += 1
    if rows < 1:
        raise ValueError("XM raw source must contain a header and at least one row")
    if digest.hexdigest() != EXPECTED_XM_RAW_SHA256 or rows != EXPECTED_XM_RAW_ROWS:
        raise ValueError("XM raw source identity mismatch")
    return {"sha256": digest.hexdigest(), "row_count": rows}


def verify_compatibility_artifact_identity(path: Path) -> str:
    digest = sha256(path.read_bytes()).hexdigest()
    if digest != EXPECTED_COMPATIBILITY_ARTIFACT_SHA256:
        raise ValueError("frozen XM compatibility artifact identity mismatch")
    return digest


def verify_frozen_production_sources(repo_root: Path) -> dict[str, str]:
    actual: dict[str, str] = {}
    for relative, expected in FROZEN_PRODUCTION_SOURCE_SHA256.items():
        digest = sha256((repo_root / relative).read_bytes()).hexdigest()
        if digest != expected:
            raise ValueError(f"frozen production source identity mismatch: {relative}")
        actual[relative] = digest
    for relative, expected in FROZEN_PINESCRIPT_SOURCE_SHA256.items():
        digest = sha256((repo_root / relative).read_bytes()).hexdigest()
        if digest != expected:
            raise ValueError(f"frozen PineScript source identity mismatch: {relative}")
        actual[relative] = digest
    manifest_sha256 = sha256(canonical_json(actual).encode("utf-8")).hexdigest()
    if manifest_sha256 != FROZEN_PRODUCTION_SOURCE_MANIFEST_SHA256:
        raise ValueError("frozen production source manifest identity mismatch")
    return actual


def _git(repo_root: Path, *args: str) -> str:
    completed = subprocess.run(["git", *args], cwd=repo_root, check=True, capture_output=True, text=True)
    return completed.stdout.strip()


def verify_canonical_git_provenance(repo_root: Path) -> dict[str, str | bool]:
    """Verify the declared canonical starting point without fetching or mutating Git."""
    try:
        head = _git(repo_root, "rev-parse", "HEAD")
        branch = _git(repo_root, "branch", "--show-current")
        local_main = _git(repo_root, "rev-parse", "main")
        origin_main = _git(repo_root, "rev-parse", "origin/main")
        tag_type = _git(repo_root, "cat-file", "-t", CANONICAL_TAG)
        tag_target = _git(repo_root, "rev-parse", f"{CANONICAL_TAG}^{{}}")
        index_clean = subprocess.run(["git", "diff", "--cached", "--quiet"], cwd=repo_root).returncode == 0
        worktree_clean = subprocess.run(["git", "diff", "--quiet"], cwd=repo_root).returncode == 0
    except subprocess.CalledProcessError as error:
        raise ValueError("canonical Git provenance cannot be verified") from error
    if (
        head != CANONICAL_STARTING_SHA or branch != "main" or local_main != CANONICAL_STARTING_SHA
        or origin_main != CANONICAL_STARTING_SHA or tag_type != "tag" or tag_target != CANONICAL_STARTING_SHA
        or not index_clean or not worktree_clean
    ):
        raise ValueError("canonical Git provenance mismatch")
    return {
        "head": head, "branch": branch, "local_main": local_main, "origin_main": origin_main,
        "tag_type": tag_type, "tag_target": tag_target, "tracked_index_clean": index_clean,
        "tracked_worktree_clean": worktree_clean,
    }


def _load_full_xm_m1(path: Path) -> tuple[XmM1Bar, ...]:
    """Parse all raw XM rows only after the Stage B guard has authorized it."""
    result: list[XmM1Bar] = []
    with path.open("r", encoding="utf-8-sig", newline="") as handle:
        reader = csv.reader(handle)
        try:
            header = tuple(next(reader))
        except StopIteration as error:
            raise ValueError("XM CSV is empty") from error
        if header != XM_RAW_SCHEMA:
            raise ValueError("XM CSV schema must exactly match the acquired MT5 export")
        previous_raw: int | None = None
        for number, row in enumerate(reader, 2):
            item = _parse_row(row, number)
            raw_open = item.raw_open_time if item.raw_open_time is not None else item.open_time
            if previous_raw is not None and raw_open <= previous_raw:
                raise ValueError("XM timestamps must be strictly increasing and unique")
            previous_raw = raw_open
            result.append(item)
    return tuple(result)


def _replay_inputs(rows: Sequence[XmM1Bar]) -> tuple[tuple[Any, ...], tuple[Any, ...], dict[str, int], dict[str, Any]]:
    """Use frozen server-clock aggregation without filling gaps or synthetic bars."""
    selected = tuple(row for row in rows if row.open_time < HISTORICAL_CUTOFF_MS)
    m15 = aggregate_m1(selected, timeframe=Timeframe.MINUTES_15)
    h4 = aggregate_m1(selected, timeframe=Timeframe.HOURS_4)
    ltf = tuple(item.bar for item in m15 if item.bar.finalized_at < HISTORICAL_CUTOFF_MS)
    htf: list[Any] = []
    excluded = defaultdict(int)
    for item in h4:
        if item.bar.finalized_at >= HISTORICAL_CUTOFF_MS:
            excluded["finalized_at_or_after_cutoff"] += 1
        elif item.actual_finalized_at != item.bar.finalized_at:
            excluded["dst_variable_duration_not_representable"] += 1
        else:
            htf.append(item.bar)
    if not ltf or not htf:
        raise ValueError("historical validation requires replay-eligible M15 and H4 bars")
    merged = tuple(sorted((*ltf, *htf), key=lambda bar: bar.processing_key))
    if any(right.processing_key <= left.processing_key for left, right in zip(merged, merged[1:])):
        raise ValueError("replay input must have strict finalized chronology")
    audit = {"pre_cutoff_m1_selected": len(selected), "m15_generated": len(m15), "m15_source_count_sum": sum(item.source_count for item in m15), "m15_incomplete": sum(not item.complete for item in m15), "m15_replay_included": len(ltf), "h4_generated": len(h4), "h4_source_count_sum": sum(item.source_count for item in h4), "h4_incomplete": sum(not item.complete for item in h4), "h4_replay_included": len(htf), "m15_source_count_reconciles_pre_cutoff_selection": sum(item.source_count for item in m15) == len(selected), "no_gap_fill": True, "synthetic_bars": 0}
    return ltf, tuple(htf), dict(sorted(excluded.items())), audit


def _strategy_bar_valid(trace: Any) -> bool:
    bar = trace.strategy_bar
    return bool(
        bar is not None and bar.htf_bias is not None and bar.hema_direction is not None
        and bar.kalman_direction is not None and bar.atr is not None and math.isfinite(bar.atr)
    )


def _run_warmed_replay(ltf: Sequence[Any], htf: Sequence[Any]) -> tuple[ReplayResult, dict[str, int | None]]:
    """Feed frozen indicators through warm-up while resetting strategy state.

    The reset is applied immediately before the first eligible M15 decision.
    Thus no warm-up setup, pending state, or open trade can affect the scored
    population while all recursive indicator checkpoints remain literal.
    """
    merged = tuple(sorted((*ltf, *htf), key=lambda bar: bar.processing_key))
    engine = ReplayEngine(ReplayConfig(), StrategyConfig())
    state: ReplayState | None = None
    traces: list[Any] = []
    ltf_count = htf_count = 0
    eligible_at: int | None = None
    first_indicator_valid: int | None = None
    for bar in merged:
        if state is None:
            state = engine.initial_state(bar.symbol)
        candidate_m15 = bar.timeframe is Timeframe.MINUTES_15 and htf_count >= MINIMUM_STRATEGY_ELIGIBLE_OBSERVED_BARS and ltf_count + 1 >= MINIMUM_STRATEGY_ELIGIBLE_OBSERVED_BARS
        if candidate_m15 and eligible_at is None:
            state = replace(state, strategy_state=StrategyState.initial(state.symbol))
        stepped = engine.step(state, bar)
        state = stepped.state
        if bar.timeframe is Timeframe.HOURS_4:
            htf_count += 1
        else:
            ltf_count += 1
            valid = _strategy_bar_valid(stepped.trace)
            if valid and first_indicator_valid is None:
                first_indicator_valid = bar.finalized_at
            if eligible_at is None:
                if candidate_m15 and valid:
                    eligible_at = bar.finalized_at
                    traces.append(stepped.trace)
                else:
                    # Suppress every warm-up strategy transition while retaining
                    # all indicator/HTF checkpoints that this candle updated.
                    state = replace(state, strategy_state=StrategyState.initial(state.symbol))
            else:
                traces.append(stepped.trace)
        if bar.timeframe is Timeframe.HOURS_4 and eligible_at is None:
            state = replace(state, strategy_state=StrategyState.initial(state.symbol))
    if state is None or eligible_at is None:
        raise ValueError("no strategy-eligible M15 decision before historical cutoff")
    return ReplayResult(state, tuple(traces)), {
        "first_indicator_valid_timestamp": first_indicator_valid,
        "first_strategy_eligible_timestamp": eligible_at,
        "first_trade_eligible_timestamp": eligible_at,
        "observed_ltf_bars": ltf_count, "observed_h4_bars": htf_count,
    }


def _closed(rows: Iterable[Mapping[str, Any]]) -> list[Mapping[str, Any]]:
    return [row for row in rows if row["outcome"] == "closed"]


def _economics(rows: Iterable[Mapping[str, Any]], *, cost_r: float = 0.0, eligible_setups: int | None = None) -> dict[str, Any]:
    items = _closed(rows)
    values = [float(row["r"]) - cost_r for row in items]
    wins, losses = [value for value in values if value > 0], [value for value in values if value < 0]
    return {
        "closed_trades": len(items), "total_r": float(sum(values)),
        "expectancy_r": None if not values else float(sum(values) / len(values)),
        "profit_factor": None if not losses else float(sum(wins) / abs(sum(losses))),
        "profit_factor_no_loss_positive_return": bool(values and not losses and sum(values) > 0),
        "win_rate": None if not values else sum(value > 0 for value in values) / len(values),
        "stop_rate": None if not items else sum(bool(row["stop_hit"]) for row in items) / len(items),
        "positive_r": float(sum(wins)), "negative_r_magnitude": float(abs(sum(losses))),
        "median_r": None if not values else float(median(values)),
        "r_per_setup": None if not eligible_setups else float(sum(values) / eligible_setups),
    }


def _pf_gt(economics: Mapping[str, Any], threshold: float) -> bool:
    """Apply the locked no-loss PF gate convention without serializing infinity."""
    return bool(economics["profit_factor_no_loss_positive_return"]) or (
        economics["profit_factor"] is not None and float(economics["profit_factor"]) > threshold
    )


def _pf_gte(economics: Mapping[str, Any], threshold: float) -> bool:
    return bool(economics["profit_factor_no_loss_positive_return"]) or (
        economics["profit_factor"] is not None and float(economics["profit_factor"]) >= threshold
    )


def _tail_counts(rows: Iterable[Mapping[str, Any]]) -> dict[str, int]:
    values = [float(row["r"]) for row in _closed(rows)]
    return {"2r_plus": sum(value >= 2 for value in values), "3r_plus": sum(value >= 3 for value in values), "5r_plus": sum(value >= 5 for value in values)}


def _ledger(replay: ReplayResult) -> tuple[list[dict[str, Any]], list[dict[str, Any]], int]:
    """Extract frozen replay/backtest events and a deterministic trade ledger."""
    backtest = BacktestEngine(BacktestConfig()).run(replay)
    setups: dict[int, dict[str, Any]] = {}
    pending: dict[Direction, int] = {}
    entries: dict[str, dict[str, Any]] = {}
    closes: dict[str, Any] = {}
    ltf_traces = [trace for trace in replay.traces if trace.source_bar.timeframe is Timeframe.MINUTES_15]
    for index, trace in enumerate(ltf_traces):
        for event in trace.events:
            if event.type is EventType.HEMA_FLIP_DETECTED and event.side is not None:
                setups[event.timestamp] = {"timestamp": event.timestamp, "direction": event.side.value, "path": "rejected"}
            elif event.type is EventType.SETUP_ARMED and event.side is not None:
                origin = trace.post_state.pending_flip_timestamp
                if origin is None or origin not in setups:
                    raise ValueError("armed setup lacks a known HEMA origin")
                setups[origin]["path"] = "armed_pending"; pending[event.side] = origin
            elif event.type is EventType.SETUP_CANCELLED and event.side is not None:
                origin = pending.pop(event.side, None)
                if origin is not None:
                    setups[origin]["path"] = "armed_then_cancelled"
            elif event.type is EventType.TRADE_OPENED:
                trade = trace.post_state.trade
                if trade is None or event.trade_id != trade.trade_id:
                    raise ValueError("trade open event/state mismatch")
                path = "armed_then_opened" if setups.get(trade.setup_origin_timestamp, {}).get("path") == "armed_pending" else "immediate_open"
                setups.setdefault(trade.setup_origin_timestamp, {"timestamp": trade.setup_origin_timestamp, "direction": trade.side.value})["path"] = path
                entries[trade.trade_id] = {"trade": trade, "entry_timestamp": event.timestamp, "path": path, "index": index}
            elif event.type is EventType.TRADE_CLOSED:
                if event.trade_id in closes:
                    raise ValueError("duplicate trade close")
                closes[event.trade_id] = (index, event)
    closed_by_id = {trade.trade_id: trade for trade in backtest.closed_trades}
    if set(closes) != set(closed_by_id):
        raise ValueError("replay/backtest closed trade identity mismatch")
    rows: list[dict[str, Any]] = []
    for trade_id, info in entries.items():
        trade = info["trade"]
        row: dict[str, Any] = {
            "trade_id": trade_id, "setup_origin_timestamp": trade.setup_origin_timestamp,
            "direction": trade.side.value, "path": info["path"], "entry_timestamp": info["entry_timestamp"],
            "entry_price": trade.entry_price, "stop_price": trade.stop_price, "outcome": "censored",
            "exit_timestamp": None, "exit_price": None, "exit_reasons": [], "r": None, "stop_hit": None,
            "mfe_r": None, "strict_pre_exit_mfe_r": None,
        }
        if trade_id in closed_by_id:
            end, event = closes[trade_id]
            closed = closed_by_id[trade_id]
            risk = abs(trade.entry_price - trade.stop_price)
            if risk <= 0:
                raise ValueError("closed frozen trade has non-positive initial risk")
            path_bars = [trace.source_bar for trace in ltf_traces[info["index"] + 1:end + 1]]
            expected = (ltf_traces[end].source_bar.open_time - ltf_traces[info["index"]].source_bar.open_time) // M15_DURATION_MS
            contiguous = len(path_bars) == expected and all(
                bar.open_time == ltf_traces[info["index"]].source_bar.open_time + (offset + 1) * M15_DURATION_MS
                for offset, bar in enumerate(path_bars)
            )
            mfe = strict = None
            if contiguous:
                high = max((bar.high for bar in path_bars), default=trade.entry_price)
                low = min((bar.low for bar in path_bars), default=trade.entry_price)
                pre_high = max((bar.high for bar in path_bars[:-1]), default=trade.entry_price)
                pre_low = min((bar.low for bar in path_bars[:-1]), default=trade.entry_price)
                favorable = max(0.0, high - trade.entry_price) if trade.side is Direction.LONG else max(0.0, trade.entry_price - low)
                pre_favorable = max(0.0, pre_high - trade.entry_price) if trade.side is Direction.LONG else max(0.0, trade.entry_price - pre_low)
                mfe, strict = favorable / risk, pre_favorable / risk
            reasons = [reason.value for reason in event.reasons]
            row.update({"outcome": "closed", "exit_timestamp": closed.exit_timestamp, "exit_price": closed.canonical_exit_price,
                        "exit_reasons": reasons, "r": closed.net_pnl / (risk * closed.quantity), "stop_hit": "exit_stop" in reasons,
                        "mfe_r": mfe, "strict_pre_exit_mfe_r": strict})
        rows.append(row)
    return sorted(rows, key=lambda row: (int(row["entry_timestamp"]), str(row["trade_id"]))), sorted(setups.values(), key=lambda row: int(row["timestamp"])), len(setups)


def _label(timestamp_ms: int, kind: str) -> str:
    stamp = datetime.fromtimestamp(timestamp_ms / 1000, UTC)
    if kind == "month": return f"{stamp.year:04d}-{stamp.month:02d}"
    if kind == "quarter": return f"{stamp.year:04d}-Q{(stamp.month - 1) // 3 + 1}"
    if kind == "year": return str(stamp.year)
    raise ValueError("unknown calendar period")


def _periods(rows: Sequence[Mapping[str, Any]], kind: str) -> dict[str, dict[str, Any]]:
    grouped: dict[str, list[Mapping[str, Any]]] = defaultdict(list)
    for row in rows:
        grouped[_label(int(row["entry_timestamp"]), kind)].append(row)
    return {label: _economics(items) for label, items in sorted(grouped.items()) if _closed(items)}


def _segment(rows: Sequence[Mapping[str, Any]], setups: Sequence[Mapping[str, Any]], field: str, values: Sequence[str]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for value in values:
        items = [item for item in rows if item[field] == value]
        eligible = [item for item in setups if item.get("path") in {"immediate_open", "armed_then_opened", "armed_then_cancelled"} and (item.get("direction") == value if field == "direction" else item.get("path") == value)]
        economics = _economics(items, eligible_setups=len(eligible))
        result[value] = {"eligible_setups": len(eligible), "setups": len(eligible), "opened_trades": len(items), "trades": len(items), **economics, **_tail_counts(items)}
    if field == "direction":
        positive = {value for value, item in result.items() if item["total_r"] > 0}
        result["edge_classification"] = "both_positive" if positive == {"long", "short"} else "long_only_edge" if positive == {"long"} else "short_only_edge" if positive == {"short"} else "neither"
    return result


def _tail(rows: Sequence[Mapping[str, Any]]) -> dict[str, Any]:
    closed = _closed(rows)
    winners = sorted((row for row in closed if float(row["r"]) > 0), key=lambda row: (-float(row["r"]), int(row["exit_timestamp"]), str(row["trade_id"])))
    total, positive = sum(float(row["r"]) for row in closed), sum(float(row["r"]) for row in winners)
    report: dict[str, Any] = {"2r_plus": sum(float(row["r"]) >= 2 for row in closed), "3r_plus": sum(float(row["r"]) >= 3 for row in closed), "5r_plus": sum(float(row["r"]) >= 5 for row in closed), "10r_plus": sum(float(row["r"]) >= 10 for row in closed), "maximum_winner_r": None if not winners else float(winners[0]["r"])}
    for count in (1, 3, 5, 10):
        selected = winners[:count]; contribution = sum(float(row["r"]) for row in selected)
        remaining = [row for row in closed if str(row["trade_id"]) not in {str(item["trade_id"]) for item in selected}]
        report[f"top_{count}"] = {"trade_ids": [str(row["trade_id"]) for row in selected], "r": float(contribution), "positive_r_percentage": None if positive == 0 else 100 * contribution / positive, "net_r_percentage": None if total == 0 else 100 * contribution / total, "removal": _economics(remaining)}
    report["extreme_tail_dependence_warning"] = report["top_5"]["removal"]["total_r"] <= 0
    report["single_trade_dependence_failure"] = report["top_1"]["removal"]["total_r"] <= 0
    return report


def _mfe_diagnostics(stopped: Sequence[Mapping[str, Any]]) -> dict[str, Any]:
    def summary(key: str) -> dict[str, Any]:
        values = [float(row[key]) for row in stopped if row[key] is not None]
        bucket = {"<0.25": 0, "0.25_to_<0.5": 0, "0.5_to_<1": 0, "1_to_<2": 0, "2_to_<3": 0, "3_to_<5": 0, "5_plus": 0}
        for value in values:
            label = "<0.25" if value < .25 else "0.25_to_<0.5" if value < .5 else "0.5_to_<1" if value < 1 else "1_to_<2" if value < 2 else "2_to_<3" if value < 3 else "3_to_<5" if value < 5 else "5_plus"; bucket[label] += 1
        return {"count": len(stopped), "observed_count": len(values), "missing_path_count": len(stopped) - len(values), "mean_mfe_r": None if not values else float(mean(values)), "median_mfe_r": None if not values else float(median(values)), "distribution_buckets": bucket}
    comparable = [(float(row["mfe_r"]), float(row["strict_pre_exit_mfe_r"])) for row in stopped if row["mfe_r"] is not None and row["strict_pre_exit_mfe_r"] is not None]
    return {"full_exit_bar_mfe": summary("mfe_r"), "strict_pre_exit_mfe": summary("strict_pre_exit_mfe_r"), "full_vs_strict_difference": {"comparable_count": len(comparable), "full_strictly_greater_count": sum(full > strict for full, strict in comparable), "aggregate_full_minus_strict_r": float(sum(full - strict for full, strict in comparable)), "mean_full_minus_strict_r": None if not comparable else float(mean(full - strict for full, strict in comparable))}}


def _ex_best_period(rows: Sequence[Mapping[str, Any]], periods: Mapping[str, Mapping[str, Any]], kind: str) -> dict[str, Any]:
    """Remove the highest-R period, choosing the earliest label on ties."""
    best = None if not periods else min(periods, key=lambda label: (-float(periods[label]["total_r"]), label))
    remaining = [row for row in rows if best is None or _label(int(row["entry_timestamp"]), kind) != best]
    return {"best_period": best, "frictionless": _economics(remaining), "friction": {f"{cost:.2f}": _economics(remaining, cost_r=cost) for cost in (.05, .10, .15, .20)}}


def _chronological(rows: Sequence[Mapping[str, Any]]) -> dict[str, Any]:
    ordered = sorted(_closed(rows), key=lambda row: (int(row["exit_timestamp"]), str(row["trade_id"])))
    def partitions(count: int) -> list[list[Mapping[str, Any]]]:
        base, remainder = divmod(len(ordered), count); cursor = 0; output = []
        for index in range(count):
            width = base + (1 if index < remainder else 0); output.append(ordered[cursor:cursor + width]); cursor += width
        return output
    quarters = {str(index + 1): _economics(items) for index, items in enumerate(partitions(4))}
    halves = {str(index + 1): _economics(items) for index, items in enumerate(partitions(2))}
    values = [float(row["r"]) for row in ordered]
    rolling: dict[str, Any] = {}
    for window in (20, 50):
        candidates = [(sum(values[index:index + window]), index) for index in range(max(0, len(values) - window + 1))]
        worst = min(candidates, key=lambda item: item[0], default=None)
        best = max(candidates, key=lambda item: item[0], default=None)
        rolling[str(window)] = {"worst_total_r": None if worst is None else float(worst[0]), "best_total_r": None if best is None else float(best[0]), "worst_start_trade_ordinal": None if worst is None else worst[1] + 1, "best_start_trade_ordinal": None if best is None else best[1] + 1, "overlapping_non_independent": True}
    cumulative = peak = 0.0; max_dd = 0.0; peak_index = trough_index = 0; current_peak = 0; drawdown_peak_value = 0.0
    longest_loss = longest_win = loss = win = 0
    for index, row in enumerate(ordered, 1):
        cumulative += float(row["r"])
        if cumulative > peak: peak, current_peak = cumulative, index
        if peak - cumulative > max_dd:
            max_dd, peak_index, trough_index, drawdown_peak_value = peak - cumulative, current_peak, index, peak
        if float(row["r"]) < 0: loss, win = loss + 1, 0
        elif float(row["r"]) > 0: win, loss = win + 1, 0
        else: loss = win = 0
        longest_loss, longest_win = max(longest_loss, loss), max(longest_win, win)
    recovery = next((index for index, _row in enumerate(ordered, 1) if index > trough_index and sum(float(item["r"]) for item in ordered[:index]) >= drawdown_peak_value), None)
    return {"quartiles": quarters, "halves": halves, "chronological_edge_decay_warning": (halves["2"]["expectancy_r"] is not None and halves["2"]["expectancy_r"] <= 0) or not _pf_gt(halves["2"], 1), "maximum_drawdown_r": float(max_dd), "peak_trade_index": peak_index, "trough_trade_index": trough_index, "recovery_trade_index": recovery, "ending_cumulative_r": float(cumulative), "longest_losing_streak": longest_loss, "longest_winning_streak": longest_win, "rolling": rolling}


def _friction(rows: Sequence[Mapping[str, Any]]) -> dict[str, Any]:
    baseline = _economics(rows)
    levels: dict[str, Any] = {}
    for cost in PREDECLARED_FRICTION_COSTS_R:
        result = _economics(rows, cost_r=cost)
        result["retained_percentage"] = None if baseline["total_r"] == 0 else 100 * result["total_r"] / baseline["total_r"]
        levels[f"{cost:.2f}"] = result
    def breakeven(items: Sequence[Mapping[str, Any]]) -> float | None:
        closed = _closed(items); return None if not closed else float(sum(float(row["r"]) for row in closed) / len(closed))
    return {"levels": levels, "breakeven_effective_cost_r": breakeven(rows), "segment_breakeven_effective_cost_r": {"long": breakeven([row for row in rows if row["direction"] == "long"]), "short": breakeven([row for row in rows if row["direction"] == "short"]), "immediate_open": breakeven([row for row in rows if row["path"] == "immediate_open"]), "armed_then_opened": breakeven([row for row in rows if row["path"] == "armed_then_opened"])}}


def _gate(report: Mapping[str, Any]) -> dict[str, Any]:
    aggregate, quarters, tail = report["aggregate"], report["temporal"]["quarters"], report["tail"]
    loo = report["temporal"]["leave_one_quarter_out"]
    ex_best, friction = report["ex_best_period"]["quarter"], report["friction"]["levels"]["0.10"]
    loo_pass = [item for item in loo.values() if item["total_r"] > 0 and item["expectancy_r"] is not None and item["expectancy_r"] > 0 and _pf_gt(item, 1)]
    matrix = {
        "population": aggregate["closed_trades"] >= 250, "total_r": aggregate["total_r"] > 0,
        "expectancy": aggregate["expectancy_r"] is not None and aggregate["expectancy_r"] >= .10,
        "profit_factor": _pf_gte(aggregate, 1.15),
        "positive_quarters": bool(quarters) and sum(item["total_r"] > 0 for item in quarters.values()) / len(quarters) >= .50,
        "leave_one_quarter_out": len(quarters) >= 4 and len(loo_pass) / len(quarters) >= .75,
        "single_trade_independence": not tail["single_trade_dependence_failure"],
        "ex_best_quarter": ex_best["total_r"] > 0 and ex_best["expectancy_r"] is not None and ex_best["expectancy_r"] > 0 and _pf_gt(ex_best, 1),
        "friction_0_10": friction["total_r"] > 0 and friction["expectancy_r"] is not None and friction["expectancy_r"] > 0 and _pf_gt(friction, 1),
        "methodology": "PENDING_INDEPENDENT_LUNA_REVIEW", "no_tuning": True,
    }
    hard_fail = aggregate["expectancy_r"] is not None and aggregate["expectancy_r"] <= 0 or aggregate["total_r"] <= 0 or (aggregate["profit_factor"] is not None and aggregate["profit_factor"] <= 1) or tail["single_trade_dependence_failure"]
    quantitative_decision = "FAIL" if hard_fail else "INCONCLUSIVE" if aggregate["closed_trades"] < 250 else "PASS" if all(value is True for value in matrix.values() if isinstance(value, bool)) else "CONDITIONAL"
    return {"matrix": matrix, "leave_one_quarter_out_pass_ratio": None if not quarters else len(loo_pass) / len(quarters), "quantitative_provisional_decision": quantitative_decision, "final_decision": "PENDING_INDEPENDENT_LUNA_REVIEW"}


def _build_stage_b_report(*, xm_m1_source: Path) -> dict[str, Any]:
    """Authorized Stage B result construction; callers must use the guard."""
    raw = _load_full_xm_m1(xm_m1_source)
    ltf, htf, h4_exclusions, aggregation_audit = _replay_inputs(raw)
    replay, warmup = _run_warmed_replay(ltf, htf)
    rows, setups, observed_setups = _ledger(replay)
    eligible_setups = sum(item.get("path") in {"immediate_open", "armed_then_opened", "armed_then_cancelled"} for item in setups)
    aggregate = _economics(rows, eligible_setups=eligible_setups)
    months, quarters, years = _periods(rows, "month"), _periods(rows, "quarter"), _periods(rows, "year")
    loo = {label: _economics([row for row in rows if _label(int(row["entry_timestamp"]), "quarter") != label]) for label in quarters}
    loo_year = {label: _economics([row for row in rows if _label(int(row["entry_timestamp"]), "year") != label]) for label in years} if len(years) >= 2 else {"status": "insufficient"}
    ex_quarter = _ex_best_period(rows, quarters, "quarter")
    ex_month = _ex_best_period(rows, months, "month")
    best_quarter, best_month = ex_quarter["best_period"], ex_month["best_period"]
    stopped = [row for row in _closed(rows) if row["stop_hit"]]
    def period_extremes(periods: Mapping[str, Mapping[str, Any]]) -> dict[str, Any]:
        if not periods: return {"best": None, "worst": None}
        labels = list(periods)
        return {"best": min(labels, key=lambda label: (-float(periods[label]["total_r"]), label)), "worst": min(labels, key=lambda label: (float(periods[label]["total_r"]), label))}
    report: dict[str, Any] = {
        "schema_version": "xm-gold-historical-validation-result/v1", "decision_authority": "Sol/main only",
        "protocol_sha256": EXPECTED_PROTOCOL_SHA256, "raw_source_sha256": EXPECTED_XM_RAW_SHA256,
        "source_and_warmup": {"raw_first_timestamp": raw[0].open_time, "raw_last_timestamp": raw[-1].open_time, "first_m15_bar": ltf[0].open_time, "first_h4_bar": htf[0].open_time, "validation_start_timestamp": warmup["first_strategy_eligible_timestamp"], "validation_end_exclusive_timestamp": HISTORICAL_CUTOFF_MS, **warmup},
        "aggregation": {**aggregation_audit, "h4_exclusions": h4_exclusions},
        "population": {"observed_setups": observed_setups, "eligible_setups": eligible_setups, "opened_trades": len(rows), "closed_trades": aggregate["closed_trades"], "censored_trades": len(rows) - aggregate["closed_trades"], "long_opened": sum(row["direction"] == "long" for row in rows), "short_opened": sum(row["direction"] == "short" for row in rows), "immediate_opened": sum(row["path"] == "immediate_open" for row in rows), "armed_opened": sum(row["path"] == "armed_then_opened" for row in rows)},
        "aggregate": aggregate, "closed_trade_ledger": rows,
        "temporal": {"months": months, "quarters": quarters, "years": years, "month_extremes": period_extremes(months), "quarter_extremes": period_extremes(quarters), "year_extremes": period_extremes(years), "leave_one_quarter_out": loo, "leave_one_year_or_partial_year_out": loo_year, "profitable_month_ratio": None if not months else sum(item["total_r"] > 0 for item in months.values()) / len(months), "profitable_quarter_ratio": None if not quarters else sum(item["total_r"] > 0 for item in quarters.values()) / len(quarters), "quarter_concentration_warning": False},
        "chronological": _chronological(rows), "direction": _segment(rows, setups, "direction", ("long", "short")), "setup_path": _segment(rows, setups, "path", ("immediate_open", "armed_then_opened")), "tail": _tail(rows),
        "stop_giveback": {"stopped_count": len(stopped), "total_stop_r": float(sum(float(row["r"]) for row in stopped)), **_mfe_diagnostics(stopped)},
        "friction": _friction(rows), "ex_best_period": {"best_quarter": best_quarter, "quarter": ex_quarter["frictionless"], "quarter_friction": ex_quarter["friction"], "best_month": best_month, "month": ex_month["frictionless"]},
        "compatibility_period_reference": {"known_closed_trades": 192, "known_total_r_approx": 70.597728, "known_expectancy_r_approx": .3677, "known_profit_factor_approx": 1.5691, "merged_into_primary": False},
        "regeneration_record": {"invalidated_result_sha256": ["dcf3163cb894d1969f7bc6156078e61b589266daf644d21782aea37eea1649dd", "a983e2cc305839af783ef70fc3709433d52f2cf75ef9034cca934f44ec3cf062"], "status": "INVALIDATED", "defect_discovery_timing": "Sol code review before examining historical economics; Terra run raced message delivery", "severity_summary": "methodological reporting and gate implementation defects required invalidation", "full_regeneration_required": True, "version_selection": "No better-performing version was selected or retained; every affected result was invalidated and requires full deterministic regeneration."},
    }
    positive_quarter_r = sum(max(0.0, item["total_r"]) for item in quarters.values())
    if best_quarter is not None:
        report["temporal"]["quarter_concentration_warning"] = positive_quarter_r > 0 and quarters[best_quarter]["total_r"] / positive_quarter_r > .70
    report["gate"] = _gate(report)
    historical_expectancy = aggregate["expectancy_r"]
    report["compatibility_period_reference"]["descriptive_classification"] = "weaker" if historical_expectancy is not None and historical_expectancy < .3677 else "similar" if historical_expectancy == .3677 else "stronger"
    report["compatibility_period_reference"]["directionally_consistent"] = bool(historical_expectancy is not None and historical_expectancy > 0)
    return report


def xm_gold_historical_validation_json(report: Mapping[str, Any]) -> bytes:
    return (canonical_json(dict(report)) + "\n").encode("utf-8")


def write_xm_gold_historical_validation_report(report: Mapping[str, Any], path: Path) -> str:
    if path.exists():
        raise FileExistsError(f"refusing to overwrite historical validation result: {path}")
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = xm_gold_historical_validation_json(report)
    path.write_bytes(payload)
    return sha256(payload).hexdigest()


def build_xm_gold_historical_validation_report(
    *, repo_root: Path, xm_m1_source: Path, compatibility_artifact: Path, protocol_path: Path,
    protocol_sha256_expected: str = EXPECTED_PROTOCOL_SHA256,
) -> dict[str, Any]:
    return run_stage_b_guarded(protocol_path, protocol_sha256_expected, lambda _protocol: _build_stage_b_report(xm_m1_source=xm_m1_source), repo_root=repo_root, xm_m1_source=xm_m1_source, compatibility_artifact=compatibility_artifact)
