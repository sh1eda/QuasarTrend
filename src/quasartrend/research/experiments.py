"""Predeclared, development-only Phase 7 experiments.

This module is isolated from production strategy/backtest paths. It consumes
the immutable canonical bundle and labels delayed entries as paired research
counterfactuals, not production simulations.
"""
from __future__ import annotations

from collections import Counter
from dataclasses import asdict, dataclass
import json
import math
from statistics import mean, median, quantiles
from typing import Callable
from pathlib import Path

from quasartrend.replay import HistoricalBar, ReplayTrace, Timeframe
from quasartrend.strategy import Direction

from .adr import BAR_MS
from .metrics import calculate_metrics
from .models import ResearchMetrics, SetupRow, TradeRow, validate_entry_feature_selectors
from .pipeline import CanonicalResearchBundle
from .provenance import fingerprint
from .splits import ChronologicalWindow

EXPERIMENT_SCHEMA_VERSION = "phase7-development-experiments/v2"
DEVELOPMENT_WINDOW = ChronologicalWindow("development", "2026-05-15", "2026-07-09")


@dataclass(frozen=True, slots=True)
class ExperimentConfig:
    feature_definition_version: str = "phase7-predeclared-development/v2"
    quantile_method: str = "inclusive"
    delays: tuple[int, ...] = (0, 1, 2, 4)
    adr_fixed_edges: tuple[float, ...] = (0.25, 0.50, 1.00)

    def __post_init__(self) -> None:
        if self.feature_definition_version != "phase7-predeclared-development/v2":
            raise ValueError("feature definition version is fixed")
        if self.quantile_method != "inclusive":
            raise ValueError("development quartiles use the inclusive method")
        if self.delays != (0, 1, 2, 4):
            raise ValueError("predeclared delays are fixed at 0, 1, 2, 4")
        if self.adr_fixed_edges != (0.25, 0.50, 1.00):
            raise ValueError("predeclared ADR edges are fixed")


@dataclass(frozen=True, slots=True)
class NumericSummary:
    count: int
    missing: int
    minimum: float | None
    maximum: float | None
    mean: float | None
    median: float | None


@dataclass(frozen=True, slots=True)
class ContinuousRelationship:
    feature: str
    definition: str
    feature_summary: NumericSummary
    paired_outcome_count: int
    pearson_r_with_realized_r: float | None


@dataclass(frozen=True, slots=True)
class OutcomeMetrics:
    eligible_baseline_setups: int
    baseline_closed_trades: int
    sample_count: int
    linked_setup_retention: float
    trade_retention: float
    win_rate: float | None
    expectancy_r: float | None
    total_r: float
    opportunity_r_per_setup: float | None
    selected_r_per_trade: float | None
    profit_factor: float | None
    stop_rate: float | None
    mean_mae_r: float | None
    mean_mfe_r: float | None
    mean_duration_ms: float | None


@dataclass(frozen=True, slots=True)
class BucketResult:
    label: str
    lower_bound: float | None
    upper_bound: float | None
    lower_inclusive: bool
    upper_inclusive: bool
    metrics: OutcomeMetrics


@dataclass(frozen=True, slots=True)
class FeatureExperiment:
    hypothesis_id: str
    feature: str
    definition: str
    continuous: ContinuousRelationship | None
    quantile_edges: tuple[float, ...]
    buckets: tuple[BucketResult, ...]
    missing: BucketResult


@dataclass(frozen=True, slots=True)
class DelayExperiment:
    hypothesis_id: str
    delay_bars: int
    semantics: str
    eligible_baseline_setups: int
    baseline_closed_trades: int
    entries_taken: int
    baseline_setups_without_open: int
    baseline_trades_skipped: int
    skipped_reason_counts: tuple[tuple[str, int], ...]
    metrics: OutcomeMetrics
    retained_trade_ids: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class DevelopmentExperimentReport:
    schema_version: str
    caveat: str
    role: str
    start_date: str
    end_date: str
    manifest_id: str
    dataset_fingerprint: str
    experiment_config: ExperimentConfig
    experiment_fingerprint: str
    eligible_baseline_setups: int
    included_closed_baseline_trades: int
    baseline_metrics: OutcomeMetrics
    feature_experiments: tuple[FeatureExperiment, ...]
    delay_experiments: tuple[DelayExperiment, ...]
    inaccessible_roles: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class _CounterfactualOutcome:
    trade_id: str
    canonical_entry_price: float
    canonical_stop_price: float
    canonical_exit_price: float
    exit_timestamp: int
    exit_mode: str
    realized_r: float
    stop_hit: bool
    mae_r: float | None
    mfe_r: float | None
    duration_ms: int


