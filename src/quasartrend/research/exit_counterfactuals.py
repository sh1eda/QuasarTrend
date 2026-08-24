"""Deterministic, research-only Phase 7.1 Stage 2 exit counterfactuals.

The module intentionally consumes a *frozen* canonical entry and its exact
post-entry 15m trace.  It never creates entries, runs indicators, or changes
the production StrategyEngine.  A canonical close is a finalised-bar event;
price-level stops and targets are evaluated only from the OHLC available on
the bar being processed.
"""
from __future__ import annotations

from dataclasses import asdict, dataclass
import json
import math
from typing import Iterable

from quasartrend.replay import HistoricalBar, Timeframe
from quasartrend.strategy import Direction


EXIT_COUNTERFACTUAL_CONVENTION_VERSION = "phase7.1-stage2-counterfactual-execution/v1"
BAR_MS = 15 * 60 * 1000
_TOLERANCE = 1e-12
_PREDECLARED_IDS = frozenset((
    "EXIT_FIXED_1R", "EXIT_FIXED_1_5R", "EXIT_FIXED_2R", "EXIT_FIXED_3R", "EXIT_FIXED_4R",
    "EXIT_PARTIAL50_1_5R_CANONICAL", "EXIT_PARTIAL50_2R_CANONICAL", "EXIT_PARTIAL50_3R_CANONICAL",
    "EXIT_BE_AFTER_1R", "EXIT_BE_AFTER_2R", "EXIT_PARTIAL50_2R_BE_RUNNER", "EXIT_PARTIAL50_3R_BE_RUNNER",
))


@dataclass(frozen=True, slots=True)
class CandidateSpec:
    """One predeclared, immutable Stage 2 exit experiment."""

    candidate_id: str
    family: str
    target_r: float
    partial_fraction: float = 0.0
    move_stop_to_entry: bool = False
    runner_uses_canonical_exit: bool = True
    convention_version: str = EXIT_COUNTERFACTUAL_CONVENTION_VERSION

    def __post_init__(self) -> None:
        if self.candidate_id not in _PREDECLARED_IDS:
            raise ValueError("candidate_id is not a predeclared Stage 2 candidate")
        if self.target_r <= 0.0 or not math.isfinite(self.target_r):
            raise ValueError("target_r must be positive and finite")
        if self.partial_fraction not in (0.0, 0.5):
            raise ValueError("Stage 2 permits only a 50% partial fraction")
        if self.convention_version != EXIT_COUNTERFACTUAL_CONVENTION_VERSION:
            raise ValueError("candidate convention version mismatch")


# These are deliberately a tuple, in predeclared order: no generated grid or
# development-result-dependent candidate construction is possible here.
STAGE2_CANDIDATE_SPECS: tuple[CandidateSpec, ...] = (
    CandidateSpec("EXIT_FIXED_1R", "fixed_full_tp", 1.0),
    CandidateSpec("EXIT_FIXED_1_5R", "fixed_full_tp", 1.5),
    CandidateSpec("EXIT_FIXED_2R", "fixed_full_tp", 2.0),
    CandidateSpec("EXIT_FIXED_3R", "fixed_full_tp", 3.0),
    CandidateSpec("EXIT_FIXED_4R", "fixed_full_tp", 4.0),
    CandidateSpec("EXIT_PARTIAL50_1_5R_CANONICAL", "partial_tp_canonical_runner", 1.5, 0.5),
    CandidateSpec("EXIT_PARTIAL50_2R_CANONICAL", "partial_tp_canonical_runner", 2.0, 0.5),
    CandidateSpec("EXIT_PARTIAL50_3R_CANONICAL", "partial_tp_canonical_runner", 3.0, 0.5),
    CandidateSpec("EXIT_BE_AFTER_1R", "break_even", 1.0, 0.0, True),
    CandidateSpec("EXIT_BE_AFTER_2R", "break_even", 2.0, 0.0, True),
    CandidateSpec("EXIT_PARTIAL50_2R_BE_RUNNER", "partial_tp_break_even_runner", 2.0, 0.5, True),
    CandidateSpec("EXIT_PARTIAL50_3R_BE_RUNNER", "partial_tp_break_even_runner", 3.0, 0.5, True),
)


