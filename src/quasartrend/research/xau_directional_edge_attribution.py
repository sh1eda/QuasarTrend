"""Stage A-only XAU directional-edge attribution protocol lock.

This boundary intentionally derives only pre-entry context.  It never builds a
trade ledger, calls the backtest engine, or reads realized R/economics.
"""
from __future__ import annotations

from collections import Counter, defaultdict
from datetime import UTC, datetime
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
PROTOCOL_COMMIT_SHA = "8d9d33751cb4c100b1ad0250f100775c86bbbd2c"
RESULT_PATH = "exports/xm/phase_xau_directional_edge_attribution.json"
HISTORICAL_HEADLINES = {
    "aggregate": {"closed_trades": 820, "total_r": 70.72001507737389, "expectancy_r": .08624392082606572, "profit_factor": 1.1182300376072547},
    "long": {"closed_trades": 468, "total_r": 127.07357644339103, "expectancy_r": .27152473599015176},
    "short": {"closed_trades": 352, "total_r": -56.353561366017146, "expectancy_r": -.16009534478982143},
}
MIN_CELL = 30
TEMPORAL_CONCENTRATION = 0.60
DOMINANCE = 0.50
ASSOCIATION = 0.25
SMALL_SAMPLE_WARNING = "SMALL-SAMPLE / DESCRIPTIVE ONLY"


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


def _context_lock_from_contexts(contexts: Sequence[Mapping[str, Any]], population: Mapping[str, int], first_eligible_timestamp_ms: int) -> dict[str, Any]:
    eligible = [row for row in contexts if row["path"] in {"immediate_open", "armed_then_opened", "armed_then_cancelled"}]
    def continuous(name: str) -> dict[str, Any]:
        values = [float(row[name]) for row in eligible if row[name] is not None]
        q1, q2 = (type_7_quantile(values, quantile) for quantile in TERTILE_QUANTILES)
        counts = Counter(tertile_bin(row[name], q1, q2) for row in eligible)
        return {"q1": q1, "q2": q2, "nonmissing": len(values),
                "counts": {bucket: counts[bucket] for bucket in ("low", "medium", "high", "missing")}}
    direction_counts = Counter(str(row["broad_market_direction_32"]) for row in eligible)
    return {"population": dict(population), "first_eligible_timestamp_ms": first_eligible_timestamp_ms,
            "bias_persistence_hours": continuous("bias_persistence_hours"), "atr_at_setup": continuous("atr_at_setup"),
            "broad_market_direction_32": {"counts": {bucket: direction_counts[bucket] for bucket in ("down", "flat", "up", "missing")}}}


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
    lock = _context_lock_from_contexts(contexts, population, int(warmup["first_strategy_eligible_timestamp"]))
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


def _git_output(root: Path, *args: str) -> str:
    return subprocess.run(("git", *args), cwd=root, check=True, text=True,
                          stdout=subprocess.PIPE, stderr=subprocess.PIPE).stdout.strip()


def _verify_stage_b_guard(*, repo_root: Path, protocol_path: Path, xm_m1_source: Path) -> dict[str, Any]:
    """Authorize Stage B only from the committed, byte-pinned Stage A lock."""
    root = repo_root.resolve()
    if not _tracked_git_clean(root):
        raise ValueError("official Stage B execution requires a clean tracked index and worktree")
    for ancestor in (CANONICAL_STARTING_SHA, PROTOCOL_COMMIT_SHA):
        if subprocess.run(("git", "merge-base", "--is-ancestor", ancestor, "HEAD"), cwd=root).returncode:
            raise ValueError("required canonical/protocol commit is not an ancestor of HEAD")
    if _git_output(root, "rev-parse", CANONICAL_TAG) != CANONICAL_TAG_OBJECT or _git_output(root, "rev-parse", f"{CANONICAL_TAG}^{{}}") != CANONICAL_STARTING_SHA:
        raise ValueError("canonical annotated tag identity mismatch")
    committed = subprocess.run(
        ("git", "show", f"{PROTOCOL_COMMIT_SHA}:exports/xm/phase_xau_directional_edge_attribution_protocol.json"),
        cwd=root, check=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
    ).stdout
    if sha256(committed).hexdigest() != EXPECTED_PROTOCOL_SHA256:
        raise ValueError("protocol commit does not contain the pinned protocol blob")
    payload = protocol_path.read_bytes()
    if payload != committed or sha256(payload).hexdigest() != EXPECTED_PROTOCOL_SHA256:
        raise ValueError("working protocol bytes differ from committed pinned protocol")
    protocol = json.loads(payload)
    verify_xau_directional_edge_attribution_protocol(protocol)
    canonical_source = (root / RAW_SOURCE_PATH).resolve()
    if xm_m1_source.resolve() != canonical_source:
        raise ValueError("Stage B requires the canonical raw XM source path")
    actual_sources = {path: _hash(root / path) for path in EXPECTED_SOURCE_SHA256}
    if actual_sources != EXPECTED_SOURCE_SHA256:
        raise ValueError("frozen Stage B source artifact identity mismatch")
    # The historical module owns the frozen production/Pine manifest; its old
    # HEAD-only provenance check is intentionally not used after the protocol commit.
    historical.verify_frozen_production_sources(root)
    return {"protocol": protocol, "source_sha256": actual_sources,
            "protocol_commit_sha": PROTOCOL_COMMIT_SHA, "head": _git_output(root, "rev-parse", "HEAD")}


def _closed(rows: Sequence[Mapping[str, Any]]) -> list[Mapping[str, Any]]:
    return [row for row in rows if row.get("outcome") == "closed"]


def _calendar_label(timestamp_ms: int, kind: str) -> str:
    stamp = datetime.fromtimestamp(timestamp_ms / 1000, UTC)
    if kind == "year": return str(stamp.year)
    if kind == "quarter": return f"{stamp.year:04d}-Q{(stamp.month - 1) // 3 + 1}"
    if kind == "month": return f"{stamp.year:04d}-{stamp.month:02d}"
    raise ValueError("unknown calendar kind")


def _distribution(values: Sequence[float]) -> dict[str, Any]:
    ordered = sorted(float(value) for value in values)
    return {"count": len(ordered), "mean": None if not ordered else sum(ordered) / len(ordered),
            "median": None if not ordered else type_7_quantile(ordered, .5),
            "p25": None if not ordered else type_7_quantile(ordered, .25),
            "p75": None if not ordered else type_7_quantile(ordered, .75),
            "p90": None if not ordered else type_7_quantile(ordered, .90),
            "maximum": None if not ordered else ordered[-1],
            "sample_warning": SMALL_SAMPLE_WARNING if len(ordered) < MIN_CELL else None}


