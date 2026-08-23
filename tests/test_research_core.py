from __future__ import annotations
from datetime import UTC, datetime, timedelta
import pytest
from quasartrend.backtest import BacktestConfig, BacktestEngine
from quasartrend.replay import HistoricalBar, ReplayConfig, ReplayEngine, Timeframe
from quasartrend.research import (AdrStatus, PHASE6_SHA, ResearchBuildContext, adr_context_for_date, build_research_dataset, daily_ranges, fingerprint, make_manifest, make_source_artifact, schema_map, validate_entry_feature_selectors)
from quasartrend.strategy import EventType, StrategyConfig
from pathlib import Path
import math
from dataclasses import replace

def _bars(days=15):
    start=datetime(2026,1,1,tzinfo=UTC); out=[]
    for d in range(days):
        for slot in range(96):
            t=int((start+timedelta(days=d,minutes=15*slot)).timestamp()*1000)
            out.append(HistoricalBar("BTCUSDT",Timeframe.MINUTES_15,t,100,102,99,101))
    return tuple(out)


def _raw_export(timestamp: int) -> bytes:
    seconds = timestamp // 1000
    return (
        "time,open,high,low,close,timestamp,open,high,low,close\n"
        f"{seconds},1,2,0.5,1.5,{timestamp},1,2,0.5,1.5\n"
    ).encode()


def _raw_bars(bars: tuple[HistoricalBar, ...] | list[HistoricalBar]) -> bytes:
    lines = ["time,open,high,low,close,timestamp,open,high,low,close"]
    for bar in bars:
        values = (
            bar.open_time // 1000, bar.open, bar.high, bar.low, bar.close,
            bar.open_time, bar.open, bar.high, bar.low, bar.close,
        )
        lines.append(",".join(str(value) for value in values))
    return ("\n".join(lines) + "\n").encode()


def _mini_build():
    bar = HistoricalBar("BTCUSDT", Timeframe.MINUTES_15, 0, 1, 2, .5, 1.5)
    htf = HistoricalBar("BTCUSDT", Timeframe.HOURS_4, 0, 1, 2, .5, 1.5)
    rc, sc, bc = ReplayConfig(), StrategyConfig(), BacktestConfig()
    replay = ReplayEngine(rc, sc).run(tuple(sorted((bar, htf), key=lambda value: value.processing_key)))
    backtest = BacktestEngine(bc).run(replay)
    raw = _raw_export(0)
    manifest = make_manifest(source_artifacts=(make_source_artifact(declared_symbol="BTCUSDT", timeframe="15m", raw_input=raw), make_source_artifact(declared_symbol="BTCUSDT", timeframe="4h", raw_input=raw)), phase6_sha=PHASE6_SHA, source_description="declared", source_reference=None, strategy_config=sc, replay_config=rc, backtest_config=bc, research_config={"v": 1}, split_config={"v": 1})
    return replay, backtest, ResearchBuildContext(manifest, (("15m", raw), ("4h", raw)), rc, sc, bc, {"v": 1}, {"v": 1})


@pytest.fixture(scope="module")
def golden_bundle():
    path15 = Path("tests/golden/tradingview_15m.csv")
    path4 = Path("tests/golden/tradingview_4h.csv")
    if not path15.exists() or not path4.exists():
        pytest.skip("golden exports unavailable")

    from quasartrend.indicators.golden import load_tradingview_export

    all_bars = []
    streams = {}
    for path, timeframe in (
        (path15, Timeframe.MINUTES_15),
        (path4, Timeframe.HOURS_4),
    ):
        _, exported = load_tradingview_export(path)
        converted = tuple(
            HistoricalBar(
                "BTCUSDT", timeframe, row.timestamp_ms, row.candle.open,
                row.candle.high, row.candle.low, row.candle.close,
                None if not math.isfinite(row.candle.volume) else row.candle.volume,
            )
            for row in exported
        )
        streams[timeframe] = converted
        all_bars.extend(converted)

    replay_config = ReplayConfig()
    strategy_config = StrategyConfig()
    backtest_config = BacktestConfig()
    replay = ReplayEngine(replay_config, strategy_config).run(
        sorted(all_bars, key=lambda bar: bar.processing_key)
    )
    backtest = BacktestEngine(backtest_config).run(replay)
    manifest = make_manifest(
        source_artifacts=(
            make_source_artifact(
                declared_symbol="BTCUSDT", timeframe="15m",
                raw_input=path15.read_bytes(),
            ),
            make_source_artifact(
                declared_symbol="BTCUSDT", timeframe="4h",
                raw_input=path4.read_bytes(),
            ),
        ),
        phase6_sha=PHASE6_SHA,
        source_description="declared golden export",
        source_reference="tests/golden",
        strategy_config=strategy_config,
        replay_config=replay_config,
        backtest_config=backtest_config,
        research_config={"v": 1},
        split_config={"v": 1},
    )
    context = ResearchBuildContext(
        manifest,
        (("15m", path15.read_bytes()), ("4h", path4.read_bytes())),
        replay_config,
        strategy_config,
        backtest_config,
        {"v": 1},
        {"v": 1},
    )
    dataset = build_research_dataset(replay, backtest, context=context)
    return replay, backtest, context, dataset, streams