def candidate_spec(candidate_id: str) -> CandidateSpec:
    """Return a predeclared candidate by stable ID."""
    for spec in STAGE2_CANDIDATE_SPECS:
        if spec.candidate_id == candidate_id:
            return spec
    raise ValueError(f"unknown predeclared Stage 2 candidate: {candidate_id}")


@dataclass(frozen=True, slots=True)
class CounterfactualTradeInput:
    """Canonical identity, accounting basis, and only the legal bar trace."""

    setup_id: str
    trade_id: str
    entry_event_id: str
    symbol: str
    side: Direction
    entry_timestamp: int
    entry_source_open_timestamp: int
    entry_price: float
    initial_stop: float
    risk_per_unit: float
    quantity: float
    canonical_exit_timestamp: int
    canonical_exit_source_open_timestamp: int
    canonical_exit_price: float
    canonical_exit_reason: str
    canonical_realized_r: float
    post_entry_bars: tuple[HistoricalBar, ...]
    fee_bps: float = 0.0
    slippage_bps: float = 0.0
    data_quality_flags: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        if not all(isinstance(value, str) and value for value in (self.setup_id, self.trade_id, self.entry_event_id, self.symbol, self.canonical_exit_reason)):
            raise ValueError("canonical identity fields must be non-empty strings")
        if not isinstance(self.side, Direction):
            raise TypeError("side must be a Direction")
        if not isinstance(self.post_entry_bars, tuple):
            raise TypeError("post_entry_bars must be an immutable tuple")
        if not isinstance(self.data_quality_flags, tuple):
            raise TypeError("data_quality_flags must be an immutable tuple")
        for name in (
            "entry_timestamp", "entry_source_open_timestamp", "canonical_exit_timestamp",
            "canonical_exit_source_open_timestamp",
        ):
            value = getattr(self, name)
            if isinstance(value, bool) or not isinstance(value, int):
                raise TypeError(f"{name} must be a non-bool integer timestamp")
        if self.entry_timestamp != self.entry_source_open_timestamp + BAR_MS:
            raise ValueError("entry timestamp must finalize its entry source bar")
        if self.canonical_exit_timestamp < self.entry_timestamp:
            raise ValueError("canonical exit cannot precede entry")
        for name in ("entry_price", "initial_stop", "risk_per_unit", "quantity", "canonical_exit_price", "canonical_realized_r", "fee_bps", "slippage_bps"):
            value = getattr(self, name)
            if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value):
                raise ValueError(f"{name} must be finite")
        if self.risk_per_unit <= 0.0 or self.quantity <= 0.0 or self.fee_bps < 0.0 or self.slippage_bps < 0.0:
            raise ValueError("risk, quantity, and transaction assumptions must be positive/non-negative")
        if self.side is Direction.LONG and not self.initial_stop < self.entry_price:
            raise ValueError("a LONG initial stop must be below entry")
        if self.side is Direction.SHORT and not self.initial_stop > self.entry_price:
            raise ValueError("a SHORT initial stop must be above entry")
        if not math.isclose(abs(self.entry_price - self.initial_stop), self.risk_per_unit, abs_tol=_TOLERANCE, rel_tol=_TOLERANCE):
            raise ValueError("risk_per_unit must match the frozen initial stop distance")
        if not self.post_entry_bars:
            raise ValueError("post_entry_bars must contain the canonical exit bar")
        previous = self.entry_source_open_timestamp
        for index, bar in enumerate(self.post_entry_bars):
            if not isinstance(bar, HistoricalBar) or bar.timeframe is not Timeframe.MINUTES_15:
                raise TypeError("post_entry_bars must be canonical 15m HistoricalBar values")
            if bar.symbol != self.symbol:
                raise ValueError("post-entry bar symbol must match canonical trade")
            if bar.open_time % BAR_MS != 0:
                raise ValueError("post-entry 15m bar open must be epoch aligned")
            if not (bar.low <= bar.open <= bar.high and bar.low <= bar.close <= bar.high):
                raise ValueError("post-entry bar OHLC must lie within its high/low envelope")
            if bar.open_time != previous + BAR_MS:
                raise ValueError("post-entry bars must be contiguous and begin after entry")
            if index < len(self.post_entry_bars) - 1 and _stop_touch_and_fill(bar, self.side, self.initial_stop)[0]:
                raise ValueError("canonical trace touches its initial stop before canonical exit bar")
            previous = bar.open_time
        final = self.post_entry_bars[-1]
        if final.open_time != self.canonical_exit_source_open_timestamp:
            raise ValueError("post-entry sequence must terminate on canonical exit bar")
        if final.finalized_at != self.canonical_exit_timestamp:
            raise ValueError("canonical exit timestamp must finalize its exit source bar")
        if len(self.data_quality_flags) != len(set(self.data_quality_flags)):
            raise ValueError("data_quality_flags must be unique")
        if any(not isinstance(flag, str) for flag in self.data_quality_flags):
            raise TypeError("data_quality_flags must contain only strings")
        if self.canonical_exit_reason not in {"exit_stop", "exit_hema_flip", "exit_htf_reversal"}:
            raise ValueError("canonical exit reason is not a frozen supported exit")
        final_stop_hit, final_stop_fill = _stop_touch_and_fill(final, self.side, self.initial_stop)
        if self.canonical_exit_reason == "exit_stop":
            if not final_stop_hit:
                raise ValueError("canonical stop exit requires final bar initial-stop touch")
            if self.canonical_exit_price != final_stop_fill:
                raise ValueError("canonical stop exit price must equal frozen adverse stop fill")
        else:
            if final_stop_hit:
                raise ValueError("canonical strategy exit cannot coexist with initial-stop touch")
            if self.canonical_exit_price != final.close:
                raise ValueError("canonical strategy exit price must equal final bar close")


