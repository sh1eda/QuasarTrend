from __future__ import annotations

import pytest

from quasartrend.replay import HistoricalBar, ReplayConfig, ReplayEngine, Timeframe
from quasartrend.strategy import Direction, EventType, StrategyStatus


def bar(timeframe: Timeframe, open_time: int, close: float, *, symbol: str = "BTC") -> HistoricalBar:
    return HistoricalBar(symbol, timeframe, open_time, close, close + 1.0, close - 1.0, close)


def test_open_time_finalization_and_equal_boundary_are_explicit() -> None:
    engine = ReplayEngine(ReplayConfig(ltf_hema_fast_length=1, ltf_hema_slow_length=2, htf_hema_fast_length=1, htf_hema_slow_length=2, kalman_period=2, kalman_atr_period=2))
    htf = bar(Timeframe.HOURS_4, 0, 100.0)
    ltf_before = bar(Timeframe.MINUTES_15, 12_600_000, 101.0)
    ltf_boundary = bar(Timeframe.MINUTES_15, 13_500_000, 102.0)
    assert ltf_before.finalized_at == 13_500_000
    assert htf.finalized_at == ltf_boundary.finalized_at == 14_400_000
    result = engine.run((ltf_before, htf, ltf_boundary))
    assert result.traces[0].strategy_bar is not None
    assert result.traces[0].strategy_bar.timestamp == 13_500_000
    assert result.traces[0].strategy_bar.htf_bias is None
    assert result.traces[1].strategy_bar is None
    assert result.traces[2].strategy_bar is not None
    assert result.traces[2].strategy_bar.timestamp == 14_400_000


def test_future_htf_contents_cannot_change_prior_ltf_trace() -> None:
    config = ReplayConfig(ltf_hema_fast_length=1, ltf_hema_slow_length=2, htf_hema_fast_length=1, htf_hema_slow_length=2, kalman_period=2, kalman_atr_period=2)
    first = bar(Timeframe.MINUTES_15, 0, 100.0)
    tail_a = (bar(Timeframe.HOURS_4, 0, 90.0), bar(Timeframe.MINUTES_15, 13_500_000, 101.0))
    tail_b = (bar(Timeframe.HOURS_4, 0, 200.0), bar(Timeframe.MINUTES_15, 13_500_000, 101.0))
    assert ReplayEngine(config).run((first, *tail_a)).traces[0] == ReplayEngine(config).run((first, *tail_b)).traces[0]


def test_prefix_resume_and_repeat_are_equality_equivalent() -> None:
    engine = ReplayEngine(ReplayConfig(ltf_hema_fast_length=1, ltf_hema_slow_length=2, htf_hema_fast_length=1, htf_hema_slow_length=2, kalman_period=2, kalman_atr_period=2))
    bars = (
        bar(Timeframe.MINUTES_15, 12_600_000, 100.0),
        bar(Timeframe.HOURS_4, 0, 100.0),
        bar(Timeframe.MINUTES_15, 13_500_000, 101.0),
        bar(Timeframe.MINUTES_15, 14_400_000, 102.0),
    )
    full = engine.run(bars)
    prefix = engine.run(bars[:2])
    suffix = engine.run(bars[2:], prefix.state)
    assert full == engine.run(bars)
    assert full.state == suffix.state
    assert full.traces == prefix.traces + suffix.traces


def test_rejects_invalid_chronology_and_symbol_and_permits_gaps() -> None:
    engine = ReplayEngine()
    htf = bar(Timeframe.HOURS_4, 0, 100.0)
    ltf = bar(Timeframe.MINUTES_15, 13_500_000, 101.0)
    with pytest.raises(ValueError, match="strict finalization order"):
        engine.run((ltf, htf))
    with pytest.raises(ValueError, match="duplicate"):
        engine.run((htf, htf))
    with pytest.raises(ValueError, match="duplicate"):
        engine.run((ltf, ltf))
    state = engine.initial_state("BTC")
    with pytest.raises(ValueError, match="match"):
        engine.step(state, bar(Timeframe.MINUTES_15, 0, 100.0, symbol="ETH"))
    result = engine.run((bar(Timeframe.MINUTES_15, 0, 100.0), bar(Timeframe.MINUTES_15, 90_000_000, 105.0)))
    assert len(result.traces) == 2


