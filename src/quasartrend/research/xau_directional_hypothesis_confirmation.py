"""Guard-only support for XAU directional-hypothesis confirmation.

This module deliberately has no parser for strategy events, no replay, and no
confirmatory economics executor.  It validates the separately committed
protocol, inventories source *identity/coverage metadata*, and provides small
deterministic ledger helpers for synthetic tests of the frozen definitions.
"""
from __future__ import annotations

import csv
from hashlib import sha256
import math
from pathlib import Path
import subprocess
from typing import Any, Callable, Mapping, Sequence, TypeVar

from .provenance import canonical_json


SCHEMA_VERSION = "xau-directional-hypothesis-confirmation-guard/v1"
PROTOCOL_PATH = "exports/xm/phase_xau_directional_hypothesis_confirmation_protocol.json"
EXPECTED_PROTOCOL_SHA256 = "c201170afb974b4299e06608556ee49acfd2b11c950e0076a841e8d4412629ef"
PROTOCOL_COMMIT_SHA = "897e20265cafe69a82d405ae65dc67f9f2f61125"
DISCOVERY_PROTOCOL_PATH = "exports/xm/phase_xau_directional_edge_attribution_protocol.json"
DISCOVERY_RESULT_PATH = "exports/xm/phase_xau_directional_edge_attribution.json"
DISCOVERY_PROTOCOL_SHA256 = "ea9820eb240754aef2ea413532b0c49dea7b3538c90af11f65adcb422e0f555e"
DISCOVERY_RESULT_SHA256 = "747402a144eaab959b7dc2d6432c0c894f5bc2104833bcd98851cc1188030e3d"
FROZEN_XM_SOURCE_PATH = "exports/xm/XM_GOLD_M1_raw.csv"
FROZEN_XM_SOURCE_SHA256 = "3817558149502888bcee711fb803e6085c5d48cbf36d33901d85ac85c0c0d81a"
FROZEN_XM_SOURCE_ROWS = 1_000_000
FROZEN_XM_LAST_NORMALIZED_MS = 1_787_950_620_000
CONFIRMATION_START_MS = 1_787_950_680_000
CONFIRMATION_END_EXCLUSIVE_MS = 1_803_942_000_000
LAST_REQUIRED_CONFIRMATION_BAR_MS = CONFIRMATION_END_EXCLUSIVE_MS - 60_000
PERSISTENCE_Q1_HOURS = 39.75
PERSISTENCE_Q2_HOURS = 116.5
MINIMUM_CELL_CLOSED_TRADES = 30
SMALL_SAMPLE_LABEL = "SMALL SAMPLE / NOT CONFIRMATORY"
WAITING_PHASE_STATUS = "XAU DIRECTIONAL HYPOTHESIS CONFIRMATION: WAITING FOR UNTOUCHED DATA"

KNOWN_DISCOVERY_HASHES = frozenset((
    DISCOVERY_PROTOCOL_SHA256,
    DISCOVERY_RESULT_SHA256,
    FROZEN_XM_SOURCE_SHA256,
))
_T = TypeVar("_T")


def _sha256_path(path: Path) -> str:
    digest = sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def confirmation_json(value: Mapping[str, Any]) -> bytes:
    """The protocol's required canonical JSON encoding."""
    return (canonical_json(dict(value)) + "\n").encode("utf-8")


def protocol_sha256(protocol: Mapping[str, Any]) -> str:
    return sha256(confirmation_json(protocol)).hexdigest()


def verify_committed_protocol(path: Path) -> dict[str, Any]:
    """Require the exact, canonical, separately committed protocol bytes."""
    raw = path.read_bytes()
    if sha256(raw).hexdigest() != EXPECTED_PROTOCOL_SHA256:
        raise ValueError("confirmation protocol hash mismatch")
    try:
        import json
        protocol = json.loads(raw)
    except (UnicodeDecodeError, ValueError) as error:
        raise ValueError("confirmation protocol is not valid JSON") from error
    if not isinstance(protocol, dict) or confirmation_json(protocol) != raw:
        raise ValueError("confirmation protocol bytes are not canonical")
    if protocol.get("schema_version") != "xau-directional-hypothesis-confirmation-protocol/v1":
        raise ValueError("confirmation protocol schema mismatch")
    return protocol


