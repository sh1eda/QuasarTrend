"""Stage A-only XAU directional-edge attribution protocol lock.

This boundary intentionally derives only pre-entry context.  It never builds a
trade ledger, calls the backtest engine, or reads realized R/economics.
"""
from __future__ import annotations

from collections import Counter
from hashlib import sha256
import json
import math
from pathlib import Path
import subprocess
from typing import Any, Mapping, Sequence

from quasartrend.replay import Timeframe
from quasartrend.strategy import Direction, EventType

from . import xm_gold_historical_validation as historical
from .provenance import canonical_json


SCHEMA_VERSION = "xau-directional-edge-attribution-protocol/v1"
CANONICAL_STARTING_SHA = "81430f834e8746ff97bf82e8122281c3a0d1dd04"
CANONICAL_TAG = "xm-gold-historical-validation-conditional"
CANONICAL_TAG_OBJECT = "4d0faa6d3654533d500f3c0eca844f174354678e"
RAW_SOURCE_PATH = "exports/xm/XM_GOLD_M1_raw.csv"
HISTORICAL_PROTOCOL_PATH = "exports/xm/phase_xm_gold_historical_validation_protocol.json"
HISTORICAL_RESULT_PATH = "exports/xm/phase_xm_gold_historical_validation.json"
COMPATIBILITY_RESULT_PATH = "exports/xm/phase_xm_gold_compatibility.json"
EXPECTED_SOURCE_SHA256 = {
    RAW_SOURCE_PATH: "3817558149502888bcee711fb803e6085c5d48cbf36d33901d85ac85c0c0d81a",
    HISTORICAL_PROTOCOL_PATH: "2c690292b3f2a53c0295cb153cf0721044ba3d24c55b56359e32f030c7ee7870",
    HISTORICAL_RESULT_PATH: "5e81a9ad805496e0a3d8578821485f9469998cf9d2f37bff2b9b5e4be1670d0d",
    COMPATIBILITY_RESULT_PATH: "d9d23efb0a6343d83c06c0fb79d67d245528e4749e834f8f26b06c7bf3c09176",
}
HISTORICAL_CUTOFF_UTC = "2026-03-01T23:00:00Z"
HISTORICAL_CUTOFF_MS = 1_772_406_000_000
FIRST_ELIGIBLE_TIMESTAMP_MS = 1_710_972_900_000
EXPECTED_POPULATION = {
    "observed_setups": 2162,
    "eligible_setups": 1072,
    "opened_trades": 820,
    "closed_trades": 820,
    "censored_trades": 0,
}
TERTILE_QUANTILES = (1 / 3, 2 / 3)
M15_DURATION_MS = 900_000
EXPECTED_CONTEXT_LOCK = {
    "population": dict(EXPECTED_POPULATION),
    "first_eligible_timestamp_ms": FIRST_ELIGIBLE_TIMESTAMP_MS,
    "bias_persistence_hours": {"q1": 39.75, "q2": 116.5, "nonmissing": 1072, "counts": {"low": 360, "medium": 358, "high": 354, "missing": 0}},
    "atr_at_setup": {"q1": 3.2163436225173365, "q2": 5.297452408665118, "nonmissing": 1072, "counts": {"low": 358, "medium": 357, "high": 357, "missing": 0}},
    "broad_market_direction_32": {"counts": {"down": 340, "flat": 0, "up": 411, "missing": 321}},
}
# This literal is updated only after canonical Stage A context serialization.
EXPECTED_PROTOCOL_SHA256 = "ea9820eb240754aef2ea413532b0c49dea7b3538c90af11f65adcb422e0f555e"


def _hash(path: Path) -> str:
    digest = sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _tracked_git_clean(repo_root: Path) -> bool:
    return all(
        subprocess.run(("git", "diff", *args, "--quiet"), cwd=repo_root).returncode == 0
        for args in ((), ("--cached",))
    )