def test_adr_never_skips_missing_calendar_session():
    bars=_bars(16); missing=bars[:96]+bars[192:]; date=datetime.fromtimestamp(missing[-1].open_time/1000,UTC).date().isoformat()
    c=adr_context_for_date(date,daily_ranges(missing))
    assert c.adr is None and c.status is AdrStatus.INCOMPLETE_PRIOR_SESSION

def test_schema_blocks_every_outcome_and_accounting_field():
    mapping=schema_map()
    assert all(value.value in {"entry_time_feature","post_entry_outcome","identity_metadata"} for table in mapping.values() for value in table.values())
    for table,field in (("trade","quantity"),("trade","realized_r"),("setup","eligible_baseline_setup"),("trade","outcome_state"),("trade","execution_entry_price")):
        with pytest.raises(ValueError): validate_entry_feature_selectors(table,(field,))

def test_provenance_bound_build_rejects_mutated_manifest():
    bar=HistoricalBar("BTCUSDT",Timeframe.MINUTES_15,0,1,2,.5,1.5); htf=HistoricalBar("BTCUSDT",Timeframe.HOURS_4,0,1,2,.5,1.5); rc=ReplayConfig(); sc=StrategyConfig(); bc=BacktestConfig(); replay=ReplayEngine(rc,sc).run(tuple(sorted((bar,htf),key=lambda value:value.processing_key))); backtest=BacktestEngine(bc).run(replay)
    raw15 = _raw_export(0); raw4 = _raw_export(0)
    manifest=make_manifest(source_artifacts=(make_source_artifact(declared_symbol="BTCUSDT",timeframe="15m",raw_input=raw15),make_source_artifact(declared_symbol="BTCUSDT",timeframe="4h",raw_input=raw4)),phase6_sha=PHASE6_SHA,source_description="declared",source_reference=None,strategy_config=sc,replay_config=rc,backtest_config=bc,research_config={"v":1},split_config={"v":1})
    ctx=ResearchBuildContext(manifest,(("15m",raw15),("4h",raw4)),rc,sc,bc,{"v":1},{"v":1})
    result=build_research_dataset(replay,backtest,context=ctx)
    assert result.manifest_id==fingerprint(manifest)
    with pytest.raises(ValueError,match="source CSV"):
        build_research_dataset(replay,backtest,context=ResearchBuildContext(manifest,(("15m",b"wrong"),("4h",raw4)),rc,sc,bc,{"v":1},{"v":1}))

def test_golden_replay_setup_linkage_counts_are_provenance_bound(golden_bundle):
    _, _, _, dataset, _ = golden_bundle
    assert len(dataset.setup_rows)==523
    assert sum(row.eligible_baseline_setup for row in dataset.setup_rows)==260
    assert sum(row.was_armed for row in dataset.setup_rows)==158
    assert sum(row.eligible_baseline_setup and not row.was_armed for row in dataset.setup_rows)==102
    assert sum(row.setup_status.value=="cancelled" for row in dataset.setup_rows)==68
    assert len(dataset.trade_rows)==192


def test_build_rejects_forged_strategy_bar():
    replay, backtest, context = _mini_build()
    trace = replay.traces[0]
    forged = replace(trace, strategy_bar=replace(trace.strategy_bar, close=2.0))
    with pytest.raises(ValueError, match="strategy bar"):
        build_research_dataset(replace(replay, traces=(forged, *replay.traces[1:])), backtest, context=context)


def test_build_rejects_mutated_backtest():
    replay, backtest, context = _mini_build()
    with pytest.raises(ValueError, match="exact accounting"):
        build_research_dataset(replay, replace(backtest, diagnostics=("mutated",)), context=context)