def _metrics(rows: Sequence[Mapping[str, Any]], *, eligible_setups: int | None = None) -> dict[str, Any]:
    closed = _closed(rows)
    values = [float(row["r"]) for row in closed]
    wins, losses = [value for value in values if value > 0], [value for value in values if value < 0]
    total = float(sum(values))
    return {"count": len(closed), "closed_trades": len(closed), "total_r": total,
            "expectancy_r": None if not values else total / len(values),
            "profit_factor": None if not losses else sum(wins) / abs(sum(losses)),
            "win_rate": None if not values else len(wins) / len(values),
            "stop_rate": None if not closed else sum(bool(row["stop_hit"]) for row in closed) / len(closed),
            "positive_r": float(sum(wins)), "negative_r_magnitude": float(abs(sum(losses)),),
            "mean_losing_r": None if not losses else sum(losses) / len(losses),
            "median_losing_r": None if not losses else type_7_quantile(losses, .5),
            "r_per_setup": None if not eligible_setups else total / eligible_setups}


def _ordered_closed(rows: Sequence[Mapping[str, Any]]) -> list[Mapping[str, Any]]:
    return sorted(_closed(rows), key=lambda row: (int(row["exit_timestamp"]), str(row["trade_id"])))


def _partitions(rows: Sequence[Mapping[str, Any]], count: int) -> list[list[Mapping[str, Any]]]:
    if count <= 0: raise ValueError("partition count must be positive")
    base, remainder = divmod(len(rows), count); cursor = 0; result = []
    for index in range(count):
        width = base + (1 if index < remainder else 0)
        result.append(list(rows[cursor:cursor + width])); cursor += width
    return result


def _small_cell(metrics: Mapping[str, Any]) -> bool:
    return int(metrics["closed_trades"]) < MIN_CELL


def _cell(metrics: Mapping[str, Any], **extra: Any) -> dict[str, Any]:
    """Attach the locked warning to every table cell with closed-trade metrics."""
    small = _small_cell(metrics)
    return {**extra, **metrics, "small_sample": small,
            "sample_warning": SMALL_SAMPLE_WARNING if small else None}


def _tail(rows: Sequence[Mapping[str, Any]]) -> dict[str, Any]:
    closed = _closed(rows)
    winners = sorted((row for row in closed if float(row["r"]) > 0), key=lambda row: (-float(row["r"]), int(row["exit_timestamp"]), str(row["trade_id"])))
    values = [float(row["r"]) for row in closed]
    result: dict[str, Any] = {"positive_trade_count": len(winners), "negative_trade_count": sum(value < 0 for value in values), "zero_trade_count": sum(value == 0 for value in values), "positive_r": sum(float(row["r"]) for row in winners),
        "negative_r_magnitude": abs(sum(value for value in values if value < 0)),
        "2r_plus": sum(value >= 2 for value in values), "3r_plus": sum(value >= 3 for value in values),
        "5r_plus": sum(value >= 5 for value in values), "10r_plus": sum(value >= 10 for value in values),
        "maximum_winner_r": None if not winners else float(winners[0]["r"]),
        "mean_winning_r": None if not winners else sum(float(row["r"]) for row in winners) / len(winners),
        "median_winning_r": None if not winners else type_7_quantile([float(row["r"]) for row in winners], .5)}
    for count in (1, 3, 5, 10):
        selected = winners[:count]; ids = {str(row["trade_id"]) for row in selected}
        contribution = sum(float(row["r"]) for row in selected)
        total, positive = sum(values), sum(float(row["r"]) for row in winners)
        result[f"top_{count}"] = {"trade_ids": [str(row["trade_id"]) for row in selected], "contribution_r": contribution,
            "positive_r_share": None if positive == 0 else contribution / positive, "net_r_share": None if total == 0 else contribution / total,
            "remaining_total_r": sum(float(row["r"]) for row in closed if str(row["trade_id"]) not in ids)}
    return result


def _enriched_ledger(rows: Sequence[Mapping[str, Any]], replay: Any, context_rows: Sequence[Mapping[str, Any]], protocol: Mapping[str, Any]) -> list[dict[str, Any]]:
    """Attach locked pre-entry context and full-exit-bar path diagnostics."""
    contexts = {int(row["timestamp"]): row for row in context_rows}
    ltf = [trace for trace in replay.traces if trace.source_bar.timeframe is Timeframe.MINUTES_15]
    by_timestamp = {trace.strategy_bar.timestamp: index for index, trace in enumerate(ltf) if trace.strategy_bar is not None}
    families = protocol["regime_families"]
    result: list[dict[str, Any]] = []
    for original in rows:
        row = dict(original)
        context = contexts.get(int(row["setup_origin_timestamp"]))
        if context is None:
            raise ValueError("closed/opened trade lacks setup-origin context")
        if context["direction"] != row["direction"] or context["path"] != row["path"]:
            raise ValueError("setup-origin context disagrees with frozen ledger")
        row["bias_persistence_hours"] = context["bias_persistence_hours"]
        row["atr_at_setup"] = context["atr_at_setup"]
        row["broad_market_direction_32"] = context["broad_market_direction_32"]
        row["bias_persistence_hours_bucket"] = tertile_bin(context["bias_persistence_hours"], float(families["bias_persistence_hours"]["q1"]), float(families["bias_persistence_hours"]["q2"]))
        row["atr_at_setup_bucket"] = tertile_bin(context["atr_at_setup"], float(families["atr_at_setup"]["q1"]), float(families["atr_at_setup"]["q2"]))
        row["broad_market_direction_32_bucket"] = context["broad_market_direction_32"]
        row["holding_duration_minutes"] = None
        row["mae_r"] = None
        if row["outcome"] == "closed":
            entry = by_timestamp.get(int(row["entry_timestamp"])); exit_ = by_timestamp.get(int(row["exit_timestamp"]))
            if entry is None or exit_ is None or exit_ < entry:
                raise ValueError("closed ledger timestamps lack replay path")
            row["holding_duration_minutes"] = (int(row["exit_timestamp"]) - int(row["entry_timestamp"])) / 60_000
            path_bars = [trace.source_bar for trace in ltf[entry + 1:exit_ + 1]]
            expected = (ltf[exit_].source_bar.open_time - ltf[entry].source_bar.open_time) // M15_DURATION_MS
            contiguous = len(path_bars) == expected and all(bar.open_time == ltf[entry].source_bar.open_time + (offset + 1) * M15_DURATION_MS for offset, bar in enumerate(path_bars))
            if contiguous:
                risk = abs(float(row["entry_price"]) - float(row["stop_price"]))
                high = max((bar.high for bar in path_bars), default=float(row["entry_price"]))
                low = min((bar.low for bar in path_bars), default=float(row["entry_price"]))
                if row["direction"] == "long":
                    mfe, mae = max(0.0, high - float(row["entry_price"])) / risk, max(0.0, float(row["entry_price"]) - low) / risk
                else:
                    mfe, mae = max(0.0, float(row["entry_price"]) - low) / risk, max(0.0, high - float(row["entry_price"])) / risk
                if row["mfe_r"] is not None and not math.isclose(mfe, float(row["mfe_r"]), rel_tol=1e-12, abs_tol=1e-12):
                    raise ValueError("full-exit-bar MFE differs from frozen ledger")
                row["mfe_r"], row["mae_r"] = mfe, mae
        result.append(row)
    return sorted(result, key=lambda row: (int(row["entry_timestamp"]), str(row["trade_id"])))


