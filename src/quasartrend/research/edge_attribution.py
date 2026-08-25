"""Phase 7.3 deterministic descriptive edge-attribution report.

This is deliberately an accounting view over the frozen canonical population.
It neither constructs candidates nor calls a candidate evaluator.
"""
from __future__ import annotations

from collections import Counter
from dataclasses import asdict
from hashlib import sha256
from datetime import UTC, datetime
import json
import math
from pathlib import Path
from statistics import mean, median
from typing import Callable

from .failure_modes import FAILURE_MODE_ORDER, failure_mode, validate_failure_partition
from .pipeline import CanonicalResearchBundle, baseline_report
from .provenance import fingerprint
from .regime_diagnosis import build_diagnosis_evidence
from .regime_features import (
    FeatureDefinitionArtifact, SetupRegimeFeatureRow,
    build_setup_regime_feature_rows, feature_definition_artifact,
    validate_regime_feature_artifact,
)
from .splits import CANONICAL_WINDOWS, ChronologicalWindow


SCHEMA_VERSION = "phase7.3-edge-attribution/v1"
PHASE7_2_BASE_SHA = "03235d2c2887075df94df0cd0ec960442623c417"
BAR_MS = 900_000
_TOL = 1e-12
ATR_EXTENSION_EDGES = (3.522108008530367, 3.992676256992967, 4.917967689345026)
STOP_ADR_RATIO_EDGES = (.0702042096073447, .09338718609335621, .1292782711341422)
PHASE72_FEATURE_ARTIFACT_SHA256 = "32b58f6a478ecdb3cd857900a343048ea79261ab2e9af13a2defda6799784822"


def _finite(value: object) -> float | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    value = float(value)
    return value if math.isfinite(value) else None


def _ratio(numerator: float | int, denominator: float | int) -> float | None:
    return None if denominator == 0 else float(numerator) / float(denominator)


def _mean_median(values: list[float]) -> dict[str, float | None]:
    return {"mean": None if not values else float(mean(values)), "median": None if not values else float(median(values))}


def _record(setup: object, trade: object | None, feature: object) -> dict[str, object]:
    return {"setup": setup, "trade": trade, "feature": feature}


def _closed(records: tuple[dict[str, object], ...]) -> tuple[object, ...]:
    return tuple(record["trade"] for record in records if record["trade"] is not None and getattr(record["trade"], "outcome_state") == "closed")


def _population(records: tuple[dict[str, object], ...], all_closed: tuple[object, ...]) -> dict[str, object]:
    trades = tuple(record["trade"] for record in records if record["trade"] is not None)
    closed = _closed(records)
    censored = tuple(trade for trade in trades if getattr(trade, "outcome_state") == "censored")
    values = [float(getattr(trade, "realized_r")) for trade in closed]
    positives = [value for value in values if value > 0.0]
    negatives = [-value for value in values if value < 0.0]
    mae = [value for trade in closed if (value := _finite(getattr(trade, "mae_r"))) is not None]
    mfe = [value for trade in closed if (value := _finite(getattr(trade, "mfe_r"))) is not None]
    durations = [float(value) for trade in closed if (value := _finite(getattr(trade, "observed_duration_bars"))) is not None]
    total_negative = sum(-float(getattr(trade, "realized_r")) for trade in all_closed if float(getattr(trade, "realized_r")) < 0.0)
    total_positive = sum(float(getattr(trade, "realized_r")) for trade in all_closed if float(getattr(trade, "realized_r")) > 0.0)
    stop_count = sum(getattr(trade, "stop_hit") is True for trade in closed)
    tails = {}
    for threshold in (2.0, 3.0, 5.0):
        matched = [value for value in values if value >= threshold]
        tails[f"ge_{int(threshold)}r"] = {"count": len(matched), "total_r": float(sum(matched)), "denominator_label": "all_positive_r", "denominator_r": total_positive, "share_of_all_positive_r": _ratio(sum(matched), total_positive)}
    winners = _top(tuple(row for row in all_closed if float(row.realized_r) > 0.0), reverse=True, count=len(all_closed))
    for count in (5, 10):
        members = winners[:count]; member_ids = {row.trade_id for row in members}
        member_r = sum(float(row.realized_r) for row in closed if row.trade_id in member_ids)
        total_member_r = sum(float(row.realized_r) for row in members)
        tails[f"global_top_{count}_winner_membership"] = {"count": sum(row.trade_id in member_ids for row in closed), "member_total_r": float(member_r), "denominator_label": "all_positive_r", "denominator_r": total_positive, "share_of_all_positive_r": _ratio(member_r, total_positive), "global_top_n_denominator_label": f"global_top_{count}_winner_r", "global_top_n_denominator_r": float(total_member_r), "share_of_global_top_n_r": _ratio(member_r, total_member_r)}
    return {
        "eligible_setups": len(records), "setup_share": None, "opened_trades": len(trades),
        "closed_trades": len(closed), "closed_trade_share": _ratio(len(closed), len(all_closed)),
        "censored_trades": len(censored), "expectancy_r": _ratio(sum(values), len(values)),
        "total_r": float(sum(values)), "r_per_setup": _ratio(sum(values), len(records)),
        "profit_factor": None if not negatives else float(sum(positives) / sum(negatives)),
        "win_rate": _ratio(sum(value > 0.0 for value in values), len(values)), "stop_rate": _ratio(stop_count, len(values)),
        "positive_r": float(sum(positives)), "positive_r_share": _ratio(sum(positives), total_positive),
        "negative_r_magnitude": float(sum(negatives)), "negative_r_magnitude_share": _ratio(sum(negatives), total_negative),
        "mean_mae_r": _mean_median(mae)["mean"], "median_mae_r": _mean_median(mae)["median"],
        "mean_mfe_r": _mean_median(mfe)["mean"], "median_mfe_r": _mean_median(mfe)["median"],
        "mean_duration_bars": _mean_median(durations)["mean"], "median_duration_bars": _mean_median(durations)["median"], "mean_elapsed_duration_ms": _mean_median([float(x.elapsed_duration_ms) for x in closed if _finite(x.elapsed_duration_ms) is not None])["mean"], "median_elapsed_duration_ms": _mean_median([float(x.elapsed_duration_ms) for x in closed if _finite(x.elapsed_duration_ms) is not None])["median"],
        "giveback": {"count": sum(_finite(x.mfe_r) is not None for x in closed), "missing": sum(_finite(x.mfe_r) is None for x in closed), **_mean_median([float(x.mfe_r) - float(x.realized_r) for x in closed if _finite(x.mfe_r) is not None])},
        "winners_ge_2r": sum(value >= 2.0 for value in values), "winners_ge_3r": sum(value >= 3.0 for value in values),
        "winners_ge_5r": sum(value >= 5.0 for value in values), "maximum_r": None if not values else max(values),
        "tail_attribution": tails,
    }


