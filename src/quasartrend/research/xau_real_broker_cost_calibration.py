"""Stage A lock for XAU real-broker-cost calibration.

This module is deliberately protocol-only.  It does not open the raw XM CSV,
the frozen trade ledger, or any result artifact, and it imports no strategy,
replay, backtest, or execution implementation.  A later, separately approved
Stage B may use :func:`protocol_before_economics` as its first guard.
"""
from __future__ import annotations

from hashlib import sha256
import json
from pathlib import Path
from typing import Any, Callable, Mapping, TypeVar

from .provenance import canonical_json


SCHEMA_VERSION = "xau-real-broker-cost-calibration-protocol/v1"
PROTOCOL_PATH = "exports/xm/phase_xau_real_broker_cost_calibration_protocol.json"
CANONICAL_STARTING_SHA = "af02995705506ab1629f558e6fbdabe13d2d0785"
WAITING_TAG = "xau-directional-confirmation-waiting"
WAITING_TAG_OBJECT = "a95c26c7cb5458df48bb49bad5791b4e22cba972"
FROZEN_RAW_PATH = "exports/xm/XM_GOLD_M1_raw.csv"
FROZEN_RAW_SHA256 = "3817558149502888bcee711fb803e6085c5d48cbf36d33901d85ac85c0c0d81a"
HISTORICAL_PROTOCOL_PATH = "exports/xm/phase_xm_gold_historical_validation_protocol.json"
HISTORICAL_PROTOCOL_SHA256 = "2c690292b3f2a53c0295cb153cf0721044ba3d24c55b56359e32f030c7ee7870"
HISTORICAL_VALIDATION_CANONICAL_SHA = "446f93cfbad601a7517caac54fb2f2791fc2e5fe"
HISTORICAL_RESULT_PATH = "exports/xm/phase_xm_gold_historical_validation.json"
HISTORICAL_RESULT_SHA256 = "5e81a9ad805496e0a3d8578821485f9469998cf9d2f37bff2b9b5e4be1670d0d"
COMPATIBILITY_PATH = "exports/xm/phase_xm_gold_compatibility.json"
COMPATIBILITY_SHA256 = "d9d23efb0a6343d83c06c0fb79d67d245528e4749e834f8f26b06c7bf3c09176"
FROZEN_DIRECTION_PROTOCOL_SHA256 = "c201170afb974b4299e06608556ee49acfd2b11c950e0076a841e8d4412629ef"
FROZEN_DIRECTION_STATUS_SHA256 = "3c76f1664d3931dd34847aeda661d731e679abfba76f6559969775f100ac1e06"
EXPECTED_PROTOCOL_SHA256 = "acd1a6573018adc5819c4f66e418c3f75f8c0e93b99dd41a864141e56106e837"

_T = TypeVar("_T")