def _eligible_setup_context_ledger(contexts: Sequence[Mapping[str, Any]], protocol: Mapping[str, Any]) -> list[dict[str, Any]]:
    """Outcome-free, deterministic context ledger used to reconcile all setup denominators."""
    families = protocol["regime_families"]
    result = []
    for context in contexts:
        if context["path"] not in {"immediate_open", "armed_then_opened", "armed_then_cancelled"}:
            continue
        result.append({"timestamp": int(context["timestamp"]), "direction": str(context["direction"]), "path": str(context["path"]),
                       "bias_persistence_hours": context["bias_persistence_hours"], "atr_at_setup": context["atr_at_setup"], "broad_market_direction_32": context["broad_market_direction_32"],
                       "bias_persistence_hours_bucket": tertile_bin(context["bias_persistence_hours"], float(families["bias_persistence_hours"]["q1"]), float(families["bias_persistence_hours"]["q2"])),
                       "atr_at_setup_bucket": tertile_bin(context["atr_at_setup"], float(families["atr_at_setup"]["q1"]), float(families["atr_at_setup"]["q2"])),
                       "broad_market_direction_32_bucket": context["broad_market_direction_32"]})
    return sorted(result, key=lambda row: int(row["timestamp"]))


def _by_direction(rows: Sequence[Mapping[str, Any]], contexts: Sequence[Mapping[str, Any]]) -> dict[str, Any]:
    eligible = [row for row in contexts if row["path"] in {"immediate_open", "armed_then_opened", "armed_then_cancelled"}]
    result = {}
    for direction in ("long", "short"):
        items = [row for row in rows if row["direction"] == direction]
        metrics = _metrics(items, eligible_setups=sum(row["direction"] == direction for row in eligible))
        result[direction] = _cell(metrics, eligible_setups=sum(row["direction"] == direction for row in eligible), opened_trades=len(items), **_tail(items))
    return result


def _calendar_tables(rows: Sequence[Mapping[str, Any]]) -> dict[str, Any]:
    output: dict[str, Any] = {}
    for kind in ("year", "quarter", "month"):
        grouped: dict[str, list[Mapping[str, Any]]] = defaultdict(list)
        for row in _closed(rows): grouped[_calendar_label(int(row["entry_timestamp"]), kind)].append(row)
        output[kind] = {label: {direction: _cell(_metrics([row for row in items if row["direction"] == direction])) for direction in ("long", "short")} for label, items in sorted(grouped.items())}
    return output


def _chronological_table(rows: Sequence[Mapping[str, Any]]) -> dict[str, Any]:
    ordered = _ordered_closed(rows)
    def report(partitions: Sequence[Sequence[Mapping[str, Any]]]) -> dict[str, Any]:
        return {str(index + 1): {direction: _cell(_metrics([row for row in part if row["direction"] == direction])) for direction in ("long", "short")} for index, part in enumerate(partitions)}
    return {"ordering": "exit_timestamp ascending, then trade_id ascending", "quartiles": report(_partitions(ordered, 4)), "halves": report(_partitions(ordered, 2))}


def _setup_path_table(rows: Sequence[Mapping[str, Any]], contexts: Sequence[Mapping[str, Any]]) -> dict[str, Any]:
    eligible = [row for row in contexts if row["path"] in {"immediate_open", "armed_then_opened", "armed_then_cancelled"}]
    result = {}
    for path in ("immediate_open", "armed_then_opened", "armed_then_cancelled"):
        result[path] = {}
        for direction in ("long", "short"):
            items = [row for row in rows if row["path"] == path and row["direction"] == direction]
            count = sum(row["path"] == path and row["direction"] == direction for row in eligible)
            metrics = _metrics(items, eligible_setups=count)
            result[path][direction] = _cell(metrics, eligible_setups=count, opened_trades=len(items), **_tail(items))
    return result


def _exit_and_holding(rows: Sequence[Mapping[str, Any]]) -> dict[str, Any]:
    output: dict[str, Any] = {}
    for direction in ("long", "short"):
        items = [row for row in _closed(rows) if row["direction"] == direction]
        membership: dict[str, Any] = {}
        combinations: dict[str, Any] = {}
        reasons = sorted({reason for row in items for reason in row["exit_reasons"]})
        for reason in reasons:
            selected = [row for row in items if reason in row["exit_reasons"]]
            membership[reason] = {"count": len(selected), "frequency": None if not items else len(selected) / len(items), "r_contribution": sum(float(row["r"]) for row in selected), "non_additive": True, "sample_warning": SMALL_SAMPLE_WARNING if len(selected) < MIN_CELL else None}
        for row in items:
            key = "|".join(row["exit_reasons"])
            combinations.setdefault(key, []).append(row)
        exclusive = {key: {"count": len(selected), "frequency": len(selected) / len(items), "r_contribution": sum(float(row["r"]) for row in selected), "sample_warning": SMALL_SAMPLE_WARNING if len(selected) < MIN_CELL else None} for key, selected in sorted(combinations.items())}
        stopped, nonstop = [row for row in items if row["stop_hit"]], [row for row in items if not row["stop_hit"]]
        stopped_values = {"count": len(stopped), "sample_warning": SMALL_SAMPLE_WARNING if len(stopped) < MIN_CELL else None, "mfe_r": _distribution([float(row["mfe_r"]) for row in stopped if row["mfe_r"] is not None]), "mae_r": _distribution([float(row["mae_r"]) for row in stopped if row["mae_r"] is not None]), "holding_duration_minutes": _distribution([float(row["holding_duration_minutes"]) for row in stopped if row["holding_duration_minutes"] is not None]), "missing_path_count": sum(row["mfe_r"] is None or row["mae_r"] is None for row in stopped)}
        output[direction] = {"sample_warning": SMALL_SAMPLE_WARNING if len(items) < MIN_CELL else None, "exit_reason_membership": membership, "exclusive_ordered_reason_combinations": exclusive,
            "stop_nonstop": {"stop": {"count": len(stopped), "frequency": None if not items else len(stopped) / len(items), "total_r": sum(float(row["r"]) for row in stopped), "sample_warning": SMALL_SAMPLE_WARNING if len(stopped) < MIN_CELL else None}, "nonstop": {"count": len(nonstop), "frequency": None if not items else len(nonstop) / len(items), "total_r": sum(float(row["r"]) for row in nonstop), "sample_warning": SMALL_SAMPLE_WARNING if len(nonstop) < MIN_CELL else None}},
            "mean_losing_r": _metrics(items)["mean_losing_r"], "median_losing_r": _metrics(items)["median_losing_r"], "stopped_trade_diagnostics": stopped_values,
            "holding_duration_minutes": {"overall": _distribution([float(row["holding_duration_minutes"]) for row in items if row["holding_duration_minutes"] is not None]), "winners": _distribution([float(row["holding_duration_minutes"]) for row in items if float(row["r"]) > 0 and row["holding_duration_minutes"] is not None]), "losers": _distribution([float(row["holding_duration_minutes"]) for row in items if float(row["r"]) < 0 and row["holding_duration_minutes"] is not None])}}
    return output