def _inside(timestamp: int) -> bool:
    return DEVELOPMENT_WINDOW.start_ms <= timestamp <= DEVELOPMENT_WINDOW.end_ms


def _development_population(bundle: CanonicalResearchBundle) -> tuple[tuple[SetupRow, ...], tuple[TradeRow, ...]]:
    """Require setup, entry, and exit to be fully inside development."""
    setups = tuple(row for row in bundle.dataset.setup_rows if row.eligible_baseline_setup and _inside(row.decision_timestamp))
    setup_by_id = {row.setup_id: row for row in bundle.dataset.setup_rows}
    trades = tuple(
        row for row in bundle.dataset.trade_rows
        if row.outcome_state == "closed"
        and _inside(setup_by_id[row.setup_id].decision_timestamp)
        and _inside(row.decision_timestamp)
        and row.exit_timestamp is not None and _inside(row.exit_timestamp)
    )
    return setups, trades


def _summary(values: tuple[float | None, ...]) -> NumericSummary:
    present = tuple(value for value in values if value is not None)
    return NumericSummary(len(present), len(values) - len(present), None if not present else min(present), None if not present else max(present), None if not present else mean(present), None if not present else median(present))


def _pearson(pairs: tuple[tuple[float, float], ...]) -> float | None:
    if len(pairs) < 2:
        return None
    xs, ys = tuple(x for x, _ in pairs), tuple(y for _, y in pairs)
    xm, ym = mean(xs), mean(ys)
    numerator = sum((x - xm) * (y - ym) for x, y in pairs)
    xscale = sum((x - xm) ** 2 for x in xs)
    yscale = sum((y - ym) ** 2 for y in ys)
    return None if xscale == 0.0 or yscale == 0.0 else numerator / math.sqrt(xscale * yscale)


def _from_metrics(metrics: ResearchMetrics, *, sample_count: int, eligible_setups: int, baseline_closed: int) -> OutcomeMetrics:
    return OutcomeMetrics(
        eligible_setups, baseline_closed, sample_count,
        0.0 if eligible_setups == 0 else sample_count / eligible_setups,
        0.0 if baseline_closed == 0 else sample_count / baseline_closed,
        metrics.win_rate, metrics.expectancy_r, metrics.total_r,
        None if eligible_setups == 0 else metrics.total_r / eligible_setups,
        metrics.expectancy_r, metrics.profit_factor, metrics.stop_rate,
        metrics.mean_mae_r, metrics.mean_mfe_r, metrics.mean_duration_ms,
    )


def _outcome_metrics(rows: tuple[TradeRow, ...], *, eligible_setups: int, baseline_closed: int) -> OutcomeMetrics:
    return _from_metrics(calculate_metrics(rows, eligible_setups=eligible_setups), sample_count=len(rows), eligible_setups=eligible_setups, baseline_closed=baseline_closed)


def _bucket(label: str, rows: tuple[TradeRow, ...], *, eligible_setups: int, baseline_closed: int, lower: float | None = None, upper: float | None = None, lower_inclusive: bool = False, upper_inclusive: bool = True) -> BucketResult:
    return BucketResult(label, lower, upper, lower_inclusive, upper_inclusive, _outcome_metrics(rows, eligible_setups=eligible_setups, baseline_closed=baseline_closed))


def _continuous(feature: str, definition: str, rows: tuple[TradeRow, ...], getter: Callable[[TradeRow], float | None]) -> ContinuousRelationship:
    values = tuple(getter(row) for row in rows)
    pairs = tuple((float(value), float(row.realized_r)) for row, value in zip(rows, values) if value is not None and row.realized_r is not None)
    return ContinuousRelationship(feature, definition, _summary(values), len(pairs), _pearson(pairs))