@dataclass(frozen=True, slots=True)
class CounterfactualEvent:
    event_type: str
    timestamp: int
    source_open_timestamp: int
    canonical_price: float | None = None
    quantity: float | None = None
    reason: str | None = None


@dataclass(frozen=True, slots=True)
class ExitComponent:
    component: str
    quantity: float
    canonical_exit_price: float
    execution_exit_price: float
    gross_pnl: float
    entry_fee: float
    exit_fee: float
    net_pnl: float
    r_contribution: float
    exit_timestamp: int
    exit_source_open_timestamp: int
    exit_reason: str


@dataclass(frozen=True, slots=True)
class CounterfactualTradeResult:
    convention_version: str
    candidate_id: str
    setup_id: str
    trade_id: str
    entry_event_id: str
    side: str
    entry_timestamp: int
    entry_price: float
    initial_stop: float
    risk_per_unit: float
    original_quantity: float
    target_price: float
    exit_events: tuple[CounterfactualEvent, ...]
    components: tuple[ExitComponent, ...]
    partial_exit_timestamp: int | None
    partial_exit_price: float | None
    partial_quantity: float | None
    partial_realized_r_contribution: float | None
    stop_transition_timestamp: int | None
    old_stop: float | None
    new_stop: float | None
    runner_exit_timestamp: int | None
    runner_exit_reason: str | None
    runner_exit_price: float | None
    runner_quantity: float | None
    runner_r_contribution: float | None
    combined_net_pnl: float
    combined_realized_r: float
    canonical_realized_r: float
    delta_r: float
    candidate_duration_bars: int
    canonical_duration_bars: int
    diagnostic_mae_r: float
    diagnostic_mfe_r: float
    diagnostic_convention: str
    intrabar_ambiguity_flags: tuple[str, ...]
    data_quality_flags: tuple[str, ...]


def _execution_entry_price(price: float, side: Direction, slippage_bps: float) -> float:
    fraction = slippage_bps / 10_000.0
    return price * (1.0 + fraction if side is Direction.LONG else 1.0 - fraction)


def _execution_exit_price(price: float, side: Direction, slippage_bps: float) -> float:
    fraction = slippage_bps / 10_000.0
    return price * (1.0 - fraction if side is Direction.LONG else 1.0 + fraction)


