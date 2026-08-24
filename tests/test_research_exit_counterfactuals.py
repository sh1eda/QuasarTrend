from __future__ import annotations

from dataclasses import replace

import pytest

from quasartrend.replay import HistoricalBar, Timeframe
from quasartrend.research.exit_counterfactuals import (
    BAR_MS,
    CandidateSpec,
    CounterfactualTradeInput,
    STAGE2_CANDIDATE_SPECS,
    candidate_spec,
    counterfactual_result_json,
    simulate_all_counterfactuals,
    simulate_counterfactual,
)
from quasartrend.strategy import Direction


def _bar(index: int, *, open_: float = 100.0, high: float = 100.0, low: float = 100.0, close: float = 100.0) -> HistoricalBar:
    return HistoricalBar("BTC", Timeframe.MINUTES_15, index * BAR_MS, open_, high, low, close)


def _input(
    *bars: HistoricalBar, side: Direction = Direction.LONG, stop: float | None = None,
    canonical_exit_price: float = 100.0, canonical_r: float | None = None,
    canonical_exit_reason: str = "exit_hema_flip",
    fee_bps: float = 0.0, slippage_bps: float = 0.0, quantity: float = 2.0,
) -> CounterfactualTradeInput:
    assert bars
    fraction = slippage_bps / 10_000.0
    execution_entry = 100.0 * (1.0 + fraction if side is Direction.LONG else 1.0 - fraction)
    execution_exit = canonical_exit_price * (1.0 - fraction if side is Direction.LONG else 1.0 + fraction)
    sign = 1.0 if side is Direction.LONG else -1.0
    component_net = sign * (execution_exit - execution_entry) * quantity
    component_net -= (execution_entry + execution_exit) * quantity * fee_bps / 10_000.0
    resolved_canonical_r = component_net / (2.0 * quantity) if canonical_r is None else canonical_r
    return CounterfactualTradeInput(
        setup_id="setup:1", trade_id="BTC:1", entry_event_id="entry:1", symbol="BTC", side=side,
        entry_timestamp=BAR_MS, entry_source_open_timestamp=0, entry_price=100.0,
        initial_stop=(98.0 if side is Direction.LONG else 102.0) if stop is None else stop,
        risk_per_unit=2.0, quantity=quantity, canonical_exit_timestamp=bars[-1].finalized_at,
        canonical_exit_source_open_timestamp=bars[-1].open_time, canonical_exit_price=canonical_exit_price,
        canonical_exit_reason=canonical_exit_reason, canonical_realized_r=resolved_canonical_r,
        post_entry_bars=tuple(bars), fee_bps=fee_bps, slippage_bps=slippage_bps,
    )


def _run(candidate: str, input_: CounterfactualTradeInput):
    return simulate_counterfactual(input_, candidate_spec(candidate))


def test_fixed_long_short_target_prices_and_exact_threshold_equality() -> None:
    long = _run("EXIT_FIXED_1R", _input(_bar(1, high=102.0, low=99.0), _bar(2, high=101.0, low=99.0)))
    short = _run("EXIT_FIXED_1R", _input(_bar(1, high=101.0, low=98.0), _bar(2, high=101.0, low=99.0), side=Direction.SHORT))
    assert (long.target_price, long.runner_exit_price, long.combined_realized_r) == (102.0, 102.0, 1.0)
    assert (short.target_price, short.runner_exit_price, short.combined_realized_r) == (98.0, 98.0, 1.0)
    assert long.candidate_duration_bars == short.candidate_duration_bars == 1


def test_stop_precedes_target_and_adverse_gap_fill_is_preserved() -> None:
    result = _run("EXIT_FIXED_1R", _input(
        _bar(1, open_=97.0, high=103.0, low=96.0), canonical_exit_price=97.0,
        canonical_r=-1.5, canonical_exit_reason="exit_stop",
    ))
    assert result.runner_exit_reason == "stop" and result.runner_exit_price == 97.0
    assert "stop_and_favorable_target_same_bar_stop_first" in result.intrabar_ambiguity_flags
    assert "resolved_conservatively_against_candidate" in result.intrabar_ambiguity_flags