def build_xau_real_broker_cost_calibration_protocol() -> dict[str, Any]:
    """Return the complete, result-independent Stage A protocol.

    All unknowns remain unknown.  In particular, no broker value, cost, or
    trade result is derived here.
    """
    return {
        "schema_version": SCHEMA_VERSION,
        "stage": "A — cost-calibration protocol freeze only",
        "decision_authority": "Sol/main only; this protocol does not approve or advance a phase",
        "protocol_before_new_net_economics": True,
        "canonical_starting_state": {
            "sha": CANONICAL_STARTING_SHA,
            "local_main_required": CANONICAL_STARTING_SHA,
            "origin_main_required": CANONICAL_STARTING_SHA,
            "divergence_required": "0 behind / 0 ahead",
            "tracked_worktree_required": "clean",
            "waiting_tag": WAITING_TAG,
            "waiting_tag_object": WAITING_TAG_OBJECT,
            "waiting_tag_peeled_target": CANONICAL_STARTING_SHA,
        },
        "directional_confirmation_freeze": {
            "protocol_sha256": FROZEN_DIRECTION_PROTOCOL_SHA256,
            "status_sha256": FROZEN_DIRECTION_STATUS_SHA256,
            "window": "[2026-08-28T20:58:00Z, 2027-03-01T23:00:00Z)",
            "hypotheses": {"H1": "WAITING FOR UNTOUCHED DATA", "H2": "WAITING FOR UNTOUCHED DATA"},
            "rule": "Do not inspect strategy economics from this future window; do not use post-cutoff XAU data for directional analysis or bias persistence.",
        },
        "frozen_historical_validation": {
            "canonical_starting_sha": HISTORICAL_VALIDATION_CANONICAL_SHA,
            "raw_source": {"path": FROZEN_RAW_PATH, "sha256": FROZEN_RAW_SHA256},
            "historical_protocol": {"path": HISTORICAL_PROTOCOL_PATH, "sha256": HISTORICAL_PROTOCOL_SHA256},
            "historical_result": {"path": HISTORICAL_RESULT_PATH, "sha256": HISTORICAL_RESULT_SHA256},
            "compatibility": {"path": COMPATIBILITY_PATH, "sha256": COMPATIBILITY_SHA256},
            "population": {"observed_setups": 2162, "eligible_setups": 1072, "opened_trades": 820, "closed_trades": 820, "censored": 0},
            "gross_reference": {"total_r_approx": 70.720015, "expectancy_r_per_trade_approx": 0.086244, "profit_factor_approx": 1.118230, "win_rate_approx": 0.240244, "stop_rate_approx": 0.706098},
            "known_synthetic_friction_context_only": {"0.00R": 70.720, "0.05R": 29.720, "0.10R": -11.280, "breakeven_r_per_trade_approx": 0.086244},
            "rule": "Synthetic friction is not a broker-cost measurement; preserve the existing frozen trades and frozen initial-risk R denominator.",
        },
        "broker_identity": {
            "provider": "XM / XMGlobal", "mt5_server": "XMGlobal-MT5 18", "symbol": "GOLD",
            "instrument_path": r"Derivatives\Spot Metals\GOLD", "digits": 2, "point": 0.01,
            "account_type": "UNKNOWN / UNVERIFIED", "account_currency": "UNKNOWN",
            "explicit_commission_report": "User-reported zero; not verified by relevant account specification or sanitized deal history.",
            "identity_rule": "Do not silently substitute an XM entity, account type, GOLD contract, symbol, server, or specification.",
        },
        "evidence_hierarchy": {
            "tier_a_direct_observed": ["actual MT5 symbol specification", "actual spread observations on the relevant XM account/server", "sanitized order/deal history for commission, swap, requested and executed fills", "actual tick or bid/ask history"],
            "tier_b_official": ["contract/commission/swap/volume/tick/stop/freeze/trading-hours/account-type documentation"],
            "tier_c_modelled": "MODELLED — NOT OBSERVED; only conservative predeclared assumptions when direct execution observations do not exist.",
            "official_retrieval_date": "2026-08-30",
            "official_sources": [
                {"name": "MqlRates", "url": "https://www.mql5.com/en/docs/constants/structures/mqlrates"},
                {"name": "MqlRates spread semantics", "url": "https://www.mql5.com/en/book/applications/timeseries/timeseries_mqlrates"},
                {"name": "Spread points", "url": "https://www.mql5.com/en/docs/standardlibrary/tradeclasses/csymbolinfo/csymbolinfospread"},
                {"name": "Symbol properties/chart mode/tick/volume/stops", "url": "https://www.mql5.com/en/docs/constants/environment_state/marketinfoconstants"},
                {"name": "Deal commission/swap", "url": "https://www.mql5.com/en/docs/constants/tradingconstants/dealproperties"},
                {"name": "Swap fields/modes", "url": "https://www.mql5.com/en/book/automation/symbols/symbols_swaps"},
                {"name": "XM account types", "url": "https://www.xm.com/account-types"},
                {"name": "XMGlobal client agreement", "url": "https://www.xm.com/assets/pdf/new/terms/XMGlobal-Client-Agreement-Terms-and-Conditions-of-Business.pdf"},
            ],
            "official_documentation_limit": "Generic official documentation cannot substitute account-specific values.",
        },
        "data_sufficiency_gate": {
            "essential_inputs": ["identity", "account type", "SYMBOL_CHART_MODE", "contract size", "tick size", "profit/loss tick values", "calculation/currency fields", "account currency implications", "commission", "swap structure/history", "spread behavior", "execution/slippage evidence", "volume rules", "order/execution constraints"],
            "waiting_outcome": "XAU REAL BROKER COST CALIBRATION: WAITING FOR BROKER COST DATA",
            "rule": "If essential inputs cannot be established defensibly, do not invent them and do not manufacture a net expectancy.",
        },
        "spread": {
            "raw_column_semantics": "MqlRates minimum spread per M1 bar in integer MT5 points.",
            "points_to_price_formula": "spread_price = spread_points * symbol_point (0.01)",
            "classification": "OBSERVED — M1 BAR-MINIMUM SPREAD, NOT POINT-IN-TIME EXECUTABLE SPREAD",
            "limitation": "There are no bid/ask levels, quote time, or fill; the minimum is a lower-bound bar statistic that can understate execution spread. Never infer spread from OHLC ranges.",
            "lookup": "Previous exact-minute M1 row only: last completed bar strictly before execution timestamp with exact prior-minute adjacency. It is no-lookahead descriptive interval evidence only, not execution-time evidence; gaps are missing with no imputation; a same-timestamp M1 row is the following interval and prohibited.",
            "chart_mode_requirement": "Actual GOLD SYMBOL_CHART_MODE is required before executable spread attribution.",
            "bid_chart_mode_attribution": {"long_entry": "crossing spread", "long_exit": "zero", "short_entry": "zero", "short_exit": "crossing spread"},
            "last_or_unknown_chart_mode": "Spread application incomplete; no core/net scenario.",
            "no_double_counting": True,
        },
        "conversion": {
            "account_value_formula": "abs(delta_price) / tick_size * tick_value * lots; use direction-appropriate profit/loss tick value and account-currency conversion when required.",
            "initial_risk_account_formula": "abs(entry_price - stop_price) / tick_size * tick_value * lots",
            "frozen_trade_risk_binding": "initial_risk_price_i = abs(entry_price_i - stop_price_i) from the frozen ledger.",
            "monetary_risk_requirement": "Monetary initial risk additionally requires frozen quantity and verified tick monetary conversion; missing quantity blocks account-unit trade values but not proportional price-distance cost_R.",
            "component_cost_r_formula": "component_cost_account / initial_risk_account",
            "price_only_cost_r_formula": "component_cost_price / abs(entry_price - stop_price)",
            "total_cost_r": "sum of component cost_R values",
            "net_r": "gross_R - total_cost_R",
            "denominator_rule": "Use each frozen trade's actual initial risk; never realized winner size or average stop distance.",
        },
        "commission": {
            "observed_source": "Sanitized relevant-account deal history using DEAL_COMMISSION plus DEAL_FEE; retain deal type and currency.",
            "required_reporting": ["per side or round trip", "per lot/notional basis", "currency", "trade-level conversion"],
            "zero_report_rule": "Reported zero remains unverified until relevant account evidence establishes it.",
        },
        "swap_financing": {
            "required": ["swap mode", "long rate", "short rate", "rollover3days", "actual historical DEAL_SWAP where possible", "server-timezone rollover crossings"],
            "current_vs_historical": "CURRENT/OBSERVED SWAP STRUCTURE must remain separate from HISTORICAL SWAP UNKNOWN; do not backfill today's rates across years.",
            "islamic_swap_free_status": "UNKNOWN",
            "missing_rule": "Historical swap unavailable is an essential unknown; scenario incomplete unless a bounded scenario is scientifically defensible and labelled.",
        },
        "slippage": {
            "observed": "Only sanitized orders/deals with direction-correct adverse executed minus request/decision/reference price; report distribution.",
            "without_fills": "Do not claim measured slippage.",
            "scenario_2": "MODELLED — NOT OBSERVED: one verified SYMBOL_TRADE_TICK_SIZE adverse per side, only when observed slippage is missing.",
            "scenario_3": "MODELLED — NOT OBSERVED: 0.5 * matched M1 bar-minimum spread price adverse at entry and at exit (one additional spread round trip); this is a lower-bound-derived stress base, not observed executable spread.",
            "stop_gap_rule": "Stop execution gaps remain slippage; never reroute exits.",
        },
        "other_execution_inputs": {
            "inspect": ["stop gaps", "minimum stop distance", "freeze level", "volume rounding", "order rejection", "market closure", "rollover spread expansion", "conversion costs", "sessions", "fill/execution/order modes", "floating spread"],
            "unknown_until_capture": "Actual frozen-account/server symbol capture is required for specification values.",
            "volume": "No synthetic strategy sizing. Use frozen trade risk; account-unit illustrations use 1.0 lot only after specification verification. Report scaling/rounding separately without suppressing trades.",
        },
        "scenarios": {
            "S0_frictionless_control": "Frozen original result.",
            "S1_observed_core": "Account-specific executable spread evidence (bid/ask ticks/quotes or fills) plus account-verified commission plus historical swap/deal costs plus observed slippage only; M1 bar-minimum spread may be reported descriptively but cannot complete S1; incomplete if any required component is missing.",
            "S2_realistic_execution": "S1 plus modelled one verified tick adverse at entry and exit only when observed slippage is missing.",
            "S3_conservative_stress": "S1 plus modelled 0.5 matched M1 bar-minimum spread adverse at each entry and exit as an explicitly lower-bound-derived stress base.",
            "missing_rule": "S2/S3 cannot overcome missing essential S1 identity, account, chart mode, executable spread, swap, or verified value specification; all are incomplete and must not manufacture net expectancy.",
            "selection_rule": "Scenario values are evidence-driven and predeclared; the synthetic break-even is context only and must not choose assumptions.",
        },
        "required_reporting": {
            "per_complete_scenario": ["closed trades", "gross total R", "total/mean/median/p75/p90/p95 cost R", "net total R", "net expectancy", "net PF", "net win rate", "net positive-R sum", "net negative-R magnitude", "spread/commission/swap/slippage/other R"],
            "trade_level": ["direction", "entry/exit timestamp", "entry/exit price", "initial risk", "holding duration", "gross R", "component costs in price/account/R", "total cost R", "net R"],
            "directions": "LONG and SHORT gross/cost/net attribution only; no filtering or directional recommendation.",
            "temporal": ["calendar year", "calendar quarter"],
            "tail": ["2R+, 3R+, 5R+, 10R+ winner counts", "max net winner", "top 1/3/5/10 contribution", "net total removing top 1/3/5/10"],
            "path_risk": ["max cumulative-R drawdown", "longest losing streak", "worst rolling 20-trade R", "worst rolling 50-trade R", "ending cumulative R"],
            "order": "Chronological closed-trade order; never reorder trades.",
            "break_even": "Recalculate exactly as gross total R / 820; cost-utilization is realistic mean cost R per trade divided by precise break-even R per trade.",
        },
        "decision_framework": {
            "precedence": ["WAITING", "FAIL", "PASS", "CONDITIONAL"],
            "WAITING": "Any essential identity/account/chart-mode/value/commission/swap evidence prevents defensible S2.",
            "FAIL": "Only with complete S2 and net total <= 0 OR expectancy <= 0 OR PF <= 1 OR utilization >= 1.",
            "PASS": "Only with complete S2 net total > 0, expectancy > 0, PF > 1, utilization < 0.75, and S3 net total > 0, expectancy > 0, PF > 1.",
            "CONDITIONAL": "Complete core positive and complete S2 positive but not PASS, including utilization in [0.75, 1), stress threat, or material nonessential uncertainty.",
            "no_retroactive_thresholds": True,
        },
        "protocol_lock": {
            "immutable": True,
            "stage_b_requires_exact_protocol_sha256": True,
            "stage_b_requires_protocol_commit_before_economics": True,
            "builder_scope": "Must not open a trade ledger/raw source or import execution/economic code.",
            "amendment_rule": "No amendment after net results are seen; a correctness defect stops for Sol review.",
        },
        "forbidden_analyses": ["strategy optimization", "XAU parameter/TP/SL/RR/entry/exit tuning", "directional-rule research", "LONG-only or SHORT-disable research", "4H bias-persistence confirmation", "confirmatory H1/H2 economics", "production deployment", "XAU live-readiness", "BTC Phase 7.4"],
        "hard_restrictions": {"production_strategy_change": "NO", "production_market_change": "NO", "xau_parameter_tuning": "NO", "entry_change": "NO", "exit_change": "NO", "tp_rr_optimization": "NO", "stop_change": "NO", "long_only": "NO", "short_disable": "NO", "directional_filter": "NO", "bias_persistence_test": "NO", "confirmatory_h1_h2_economics": "NO", "btc_phase_7_4": "DEFERRED"},
        "no_tuning_statement": "The frozen strategy remains unchanged. Costs are applied to existing frozen trades only; no exit rerouting, stop changes, or trade suppression is permitted.",
        "privacy": "Do not commit credentials, account numbers, personal information, or secrets. Sanitize broker exports while retaining reproducible cost evidence.",
    }