def _numeric_buckets(*, hypothesis_id: str, feature: str, definition: str, rows: tuple[TradeRow, ...], eligible_setups: int, getter: Callable[[TradeRow], float | None], edges: tuple[float, ...], labels: tuple[str, ...] | None = None) -> FeatureExperiment:
    if tuple(sorted(edges)) != edges:
        raise ValueError("bucket edges must be non-decreasing")
    present = tuple(row for row in rows if getter(row) is not None)
    groups: list[tuple[TradeRow, ...]] = []
    for index in range(len(edges) + 1):
        lower = None if index == 0 else edges[index - 1]
        upper = None if index == len(edges) else edges[index]
        groups.append(tuple(row for row in present if (lower is None or getter(row) > lower) and (upper is None or getter(row) <= upper)))
    if sum(map(len, groups)) != len(present):
        raise AssertionError("numeric buckets must exhaust non-missing rows")
    names = labels or tuple(f"Q{index + 1}" for index in range(len(groups)))
    if len(names) != len(groups):
        raise ValueError("bucket label count mismatch")
    buckets = tuple(_bucket(names[index], group, eligible_setups=eligible_setups, baseline_closed=len(rows), lower=None if index == 0 else edges[index - 1], upper=None if index == len(edges) else edges[index], lower_inclusive=index == 0, upper_inclusive=index < len(edges)) for index, group in enumerate(groups))
    missing_rows = tuple(row for row in rows if getter(row) is None)
    return FeatureExperiment(hypothesis_id, feature, definition, _continuous(feature, definition, rows, getter), edges, buckets, _bucket("missing", missing_rows, eligible_setups=eligible_setups, baseline_closed=len(rows)))


def _quartiles(*, hypothesis_id: str, feature: str, definition: str, rows: tuple[TradeRow, ...], eligible_setups: int, getter: Callable[[TradeRow], float | None]) -> FeatureExperiment:
    values = tuple(getter(row) for row in rows if getter(row) is not None)
    edges = tuple(quantiles(values, n=4, method="inclusive")) if len(values) >= 2 else ()
    return _numeric_buckets(hypothesis_id=hypothesis_id, feature=feature, definition=definition, rows=rows, eligible_setups=eligible_setups, getter=getter, edges=edges)


def _continuous_only(*, hypothesis_id: str, feature: str, definition: str, rows: tuple[TradeRow, ...], eligible_setups: int, getter: Callable[[TradeRow], float | None]) -> FeatureExperiment:
    present = tuple(row for row in rows if getter(row) is not None)
    missing = tuple(row for row in rows if getter(row) is None)
    return FeatureExperiment(
        hypothesis_id, feature, definition, _continuous(feature, definition, rows, getter), (),
        (_bucket("all_non_missing", present, eligible_setups=eligible_setups, baseline_closed=len(rows)),),
        _bucket("missing", missing, eligible_setups=eligible_setups, baseline_closed=len(rows)),
    )


def _categories(*, hypothesis_id: str, feature: str, definition: str, rows: tuple[TradeRow, ...], eligible_setups: int, getter: Callable[[TradeRow], int | None], labels: tuple[tuple[int, str], ...]) -> FeatureExperiment:
    allowed = {value for value, _ in labels}
    unexpected = {getter(row) for row in rows if getter(row) is not None} - allowed
    if unexpected:
        raise ValueError(f"unexpected {feature} values: {unexpected}")
    buckets = tuple(_bucket(label, tuple(row for row in rows if getter(row) == value), eligible_setups=eligible_setups, baseline_closed=len(rows)) for value, label in labels)
    missing = tuple(row for row in rows if getter(row) is None)
    return FeatureExperiment(hypothesis_id, feature, definition, None, (), buckets, _bucket("missing", missing, eligible_setups=eligible_setups, baseline_closed=len(rows)))


def _adr_fixed_buckets(rows: tuple[TradeRow, ...], eligible: int, definition: str) -> FeatureExperiment:
    getter = lambda row: row.adr_extension
    predicates = (
        ("<0.25", None, 0.25, True, False, lambda value: value < 0.25),
        ("[0.25,0.50)", 0.25, 0.50, True, False, lambda value: 0.25 <= value < 0.50),
        ("[0.50,1.00]", 0.50, 1.00, True, True, lambda value: 0.50 <= value <= 1.00),
        (">1.00", 1.00, None, False, False, lambda value: value > 1.00),
    )
    buckets = tuple(
        BucketResult(
            label, lower, upper, lower_inclusive, upper_inclusive,
            _outcome_metrics(
                tuple(row for row in rows if getter(row) is not None and predicate(getter(row))),
                eligible_setups=eligible, baseline_closed=len(rows),
            ),
        )
        for label, lower, upper, lower_inclusive, upper_inclusive, predicate in predicates
    )
    missing = tuple(row for row in rows if getter(row) is None)
    if sum(item.metrics.sample_count for item in buckets) + len(missing) != len(rows):
        raise AssertionError("fixed ADR buckets must exhaust the development rows")
    return FeatureExperiment(
        "ADR_EXTENSION_FIXED_BUCKETS", "adr_extension", definition,
        _continuous("adr_extension", definition, rows, getter),
        (0.25, 0.50, 1.00), buckets,
        _bucket("missing", missing, eligible_setups=eligible, baseline_closed=len(rows)),
    )


