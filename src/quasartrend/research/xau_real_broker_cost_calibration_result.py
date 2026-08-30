"""Stage B WAITING report for frozen XAU broker-cost calibration.

The only historical market observation admitted here is the documented MT5 M1
*bar-minimum* spread statistic.  It is never converted into executable spread
or a cost applied to a trade.  The entry point verifies the separately
committed Stage A protocol before it opens any frozen artifact or raw source.
"""
from __future__ import annotations

import csv
from hashlib import sha256
import json
import math
from pathlib import Path
import subprocess
from typing import Any, Callable, Iterable, Mapping, Sequence, TypeVar

from .provenance import canonical_json
from .xau_real_broker_cost_calibration import (
    COMPATIBILITY_PATH, COMPATIBILITY_SHA256, FROZEN_DIRECTION_PROTOCOL_SHA256,
    FROZEN_DIRECTION_STATUS_SHA256, FROZEN_RAW_PATH, FROZEN_RAW_SHA256,
    HISTORICAL_PROTOCOL_PATH, HISTORICAL_PROTOCOL_SHA256, HISTORICAL_RESULT_PATH,
    HISTORICAL_RESULT_SHA256, PROTOCOL_PATH, EXPECTED_PROTOCOL_SHA256,
    verify_stage_b_protocol_lock,
)
from .xm_gold_compatibility import M1_MS, XM_RAW_SCHEMA, _parse_row
from .xm_gold_historical_validation import HISTORICAL_CUTOFF_MS


PROTOCOL_COMMIT_SHA = "14f54535a2b4bce9ac849a1669495238fe2fd280"
CANONICAL_STARTING_SHA = "af02995705506ab1629f558e6fbdabe13d2d0785"
WAITING_TAG = "xau-directional-confirmation-waiting"
WAITING_TAG_OBJECT = "a95c26c7cb5458df48bb49bad5791b4e22cba972"
RESULT_PATH = "exports/xm/phase_xau_real_broker_cost_calibration.json"
SCHEMA_VERSION = "xau-real-broker-cost-calibration-result/v1"
POINT = 0.01
EXPECTED_RAW_ROWS = 1_000_000
EXPECTED_CLOSED_TRADES = 820
EXPECTED_ENDPOINT_MATCHED = 1627
EXPECTED_ENDPOINT_MISSING = {
    "entry": [9, 12, 49, 86, 277, 432, 439, 526, 752],
    "exit": [592, 674, 683, 730],
}
EXPECTED_RESULT_SHA256 = "ab77f961c7babf1fc78c44432b3c1130137038cefbdf71750a5cdb6f2d4e1881"
_T = TypeVar("_T")


def _sha256_path(path: Path) -> str:
    digest = sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def verify_committed_protocol_before_inputs(repo_root: Path, protocol_path: Path) -> dict[str, Any]:
    """Verify exact committed Stage A bytes before a caller can open inputs."""
    root = repo_root.resolve()
    expected_path = (root / PROTOCOL_PATH).resolve()
    if protocol_path.resolve() != expected_path:
        raise ValueError("Stage B requires the canonical committed protocol path")
    def git(*args: str) -> bytes:
        return subprocess.run(("git", *args), cwd=root, check=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE).stdout
    try:
        commit = git("rev-parse", f"{PROTOCOL_COMMIT_SHA}^{{commit}}").decode().strip()
        if subprocess.run(("git", "merge-base", "--is-ancestor", commit, "HEAD"), cwd=root).returncode != 0:
            raise ValueError("cost-calibration protocol commit is not an ancestor of HEAD")
        committed = git("show", f"{commit}:{PROTOCOL_PATH}")
        parent = git("rev-parse", f"{commit}^").decode().strip()
        origin_main = git("rev-parse", "origin/main").decode().strip()
        tag_object = git("rev-parse", WAITING_TAG).decode().strip()
        peeled_target = git("rev-parse", f"{WAITING_TAG}^{{}}").decode().strip()
    except subprocess.CalledProcessError as error:
        raise ValueError("cost-calibration protocol commit or blob is unavailable") from error
    if sha256(committed).hexdigest() != EXPECTED_PROTOCOL_SHA256:
        raise ValueError("committed cost-calibration protocol hash mismatch")
    if parent != CANONICAL_STARTING_SHA:
        raise ValueError("cost-calibration protocol commit parent is not the canonical starting SHA")
    if origin_main != CANONICAL_STARTING_SHA:
        raise ValueError("origin/main is not the canonical starting SHA")
    if tag_object != WAITING_TAG_OBJECT or peeled_target != CANONICAL_STARTING_SHA:
        raise ValueError("directional-confirmation waiting tag identity mismatch")
    if protocol_path.read_bytes() != committed:
        raise ValueError("working cost-calibration protocol differs from committed bytes")
    protocol = verify_stage_b_protocol_lock(protocol_path, EXPECTED_PROTOCOL_SHA256)
    return {"protocol": protocol, "protocol_commit_sha": commit, "protocol_sha256": EXPECTED_PROTOCOL_SHA256, "protocol_parent_sha": parent, "origin_main_sha": origin_main, "waiting_tag_object": tag_object, "waiting_tag_peeled_target": peeled_target}