def _regime_tables(rows: Sequence[Mapping[str, Any]], contexts: Sequence[Mapping[str, Any]]) -> dict[str, Any]:
    families = {"bias_persistence_hours": ("bias_persistence_hours_bucket", ("low", "medium", "high", "missing")),
                "atr_at_setup": ("atr_at_setup_bucket", ("low", "medium", "high", "missing")),
                "broad_market_direction_32": ("broad_market_direction_32_bucket", ("down", "flat", "up", "missing"))}
    eligible = [row for row in contexts if row["path"] in {"immediate_open", "armed_then_opened", "armed_then_cancelled"}]
    result = {}
    for name, (key, buckets) in families.items():
        table = {}
        for bucket in buckets:
            table[bucket] = {}
            for direction in ("long", "short"):
                eligible_count = sum(row["direction"] == direction and (tertile_bin(row[name], EXPECTED_CONTEXT_LOCK[name]["q1"], EXPECTED_CONTEXT_LOCK[name]["q2"]) if name in {"bias_persistence_hours", "atr_at_setup"} else row[name]) == bucket for row in eligible)
                selected = [row for row in rows if row["direction"] == direction and row[key] == bucket]
                metrics = _metrics(selected, eligible_setups=eligible_count)
                table[bucket][direction] = _cell(metrics, eligible_setups=eligible_count, opened_trades=len(selected))
        result[name] = table
    return result


def _product_components(left_rows: Sequence[Mapping[str, Any]], right_rows: Sequence[Mapping[str, Any]], predicate: Any) -> dict[str, Any]:
    def pair(rows: Sequence[Mapping[str, Any]]) -> tuple[float, float]:
        values_ = [float(row["r"]) for row in _closed(rows)]
        selected = [value for value in values_ if predicate(value)]
        return (0.0, 0.0) if not values_ else (len(selected) / len(values_), 0.0 if not selected else sum(selected) / len(selected))
    p_l, m_l = pair(left_rows); p_s, m_s = pair(right_rows)
    frequency = (p_l - p_s) * (m_l + m_s) / 2
    magnitude = (m_l - m_s) * (p_l + p_s) / 2
    return {"p_long": p_l, "m_long": m_l, "p_short": p_s, "m_short": m_s, "frequency": frequency, "magnitude": magnitude, "sum": frequency + magnitude}


def _composition_within(left_rows: Sequence[Mapping[str, Any]], right_rows: Sequence[Mapping[str, Any]], key: str | Any, buckets: Sequence[str]) -> dict[str, Any]:
    """Exact symmetric decomposition, unavailable rather than zero-imputed one-sided means."""
    def bucket_of(row: Mapping[str, Any]) -> str:
        return key(row) if callable(key) else str(row[key])
    def weights(rows: Sequence[Mapping[str, Any]], bucket: str) -> tuple[float, float | None]:
        closed = _closed(rows); selected = [row for row in closed if bucket_of(row) == bucket]
        return (0.0 if not closed else len(selected) / len(closed), None if not selected else sum(float(row["r"]) for row in selected) / len(selected))
    composition = within = 0.0; available = True; detail = {}
    for bucket in buckets:
        w_l, m_l = weights(left_rows, bucket); w_s, m_s = weights(right_rows, bucket)
        if m_l is None and m_s is None:
            frequency = magnitude = 0.0
        elif m_l is None or m_s is None:
            available = False; frequency = magnitude = None
        else:
            frequency = (w_l - w_s) * (m_l + m_s) / 2
            magnitude = (m_l - m_s) * (w_l + w_s) / 2
            composition += frequency; within += magnitude
        detail[bucket] = {"w_long": w_l, "m_long": m_l, "w_short": w_s, "m_short": m_s,
                          "frequency_effect": frequency, "magnitude_effect": magnitude,
                          "composition": frequency, "within": magnitude,
                          "available": frequency is not None}
    return {"buckets": detail, "available": available,
            "unresolved_reason": None if available else "one-sided bucket mean is unavailable; no zero imputation",
            "frequency_effect": composition if available else None, "magnitude_effect": within if available else None,
            "composition": composition if available else None, "within": within if available else None,
            "sum": composition + within if available else None}


def _decompositions(rows: Sequence[Mapping[str, Any]], calendar: Mapping[str, Any], regimes: Mapping[str, Any]) -> dict[str, Any]:
    long_rows, short_rows = [row for row in rows if row["direction"] == "long"], [row for row in rows if row["direction"] == "short"]
    win, loss = _product_components(long_rows, short_rows, lambda value: value > 0), _product_components(long_rows, short_rows, lambda value: value < 0)
    baseline_gap = _metrics(long_rows)["expectancy_r"] - _metrics(short_rows)["expectancy_r"]
    components_sum = win["sum"] + loss["sum"]
    if not math.isclose(components_sum, baseline_gap, rel_tol=1e-12, abs_tol=1e-12): raise ValueError("exact symmetric win/loss components do not sum to expectancy gap")
    stop = _composition_within(long_rows, short_rows, lambda row: "stop" if row["stop_hit"] else "nonstop", ("stop", "nonstop"))
    path = _composition_within(long_rows, short_rows, "path", ("immediate_open", "armed_then_opened"))
    regime = {name: _composition_within(long_rows, short_rows, f"{name}_bucket", buckets) for name, buckets in (("bias_persistence_hours", ("low", "medium", "high", "missing")), ("atr_at_setup", ("low", "medium", "high", "missing")), ("broad_market_direction_32", ("down", "flat", "up", "missing")))}
    for name, value in {"stop/nonstop": stop, "setup path": path, **regime}.items():
        if value["available"] and not math.isclose(float(value["sum"]), baseline_gap, rel_tol=1e-12, abs_tol=1e-12):
            raise ValueError(f"exact {name} decomposition does not sum to expectancy gap")
    quarter_gaps = {label: {"long_total_r": table["long"]["total_r"], "short_total_r": table["short"]["total_r"], "total_r_gap": table["long"]["total_r"] - table["short"]["total_r"]} for label, table in calendar["quarter"].items()}
    tails = {direction: _tail([row for row in rows if row["direction"] == direction]) for direction in ("long", "short")}
    post_top5_gap = tails["long"]["top_5"]["remaining_total_r"] - tails["short"]["top_5"]["remaining_total_r"]
    return {"baseline_expectancy_gap": baseline_gap, "baseline_total_r_gap": _metrics(long_rows)["total_r"] - _metrics(short_rows)["total_r"],
            "direct_arithmetic": {"winner_frequency": win["frequency"], "winner_magnitude": win["magnitude"], "loss_frequency": loss["frequency"], "loss_magnitude": loss["magnitude"], "sum": components_sum},
            "stop_nonstop": stop, "setup_path": path, "regime": regime,
            "descriptive_associations": {"calendar_quarter_total_r_gap": quarter_gaps, "top_5_each_direction_post_removal_total_r_gap": post_top5_gap}}


