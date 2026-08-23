"""The dependency-free deterministic Phase 2 strategy state machine."""

from __future__ import annotations

import math

from .models import (
    BiasReversalBehavior,
    ConfirmationMode,
    Direction,
    EventType,
    OpenTrade,
    OutOfOrderTimestampError,
    ReadinessState,
    ReasonCode,
    StrategyBar,
    StrategyConfig,
    StrategyEvent,
    StrategyResult,
    StrategyState,
    StrategyStatus,
)


class StrategyEngine:
    """Pure transition function; callers own one :class:`StrategyState` per symbol."""

    def __init__(self, config: StrategyConfig | None = None) -> None:
        self.config = config or StrategyConfig()
        if self.config.confirmation_mode is not ConfirmationMode.STATEFUL_EITHER_ORDER:
            raise ValueError(
                "only STATEFUL_EITHER_ORDER is implemented in Phase 2; "
                "SAME_CANDLE and HEMA_THEN_KALMAN_EVENT are interface-only"
            )

    def step(self, state: StrategyState, bar: StrategyBar) -> StrategyResult:
        """Apply exactly one finalized bar without mutating ``state`` or ``bar``."""

        if state.symbol != bar.symbol:
            raise ValueError("bar.symbol must match state.symbol")
        if state.last_timestamp is not None:
            if bar.timestamp < state.last_timestamp:
                raise OutOfOrderTimestampError(
                    f"bar timestamp {bar.timestamp} is before {state.last_timestamp}"
                )
            if bar.timestamp == state.last_timestamp:
                return StrategyResult(
                    state=state,
                    events=(self._event(bar, EventType.DECISION_REJECTED, ReasonCode.DUPLICATE_TIMESTAMP),),
                )

        events: list[StrategyEvent] = []
        old_bias = state.current_bias
        bias_changed = bar.htf_bias is not old_bias
        epoch = state.bias_epoch
        activation_timestamp = state.bias_activation_timestamp
        if bias_changed:
            if bar.htf_bias is not None:
                epoch = 1 if epoch == 0 else epoch + 1
                activation_timestamp = bar.timestamp
            events.append(
                self._event(
                    bar,
                    EventType.HTF_BIAS_CHANGED,
                    ReasonCode.HTF_BIAS_CHANGED,
                    side=bar.htf_bias,
                    metadata=(("epoch", epoch),),
                )
            )

        fresh_hema_flip = self._is_fresh_event(
            bar.hema_flip, bar.hema_direction, state.current_hema
        )
        if fresh_hema_flip:
            events.append(
                self._event(
                    bar,
                    EventType.HEMA_FLIP_DETECTED,
                    ReasonCode.HEMA_FLIP_DETECTED,
                    side=bar.hema_flip,
                )
            )
        fresh_kalman_transition = self._is_fresh_event(
            bar.kalman_transition, bar.kalman_direction, state.current_kalman
        )
        if fresh_kalman_transition:
            events.append(
                self._event(
                    bar,
                    EventType.KALMAN_TRANSITION_DETECTED,
                    ReasonCode.KALMAN_TRANSITION_DETECTED,
                    side=bar.kalman_transition,
                )
            )

        readiness = self._readiness(bar)
        entry_ready = readiness is ReadinessState.READY
        trade = state.trade
        pending_direction = state.pending_direction
        pending_flip_timestamp = state.pending_flip_timestamp
        pending_bias_epoch = state.pending_bias_epoch
        closed_this_bar = False

        # An existing position always gets its OHLC stop evaluation, even if
        # current indicator data or explicit strategy readiness is unavailable.
        if trade is not None:
            close_reasons: list[ReasonCode] = []
            stop_hit, stop_fill = self._stop_result(trade, bar)
            if stop_hit:
                close_reasons.append(ReasonCode.EXIT_STOP)
            if (
                self.config.bias_reversal_behavior is BiasReversalBehavior.EXIT
                and bar.htf_bias is self._opposite(trade.side)
            ):
                close_reasons.append(ReasonCode.EXIT_HTF_REVERSAL)
            if fresh_hema_flip and bar.hema_flip is self._opposite(trade.side):
                close_reasons.append(ReasonCode.EXIT_HEMA_FLIP)
            if close_reasons:
                primary = close_reasons[0]
                assert stop_fill is not None or primary is not ReasonCode.EXIT_STOP
                fill_price = stop_fill if primary is ReasonCode.EXIT_STOP else bar.close
                if primary is ReasonCode.EXIT_STOP:
                    events.append(
                        self._event(
                            bar,
                            EventType.STOP_HIT,
                            ReasonCode.EXIT_STOP,
                            trade_id=trade.trade_id,
                            side=trade.side,
                            price=fill_price,
                        )
                    )
                events.append(
                    self._event(
                        bar,
                        EventType.TRADE_CLOSED,
                        primary,
                        trade_id=trade.trade_id,
                        side=trade.side,
                        price=fill_price,
                        reasons=tuple(close_reasons),
                    )
                )
                trade = None
                closed_this_bar = True

        # Pending orders are meaningful only while entry data is usable and
        # their directional HEMA alignment remains in the active bias epoch.
        if pending_direction is not None:
            cancel_reason: ReasonCode | None = None
            if not entry_ready:
                cancel_reason = (
                    ReasonCode.PENDING_SETUP_CANCELLED_BY_READINESS
                    if not bar.strategy_ready
                    else ReasonCode.REQUIRED_DATA_UNAVAILABLE
                )
            elif pending_bias_epoch != epoch or bar.htf_bias is not pending_direction:
                cancel_reason = ReasonCode.PENDING_SETUP_CANCELLED_BY_BIAS
            elif bar.hema_direction is not pending_direction:
                cancel_reason = ReasonCode.PENDING_SETUP_CANCELLED_BY_HEMA
            if cancel_reason is not None:
                events.append(
                    self._event(
                        bar,
                        EventType.SETUP_CANCELLED,
                        cancel_reason,
                        side=pending_direction,
                    )
                )
                pending_direction = None
                pending_flip_timestamp = None
                pending_bias_epoch = None

        if closed_this_bar:
            # An explicit marker makes the rejection inspectable without
            # allowing a same-bar close/reverse even if all inputs align.
            if fresh_hema_flip:
                events.append(
                    self._event(bar, EventType.DECISION_REJECTED, ReasonCode.NO_SAME_BAR_REVERSAL)
                )
        elif trade is None and entry_ready:
            if pending_direction is not None:
                if bar.kalman_direction is pending_direction:
                    trade = self._open_trade(
                        bar,
                        pending_direction,
                        epoch,
                        (
                            pending_flip_timestamp
                            if pending_flip_timestamp is not None
                            else bar.timestamp
                        ),
                        state.next_trade_sequence,
                    )
                    events.append(self._opened_event(bar, trade))
                    pending_direction = None
                    pending_flip_timestamp = None
                    pending_bias_epoch = None
            elif fresh_hema_flip:
                assert bar.hema_flip is not None
                if bar.hema_flip is not bar.htf_bias:
                    events.append(
                        self._event(bar, EventType.DECISION_REJECTED, ReasonCode.HEMA_FLIP_WRONG_DIRECTION, side=bar.hema_flip)
                    )
                elif activation_timestamp is None or bar.timestamp < activation_timestamp:
                    events.append(
                        self._event(bar, EventType.DECISION_REJECTED, ReasonCode.HEMA_FLIP_BEFORE_BIAS_EPOCH, side=bar.hema_flip)
                    )
                elif bar.kalman_direction is bar.hema_flip:
                    trade = self._open_trade(bar, bar.hema_flip, epoch, bar.timestamp, state.next_trade_sequence)
                    events.append(self._opened_event(bar, trade))
                else:
                    pending_direction = bar.hema_flip
                    pending_flip_timestamp = bar.timestamp
                    pending_bias_epoch = epoch
                    events.append(
                        self._event(bar, EventType.SETUP_ARMED, ReasonCode.PENDING_SETUP_ARMED, side=bar.hema_flip)
                    )
        elif trade is None and pending_direction is None and fresh_hema_flip:
            # Useful, non-noisy diagnostics only for an actual fresh event.
            reason = self._entry_block_reason(bar, readiness)
            events.append(self._event(bar, EventType.DECISION_REJECTED, reason, side=bar.hema_flip))

        if trade is not None and trade.trade_id.endswith(f":{state.next_trade_sequence}"):
            next_sequence = state.next_trade_sequence + 1
        else:
            next_sequence = state.next_trade_sequence
        status = self._status(trade, pending_direction, readiness)
        new_state = StrategyState(
            symbol=state.symbol,
            status=status,
            current_bias=bar.htf_bias,
            previous_bias=old_bias,
            bias_epoch=epoch,
            bias_activation_timestamp=activation_timestamp,
            current_hema=bar.hema_direction,
            previous_hema=state.current_hema,
            current_kalman=bar.kalman_direction,
            previous_kalman=state.current_kalman,
            pending_direction=pending_direction,
            pending_flip_timestamp=pending_flip_timestamp,
            pending_bias_epoch=pending_bias_epoch,
            trade=trade,
            next_trade_sequence=next_sequence,
            last_timestamp=bar.timestamp,
            readiness=readiness,
        )
        return StrategyResult(state=new_state, events=tuple(events))

    @staticmethod
    def _opposite(direction: Direction) -> Direction:
        return Direction.SHORT if direction is Direction.LONG else Direction.LONG

    @staticmethod
    def _is_fresh_event(
        event: Direction | None, current: Direction | None, previous: Direction | None
    ) -> bool:
        return event is not None and event is current and event is not previous

    @staticmethod
    def _readiness(bar: StrategyBar) -> ReadinessState:
        if not bar.strategy_ready:
            return ReadinessState.DATA_BLOCKED
        if (
            bar.htf_bias is None
            or bar.hema_direction is None
            or bar.kalman_direction is None
            or bar.atr is None
            or not math.isfinite(bar.atr)
            or bar.atr <= 0.0
        ):
            return ReadinessState.WARMING_UP
        return ReadinessState.READY

    @staticmethod
    def _status(
        trade: OpenTrade | None, pending: Direction | None, readiness: ReadinessState
    ) -> StrategyStatus:
        if trade is not None:
            return StrategyStatus.OPEN_LONG if trade.side is Direction.LONG else StrategyStatus.OPEN_SHORT
        if pending is Direction.LONG:
            return StrategyStatus.PENDING_LONG
        if pending is Direction.SHORT:
            return StrategyStatus.PENDING_SHORT
        if readiness is ReadinessState.DATA_BLOCKED:
            return StrategyStatus.DATA_BLOCKED
        if readiness is ReadinessState.WARMING_UP:
            return StrategyStatus.WARMING_UP
        return StrategyStatus.FLAT

    def _open_trade(
        self, bar: StrategyBar, side: Direction, epoch: int, origin: int, sequence: int
    ) -> OpenTrade:
        assert bar.atr is not None
        distance = bar.atr * self.config.atr_multiplier
        stop = bar.close - distance if side is Direction.LONG else bar.close + distance
        return OpenTrade(
            trade_id=f"{bar.symbol}:{sequence}",
            side=side,
            entry_price=bar.close,
            entry_timestamp=bar.timestamp,
            atr_at_entry=bar.atr,
            stop_price=stop,
            bias_epoch=epoch,
            setup_origin_timestamp=origin,
        )

    @staticmethod
    def _stop_result(trade: OpenTrade, bar: StrategyBar) -> tuple[bool, float | None]:
        if trade.side is Direction.LONG and bar.low <= trade.stop_price:
            return True, bar.open if bar.open < trade.stop_price else trade.stop_price
        if trade.side is Direction.SHORT and bar.high >= trade.stop_price:
            return True, bar.open if bar.open > trade.stop_price else trade.stop_price
        return False, None

    @staticmethod
    def _event(
        bar: StrategyBar,
        type: EventType,
        reason: ReasonCode,
        *,
        trade_id: str | None = None,
        side: Direction | None = None,
        price: float | None = None,
        reasons: tuple[ReasonCode, ...] = (),
        metadata: tuple[tuple[str, str | int | float], ...] = (),
    ) -> StrategyEvent:
        return StrategyEvent(type, bar.symbol, bar.timestamp, reason, trade_id, side, price, reasons, metadata)

    def _opened_event(self, bar: StrategyBar, trade: OpenTrade) -> StrategyEvent:
        return self._event(
            bar,
            EventType.TRADE_OPENED,
            ReasonCode.ENTRY_ACCEPTED,
            trade_id=trade.trade_id,
            side=trade.side,
            price=trade.entry_price,
            metadata=(("atr", trade.atr_at_entry), ("bias_epoch", trade.bias_epoch), ("stop", trade.stop_price)),
        )

    @staticmethod
    def _entry_block_reason(bar: StrategyBar, readiness: ReadinessState) -> ReasonCode:
        if not bar.strategy_ready:
            return ReasonCode.STRATEGY_NOT_READY
        if bar.htf_bias is None:
            return ReasonCode.NO_HTF_BIAS
        if bar.atr is None or not math.isfinite(bar.atr) or bar.atr <= 0.0:
            return ReasonCode.INVALID_ATR
        return ReasonCode.REQUIRED_DATA_UNAVAILABLE