@pytest.mark.parametrize(
    "kwargs, error",
    [
        ({"open_time": True}, TypeError),
        ({"high": 99.0, "low": 100.0}, ValueError),
        ({"volume": -1.0}, ValueError),
    ],
)
def test_historical_bar_validation(kwargs: dict[str, object], error: type[Exception]) -> None:
    values: dict[str, object] = dict(symbol="BTC", timeframe=Timeframe.MINUTES_15, open_time=0, open=100.0, high=101.0, low=99.0, close=100.0)
    values.update(kwargs)
    with pytest.raises(error):
        HistoricalBar(**values)  # type: ignore[arg-type]


def _warmed_boundary_stream() -> tuple[HistoricalBar, ...]:
    """Sparse but legal bars around two 4H finalization boundaries."""
    return (
        bar(Timeframe.MINUTES_15, 12_600_000, 100.0),
        bar(Timeframe.HOURS_4, 0, 100.0),
        bar(Timeframe.MINUTES_15, 27_000_000, 99.0),
        bar(Timeframe.HOURS_4, 14_400_000, 110.0),
        bar(Timeframe.MINUTES_15, 27_900_000, 100.0),
        bar(Timeframe.MINUTES_15, 28_800_000, 101.0),
        bar(Timeframe.HOURS_4, 28_800_000, 90.0),
        bar(Timeframe.MINUTES_15, 42_300_000, 102.0),
    )


def _short_config() -> ReplayConfig:
    return ReplayConfig(
        ltf_hema_fast_length=1, ltf_hema_slow_length=2,
        htf_hema_fast_length=1, htf_hema_slow_length=2,
        kalman_period=2, kalman_alpha=0.05, kalman_beta=0.2,
        kalman_atr_period=2,
    )


def test_warmed_htf_old_bias_then_new_bias_at_exact_boundaries() -> None:
    traces = ReplayEngine(_short_config()).run(_warmed_boundary_stream()).traces
    before_first_boundary = traces[2].strategy_bar
    exact_first_boundary = traces[4].strategy_bar
    before_reversal = traces[5].strategy_bar
    exact_reversal = traces[7].strategy_bar
    assert before_first_boundary is not None and before_first_boundary.htf_bias is None
    assert exact_first_boundary is not None and exact_first_boundary.htf_bias is Direction.SHORT
    assert before_reversal is not None and before_reversal.htf_bias is Direction.SHORT
    assert exact_reversal is not None and exact_reversal.htf_bias is Direction.LONG


def test_replay_generated_bars_preserve_warmup_then_phase2_open_stop_close() -> None:
    # The LTF HEMA flip at the warmed 4H boundary opens a real Phase 2 short;
    # the following source OHLC touches that Phase 2 stop, so replay must pass
    # through its event and canonical fill rather than invent a new exit model.
    bars = _warmed_boundary_stream()[:5] + (bar(Timeframe.MINUTES_15, 28_800_000, 101.0),)
    result = ReplayEngine(_short_config()).run(bars)
    warming = result.traces[0].post_state
    opened = result.traces[4]
    closed = result.traces[5]
    assert warming.status is StrategyStatus.WARMING_UP
    assert all(
        event.type is not EventType.TRADE_OPENED
        for trace in result.traces[:4]
        for event in trace.events
    )
    assert opened.post_state.trade is not None and opened.post_state.trade.side is Direction.SHORT
    assert [event.type for event in opened.events] == [EventType.HTF_BIAS_CHANGED, EventType.HEMA_FLIP_DETECTED, EventType.TRADE_OPENED]
    close_event = next(event for event in closed.events if event.type is EventType.TRADE_CLOSED)
    assert close_event.price == opened.post_state.trade.stop_price