def _classifications(rows: Sequence[Mapping[str, Any]], chronology: Mapping[str, Any], calendar: Mapping[str, Any], decompositions: Mapping[str, Any], regimes: Mapping[str, Any]) -> dict[str, Any]:
    gap, total_gap = float(decompositions["baseline_expectancy_gap"]), float(decompositions["baseline_total_r_gap"])
    labels: list[str] = []; evidence: dict[str, Any] = {}; unresolved = gap == 0
    quartile_gaps = [table["long"]["expectancy_r"] - table["short"]["expectancy_r"] for table in chronology["quartiles"].values() if not table["long"]["small_sample"] and not table["short"]["small_sample"]]
    persistent = gap > 0 and len(quartile_gaps) >= 2 and all(value > 0 for value in quartile_gaps)
    evidence["persistent_diagnostic"] = {"value": persistent, "interpretable_quartiles": len(quartile_gaps), "gaps": quartile_gaps}
    quarter_gaps = decompositions["descriptive_associations"]["calendar_quarter_total_r_gap"]
    positive = sorted((float(value["total_r_gap"]) for value in quarter_gaps.values() if value["total_r_gap"] > 0), reverse=True)
    temporal_ratio = None if not positive else sum(positive[:2]) / sum(positive)
    if not persistent and len(quarter_gaps) >= 3 and temporal_ratio is not None and temporal_ratio >= TEMPORAL_CONCENTRATION: labels.append("TEMPORALLY_CONCENTRATED")
    direct = decompositions["direct_arithmetic"]
    loss_contribution = max(0.0, direct["loss_frequency"] + direct["loss_magnitude"])
    loss_ratio = None if gap <= 0 else loss_contribution / gap
    if loss_ratio is not None and loss_ratio >= DOMINANCE and loss_contribution > max(0.0, direct["winner_frequency"]) and loss_contribution > max(0.0, direct["winner_magnitude"]): labels.append("LOSS_STRUCTURE_DOMINATED")
    tail_ratio = None if total_gap <= 0 else (total_gap - decompositions["descriptive_associations"]["top_5_each_direction_post_removal_total_r_gap"]) / total_gap
    winner_ratio = None if gap <= 0 else direct["winner_magnitude"] / gap
    if (tail_ratio is not None and tail_ratio >= DOMINANCE) or (winner_ratio is not None and winner_ratio >= DOMINANCE): labels.append("POSITIVE_TAIL_DOMINATED")
    path_effect = decompositions["setup_path"]["composition"]
    path_ratio = None if gap == 0 or path_effect is None else abs(path_effect) / abs(gap)
    if path_ratio is not None and path_ratio >= ASSOCIATION: labels.append("SETUP_COMPOSITION_ASSOCIATED")
    regime_hits = {}
    for name, table in regimes.items():
        paired = [bucket for bucket, values in table.items() if not values["long"]["small_sample"] and not values["short"]["small_sample"]]
        values = [table[bucket]["long"]["expectancy_r"] - table[bucket]["short"]["expectancy_r"] for bucket in paired]
        effect = decompositions["regime"][name]["composition"]
        comp_ratio = None if gap == 0 or effect is None else abs(effect) / abs(gap)
        gap_range = None if not values else max(values) - min(values)
        sign_change = len(values) >= 2 and min(values) * max(values) < 0
        rule_triggered = len(values) >= 2 and (
            (gap_range is not None and gap_range >= DOMINANCE * abs(gap) and sign_change)
            or (comp_ratio is not None and comp_ratio >= ASSOCIATION)
        )
        regime_hits[name] = {"interpretable_buckets": paired, "gap_range": gap_range,
                             "sign_change": sign_change, "composition_ratio": comp_ratio,
                             "decomposition_available": decompositions["regime"][name]["available"],
                             "rule_triggered": rule_triggered}
        if rule_triggered:
            labels.append("REGIME_ASSOCIATED")
    direct_shares = {key: None if gap == 0 else direct[key] / gap for key in ("winner_frequency", "winner_magnitude", "loss_frequency", "loss_magnitude")}
    shares = [abs(value) for value in direct_shares.values() if value is not None]
    if (sum(share >= ASSOCIATION for share in shares) >= 2 and all(share < DOMINANCE for share in shares)) or len(set(labels) & {"TEMPORALLY_CONCENTRATED", "LOSS_STRUCTURE_DOMINATED", "POSITIVE_TAIL_DOMINATED", "SETUP_COMPOSITION_ASSOCIATED", "REGIME_ASSOCIATED"}) >= 2: labels.append("BROAD_MULTIFACTOR")
    if unresolved or not labels: labels.append("INSUFFICIENT_UNRESOLVED")
    triggered = [name for name, value in regime_hits.items() if value["rule_triggered"]]
    evidence.update({"temporal_ratio": temporal_ratio, "loss_ratio": loss_ratio, "tail_ratio": tail_ratio, "winner_magnitude_ratio": winner_ratio, "path_composition_ratio": path_ratio, "regime": regime_hits, "triggered_regime_families": triggered, "direct_component_shares": direct_shares})
    return {"labels": sorted(set(labels)), "evidence": evidence, "zero_baseline_gap_unresolved": unresolved}


def _assert_historical_reproduction(rows: Sequence[Mapping[str, Any]], setups: Sequence[Mapping[str, Any]], observed: int, root: Path) -> None:
    frozen = json.loads((root / HISTORICAL_RESULT_PATH).read_bytes())
    if canonical_json(list(rows)) != canonical_json(frozen["closed_trade_ledger"]):
        raise ValueError("regenerated frozen historical ledger identity mismatch")
    eligible = sum(row.get("path") in {"immediate_open", "armed_then_opened", "armed_then_cancelled"} for row in setups)
    population = {"observed_setups": observed, "eligible_setups": eligible, "opened_trades": len(rows), "closed_trades": len(_closed(rows)), "censored_trades": len(rows) - len(_closed(rows))}
    if population != EXPECTED_POPULATION:
        raise ValueError("regenerated historical population mismatch")
    actual = {"aggregate": _metrics(rows), "long": _metrics([row for row in rows if row["direction"] == "long"]), "short": _metrics([row for row in rows if row["direction"] == "short"])}
    for segment, expected in HISTORICAL_HEADLINES.items():
        for key, value in expected.items():
            if key == "closed_trades":
                if actual[segment][key] != value: raise ValueError("historical headline count mismatch")
            elif not math.isclose(float(actual[segment][key]), value, rel_tol=1e-12, abs_tol=1e-12):
                raise ValueError("historical headline value mismatch")
            frozen_value = frozen["aggregate" if segment == "aggregate" else "direction"][key] if segment == "aggregate" else frozen["direction"][segment][key]
            if actual[segment][key] != frozen_value if key == "closed_trades" else not math.isclose(float(actual[segment][key]), float(frozen_value), rel_tol=1e-12, abs_tol=1e-12):
                raise ValueError("regenerated headline differs from frozen historical result")