def required_hashes(repo_root: Path) -> dict[str, str]:
    """Verify frozen identities only after the protocol guard has succeeded."""
    root = repo_root.resolve()
    expected = {
        FROZEN_RAW_PATH: FROZEN_RAW_SHA256,
        HISTORICAL_PROTOCOL_PATH: HISTORICAL_PROTOCOL_SHA256,
        HISTORICAL_RESULT_PATH: HISTORICAL_RESULT_SHA256,
        COMPATIBILITY_PATH: COMPATIBILITY_SHA256,
        "exports/xm/phase_xau_directional_hypothesis_confirmation_protocol.json": FROZEN_DIRECTION_PROTOCOL_SHA256,
        "exports/xm/phase_xau_directional_hypothesis_confirmation.json": FROZEN_DIRECTION_STATUS_SHA256,
    }
    actual = {path: _sha256_path(root / path) for path in expected}
    if actual != expected:
        raise ValueError("frozen artifact hash mismatch")
    return actual


def percentile_type7(values: Sequence[float], percentile: float) -> float | None:
    """Deterministic Hyndman-Fan type-7 percentile used for spread reporting."""
    if not values:
        return None
    if not 0.0 <= percentile <= 100.0:
        raise ValueError("percentile must be in [0, 100]")
    ordered = sorted(float(value) for value in values)
    position = (len(ordered) - 1) * percentile / 100.0
    lower = int(position)
    upper = min(lower + 1, len(ordered) - 1)
    return ordered[lower] + (ordered[upper] - ordered[lower]) * (position - lower)


def spread_distribution_points(values: Sequence[float]) -> dict[str, float | int | None]:
    """Describe M1 bar-minimum points; this is not an executable cost."""
    if not values:
        return {"count": 0, "min": None, "mean": None, "median": None, "p75": None, "p90": None, "p95": None, "p99": None, "max": None}
    ordered = sorted(float(value) for value in values)
    return {
        "count": len(ordered), "min": ordered[0], "mean": sum(ordered) / len(ordered),
        "median": percentile_type7(ordered, 50), "p75": percentile_type7(ordered, 75),
        "p90": percentile_type7(ordered, 90), "p95": percentile_type7(ordered, 95),
        "p99": percentile_type7(ordered, 99), "max": ordered[-1],
    }


def points_distribution_with_price(values: Sequence[float]) -> dict[str, Any]:
    points = spread_distribution_points(values)
    return {
        "points": points,
        "price": {key: (value if key == "count" else None if value is None else value * POINT) for key, value in points.items()},
        "point": POINT,
    }


def stream_pre_cutoff_m1_bar_minimum_spreads(raw_path: Path) -> tuple[dict[int, float], list[float], int]:
    """Parse exact established MT5 schema/timezone semantics and retain pre-cutoff rows."""
    bars: dict[int, float] = {}
    values: list[float] = []
    count = 0
    with raw_path.open("rb") as handle:
        text = (line.decode("utf-8-sig") if number == 0 else line.decode("utf-8") for number, line in enumerate(handle))
        reader = csv.reader(text)
        header = next(reader, None)
        if tuple(header or ()) != XM_RAW_SCHEMA:
            raise ValueError("XM CSV schema mismatch")
        for row_number, row in enumerate(reader, 2):
            count += 1
            bar = _parse_row(row, row_number)
            validate_integral_spread_points(bar.spread_points)
            if bar.open_time < HISTORICAL_CUTOFF_MS:
                if bar.open_time in bars:
                    raise ValueError("duplicate normalized M1 timestamp")
                bars[bar.open_time] = bar.spread_points
                values.append(bar.spread_points)
    if count != EXPECTED_RAW_ROWS:
        raise ValueError("XM raw row count mismatch")
    return bars, values, count


def validate_integral_spread_points(value: float) -> None:
    """MqlRates.spread is a count of MT5 points, never a fractional point."""
    if not math.isfinite(value) or value < 0.0 or value != float(math.trunc(value)):
        raise ValueError("M1 MqlRates spread_points must be a nonnegative integer count of MT5 points")


