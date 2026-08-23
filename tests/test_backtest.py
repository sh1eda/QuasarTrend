from __future__ import annotations

from dataclasses import replace

import pytest

from quasartrend.backtest import BacktestConfig, BacktestEngine
from quasartrend.replay import HistoricalBar, ReplayConfig, ReplayEngine, ReplayResult, ReplayTrace, Timeframe
from quasartrend.strategy import Direction, EventType, OpenTrade, ReasonCode, StrategyEvent, StrategyState, StrategyStatus


def source(timestamp: int) -> HistoricalBar:
    return HistoricalBar("BTC", Timeframe.MINUTES_15, timestamp - 900_000, 100.0, 101.0, 99.0, 100.0)


def replay(open_price: float, close_price: float, side: Direction = Direction.LONG) -> ReplayResult:
    trade = OpenTrade("BTC:1", side, open_price, 900_000, 1.0, open_price - 1 if side is Direction.LONG else open_price + 1, 3, 800_000)
    opened_state = StrategyState(symbol="BTC", status=StrategyStatus.OPEN_LONG if side is Direction.LONG else StrategyStatus.OPEN_SHORT, trade=trade, bias_epoch=3)
    flat = StrategyState(symbol="BTC", status=StrategyStatus.FLAT, bias_epoch=3)
    open_event = StrategyEvent(EventType.TRADE_OPENED, "BTC", 900_000, ReasonCode.ENTRY_ACCEPTED, "BTC:1", side, open_price)
    close_event = StrategyEvent(EventType.TRADE_CLOSED, "BTC", 1_800_000, ReasonCode.EXIT_STOP, "BTC:1", side, close_price)
    traces = (
        ReplayTrace(source(900_000), None, (open_event,), opened_state, side),
        ReplayTrace(source(1_800_000), None, (close_event,), flat, side),
    )
    # The real replay state is not material to event-only accounting; use the
    # state carried by its final trace for this synthetic accounting fixture.
    from quasartrend.replay import ReplayEngine
    state = ReplayEngine().initial_state("BTC")
    return ReplayResult(replace(state, strategy_state=flat), traces)


def replay_many(rows: tuple[tuple[float, float, Direction], ...]) -> ReplayResult:
    traces: list[ReplayTrace] = []
    flat = StrategyState(symbol="BTC", status=StrategyStatus.FLAT)
    for index, (entry, exit_, side) in enumerate(rows, start=1):
        trade_id = f"BTC:{index}"
        entry_timestamp = index * 3_000_000
        exit_timestamp = entry_timestamp + 900_000
        trade = OpenTrade(
            trade_id, side, entry, entry_timestamp, 1.0,
            entry - 1.0 if side is Direction.LONG else entry + 1.0,
            index, entry_timestamp,
        )
        opened = StrategyState(
            symbol="BTC",
            status=StrategyStatus.OPEN_LONG if side is Direction.LONG else StrategyStatus.OPEN_SHORT,
            trade=trade,
            bias_epoch=index,
        )
        open_event = StrategyEvent(EventType.TRADE_OPENED, "BTC", entry_timestamp, ReasonCode.ENTRY_ACCEPTED, trade_id, side, entry)
        close_event = StrategyEvent(EventType.TRADE_CLOSED, "BTC", exit_timestamp, ReasonCode.EXIT_STOP, trade_id, side, exit_)
        traces.extend((
            ReplayTrace(source(entry_timestamp), None, (open_event,), opened, side),
            ReplayTrace(source(exit_timestamp), None, (close_event,), flat, side),
        ))
    from quasartrend.replay import ReplayEngine
    return ReplayResult(replace(ReplayEngine().initial_state("BTC"), strategy_state=flat), tuple(traces))