def test_canonical_finalized_exit_wins_over_same_bar_target() -> None:
    result = _run("EXIT_FIXED_1R", _input(_bar(1, high=103.0, low=99.0, close=101.0), canonical_exit_price=101.0, canonical_r=0.5))
    assert result.runner_exit_reason == "canonical_exit"
    assert result.runner_exit_price == 101.0
    assert result.candidate_duration_bars == 1
    assert "candidate_and_canonical_exit_same_bar_canonical_first" in result.intrabar_ambiguity_flags


def test_break_even_trigger_absent_then_later_hit_and_same_bar_ambiguity() -> None:
    absent = _run("EXIT_BE_AFTER_1R", _input(_bar(1, high=101.9, low=99.0), _bar(2, high=101.0, low=99.0, close=101.0), canonical_exit_price=101.0))
    assert absent.stop_transition_timestamp is None and absent.runner_exit_reason == "canonical_exit"

    later = _run("EXIT_BE_AFTER_1R", _input(_bar(1, open_=101.0, high=102.0, low=100.5, close=101.0), _bar(2, high=101.0, low=99.5), canonical_r=0.0))
    assert later.stop_transition_timestamp == 2 * BAR_MS
    assert later.runner_exit_reason == "break_even_stop" and later.runner_exit_price == 100.0

    no_conflict = _run("EXIT_BE_AFTER_1R", _input(
        _bar(1, open_=101.0, high=102.0, low=100.5, close=101.0), _bar(2, open_=100.5, high=101.0, low=100.1, close=101.0),
        canonical_exit_price=101.0,
    ))
    assert no_conflict.runner_exit_reason == "canonical_exit"
    assert "break_even_stop_and_canonical_exit_same_bar" not in no_conflict.intrabar_ambiguity_flags

    ambiguous = _run("EXIT_BE_AFTER_1R", _input(_bar(1, high=102.0, low=99.9), _bar(2, high=101.0, low=99.0)))
    assert ambiguous.runner_exit_reason == "break_even_stop" and ambiguous.runner_exit_price == 100.0
    assert "break_even_trigger_and_new_stop_same_bar" in ambiguous.intrabar_ambiguity_flags


def test_break_even_costs_are_not_claimed_as_zero_r_and_canonical_precedes_trigger() -> None:
    costly = _run("EXIT_BE_AFTER_1R", _input(_bar(1, open_=101.0, high=102.0, low=100.4, close=101.0), _bar(2, high=101.0, low=99.9), fee_bps=10.0, slippage_bps=10.0))
    assert costly.runner_exit_reason == "break_even_stop"
    assert costly.combined_realized_r < 0.0
    assert "break_even_stop_and_canonical_exit_same_bar" in costly.intrabar_ambiguity_flags
    canonical_first = _run("EXIT_BE_AFTER_1R", _input(_bar(1, high=102.0, low=99.0, close=99.0), canonical_exit_price=99.0, canonical_r=-0.5))
    # This finalized close shares a bar with the BE trigger, so it closes
    # before the close-derived stop transition can become active.
    assert canonical_first.runner_exit_reason == "canonical_exit" and canonical_first.stop_transition_timestamp is None


def test_new_be_never_uses_pre_activation_open_but_prior_be_uses_adverse_gap_fill() -> None:
    long_new = _run("EXIT_BE_AFTER_1R", _input(
        _bar(1, open_=99.0, high=102.0, low=98.5, close=101.0),
        _bar(2, high=101.0, low=99.0),
    ))
    assert long_new.runner_exit_reason == "break_even_stop" and long_new.runner_exit_price == 100.0

    short_new = _run("EXIT_BE_AFTER_1R", _input(
        _bar(1, open_=101.0, high=101.5, low=97.5, close=99.0),
        _bar(2, high=101.0, low=99.0), side=Direction.SHORT,
    ))
    assert short_new.runner_exit_reason == "break_even_stop" and short_new.runner_exit_price == 100.0

    partial_new = _run("EXIT_PARTIAL50_2R_BE_RUNNER", _input(
        _bar(1, open_=99.0, high=104.0, low=98.5, close=101.0),
        _bar(2, high=101.0, low=99.0),
    ))
    assert partial_new.partial_exit_price == 104.0
    assert partial_new.runner_exit_reason == "break_even_stop" and partial_new.runner_exit_price == 100.0

    long_prior = _run("EXIT_BE_AFTER_1R", _input(
        _bar(1, open_=101.0, high=102.0, low=100.5, close=101.0),
        _bar(2, open_=99.0, high=101.0, low=98.5, close=99.0), canonical_exit_price=99.0,
    ))
    assert long_prior.runner_exit_reason == "break_even_stop" and long_prior.runner_exit_price == 99.0

    short_prior = _run("EXIT_BE_AFTER_1R", _input(
        _bar(1, open_=99.0, high=99.5, low=98.0, close=99.0),
        _bar(2, open_=101.0, high=101.5, low=100.5, close=101.0),
        side=Direction.SHORT, canonical_exit_price=101.0,
    ))
    assert short_prior.runner_exit_reason == "break_even_stop" and short_prior.runner_exit_price == 101.0