def _headline_reproduction(rows: Sequence[Mapping[str, Any]]) -> dict[str, Any]:
    actual = {"aggregate": _metrics(rows), "long": _metrics([row for row in rows if row["direction"] == "long"]), "short": _metrics([row for row in rows if row["direction"] == "short"])}
    cells = {}
    for segment, expected in HISTORICAL_HEADLINES.items():
        cells[segment] = {key: {"actual": actual[segment][key], "expected": value,
                                "status": "PASS" if (actual[segment][key] == value if key == "closed_trades" else math.isclose(float(actual[segment][key]), value, rel_tol=1e-12, abs_tol=1e-12)) else "FAIL"} for key, value in expected.items()}
    if any(item["status"] != "PASS" for segment in cells.values() for item in segment.values()):
        raise ValueError("headline reproduction serialization gate failed")
    return {"actual": actual, "expected": HISTORICAL_HEADLINES, "tolerance": {"relative": 1e-12, "absolute": 1e-12}, "status": "PASS", "cells": cells}


def _later_contrast(root: Path, directional: Mapping[str, Any]) -> dict[str, Any]:
    compatibility = json.loads((root / COMPATIBILITY_RESULT_PATH).read_bytes())["provider_economics"]["xm"]["by_direction"]
    fields = ("closed_trades", "total_r", "expectancy_r", "profit_factor", "win_rate", "stop_rate", "winners_ge_2r", "winners_ge_3r", "winners_ge_5r", "positive_r_mean", "negative_r_mean")
    output = {}
    for direction in ("long", "short"):
        historical_values = directional[direction] | {"winners_ge_2r": directional[direction]["2r_plus"], "winners_ge_3r": directional[direction]["3r_plus"], "winners_ge_5r": directional[direction]["5r_plus"], "positive_r_mean": directional[direction]["mean_winning_r"], "negative_r_mean": directional[direction]["mean_losing_r"]}
        later = compatibility[direction]
        aligned = {field: {"historical": historical_values.get(field), "later_compatibility": later.get(field), "later_minus_historical": None if historical_values.get(field) is None or later.get(field) is None else later[field] - historical_values[field]} for field in fields}
        output[direction] = aligned
    return {"label": "DESCRIPTIVE CROSS-PERIOD CONTRAST — NOT VALIDATION OF A NEW HYPOTHESIS", "separate_not_validation": True, "merge_with_historical": False, "source": "frozen compatibility provider_economics.xm.by_direction", "by_direction": output}


def _hypotheses(classification: Mapping[str, Any], decompositions: Mapping[str, Any]) -> list[dict[str, Any]]:
    common = {"plausible_confounders": ["shared regime clustering", "finite-sample tail dependence", "provider/sample-period differences", "overlapping descriptive decompositions"],
              "future_confirmatory_test": "A separately predeclared directional-hypothesis study with no parameter tuning or production rule change.",
              "independent_untouched_data_needed": "New untouched XAU data collected or acquired after the confirmatory protocol is frozen.",
              "discovery_disclaimer": "This hypothesis was generated using the historical attribution population and is NOT TESTED."}
    output = []
    for label in classification["labels"]:
        if label == "INSUFFICIENT_UNRESOLVED":
            continue
        if label == "REGIME_ASSOCIATED":
            evidence = {name: classification["evidence"]["regime"][name] for name in classification["evidence"]["triggered_regime_families"]}
            output.append({"label": label, "observation": "Predeclared regime family differences met the locked descriptive association rule.", "possible_mechanism": "Directional payoff may vary with the frozen setup-origin regime context.", "evidence_path": "classification.evidence.regime and directional_gap_decompositions.regime", "evidence_values": evidence, **common})
        elif label == "BROAD_MULTIFACTOR":
            output.append({"label": label, "observation": "No single locked direct component dominates the directional gap.", "possible_mechanism": "Several overlapping descriptive mechanisms may jointly coincide with the gap.", "evidence_path": "directional_gap_decompositions.direct_arithmetic and classification.evidence.direct_component_shares", "evidence_values": {"components": decompositions["direct_arithmetic"], "shares": classification["evidence"]["direct_component_shares"], "labels": classification["labels"]}, **common})
        else:
            output.append({"label": label, "observation": f"The locked {label} rule was mechanically triggered.", "possible_mechanism": "The corresponding descriptive association may be sample-dependent rather than causal.", "evidence_path": "classification.evidence", "evidence_values": classification["evidence"], **common})
    return output


