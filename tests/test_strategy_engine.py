from __future__ import annotations

from dataclasses import replace

import pytest

from quasartrend.strategy import (
    BiasReversalBehavior,
    ConfirmationMode,
    Direction,
    EventType,
    OpenTrade,
    OutOfOrderTimestampError,
    ReasonCode,
    StrategyBar,
    StrategyConfig,
    StrategyEngine,
    StrategyState,
    StrategyStatus,
)


LONG = Direction.LONG
SHORT = Direction.SHORT


def bar(timestamp: int, **changes: object) -> StrategyBar:
    values: dict[str, object] = {
        "symbol": "BTC/USDT",
        "timestamp": timestamp,
        "open": 100.0,
        "high": 101.0,
        "low": 99.0,
        "close": 100.0,
        "htf_bias": LONG,
        "hema_direction": LONG,
        "kalman_direction": LONG,
        "atr": 2.0,
        "strategy_ready": True,
        "hema_flip": None,
        "kalman_transition": None,
    }
    values.update(changes)
    return StrategyBar(**values)  # type: ignore[arg-type]


def run(engine: StrategyEngine, *bars: StrategyBar):
    state = StrategyState.initial(bars[0].symbol)
    results = []
    for item in bars:
        result = engine.step(state, item)
        results.append(result)
        state = result.state
    return state, results


def events(result, type: EventType):
    return [event for event in result.events if event.type is type]


@pytest.mark.parametrize(
    ("bias", "side"),
    [(LONG, LONG), (SHORT, SHORT)],
    ids=["long", "short"],
)
def test_matching_bias_fresh_hema_and_kalman_enters(bias: Direction, side: Direction) -> None:
    state, results = run(
        StrategyEngine(),
        bar(1, htf_bias=bias, hema_direction=None, kalman_direction=side, atr=2.0),
        bar(2, htf_bias=bias, hema_direction=side, kalman_direction=side, hema_flip=side),
    )
    assert state.trade is not None and state.trade.side is side
    assert state.trade.entry_price == 100.0
    assert state.trade.stop_price == (98.0 if side is LONG else 102.0)
    assert events(results[-1], EventType.TRADE_OPENED)[0].reason is ReasonCode.ENTRY_ACCEPTED


@pytest.mark.parametrize("bias, flip", [(LONG, SHORT), (SHORT, LONG)])
def test_wrong_direction_hema_flip_does_not_trade(bias: Direction, flip: Direction) -> None:
    state, results = run(
        StrategyEngine(),
        bar(1, htf_bias=bias, hema_direction=None, kalman_direction=flip),
        bar(2, htf_bias=bias, hema_direction=flip, kalman_direction=flip, hema_flip=flip),
    )
    assert state.trade is None
    assert events(results[-1], EventType.DECISION_REJECTED)[0].reason is ReasonCode.HEMA_FLIP_WRONG_DIRECTION


def test_no_bias_or_pre_epoch_alignment_never_enters_until_fresh_flip() -> None:
    state, results = run(
        StrategyEngine(),
        bar(1, htf_bias=None, hema_direction=None, kalman_direction=LONG, atr=2.0),
        bar(2, htf_bias=None, hema_direction=LONG, kalman_direction=LONG, atr=2.0, hema_flip=LONG),
        bar(3, htf_bias=LONG, hema_direction=LONG, kalman_direction=LONG, atr=2.0),
        bar(4, htf_bias=LONG, hema_direction=SHORT, kalman_direction=LONG, atr=2.0, hema_flip=SHORT),
        bar(5, htf_bias=LONG, hema_direction=LONG, kalman_direction=LONG, atr=2.0, hema_flip=LONG),
    )
    assert not events(results[1], EventType.TRADE_OPENED)
    assert not events(results[2], EventType.TRADE_OPENED)
    assert state.trade is not None and state.trade.entry_timestamp == 5
    assert state.bias_epoch == 1