def verify_protocol_commit(repo_root: Path, protocol_path: Path) -> dict[str, Any]:
    """Require the protocol's frozen commit and exact blob before any results.

    This is intentionally a read-only Git provenance check.  It proves that
    the exact locked bytes existed in commit ``897e202`` and that the current
    HEAD descends from that commit; it does not evaluate any market data.
    """
    root = repo_root.resolve()
    expected_path = (root / PROTOCOL_PATH).resolve()
    if protocol_path.resolve() != expected_path:
        raise ValueError("confirmation protocol path must be the committed protocol path")
    def git_bytes(*args: str) -> bytes:
        return subprocess.run(("git", *args), cwd=root, check=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE).stdout
    try:
        git_bytes("cat-file", "-e", f"{PROTOCOL_COMMIT_SHA}^{{commit}}")
        ancestor = subprocess.run(("git", "merge-base", "--is-ancestor", PROTOCOL_COMMIT_SHA, "HEAD"), cwd=root)
        if ancestor.returncode != 0:
            raise ValueError("protocol commit is not an ancestor of HEAD")
        committed_bytes = git_bytes("show", f"{PROTOCOL_COMMIT_SHA}:{PROTOCOL_PATH}")
    except subprocess.CalledProcessError as error:
        raise ValueError("protocol commit or committed protocol blob is unavailable") from error
    if sha256(committed_bytes).hexdigest() != EXPECTED_PROTOCOL_SHA256:
        raise ValueError("committed protocol blob hash mismatch")
    if protocol_path.read_bytes() != committed_bytes:
        raise ValueError("working protocol bytes differ from committed protocol")
    protocol = verify_committed_protocol(protocol_path)
    return {"commit_sha": PROTOCOL_COMMIT_SHA, "protocol_sha256": protocol_sha256(protocol)}


def verify_discovery_hashes(repo_root: Path) -> dict[str, str]:
    """Verify discovery artifacts by identity only; never deserialize economics."""
    root = repo_root.resolve()
    actual = {
        DISCOVERY_PROTOCOL_PATH: _sha256_path(root / DISCOVERY_PROTOCOL_PATH),
        DISCOVERY_RESULT_PATH: _sha256_path(root / DISCOVERY_RESULT_PATH),
    }
    expected = {
        DISCOVERY_PROTOCOL_PATH: DISCOVERY_PROTOCOL_SHA256,
        DISCOVERY_RESULT_PATH: DISCOVERY_RESULT_SHA256,
    }
    if actual != expected:
        raise ValueError("frozen discovery artifact hash mismatch")
    return actual


def frozen_bias_persistence_hours(
    setup_origin_timestamp: int, bias_activation_timestamp: int | None,
) -> float | None:
    """Frozen `(setup-origin post_state - bias activation) / 3600000` definition."""
    if isinstance(setup_origin_timestamp, bool) or not isinstance(setup_origin_timestamp, int):
        raise TypeError("setup origin timestamp must be an integer epoch ms")
    if bias_activation_timestamp is None:
        return None
    if isinstance(bias_activation_timestamp, bool) or not isinstance(bias_activation_timestamp, int):
        raise TypeError("bias activation timestamp must be an integer epoch ms or None")
    value = (setup_origin_timestamp - bias_activation_timestamp) / 3_600_000
    if not math.isfinite(value) or value < 0.0:
        raise ValueError("bias persistence must be finite and nonnegative")
    return value


def frozen_persistence_bucket(value: float | None) -> str:
    """LOW <= 39.75; MEDIUM <= 116.5; HIGH above; otherwise MISSING."""
    if value is None or not isinstance(value, (int, float)) or isinstance(value, bool) or not math.isfinite(value) or value < 0:
        return "MISSING"
    if value <= PERSISTENCE_Q1_HOURS:
        return "LOW"
    if value <= PERSISTENCE_Q2_HOURS:
        return "MEDIUM"
    return "HIGH"