def _stop_touch_and_fill(bar: HistoricalBar, side: Direction, stop: float) -> tuple[bool, float | None]:
    if side is Direction.LONG and bar.low <= stop:
        return True, bar.open if bar.open < stop else stop
    if side is Direction.SHORT and bar.high >= stop:
        return True, bar.open if bar.open > stop else stop
    return False, None


def _favorable_reached(bar: HistoricalBar, side: Direction, target: float) -> bool:
    return bar.high >= target if side is Direction.LONG else bar.low <= target


def _target_price(entry: float, side: Direction, risk: float, target_r: float) -> float:
    return entry + target_r * risk if side is Direction.LONG else entry - target_r * risk


def _component(
    *, name: str, quantity: float, input_: CounterfactualTradeInput,
    canonical_exit_price: float, exit_timestamp: int, exit_source_open_timestamp: int,
    exit_reason: str,
) -> ExitComponent:
    execution_entry = _execution_entry_price(input_.entry_price, input_.side, input_.slippage_bps)
    execution_exit = _execution_exit_price(canonical_exit_price, input_.side, input_.slippage_bps)
    fraction = input_.fee_bps / 10_000.0
    entry_fee = execution_entry * quantity * fraction
    exit_fee = execution_exit * quantity * fraction
    sign = 1.0 if input_.side is Direction.LONG else -1.0
    gross = sign * (execution_exit - execution_entry) * quantity
    net = gross - entry_fee - exit_fee
    return ExitComponent(name, quantity, canonical_exit_price, execution_exit, gross, entry_fee, exit_fee, net,
                         net / (input_.risk_per_unit * input_.quantity), exit_timestamp,
                         exit_source_open_timestamp, exit_reason)


def _diagnostic_excursions(input_: CounterfactualTradeInput, bars: tuple[HistoricalBar, ...]) -> tuple[float, float]:
    adverse = max((input_.entry_price - bar.low) if input_.side is Direction.LONG else (bar.high - input_.entry_price) for bar in bars)
    favorable = max((bar.high - input_.entry_price) if input_.side is Direction.LONG else (input_.entry_price - bar.low) for bar in bars)
    return max(0.0, adverse) / input_.risk_per_unit, max(0.0, favorable) / input_.risk_per_unit


def _validate_spec(spec: CandidateSpec) -> None:
    canonical = candidate_spec(spec.candidate_id)
    if spec != canonical:
        raise ValueError("candidate specification differs from its immutable predeclared semantics")