def xau_real_broker_cost_calibration_protocol_json(protocol: Mapping[str, Any]) -> bytes:
    """Serialize exactly one canonical protocol representation."""
    return (canonical_json(dict(protocol)) + "\n").encode("utf-8")


def protocol_sha256(protocol: Mapping[str, Any]) -> str:
    return sha256(xau_real_broker_cost_calibration_protocol_json(protocol)).hexdigest()


def expected_protocol_sha256() -> str:
    actual = protocol_sha256(build_xau_real_broker_cost_calibration_protocol())
    if actual != EXPECTED_PROTOCOL_SHA256:
        raise ValueError("canonical cost-calibration protocol bytes do not match the pinned SHA-256")
    return actual


def verify_stage_b_protocol_lock(path: Path, expected_sha256: str) -> dict[str, Any]:
    """Fail closed before a Stage B evaluator can inspect economic inputs."""
    if expected_sha256 != expected_protocol_sha256():
        raise ValueError("unknown protocol hash; Stage B requires the exact Stage A lock hash")
    raw = path.read_bytes()
    if sha256(raw).hexdigest() != expected_sha256:
        raise ValueError("protocol hash mismatch; Stage B is not authorized")
    canonical = xau_real_broker_cost_calibration_protocol_json(build_xau_real_broker_cost_calibration_protocol())
    if raw != canonical:
        raise ValueError("protocol bytes differ from the immutable Stage A protocol")
    try:
        parsed = json.loads(raw)
    except (UnicodeDecodeError, ValueError) as error:
        raise ValueError("locked protocol is not valid JSON") from error
    if parsed != build_xau_real_broker_cost_calibration_protocol():
        raise ValueError("protocol semantic content differs from the immutable Stage A protocol")
    return parsed


def protocol_before_economics(path: Path, expected_sha256: str, evaluator: Callable[[dict[str, Any]], _T]) -> _T:
    """Authorize a caller only after exact locked protocol verification."""
    return evaluator(verify_stage_b_protocol_lock(path, expected_sha256))


def write_xau_real_broker_cost_calibration_protocol(protocol: Mapping[str, Any], path: Path) -> str:
    """Write the sole predeclared protocol once; never overwrite a lock."""
    expected = build_xau_real_broker_cost_calibration_protocol()
    payload = xau_real_broker_cost_calibration_protocol_json(protocol)
    if payload != xau_real_broker_cost_calibration_protocol_json(expected):
        raise ValueError("refusing to lock a protocol other than the predeclared canonical protocol")
    digest = sha256(payload).hexdigest()
    if digest != expected_protocol_sha256():
        raise ValueError("refusing an unpinned cost-calibration protocol hash")
    if path.exists():
        raise FileExistsError(f"refusing to overwrite immutable protocol lock: {path}")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(payload)
    return digest