def test_wrong_equal_boundary_order_and_two_symbol_runs_are_isolated() -> None:
    engine = ReplayEngine(_short_config())
    htf = bar(Timeframe.HOURS_4, 0, 100.0)
    coincident_ltf = bar(Timeframe.MINUTES_15, 13_500_000, 100.0)
    with pytest.raises(ValueError, match="strict finalization order"):
        engine.run((coincident_ltf, htf))
    btc = engine.run((bar(Timeframe.MINUTES_15, 0, 100.0, symbol="BTC"),))
    eth = engine.run((bar(Timeframe.MINUTES_15, 0, 100.0, symbol="ETH"),))
    assert btc.state.symbol == "BTC" and eth.state.symbol == "ETH"
    assert btc.state.ltf_hema_checkpoint == eth.state.ltf_hema_checkpoint


@pytest.mark.parametrize(
    "kwargs, error",
    [
        ({"open": float("nan")}, ValueError),
        ({"close": float("inf")}, ValueError),
        ({"timeframe": "15m"}, TypeError),
    ],
)
def test_nonfinite_ohlc_and_unsupported_timeframe_type_rejected(
    kwargs: dict[str, object], error: type[Exception]
) -> None:
    values: dict[str, object] = dict(symbol="BTC", timeframe=Timeframe.MINUTES_15, open_time=0, open=100.0, high=101.0, low=99.0, close=100.0)
    values.update(kwargs)
    with pytest.raises(error):
        HistoricalBar(**values)  # type: ignore[arg-type]


@pytest.mark.parametrize("field", ["kalman_alpha", "kalman_beta", "kalman_factor"])
def test_replay_config_rejects_boolean_float_fields(field: str) -> None:
    with pytest.raises(ValueError, match=field):
        ReplayConfig(**{field: True})  # type: ignore[arg-type]


def test_warmed_ltf_without_legal_htf_bias_remains_warming_up() -> None:
    result = ReplayEngine(_short_config()).run((
        bar(Timeframe.MINUTES_15, 0, 100.0),
        bar(Timeframe.MINUTES_15, 900_000, 99.0),
        bar(Timeframe.MINUTES_15, 1_800_000, 100.0),
    ))
    final = result.traces[-1]
    assert final.strategy_bar is not None
    assert final.strategy_bar.hema_direction is not None
    assert final.strategy_bar.kalman_direction is not None
    assert final.strategy_bar.atr is not None
    assert final.strategy_bar.htf_bias is None
    assert final.post_state.status is StrategyStatus.WARMING_UP
    assert not any(event.type is EventType.TRADE_OPENED for event in result.events)


def test_active_position_htf_reversal_closes_only_at_shared_boundary() -> None:
    # A short is opened at the first warmed 4H boundary.  The immediately
    # preceding LTF bar retains the old short bias and the open position; at
    # the next shared boundary the newly finalized 4H long bias closes it at
    # that LTF bar's canonical close.
    result = ReplayEngine(_short_config()).run((
        bar(Timeframe.MINUTES_15, 12_600_000, 100.0),
        bar(Timeframe.HOURS_4, 0, 100.0),
        bar(Timeframe.MINUTES_15, 27_000_000, 99.0),
        bar(Timeframe.HOURS_4, 14_400_000, 110.0),
        bar(Timeframe.MINUTES_15, 27_900_000, 100.0),
        bar(Timeframe.MINUTES_15, 41_400_000, 100.0),
        bar(Timeframe.HOURS_4, 28_800_000, 90.0),
        bar(Timeframe.MINUTES_15, 42_300_000, 100.0),
    ))
    before = result.traces[5]
    boundary = result.traces[7]
    assert before.strategy_bar is not None and before.strategy_bar.htf_bias is Direction.SHORT
    assert before.post_state.trade is not None and not before.events
    assert boundary.strategy_bar is not None and boundary.strategy_bar.htf_bias is Direction.LONG
    close = next(event for event in boundary.events if event.type is EventType.TRADE_CLOSED)
    assert close.reason.value == "exit_htf_reversal"
    assert close.price == boundary.strategy_bar.close
    assert boundary.post_state.trade is None
