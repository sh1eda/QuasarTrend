"""Canonical deterministic Phase 7 baseline pipeline (no experiments)."""

from __future__ import annotations

from collections import Counter
from dataclasses import asdict, dataclass
import json
from pathlib import Path

from quasartrend.backtest import BacktestConfig, BacktestEngine, BacktestResult
from quasartrend.replay import (
    HistoricalBar,
    ReplayConfig,
    ReplayEngine,
    ReplayResult,
    Timeframe,
)
from quasartrend.strategy import StrategyConfig

from .adr import utc_date
from .dataset import build_research_dataset
from .metrics import calculate_metrics
from .models import (
    AdrStatus,
    ResearchBuildContext,
    ResearchConfig,
    ResearchDataset,
    ResearchMetrics,
    SessionStatus,
    SetupRow,
    SplitConfig,
    TradeRow,
)
from .provenance import fingerprint, make_manifest, make_source_artifact
from .source import parse_tradingview_export
from .splits import ChronologicalWindow


BASELINE_REPORT_SCHEMA_VERSION = "phase7-baseline-report/v1"


@dataclass(frozen=True, slots=True)
class CanonicalResearchBundle:
    dataset: ResearchDataset
    replay: ReplayResult
    backtest: BacktestResult
    source_counts: tuple[tuple[str, int], ...]
    source_day_counts: tuple[tuple[str, tuple[tuple[str, int], ...]], ...]
    replay_config: ReplayConfig
    strategy_config: StrategyConfig
    backtest_config: BacktestConfig
    research_config: ResearchConfig
    split_config: SplitConfig


@dataclass(frozen=True, slots=True)
class WindowBaselineReport:
    role: str
    start_date: str
    end_date: str
    observed_setups: int
    eligible_setups: int
    armed: int
    immediate_opened: int
    cancelled: int
    opened_setups: int
    included_closed: int
    purged_entry_exit_boundary: int
    purged_setup_boundary: int
    censored: int
    metrics: ResearchMetrics
    evidence_floor_pass: bool
    evidence_floor_reasons: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class BaselineReport:
    schema_version: str
    phase6_sha: str
    manifest_id: str
    dataset_fingerprint: str
    declared_symbol: str
    source_identity_status: str
    source_artifacts: tuple[object, ...]
    merged_source_bar_count: int
    source_day_counts: tuple[tuple[str, tuple[tuple[str, int], ...]], ...]
    effective_research_date_range: tuple[str, str]
    top_level_population_scope: str
    effective_population_counts: tuple[tuple[str, int], ...]
    incomplete_source_dates: tuple[tuple[str, str, int, int], ...]
    excluded_dates_and_reasons: tuple[tuple[str, str], ...]
    replay_config: ReplayConfig
    strategy_config: StrategyConfig
    backtest_config: BacktestConfig
    research_config: ResearchConfig
    split_config: SplitConfig
    replay_fingerprint: str
    strategy_fingerprint: str
    backtest_fingerprint: str
    research_fingerprint: str
    split_fingerprint: str
    observed_flips: int
    eligible_setups: int
    noneligible_rejected: int
    armed: int
    cancelled: int
    opened_trades: int
    closed_trades: int
    censored_trades: int
    metrics: ResearchMetrics
    observed_setup_direction_counts: tuple[tuple[str, int], ...]
    eligible_setup_direction_counts: tuple[tuple[str, int], ...]
    trade_direction_counts: tuple[tuple[str, int], ...]
    exit_reason_counts: tuple[tuple[str, int], ...]
    setup_resolution_reason_counts: tuple[tuple[str, int], ...]
    quality_counts: tuple[tuple[str, int], ...]
    windows: tuple[WindowBaselineReport, ...]


def _merge_streams(
    first: tuple[HistoricalBar, ...], second: tuple[HistoricalBar, ...]
) -> tuple[HistoricalBar, ...]:
    """Two-way processing-key merge; never repair an invalid source stream."""
    result: list[HistoricalBar] = []
    left = 0
    right = 0
    prior: tuple[int, int] | None = None
    while left < len(first) or right < len(second):
        if right == len(second) or (
            left < len(first)
            and first[left].processing_key < second[right].processing_key
        ):
            bar = first[left]
            left += 1
        elif left == len(first) or second[right].processing_key < first[left].processing_key:
            bar = second[right]
            right += 1
        else:
            raise ValueError("duplicate equal processing key in MTF merge")
        if prior is not None and bar.processing_key <= prior:
            raise ValueError("non-increasing MTF merge")
        prior = bar.processing_key
        result.append(bar)
    return tuple(result)