def test_kalman_before_hema_enters_and_hema_before_kalman_arms_then_enters() -> None:
    engine = StrategyEngine()
    first, _ = run(
        engine,
        bar(1, hema_direction=None, kalman_direction=LONG),
        bar(2, hema_direction=LONG, kalman_direction=LONG, hema_flip=LONG),
    )
    assert first.trade is not None and first.trade.entry_timestamp == 2

    second, results = run(
        engine,
        bar(10, hema_direction=None, kalman_direction=SHORT),
        bar(11, hema_direction=LONG, kalman_direction=SHORT, hema_flip=LONG),
        bar(12, hema_direction=LONG, kalman_direction=LONG, kalman_transition=LONG),
    )
    assert results[1].state.status is StrategyStatus.PENDING_LONG
    assert events(results[1], EventType.SETUP_ARMED)
    assert second.trade is not None and second.trade.setup_origin_timestamp == 11


def test_pending_cancels_on_opposing_hema_bias_change_stale_epoch_and_readiness_loss() -> None:
    engine = StrategyEngine()
    state, results = run(
        engine,
        bar(1, hema_direction=None, kalman_direction=SHORT),
        bar(2, hema_direction=LONG, kalman_direction=SHORT, hema_flip=LONG),
        bar(3, hema_direction=SHORT, kalman_direction=SHORT, hema_flip=SHORT),
    )
    assert state.trade is None and state.pending_direction is None
    assert events(results[-1], EventType.SETUP_CANCELLED)[0].reason is ReasonCode.PENDING_SETUP_CANCELLED_BY_HEMA

    state, results = run(
        engine,
        bar(10, hema_direction=None, kalman_direction=SHORT),
        bar(11, hema_direction=LONG, kalman_direction=SHORT, hema_flip=LONG),
        bar(12, htf_bias=SHORT, hema_direction=LONG, kalman_direction=LONG),
    )
    assert state.bias_epoch == 2 and state.trade is None and state.pending_direction is None
    assert events(results[-1], EventType.SETUP_CANCELLED)[0].reason is ReasonCode.PENDING_SETUP_CANCELLED_BY_BIAS

    state, results = run(
        engine,
        bar(20, hema_direction=None, kalman_direction=SHORT),
        bar(21, hema_direction=LONG, kalman_direction=SHORT, hema_flip=LONG),
        bar(22, hema_direction=LONG, kalman_direction=LONG, strategy_ready=False),
    )
    assert state.status is StrategyStatus.DATA_BLOCKED and state.pending_direction is None
    assert events(results[-1], EventType.SETUP_CANCELLED)[0].reason is ReasonCode.PENDING_SETUP_CANCELLED_BY_READINESS


def long_open(engine: StrategyEngine | None = None):
    engine = engine or StrategyEngine()
    return run(
        engine,
        bar(1, hema_direction=None),
        bar(2, hema_direction=LONG, hema_flip=LONG),
    )


@pytest.mark.parametrize("side, exit_flip", [(LONG, SHORT), (SHORT, LONG)])
def test_opposite_fresh_hema_flip_exits(side: Direction, exit_flip: Direction) -> None:
    engine = StrategyEngine()
    state, results = run(
        engine,
        bar(1, htf_bias=side, hema_direction=None, kalman_direction=side),
        bar(2, htf_bias=side, hema_direction=side, kalman_direction=side, hema_flip=side),
        bar(3, htf_bias=side, hema_direction=exit_flip, kalman_direction=side, hema_flip=exit_flip),
    )
    assert state.trade is None and state.status is StrategyStatus.FLAT
    assert events(results[-1], EventType.TRADE_CLOSED)[0].reason is ReasonCode.EXIT_HEMA_FLIP