def prior_minute_bar_minimum(bars_by_normalized_open_time: Mapping[int, float], execution_timestamp: int) -> float | None:
    """Return only t-60s; same-timestamp t is deliberately never considered."""
    if isinstance(execution_timestamp, bool) or not isinstance(execution_timestamp, int):
        raise TypeError("execution timestamp must be an epoch millisecond integer")
    return bars_by_normalized_open_time.get(execution_timestamp - M1_MS)


def endpoint_audit(ledger: Sequence[Mapping[str, Any]], bars_by_normalized_open_time: Mapping[int, float]) -> dict[str, Any]:
    """Match all entry/exit endpoints to prior M1 intervals without imputation."""
    matched: dict[str, list[float]] = {"entry": [], "exit": []}
    missing: dict[str, list[int]] = {"entry": [], "exit": []}
    for ordinal, row in enumerate(ledger, 1):
        for endpoint, field in (("entry", "entry_timestamp"), ("exit", "exit_timestamp")):
            timestamp = row.get(field)
            if isinstance(timestamp, bool) or not isinstance(timestamp, int):
                raise ValueError(f"closed trade {ordinal} lacks {field}")
            spread = prior_minute_bar_minimum(bars_by_normalized_open_time, timestamp)
            if spread is None:
                missing[endpoint].append(ordinal)
            else:
                matched[endpoint].append(spread)
    total_matched = len(matched["entry"]) + len(matched["exit"])
    result = {
        "rule": "Exact prior normalized M1 bar at execution_timestamp - 60000 only; same timestamp is the following interval and prohibited; gaps remain missing with no imputation.",
        "endpoints": len(ledger) * 2, "matched": total_matched, "missing": len(missing["entry"]) + len(missing["exit"]),
        "missing_trade_ordinals": missing,
        "combined_previous_bar_minimum": points_distribution_with_price(matched["entry"] + matched["exit"]),
        "entry_previous_bar_minimum": points_distribution_with_price(matched["entry"]),
        "exit_previous_bar_minimum": points_distribution_with_price(matched["exit"]),
        "classification": "DESCRIPTIVE INTERVAL EVIDENCE ONLY — NOT EXECUTION-TIME OR EXECUTABLE SPREAD",
    }
    if total_matched != EXPECTED_ENDPOINT_MATCHED or missing != EXPECTED_ENDPOINT_MISSING:
        raise ValueError("frozen endpoint no-lookahead audit mismatch")
    return result


def _closed_ledger_from_historical_result(path: Path) -> list[dict[str, Any]]:
    try:
        parsed = json.loads(path.read_bytes())
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise ValueError("frozen historical result is not valid JSON") from error
    ledger = parsed.get("closed_trade_ledger")
    if not isinstance(ledger, list) or len(ledger) != EXPECTED_CLOSED_TRADES:
        raise ValueError("frozen historical closed-trade ledger mismatch")
    return ledger


def frozen_s0_control(ledger: Sequence[Mapping[str, Any]]) -> dict[str, Any]:
    """Validate supplied frozen control values without applying a new cost."""
    gross = [float(row["r"]) for row in ledger]
    total = sum(gross)
    positive = sum(value for value in gross if value > 0.0)
    negative = -sum(value for value in gross if value < 0.0)
    wins = sum(value > 0.0 for value in gross)
    control = {
        "complete": True, "classification": "FROZEN FRICTIONLESS CONTROL ONLY",
        "closed_trades": len(gross), "gross_total_r": total, "total_cost_r": 0.0,
        "mean_cost_r_per_trade": 0.0, "median_cost_r": 0.0, "p75_cost_r": 0.0, "p90_cost_r": 0.0, "p95_cost_r": 0.0,
        "net_total_r": total, "gross_expectancy_r": total / len(gross), "net_expectancy_r": total / len(gross),
        "gross_profit_factor": positive / negative, "net_profit_factor": positive / negative,
        "gross_win_rate": wins / len(gross), "net_win_rate": wins / len(gross),
        "positive_r": positive, "negative_r_magnitude": negative, "net_positive_r": positive, "net_negative_r_magnitude": negative,
        "cost_components_r": {"spread": 0.0, "commission": 0.0, "swap": 0.0, "slippage": 0.0, "other": 0.0},
    }
    expected = (70.72001507737389, .08624392082606572, 1.1182300376072547, .24024390243902438, 668.8760886827708, 598.156073605397)
    actual = (control["gross_total_r"], control["gross_expectancy_r"], control["gross_profit_factor"], control["gross_win_rate"], positive, negative)
    if actual != expected:
        raise ValueError("frozen S0 population metrics mismatch")
    return control