def _feature_experiments(rows: tuple[TradeRow, ...], eligible: int, config: ExperimentConfig) -> tuple[FeatureExperiment, ...]:
    validate_entry_feature_selectors("trade", ("adr_extension", "atr_extension", "utc_six_hour_bucket", "utc_weekday", "htf_bias_age_ms", "setup_age_ms", "kalman_transition_age_ms", "kalman_persistence_bars", "stop_atr_ratio", "stop_adr_ratio"))
    adr_def = "directional distance from legally available UTC-session extreme to canonical entry / prior-14-complete-date ADR"
    specs = (
        _adr_fixed_buckets(rows, eligible, adr_def),
        _quartiles(hypothesis_id="ADR_EXTENSION_DEVELOPMENT_QUARTILES", feature="adr_extension", definition=adr_def, rows=rows, eligible_setups=eligible, getter=lambda row: row.adr_extension),
        _quartiles(hypothesis_id="ATR_EXTENSION_DEVELOPMENT_QUARTILES", feature="atr_extension", definition="directional distance from legally available session extreme to entry / entry ATR", rows=rows, eligible_setups=eligible, getter=lambda row: row.atr_extension),
        _categories(hypothesis_id="UTC_SIX_HOUR_BUCKET", feature="utc_six_hour_bucket", definition="entry source-open UTC hour in deterministic six-hour intervals", rows=rows, eligible_setups=eligible, getter=lambda row: row.utc_six_hour_bucket, labels=((0, "00:00-05:59"), (1, "06:00-11:59"), (2, "12:00-17:59"), (3, "18:00-23:59"))),
        _categories(hypothesis_id="UTC_WEEKDAY", feature="utc_weekday", definition="entry source-open UTC weekday, Monday=0", rows=rows, eligible_setups=eligible, getter=lambda row: row.utc_weekday, labels=tuple(enumerate(("Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday")))),
    )
    numeric = (
        ("HTF_BIAS_AGE_DEVELOPMENT_QUARTILES", "htf_bias_age_bars", "elapsed since latest available 4H bias change / 15m", lambda row: None if row.htf_bias_age_ms is None else row.htf_bias_age_ms / BAR_MS),
        ("SETUP_AGE_DEVELOPMENT_QUARTILES", "setup_age_bars", "entry decision minus fresh HEMA setup origin / 15m", lambda row: row.setup_age_ms / BAR_MS),
        ("KALMAN_TRANSITION_AGE_DEVELOPMENT_QUARTILES", "kalman_transition_age_bars", "elapsed since latest available Kalman transition / 15m", lambda row: None if row.kalman_transition_age_ms is None else row.kalman_transition_age_ms / BAR_MS),
        ("KALMAN_PERSISTENCE_DEVELOPMENT_QUARTILES", "kalman_persistence_bars", "entry-time consecutive finalized 15m bars in current Kalman direction", lambda row: None if row.kalman_persistence_bars is None else float(row.kalman_persistence_bars)),
        ("STOP_ADR_DEVELOPMENT_QUARTILES", "stop_adr_ratio", "absolute canonical stop distance / prior-14-complete-date ADR", lambda row: row.stop_adr_ratio),
    )
    return specs + tuple(_quartiles(hypothesis_id=hypothesis, feature=feature, definition=definition, rows=rows, eligible_setups=eligible, getter=getter) for hypothesis, feature, definition, getter in numeric) + (
        _continuous_only(
            hypothesis_id="STOP_ATR_CONTEXT",
            feature="stop_atr_ratio",
            definition="absolute canonical stop distance / entry ATR; expected to equal the frozen ATR multiplier apart from floating representation",
            rows=rows,
            eligible_setups=eligible,
            getter=lambda row: row.stop_atr_ratio,
        ),
    )


