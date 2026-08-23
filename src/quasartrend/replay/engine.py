"""Chronological closed-candle replay using the existing indicator and strategy APIs."""

from __future__ import annotations

from collections.abc import Iterable
import math

from quasartrend.indicators import Candle, HemaRelation, HemaTrend, KalmanStep, TrendDirection
from quasartrend.indicators.pine import dumps_checkpoint, loads_checkpoint
from quasartrend.strategy import Direction, StrategyBar, StrategyConfig, StrategyEngine, StrategyState

from .models import (
    HistoricalBar,
    ReplayConfig,
    ReplayResult,
    ReplayState,
    ReplayStepResult,
    ReplayTrace,
    Timeframe,
)


def _hema_direction(relation: HemaRelation) -> Direction | None:
    if relation is HemaRelation.ABOVE:
        return Direction.LONG
    if relation is HemaRelation.BELOW:
        return Direction.SHORT
    return None


def _kalman_direction(direction: TrendDirection) -> Direction:
    return Direction.LONG if direction is TrendDirection.BULLISH else Direction.SHORT


class ReplayEngine:
    """Stateful-by-checkpoint adapter for one symbol's ordered finalized bars."""

    def __init__(
        self,
        config: ReplayConfig | None = None,
        strategy_config: StrategyConfig | None = None,
    ) -> None:
        self.config = config or ReplayConfig()
        self.strategy_engine = StrategyEngine(strategy_config)

    def initial_state(self, symbol: str) -> ReplayState:
        if not symbol:
            raise ValueError("symbol must be non-empty")
        ltf_hema = HemaTrend(self.config.ltf_hema_fast_length, self.config.ltf_hema_slow_length)
        htf_hema = HemaTrend(self.config.htf_hema_fast_length, self.config.htf_hema_slow_length)
        kalman = KalmanStep(
            kalman_period=self.config.kalman_period,
            kalman_alpha=self.config.kalman_alpha,
            kalman_beta=self.config.kalman_beta,
            factor=self.config.kalman_factor,
            atr_period=self.config.kalman_atr_period,
        )
        return ReplayState(
            symbol=symbol,
            strategy_state=StrategyState.initial(symbol),
            ltf_hema_checkpoint=dumps_checkpoint(ltf_hema.to_checkpoint()),
            ltf_kalman_checkpoint=dumps_checkpoint(kalman.to_checkpoint()),
            htf_hema_checkpoint=dumps_checkpoint(htf_hema.to_checkpoint()),
        )

    def step(self, state: ReplayState, bar: HistoricalBar) -> ReplayStepResult:
        if bar.symbol != state.symbol:
            raise ValueError("bar.symbol must match replay state.symbol")
        if state.chronology_cursor is not None and bar.processing_key <= state.chronology_cursor:
            if bar.processing_key == state.chronology_cursor:
                raise ValueError("duplicate bar or invalid equal-finalization ordering")
            raise ValueError("bars must be supplied in strict finalization order")

        candle = Candle(
            bar.open, bar.high, bar.low, bar.close,
            float("nan") if bar.volume is None else bar.volume,
            bar.open_time,
        )
        if bar.timeframe is Timeframe.HOURS_4:
            htf = HemaTrend.from_checkpoint(
                loads_checkpoint(state.htf_hema_checkpoint),
                expected_fast_length=self.config.htf_hema_fast_length,
                expected_slow_length=self.config.htf_hema_slow_length,
            )
            result = htf.update(candle)
            bias = _hema_direction(result.relation)
            next_state = ReplayState(
                symbol=state.symbol,
                strategy_state=state.strategy_state,
                ltf_hema_checkpoint=state.ltf_hema_checkpoint,
                ltf_kalman_checkpoint=state.ltf_kalman_checkpoint,
                htf_hema_checkpoint=dumps_checkpoint(htf.to_checkpoint()),
                latest_htf_bias=bias,
                chronology_cursor=bar.processing_key,
            )
            return ReplayStepResult(
                next_state,
                ReplayTrace(bar, None, (), state.strategy_state, bias),
            )

        ltf_hema = HemaTrend.from_checkpoint(
            loads_checkpoint(state.ltf_hema_checkpoint),
            expected_fast_length=self.config.ltf_hema_fast_length,
            expected_slow_length=self.config.ltf_hema_slow_length,
        )
        kalman = KalmanStep.from_checkpoint(
            loads_checkpoint(state.ltf_kalman_checkpoint),
            expected_kalman_period=self.config.kalman_period,
            expected_kalman_alpha=self.config.kalman_alpha,
            expected_kalman_beta=self.config.kalman_beta,
            expected_factor=self.config.kalman_factor,
            expected_atr_period=self.config.kalman_atr_period,
        )
        hema_result = ltf_hema.update(candle)
        kalman_result = kalman.update(candle)
        hema_direction = _hema_direction(hema_result.relation)
        hema_flip = (
            Direction.LONG if hema_result.bullish_cross else
            Direction.SHORT if hema_result.bearish_cross else None
        )
        kalman_direction = _kalman_direction(kalman_result.semantic_direction)
        kalman_transition = (
            Direction.LONG if kalman_result.bullish_transition else
            Direction.SHORT if kalman_result.bearish_transition else None
        )
        atr = kalman_result.atr if math.isfinite(kalman_result.atr) else None
        strategy_bar = StrategyBar(
            symbol=bar.symbol,
            timestamp=bar.finalized_at,
            open=bar.open,
            high=bar.high,
            low=bar.low,
            close=bar.close,
            htf_bias=state.latest_htf_bias,
            hema_direction=hema_direction,
            kalman_direction=kalman_direction,
            atr=atr,
            strategy_ready=True,
            hema_flip=hema_flip,
            kalman_transition=kalman_transition,
        )
        strategy_result = self.strategy_engine.step(state.strategy_state, strategy_bar)
        next_state = ReplayState(
            symbol=state.symbol,
            strategy_state=strategy_result.state,
            ltf_hema_checkpoint=dumps_checkpoint(ltf_hema.to_checkpoint()),
            ltf_kalman_checkpoint=dumps_checkpoint(kalman.to_checkpoint()),
            htf_hema_checkpoint=state.htf_hema_checkpoint,
            latest_htf_bias=state.latest_htf_bias,
            chronology_cursor=bar.processing_key,
        )
        return ReplayStepResult(
            next_state,
            ReplayTrace(
                bar, strategy_bar, strategy_result.events, strategy_result.state,
                state.latest_htf_bias,
            ),
        )

    def run(
        self, bars: Iterable[HistoricalBar], state: ReplayState | None = None
    ) -> ReplayResult:
        iterator = iter(bars)
        try:
            first = next(iterator)
        except StopIteration:
            if state is None:
                raise ValueError("an initial state or at least one bar is required")
            return ReplayResult(state)
        active_state = state or self.initial_state(first.symbol)
        first_result = self.step(active_state, first)
        active_state = first_result.state
        traces = [first_result.trace]
        for bar in iterator:
            stepped = self.step(active_state, bar)
            active_state = stepped.state
            traces.append(stepped.trace)
        return ReplayResult(active_state, tuple(traces))
