"""Event-only deterministic accounting; all trading decisions remain Phase 2's."""

from __future__ import annotations

from dataclasses import dataclass
from collections.abc import Iterable
import math

from quasartrend.replay import ReplayResult, ReplayTrace
from quasartrend.strategy import Direction, EventType, StrategyEvent, StrategyState

from .metrics import calculate_metrics
from .models import BacktestConfig, BacktestResult, ClosedTrade, EquityPoint


@dataclass(frozen=True, slots=True)
class _OpenAccountingTrade:
    trade_id: str
    symbol: str
    side: Direction
    timestamp: int
    canonical_price: float
    execution_price: float
    entry_fee: float
    setup_origin_timestamp: int | None
    bias_epoch: int | None


class BacktestEngine:
    """Apply only ``TRADE_OPENED``/``TRADE_CLOSED`` replay events.

    Realized equity starts at zero and changes only on close.  Slippage is in
    basis points of the canonical price and always worsens the fill.
    """

    def __init__(self, config: BacktestConfig | None = None) -> None:
        self.config = config or BacktestConfig()

    def run(self, replay: ReplayResult) -> BacktestResult:
        return self.run_traces(replay.traces, replay.final_strategy_state)

    def run_traces(
        self, traces: Iterable[ReplayTrace], final_strategy_state: StrategyState
    ) -> BacktestResult:
        trace_tuple = tuple(traces)
        prior_key: tuple[int, int] | None = None
        for trace in trace_tuple:
            key = trace.source_bar.processing_key
            if prior_key is not None and key <= prior_key:
                raise ValueError("replay traces must be in strict source processing order")
            prior_key = key
            if trace.post_state.symbol != trace.source_bar.symbol:
                raise ValueError("trace post-state symbol must match source bar symbol")
            for event in trace.events:
                if event.timestamp != trace.source_bar.finalized_at:
                    raise ValueError("event timestamp must match source bar finalization")
                if event.symbol != trace.source_bar.symbol or event.symbol != trace.post_state.symbol:
                    raise ValueError("event symbol must match trace source and post-state symbols")
        open_trade: _OpenAccountingTrade | None = None
        trades: list[ClosedTrade] = []
        equity: list[EquityPoint] = []
        realized_equity = 0.0
        all_events: list[StrategyEvent] = []
        seen_trade_ids: set[str] = set()
        for trace in trace_tuple:
            for event in trace.events:
                all_events.append(event)
                if event.type is EventType.TRADE_OPENED:
                    if open_trade is not None:
                        raise ValueError("received TRADE_OPENED while another trade is open")
                    if event.trade_id in seen_trade_ids:
                        raise ValueError("received duplicate TRADE_OPENED trade id")
                    open_trade = self._open(event, trace)
                    seen_trade_ids.add(open_trade.trade_id)
                elif event.type is EventType.TRADE_CLOSED:
                    if open_trade is None:
                        raise ValueError("received TRADE_CLOSED without an open trade")
                    closed = self._close(open_trade, event)
                    trades.append(closed)
                    realized_equity += closed.net_pnl
                    equity.append(EquityPoint(event.timestamp, closed.trade_id, realized_equity))
                    open_trade = None
        diagnostics = () if open_trade is None else (f"open trade remains at replay end: {open_trade.trade_id}",)
        trade_tuple = tuple(trades)
        equity_tuple = tuple(equity)
        return BacktestResult(
            final_strategy_state=final_strategy_state,
            replay_traces=trace_tuple,
            strategy_events=tuple(all_events),
            closed_trades=trade_tuple,
            equity_curve=equity_tuple,
            metrics=calculate_metrics(trade_tuple, equity_tuple),
            diagnostics=diagnostics,
        )

    def _open(self, event: StrategyEvent, trace: ReplayTrace) -> _OpenAccountingTrade:
        if event.trade_id is None or event.side is None or event.price is None:
            raise ValueError("TRADE_OPENED requires trade_id, side, and price")
        if not math.isfinite(event.price):
            raise ValueError("TRADE_OPENED price must be finite")
        trade = trace.post_state.trade
        if trade is None or trade.trade_id != event.trade_id:
            raise ValueError("TRADE_OPENED trace must retain its matching open strategy trade")
        if (
            trade.side is not event.side
            or trade.entry_timestamp != event.timestamp
            or trade.entry_price != event.price
        ):
            raise ValueError("TRADE_OPENED event must match its open strategy trade")
        if event.symbol != trade_id_symbol(event.trade_id):
            # The engine's trade IDs are symbol-prefixed, but this check is only
            # structural; a colon can legitimately occur in a symbol.
            raise ValueError("TRADE_OPENED trade id does not belong to event symbol")
        execution = self._entry_execution_price(event.price, event.side)
        entry_fee = execution * self.config.quantity * self.config.fee_bps / 10_000.0
        return _OpenAccountingTrade(
            event.trade_id, event.symbol, event.side, event.timestamp, event.price,
            execution, entry_fee, trade.setup_origin_timestamp, trade.bias_epoch,
        )

    def _close(self, opened: _OpenAccountingTrade, event: StrategyEvent) -> ClosedTrade:
        if event.trade_id is None or event.side is None or event.price is None:
            raise ValueError("TRADE_CLOSED requires trade_id, side, and price")
        if not math.isfinite(event.price):
            raise ValueError("TRADE_CLOSED price must be finite")
        if event.trade_id != opened.trade_id or event.side is not opened.side or event.symbol != opened.symbol:
            raise ValueError("TRADE_CLOSED must match the currently open trade")
        if event.timestamp < opened.timestamp:
            raise ValueError("TRADE_CLOSED cannot precede TRADE_OPENED")
        execution_exit = self._exit_execution_price(event.price, opened.side)
        exit_fee = execution_exit * self.config.quantity * self.config.fee_bps / 10_000.0
        sign = 1.0 if opened.side is Direction.LONG else -1.0
        gross = sign * (execution_exit - opened.execution_price) * self.config.quantity
        total_fees = opened.entry_fee + exit_fee
        net = gross - total_fees
        entry_notional = opened.execution_price * self.config.quantity
        return ClosedTrade(
            trade_id=opened.trade_id, symbol=opened.symbol, side=opened.side,
            entry_timestamp=opened.timestamp, exit_timestamp=event.timestamp,
            canonical_entry_price=opened.canonical_price, canonical_exit_price=event.price,
            execution_entry_price=opened.execution_price, execution_exit_price=execution_exit,
            quantity=self.config.quantity, gross_pnl=gross, entry_fee=opened.entry_fee,
            exit_fee=exit_fee, total_fees=total_fees, net_pnl=net,
            trade_return=net / entry_notional, exit_reason=event.reason.value,
            setup_origin_timestamp=opened.setup_origin_timestamp, bias_epoch=opened.bias_epoch,
        )

    def _entry_execution_price(self, price: float, side: Direction) -> float:
        fraction = self.config.slippage_bps / 10_000.0
        return price * (1.0 + fraction if side is Direction.LONG else 1.0 - fraction)

    def _exit_execution_price(self, price: float, side: Direction) -> float:
        fraction = self.config.slippage_bps / 10_000.0
        return price * (1.0 - fraction if side is Direction.LONG else 1.0 + fraction)


def trade_id_symbol(trade_id: str) -> str:
    """Extract Phase 2's final-colon-separated sequence suffix safely."""

    symbol, separator, sequence = trade_id.rpartition(":")
    if not separator or not symbol or not sequence.isdigit():
        raise ValueError("malformed strategy trade id")
    return symbol