def test_build_rejects_altered_replay_config():
    replay, backtest, context = _mini_build()
    with pytest.raises(ValueError, match="configuration"):
        build_research_dataset(replay, backtest, context=replace(context, replay_config=ReplayConfig(kalman_period=22)))


@pytest.mark.parametrize("timeframe", ("15m", "4h"))
def test_build_rejects_valid_but_different_15m_or_4h_payload(timeframe):
    replay, backtest, context = _mini_build()
    altered = _raw_export(0).replace(b"0,1,2,0.5,1.5", b"0,1.1,2,0.5,1.5")
    inputs = dict(context.raw_inputs)
    inputs[timeframe] = altered
    with pytest.raises(ValueError):
        build_research_dataset(
            replay,
            backtest,
            context=replace(context, raw_inputs=tuple(inputs.items())),
        )


@pytest.mark.parametrize(
    "mutation",
    ("schema", "feature", "parser", "row_count", "date_range", "identity"),
)
def test_build_rejects_manifest_schema_feature_and_artifact_metadata_mutations(mutation):
    replay, backtest, context = _mini_build()
    manifest = context.manifest
    artifacts = list(manifest.source_artifacts)
    if mutation == "schema":
        manifest = replace(manifest, schema_version="bad")
    elif mutation == "feature":
        manifest = replace(manifest, feature_definition_version="bad")
    elif mutation == "parser":
        artifacts[0] = replace(artifacts[0], parser_id="bad")
        manifest = replace(manifest, source_artifacts=tuple(artifacts))
    elif mutation == "row_count":
        artifacts[0] = replace(artifacts[0], row_count=2)
        manifest = replace(manifest, source_artifacts=tuple(artifacts))
    elif mutation == "date_range":
        artifacts[0] = replace(artifacts[0], date_range=("bad", "bad"))
        manifest = replace(manifest, source_artifacts=tuple(artifacts))
    else:
        artifacts[0] = replace(artifacts[0], identity_status="verified")
        manifest = replace(manifest, source_artifacts=tuple(artifacts))
    with pytest.raises(ValueError):
        build_research_dataset(
            replay, backtest, context=replace(context, manifest=manifest)
        )


def test_metrics_accepts_valid_closed_and_censored_rows_with_coverage(golden_bundle):
    from quasartrend.research import calculate_metrics
    dataset = golden_bundle[3]
    closed = next(row for row in dataset.trade_rows if row.outcome_state == "closed")
    censored = replace(
        dataset.trade_rows[-1], outcome_state="censored", exit_event_id=None,
        exit_timestamp=None, exit_source_open_timestamp=None, exit_finalized_timestamp=None,
        canonical_exit_price=None, execution_exit_price=None, exit_primary_reason=None,
        exit_all_reasons=(), stop_hit=None, strategy_exit=None, gross_pnl=None,
        net_pnl=None, entry_fee=None, exit_fee=None, total_fees=None, realized_r=None,
        mae=None, mfe=None, mae_r=None, mfe_r=None, observed_duration_bars=None,
        expected_duration_bars=None, elapsed_duration_ms=None,
    )
    metrics = calculate_metrics((closed, censored), eligible_setups=2)
    assert metrics.closed_trades == 1 and metrics.mae_observation_count == 1
    assert metrics.r_per_setup == metrics.total_r / 2
    with pytest.raises(ValueError, match="censored"):
        calculate_metrics((replace(censored, net_pnl=1.0),), eligible_setups=1)
    with pytest.raises(ValueError, match="exit event identity"):
        calculate_metrics((replace(closed, exit_event_id=None),), eligible_setups=1)


def test_golden_all_trade_setup_links_round_trip_and_are_unique(golden_bundle):
    dataset = golden_bundle[3]
    setups = {row.setup_id: row for row in dataset.setup_rows}
    assert len(setups) == len(dataset.setup_rows)
    assert all(row.setup_id in setups for row in dataset.trade_rows)
    assert all(setups[row.setup_id].linked_trade_id == row.trade_id for row in dataset.trade_rows)