def verify_stage_a_identities(repo_root: Path) -> dict[str, Any]:
    """Fail closed before raw replay/context extraction."""
    root = repo_root.resolve()
    def git(*args: str) -> str:
        return subprocess.run(("git", *args), cwd=root, check=True, text=True,
                              stdout=subprocess.PIPE, stderr=subprocess.PIPE).stdout.strip()
    head = git("rev-parse", "HEAD")
    tag_object = git("rev-parse", CANONICAL_TAG)
    peeled = git("rev-parse", f"{CANONICAL_TAG}^{{}}")
    branch = git("branch", "--show-current")
    local_main = git("rev-parse", "main")
    origin_main = git("rev-parse", "origin/main")
    if (head, branch, local_main, origin_main, tag_object, peeled) != (
        CANONICAL_STARTING_SHA, "main", CANONICAL_STARTING_SHA,
        CANONICAL_STARTING_SHA, CANONICAL_TAG_OBJECT, CANONICAL_STARTING_SHA,
    ):
        raise ValueError("canonical Git identity mismatch")
    if not _tracked_git_clean(root):
        raise ValueError("tracked index or worktree is not clean")
    actual = {path: _hash(root / path) for path in EXPECTED_SOURCE_SHA256}
    if actual != EXPECTED_SOURCE_SHA256:
        raise ValueError("frozen source artifact identity mismatch")
    return {"head": head, "tag_object": tag_object, "peeled_target": peeled,
            "source_sha256": actual}


def type_7_quantile(values: Sequence[float], q: float) -> float:
    """R-7 linear-interpolated quantile, deliberately independent of outcomes."""
    if not values or not math.isfinite(q) or not 0.0 <= q <= 1.0:
        raise ValueError("type-7 quantile requires non-empty values and q in [0, 1]")
    ordered = sorted(float(value) for value in values)
    if any(not math.isfinite(value) for value in ordered):
        raise ValueError("type-7 quantile values must be finite")
    position = (len(ordered) - 1) * q
    lower = math.floor(position)
    upper = math.ceil(position)
    return ordered[lower] if lower == upper else ordered[lower] + (position - lower) * (ordered[upper] - ordered[lower])


def tertile_bin(value: float | None, q1: float, q2: float) -> str:
    if value is None or not math.isfinite(value):
        return "missing"
    if value <= q1:
        return "low"
    if value <= q2:
        return "medium"
    return "high"


def _direction_32(closes: Sequence[tuple[int, float]]) -> str:
    if len(closes) < 33:
        return "missing"
    window = closes[-33:]
    if any(right[0] - left[0] != M15_DURATION_MS for left, right in zip(window, window[1:])):
        return "missing"
    change = window[-1][1] - window[0][1]
    return "up" if change > 0 else "down" if change < 0 else "flat"


def extract_setup_contexts(replay: Any) -> tuple[list[dict[str, Any]], dict[str, int]]:
    """Collect setup-origin context and final path classes without outcomes."""
    contexts: dict[int, dict[str, Any]] = {}
    paths: dict[Direction, int] = {}
    closes: list[tuple[int, float]] = []
    opened = closed = 0
    for trace in replay.traces:
        if trace.source_bar.timeframe is not Timeframe.MINUTES_15:
            continue
        bar = trace.strategy_bar
        if bar is None:
            raise ValueError("M15 trace lacks StrategyBar")
        closes.append((bar.timestamp, bar.close))
        for event in trace.events:
            if event.type is EventType.HEMA_FLIP_DETECTED and event.side is not None:
                activation = trace.post_state.bias_activation_timestamp
                persistence = None if activation is None else (bar.timestamp - activation) / 3_600_000
                if persistence is not None and (not math.isfinite(persistence) or persistence < 0):
                    raise ValueError("invalid bias persistence context")
                atr = bar.atr if bar.atr is not None and math.isfinite(bar.atr) and bar.atr > 0 else None
                contexts[event.timestamp] = {
                    "timestamp": event.timestamp, "direction": event.side.value,
                    "bias_persistence_hours": persistence, "atr_at_setup": atr,
                    "broad_market_direction_32": _direction_32(closes), "path": "rejected",
                }
            elif event.type is EventType.SETUP_ARMED and event.side is not None:
                origin = trace.post_state.pending_flip_timestamp
                if origin is None or origin not in contexts:
                    raise ValueError("armed setup lacks HEMA setup-origin context")
                contexts[origin]["path"] = "armed_pending"
                paths[event.side] = origin
            elif event.type is EventType.SETUP_CANCELLED and event.side is not None:
                origin = paths.pop(event.side, None)
                if origin is not None:
                    contexts[origin]["path"] = "armed_then_cancelled"
            elif event.type is EventType.TRADE_OPENED:
                trade = trace.post_state.trade
                if trade is None or event.trade_id != trade.trade_id:
                    raise ValueError("trade open event/state mismatch")
                origin = trade.setup_origin_timestamp
                if origin not in contexts:
                    raise ValueError("trade open lacks HEMA setup-origin context")
                contexts[origin]["path"] = "armed_then_opened" if contexts[origin]["path"] == "armed_pending" else "immediate_open"
                paths.pop(trade.side, None)
                opened += 1
            elif event.type is EventType.TRADE_CLOSED:
                closed += 1
    output = [contexts[timestamp] for timestamp in sorted(contexts)]
    eligible = [row for row in output if row["path"] in {"immediate_open", "armed_then_opened", "armed_then_cancelled"}]
    return output, {"observed_setups": len(output), "eligible_setups": len(eligible),
                    "opened_trades": opened, "closed_trades": closed,
                    "censored_trades": opened - closed}