def _counts(values: list[str] | tuple[str, ...]) -> tuple[tuple[str, int], ...]:
    return tuple(sorted(Counter(values).items()))


def _day_counts(bars: tuple[HistoricalBar, ...]) -> tuple[tuple[str, int], ...]:
    return _counts([utc_date(bar.open_time) for bar in bars])


def build_canonical_bundle(
    *,
    golden_15m: Path,
    golden_4h: Path,
    declared_symbol: str = "BTCUSDT",
    replay_config: ReplayConfig | None = None,
    strategy_config: StrategyConfig | None = None,
    backtest_config: BacktestConfig | None = None,
    research_config: ResearchConfig | None = None,
    split_config: SplitConfig | None = None,
) -> CanonicalResearchBundle:
    if (
        golden_15m.resolve() == golden_4h.resolve()
        or not golden_15m.is_file()
        or not golden_4h.is_file()
    ):
        raise ValueError("require distinct existing 15m and 4H source paths")
    replay_cfg = replay_config or ReplayConfig()
    strategy_cfg = strategy_config or StrategyConfig()
    backtest_cfg = backtest_config or BacktestConfig()
    research_cfg = research_config or ResearchConfig()
    split_cfg = split_config or SplitConfig()
    raw_15m = golden_15m.read_bytes()
    raw_4h = golden_4h.read_bytes()
    if raw_15m == raw_4h:
        raise ValueError("15m and 4H source content must be distinct")
    bars_15m = parse_tradingview_export(
        raw_15m, declared_symbol=declared_symbol, timeframe=Timeframe.MINUTES_15
    )
    bars_4h = parse_tradingview_export(
        raw_4h, declared_symbol=declared_symbol, timeframe=Timeframe.HOURS_4
    )
    source_values_15m = tuple(
        (bar.open_time, bar.open, bar.high, bar.low, bar.close) for bar in bars_15m
    )
    source_values_4h = tuple(
        (bar.open_time, bar.open, bar.high, bar.low, bar.close) for bar in bars_4h
    )
    if source_values_15m == source_values_4h:
        raise ValueError("15m and 4H normalized source content must be distinct")
    merged = _merge_streams(bars_15m, bars_4h)
    replay = ReplayEngine(replay_cfg, strategy_cfg).run(merged)
    backtest = BacktestEngine(backtest_cfg).run(replay)
    artifacts = (
        make_source_artifact(
            declared_symbol=declared_symbol, timeframe="15m", raw_input=raw_15m
        ),
        make_source_artifact(
            declared_symbol=declared_symbol, timeframe="4h", raw_input=raw_4h
        ),
    )
    manifest = make_manifest(
        source_artifacts=artifacts,
        phase6_sha=research_cfg.phase6_sha,
        source_description="declared TradingView CSV exports",
        source_reference="tradingview_dual_ohlc_declared_v1",
        strategy_config=strategy_cfg,
        replay_config=replay_cfg,
        backtest_config=backtest_cfg,
        research_config=research_cfg,
        split_config=split_cfg,
    )
    dataset = build_research_dataset(
        replay,
        backtest,
        context=ResearchBuildContext(
            manifest,
            (("15m", raw_15m), ("4h", raw_4h)),
            replay_cfg,
            strategy_cfg,
            backtest_cfg,
            research_cfg,
            split_cfg,
        ),
    )
    return CanonicalResearchBundle(
        dataset=dataset,
        replay=replay,
        backtest=backtest,
        source_counts=(("15m", len(bars_15m)), ("4h", len(bars_4h))),
        source_day_counts=(("15m", _day_counts(bars_15m)), ("4h", _day_counts(bars_4h))),
        replay_config=replay_cfg,
        strategy_config=strategy_cfg,
        backtest_config=backtest_cfg,
        research_config=research_cfg,
        split_config=split_cfg,
    )