def test_partial_tp_canonical_runner_original_stop_and_break_even_runner() -> None:
    canonical_runner = _run("EXIT_PARTIAL50_1_5R_CANONICAL", _input(_bar(1, open_=101.0, high=103.0, low=100.1, close=101.0), _bar(2, high=101.0, low=99.0, close=101.0), canonical_exit_price=101.0))
    assert canonical_runner.partial_exit_price == 103.0
    assert canonical_runner.runner_exit_reason == "canonical_exit"
    assert sum(component.quantity for component in canonical_runner.components) == 2.0

    stopped = _run("EXIT_PARTIAL50_1_5R_CANONICAL", _input(_bar(1, open_=101.0, high=103.0, low=100.1, close=101.0), _bar(2, open_=97.0, high=100.0, low=96.0), canonical_exit_price=97.0, canonical_exit_reason="exit_stop"))
    assert stopped.runner_exit_reason == "stop" and stopped.runner_exit_price == 97.0

    be = _run("EXIT_PARTIAL50_2R_BE_RUNNER", _input(_bar(1, high=104.0, low=99.9), _bar(2, high=101.0, low=99.0)))
    assert be.partial_exit_price == 104.0
    assert be.runner_exit_reason == "break_even_stop" and be.runner_exit_price == 100.0
    assert be.partial_quantity == be.runner_quantity == 1.0


def test_component_fee_quantity_and_r_reconciliation_with_nonzero_costs() -> None:
    result = _run("EXIT_PARTIAL50_2R_CANONICAL", _input(
        _bar(1, open_=101.0, high=104.0, low=100.1, close=101.0), _bar(2, high=101.0, low=99.0, close=101.0), canonical_exit_price=101.0,
        fee_bps=5.0, slippage_bps=10.0, quantity=4.0,
    ))
    assert sum(component.quantity for component in result.components) == pytest.approx(4.0)
    assert result.combined_net_pnl == pytest.approx(sum(component.net_pnl for component in result.components))
    assert result.combined_realized_r == pytest.approx(sum(component.r_contribution for component in result.components))
    assert result.delta_r == pytest.approx(result.combined_realized_r - result.canonical_realized_r)
    assert sum(component.entry_fee for component in result.components) > 0.0
    assert sum(component.exit_fee for component in result.components) > 0.0


def test_finalized_canonical_event_never_acts_intrabar_and_future_bars_are_not_used() -> None:
    # The target on bar 1 closes the candidate.  The later canonical bar and
    # its OHLC cannot change the candidate's result or diagnostic excursion.
    input_ = _input(_bar(1, high=102.0, low=99.0), _bar(2, high=150.0, low=99.0, close=99.0), canonical_exit_price=99.0)
    result = _run("EXIT_FIXED_1R", input_)
    assert result.candidate_duration_bars == 1
    assert result.runner_exit_timestamp == 2 * BAR_MS
    assert result.diagnostic_mfe_r == 1.0

    # A canonical close only applies on its designated final bar, despite the
    # same close value appearing in earlier OHLC data.
    delayed = _run("EXIT_FIXED_4R", _input(_bar(1, high=103.0, low=99.0, close=101.0), _bar(2, high=101.0, low=99.0, close=99.0), canonical_exit_price=99.0))
    assert delayed.runner_exit_timestamp == 3 * BAR_MS
    assert delayed.runner_exit_reason == "canonical_exit"