def _bucket_table(name: str, records: tuple[dict[str, object], ...], all_closed: tuple[object, ...], classifier: Callable[[dict[str, object]], str], order: tuple[str, ...], *, source: str, universe_label: str = "eligible_setups", universe_count: int | None = None, excluded: int = 0, reasons: dict[str, int] | None = None) -> dict[str, object]:
    grouped = {label: tuple(record for record in records if classifier(record) == label) for label in order}
    if sum(len(value) for value in grouped.values()) != len(records):
        raise ValueError(f"{name} buckets do not exhaust eligible setup population")
    buckets = []
    for label in order:
        item = _population(grouped[label], all_closed)
        item["label"] = label
        item["setup_share"] = _ratio(len(grouped[label]), len(records))
        buckets.append(item)
    missing = sum(classifier(r) in {"missing", "missing_or_invalid"} for r in records)
    accounting = {"universe_label": universe_label, "universe_count": len(records) if universe_count is None else universe_count, "included": len(records) - missing, "missing": missing, "excluded": excluded, "reasons": {} if reasons is None else dict(sorted(reasons.items())), "realized_closed": len(_closed(records)), "censored": sum(r["trade"] is not None and getattr(r["trade"], "outcome_state") == "censored" for r in records)}
    return {"dimension": name, "source": source, "partition": "mutually_exclusive_exhaustive", "population_accounting": accounting, "buckets": buckets}


def _trade_value(record: dict[str, object], name: str) -> object | None:
    trade = record["trade"]
    return None if trade is None else getattr(trade, name)


def _direction_label(value: object) -> str:
    """Stable presentation labels independent of the enum's lowercase value."""
    raw = getattr(value, "value", value)
    if raw == "long": return "LONG"
    if raw == "short": return "SHORT"
    return "missing"