def unavailable_scenario(name: str) -> dict[str, Any]:
    reason = "INCOMPLETE: account-specific executable spread, account type/currency and full symbol specification, commission evidence, historical swap evidence, and order/fill/slippage evidence are unavailable. M1 bar-minimum spread is descriptive only and cannot substitute."
    unavailable = {"closed_trades": None, "gross_total_r": None, "total_cost_r": None, "mean_cost_r_per_trade": None, "median_cost_r": None, "p75_cost_r": None, "p90_cost_r": None, "p95_cost_r": None, "net_total_r": None, "net_expectancy_r": None, "net_profit_factor": None, "net_win_rate": None, "net_positive_r": None, "net_negative_r_magnitude": None, "cost_utilization_ratio": None}
    return {
        "name": name, "complete": False, "unavailable_reason": reason,
        "aggregate": dict(unavailable),
        "cost_components_r": {"spread": None, "commission": None, "swap": None, "slippage": None, "other": None},
        "long_short": {"LONG": {"available": False, "reason": reason}, "SHORT": {"available": False, "reason": reason}},
        "temporal": {"available": False, "reason": reason}, "tail_preservation": {"available": False, "reason": reason},
        "drawdown_path_risk": {"available": False, "reason": reason},
    }


def cost_r_from_price_distance(component_cost_price: float, entry_price: float, stop_price: float) -> float:
    """Price-only R helper; account quantities and tick values are intentionally absent."""
    risk = abs(entry_price - stop_price)
    if risk == 0.0:
        raise ValueError("initial price risk must be nonzero")
    return component_cost_price / risk


def net_r_identity(gross_r: float, total_cost_r: float) -> float:
    return gross_r - total_cost_r