def test_htf_exit_vs_hold_and_kalman_reversal_alone() -> None:
    _, results = long_open()
    state = results[-1].state
    exit_result = StrategyEngine().step(state, bar(3, htf_bias=SHORT, hema_direction=LONG, kalman_direction=LONG))
    assert exit_result.state.trade is None
    assert events(exit_result, EventType.TRADE_CLOSED)[0].reason is ReasonCode.EXIT_HTF_REVERSAL

    hold = StrategyEngine(StrategyConfig(bias_reversal_behavior=BiasReversalBehavior.HOLD_UNTIL_LTF_EXIT))
    _, hold_results = long_open(hold)
    hold_result = hold.step(hold_results[-1].state, bar(3, htf_bias=SHORT, hema_direction=LONG, kalman_direction=SHORT, kalman_transition=SHORT))
    assert hold_result.state.trade is not None
    assert not events(hold_result, EventType.TRADE_CLOSED)


def test_stop_priority_coincident_reasons_and_fixed_historical_fills() -> None:
    state, results = long_open()
    opened = results[-1].state.trade
    assert opened is not None
    result = StrategyEngine().step(
        state,
        bar(3, htf_bias=SHORT, hema_direction=SHORT, kalman_direction=LONG, hema_flip=SHORT, low=98.0),
    )
    closed = events(result, EventType.TRADE_CLOSED)[0]
    assert closed.reason is ReasonCode.EXIT_STOP
    assert closed.reasons == (ReasonCode.EXIT_STOP, ReasonCode.EXIT_HTF_REVERSAL, ReasonCode.EXIT_HEMA_FLIP)
    assert closed.price == opened.stop_price
    assert len(events(result, EventType.STOP_HIT)) == 1

    # With no stop touch, HTF reversal wins over the coincident HEMA exit.
    state, results = long_open()
    result = StrategyEngine().step(state, bar(3, htf_bias=SHORT, hema_direction=SHORT, hema_flip=SHORT, low=99.0))
    assert events(result, EventType.TRADE_CLOSED)[0].reason is ReasonCode.EXIT_HTF_REVERSAL


def test_entry_bar_does_not_stop_and_future_touch_and_gaps_do() -> None:
    engine = StrategyEngine()
    state, results = run(
        engine,
        bar(1, hema_direction=None),
        bar(2, hema_direction=LONG, hema_flip=LONG, low=1.0),
    )
    assert state.trade is not None  # entry-close means bar 2 low cannot stop it.
    touch = engine.step(state, bar(3, low=98.0))
    assert events(touch, EventType.TRADE_CLOSED)[0].price == 98.0

    state, results = long_open()
    gap = StrategyEngine().step(state, bar(3, open=97.0, high=99.0, low=96.0))
    assert events(gap, EventType.TRADE_CLOSED)[0].price == 97.0

    short_state, short_results = run(
        StrategyEngine(),
        bar(10, htf_bias=SHORT, hema_direction=None, kalman_direction=SHORT),
        bar(11, htf_bias=SHORT, hema_direction=SHORT, kalman_direction=SHORT, hema_flip=SHORT),
    )
    short_gap = StrategyEngine().step(short_state, bar(12, htf_bias=SHORT, hema_direction=SHORT, kalman_direction=SHORT, open=103.0, high=104.0))
    assert events(short_gap, EventType.TRADE_CLOSED)[0].price == 103.0


def test_multiple_trades_require_later_genuine_fresh_flips_and_no_same_bar_reverse() -> None:
    state, results = run(
        StrategyEngine(),
        bar(1, hema_direction=None),
        bar(2, hema_direction=LONG, hema_flip=LONG),
        bar(3, hema_direction=SHORT, hema_flip=SHORT),
        # Existing SHORT alignment cannot re-enter in the long epoch.
        bar(4, hema_direction=LONG, kalman_direction=LONG),
        bar(5, hema_direction=SHORT, kalman_direction=LONG, hema_flip=SHORT),
        bar(6, hema_direction=LONG, kalman_direction=LONG, hema_flip=LONG),
    )
    assert events(results[2], EventType.TRADE_CLOSED)
    assert not events(results[3], EventType.TRADE_OPENED)
    assert not events(results[4], EventType.TRADE_OPENED)
    assert state.trade is not None and state.trade.trade_id == "BTC/USDT:2"
    assert state.trade.bias_epoch == 1