def test_long_short_adverse_fill_fee_and_traceability() -> None:
    long = BacktestEngine(BacktestConfig(fee_bps=10.0, slippage_bps=100.0)).run(replay(100.0, 110.0))
    trade = long.closed_trades[0]
    assert trade.execution_entry_price == 101.0
    assert trade.execution_exit_price == 108.9
    assert trade.gross_pnl == 7.900000000000006
    assert trade.total_fees == 0.2099
    assert trade.setup_origin_timestamp == 800_000 and trade.bias_epoch == 3
    short = BacktestEngine(BacktestConfig(slippage_bps=100.0)).run(replay(100.0, 90.0, Direction.SHORT)).closed_trades[0]
    assert short.execution_entry_price == 99.0 and short.execution_exit_price == 90.9
    assert short.gross_pnl == 8.099999999999994


def test_realized_equity_metrics_and_profit_factor_edge_case() -> None:
    winner = BacktestEngine().run(replay(100.0, 110.0))
    assert winner.equity_curve[0].realized_equity == 10.0
    assert winner.metrics.profit_factor is None
    assert winner.metrics.max_drawdown == 0.0
    flat = BacktestEngine().run(replay(100.0, 100.0))
    assert flat.metrics.flat_trades == 1
    assert flat.metrics.average_winner is None and flat.metrics.average_loser is None


@pytest.mark.parametrize(
    "rows, expected",
    [
        ((), (0, 0, 0, 0, None, None)),
        (((100.0, 90.0, Direction.LONG),), (1, 0, 1, 0, 0.0, 0.0)),
        (((100.0, 110.0, Direction.LONG),), (1, 1, 0, 0, 1.0, None)),
        (((100.0, 110.0, Direction.LONG), (100.0, 90.0, Direction.LONG)), (2, 1, 1, 0, 0.5, 1.0)),
        (((100.0, 100.0, Direction.LONG),), (1, 0, 0, 1, 0.0, None)),
    ],
)
def test_metrics_edge_matrix(rows: tuple[tuple[float, float, Direction], ...], expected: tuple[object, ...]) -> None:
    result = BacktestEngine().run(replay_many(rows))
    metrics = result.metrics
    assert (
        metrics.total_closed_trades, metrics.winning_trades, metrics.losing_trades,
        metrics.flat_trades, metrics.win_rate, metrics.profit_factor,
    ) == expected
    assert metrics.max_drawdown_percentage is None


def test_gross_metrics_are_pre_fee_and_equity_drawdown_is_realized_only() -> None:
    result = BacktestEngine(BacktestConfig(fee_bps=100.0, quantity=2.0)).run(
        replay_many(((100.0, 110.0, Direction.LONG), (100.0, 90.0, Direction.LONG)))
    )
    metrics = result.metrics
    assert metrics.gross_profit == 20.0 and metrics.gross_loss == 20.0
    assert metrics.net_profit == sum(trade.gross_pnl for trade in result.closed_trades) - metrics.total_fees
    assert tuple(point.realized_equity for point in result.equity_curve) == (15.8, -8.0)
    assert metrics.max_drawdown == 23.8


def test_zero_and_nonzero_costs_for_long_short_and_canonical_stop_price() -> None:
    zero_long = BacktestEngine().run(replay(100.0, 98.0)).closed_trades[0]
    zero_short = BacktestEngine().run(replay(100.0, 102.0, Direction.SHORT)).closed_trades[0]
    assert (zero_long.execution_entry_price, zero_long.execution_exit_price, zero_long.total_fees) == (100.0, 98.0, 0.0)
    assert (zero_short.execution_entry_price, zero_short.execution_exit_price, zero_short.total_fees) == (100.0, 102.0, 0.0)
    costly = BacktestEngine(BacktestConfig(quantity=3.0, fee_bps=5.0, slippage_bps=10.0)).run(replay(100.0, 98.0)).closed_trades[0]
    assert costly.quantity == 3.0
    assert costly.canonical_exit_price == 98.0  # Phase 2 stop price is consumed unchanged.
    assert costly.execution_entry_price > costly.canonical_entry_price
    assert costly.execution_exit_price < costly.canonical_exit_price
    costly_short = BacktestEngine(BacktestConfig(quantity=3.0, fee_bps=5.0, slippage_bps=10.0)).run(
        replay(100.0, 102.0, Direction.SHORT)
    ).closed_trades[0]
    assert costly_short.execution_entry_price < costly_short.canonical_entry_price
    assert costly_short.execution_exit_price > costly_short.canonical_exit_price
    assert costly_short.total_fees > 0.0