def _age_bucket(value: object) -> str:
    value = _finite(value)
    if value is None or value < 0.0 or value % BAR_MS != 0.0:
        return "missing_or_invalid"
    bars = int(value // BAR_MS)
    return "same_decision" if bars == 0 else "one_bar" if bars == 1 else "two_to_four" if bars <= 4 else "five_plus"


def _duration_bucket(value: object) -> str:
    value = _finite(value)
    if value is None or value < 0 or value != int(value): return "missing"
    bars = int(value)
    return "zero" if bars == 0 else "one_bar" if bars == 1 else "two_to_four" if bars <= 4 else "five_to_eight" if bars <= 8 else "nine_to_sixteen" if bars <= 16 else "seventeen_plus"


def _exit_bucket(record: dict[str, object]) -> str:
    trade = record["trade"]
    if trade is None or getattr(trade, "outcome_state") != "closed": return "other_or_missing"
    stop, strategy = getattr(trade, "stop_hit"), getattr(trade, "strategy_exit")
    if stop is True and strategy is False: return "stop_only"
    if stop is False and strategy is True: return "strategy_only"
    if stop is True and strategy is True: return "stop_and_strategy_same_bar"
    return "other_or_missing"


def _numeric_bucket(value: object, edges: tuple[float, ...], *, prefix: str = "Q") -> str:
    value = _finite(value)
    if value is None: return "missing"
    if not edges: return "nonmissing"
    for index, edge in enumerate(edges):
        if value <= edge: return f"{prefix}{index + 1}"
    return f"{prefix}{len(edges) + 1}"


def _fixed_bucket(value: object, edges: tuple[float, ...]) -> str:
    value = _finite(value)
    if value is None: return "missing"
    for edge in edges:
        if value < edge: return f"lt_{edge:g}"
    return f"ge_{edges[-1]:g}"


def _adr_extension_bucket(value: object) -> str:
    value = _finite(value)
    if value is None: return "missing"
    if value < .25: return "lt_0.25"
    if value < .5: return "gte_0.25_lt_0.5"
    if value <= 1.: return "gte_0.5_lte_1"
    return "gt_1"


def _context_classifiers(edges: dict[str, tuple[float, ...]]) -> tuple[tuple[str, Callable[[dict[str, object]], str], tuple[str, ...]], ...]:
    """The same stable bucket labels used by the main attribution tables."""
    result = [
        ("direction", lambda r: _direction_label(getattr(r["setup"], "direction")), ("LONG", "SHORT")),
        ("htf_bias_direction", lambda r: _direction_label(getattr(r["setup"], "htf_bias")), ("LONG", "SHORT", "missing")),
        ("setup_path", lambda r: "armed_opened" if getattr(r["setup"], "was_armed") else "immediate_opened", ("immediate_opened", "armed_opened")),
        ("setup_age", lambda r: _age_bucket(_trade_value(r, "setup_age_ms")), ("same_decision", "one_bar", "two_to_four", "five_plus", "missing_or_invalid")),
        ("kalman_transition_age", lambda r: _age_bucket(_trade_value(r, "kalman_transition_age_ms")), ("same_decision", "one_bar", "two_to_four", "five_plus", "missing_or_invalid")),
        ("utc_six_hour_bucket", lambda r: str(int(value)) if (value := _finite(_trade_value(r, "utc_six_hour_bucket"))) is not None else "missing", ("0", "1", "2", "3", "missing")),
        ("utc_weekday", lambda r: str(int(value)) if (value := _finite(_trade_value(r, "utc_weekday"))) is not None else "missing", ("0", "1", "2", "3", "4", "5", "6", "missing")),
    ]
    for field in ("kalman_persistence_bars", "hema_fast_slope_atr_8", "directional_efficiency_8", "directional_efficiency_16", "directional_efficiency_32", "hema_flip_count_16", "kalman_flip_count_16", "combined_flip_count_16", "atr_adr_ratio"):
        result.append((field, lambda r, f=field: _numeric_bucket(getattr(r["feature"], f), edges.get(f, ())), ("Q1", "Q2", "Q3", "Q4", "missing")))
    result.append(("hema_kalman_aligned", lambda r: "true" if getattr(r["feature"], "hema_kalman_aligned") is True else "false" if getattr(r["feature"], "hema_kalman_aligned") is False else "missing", ("false", "true", "missing")))
    result.extend((field, lambda r, f=field: _adr_extension_bucket(_trade_value(r, f)) if f == "adr_extension" else _numeric_bucket(_trade_value(r, f), ATR_EXTENSION_EDGES if f == "atr_extension" else STOP_ADR_RATIO_EDGES) if f in {"atr_extension", "stop_adr_ratio"} else "all_nonmissing" if _finite(_trade_value(r, f)) is not None else "missing", ("lt_0.25", "gte_0.25_lt_0.5", "gte_0.5_lte_1", "gt_1", "missing") if field == "adr_extension" else ("Q1", "Q2", "Q3", "Q4", "missing") if field in {"atr_extension", "stop_adr_ratio"} else ("all_nonmissing", "missing")) for field in ("adr_extension", "atr_extension", "stop_adr_ratio", "stop_atr_ratio", "atr_at_entry", "adr"))
    return tuple(result)


def _context_distribution(records: tuple[dict[str, object], ...], classifiers: tuple[tuple[str, Callable[[dict[str, object]], str], tuple[str, ...]], ...]) -> dict[str, dict[str, int]]:
    result = {}
    for name, classifier, labels in classifiers:
        distribution = {label: sum(classifier(record) == label for record in records) for label in labels}
        if sum(distribution.values()) != len(records):
            raise ValueError(f"{name} entry-context buckets do not exhaust cohort")
        result[name] = distribution
    return result


def _top(rows: tuple[object, ...], *, reverse: bool, count: int) -> tuple[object, ...]:
    return tuple(sorted(rows, key=lambda row: ((-1 if reverse else 1) * float(getattr(row, "realized_r")), getattr(row, "trade_id")))[:count])


def _concentration(closed: tuple[object, ...]) -> dict[str, object]:
    losers = _top(tuple(row for row in closed if float(row.realized_r) < 0.0), reverse=False, count=len(closed))
    winners = _top(tuple(row for row in closed if float(row.realized_r) > 0.0), reverse=True, count=len(closed))
    negative = sum(-float(row.realized_r) for row in losers); positive = sum(float(row.realized_r) for row in winners)
    return {"top_5_losers_share_negative_r": _ratio(sum(-float(row.realized_r) for row in losers[:5]), negative), "top_10_losers_share_negative_r": _ratio(sum(-float(row.realized_r) for row in losers[:10]), negative), "top_5_winners_share_positive_r": _ratio(sum(float(row.realized_r) for row in winners[:5]), positive), "top_10_winners_share_positive_r": _ratio(sum(float(row.realized_r) for row in winners[:10]), positive), "top_5_winners_total_r": float(sum(float(row.realized_r) for row in winners[:5])), "top_10_winners_total_r": float(sum(float(row.realized_r) for row in winners[:10]))}


def _failure_report(records: tuple[dict[str, object], ...], closed: tuple[object, ...], edges: dict[str, tuple[float, ...]]) -> dict[str, object]:
    losses = tuple(row for row in closed if float(row.realized_r) < 0.0)
    validate_failure_partition(losses)
    result = []
    for label in FAILURE_MODE_ORDER:
        subset = tuple(row for row in losses if failure_mode(row) == label)
        values = [float(row.realized_r) for row in subset]
        mfe = [_finite(row.mfe_r) for row in subset]; mae = [_finite(row.mae_r) for row in subset]; durations = [_finite(row.observed_duration_bars) for row in subset]
        member_ids = {row.trade_id for row in subset}
        selected = tuple(record for record in records if record["trade"] is not None and getattr(record["trade"], "trade_id") in member_ids)
        contexts = _context_distribution(selected, _context_classifiers(edges))
        result.append({"label": label, "count": len(subset), "loss_share": _ratio(len(subset), len(losses)), "total_r": float(sum(values)), "negative_r_magnitude": float(-sum(values)), "negative_r_magnitude_share": _ratio(-sum(values), -sum(float(row.realized_r) for row in losses)), "mean_mae_r": _mean_median([x for x in mae if x is not None])["mean"], "median_mae_r": _mean_median([x for x in mae if x is not None])["median"], "mean_mfe_r": _mean_median([x for x in mfe if x is not None])["mean"], "median_mfe_r": _mean_median([x for x in mfe if x is not None])["median"], "mean_duration_bars": _mean_median([x for x in durations if x is not None])["mean"], "median_duration_bars": _mean_median([x for x in durations if x is not None])["median"], "entry_context": contexts})
    return {"definition": {"F1_immediate_stop_failure": "stop_hit and MFE < 0.25R", "F2_weak_follow_through_then_stop": "stop_hit and 0.25R <= MFE < 1R", "F3_material_follow_through_then_loss": "stop_hit and MFE >= 1R", "F4_non_stop_strategy_loss": "non-stop closed loss"}, "losses": len(losses), "buckets": result}


def _stopped_report(closed: tuple[object, ...]) -> dict[str, object]:
    stopped = tuple(row for row in closed if row.stop_hit is True)
    mfe = [float(row.mfe_r) for row in stopped if _finite(row.mfe_r) is not None]
    duration = [float(row.observed_duration_bars) for row in stopped if _finite(row.observed_duration_bars) is not None]
    elapsed = [float(row.elapsed_duration_ms) for row in stopped if _finite(row.elapsed_duration_ms) is not None]
    classes = Counter(failure_mode(row) for row in stopped if float(row.realized_r) < 0.0)
    return {"stopped_trade_count": len(stopped), "mfe_missing": len(stopped) - len(mfe), "duration_missing": len(stopped) - len(duration), "total_r": float(sum(float(row.realized_r) for row in stopped)), "mean_mfe_r": _mean_median(mfe)["mean"], "median_mfe_r": _mean_median(mfe)["median"], "mean_time_to_stop_bars": _mean_median(duration)["mean"], "median_time_to_stop_bars": _mean_median(duration)["median"], "mean_time_to_stop_ms": _mean_median(elapsed)["mean"], "median_time_to_stop_ms": _mean_median(elapsed)["median"], "failure_class_counts": dict(sorted(classes.items())), "reached": [{"threshold_r": threshold, "count": sum(value >= threshold for value in mfe), "percentage": _ratio(sum(value >= threshold for value in mfe), len(stopped))} for threshold in (.25, .5, 1., 2.)]}


def _winner_anatomy(records: tuple[dict[str, object], ...], closed: tuple[object, ...], edges: dict[str, tuple[float, ...]]) -> dict[str, object]:
    positive = tuple(row for row in closed if float(row.realized_r) > 0.0)
    groups = (("profitable", positive), ("ge_2r", tuple(row for row in positive if float(row.realized_r) >= 2.0)), ("ge_3r", tuple(row for row in positive if float(row.realized_r) >= 3.0)), ("ge_5r", tuple(row for row in positive if float(row.realized_r) >= 5.0)), ("top_5", _top(positive, reverse=True, count=5)), ("top_10", _top(positive, reverse=True, count=10)))
    total_positive = sum(float(row.realized_r) for row in positive)
    items = []
    for label, rows in groups:
        values = [float(row.realized_r) for row in rows]
        mfe = [float(row.mfe_r) for row in rows if _finite(row.mfe_r) is not None]
        duration = [float(row.observed_duration_bars) for row in rows if _finite(row.observed_duration_bars) is not None]
        giveback = [float(row.mfe_r) - float(row.realized_r) for row in rows if _finite(row.mfe_r) is not None]
        ids = {row.trade_id for row in rows}
        selected = tuple(record for record in records if record["trade"] is not None and getattr(record["trade"], "trade_id") in ids)
        contexts = _context_distribution(selected, _context_classifiers(edges))
        items.append({"label": label, "count": len(rows), "total_r": float(sum(values)), "positive_r_share": _ratio(sum(values), total_positive), "mean_r": _mean_median(values)["mean"], "median_r": _mean_median(values)["median"], "mean_mfe_r": _mean_median(mfe)["mean"], "median_mfe_r": _mean_median(mfe)["median"], "mean_giveback_r": _mean_median(giveback)["mean"], "median_giveback_r": _mean_median(giveback)["median"], "mean_duration_bars": _mean_median(duration)["mean"], "median_duration_bars": _mean_median(duration)["median"], "entry_context": contexts})
    return {"groups": items}


def _windows(bundle: CanonicalResearchBundle, records: tuple[dict[str, object], ...], all_closed: tuple[object, ...]) -> dict[str, object]:
    canonical = []
    frozen_windows = {window.role: window for window in baseline_report(bundle).windows}
    for window in CANONICAL_WINDOWS:
        selected = tuple(record for record in records if record["trade"] is not None and getattr(record["trade"], "outcome_state") == "closed" and window.start_ms <= getattr(record["setup"], "decision_timestamp") <= window.end_ms and window.start_ms <= getattr(record["trade"], "decision_timestamp") <= window.end_ms and window.start_ms <= getattr(record["trade"], "exit_timestamp") <= window.end_ms)
        item = _population(selected, all_closed)
        frozen = frozen_windows[window.role]
        frozen_metrics = {"total_r": frozen.metrics.total_r, "expectancy_r": frozen.metrics.expectancy_r, "r_per_setup": frozen.metrics.r_per_setup, "profit_factor": frozen.metrics.profit_factor, "stop_rate": frozen.metrics.stop_rate, "win_rate": frozen.metrics.win_rate}
        item["r_per_setup"] = _ratio(float(item["total_r"]), frozen.eligible_setups)
        observed_setups = tuple(setup for setup in bundle.dataset.setup_rows if window.start_ms <= setup.decision_timestamp <= window.end_ms)
        eligible_setups = tuple(setup for setup in observed_setups if setup.eligible_baseline_setup)
        censored_trades = tuple(trade for trade in bundle.dataset.trade_rows if trade.outcome_state == "censored" and window.start_ms <= trade.decision_timestamp <= window.end_ms)
        checks = {
            "observed_setups": len(observed_setups) == frozen.observed_setups,
            "eligible_setups": len(eligible_setups) == frozen.eligible_setups,
            "opened_setups": sum(setup.setup_status.value == "opened" for setup in observed_setups) == frozen.opened_setups,
            "closed_trades": int(item["closed_trades"]) == frozen.included_closed,
            "censored": len(censored_trades) == frozen.censored,
            **{name: math.isclose(float(item[name]), float(expected), abs_tol=_TOL, rel_tol=_TOL) for name, expected in frozen_metrics.items()},
        }
        if not all(checks.values()):
            raise ValueError(f"canonical {window.role} baseline window does not reconcile")
        item.update({"role": window.role, "start_date": window.start_date, "end_date": window.end_date, "boundary_rule": "frozen baseline window accounting: entry and exit must both be inside", "observed_setups": frozen.observed_setups, "eligible_setups": frozen.eligible_setups, "opened_setups": frozen.opened_setups, "closed_trades": frozen.included_closed, "purged_setup_boundary": frozen.purged_setup_boundary, "purged_entry_exit_boundary": frozen.purged_entry_exit_boundary, "censored": frozen.censored, "frozen_metrics": frozen_metrics, "frozen_reconciliation": checks})
        canonical.append(item)
    months: dict[str, list[dict[str, object]]] = {}
    effective_start, effective_end = CANONICAL_WINDOWS[0].start_ms, CANONICAL_WINDOWS[-1].end_ms
    outside = 0
    for record in records:
        if not effective_start <= getattr(record["setup"], "decision_timestamp") <= effective_end:
            outside += 1
            continue
        date = datetime.fromtimestamp(getattr(record["setup"], "decision_timestamp") / 1000, UTC).date().isoformat()[:7]
        months.setdefault(date, []).append(record)
    blocks = []
    for label in sorted(months):
        item = _population(tuple(months[label]), all_closed); item.update({"month": label, "boundary_rule": "setup decision timestamp calendar-month inclusive", "observation_count": len(months[label]), "population_scope": "effective_canonical_research_range"}); blocks.append(item)
    ranked = sorted(blocks, key=lambda item: (float(item["total_r"]), item["month"]))
    return {"canonical_baseline_windows": canonical, "calendar_month_blocks": blocks, "outside_effective_range_excluded": outside, "best_window": None if not ranked else ranked[-1]["month"], "worst_window": None if not ranked else ranked[0]["month"], "positive_windows": sum(float(item["total_r"]) > 0.0 for item in blocks), "negative_windows": sum(float(item["total_r"]) < 0.0 for item in blocks)}


def _direction_month_cross_tab(records: tuple[dict[str, object], ...], closed: tuple[object, ...]) -> list[dict[str, object]]:
    """Fixed setup-origin monthly descriptive cross-tab; no candidate evaluation."""
    result = []
    for label in ("LONG", "SHORT"):
        for month in ("2026-05", "2026-06", "2026-07", "2026-08"):
            selected = tuple(record for record in records if _direction_label(getattr(record["setup"], "direction")) == label and datetime.fromtimestamp(getattr(record["setup"], "decision_timestamp") / 1000, UTC).date().isoformat()[:7] == month)
            item = _population(selected, closed)
            item.update({"dimension": "direction", "bucket": label, "period": month, "observation_count": len(selected), "cohort_anchor": "setup_decision_timestamp"})
            result.append(item)
    return result


_MONTHS = ("2026-05", "2026-06", "2026-07", "2026-08")


def _month(record: dict[str, object]) -> str:
    return datetime.fromtimestamp(getattr(record["setup"], "decision_timestamp") / 1000, UTC).date().isoformat()[:7]


def _monthly_cross_tabs(specs: tuple[tuple[str, tuple[dict[str, object], ...], Callable[[dict[str, object]], str], tuple[str, ...]], ...], closed: tuple[object, ...]) -> list[dict[str, object]]:
    """Stable setup-origin month cross-tabs for predeclared descriptive tables."""
    result: list[dict[str, object]] = []
    for name, universe, classifier, labels in specs:
        for label in labels:
            for month in _MONTHS:
                selected = tuple(record for record in universe if classifier(record) == label and _month(record) == month)
                item = _population(selected, closed)
                item.update({"dimension": name, "bucket": label, "period": month, "observation_count": len(selected), "cohort_anchor": "setup_decision_timestamp"})
                result.append(item)
    return result


def _winner_group_rows(records: tuple[dict[str, object], ...]) -> tuple[tuple[str, tuple[dict[str, object], ...]], ...]:
    positive = tuple(record for record in records if float(getattr(record["trade"], "realized_r")) > 0.0)
    top = _top(tuple(record["trade"] for record in positive), reverse=True, count=len(positive))
    by_id = {getattr(record["trade"], "trade_id"): record for record in positive}
    return (
        ("profitable", positive),
        ("ge_2r", tuple(record for record in positive if float(getattr(record["trade"], "realized_r")) >= 2.0)),
        ("ge_3r", tuple(record for record in positive if float(getattr(record["trade"], "realized_r")) >= 3.0)),
        ("ge_5r", tuple(record for record in positive if float(getattr(record["trade"], "realized_r")) >= 5.0)),
        ("top_5", tuple(by_id[row.trade_id] for row in top[:5])),
        ("top_10", tuple(by_id[row.trade_id] for row in top[:10])),
    )


def _winner_group_month_cross_tabs(records: tuple[dict[str, object], ...], closed: tuple[object, ...]) -> list[dict[str, object]]:
    result = []
    for label, group in _winner_group_rows(records):
        for month in _MONTHS:
            selected = tuple(record for record in group if _month(record) == month)
            item = _population(selected, closed)
            item.update({"dimension": "winner_group", "bucket": label, "period": month, "observation_count": len(selected), "cohort_anchor": "setup_decision_timestamp", "overlapping_diagnostic_group": True, "bridge_eligible": False})
            result.append(item)
    return result


def _cross_tab_reconciliation(specs: tuple[tuple[str, tuple[dict[str, object], ...], Callable[[dict[str, object]], str], tuple[str, ...]], ...], rows: list[dict[str, object]], closed: tuple[object, ...], losing_closed_records: tuple[dict[str, object], ...]) -> dict[str, object]:
    main = []
    for name, universe, _, _ in specs:
        for month in _MONTHS:
            expected = _population(tuple(record for record in universe if _month(record) == month), closed)
            buckets = tuple(row for row in rows if row["dimension"] == name and row["period"] == month)
            item = {"dimension": name, "period": month, "expected_observation_count": len(tuple(record for record in universe if _month(record) == month)), "bucket_observation_count": sum(int(row["observation_count"]) for row in buckets), "expected_closed_trades": expected["closed_trades"], "bucket_closed_trades": sum(int(row["closed_trades"]) for row in buckets), "expected_total_r": expected["total_r"], "bucket_total_r": float(sum(float(row["total_r"]) for row in buckets))}
            item.update({"observation_count_reconciles": item["bucket_observation_count"] == item["expected_observation_count"], "closed_trades_reconciles": item["bucket_closed_trades"] == item["expected_closed_trades"], "total_r_reconciles": math.isclose(item["bucket_total_r"], float(item["expected_total_r"]), abs_tol=_TOL, rel_tol=_TOL)})
            main.append(item)
    failures = []
    for month in _MONTHS:
        expected = tuple(record for record in losing_closed_records if _month(record) == month)
        buckets = tuple(row for row in rows if row["dimension"] == "failure_mode" and row["period"] == month)
        expected_negative = -sum(float(getattr(record["trade"], "realized_r")) for record in expected)
        item = {"period": month, "expected_losing_closed_trades": len(expected), "bucket_losing_closed_trades": sum(int(row["closed_trades"]) for row in buckets), "expected_negative_r_magnitude": float(expected_negative), "bucket_negative_r_magnitude": float(sum(float(row["negative_r_magnitude"]) for row in buckets))}
        item.update({"losing_closed_trades_reconciles": item["bucket_losing_closed_trades"] == item["expected_losing_closed_trades"], "negative_r_magnitude_reconciles": math.isclose(item["bucket_negative_r_magnitude"], item["expected_negative_r_magnitude"], abs_tol=_TOL, rel_tol=_TOL)})
        failures.append(item)
    if not all(all(item[key] for key in ("observation_count_reconciles", "closed_trades_reconciles", "total_r_reconciles")) for item in main) or not all(all(item[key] for key in ("losing_closed_trades_reconciles", "negative_r_magnitude_reconciles")) for item in failures):
        raise ValueError("chronology cross-tab reconciliation failed")
    return {"mutually_exclusive_main_dimensions": main, "failure_modes": failures, "winner_groups": {"overlapping_diagnostic_groups": True, "bridge_reconciliation_applicable": False, "explicitly_non_bridge": True}}


def _validate_inputs(bundle: CanonicalResearchBundle, artifact: FeatureDefinitionArtifact, rows: tuple[SetupRegimeFeatureRow, ...]) -> None:
    if PHASE7_2_BASE_SHA != "03235d2c2887075df94df0cd0ec960442623c417" or ATR_EXTENSION_EDGES != (3.522108008530367, 3.992676256992967, 4.917967689345026) or STOP_ADR_RATIO_EDGES != (.0702042096073447, .09338718609335621, .1292782711341422): raise ValueError("frozen Phase 7.3 constants rebound")
    validate_regime_feature_artifact(artifact)
    if artifact != feature_definition_artifact(bundle): raise ValueError("exact canonical Phase 7.2 feature artifact required")
    if rows != build_setup_regime_feature_rows(bundle): raise ValueError("exact canonical setup-origin rows required")
    if sha256(json.dumps(asdict(artifact), sort_keys=True, separators=(",", ":"), allow_nan=False).encode() + b"\n").hexdigest() != PHASE72_FEATURE_ARTIFACT_SHA256: raise ValueError("Phase 7.2 feature artifact SHA mismatch")
    if tuple(bundle.source_counts) != (("15m", 10452), ("4h", 8480)): raise ValueError("canonical source counts required")


def _build(bundle: CanonicalResearchBundle, artifact: FeatureDefinitionArtifact, rows: tuple[SetupRegimeFeatureRow, ...]) -> dict[str, object]:
    _validate_inputs(bundle, artifact, rows)
    setups = tuple(row for row in bundle.dataset.setup_rows if row.eligible_baseline_setup)
    trades = tuple(bundle.dataset.trade_rows)
    by_setup = {row.setup_id: row for row in trades}; feature_by_id = {row.setup_id: row for row in rows}
    if len(by_setup) != len(trades) or len(feature_by_id) != len(rows): raise ValueError("duplicate canonical identity")
    if len(setups) != 260 or len(trades) != 192 or sum(row.outcome_state == "closed" for row in trades) != 191 or sum(row.outcome_state == "censored" for row in trades) != 1: raise ValueError("canonical Phase 7.3 population binding mismatch")
    records = tuple(_record(setup, by_setup.get(setup.setup_id), feature_by_id[setup.setup_id]) for setup in setups)
    closed = _closed(records)
    opened_records = tuple(record for record in records if record["trade"] is not None)
    closed_records = tuple(record for record in opened_records if getattr(record["trade"], "outcome_state") == "closed")
    diagnosis = build_diagnosis_evidence(bundle, artifact, rows)
    edges = {feature.name: tuple(feature.quantile_edges) for feature in diagnosis.features}
    direction = _bucket_table("direction", records, closed, lambda r: _direction_label(getattr(r["setup"], "direction")), ("LONG", "SHORT"), source="canonical setup/trade direction")
    bias = _bucket_table("htf_bias_direction", records, closed, lambda r: _direction_label(getattr(r["setup"], "htf_bias")), ("LONG", "SHORT", "missing"), source="SetupRow.htf_bias at decision")
    path = _bucket_table("setup_path", records, closed, lambda r: "immediate_opened" if r["trade"] is not None and not getattr(r["setup"], "was_armed") else "armed_opened" if r["trade"] is not None else "armed_cancelled" if getattr(r["setup"], "was_armed") and getattr(r["setup"], "setup_status").value == "cancelled" else "eligible_no_trade_other", ("immediate_opened", "armed_opened", "armed_cancelled", "eligible_no_trade_other"), source="canonical SetupRow setup status and was_armed")
    age = _bucket_table("setup_age", opened_records, closed, lambda r: _age_bucket(_trade_value(r, "setup_age_ms")), ("same_decision", "one_bar", "two_to_four", "five_plus", "missing_or_invalid"), source="TradeRow.setup_age_ms / 15m", universe_label="opened_trades", universe_count=192, reasons={"censored": 1})
    transition = _bucket_table("kalman_transition_age", opened_records, closed, lambda r: _age_bucket(_trade_value(r, "kalman_transition_age_ms")), ("same_decision", "one_bar", "two_to_four", "five_plus", "missing_or_invalid"), source="TradeRow.kalman_transition_age_ms / 15m", universe_label="opened_trades", universe_count=192, reasons={"censored": 1})
    duration = _bucket_table("trade_duration", closed_records, closed, lambda r: _duration_bucket(_trade_value(r, "observed_duration_bars")), ("zero", "one_bar", "two_to_four", "five_to_eight", "nine_to_sixteen", "seventeen_plus", "missing"), source="post-entry outcome diagnostic", universe_label="opened_trades", universe_count=192, excluded=1, reasons={"censored": 1})
    exit_path = _bucket_table("exit_path", closed_records, closed, _exit_bucket, ("stop_only", "strategy_only", "stop_and_strategy_same_bar", "other_or_missing"), source="canonical exact stop_hit/strategy_exit", universe_label="opened_trades", universe_count=192, excluded=1, reasons={"censored": 1})
    time = {"six_hour": _bucket_table("utc_six_hour_bucket", opened_records, closed, lambda r: str(_trade_value(r, "utc_six_hour_bucket")), ("0", "1", "2", "3", "missing"), source="TradeRow UTC entry time", universe_label="opened_trades", universe_count=192, reasons={"censored": 1}), "weekday": _bucket_table("utc_weekday", opened_records, closed, lambda r: str(_trade_value(r, "utc_weekday")), ("0", "1", "2", "3", "4", "5", "6", "missing"), source="TradeRow UTC entry time", universe_label="opened_trades", universe_count=192, reasons={"censored": 1})}
    regime_tables = []
    for field in ("kalman_persistence_bars", "hema_fast_slope_atr_8", "directional_efficiency_8", "directional_efficiency_16", "directional_efficiency_32", "hema_flip_count_16", "kalman_flip_count_16", "combined_flip_count_16"):
        labels = tuple(f"Q{i}" for i in range(1, 5)) + ("missing",) if edges.get(field) else ("nonmissing", "missing")
        regime_tables.append(_bucket_table(field, records, closed, lambda r, f=field: _numeric_bucket(getattr(r["feature"], f), edges.get(f, ()), prefix="Q"), labels, source="frozen Phase 7.2 development-inclusive quartile edges"))
    regime_tables.append(_bucket_table("hema_kalman_aligned", records, closed, lambda r: "true" if getattr(r["feature"], "hema_kalman_aligned") is True else "false" if getattr(r["feature"], "hema_kalman_aligned") is False else "missing", ("false", "true", "missing"), source="frozen Phase 7.2 setup-origin definition"))
    volatility = [
        _bucket_table("atr_adr_ratio", records, closed, lambda r: _numeric_bucket(getattr(r["feature"], "atr_adr_ratio"), edges.get("atr_adr_ratio", ()), prefix="Q"), tuple(f"Q{i}" for i in range(1, 5)) + ("missing",), source="frozen Phase 7.2 development-inclusive quartile edges"),
    ]
    # ADR extension has existing Phase-7 fixed semantic levels.  Raw ATR/ADR
    # are instrument-unit quantities, so only presence is descriptively valid.
    volatility.append(_bucket_table("adr_extension", opened_records, closed, lambda r: _adr_extension_bucket(_trade_value(r, "adr_extension")), ("lt_0.25", "gte_0.25_lt_0.5", "gte_0.5_lte_1", "gt_1", "missing"), source="frozen Phase 7 ADR-extension intervals", universe_label="opened_trades", universe_count=192, reasons={"censored": 1}))
    for field, fixed_edges in (("atr_extension", ATR_EXTENSION_EDGES), ("stop_adr_ratio", STOP_ADR_RATIO_EDGES)):
        volatility.append(_bucket_table(field, opened_records, closed, lambda r, f=field, e=fixed_edges: _numeric_bucket(_trade_value(r, f), e), ("Q1", "Q2", "Q3", "Q4", "missing"), source="frozen pre-existing Phase 7 development edges", universe_label="opened_trades", universe_count=192, reasons={"censored": 1}))
    for field in ("stop_atr_ratio", "atr_at_entry", "adr"):
        volatility.append(_bucket_table(field, opened_records, closed, lambda r, f=field: "all_nonmissing" if _finite(_trade_value(r, f)) is not None else "missing", ("all_nonmissing", "missing"), source="no frozen Phase 7 edge reused; descriptive missingness only", universe_label="opened_trades", universe_count=192, reasons={"censored": 1}))
    baseline = _population(records, closed)
    immediate, armed = path["buckets"][0], path["buckets"][1]
    exit_total = sum(float(item["total_r"]) for item in exit_path["buckets"])
    bridge = {"positive_negative": {"positive_r": baseline["positive_r"], "negative_r_magnitude": baseline["negative_r_magnitude"], "net_total_r": baseline["total_r"], "reconciles": math.isclose(float(baseline["positive_r"]) - float(baseline["negative_r_magnitude"]), float(baseline["total_r"]), abs_tol=_TOL, rel_tol=_TOL)}, "direction": {"bucket_total_r": sum(float(item["total_r"]) for item in direction["buckets"]), "canonical_total_r": baseline["total_r"], "reconciles": math.isclose(sum(float(item["total_r"]) for item in direction["buckets"]), float(baseline["total_r"]), abs_tol=_TOL, rel_tol=_TOL)}, "opened_path": {"immediate_opened": immediate["opened_trades"], "armed_opened": armed["opened_trades"], "opened_total": baseline["opened_trades"], "opened_reconciles": int(immediate["opened_trades"]) + int(armed["opened_trades"]) == int(baseline["opened_trades"]), "closed_total": baseline["closed_trades"], "closed_reconciles": int(immediate["closed_trades"]) + int(armed["closed_trades"]) == int(baseline["closed_trades"]), "total_r": baseline["total_r"], "total_r_reconciles": math.isclose(float(immediate["total_r"]) + float(armed["total_r"]), float(baseline["total_r"]), abs_tol=_TOL, rel_tol=_TOL)}, "exit_path": {"bucket_closed_trades": sum(int(item["closed_trades"]) for item in exit_path["buckets"]), "closed_total": baseline["closed_trades"], "closed_reconciles": sum(int(item["closed_trades"]) for item in exit_path["buckets"]) == int(baseline["closed_trades"]), "bucket_total_r": exit_total, "canonical_total_r": baseline["total_r"], "total_r_reconciles": math.isclose(exit_total, float(baseline["total_r"]), abs_tol=_TOL, rel_tol=_TOL)}}
    if not (bridge["positive_negative"]["reconciles"] and bridge["direction"]["reconciles"] and bridge["opened_path"]["opened_reconciles"] and bridge["opened_path"]["closed_reconciles"] and bridge["opened_path"]["total_r_reconciles"] and bridge["exit_path"]["closed_reconciles"] and bridge["exit_path"]["total_r_reconciles"]): raise ValueError("attribution bridge does not reconcile")
    return {"schema_version": SCHEMA_VERSION, "phase7_2_base_sha": PHASE7_2_BASE_SHA, "interpretation": "descriptive_attribution_not_causal", "provenance": {"manifest_id": artifact.manifest_id, "dataset_fingerprint": artifact.dataset_fingerprint, "strategy_fingerprint": artifact.strategy_fingerprint, "replay_fingerprint": artifact.replay_fingerprint, "backtest_fingerprint": artifact.backtest_fingerprint, "research_fingerprint": artifact.research_fingerprint, "split_fingerprint": artifact.split_fingerprint, "source_artifacts": [asdict(x) for x in artifact.source_artifacts], "source_counts": list(artifact.source_counts), "phase72_feature_version": artifact.feature_version, "phase72_feature_definition_fingerprint": artifact.definition_fingerprint, "phase72_feature_artifact_fingerprint": fingerprint(artifact)}, "population": {"observed_setups": len(bundle.dataset.setup_rows), "noneligible_setups": len(bundle.dataset.setup_rows) - len(setups), "eligible_setups": len(setups), "opened_trades": len(trades), "closed_trades": len(closed), "censored_trades": len(trades) - len(closed)}, "baseline_economics": baseline, "attribution": {"direction": direction, "htf_bias_direction": {"redundant_with_direction": all(_direction_label(getattr(r['setup'], 'htf_bias')) == _direction_label(getattr(r['setup'], 'direction')) for r in records if getattr(r['setup'], 'htf_bias') is not None), "table": bias}, "setup_path": path, "setup_age": age, "kalman_transition_age": transition, "duration": duration, "exit_path": exit_path, "time": time, "regime": regime_tables, "volatility_range": volatility}, "failure_modes": _failure_report(records, closed, edges), "stopped_trade_analysis": _stopped_report(closed), "chronology": _windows(bundle, records, closed), "concentration": _concentration(closed), "contribution_bridge": bridge, "winner_anatomy": _winner_anatomy(records, closed, edges), "hypotheses": []}


def build_edge_attribution_report(bundle: CanonicalResearchBundle, artifact: FeatureDefinitionArtifact, feature_rows: tuple[SetupRegimeFeatureRow, ...]) -> dict[str, object]:
    """Build full-population Phase 7.3 descriptive attribution from exact inputs."""
    report = _build(bundle, artifact, feature_rows)
    report["provenance"]["phase72_feature_artifact_sha256"] = PHASE72_FEATURE_ARTIFACT_SHA256
    eligible = tuple(row for row in bundle.dataset.setup_rows if row.eligible_baseline_setup)
    armed = tuple(row for row in eligible if row.was_armed)
    immediate = tuple(row for row in eligible if not row.was_armed)
    opened = tuple(row for row in eligible if row.setup_status.value == "opened")
    report["setup_conversion"] = {"eligible_to_opened": {"numerator": len(opened), "denominator": len(eligible), "rate": _ratio(len(opened), len(eligible))}, "armed": {"denominator": len(armed), "opened": sum(row.setup_status.value == "opened" for row in armed), "cancelled": sum(row.setup_status.value == "cancelled" for row in armed), "opened_rate": _ratio(sum(row.setup_status.value == "opened" for row in armed), len(armed)), "cancelled_rate": _ratio(sum(row.setup_status.value == "cancelled" for row in armed), len(armed))}, "immediate": {"denominator": len(immediate), "opened": sum(row.setup_status.value == "opened" for row in immediate), "opened_rate": _ratio(sum(row.setup_status.value == "opened" for row in immediate), len(immediate)), "statuses": dict(sorted(Counter(row.setup_status.value for row in immediate).items()))}}
    opened_trades = tuple(row for row in bundle.dataset.trade_rows)
    report["raw_numeric_summaries"] = {field: {"count": len(values), "missing": len(opened_trades) - len(values), "minimum": None if not values else min(values), "maximum": None if not values else max(values), **_mean_median(values)} for field in ("atr_at_entry", "adr", "stop_atr_ratio") if (values := [float(getattr(row, field)) for row in opened_trades if _finite(getattr(row, field)) is not None])}
    feature_by_id = {row.setup_id: row for row in feature_rows}
    records = tuple(_record(row, next((trade for trade in bundle.dataset.trade_rows if trade.setup_id == row.setup_id), None), feature_by_id[row.setup_id]) for row in eligible)
    opened_records = tuple(record for record in records if record["trade"] is not None)
    closed_records = tuple(record for record in opened_records if getattr(record["trade"], "outcome_state") == "closed")
    evidence = build_diagnosis_evidence(bundle, artifact, feature_rows)
    edges = {feature.name: tuple(feature.quantile_edges) for feature in evidence.features}
    specs: list[tuple[str, tuple[dict[str, object], ...], Callable[[dict[str, object]], str], tuple[str, ...]]] = [
        ("direction", records, lambda r: _direction_label(getattr(r["setup"], "direction")), ("LONG", "SHORT")),
        ("htf_bias_direction", records, lambda r: _direction_label(getattr(r["setup"], "htf_bias")), ("LONG", "SHORT", "missing")),
        ("setup_path", records, lambda r: "immediate_opened" if r["trade"] is not None and not getattr(r["setup"], "was_armed") else "armed_opened" if r["trade"] is not None else "armed_cancelled" if getattr(r["setup"], "was_armed") and getattr(r["setup"], "setup_status").value == "cancelled" else "eligible_no_trade_other", ("immediate_opened", "armed_opened", "armed_cancelled", "eligible_no_trade_other")),
        ("setup_age", opened_records, lambda r: _age_bucket(_trade_value(r, "setup_age_ms")), ("same_decision", "one_bar", "two_to_four", "five_plus", "missing_or_invalid")),
        ("kalman_transition_age", opened_records, lambda r: _age_bucket(_trade_value(r, "kalman_transition_age_ms")), ("same_decision", "one_bar", "two_to_four", "five_plus", "missing_or_invalid")),
        ("trade_duration", closed_records, lambda r: _duration_bucket(_trade_value(r, "observed_duration_bars")), ("zero", "one_bar", "two_to_four", "five_to_eight", "nine_to_sixteen", "seventeen_plus", "missing")),
        ("exit_path", closed_records, _exit_bucket, ("stop_only", "strategy_only", "stop_and_strategy_same_bar", "other_or_missing")),
        ("utc_six_hour_bucket", opened_records, lambda r: str(_trade_value(r, "utc_six_hour_bucket")), ("0", "1", "2", "3", "missing")),
        ("utc_weekday", opened_records, lambda r: str(_trade_value(r, "utc_weekday")), ("0", "1", "2", "3", "4", "5", "6", "missing")),
    ]
    for field in ("kalman_persistence_bars", "hema_fast_slope_atr_8", "directional_efficiency_8", "directional_efficiency_16", "directional_efficiency_32", "hema_flip_count_16", "kalman_flip_count_16", "combined_flip_count_16", "atr_adr_ratio"):
        specs.append((field, records, lambda r, f=field: _numeric_bucket(getattr(r["feature"], f), edges.get(f, ())), ("Q1", "Q2", "Q3", "Q4", "missing")))
    specs.append(("hema_kalman_aligned", records, lambda r: "true" if getattr(r["feature"], "hema_kalman_aligned") is True else "false" if getattr(r["feature"], "hema_kalman_aligned") is False else "missing", ("false", "true", "missing")))
    specs.extend((field, opened_records, lambda r, f=field: _adr_extension_bucket(_trade_value(r, f)) if f == "adr_extension" else _numeric_bucket(_trade_value(r, f), ATR_EXTENSION_EDGES if f == "atr_extension" else STOP_ADR_RATIO_EDGES) if f in {"atr_extension", "stop_adr_ratio"} else "all_nonmissing" if _finite(_trade_value(r, f)) is not None else "missing", ("lt_0.25", "gte_0.25_lt_0.5", "gte_0.5_lte_1", "gt_1", "missing") if field == "adr_extension" else ("Q1", "Q2", "Q3", "Q4", "missing") if field in {"atr_extension", "stop_adr_ratio"} else ("all_nonmissing", "missing")) for field in ("adr_extension", "atr_extension", "stop_adr_ratio", "stop_atr_ratio", "atr_at_entry", "adr"))
    closed = _closed(records)
    losing_closed_records = tuple(record for record in closed_records if float(getattr(record["trade"], "realized_r")) < 0.0)
    failure_specs = (("failure_mode", losing_closed_records, lambda r: failure_mode(r["trade"]), FAILURE_MODE_ORDER),)
    cross_tabs = _monthly_cross_tabs(tuple(specs), closed)
    cross_tabs.extend(_monthly_cross_tabs(failure_specs, closed))
    cross_tabs.extend(_winner_group_month_cross_tabs(closed_records, closed))
    report["chronology"]["dimension_cross_tabs"] = cross_tabs
    report["chronology"]["cross_tab_reconciliation"] = _cross_tab_reconciliation(tuple(specs), cross_tabs, closed, losing_closed_records)
    return report


def validate_edge_attribution_report(bundle: CanonicalResearchBundle, artifact: FeatureDefinitionArtifact, feature_rows: tuple[SetupRegimeFeatureRow, ...], report: dict[str, object]) -> dict[str, object]:
    if not isinstance(report, dict): raise TypeError("edge attribution report must be a dictionary")
    expected = build_edge_attribution_report(bundle, artifact, feature_rows)
    if report != expected: raise ValueError("edge attribution report does not match canonical deterministic reconstruction")
    return report


def edge_attribution_json(report: dict[str, object]) -> bytes:
    """Path-independent, finite-only, stable JSON serializer for the report."""
    return json.dumps(report, sort_keys=True, separators=(",", ":"), allow_nan=False).encode("utf-8") + b"\n"


def write_edge_attribution_report(bundle: CanonicalResearchBundle, artifact: FeatureDefinitionArtifact, feature_rows: tuple[SetupRegimeFeatureRow, ...], report: dict[str, object], path: Path, *, overwrite: bool = False) -> None:
    """Write a previously validated deterministic report exactly once by default."""
    validate_edge_attribution_report(bundle, artifact, feature_rows, report)
    if path.exists() and not overwrite:
        raise FileExistsError("refusing to overwrite existing edge attribution report")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(edge_attribution_json(report))