def is_known_discovery_observation(*, source_sha256: str, normalized_timestamp_ms: int) -> bool:
    """All frozen-source timestamps and all known artifact identities are contaminated."""
    return source_sha256 in KNOWN_DISCOVERY_HASHES or normalized_timestamp_ms <= FROZEN_XM_LAST_NORMALIZED_MS


def admit_untouched_source(candidate: Mapping[str, Any]) -> dict[str, Any]:
    """Apply the predeclared source-admission gate without opening any economics.

    Candidate metadata must already be normalized by the XM-compatible source
    procedure.  A candidate is not admitted merely because it is new: its
    provider identity and untouched-data attestations must also be present.
    """
    required = ("sha256", "row_count", "normalized_first_epoch_ms", "normalized_last_epoch_ms", "provider", "server", "instrument", "digits", "point", "previously_inspected_directional_or_persistence_economics", "previously_used_for_discovery", "all_rows_strictly_after_discovery_boundary", "timestamp_provider_semantics_verified", "warmup_verified", "fixed_confirmation_window_complete", "provider_gaps_verified", "acquired_after_protocol_commit")
    missing = [name for name in required if name not in candidate]
    if missing:
        raise ValueError("candidate metadata missing: " + ", ".join(missing))
    reasons: list[str] = []
    source_hash = candidate["sha256"]
    first = candidate["normalized_first_epoch_ms"]
    last = candidate["normalized_last_epoch_ms"]
    rows = candidate["row_count"]
    if not isinstance(source_hash, str) or len(source_hash) != 64 or any(char not in "0123456789abcdefABCDEF" for char in source_hash):
        reasons.append("invalid raw SHA-256")
    else:
        source_hash = source_hash.lower()
    if isinstance(rows, bool) or not isinstance(rows, int) or rows <= 0:
        reasons.append("row count must be positive")
    if any(isinstance(value, bool) or not isinstance(value, int) for value in (first, last)) or first > last:
        reasons.append("normalized coverage is invalid")
    else:
        if first < CONFIRMATION_START_MS:
            reasons.append("coverage starts before the fixed confirmation boundary")
        if last < LAST_REQUIRED_CONFIRMATION_BAR_MS:
            reasons.append("coverage does not reach the fixed confirmation-window end")
        if source_hash in KNOWN_DISCOVERY_HASHES:
            reasons.append("source hash is a known discovery artifact")
        if first <= FROZEN_XM_LAST_NORMALIZED_MS:
            reasons.append("source includes frozen discovery-used timestamps")
    if (candidate["provider"], candidate["server"], candidate["instrument"], candidate["digits"], candidate["point"]) != ("XM", "XMGlobal-MT5 18", "GOLD", 2, 0.01):
        reasons.append("provider identity is not the predeclared XM GOLD identity")
    if candidate["previously_inspected_directional_or_persistence_economics"] is not False:
        reasons.append("candidate lacks an untouched-economics attestation")
    if candidate["previously_used_for_discovery"] is not False:
        reasons.append("candidate lacks a not-used-for-discovery attestation")
    if candidate["all_rows_strictly_after_discovery_boundary"] is not True:
        reasons.append("candidate lacks strict all-row post-discovery coverage verification")
    if candidate["timestamp_provider_semantics_verified"] is not True:
        reasons.append("candidate lacks timestamp/provider-semantics verification")
    if candidate["warmup_verified"] is not True:
        reasons.append("candidate lacks required warm-up verification")
    if candidate["fixed_confirmation_window_complete"] is not True:
        reasons.append("candidate lacks a complete fixed-window attestation")
    if candidate["provider_gaps_verified"] is not True:
        reasons.append("candidate lacks provider-gap verification")
    if candidate["acquired_after_protocol_commit"] is not True:
        reasons.append("candidate lacks post-protocol acquisition attestation")
    return {"admitted": not reasons, "reasons": reasons, "confirmation_start_ms": CONFIRMATION_START_MS}