def test_repeated_hema_flags_and_persistent_alignment_do_not_create_entries() -> None:
    state, results = run(
        StrategyEngine(),
        bar(1, hema_direction=LONG, kalman_direction=LONG),
        bar(2, hema_direction=LONG, kalman_direction=LONG, hema_flip=LONG),
        bar(3, hema_direction=LONG, kalman_direction=LONG, hema_flip=LONG),
    )
    assert state.trade is None
    assert not events(results[1], EventType.TRADE_OPENED)
    assert not events(results[2], EventType.HEMA_FLIP_DETECTED)


def test_duplicate_out_of_order_replay_and_symbol_isolation_are_deterministic() -> None:
    engine = StrategyEngine()
    initial = StrategyState.initial("BTC/USDT")
    first = engine.step(initial, bar(1, hema_direction=None))
    duplicate = engine.step(first.state, bar(1, hema_direction=LONG, hema_flip=LONG))
    assert duplicate.state == first.state
    assert duplicate.events[0].reason is ReasonCode.DUPLICATE_TIMESTAMP
    with pytest.raises(OutOfOrderTimestampError):
        engine.step(first.state, bar(0))

    sequence = (bar(1, hema_direction=None), bar(2, hema_direction=LONG, hema_flip=LONG))
    assert run(engine, *sequence) == run(engine, *sequence)

    a = engine.step(StrategyState.initial("A"), bar(1, symbol="A", hema_direction=None))
    b = engine.step(StrategyState.initial("B"), bar(1, symbol="B", hema_direction=None))
    a2 = engine.step(a.state, bar(2, symbol="A", hema_direction=LONG, hema_flip=LONG))
    assert a2.state.trade is not None
    assert b.state.trade is None and b.state.symbol == "B"


def test_state_invariants_and_configuration_interface() -> None:
    state, _ = long_open()
    assert state.trade is not None
    assert state.status is StrategyStatus.OPEN_LONG
    assert state.trade.stop_price < state.trade.entry_price
    with pytest.raises(ValueError, match="STATEFUL_EITHER_ORDER"):
        StrategyEngine(StrategyConfig(confirmation_mode=ConfirmationMode.SAME_CANDLE))
    with pytest.raises(ValueError):
        replace(state, status=StrategyStatus.OPEN_SHORT)


def test_same_timestamp_bias_activation_and_fresh_flip_belongs_to_new_epoch() -> None:
    state, _ = run(
        StrategyEngine(),
        bar(1, htf_bias=None, hema_direction=None, kalman_direction=LONG),
        bar(2, htf_bias=LONG, hema_direction=LONG, kalman_direction=LONG, hema_flip=LONG),
    )
    assert state.trade is not None
    assert state.bias_epoch == 1
    assert state.bias_activation_timestamp == 2


@pytest.mark.parametrize(
    "changes, expected_status",
    [
        ({"strategy_ready": False}, StrategyStatus.DATA_BLOCKED),
        ({"atr": None}, StrategyStatus.WARMING_UP),
        ({"atr": 0.0}, StrategyStatus.WARMING_UP),
        ({"kalman_direction": None}, StrategyStatus.WARMING_UP),
    ],
)
def test_entry_readiness_requires_explicit_readiness_all_directions_and_valid_atr(
    changes: dict[str, object], expected_status: StrategyStatus
) -> None:
    state, results = run(
        StrategyEngine(),
        bar(1, hema_direction=None),
        bar(2, hema_direction=LONG, hema_flip=LONG, **changes),
    )
    assert state.trade is None and state.status is expected_status
    assert not events(results[-1], EventType.TRADE_OPENED)


def test_open_position_still_stops_when_current_entry_readiness_is_lost() -> None:
    state, _ = long_open()
    result = StrategyEngine().step(
        state,
        bar(3, strategy_ready=False, htf_bias=None, hema_direction=None, kalman_direction=None, atr=None, low=98.0),
    )
    assert result.state.trade is None
    assert events(result, EventType.TRADE_CLOSED)[0].reason is ReasonCode.EXIT_STOP
    assert result.state.status is StrategyStatus.DATA_BLOCKED