def simulate_counterfactual(
    input_: CounterfactualTradeInput, spec: CandidateSpec,
) -> CounterfactualTradeResult:
    """Simulate exactly one candidate trade against one frozen canonical trade.

    Ordering is intentionally conservative: active adverse stop, canonical
    finalized exit (on its final bar), then favorable target/trigger.  A newly
    activated BE stop is checked on the trigger bar and therefore can close the
    remaining position at entry immediately, without assuming a favorable OHLC
    traversal.
    """
    _validate_spec(spec)
    canonical_component = _component(
        name="canonical_full_position", quantity=input_.quantity, input_=input_,
        canonical_exit_price=input_.canonical_exit_price,
        exit_timestamp=input_.canonical_exit_timestamp,
        exit_source_open_timestamp=input_.canonical_exit_source_open_timestamp,
        exit_reason=input_.canonical_exit_reason,
    )
    if not math.isclose(
        canonical_component.r_contribution, input_.canonical_realized_r,
        abs_tol=_TOLERANCE, rel_tol=_TOLERANCE,
    ):
        raise ValueError("canonical realized R does not match the frozen accounting basis")
    target = _target_price(input_.entry_price, input_.side, input_.risk_per_unit, spec.target_r)
    original_stop = input_.initial_stop
    active_stop = original_stop
    remaining = input_.quantity
    partial_done = False
    components: list[ExitComponent] = []
    events: list[CounterfactualEvent] = []
    flags: list[str] = []
    transition: tuple[int, float, float] | None = None
    used_bars: list[HistoricalBar] = []

    def flag(value: str) -> None:
        if value not in flags:
            flags.append(value)

    def close_remaining(*, bar: HistoricalBar, price: float, reason: str) -> None:
        nonlocal remaining
        if remaining <= 0.0:
            return
        components.append(_component(
            name="runner" if partial_done else "full_position", quantity=remaining, input_=input_,
            canonical_exit_price=price, exit_timestamp=bar.finalized_at,
            exit_source_open_timestamp=bar.open_time, exit_reason=reason,
        ))
        events.append(CounterfactualEvent("runner_exit" if partial_done else "full_exit", bar.finalized_at, bar.open_time, price, remaining, reason))
        remaining = 0.0

    for bar in input_.post_entry_bars:
        used_bars.append(bar)
        stop_hit, stop_fill = _stop_touch_and_fill(bar, input_.side, active_stop)
        favorable = _favorable_reached(bar, input_.side, target)
        favorable_action_available = favorable and not partial_done and not (
            spec.partial_fraction == 0.0 and spec.move_stop_to_entry and transition is not None
        )
        if stop_hit:
            if favorable_action_available:
                flag("stop_and_favorable_target_same_bar_stop_first")
                flag("resolved_conservatively_against_candidate")
            if transition is not None and active_stop == input_.entry_price:
                flag("break_even_stop_hit")
            if bar.open_time == input_.canonical_exit_source_open_timestamp:
                flag("candidate_stop_and_canonical_exit_same_bar_stop_first")
                if active_stop == input_.entry_price:
                    flag("break_even_stop_and_canonical_exit_same_bar")
            close_remaining(bar=bar, price=stop_fill if stop_fill is not None else active_stop, reason="break_even_stop" if active_stop == input_.entry_price else "stop")
            break

        # The frozen strategy event occurs only on the finalised canonical exit
        # bar and must not be promoted to an intrabar event.
        if bar.open_time == input_.canonical_exit_source_open_timestamp:
            if favorable_action_available:
                flag("candidate_and_canonical_exit_same_bar_canonical_first")
                flag("resolved_conservatively_against_candidate")
            close_remaining(bar=bar, price=input_.canonical_exit_price, reason="canonical_exit")
            break

        if not favorable_action_available:
            continue

        if spec.partial_fraction == 0.5 and not partial_done:
            partial_quantity = input_.quantity * 0.5
            components.append(_component(
                name="partial_take_profit", quantity=partial_quantity, input_=input_,
                canonical_exit_price=target, exit_timestamp=bar.finalized_at,
                exit_source_open_timestamp=bar.open_time, exit_reason="take_profit",
            ))
            events.append(CounterfactualEvent("partial_take_profit", bar.finalized_at, bar.open_time, target, partial_quantity, "take_profit"))
            partial_done = True
            remaining -= partial_quantity
            if spec.move_stop_to_entry:
                old = active_stop
                active_stop = input_.entry_price
                transition = (bar.finalized_at, old, active_stop)
                events.append(CounterfactualEvent("stop_transition", bar.finalized_at, bar.open_time, active_stop, remaining, "break_even"))
                # The newly activated BE stop is conservatively reachable if
                # the same bar has travelled back to entry.
                be_hit, _ = _stop_touch_and_fill(bar, input_.side, active_stop)
                if be_hit:
                    flag("break_even_trigger_and_new_stop_same_bar")
                    flag("resolved_conservatively_against_candidate")
                    # The BE stop did not exist at this bar's open, so the
                    # frozen adverse opening-gap fill rule cannot be applied.
                    # This is a same-bar OHLC ambiguity and closes at entry.
                    close_remaining(bar=bar, price=active_stop, reason="break_even_stop")
                    break
            continue

        if spec.partial_fraction == 0.0 and spec.move_stop_to_entry:
            old = active_stop
            active_stop = input_.entry_price
            transition = (bar.finalized_at, old, active_stop)
            events.append(CounterfactualEvent("stop_transition", bar.finalized_at, bar.open_time, active_stop, remaining, "break_even"))
            be_hit, _ = _stop_touch_and_fill(bar, input_.side, active_stop)
            if be_hit:
                flag("break_even_trigger_and_new_stop_same_bar")
                flag("resolved_conservatively_against_candidate")
                # The newly activated stop cannot fill at a pre-activation
                # opening price; conservative same-bar resolution is entry.
                close_remaining(bar=bar, price=active_stop, reason="break_even_stop")
                break
            continue

        # Fixed full-position TP is the only remaining no-partial family.
        close_remaining(bar=bar, price=target, reason="take_profit")
        break

    if remaining != 0.0:
        raise AssertionError("counterfactual trace ended without a closure")
    if not components:
        raise AssertionError("counterfactual trade has no accounting component")
    if not math.isclose(sum(component.quantity for component in components), input_.quantity, abs_tol=_TOLERANCE, rel_tol=_TOLERANCE):
        raise AssertionError("component quantity does not conserve original quantity")
    combined_net = sum(component.net_pnl for component in components)
    combined_r = combined_net / (input_.risk_per_unit * input_.quantity)
    if not math.isclose(sum(component.r_contribution for component in components), combined_r, abs_tol=_TOLERANCE, rel_tol=_TOLERANCE):
        raise AssertionError("component R contributions do not reconcile")
    partial = next((component for component in components if component.component == "partial_take_profit"), None)
    runner = next((component for component in components if component.component != "partial_take_profit"), None)
    if runner is None:
        raise AssertionError("counterfactual trade has no closing runner/full component")
    mae_r, mfe_r = _diagnostic_excursions(input_, tuple(used_bars))
    candidate_duration = len(used_bars)
    return CounterfactualTradeResult(
        EXIT_COUNTERFACTUAL_CONVENTION_VERSION, spec.candidate_id, input_.setup_id, input_.trade_id,
        input_.entry_event_id, input_.side.value, input_.entry_timestamp, input_.entry_price,
        input_.initial_stop, input_.risk_per_unit, input_.quantity, target, tuple(events), tuple(components),
        None if partial is None else partial.exit_timestamp,
        None if partial is None else partial.canonical_exit_price,
        None if partial is None else partial.quantity,
        None if partial is None else partial.r_contribution,
        None if transition is None else transition[0], None if transition is None else transition[1],
        None if transition is None else transition[2], runner.exit_timestamp, runner.exit_reason,
        runner.canonical_exit_price, runner.quantity, runner.r_contribution, combined_net, combined_r,
        input_.canonical_realized_r, combined_r - input_.canonical_realized_r, candidate_duration,
        len(input_.post_entry_bars), mae_r, mfe_r,
        "post_entry_15m_ohlc_through_candidate_exit_bar/diagnostic_only/v1", tuple(flags),
        tuple(dict.fromkeys(input_.data_quality_flags)),
    )