def _finite_r(row: Mapping[str, Any]) -> float:
    value = row.get("r")
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value):
        raise ValueError("closed trade R must be finite")
    return float(value)


def closed_trade_metrics(rows: Sequence[Mapping[str, Any]]) -> dict[str, Any]:
    """Fixed descriptive metrics for already-built synthetic closed-trade rows."""
    values = [_finite_r(row) for row in rows]
    count = len(values)
    total = sum(values)
    gains = sum(value for value in values if value > 0.0)
    losses = -sum(value for value in values if value < 0.0)
    return {
        "count": count,
        "closed_trades": count,
        "total_r": total,
        "expectancy_r": None if count == 0 else total / count,
        "profit_factor": None if losses == 0.0 else gains / losses,
        "win_rate": None if count == 0 else sum(value > 0.0 for value in values) / count,
        "stop_rate": None if count == 0 else sum(row.get("stop_hit") is True for row in rows) / count,
    }


def minimum_cell_label(metrics: Mapping[str, Any]) -> str | None:
    count = metrics.get("closed_trades", metrics.get("count"))
    if isinstance(count, bool) or not isinstance(count, int):
        raise ValueError("metrics require an integer closed-trade count")
    return SMALL_SAMPLE_LABEL if count < MINIMUM_CELL_CLOSED_TRADES else None


def aggregate_direction_metrics(closed_rows: Sequence[Mapping[str, Any]]) -> dict[str, dict[str, Any]]:
    result: dict[str, dict[str, Any]] = {}
    for direction in ("LONG", "SHORT"):
        rows = [row for row in closed_rows if str(row.get("direction", "")).upper() == direction]
        metrics = closed_trade_metrics(rows)
        metrics["sample_warning"] = minimum_cell_label(metrics)
        result[direction] = metrics
    return result


def aggregate_persistence_metrics(closed_rows: Sequence[Mapping[str, Any]]) -> dict[str, dict[str, dict[str, Any]]]:
    result: dict[str, dict[str, dict[str, Any]]] = {}
    for bucket in ("LOW", "MEDIUM", "HIGH", "MISSING"):
        result[bucket] = {}
        for direction in ("LONG", "SHORT"):
            rows = [row for row in closed_rows if str(row.get("direction", "")).upper() == direction and str(row.get("persistence_bucket", "")).upper() == bucket]
            metrics = closed_trade_metrics(rows)
            metrics["sample_warning"] = minimum_cell_label(metrics)
            result[bucket][direction] = metrics
    return result