def test_no_stop_touch_keeps_open_position_and_kalman_never_exits() -> None:
    state, _ = long_open()
    result = StrategyEngine().step(
        state,
        bar(3, low=98.01, kalman_direction=SHORT, kalman_transition=SHORT),
    )
    assert result.state.trade is not None
    assert not events(result, EventType.TRADE_CLOSED)


def test_stopped_trade_needs_a_later_fresh_hema_flip_to_reenter() -> None:
    engine = StrategyEngine()
    state, _ = long_open(engine)
    stopped = engine.step(state, bar(3, low=98.0))
    unchanged = engine.step(stopped.state, bar(4, hema_direction=LONG, kalman_direction=LONG))
    reentered = engine.step(
        unchanged.state,
        bar(5, hema_direction=SHORT, kalman_direction=LONG, hema_flip=SHORT),
    )
    reentered = engine.step(
        reentered.state,
        bar(6, hema_direction=LONG, kalman_direction=LONG, hema_flip=LONG),
    )
    assert not events(unchanged, EventType.TRADE_OPENED)
    assert reentered.state.trade is not None and reentered.state.trade.trade_id == "BTC/USDT:2"


def test_close_bar_with_opposite_confirmation_emits_no_open_event() -> None:
    state, _ = long_open()
    result = StrategyEngine().step(
        state,
        bar(3, htf_bias=SHORT, hema_direction=SHORT, kalman_direction=SHORT, hema_flip=SHORT),
    )
    assert len(events(result, EventType.TRADE_CLOSED)) == 1
    assert not events(result, EventType.TRADE_OPENED)
    assert events(result, EventType.DECISION_REJECTED)[0].reason is ReasonCode.NO_SAME_BAR_REVERSAL


def test_state_symbol_must_match_bar_symbol() -> None:
    with pytest.raises(ValueError, match="bar.symbol"):
        StrategyEngine().step(StrategyState.initial("A"), bar(1, symbol="B"))


def test_pending_has_active_epoch_and_trade_creation_clears_pending() -> None:
    engine = StrategyEngine()
    state, _ = run(
        engine,
        bar(1, hema_direction=None, kalman_direction=SHORT),
        bar(2, hema_direction=LONG, kalman_direction=SHORT, hema_flip=LONG),
    )
    assert state.pending_direction is LONG and state.pending_bias_epoch == state.bias_epoch
    result = engine.step(state, bar(3, hema_direction=LONG, kalman_direction=LONG, kalman_transition=LONG))
    assert result.state.trade is not None and result.state.pending_direction is None


def test_invalid_atr_has_stable_diagnostic_on_actual_fresh_flip() -> None:
    _, results = run(
        StrategyEngine(),
        bar(1, hema_direction=None),
        bar(2, hema_direction=LONG, hema_flip=LONG, atr=float("nan")),
    )
    assert events(results[-1], EventType.DECISION_REJECTED)[0].reason is ReasonCode.INVALID_ATR


def test_no_htf_bias_fresh_flip_is_rejected_without_a_trade() -> None:
    state, results = run(
        StrategyEngine(),
        bar(1, htf_bias=None, hema_direction=None),
        bar(2, htf_bias=None, hema_direction=LONG, hema_flip=LONG),
    )
    assert state.trade is None
    assert events(results[-1], EventType.DECISION_REJECTED)[0].reason is ReasonCode.NO_HTF_BIAS


def test_valid_explicit_flip_when_prior_hema_is_unknown_is_accepted() -> None:
    state, _ = run(
        StrategyEngine(),
        bar(1, hema_direction=None, kalman_direction=LONG),
        bar(2, hema_direction=LONG, kalman_direction=LONG, hema_flip=LONG),
    )
    assert state.trade is not None