def build_context_lock(*, repo_root: Path, xm_m1_source: Path) -> dict[str, Any]:
    """Replay frozen inputs and serialize only context boundaries/counts."""
    root = repo_root.resolve()
    canonical_source = (root / RAW_SOURCE_PATH).resolve()
    if xm_m1_source.resolve() != canonical_source:
        raise ValueError("Stage A requires the canonical raw XM source path")
    verify_stage_a_identities(root)
    if _hash(canonical_source) != EXPECTED_SOURCE_SHA256[RAW_SOURCE_PATH]:
        raise ValueError("canonical raw XM source identity mismatch")
    raw = historical._load_full_xm_m1(canonical_source)
    ltf, htf, _excluded, _audit = historical._replay_inputs(raw)
    replay, warmup = historical._run_warmed_replay(ltf, htf)
    contexts, population = extract_setup_contexts(replay)
    if warmup["first_strategy_eligible_timestamp"] != FIRST_ELIGIBLE_TIMESTAMP_MS:
        raise ValueError("first strategy-eligible timestamp identity mismatch")
    if population != EXPECTED_POPULATION:
        raise ValueError("Stage A structural population identity mismatch")
    eligible = [row for row in contexts if row["path"] in {"immediate_open", "armed_then_opened", "armed_then_cancelled"}]
    def continuous(name: str) -> dict[str, Any]:
        values = [float(row[name]) for row in eligible if row[name] is not None]
        q1, q2 = (type_7_quantile(values, quantile) for quantile in TERTILE_QUANTILES)
        counts = Counter(tertile_bin(row[name], q1, q2) for row in eligible)
        return {"q1": q1, "q2": q2, "nonmissing": len(values),
                "counts": {bucket: counts[bucket] for bucket in ("low", "medium", "high", "missing")}}
    direction_counts = Counter(str(row["broad_market_direction_32"]) for row in eligible)
    lock = {"population": population, "first_eligible_timestamp_ms": warmup["first_strategy_eligible_timestamp"],
            "bias_persistence_hours": continuous("bias_persistence_hours"),
            "atr_at_setup": continuous("atr_at_setup"),
            "broad_market_direction_32": {"counts": {bucket: direction_counts[bucket] for bucket in ("down", "flat", "up", "missing")}}}
    if lock != EXPECTED_CONTEXT_LOCK:
        raise ValueError("Stage A context-lock boundary/count identity mismatch")
    return lock