def build_waiting_result(*, protocol_guard: Mapping[str, Any], hashes: Mapping[str, str], raw_distribution: Mapping[str, Any], endpoint: Mapping[str, Any], s0: Mapping[str, Any]) -> dict[str, Any]:
    """Build the complete review-status result without calculating S1-S3 economics."""
    unavailable = {name: unavailable_scenario(name) for name in ("S1_observed_core", "S2_realistic_execution", "S3_conservative_stress")}
    missing = [
        "verified relevant-account type and account currency", "actual GOLD SYMBOL_CHART_MODE", "contract size/tick size/profit and loss tick values/calculation and currency fields", "volume min/max/step/limit and frozen trade quantity for account-unit values", "stops/freeze/fill/execution/order modes and sessions", "account-specific bid/ask ticks, quotes, or fills establishing executable spread", "relevant-account commission schedule or sanitized deal commission/fee history", "swap mode, long/short rates, rollover3days, historical DEAL_SWAP, and Islamic/swap-free status", "sanitized orders/deals with requested/decision/reference and executed prices for observed slippage",
    ]
    return {
        "schema_version": SCHEMA_VERSION,
        "phase_status": "XAU REAL BROKER COST CALIBRATION: WAITING FOR BROKER COST DATA",
        "classification": "XAU REAL BROKER COST CALIBRATION: WAITING FOR BROKER COST DATA",
        "canonical_starting_sha": "af02995705506ab1629f558e6fbdabe13d2d0785",
        "protocol": {"path": PROTOCOL_PATH, "sha256": EXPECTED_PROTOCOL_SHA256, "commit_sha": protocol_guard["protocol_commit_sha"]},
        "broker_identity": {"provider": "XM / XMGlobal", "server": "XMGlobal-MT5 18", "symbol": "GOLD", "instrument_path": r"Derivatives\Spot Metals\GOLD", "digits": 2, "point": POINT, "account_type": "UNKNOWN / UNVERIFIED", "account_currency": "UNKNOWN"},
        "artifact_hashes_verified": dict(hashes),
        "evidence_inventory": {
            "observed": "Frozen raw M1 spread field only: minimum spread per M1 bar in MT5 points.",
            "official": "MetaQuotes MqlRates/spread/symbol/deal/swap documentation and generic XM documentation; not account-specific values.",
            "modelled_not_executed": "S2 one verified tick per side and S3 0.5 matched M1 bar-minimum per side remain protocol formulas only.",
            "user_reported_commission": "zero explicit commission reported by user, UNVERIFIED without account specification or deal history.",
        },
        "known_specification": {"digits": 2, "point": POINT, "bar_spread_points_to_price": "spread_points * 0.01"},
        "unknown_specification_and_evidence": missing,
        "spread_evidence": {
            "classification": "OBSERVED — M1 BAR-MINIMUM SPREAD, NOT POINT-IN-TIME EXECUTABLE SPREAD",
            "limitation": "No bid/ask levels, quote time, or fill. The minimum is a lower-bound bar statistic and can understate execution spread; it is never applied as a broker cost.",
            "pre_cutoff_distribution": dict(raw_distribution), "endpoint_audit": dict(endpoint),
            "cutoff_rule": "Only normalized M1 timestamps strictly before 2026-03-01T23:00:00Z are included; no post-cutoff/directional-window data is used.",
        },
        "commission": {"status": "UNVERIFIED / UNAVAILABLE", "reason": "No relevant-account specification or sanitized DEAL_COMMISSION/DEAL_FEE history."},
        "swap_financing": {"status": "UNVERIFIED / UNAVAILABLE", "reason": "No historical swap mode/rates/rollover3days or sanitized DEAL_SWAP history; current rates are not backfilled."},
        "slippage": {"status": "UNOBSERVED", "reason": "No sanitized request/decision/reference and executed-price fills."},
        "r_conversion": {"price_only": "component_cost_price / abs(entry_price - stop_price)", "account": "component_cost_account / (abs(entry_price - stop_price) / tick_size * tick_value * lots)", "binding": "initial_risk_price_i = abs(entry_price_i - stop_price_i); frozen quantity plus verified monetary conversion are required for account-unit values.", "net_identity": "net_R = gross_R - total_cost_R"},
        "frozen_population": {"observed_setups": 2162, "eligible_setups": 1072, "opened_trades": 820, "closed_trades": 820, "censored": 0, "s0": dict(s0)},
        "scenarios": {"S0_frictionless_control": dict(s0), **unavailable},
        "break_even": {"gross_total_r": s0["gross_total_r"], "closed_trades": EXPECTED_CLOSED_TRADES, "break_even_r_per_trade": s0["gross_total_r"] / EXPECTED_CLOSED_TRADES, "realistic_cost_utilization_ratio": None, "reason": "No defensible realistic mean cost exists."},
        "decision": {"classification": "WAITING", "rule": "Essential evidence prevents defensible S2; no S1-S3 economics are manufactured."},
        "limitations": ["M1 bar-minimum spread is not executable spread.", "No account-specific broker cost calibration can be made.", "No directional filtering, optimization, or production conclusion follows."],
        "checks": {"protocol_before_inputs": True, "frozen_hashes_verified": True, "raw_full_byte_hash_read_disclosed": True, "post_cutoff_data_excluded_from_spread_statistics": True, "endpoint_no_lookahead": True},
        "confirmations": {"strategy_optimization": "NO", "tp_rr_sl_changes": "NO", "directional_rule_tested": "NO", "directional_confirmation_freeze_untouched": "YES", "production_behavior_changed": "NO", "btc_phase_7_4": "DEFERRED", "live_readiness_started": "NO"},
        "recommended_next_scientific_step": "Collect sanitized relevant-account/server GOLD symbol specification, bid/ask or fill evidence, commission/deal history, swap history, and requested-versus-executed fill data before a new protocol-governed calibration.",
    }


def result_json(result: Mapping[str, Any]) -> bytes:
    return (canonical_json(dict(result)) + "\n").encode("utf-8")


def result_sha256(result: Mapping[str, Any]) -> str:
    return sha256(result_json(result)).hexdigest()


def build_result_guarded(repo_root: Path, protocol_path: Path) -> dict[str, Any]:
    """The only input-opening orchestration path; guard is intentionally first."""
    guard = verify_committed_protocol_before_inputs(repo_root, protocol_path)
    hashes = required_hashes(repo_root)
    root = repo_root.resolve()
    ledger = _closed_ledger_from_historical_result(root / HISTORICAL_RESULT_PATH)
    s0 = frozen_s0_control(ledger)
    bars, values, _count = stream_pre_cutoff_m1_bar_minimum_spreads(root / FROZEN_RAW_PATH)
    endpoint = endpoint_audit(ledger, bars)
    return build_waiting_result(protocol_guard=guard, hashes=hashes, raw_distribution=points_distribution_with_price(values), endpoint=endpoint, s0=s0)


def write_result(result: Mapping[str, Any], path: Path) -> str:
    payload = result_json(result)
    digest = sha256(payload).hexdigest()
    if EXPECTED_RESULT_SHA256 != "TO_BE_PINNED" and digest != EXPECTED_RESULT_SHA256:
        raise ValueError("result bytes do not match the pinned SHA-256")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(payload)
    return digest