def test_repeated_true_hema_flag_cannot_manufacture_another_fresh_flip() -> None:
    state, _ = long_open()
    result = StrategyEngine().step(state, bar(3, hema_direction=LONG, kalman_direction=LONG, hema_flip=LONG))
    assert result.state.trade is not None
    assert not events(result, EventType.HEMA_FLIP_DETECTED)


def test_pending_has_no_timeout() -> None:
    engine = StrategyEngine()
    state, _ = run(
        engine,
        bar(1, hema_direction=None, kalman_direction=SHORT),
        bar(2, hema_direction=LONG, kalman_direction=SHORT, hema_flip=LONG),
    )
    delayed = engine.step(state, bar(100, hema_direction=LONG, kalman_direction=SHORT))
    assert delayed.state.status is StrategyStatus.PENDING_LONG
    confirmed = engine.step(delayed.state, bar(101, hema_direction=LONG, kalman_direction=LONG))
    assert confirmed.state.trade is not None


def test_long_stop_equality_fills_at_fixed_stop() -> None:
    state, _ = long_open()
    result = StrategyEngine().step(state, bar(3, low=98.0))
    assert events(result, EventType.TRADE_CLOSED)[0].price == 98.0


def test_short_stop_equality_fills_at_fixed_stop() -> None:
    engine = StrategyEngine()
    state, _ = run(
        engine,
        bar(1, htf_bias=SHORT, hema_direction=None, kalman_direction=SHORT),
        bar(2, htf_bias=SHORT, hema_direction=SHORT, kalman_direction=SHORT, hema_flip=SHORT),
    )
    result = engine.step(state, bar(3, htf_bias=SHORT, hema_direction=SHORT, kalman_direction=SHORT, high=102.0))
    assert events(result, EventType.TRADE_CLOSED)[0].price == 102.0


def test_pending_cancels_when_bias_becomes_unavailable() -> None:
    state, results = run(
        StrategyEngine(),
        bar(1, hema_direction=None, kalman_direction=SHORT),
        bar(2, hema_direction=LONG, kalman_direction=SHORT, hema_flip=LONG),
        bar(3, htf_bias=None, hema_direction=LONG, kalman_direction=LONG),
    )
    assert state.pending_direction is None
    assert events(results[-1], EventType.SETUP_CANCELLED)[0].reason is ReasonCode.REQUIRED_DATA_UNAVAILABLE


def test_bias_reappearing_after_unavailable_gets_a_new_epoch() -> None:
    state, _ = run(
        StrategyEngine(),
        bar(1, htf_bias=LONG, hema_direction=None),
        bar(2, htf_bias=None, hema_direction=None),
        bar(3, htf_bias=LONG, hema_direction=None),
    )
    assert state.bias_epoch == 2 and state.bias_activation_timestamp == 3


def test_trade_ids_are_deterministic_and_monotonic_per_symbol_state() -> None:
    state, _ = long_open()
    assert state.trade is not None and state.trade.trade_id == "BTC/USDT:1"
    assert state.next_trade_sequence == 2


def test_flat_state_contains_no_open_trade() -> None:
    state, _ = run(StrategyEngine(), bar(1, htf_bias=LONG, hema_direction=LONG, kalman_direction=LONG))
    assert state.status is StrategyStatus.FLAT and state.trade is None


def test_invalid_atr_multiplier_is_rejected() -> None:
    with pytest.raises(ValueError, match="atr_multiplier"):
        StrategyConfig(atr_multiplier=0.0)


def test_strategy_bar_requires_an_orderable_integer_timestamp() -> None:
    with pytest.raises(TypeError, match="timestamp"):
        bar("not-an-integer")  # type: ignore[arg-type]


@pytest.mark.parametrize(
    "field",
    [
        "htf_bias",
        "hema_direction",
        "kalman_direction",
        "hema_flip",
        "kalman_transition",
    ],
)
def test_strategy_bar_rejects_non_direction_indicator_values(field: str) -> None:
    with pytest.raises(TypeError, match=field):
        bar(1, **{field: "long"})