def build_xau_directional_edge_attribution_protocol(context_lock: Mapping[str, Any]) -> dict[str, Any]:
    """Build the exhaustive, result-independent Stage A protocol."""
    if dict(context_lock.get("population", {})) != EXPECTED_POPULATION or context_lock.get("first_eligible_timestamp_ms") != FIRST_ELIGIBLE_TIMESTAMP_MS:
        raise ValueError("context lock structural identity mismatch")
    for name in ("bias_persistence_hours", "atr_at_setup"):
        family = context_lock.get(name, {})
        if not isinstance(family, Mapping) or not family.get("nonmissing") or family["q1"] > family["q2"]:
            raise ValueError("invalid continuous context lock")
    return {
        "schema_version": SCHEMA_VERSION,
        "stage": "A — attribution protocol freeze only",
        "decision_authority": "Sol/main only; no phase approval is granted",
        "descriptive_non_optimizing_declaration": "Descriptive attribution only. No strategy, parameter, direction, entry, exit, stop, broker-cost, or filter change is authorized.",
        "canonical_starting_state": {"sha": CANONICAL_STARTING_SHA, "annotated_tag": CANONICAL_TAG, "annotated_tag_object": CANONICAL_TAG_OBJECT, "peeled_target": CANONICAL_STARTING_SHA},
        "frozen_inputs": {"source_sha256": dict(EXPECTED_SOURCE_SHA256), "historical_cutoff": {"strictly_before_utc": HISTORICAL_CUTOFF_UTC, "epoch_ms": HISTORICAL_CUTOFF_MS}, "first_eligible_timestamp": {"epoch_ms": FIRST_ELIGIBLE_TIMESTAMP_MS, "utc": "2024-03-20T22:15:00Z"}, "context_lock": dict(context_lock)},
        "population": {**EXPECTED_POPULATION, "observed_definition": "all HEMA_FLIP_DETECTED setup origins after frozen warm-up", "eligible_definition": "only final paths immediate_open, armed_then_opened, armed_then_cancelled", "closed_definition": "frozen opened trade closed strictly before cutoff", "censored_definition": "opened trade not closed strictly before cutoff"},
        "inclusion_exclusion": {"include": ["exact frozen historical warmed-replay population", "setup origins at/after first eligible timestamp", "closed trades strictly before cutoff for outcome tables"], "exclude": ["compatibility period from primary population", "pre-warm-up setup/trade state", "censored trades from closed-trade metrics"], "unchanged_semantics": "frozen immediate/armed, cancellation, entry, exit, stop, R, no-lookahead, and direction semantics are inherited unchanged"},
        "direction_definition": "LONG and SHORT are the frozen StrategyEvent/OpenTrade Direction values; total-R gap is LONG total R minus SHORT total R; expectancy gap is LONG expectancy minus SHORT expectancy.",
        "metric_definitions": {
            "closed_trade_count": "number of frozen opened trades with a canonical exit finalized strictly before the historical cutoff",
            "total_r": "sum of canonical closed-trade R",
            "expectancy_r": "total R / closed-trade count; null when count is zero",
            "profit_factor": "sum of strictly positive R / absolute sum of strictly negative R; null when there are no strictly negative trades",
            "win_rate": "strictly positive closed-trade count / closed-trade count; zero-R trades are not wins",
            "stop_rate": "closed trades whose exit-reason membership contains exit_stop / closed-trade count",
            "positive_r": "sum of strictly positive closed-trade R",
            "negative_r_magnitude": "absolute sum of strictly negative closed-trade R",
            "mean_losing_r": "arithmetic mean of strictly negative closed-trade R; null when there are no losses",
            "median_losing_r": "ordinary median of strictly negative closed-trade R; null when there are no losses",
            "holding_duration_minutes": "(exit_timestamp - entry_timestamp) / 60000 for a closed trade",
            "initial_risk": "abs(entry_price - frozen initial stop_price); must be finite and strictly positive",
            "full_exit_bar_mfe_r": "over contiguous post-entry 15m bars through and including the exit bar: LONG max(0,max(high)-entry)/initial_risk; SHORT max(0,entry-min(low))/initial_risk",
            "full_exit_bar_mae_r": "over contiguous post-entry 15m bars through and including the exit bar: LONG max(0,entry-min(low))/initial_risk; SHORT max(0,max(high)-entry)/initial_risk",
        },
        "setup_paths": {"immediate_open": "trade opens at originating HEMA flip", "armed_then_opened": "HEMA flip arms then later opens", "armed_then_cancelled": "HEMA flip arms then is cancelled; eligible but unopened", "exhaustive_eligible_paths": ["immediate_open", "armed_then_opened", "armed_then_cancelled"]},
        "primary_questions": ["temporal persistence versus concentration", "loss/stop structure", "winner frequency and magnitude", "positive-tail dependence", "setup-path composition", "holding/path behavior", "pre-entry regime context", "predeclared interactions"],
        "calendar_and_ordering": {"entry_calendar_timezone": "UTC", "year": "entry-finalization calendar YYYY", "quarter": "entry-finalization calendar YYYY-Q1 through YYYY-Q4", "month": "entry-finalization chronological YYYY-MM; never pool same-named months across years", "closed_trade_order": "exit_timestamp ascending, then trade_id ascending", "quartiles": "contiguous order partitions; first remainder buckets receive one additional trade", "first_half": "first ceil(n/2) ordered closed trades; second floor(n/2) ordered closed trades"},
        "regime_families": {
            "bias_persistence_hours": {"definition": "(setup-origin decision timestamp - post_state.bias_activation_timestamp) / 3600000", "valid": "finite and nonnegative", "binning": "combined eligible setup-only type-7 tertiles: low <= q1, medium q1 < value <= q2, high > q2, else missing", **dict(context_lock["bias_persistence_hours"])},
            "atr_at_setup": {"definition": "exact StrategyBar.atr at setup-origin decision", "valid": "finite and positive", "binning": "combined eligible setup-only type-7 tertiles: low <= q1, medium q1 < value <= q2, high > q2, else missing", **dict(context_lock["atr_at_setup"])},
            "broad_market_direction_32": {"definition": "sign(current finalized 15m close - close exactly 32 finalized 15m bars earlier)", "contiguity": "all 33 finalized bars must be exactly contiguous at 900000 ms", "categories": ["down", "flat", "up", "missing"], "lookback_origin": "reused frozen Phase 7 regime feature family; not selected here", **dict(context_lock["broad_market_direction_32"])},
            "quantile_method": "ordered finite combined eligible context values; position=(n-1)*q; linear interpolation; q=1/3 and 2/3; ties at boundaries remain in lower-inclusive bucket"},
        "required_output": {"directional_baseline": ["eligible_setups", "opened_trades", "closed_trades", "total_r", "expectancy_r", "profit_factor", "win_rate", "stop_rate", "positive_r", "negative_r_magnitude", "2r_plus", "3r_plus", "5r_plus"], "direction_x_calendar": ["count", "total_r", "expectancy_r", "profit_factor", "win_rate", "stop_rate", "positive_r", "negative_r_magnitude"], "direction_x_quartile_and_half": ["count", "total_r", "expectancy_r", "profit_factor", "win_rate", "stop_rate"], "direction_x_setup_path": ["eligible_setups", "opened_trades", "closed_trades", "total_r", "expectancy_r", "profit_factor", "win_rate", "stop_rate", "2r_plus", "3r_plus", "5r_plus"], "exit_loss": "exit-reason membership counts/frequencies/R contribution are NON-additive when a trade has multiple reasons; separate exclusive ordered reason-combination counts/frequencies/R contributions are additive. Also report mean losing R and median losing R.", "stop_nonstop": "stop is any exit reason EXIT_STOP; stop/non-stop are exhaustive groups; report count, frequency, total R, and non-stop total R separately by direction", "stopped_trade_mfe_mae_holding": "for stopped trades only, full-exit-bar contiguous M15 path MFE/MAE in R and elapsed holding minutes: count, mean, median, p25, p75, p90; null on missing path", "holding_duration": "overall, winners, and losers separately: elapsed holding minutes count, mean, median, p25, p75, p90, maximum", "winner_tail": "positive/negative counts and R, thresholds 2/3/5/10R, max/mean/median winner; for each direction top 1/3/5/10 winner contribution and remaining total R after each removal"},
        "distribution_and_null_policy": {"percentiles": "type-7 p25/p50/p75/p90", "zero_or_missing_denominator": "serialize null, never zero/Infinity", "regime_missing": "retain and report an explicit missing bucket; exclude only missing continuous values from tertile-boundary derivation", "path_missing": "if any expected post-entry 15m bar is absent, MFE and MAE are null and missing counts are reported; holding elapsed minutes remains available", "minimum_cell": "n < 30 closed trades => SMALL-SAMPLE / DESCRIPTIVE ONLY", "tie_policies": {"top_winners": "R descending, then exit_timestamp ascending, then trade_id ascending", "calendar": "chronological labels", "quartile": "first remainder buckets larger"}, "serialization": "canonical JSON sort_keys=true, compact separators, UTF-8, terminal newline"},
        "interactions": {"required": ["direction × chronological quartile", "direction × each primary regime family"], "forbidden": "arbitrary high-dimensional interactions; optional direction × half × regime only if predeclared future correction and all cells are described"},
        "later_period_contrast": {"source": "frozen compatibility provider_economics.xm.by_direction only", "label": "DESCRIPTIVE CROSS-PERIOD CONTRAST — NOT VALIDATION OF A NEW HYPOTHESIS", "separate_not_validation": True, "merge_with_historical": False},
        "gap_decompositions": {"baseline": "E[R]=pwin*meanwin + ploss*meanloss + pzero*0", "exact_symmetric_product_contributions": "For every product p*m, with direction values pL,mL,pS,mS: frequency=(pL-pS)*(mL+mS)/2 and magnitude=(mL-mS)*(pL+pS)/2. Apply separately to pwin*meanwin and ploss*meanloss; winner-frequency + winner-magnitude + loss-frequency + loss-magnitude equals the LONG-minus-SHORT expectancy gap exactly.", "setup_path": "Across exhaustive opened paths immediate_open and armed_then_opened, E_d=sum_k(w_dk*mu_dk). Exact symmetric composition=sum_k((w_Lk-w_Sk)*(mu_Lk+mu_Sk)/2); exact within-path=sum_k((mu_Lk-mu_Sk)*(w_Lk+w_Sk)/2); composition+within-path equals the expectancy gap exactly.", "stop_nonstop": "Across exhaustive stop and non-stop groups s, E_d=sum_s(p_ds*m_ds). For each s, frequency=(p_Ls-p_Ss)*(m_Ls+m_Ss)/2 and magnitude=(m_Ls-m_Ss)*(p_Ls+p_Ss)/2; summed stop/non-stop components equal the expectancy gap exactly, overlap win/loss components, and are not added to them.", "regime": "For every one of the three frozen regime families and all of its fixed buckets k including missing, E_d=sum_k(w_dk*mu_dk). Exact composition=sum_k((w_Lk-w_Sk)*(mu_Lk+mu_Sk)/2); exact within-bucket=sum_k((mu_Lk-mu_Sk)*(w_Lk+w_Sk)/2); components sum exactly to the expectancy gap.", "associations_not_summed": ["tail 5R+", "top-N removal", "calendar-quarter total-R gap"], "total_r_gap": "LONG total R minus SHORT total R"},
        "classification": {"labels": ["TEMPORALLY_CONCENTRATED", "LOSS_STRUCTURE_DOMINATED", "POSITIVE_TAIL_DOMINATED", "SETUP_COMPOSITION_ASSOCIATED", "REGIME_ASSOCIATED", "BROAD_MULTIFACTOR", "INSUFFICIENT_UNRESOLVED"], "interpretability": "paired chronological quartile or regime bucket is interpretable only when both LONG and SHORT have n>=30 closed trades; an evaluable calendar quarter has at least one closed trade in either direction", "rules": {"persistent_diagnostic": "requires a positive baseline expectancy gap, at least two interpretable chronological quartiles, and every quartile gap nonzero with the same positive sign as the baseline; it is a diagnostic, not a final label", "TEMPORALLY_CONCENTRATED": "not persistent_diagnostic and top two positive calendar-quarter total-R-gap contributions / sum all positive contributions >=0.60, with at least 3 evaluable calendar quarters", "LOSS_STRUCTURE_DOMINATED": "combined positive loss-frequency plus loss-magnitude contribution / positive baseline expectancy gap >=0.50 and the combined positive loss contribution exceeds each nonnegative winner-frequency and winner-magnitude contribution", "POSITIVE_TAIL_DOMINATED": "for a positive baseline total-R gap, (baseline total-R gap - post-top-5-each-direction-removal total-R gap) / baseline total-R gap >=0.50 OR winner-magnitude exact symmetric product contribution / positive baseline expectancy gap >=0.50", "SETUP_COMPOSITION_ASSOCIATED": "abs(path composition effect) / abs(expectancy gap) >=0.25", "REGIME_ASSOCIATED": "at least one of all three frozen primary regime families has >=2 interpretable paired buckets and either bucket expectancy-gap range >=0.5*abs(baseline gap) with sign change, OR abs(regime composition effect)/abs(gap)>=0.25; no family may be post-hoc excluded", "BROAD_MULTIFACTOR": "at least two non-overlapping primary exact symmetric product components have abs share>=0.25 with none>=0.50, OR at least two of temporal/loss/tail/setup/regime labels apply", "INSUFFICIENT_UNRESOLVED": "reproduction fails (Stage B stops) or no prior label applies because interpretable evidence is inadequate/contradictory"}, "zero_baseline_gap": "every denominator-based ratio is null and classification is unresolved; multiple final labels otherwise allowed"},
        "hypotheses": "Only after all predeclared outputs: HYPOTHESES GENERATED — NOT TESTED, with observation, mechanism, evidence, confounders, confirmatory test, independent data, and discovery disclaimer.",
        "forbidden_analyses": ["LONG-only or SHORT-disable backtests", "directional filters", "counterfactual strategy variants", "entry/exit/stop/indicator/bias changes", "XAU parameter or threshold tuning", "profitability-ranked filters", "date/volatility/ATR/regime threshold searches", "broker-cost calibration", "production migration", "BTC Phase 7.4"],
        "stage_a_economics": "NEW ATTRIBUTION ECONOMICS INSPECTED BEFORE LOCK: NO",
    }