def _build_xau_directional_edge_attribution_result_unchecked(*, repo_root: Path, xm_m1_source: Path, protocol: Mapping[str, Any], guard: Mapping[str, Any]) -> dict[str, Any]:
    """Internal deterministic builder; caller must have authorized economics."""
    root = repo_root.resolve()
    raw = historical._load_full_xm_m1(xm_m1_source.resolve())
    ltf, htf, _excluded, aggregation = historical._replay_inputs(raw)
    replay, warmup = historical._run_warmed_replay(ltf, htf)
    rows, setups, observed = historical._ledger(replay)
    _assert_historical_reproduction(rows, setups, observed, root)
    contexts, population = extract_setup_contexts(replay)
    if population != EXPECTED_POPULATION or _context_lock_from_contexts(contexts, population, int(warmup["first_strategy_eligible_timestamp"])) != EXPECTED_CONTEXT_LOCK:
        raise ValueError("Stage B context/population reproduction mismatch")
    eligible_contexts = _eligible_setup_context_ledger(contexts, protocol)
    if len(eligible_contexts) != EXPECTED_POPULATION["eligible_setups"]:
        raise ValueError("eligible setup context ledger count mismatch")
    enriched = _enriched_ledger(rows, replay, contexts, protocol)
    directional, calendar = _by_direction(enriched, contexts), _calendar_tables(enriched)
    chronology, paths, exits = _chronological_table(enriched), _setup_path_table(enriched, contexts), _exit_and_holding(enriched)
    regimes = _regime_tables(enriched, contexts)
    decompositions = _decompositions(enriched, calendar, regimes)
    classification = _classifications(enriched, chronology, calendar, decompositions, regimes)
    contrast = _later_contrast(root, directional)
    headlines = _headline_reproduction(enriched)
    broad_missing = EXPECTED_CONTEXT_LOCK["broad_market_direction_32"]["counts"]["missing"]
    stopped_missing = sum(int(exits[direction]["stopped_trade_diagnostics"]["missing_path_count"]) for direction in ("long", "short"))
    return {"schema_version": "xau-directional-edge-attribution-result/v1", "decision_authority": "Sol/main only; this result does not authorize production changes",
        "metadata": {"canonical_starting_sha": CANONICAL_STARTING_SHA, "protocol_commit_sha": PROTOCOL_COMMIT_SHA, "execution_head_sha": guard["head"], "protocol_sha256": EXPECTED_PROTOCOL_SHA256, "result_ordering": "canonical JSON sort_keys=true, UTF-8 terminal newline"},
        "source_sha256": guard["source_sha256"], "population_reproduction": {**EXPECTED_POPULATION, "warmup": warmup, "aggregation": aggregation, "historical_ledger_identity": "exact", "headline_reproduction": headlines},
        "aggregate_baseline": _cell(_metrics(enriched, eligible_setups=EXPECTED_POPULATION["eligible_setups"])), "directional_baseline": directional, "calendar": calendar, "chronological": chronology, "setup_path": paths, "exit_loss_and_holding": exits, "regime": regimes,
        "interactions": {"direction_x_chronological_quartile": chronology["quartiles"], "direction_x_primary_regime": regimes},
        "directional_gap_decompositions": decompositions, "later_period_directional_contrast": contrast,
        "classification": classification, "hypotheses_generated_not_tested": _hypotheses(classification, decompositions),
        "enriched_closed_trade_ledger": enriched, "eligible_setup_context_ledger": eligible_contexts,
        "limitations": ["All associations are descriptive and noncausal; hypotheses use the discovery sample.", "Regime values are setup-origin context only and use no future information.", f"Broad-market direction context is missing for {broad_missing} eligible setups.", f"Stopped-trade MFE/MAE has {stopped_missing} missing contiguous paths.", "Bar-level MFE/MAE cannot resolve intrabar path ordering beyond the frozen stop-first semantics.", "Later compatibility contrast is limited to frozen provider direction headlines and is not validation.", "No broker-cost calibration was performed.", "Cells with n<30 are SMALL-SAMPLE / DESCRIPTIVE ONLY."],
        "non_optimization": "No counterfactual strategy variant, directional filter, LONG-only/SHORT-disable test, XAU tuning, or broker-cost calibration was run.",
        "restriction_state": {"production_strategy_change": "NO", "directional_filter_tested": "NO", "long_only_or_short_disable_tested": "NO", "xau_specific_tuning": "NO", "broker_cost_calibration": "NO", "btc_phase_7_4": "UNTOUCHED"}}


def build_xau_directional_edge_attribution_result(*, repo_root: Path, xm_m1_source: Path, protocol_path: Path) -> dict[str, Any]:
    """Guarded Stage B execution. This is the sole official economics entrypoint."""
    guard = _verify_stage_b_guard(repo_root=repo_root, protocol_path=protocol_path, xm_m1_source=xm_m1_source)
    return _build_xau_directional_edge_attribution_result_unchecked(
        repo_root=repo_root, xm_m1_source=xm_m1_source, protocol=guard["protocol"], guard=guard,
    )


def xau_directional_edge_attribution_json(result: Mapping[str, Any]) -> bytes:
    return (canonical_json(dict(result)) + "\n").encode("utf-8")


def _reconcile_context_ledger(items: Sequence[Mapping[str, Any]]) -> list[dict[str, Any]]:
    if len(items) != EXPECTED_POPULATION["eligible_setups"]:
        raise ValueError("eligible setup context ledger length mismatch")
    required = {"timestamp", "direction", "path", "bias_persistence_hours", "atr_at_setup", "broad_market_direction_32", "bias_persistence_hours_bucket", "atr_at_setup_bucket", "broad_market_direction_32_bucket"}
    if any(not required <= set(item) for item in items): raise ValueError("eligible setup context ledger fields missing")
    normalized = [dict(item) for item in items]
    if normalized != sorted(normalized, key=lambda row: int(row["timestamp"])) or len({int(row["timestamp"]) for row in normalized}) != len(normalized):
        raise ValueError("eligible setup context ledger order/identity mismatch")
    if any(row["direction"] not in {"long", "short"} or row["path"] not in {"immediate_open", "armed_then_opened", "armed_then_cancelled"} for row in normalized):
        raise ValueError("eligible setup context ledger semantics mismatch")
    expected = EXPECTED_CONTEXT_LOCK
    continuous = {"bias_persistence_hours": ("bias_persistence_hours_bucket", "q1", "q2"), "atr_at_setup": ("atr_at_setup_bucket", "q1", "q2")}
    for name, (bucket_key, q1_key, q2_key) in continuous.items():
        values = [float(row[name]) for row in normalized if row[name] is not None]
        family = expected[name]
        if len(values) != family["nonmissing"] or Counter(str(row[bucket_key]) for row in normalized) != Counter(family["counts"]):
            raise ValueError("eligible setup context continuous bucket counts mismatch")
        if type_7_quantile(values, 1 / 3) != family[q1_key] or type_7_quantile(values, 2 / 3) != family[q2_key]:
            raise ValueError("eligible setup context boundary mismatch")
        if any(row[bucket_key] != tertile_bin(row[name], family[q1_key], family[q2_key]) for row in normalized):
            raise ValueError("eligible setup context bucket mismatch")
    if any(row["broad_market_direction_32"] != row["broad_market_direction_32_bucket"]
           or row["broad_market_direction_32"] not in {"down", "flat", "up", "missing"}
           for row in normalized):
        raise ValueError("eligible setup context direction value mismatch")
    if Counter(str(row["broad_market_direction_32_bucket"]) for row in normalized) != Counter(expected["broad_market_direction_32"]["counts"]):
        raise ValueError("eligible setup context direction bucket mismatch")
    return normalized


def _reconcile_closed_ledger(items: Sequence[Mapping[str, Any]], contexts: Sequence[Mapping[str, Any]]) -> list[dict[str, Any]]:
    if len(items) != EXPECTED_POPULATION["closed_trades"]:
        raise ValueError("closed ledger length mismatch")
    rows = [dict(item) for item in items]
    if any(row.get("outcome") != "closed" for row in rows) or len({str(row.get("trade_id")) for row in rows}) != len(rows):
        raise ValueError("closed ledger outcome/identity mismatch")
    if rows != sorted(rows, key=lambda row: (int(row["entry_timestamp"]), str(row["trade_id"]))):
        raise ValueError("closed ledger deterministic order mismatch")
    context_by_timestamp = {int(row["timestamp"]): row for row in contexts}
    for row in rows:
        context = context_by_timestamp.get(int(row.get("setup_origin_timestamp", -1)))
        if context is None or row.get("direction") != context["direction"] or row.get("path") != context["path"]:
            raise ValueError("closed ledger setup context mismatch")
        for name in ("bias_persistence_hours", "atr_at_setup", "broad_market_direction_32", "bias_persistence_hours_bucket", "atr_at_setup_bucket", "broad_market_direction_32_bucket"):
            if row.get(name) != context[name]: raise ValueError("closed ledger context value mismatch")
        if row.get("mfe_r") is not None and (float(row["mfe_r"]) < 0 or float(row.get("mae_r", -1)) < 0):
            raise ValueError("closed ledger excursion must be nonnegative")
    return rows