def test_strategy_bar_rejects_non_bool_strategy_ready() -> None:
    with pytest.raises(TypeError, match="strategy_ready"):
        bar(1, strategy_ready=1)


def test_contradictory_hema_flip_cannot_be_constructed_or_change_pending_long() -> None:
    engine = StrategyEngine()
    pending, _ = run(
        engine,
        bar(0, hema_direction=None, kalman_direction=SHORT),
        bar(1, hema_direction=LONG, kalman_direction=SHORT, hema_flip=LONG),
    )
    assert pending.status is StrategyStatus.PENDING_LONG
    with pytest.raises(ValueError, match="hema_flip must match"):
        bar(2, hema_direction=SHORT, kalman_direction=SHORT, hema_flip=LONG)
    assert pending.pending_direction is LONG


def test_contradictory_kalman_transition_cannot_be_constructed_or_open_pending_long() -> None:
    engine = StrategyEngine()
    pending, _ = run(
        engine,
        bar(0, hema_direction=None, kalman_direction=SHORT),
        bar(1, hema_direction=LONG, kalman_direction=SHORT, hema_flip=LONG),
    )
    assert pending.status is StrategyStatus.PENDING_LONG
    with pytest.raises(ValueError, match="kalman_transition must match"):
        bar(2, hema_direction=LONG, kalman_direction=LONG, kalman_transition=SHORT)
    assert pending.trade is None and pending.pending_direction is LONG


def test_pending_flip_at_timestamp_zero_remains_trade_setup_origin() -> None:
    engine = StrategyEngine()
    pending, _ = run(
        engine,
        bar(-1, hema_direction=None, kalman_direction=SHORT),
        bar(0, hema_direction=LONG, kalman_direction=SHORT, hema_flip=LONG),
    )
    assert pending.pending_flip_timestamp == 0
    opened = engine.step(pending, bar(1, hema_direction=LONG, kalman_direction=LONG))
    assert opened.state.trade is not None
    assert opened.state.trade.setup_origin_timestamp == 0


def test_open_trade_requires_direction_side() -> None:
    with pytest.raises(TypeError, match="side"):
        OpenTrade(
            trade_id="x:1",
            side="long",  # type: ignore[arg-type]
            entry_price=100.0,
            entry_timestamp=1,
            atr_at_entry=2.0,
            stop_price=98.0,
            bias_epoch=1,
            setup_origin_timestamp=1,
        )


@pytest.mark.parametrize(
    "field",
    [
        "current_bias",
        "previous_bias",
        "current_hema",
        "previous_hema",
        "current_kalman",
        "previous_kalman",
        "pending_direction",
    ],
)
def test_state_rejects_non_direction_directional_fields(field: str) -> None:
    kwargs: dict[str, object] = {field: "long"}
    if field == "pending_direction":
        kwargs.update(
            pending_flip_timestamp=1,
            pending_bias_epoch=1,
            bias_epoch=1,
            status=StrategyStatus.PENDING_LONG,
        )
    with pytest.raises(TypeError, match=field):
        StrategyState(symbol="BTC/USDT", **kwargs)  # type: ignore[arg-type]


@pytest.mark.parametrize(
    "pending_direction, status",
    [
        (LONG, StrategyStatus.FLAT),
        (SHORT, StrategyStatus.PENDING_LONG),
        (None, StrategyStatus.PENDING_LONG),
    ],
)
def test_pending_status_must_match_pending_direction_both_ways(
    pending_direction: Direction | None, status: StrategyStatus
) -> None:
    kwargs: dict[str, object] = {}
    if pending_direction is not None:
        kwargs.update(
            pending_direction=pending_direction,
            pending_flip_timestamp=1,
            pending_bias_epoch=1,
        )
    with pytest.raises(ValueError, match="pending (setup direction|status)"):
        StrategyState(
            symbol="BTC/USDT",
            status=status,
            bias_epoch=1,
            **kwargs,
        )