def construct_confirmation_population(
    setup_rows: Sequence[Mapping[str, Any]], trade_rows: Sequence[Mapping[str, Any]],
) -> dict[str, Any]:
    """Apply only fixed inclusion/censoring rules to a caller-supplied ledger.

    This is intentionally not a strategy replay.  It makes the report shape
    testable with synthetic records while preserving the protocol's exact
    setup-origin and close-cutoff semantics.
    """
    observed_setups: dict[str, Mapping[str, Any]] = {}
    included_setups: dict[str, Mapping[str, Any]] = {}
    for row in setup_rows:
        setup_id = row.get("setup_id")
        origin = row.get("setup_origin_timestamp")
        if not isinstance(setup_id, str) or not isinstance(origin, int) or isinstance(origin, bool):
            raise ValueError("setup rows require string setup_id and integer setup_origin_timestamp")
        if CONFIRMATION_START_MS <= origin < CONFIRMATION_END_EXCLUSIVE_MS:
            if setup_id in observed_setups:
                raise ValueError("duplicate observed setup_id")
            observed_setups[setup_id] = row
            if row.get("eligible", True) is True:
                included_setups[setup_id] = row
    opened: list[dict[str, Any]] = []
    closed: list[dict[str, Any]] = []
    censored: list[dict[str, Any]] = []
    for trade in trade_rows:
        setup_id = trade.get("setup_id")
        if setup_id not in included_setups:
            continue
        direction = str(trade.get("direction", "")).upper()
        if direction not in ("LONG", "SHORT"):
            raise ValueError("trade direction must be LONG or SHORT")
        row = dict(trade)
        row["direction"] = direction
        opened.append(row)
        exit_timestamp = row.get("exit_timestamp")
        if isinstance(exit_timestamp, int) and not isinstance(exit_timestamp, bool) and exit_timestamp < CONFIRMATION_END_EXCLUSIVE_MS:
            origin = included_setups[setup_id]["setup_origin_timestamp"]
            if exit_timestamp < CONFIRMATION_START_MS or exit_timestamp < origin:
                raise ValueError("closed trade exit precedes its included setup origin or confirmation start")
            row["outcome"] = "closed"
            _finite_r(row)
            closed.append(row)
        else:
            row["outcome"] = "censored"
            censored.append(row)
    directions = aggregate_direction_metrics(closed)
    persistence = aggregate_persistence_metrics(closed)
    overall = closed_trade_metrics(closed)
    overall.update({
        "observed_setups": len(observed_setups), "eligible_setups": len(included_setups),
        "opened": len(opened), "closed": len(closed), "censored": len(censored),
        "sample_warning": minimum_cell_label(overall),
    })
    return {
        "overall": overall,
        "by_direction": directions,
        "by_persistence_and_direction": persistence,
        "directional_differences": {
            "long_minus_short_expectancy_r": None if directions["LONG"]["expectancy_r"] is None or directions["SHORT"]["expectancy_r"] is None else directions["LONG"]["expectancy_r"] - directions["SHORT"]["expectancy_r"],
            "long_minus_short_total_r": directions["LONG"]["total_r"] - directions["SHORT"]["total_r"],
        },
        "included_setup_ids": sorted(included_setups),
        "opened_trade_ledger": opened,
        "closed_trade_ledger": closed,
        "censored_trade_ledger": censored,
    }


def inventory_source_identity(path: Path) -> dict[str, Any]:
    """Read only CSV timestamp/row metadata and raw bytes identity, never OHLC."""
    digest = _sha256_path(path)
    with path.open("r", encoding="utf-8-sig", newline="") as handle:
        reader = csv.DictReader(handle)
        fieldnames = reader.fieldnames or []
        timestamp_column = next((name for name in ("time_epoch", "time", "timestamp") if name in fieldnames), None)
        if timestamp_column is None:
            raise ValueError(f"{path} lacks time_epoch/time/timestamp metadata")
        first: int | None = None
        last: int | None = None
        rows = 0
        for row in reader:
            try:
                timestamp = int(row[timestamp_column])
            except (KeyError, TypeError, ValueError) as error:
                raise ValueError(f"{path} lacks integer {timestamp_column} metadata") from error
            if first is None:
                first = timestamp
            last = timestamp
            rows += 1
    return {"path": str(path), "sha256": digest, "row_count": rows, "timestamp_column": timestamp_column, "first_labeled_timestamp": first, "last_labeled_timestamp": last}


def verify_frozen_xm_source_identity(path: Path) -> dict[str, Any]:
    """Verify only the frozen source identity and declared coverage metadata."""
    inventory = inventory_source_identity(path)
    if (inventory["sha256"], inventory["row_count"]) != (FROZEN_XM_SOURCE_SHA256, FROZEN_XM_SOURCE_ROWS):
        raise ValueError("frozen XM source identity mismatch")
    return {
        "sha256": FROZEN_XM_SOURCE_SHA256,
        "row_count": FROZEN_XM_SOURCE_ROWS,
        "normalized_last_epoch_ms": FROZEN_XM_LAST_NORMALIZED_MS,
    }