def _counterfactual_metrics(rows: tuple[_CounterfactualOutcome, ...], *, eligible: int, baseline_closed: int) -> OutcomeMetrics:
    rs = tuple(row.realized_r for row in rows)
    gains, losses = sum(value for value in rs if value > 0), sum(value for value in rs if value < 0)
    maes = tuple(row.mae_r for row in rows if row.mae_r is not None)
    mfes = tuple(row.mfe_r for row in rows if row.mfe_r is not None)
    total = float(sum(rs))
    metrics = ResearchMetrics(eligible, len(rows), len(rows), len(rows), len(rows), None if not eligible else len(rows) / eligible, None if not baseline_closed else len(rows) / baseline_closed, total, None if not rows else total / len(rows), None if not eligible else total / eligible, None if losses == 0 else gains / abs(losses), None if not rows else sum(row.stop_hit for row in rows) / len(rows), None if not rows else sum(value > 0 for value in rs) / len(rows), None if not rows else mean(rs), None if not rows else median(rs), len(maes), len(mfes), None if not maes else mean(maes), None if not mfes else mean(mfes), None if not rows else mean(row.duration_ms for row in rows))
    return _from_metrics(metrics, sample_count=len(rows), eligible_setups=eligible, baseline_closed=baseline_closed)


def _execution_price(price: float, side: Direction, *, entry: bool, slippage_bps: float) -> float:
    fraction = slippage_bps / 10_000.0
    if entry:
        return price * (1 + fraction if side is Direction.LONG else 1 - fraction)
    return price * (1 - fraction if side is Direction.LONG else 1 + fraction)


def _stop_fill(side: Direction, stop: float, bar: HistoricalBar) -> float | None:
    """Frozen Phase 2 stop touch and adverse opening-gap fill semantics."""
    if side is Direction.LONG and bar.low <= stop:
        return bar.open if bar.open < stop else stop
    if side is Direction.SHORT and bar.high >= stop:
        return bar.open if bar.open > stop else stop
    return None


def _accounted_r(*, side: Direction, entry: float, exit: float, risk: float, quantity: float, fee_bps: float, slippage_bps: float) -> float:
    entry_exec = _execution_price(entry, side, entry=True, slippage_bps=slippage_bps)
    exit_exec = _execution_price(exit, side, entry=False, slippage_bps=slippage_bps)
    sign = 1 if side is Direction.LONG else -1
    gross = sign * (exit_exec - entry_exec) * quantity
    fees = (entry_exec + exit_exec) * quantity * fee_bps / 10_000
    return (gross - fees) / (risk * quantity)


def _excursion_r(*, side: Direction, entry: float, risk: float, bars: tuple[HistoricalBar, ...], contiguous: bool) -> tuple[float | None, float | None]:
    if not contiguous:
        return None, None
    if side is Direction.LONG:
        adverse = max((entry - item.low for item in bars), default=0)
        favorable = max((item.high - entry for item in bars), default=0)
    else:
        adverse = max((item.high - entry for item in bars), default=0)
        favorable = max((entry - item.low for item in bars), default=0)
    return max(0, adverse) / risk, max(0, favorable) / risk


def _delay_outcome(*, row: TradeRow, delay: int, traces: tuple[ReplayTrace, ...], indexes: dict[int, int], bundle: CanonicalResearchBundle) -> tuple[_CounterfactualOutcome | None, str | None]:
    entry_index = indexes.get(row.decision_timestamp)
    if entry_index is None:
        raise ValueError(f"baseline entry trace unavailable: {row.trade_id}")
    delayed_index = entry_index + delay
    if delayed_index >= len(traces):
        return None, "insufficient_subsequent_bars"
    delayed = traces[delayed_index]
    bar = delayed.source_bar
    if row.exit_timestamp is None or bar.finalized_at >= row.exit_timestamp:
        return None, "baseline_not_open_strictly_after_delayed_decision"
    if delayed.strategy_bar is None or delayed.strategy_bar.atr is None or delayed.strategy_bar.atr <= 0:
        return None, "delayed_atr_unavailable"
    entry, atr = bar.close, delayed.strategy_bar.atr
    risk = atr * bundle.strategy_config.atr_multiplier
    stop = entry - risk if row.direction is Direction.LONG else entry + risk
    exit_index = indexes.get(row.exit_timestamp)
    if exit_index is None or exit_index <= delayed_index or row.canonical_exit_price is None:
        raise ValueError(f"baseline exit trace unavailable: {row.trade_id}")
    exit_price, exit_timestamp, stopped = row.canonical_exit_price, row.exit_timestamp, False
    observed: list[HistoricalBar] = []
    expected, contiguous = bar.finalized_at + BAR_MS, True
    for trace in traces[delayed_index + 1:exit_index + 1]:
        candidate = trace.source_bar
        contiguous = contiguous and candidate.finalized_at == expected
        expected = candidate.finalized_at + BAR_MS
        observed.append(candidate)
        stop_fill = _stop_fill(row.direction, stop, candidate)
        if stop_fill is not None:
            exit_price, exit_timestamp, stopped = stop_fill, candidate.finalized_at, True
            break
    cfg = bundle.backtest_config
    realized_r = _accounted_r(
        side=row.direction, entry=entry, exit=exit_price, risk=risk,
        quantity=cfg.quantity, fee_bps=cfg.fee_bps, slippage_bps=cfg.slippage_bps,
    )
    mae_r, mfe_r = _excursion_r(
        side=row.direction, entry=entry, risk=risk,
        bars=tuple(observed), contiguous=contiguous,
    )
    return _CounterfactualOutcome(
        row.trade_id, entry, stop, exit_price, exit_timestamp,
        "delayed_stop" if stopped else "baseline_exit_clock_and_price",
        realized_r, stopped, mae_r, mfe_r, exit_timestamp - bar.finalized_at,
    ), None