def simulate_all_counterfactuals(
    inputs: Iterable[CounterfactualTradeInput],
    specs: tuple[CandidateSpec, ...] = STAGE2_CANDIDATE_SPECS,
) -> tuple[CounterfactualTradeResult, ...]:
    """Stable candidate-major, canonical-input-order simulation helper."""
    frozen_inputs = tuple(inputs)
    candidate_ids = tuple(spec.candidate_id for spec in specs)
    if len(candidate_ids) != len(set(candidate_ids)):
        raise ValueError("candidate specs must not contain duplicate candidate IDs")
    if specs != tuple(sorted(specs, key=lambda spec: next(i for i, item in enumerate(STAGE2_CANDIDATE_SPECS) if item.candidate_id == spec.candidate_id))):
        raise ValueError("candidate specs must retain predeclared stable order")
    seen: set[str] = set()
    for item in frozen_inputs:
        if item.trade_id in seen:
            raise ValueError("each canonical trade may map to exactly one candidate result")
        seen.add(item.trade_id)
    return tuple(simulate_counterfactual(item, spec) for spec in specs for item in frozen_inputs)


def counterfactual_result_json(result: CounterfactualTradeResult) -> bytes:
    """Deterministic audit serialization for one synthetic or research result."""
    return (json.dumps(asdict(result), sort_keys=True, separators=(",", ":"), allow_nan=False) + "\n").encode()