def _window_report(
    *,
    role: str,
    start_date: str,
    end_date: str,
    setups: tuple[SetupRow, ...],
    trades: tuple[TradeRow, ...],
    research_config: ResearchConfig,
) -> WindowBaselineReport:
    window = ChronologicalWindow(role, start_date, end_date)

    def inside(timestamp: int) -> bool:
        return window.start_ms <= timestamp <= window.end_ms

    observed = tuple(row for row in setups if inside(row.decision_timestamp))
    eligible = tuple(row for row in observed if row.eligible_baseline_setup)
    setup_by_id = {row.setup_id: row for row in setups}
    included: list[TradeRow] = []
    purged_setup = 0
    purged_entry_exit = 0
    censored = 0
    for trade in trades:
        setup = setup_by_id[trade.setup_id]
        setup_inside = inside(setup.decision_timestamp)
        entry_inside = inside(trade.decision_timestamp)
        exit_inside = (
            trade.exit_timestamp is not None and inside(trade.exit_timestamp)
        )
        if not (setup_inside or entry_inside or exit_inside):
            continue
        if trade.outcome_state == "censored":
            if entry_inside and not setup_inside:
                purged_setup += 1
            elif setup_inside and not entry_inside:
                purged_entry_exit += 1
            elif entry_inside:
                censored += 1
        elif entry_inside and not setup_inside:
            purged_setup += 1
        elif not (entry_inside and exit_inside):
            purged_entry_exit += 1
        else:
            included.append(trade)

    metrics = calculate_metrics(tuple(included), eligible_setups=len(eligible))
    reasons: list[str] = []
    if metrics.closed_trades < research_config.final_oos_closed_trade_floor:
        reasons.append(
            f"closed trades {metrics.closed_trades} below floor "
            f"{research_config.final_oos_closed_trade_floor}"
        )
    if not research_config.production_lineage_enabled:
        reasons.append("production lineage runner disabled")
    return WindowBaselineReport(
        role=role,
        start_date=start_date,
        end_date=end_date,
        observed_setups=len(observed),
        eligible_setups=len(eligible),
        armed=sum(row.was_armed for row in observed),
        immediate_opened=sum(
            row.setup_status.value == "opened" and not row.was_armed for row in observed
        ),
        cancelled=sum(row.setup_status.value == "cancelled" for row in observed),
        opened_setups=sum(row.setup_status.value == "opened" for row in observed),
        included_closed=len(included),
        purged_entry_exit_boundary=purged_entry_exit,
        purged_setup_boundary=purged_setup,
        censored=censored,
        metrics=metrics,
        evidence_floor_pass=not reasons,
        evidence_floor_reasons=tuple(reasons),
    )