def xau_directional_edge_attribution_protocol_json(protocol: Mapping[str, Any]) -> bytes:
    return (canonical_json(dict(protocol)) + "\n").encode("utf-8")


def protocol_sha256(protocol: Mapping[str, Any]) -> str:
    return sha256(xau_directional_edge_attribution_protocol_json(protocol)).hexdigest()


def verify_xau_directional_edge_attribution_protocol(protocol: Mapping[str, Any]) -> None:
    if protocol.get("schema_version") != SCHEMA_VERSION:
        raise ValueError("protocol schema mismatch")
    canonical = xau_directional_edge_attribution_protocol_json(protocol)
    if not canonical.endswith(b"\n") or json.loads(canonical) != protocol:
        raise ValueError("protocol serialization is not canonical")
    frozen = protocol.get("frozen_inputs", {})
    if not isinstance(frozen, Mapping) or frozen.get("source_sha256") != EXPECTED_SOURCE_SHA256 or frozen.get("historical_cutoff", {}).get("epoch_ms") != HISTORICAL_CUTOFF_MS:
        raise ValueError("protocol frozen identities mismatch")
    if protocol.get("canonical_starting_state") != {"sha": CANONICAL_STARTING_SHA, "annotated_tag": CANONICAL_TAG, "annotated_tag_object": CANONICAL_TAG_OBJECT, "peeled_target": CANONICAL_STARTING_SHA}:
        raise ValueError("protocol canonical Git identity mismatch")
    if frozen.get("first_eligible_timestamp") != {"epoch_ms": FIRST_ELIGIBLE_TIMESTAMP_MS, "utc": "2024-03-20T22:15:00Z"}:
        raise ValueError("protocol first eligible timestamp mismatch")
    context_lock = frozen.get("context_lock")
    if context_lock != EXPECTED_CONTEXT_LOCK:
        raise ValueError("protocol context-lock identity mismatch")
    if protocol.get("population") is None or {key: protocol["population"].get(key) for key in EXPECTED_POPULATION} != EXPECTED_POPULATION:
        raise ValueError("protocol population mismatch")
    if protocol.get("stage_a_economics") != "NEW ATTRIBUTION ECONOMICS INSPECTED BEFORE LOCK: NO":
        raise ValueError("protocol economics declaration mismatch")
    expected = build_xau_directional_edge_attribution_protocol(context_lock)
    if dict(protocol) != expected:
        raise ValueError("protocol semantics differ from the canonical Stage A builder")
    if sha256(canonical).hexdigest() != EXPECTED_PROTOCOL_SHA256:
        raise ValueError("protocol SHA-256 differs from the pinned Stage A lock")


def write_xau_directional_edge_attribution_protocol(protocol: Mapping[str, Any], path: Path) -> str:
    verify_xau_directional_edge_attribution_protocol(protocol)
    if path.exists():
        raise FileExistsError(f"immutable protocol lock already exists: {path}")
    payload = xau_directional_edge_attribution_protocol_json(protocol)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(payload)
    return sha256(payload).hexdigest()
