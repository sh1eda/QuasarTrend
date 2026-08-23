"""Small deterministic realized-PnL metrics with no capital-base assumptions."""

from __future__ import annotations

from .models import BacktestMetrics, ClosedTrade, EquityPoint


def calculate_metrics(
    trades: tuple[ClosedTrade, ...], equity_curve: tuple[EquityPoint, ...]
) -> BacktestMetrics:
    winners = tuple(trade for trade in trades if trade.net_pnl > 0.0)
    losers = tuple(trade for trade in trades if trade.net_pnl < 0.0)
    flats = tuple(trade for trade in trades if trade.net_pnl == 0.0)
    # Gross profit/loss deliberately describe fills before brokerage fees.
    # Win/loss classification remains net, since it describes realized outcome.
    gross_profit = sum(trade.gross_pnl for trade in trades if trade.gross_pnl > 0.0)
    gross_loss = -sum(trade.gross_pnl for trade in trades if trade.gross_pnl < 0.0)
    net_profit = sum(trade.net_pnl for trade in trades)
    total_fees = sum(trade.total_fees for trade in trades)
    peak = 0.0
    maximum_drawdown = 0.0
    for point in equity_curve:
        peak = max(peak, point.realized_equity)
        maximum_drawdown = max(maximum_drawdown, peak - point.realized_equity)
    total = len(trades)
    return BacktestMetrics(
        total_closed_trades=total,
        winning_trades=len(winners),
        losing_trades=len(losers),
        flat_trades=len(flats),
        win_rate=(len(winners) / total) if total else None,
        gross_profit=gross_profit,
        gross_loss=gross_loss,
        net_profit=net_profit,
        total_fees=total_fees,
        average_trade_pnl=(net_profit / total) if total else None,
        average_winner=(sum(trade.net_pnl for trade in winners) / len(winners)) if winners else None,
        average_loser=(sum(trade.net_pnl for trade in losers) / len(losers)) if losers else None,
        profit_factor=(gross_profit / gross_loss) if gross_loss else None,
        max_drawdown=maximum_drawdown,
        max_drawdown_percentage=None,
        long_trade_count=sum(trade.side.value == "long" for trade in trades),
        short_trade_count=sum(trade.side.value == "short" for trade in trades),
    )