def baseline_report(bundle: CanonicalResearchBundle) -> BaselineReport:
    setups = bundle.dataset.setup_rows
    trades = bundle.dataset.trade_rows
    eligible = tuple(row for row in setups if row.eligible_baseline_setup)
    metrics = calculate_metrics(trades, eligible_setups=len(eligible))
    quality = {
        "setup_adr_available": sum(row.adr_status is AdrStatus.AVAILABLE for row in setups),
        "setup_adr_warmup": sum(row.adr_status is AdrStatus.WARMUP for row in setups),
        "setup_adr_incomplete_prior_session": sum(
            row.adr_status is AdrStatus.INCOMPLETE_PRIOR_SESSION for row in setups
        ),
        "trade_adr_available": sum(row.adr_status is AdrStatus.AVAILABLE for row in trades),
        "trade_adr_warmup": sum(row.adr_status is AdrStatus.WARMUP for row in trades),
        "trade_adr_incomplete_prior_session": sum(
            row.adr_status is AdrStatus.INCOMPLETE_PRIOR_SESSION for row in trades
        ),
        "setup_missing_adr_extension": sum(row.adr_extension is None for row in setups),
        "trade_missing_adr_extension": sum(row.adr_extension is None for row in trades),
        "setup_incomplete_session_prefix": sum(
            row.session_status is SessionStatus.INCOMPLETE_PREFIX for row in setups
        ),
        "trade_incomplete_session_prefix": sum(
            row.session_status is SessionStatus.INCOMPLETE_PREFIX for row in trades
        ),
        "post_entry_15m_gap": sum(
            "post_entry_15m_gap" in row.data_quality_flags for row in trades
        ),
        "missing_mae_mfe": sum(row.mae_r is None for row in trades),
        "censored_trades": sum(row.outcome_state == "censored" for row in trades),
    }
    window_specs = (
        ("development", bundle.split_config.development_start, bundle.split_config.development_end),
        ("validation", bundle.split_config.validation_start, bundle.split_config.validation_end),
        ("final_oos", bundle.split_config.final_oos_start, bundle.split_config.final_oos_end),
    )
    windows = tuple(
        _window_report(
            role=role,
            start_date=start,
            end_date=end,
            setups=setups,
            trades=trades,
            research_config=bundle.research_config,
        )
        for role, start, end in window_specs
    )
    day_counts = dict(bundle.source_day_counts)["15m"]
    incomplete_dates = tuple(
        (timeframe, date, count, expected)
        for timeframe, expected in (("15m", 96), ("4h", 6))
        for date, count in dict(bundle.source_day_counts)[timeframe]
        if count != expected
    )
    effective_range = (
        bundle.split_config.development_start,
        bundle.split_config.final_oos_end,
    )
    excluded_dates = tuple(
        (f"{timeframe}:{date}", f"incomplete {timeframe} UTC session")
        for timeframe, date, _, _ in incomplete_dates
    ) + (("before 2026-05-15", "ADR warmup/development exclusion"),)
    effective_start = ChronologicalWindow(
        "effective", effective_range[0], effective_range[1]
    ).start_ms
    effective_end = ChronologicalWindow(
        "effective", effective_range[0], effective_range[1]
    ).end_ms
    effective_population = {
        "observed_setups_before": sum(row.decision_timestamp < effective_start for row in setups),
        "observed_setups_within": sum(
            effective_start <= row.decision_timestamp <= effective_end for row in setups
        ),
        "observed_setups_after": sum(row.decision_timestamp > effective_end for row in setups),
        "trades_before": sum(row.decision_timestamp < effective_start for row in trades),
        "trades_within": sum(
            effective_start <= row.decision_timestamp <= effective_end for row in trades
        ),
        "trades_after": sum(row.decision_timestamp > effective_end for row in trades),
    }
    return BaselineReport(
        schema_version=BASELINE_REPORT_SCHEMA_VERSION,
        phase6_sha=bundle.research_config.phase6_sha,
        manifest_id=bundle.dataset.manifest_id,
        dataset_fingerprint=fingerprint(bundle.dataset),
        declared_symbol=bundle.dataset.manifest.source_artifacts[0].declared_symbol,
        source_identity_status=bundle.dataset.manifest.source_artifacts[0].identity_status,
        source_artifacts=bundle.dataset.manifest.source_artifacts,
        merged_source_bar_count=sum(value for _, value in bundle.source_counts),
        source_day_counts=bundle.source_day_counts,
        effective_research_date_range=effective_range,
        top_level_population_scope="full_source_history",
        effective_population_counts=tuple(sorted(effective_population.items())),
        incomplete_source_dates=incomplete_dates,
        excluded_dates_and_reasons=excluded_dates,
        replay_config=bundle.replay_config,
        strategy_config=bundle.strategy_config,
        backtest_config=bundle.backtest_config,
        research_config=bundle.research_config,
        split_config=bundle.split_config,
        replay_fingerprint=bundle.dataset.replay_fingerprint,
        strategy_fingerprint=bundle.dataset.strategy_fingerprint,
        backtest_fingerprint=bundle.dataset.backtest_fingerprint,
        research_fingerprint=bundle.dataset.research_fingerprint,
        split_fingerprint=bundle.dataset.split_fingerprint,
        observed_flips=len(setups),
        eligible_setups=len(eligible),
        noneligible_rejected=sum(not row.eligible_baseline_setup for row in setups),
        armed=sum(row.was_armed for row in setups),
        cancelled=sum(row.setup_status.value == "cancelled" for row in setups),
        opened_trades=len(trades),
        closed_trades=sum(row.outcome_state == "closed" for row in trades),
        censored_trades=sum(row.outcome_state == "censored" for row in trades),
        metrics=metrics,
        observed_setup_direction_counts=_counts([row.direction.value for row in setups]),
        eligible_setup_direction_counts=_counts([row.direction.value for row in eligible]),
        trade_direction_counts=_counts([row.direction.value for row in trades]),
        exit_reason_counts=_counts(
            [row.exit_primary_reason for row in trades if row.exit_primary_reason]
        ),
        setup_resolution_reason_counts=_counts(
            [reason for row in setups for reason in row.resolution_reasons]
        ),
        quality_counts=tuple(sorted(quality.items())),
        windows=windows,
    )


def report_json(report: BaselineReport) -> bytes:
    return (
        json.dumps(asdict(report), sort_keys=True, separators=(",", ":"), allow_nan=False)
        .encode()
        + b"\n"
    )


def write_report(report: BaselineReport, output: Path, *, overwrite: bool = False) -> None:
    if output.exists() and not overwrite:
        raise FileExistsError("refusing to overwrite existing report")
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_bytes(report_json(report))