def _delay_experiments(bundle: CanonicalResearchBundle, setups: tuple[SetupRow, ...], rows: tuple[TradeRow, ...], config: ExperimentConfig) -> tuple[DelayExperiment, ...]:
    traces = tuple(trace for trace in bundle.replay.traces if trace.source_bar.timeframe is Timeframe.MINUTES_15)
    indexes = {trace.source_bar.finalized_at: index for index, trace in enumerate(traces)}
    if len(indexes) != len(traces):
        raise ValueError("duplicate finalized 15m timestamps")
    eligible, baseline_closed = len(setups), len(rows)
    baseline_metrics = _outcome_metrics(rows, eligible_setups=eligible, baseline_closed=baseline_closed)
    results = []
    for delay in config.delays:
        identity = f"PAIRED_BASELINE_EXIT_CLOCK_N{delay}"
        if delay == 0:
            results.append(DelayExperiment(identity, delay, "exact frozen baseline; no counterfactual timing change", eligible, baseline_closed, baseline_closed, eligible - baseline_closed, 0, (), baseline_metrics, tuple(row.trade_id for row in rows)))
            continue
        outcomes, skipped = [], Counter()
        for row in rows:
            outcome, reason = _delay_outcome(row=row, delay=delay, traces=traces, indexes=indexes, bundle=bundle)
            if outcome is None:
                skipped[reason] += 1
            else:
                outcomes.append(outcome)
        outcome_rows = tuple(outcomes)
        results.append(DelayExperiment(identity, delay, "research-only Nth-subsequent-15m close entry; original trade must remain open strictly afterward; delayed ATR stop begins next bar with frozen gap priority; otherwise paired original exit clock/price", eligible, baseline_closed, len(outcome_rows), eligible - baseline_closed, baseline_closed - len(outcome_rows), tuple(sorted(skipped.items())), _counterfactual_metrics(outcome_rows, eligible=eligible, baseline_closed=baseline_closed), tuple(row.trade_id for row in outcome_rows)))
    return tuple(results)


def development_report(bundle: CanonicalResearchBundle, config: ExperimentConfig | None = None) -> DevelopmentExperimentReport:
    """Run registered hypotheses without exposing validation or final OOS rows."""
    config = config or ExperimentConfig()
    setups, rows = _development_population(bundle)
    baseline = _outcome_metrics(rows, eligible_setups=len(setups), baseline_closed=len(rows))
    return DevelopmentExperimentReport(EXPERIMENT_SCHEMA_VERSION, "DEVELOPMENT ONLY. Buckets are not rules; paired delays are not production simulations. Validation and final OOS outcomes are inaccessible.", DEVELOPMENT_WINDOW.role, DEVELOPMENT_WINDOW.start_date, DEVELOPMENT_WINDOW.end_date, bundle.dataset.manifest_id, fingerprint(bundle.dataset), config, fingerprint(config), len(setups), len(rows), baseline, _feature_experiments(rows, len(setups), config), _delay_experiments(bundle, setups, rows, config), ("validation", "final_oos"))


def experiment_json(report: DevelopmentExperimentReport) -> bytes:
    return json.dumps(asdict(report), sort_keys=True, separators=(",", ":"), allow_nan=False).encode("utf-8") + b"\n"


def write_experiment_report(
    report: DevelopmentExperimentReport, path: Path, *, overwrite: bool = False
) -> None:
    if path.exists() and not overwrite:
        raise FileExistsError(f"refusing to overwrite existing report: {path}")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(experiment_json(report))