def test_partition_trades_is_exhaustive_for_included_purged_censored_outside(golden_bundle):
    from quasartrend.research import ChronologicalWindow, partition_trades
    dataset = golden_bundle[3]
    row = next(item for item in dataset.trade_rows if item.outcome_state == "closed")
    from datetime import UTC, datetime
    date = datetime.fromtimestamp(row.decision_timestamp / 1000, UTC).date().isoformat()
    window = ChronologicalWindow("window", date, date)
    included = replace(row, exit_timestamp=row.decision_timestamp, exit_source_open_timestamp=row.decision_timestamp - 900_000, exit_finalized_timestamp=row.decision_timestamp)
    purged = replace(included, trade_id="purged", exit_timestamp=window.end_ms + 900_000, exit_source_open_timestamp=window.end_ms, exit_finalized_timestamp=window.end_ms + 900_000)
    censored = replace(included, trade_id="censored", outcome_state="censored", exit_timestamp=None, exit_source_open_timestamp=None, exit_finalized_timestamp=None, canonical_exit_price=None, execution_exit_price=None, exit_primary_reason=None, exit_all_reasons=(), stop_hit=None, strategy_exit=None, gross_pnl=None, net_pnl=None, entry_fee=None, exit_fee=None, total_fees=None, realized_r=None, mae=None, mfe=None, mae_r=None, mfe_r=None, observed_duration_bars=None, expected_duration_bars=None, elapsed_duration_ms=None)
    outside = replace(included, trade_id="outside", decision_timestamp=window.end_ms + 1, exit_timestamp=window.end_ms + 1, exit_source_open_timestamp=window.end_ms - 899_999, exit_finalized_timestamp=window.end_ms + 1, source_processing_key=(window.end_ms + 1, 1))
    ordered = tuple(sorted((included, purged, censored, outside), key=lambda item: (item.decision_timestamp, item.source_processing_key, item.trade_id)))
    result = partition_trades(ordered, (window,))["window"]
    assert {item.trade_id for item in result.included} == {row.trade_id}
    assert purged in result.purged_boundary_crossing and censored in result.censored and outside in result.outside_window


@pytest.mark.parametrize("event_type", (EventType.TRADE_OPENED, EventType.TRADE_CLOSED))
def test_build_rejects_duplicate_open_and_close_events(golden_bundle, event_type):
    replay, backtest, context, _, _ = golden_bundle
    trace_index = next(
        index
        for index, trace in enumerate(replay.traces)
        if any(event.type is event_type for event in trace.events)
    )
    trace = replay.traces[trace_index]
    event = next(event for event in trace.events if event.type is event_type)
    forged_trace = replace(trace, events=(*trace.events, event))
    forged_replay = replace(
        replay,
        traces=(
            *replay.traces[:trace_index], forged_trace,
            *replay.traces[trace_index + 1:],
        ),
    )
    with pytest.raises(ValueError):
        build_research_dataset(forged_replay, backtest, context=context)


def test_gapped_build_clears_excursions_and_records_duration_gap(golden_bundle):
    _, _, original_context, _, streams = golden_bundle
    removed_open_time = 1_777_639_500_000
    ltf = tuple(
        bar for bar in streams[Timeframe.MINUTES_15]
        if bar.open_time != removed_open_time
    )
    htf = streams[Timeframe.HOURS_4]
    replay = ReplayEngine(
        original_context.replay_config, original_context.strategy_config,
    ).run(sorted((*ltf, *htf), key=lambda bar: bar.processing_key))
    backtest = BacktestEngine(original_context.backtest_config).run(replay)
    raw15 = _raw_bars(ltf)
    raw4 = _raw_bars(htf)
    manifest = make_manifest(
        source_artifacts=(
            make_source_artifact(
                declared_symbol="BTCUSDT", timeframe="15m", raw_input=raw15,
            ),
            make_source_artifact(
                declared_symbol="BTCUSDT", timeframe="4h", raw_input=raw4,
            ),
        ),
        phase6_sha=PHASE6_SHA,
        source_description="declared gapped golden source",
        source_reference="generated from tests/golden",
        strategy_config=original_context.strategy_config,
        replay_config=original_context.replay_config,
        backtest_config=original_context.backtest_config,
        research_config=original_context.research_config,
        split_config=original_context.split_config,
    )
    context = replace(
        original_context,
        manifest=manifest,
        raw_inputs=(("15m", raw15), ("4h", raw4)),
    )
    dataset = build_research_dataset(replay, backtest, context=context)
    row = next(item for item in dataset.trade_rows if item.trade_id == "BTCUSDT:1")
    assert row.realized_r is not None
    assert row.observed_duration_bars < row.expected_duration_bars
    assert "post_entry_15m_gap" in row.data_quality_flags
    assert (row.mae, row.mfe, row.mae_r, row.mfe_r) == (None, None, None, None)

    from quasartrend.research import calculate_metrics
    metrics = calculate_metrics((row,), eligible_setups=1)
    assert metrics.mae_observation_count == 0
    assert metrics.mfe_observation_count == 0