def build_waiting_status(repo_root: Path) -> dict[str, Any]:
    """Create a deterministic, economics-free status payload for Sol to write."""
    root = repo_root.resolve()
    protocol = verify_committed_protocol(root / PROTOCOL_PATH)
    verify_protocol_commit(root, root / PROTOCOL_PATH)
    verify_discovery_hashes(root)
    sources = []
    for relative, status, reason in (
        (FROZEN_XM_SOURCE_PATH, "CONTAMINATED", "exact frozen discovery-used XM extraction"),
        ("exports/xauusd_pending/XAUUSD_15m.csv", "CONTAMINATED", "previously inspected TradingView compatibility input"),
        ("exports/xauusd_pending/XAUUSD_4h.csv", "CONTAMINATED", "previously inspected TradingView compatibility input"),
    ):
        item = inventory_source_identity(root / relative)
        item["path"] = relative
        item.update({"identity_status": status, "admission": "REJECTED", "reason": reason})
        if relative == FROZEN_XM_SOURCE_PATH:
            item["frozen_identity"] = verify_frozen_xm_source_identity(root / relative)
        sources.append(item)
    return {
        "schema_version": SCHEMA_VERSION,
        "phase_status": WAITING_PHASE_STATUS,
        "protocol_path": PROTOCOL_PATH,
        "protocol_sha256": EXPECTED_PROTOCOL_SHA256,
        "protocol_commit_sha": PROTOCOL_COMMIT_SHA,
        "protocol": {"path": PROTOCOL_PATH, "sha256": EXPECTED_PROTOCOL_SHA256, "commit_sha": PROTOCOL_COMMIT_SHA, "schema_version": protocol["schema_version"]},
        "discovery_freeze": {"protocol_path": DISCOVERY_PROTOCOL_PATH, "protocol_sha256": DISCOVERY_PROTOCOL_SHA256, "result_path": DISCOVERY_RESULT_PATH, "result_sha256": DISCOVERY_RESULT_SHA256},
        "hypotheses": {"H1": "WAITING FOR UNTOUCHED DATA", "H2": "WAITING FOR UNTOUCHED DATA"},
        "sample_adequacy": "UNAVAILABLE; no admitted confirmation population",
        "limitations": ["No genuinely untouched, admitted XAU/GOLD confirmation source is available.", "No confirmatory economics were computed."],
        "available_xau_source_inventory": sources,
        "wrong_instrument_exclusions": [{"category": "BTC golden CSVs", "admission": "REJECTED", "reason": "wrong instrument; not XAU/GOLD confirmation data"}],
        "future_admission_boundary": {"normalized_timestamp_strictly_after_ms": FROZEN_XM_LAST_NORMALIZED_MS, "first_confirmation_setup_origin_ms": CONFIRMATION_START_MS, "confirmation_end_exclusive_ms": CONFIRMATION_END_EXCLUSIVE_MS, "required_identity": {"provider": "XM", "server": "XMGlobal-MT5 18", "instrument": "GOLD", "digits": 2, "point": 0.01}},
        "confirmatory_economics_executed": "NO",
        "directional_rule_tested": "NO",
        "xau_specific_parameter_tuning": "NO",
        "production_strategy_change": "NO",
        "production_market_change": "NO",
        "long_only_or_short_disable_tested": "NO",
        "broker_cost_calibration": "NO",
        "btc_phase_7_4": "DEFERRED",
    }


def write_waiting_status(status: Mapping[str, Any], path: Path) -> str:
    payload = confirmation_json(status)
    if path.exists():
        raise FileExistsError("status artifact already exists")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(payload)
    return sha256(payload).hexdigest()


def execute_confirmatory_economics(*_args: Any, **_kwargs: Any) -> None:
    """This protocol phase is intentionally waiting; execution is prohibited."""
    raise RuntimeError("WAITING FOR UNTOUCHED DATA: confirmatory economics execution is prohibited")


def protocol_before_results(protocol_path: Path, evaluator: Callable[[dict[str, Any]], _T], *, repo_root: Path = Path(".")) -> _T:
    """Validate the exact lock before an evaluator; callers still need admission.

    The guard is useful for synthetic tests only.  Production confirmation
    execution remains prohibited by :func:`execute_confirmatory_economics`.
    """
    verify_protocol_commit(repo_root, protocol_path)
    return evaluator(verify_committed_protocol(protocol_path))