@pytest.mark.parametrize("config", [BacktestConfig(quantity=1.0), BacktestConfig(fee_bps=0.0, slippage_bps=0.0)])
def test_backtest_repeatability(config: BacktestConfig) -> None:
    replay_result = replay_many(((100.0, 110.0, Direction.LONG), (100.0, 90.0, Direction.SHORT)))
    assert BacktestEngine(config).run(replay_result) == BacktestEngine(config).run(replay_result)


def test_malformed_event_sequence_is_rejected() -> None:
    result = replay(100.0, 110.0)
    from dataclasses import replace
    orphan = replace(result.traces[1], events=(result.traces[1].events[0],))
    try:
        BacktestEngine().run(ReplayResult(result.state, (orphan,)))
    except ValueError as error:
        assert "without an open" in str(error)
    else:
        raise AssertionError("orphan close must fail")


def test_backtest_rejects_out_of_order_traces_and_source_event_mismatch() -> None:
    result = replay_many(((100.0, 110.0, Direction.LONG), (100.0, 90.0, Direction.LONG)))
    with pytest.raises(ValueError, match="strict source processing order"):
        BacktestEngine().run(ReplayResult(result.state, tuple(reversed(result.traces))))
    mismatched_event = replace(
        result.traces[0].events[0], timestamp=result.traces[0].source_bar.finalized_at + 1
    )
    with pytest.raises(ValueError, match="timestamp"):
        BacktestEngine().run(ReplayResult(result.state, (replace(result.traces[0], events=(mismatched_event,)),)))


def test_backtest_rejects_event_and_post_state_symbol_inconsistency() -> None:
    result = replay(100.0, 110.0)
    wrong_event = replace(result.traces[0].events[0], symbol="ETH")
    with pytest.raises(ValueError, match="symbol"):
        BacktestEngine().run(ReplayResult(result.state, (replace(result.traces[0], events=(wrong_event,)),)))
    wrong_state = StrategyState(symbol="ETH", status=StrategyStatus.FLAT)
    with pytest.raises(ValueError, match="post-state symbol"):
        BacktestEngine().run(ReplayResult(result.state, (replace(result.traces[0], post_state=wrong_state),)))


def test_replay_to_backtest_integration_preserves_canonical_phase2_prices() -> None:
    config = ReplayConfig(
        ltf_hema_fast_length=1, ltf_hema_slow_length=2,
        htf_hema_fast_length=1, htf_hema_slow_length=2,
        kalman_period=2, kalman_alpha=0.05, kalman_beta=0.2, kalman_atr_period=2,
    )
    def market(timeframe: Timeframe, open_time: int, close: float) -> HistoricalBar:
        return HistoricalBar("BTC", timeframe, open_time, close, close + 1.0, close - 1.0, close)
    replay_result = ReplayEngine(config).run((
        market(Timeframe.MINUTES_15, 12_600_000, 100.0),
        market(Timeframe.HOURS_4, 0, 100.0),
        market(Timeframe.MINUTES_15, 27_000_000, 99.0),
        market(Timeframe.HOURS_4, 14_400_000, 110.0),
        market(Timeframe.MINUTES_15, 27_900_000, 100.0),
        market(Timeframe.MINUTES_15, 28_800_000, 101.0),
    ))
    opening_trace = replay_result.traces[4]
    closing_trace = replay_result.traces[5]
    opened = next(event for event in opening_trace.events if event.type is EventType.TRADE_OPENED)
    closed = next(event for event in closing_trace.events if event.type is EventType.TRADE_CLOSED)
    result = BacktestEngine().run(replay_result)
    trade = result.closed_trades[0]
    assert (trade.trade_id, trade.setup_origin_timestamp, trade.bias_epoch) == (
        opened.trade_id, opening_trace.post_state.trade.setup_origin_timestamp, opening_trace.post_state.trade.bias_epoch,
    )
    assert (trade.canonical_entry_price, trade.canonical_exit_price) == (opened.price, closed.price)
    assert result.metrics.net_profit == trade.net_pnl