def _assert_exact_section(actual: Any, expected: Any, name: str) -> None:
    if canonical_json(actual) != canonical_json(expected):
        raise ValueError(f"result {name} reconciliation mismatch")


def verify_xau_directional_edge_attribution_result(result: Mapping[str, Any], *, repo_root: Path = Path(".")) -> None:
    """Fail closed on an incomplete, noncanonical, or unpinned Stage B result."""
    if result.get("schema_version") != "xau-directional-edge-attribution-result/v1":
        raise ValueError("result schema mismatch")
    payload = xau_directional_edge_attribution_json(result)
    if not payload.endswith(b"\n") or json.loads(payload) != result:
        raise ValueError("result serialization is not canonical")
    metadata = result.get("metadata", {})
    if metadata.get("canonical_starting_sha") != CANONICAL_STARTING_SHA or metadata.get("protocol_commit_sha") != PROTOCOL_COMMIT_SHA or metadata.get("protocol_sha256") != EXPECTED_PROTOCOL_SHA256:
        raise ValueError("result pinned protocol metadata mismatch")
    if result.get("source_sha256") != EXPECTED_SOURCE_SHA256:
        raise ValueError("result frozen source metadata mismatch")
    population = result.get("population_reproduction", {})
    headline = population.get("headline_reproduction", {})
    if {key: population.get(key) for key in EXPECTED_POPULATION} != EXPECTED_POPULATION or headline.get("status") != "PASS" or headline.get("expected") != HISTORICAL_HEADLINES:
        raise ValueError("result population/headline reproduction mismatch")
    for segment, expected in HISTORICAL_HEADLINES.items():
        for key, value in expected.items():
            cell = headline.get("cells", {}).get(segment, {}).get(key, {})
            if cell.get("status") != "PASS":
                raise ValueError("result exact headline cell mismatch")
            if key == "closed_trades":
                if cell.get("actual") != value: raise ValueError("result exact headline cell mismatch")
            elif cell.get("actual") is None or not math.isclose(float(cell["actual"]), value, rel_tol=1e-12, abs_tol=1e-12):
                raise ValueError("result exact headline cell mismatch")
    required = {"aggregate_baseline", "directional_baseline", "calendar", "chronological", "setup_path", "exit_loss_and_holding", "regime", "interactions", "directional_gap_decompositions", "later_period_directional_contrast", "classification", "hypotheses_generated_not_tested", "enriched_closed_trade_ledger", "eligible_setup_context_ledger", "limitations", "non_optimization", "restriction_state"}
    if not required <= set(result):
        raise ValueError("result required sections missing")
    restrictions = result["restriction_state"]
    if restrictions != {"production_strategy_change": "NO", "directional_filter_tested": "NO", "long_only_or_short_disable_tested": "NO", "xau_specific_tuning": "NO", "broker_cost_calibration": "NO", "btc_phase_7_4": "UNTOUCHED"}:
        raise ValueError("result restriction state mismatch")
    if "No counterfactual strategy variant" not in str(result["non_optimization"]):
        raise ValueError("result non-optimization declaration mismatch")
    root = repo_root.resolve()
    if _hash(root / COMPATIBILITY_RESULT_PATH) != EXPECTED_SOURCE_SHA256[COMPATIBILITY_RESULT_PATH]:
        raise ValueError("frozen compatibility artifact identity mismatch")
    contexts = _reconcile_context_ledger(result["eligible_setup_context_ledger"])
    rows = _reconcile_closed_ledger(result["enriched_closed_trade_ledger"], contexts)
    directional, calendar = _by_direction(rows, contexts), _calendar_tables(rows)
    chronology, paths, exits = _chronological_table(rows), _setup_path_table(rows, contexts), _exit_and_holding(rows)
    regimes = _regime_tables(rows, contexts)
    decompositions = _decompositions(rows, calendar, regimes)
    classification = _classifications(rows, chronology, calendar, decompositions, regimes)
    contrast = _later_contrast(root, directional)
    headlines = _headline_reproduction(rows)
    broad_missing = EXPECTED_CONTEXT_LOCK["broad_market_direction_32"]["counts"]["missing"]
    stopped_missing = sum(int(exits[direction]["stopped_trade_diagnostics"]["missing_path_count"]) for direction in ("long", "short"))
    expected_sections = {
        "aggregate_baseline": _cell(_metrics(rows, eligible_setups=EXPECTED_POPULATION["eligible_setups"])),
        "directional_baseline": directional, "calendar": calendar, "chronological": chronology,
        "setup_path": paths, "exit_loss_and_holding": exits, "regime": regimes,
        "interactions": {"direction_x_chronological_quartile": chronology["quartiles"], "direction_x_primary_regime": regimes},
        "directional_gap_decompositions": decompositions, "later_period_directional_contrast": contrast,
        "classification": classification, "hypotheses_generated_not_tested": _hypotheses(classification, decompositions),
        "limitations": ["All associations are descriptive and noncausal; hypotheses use the discovery sample.", "Regime values are setup-origin context only and use no future information.", f"Broad-market direction context is missing for {broad_missing} eligible setups.", f"Stopped-trade MFE/MAE has {stopped_missing} missing contiguous paths.", "Bar-level MFE/MAE cannot resolve intrabar path ordering beyond the frozen stop-first semantics.", "Later compatibility contrast is limited to frozen provider direction headlines and is not validation.", "No broker-cost calibration was performed.", "Cells with n<30 are SMALL-SAMPLE / DESCRIPTIVE ONLY."],
    }
    for name, expected in expected_sections.items(): _assert_exact_section(result[name], expected, name)
    _assert_exact_section(population["headline_reproduction"], headlines, "headline reproduction")


def write_xau_directional_edge_attribution_result(result: Mapping[str, Any], path: Path, *, repo_root: Path = Path(".")) -> str:
    verify_xau_directional_edge_attribution_result(result, repo_root=repo_root)
    if path.exists(): raise FileExistsError(f"refusing to overwrite attribution result: {path}")
    payload = xau_directional_edge_attribution_json(result)
    path.parent.mkdir(parents=True, exist_ok=True); path.write_bytes(payload)
    return sha256(payload).hexdigest()