def test_identity_no_reentry_duplicate_rejection_and_stable_serialization() -> None:
    input_ = _input(_bar(1, high=102.0, low=99.0))
    result = _run("EXIT_FIXED_1R", input_)
    assert (result.setup_id, result.trade_id, result.entry_event_id) == ("setup:1", "BTC:1", "entry:1")
    independent = _run("EXIT_FIXED_1R", input_)
    assert result == independent
    assert counterfactual_result_json(result) == counterfactual_result_json(independent)
    assert [spec.candidate_id for spec in STAGE2_CANDIDATE_SPECS] == [
        "EXIT_FIXED_1R", "EXIT_FIXED_1_5R", "EXIT_FIXED_2R", "EXIT_FIXED_3R", "EXIT_FIXED_4R",
        "EXIT_PARTIAL50_1_5R_CANONICAL", "EXIT_PARTIAL50_2R_CANONICAL", "EXIT_PARTIAL50_3R_CANONICAL",
        "EXIT_BE_AFTER_1R", "EXIT_BE_AFTER_2R", "EXIT_PARTIAL50_2R_BE_RUNNER", "EXIT_PARTIAL50_3R_BE_RUNNER",
    ]
    with pytest.raises(ValueError, match="exactly one candidate result"):
        simulate_all_counterfactuals((input_, input_), (candidate_spec("EXIT_FIXED_1R"),))
    with pytest.raises(ValueError, match="duplicate candidate IDs"):
        simulate_all_counterfactuals((input_,), (candidate_spec("EXIT_FIXED_1R"), candidate_spec("EXIT_FIXED_1R")))


def test_malformed_trace_and_mutated_candidate_spec_are_rejected() -> None:
    with pytest.raises(ValueError, match="contiguous"):
        _input(_bar(2, high=102.0, low=99.0))
    input_ = _input(_bar(1, high=102.0, low=99.0))
    mutated = replace(candidate_spec("EXIT_FIXED_1R"), target_r=1.5)
    with pytest.raises(ValueError, match="immutable predeclared"):
        simulate_counterfactual(input_, mutated)
    with pytest.raises(ValueError, match="predeclared"):
        CandidateSpec("EXIT_FIXED_1_1R", "fixed_full_tp", 1.1)
    with pytest.raises(ValueError, match="frozen accounting"):
        simulate_counterfactual(replace(input_, canonical_realized_r=3.0), candidate_spec("EXIT_FIXED_1R"))
    with pytest.raises(TypeError, match="side"):
        replace(input_, side="long")
    with pytest.raises(TypeError, match="post_entry_bars"):
        replace(input_, post_entry_bars=list(input_.post_entry_bars))
    with pytest.raises(TypeError, match="data_quality_flags"):
        replace(input_, data_quality_flags=["flag"])
    with pytest.raises(TypeError, match="only strings"):
        replace(input_, data_quality_flags=("flag", 1))
    with pytest.raises(ValueError, match="epoch aligned"):
        replace(input_, post_entry_bars=(replace(input_.post_entry_bars[0], open_time=BAR_MS + 1),))
    with pytest.raises(ValueError, match="OHLC"):
        replace(input_, post_entry_bars=(_bar(1, open_=97.0, high=101.0, low=99.0),))
    with pytest.raises(TypeError, match="timestamp"):
        replace(input_, entry_timestamp=True)
    with pytest.raises(ValueError, match="supported exit"):
        replace(input_, canonical_exit_reason="exit_unknown")
    with pytest.raises(ValueError, match="strategy exit cannot"):
        replace(input_, post_entry_bars=(_bar(1, high=102.0, low=98.0),))
    with pytest.raises(ValueError, match="stop exit requires"):
        replace(input_, canonical_exit_reason="exit_stop")
    with pytest.raises(ValueError, match="stop exit price"):
        replace(
            input_, post_entry_bars=(_bar(1, high=102.0, low=98.0),),
            canonical_exit_reason="exit_stop",
        )
