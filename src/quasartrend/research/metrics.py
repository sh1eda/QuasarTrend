"""Validated R-multiple aggregation and deliberately preliminary evidence."""
from __future__ import annotations

import math
from statistics import mean, median

from .models import CandidateComparison, CandidateEvidence, ResearchMetrics, TradeRow
from .models import MAE_MFE_CONVENTION_VERSION
from .adr import BAR_MS

_TOLERANCE = 1e-12


def _finite(value: float | None, name: str) -> float:
    if value is None or not math.isfinite(value):
        raise ValueError(f"{name} must be finite")
    return value


def _validate_closed(row: TradeRow) -> None:
    if row.mae_mfe_convention_version != MAE_MFE_CONVENTION_VERSION:
        raise ValueError("closed row MAE/MFE convention mismatch")
    if not isinstance(row.exit_event_id, str) or not row.exit_event_id:
        raise ValueError("closed row requires an exit event identity")
    risk = _finite(row.canonical_risk_per_unit, "risk")
    quantity = _finite(row.quantity, "quantity")
    net_pnl = _finite(row.net_pnl, "net_pnl")
    realized_r = _finite(row.realized_r, "realized_r")
    if risk <= 0 or quantity <= 0:
        raise ValueError("risk and quantity must be positive")
    if not math.isclose(realized_r, net_pnl / (risk * quantity), rel_tol=_TOLERANCE, abs_tol=_TOLERANCE):
        raise ValueError("realized_r does not match net_pnl/risk/quantity")
    exit_timestamp = row.exit_timestamp
    exit_open = row.exit_source_open_timestamp
    exit_finalized = row.exit_finalized_timestamp
    for name, value in (("exit_timestamp", exit_timestamp), ("exit_source_open_timestamp", exit_open), ("exit_finalized_timestamp", exit_finalized)):
        if isinstance(value, bool) or not isinstance(value, int):
            raise ValueError(f"closed rows require integer {name}")
    assert exit_timestamp is not None and exit_open is not None and exit_finalized is not None
    if exit_finalized != exit_timestamp or exit_open + BAR_MS != exit_finalized or exit_timestamp < row.decision_timestamp:
        raise ValueError("closed exit timestamps are inconsistent")
    canonical_exit = _finite(row.canonical_exit_price, "canonical_exit_price")
    execution_exit = _finite(row.execution_exit_price, "execution_exit_price")
    gross_pnl = _finite(row.gross_pnl, "gross_pnl")
    entry_fee = _finite(row.entry_fee, "entry_fee")
    exit_fee = _finite(row.exit_fee, "exit_fee")
    total_fees = _finite(row.total_fees, "total_fees")
    _ = canonical_exit, execution_exit
    if entry_fee < 0 or exit_fee < 0 or total_fees < 0:
        raise ValueError("fees must be non-negative")
    if not math.isclose(total_fees, entry_fee + exit_fee, rel_tol=_TOLERANCE, abs_tol=_TOLERANCE):
        raise ValueError("total fees mismatch")
    if not math.isclose(net_pnl, gross_pnl - total_fees, rel_tol=_TOLERANCE, abs_tol=_TOLERANCE):
        raise ValueError("net pnl accounting mismatch")
    if not isinstance(row.exit_primary_reason, str) or not row.exit_primary_reason:
        raise ValueError("closed row requires primary exit reason")
    if not row.exit_all_reasons or row.exit_primary_reason != row.exit_all_reasons[0]:
        raise ValueError("closed row exit reasons are inconsistent")
    if row.stop_hit is not ("exit_stop" in row.exit_all_reasons):
        raise ValueError("stop_hit must match exit reasons")
    if row.strategy_exit is not any(reason != "exit_stop" for reason in row.exit_all_reasons):
        raise ValueError("strategy_exit must match exit reasons")
    if not isinstance(row.stop_hit, bool) or not isinstance(row.strategy_exit, bool):
        raise ValueError("closed rows require boolean stop_hit and strategy_exit")
    for name, value in (("observed_duration_bars", row.observed_duration_bars), ("expected_duration_bars", row.expected_duration_bars), ("elapsed_duration_ms", row.elapsed_duration_ms)):
        if isinstance(value, bool) or not isinstance(value, int) or value < 0:
            raise ValueError(f"closed rows require non-negative integer {name}")
    assert row.elapsed_duration_ms is not None and row.expected_duration_bars is not None and row.observed_duration_bars is not None
    if row.elapsed_duration_ms != exit_timestamp - row.decision_timestamp or row.expected_duration_bars * BAR_MS != row.elapsed_duration_ms or row.observed_duration_bars > row.expected_duration_bars:
        raise ValueError("closed duration fields are inconsistent")
    if len(row.data_quality_flags) != len(set(row.data_quality_flags)):
        raise ValueError("quality flags must be unique")
    values = (row.mae, row.mfe, row.mae_r, row.mfe_r)
    if all(value is None for value in values):
        if "post_entry_15m_gap" not in row.data_quality_flags or row.observed_duration_bars >= row.expected_duration_bars:
            raise ValueError("unavailable excursions require explicit quality reason")
        return
    if any(value is None for value in values):
        raise ValueError("MAE/MFE fields must be all present or all absent")
    if "post_entry_15m_gap" in row.data_quality_flags or row.observed_duration_bars != row.expected_duration_bars:
        raise ValueError("complete excursions require contiguous observed bars")
    mae, mfe, mae_r, mfe_r = (_finite(value, "excursion") for value in values)
    if min(mae, mfe, mae_r, mfe_r) < 0:
        raise ValueError("MAE/MFE values must be non-negative")
    if not math.isclose(mae_r, mae / risk, rel_tol=_TOLERANCE, abs_tol=_TOLERANCE):
        raise ValueError("mae_r is not normalized by risk")
    if not math.isclose(mfe_r, mfe / risk, rel_tol=_TOLERANCE, abs_tol=_TOLERANCE):
        raise ValueError("mfe_r is not normalized by risk")


def _validate_censored(row: TradeRow) -> None:
    if row.mae_mfe_convention_version != MAE_MFE_CONVENTION_VERSION:
        raise ValueError("censored row MAE/MFE convention mismatch")
    forbidden = (
        row.exit_timestamp, row.exit_source_open_timestamp, row.exit_finalized_timestamp,
        row.canonical_exit_price, row.execution_exit_price, row.exit_primary_reason,
        row.gross_pnl, row.net_pnl, row.entry_fee, row.exit_fee, row.total_fees,
        row.realized_r, row.mae, row.mfe, row.mae_r, row.mfe_r,
        row.observed_duration_bars, row.expected_duration_bars, row.elapsed_duration_ms,
    )
    if any(value is not None for value in forbidden) or row.exit_event_id is not None or row.exit_all_reasons or row.stop_hit is not None or row.strategy_exit is not None:
        raise ValueError("censored row contains outcome fields")


def calculate_metrics(
    rows: tuple[TradeRow, ...] | list[TradeRow], *, eligible_setups: int,
    baseline: ResearchMetrics | None = None,
) -> ResearchMetrics:
    if isinstance(eligible_setups, bool) or not isinstance(eligible_setups, int) or eligible_setups < 0:
        raise ValueError("eligible_setups must be a non-bool non-negative integer")
    trade_rows = tuple(rows)
    ids: set[str] = set()
    for row in trade_rows:
        if row.trade_id in ids:
            raise ValueError("duplicate trade id")
        ids.add(row.trade_id)
        if row.outcome_state == "closed":
            _validate_closed(row)
        elif row.outcome_state == "censored":
            _validate_censored(row)
        else:
            raise ValueError("unknown outcome state")
    closed = tuple(row for row in trade_rows if row.outcome_state == "closed")
    r_values = tuple(_finite(row.realized_r, "realized_r") for row in closed)
    total_r = float(sum(r_values))
    gross_profit = sum(value for value in r_values if value > 0)
    gross_loss = sum(value for value in r_values if value < 0)
    mae_values = tuple(_finite(row.mae_r, "mae_r") for row in closed if row.mae_r is not None)
    mfe_values = tuple(_finite(row.mfe_r, "mfe_r") for row in closed if row.mfe_r is not None)
    duration_values = tuple(row.elapsed_duration_ms for row in closed if row.elapsed_duration_ms is not None)
    observed_duration_bars = tuple(
        row.observed_duration_bars
        for row in closed
        if row.observed_duration_bars is not None
    )
    expected_duration_bars = tuple(
        row.expected_duration_bars
        for row in closed
        if row.expected_duration_bars is not None
    )
    if baseline is None:
        setup_retention = None if eligible_setups == 0 else 1.0
        trade_retention = None if not trade_rows else 1.0
    else:
        setup_retention = None if baseline.eligible_setups == 0 else eligible_setups / baseline.eligible_setups
        trade_retention = None if baseline.opened_trades == 0 else len(trade_rows) / baseline.opened_trades
    return ResearchMetrics(
        eligible_setups=eligible_setups,
        opened_trades=len(trade_rows),
        closed_trades=len(closed),
        retained_setups=eligible_setups,
        retained_trades=len(trade_rows),
        setup_retention=setup_retention,
        trade_retention=trade_retention,
        total_r=total_r,
        expectancy_r=None if not closed else total_r / len(closed),
        r_per_setup=None if eligible_setups == 0 else total_r / eligible_setups,
        profit_factor=None if not closed or gross_loss == 0 else gross_profit / abs(gross_loss),
        stop_rate=None if not closed else sum(row.stop_hit for row in closed) / len(closed),
        win_rate=None if not closed else sum(value > 0 for value in r_values) / len(closed),
        mean_r=None if not closed else mean(r_values),
        median_r=None if not closed else median(r_values),
        mae_observation_count=len(mae_values),
        mfe_observation_count=len(mfe_values),
        mean_mae_r=None if not mae_values else mean(mae_values),
        mean_mfe_r=None if not mfe_values else mean(mfe_values),
        mean_duration_ms=None if not duration_values else mean(duration_values),
        mean_observed_duration_bars=(
            None if not observed_duration_bars else mean(observed_duration_bars)
        ),
        mean_expected_duration_bars=(
            None if not expected_duration_bars else mean(expected_duration_bars)
        ),
    )


def compare_candidate(
    baseline: ResearchMetrics, candidate: ResearchMetrics, *, window_role: str,
    window_start_ms: int, window_end_ms: int,
) -> CandidateComparison:
    if window_start_ms > window_end_ms:
        raise ValueError("invalid comparison window")
    def delta(left: float | None, right: float | None) -> float | None:
        return None if left is None or right is None else right - left
    economically_improved = (
        baseline.expectancy_r is not None and candidate.expectancy_r is not None
        and candidate.expectancy_r > baseline.expectancy_r
        and baseline.r_per_setup is not None and candidate.r_per_setup is not None
        and candidate.r_per_setup > baseline.r_per_setup
        and baseline.profit_factor is not None and candidate.profit_factor is not None
        and candidate.profit_factor >= baseline.profit_factor
        and (baseline.total_r <= 0 or candidate.total_r >= 0.8 * baseline.total_r)
    )
    return CandidateComparison(window_role, window_start_ms, window_end_ms, baseline, candidate, delta(baseline.expectancy_r, candidate.expectancy_r), candidate.total_r - baseline.total_r, delta(baseline.r_per_setup, candidate.r_per_setup), delta(baseline.profit_factor, candidate.profit_factor), delta(baseline.stop_rate, candidate.stop_rate), None if baseline.retained_setups is None or candidate.retained_setups is None else candidate.retained_setups - baseline.retained_setups, None if baseline.retained_trades is None or candidate.retained_trades is None else candidate.retained_trades - baseline.retained_trades, economically_improved)


def evaluate_candidate_evidence(*, baseline: ResearchMetrics, candidate: ResearchMetrics, final_oos: CandidateComparison, oos_comparisons: tuple[CandidateComparison, ...] | list[CandidateComparison]) -> CandidateEvidence:
    comparisons = tuple(oos_comparisons)
    reasons = ["production promotion requires a lineage-bound registered experiment"]
    identities: set[tuple[str, int, int]] = set()
    prior_end: int | None = None
    for comparison in sorted(comparisons, key=lambda item: item.window_start_ms):
        identity = (comparison.window_role, comparison.window_start_ms, comparison.window_end_ms)
        if identity in identities:
            raise ValueError("repeated OOS comparison")
        if prior_end is not None and comparison.window_start_ms <= prior_end:
            raise ValueError("overlapping OOS comparisons")
        identities.add(identity)
        prior_end = comparison.window_end_ms
    if (final_oos.window_role, final_oos.window_start_ms, final_oos.window_end_ms) not in identities:
        raise ValueError("final OOS comparison must be registered")
    if final_oos.candidate.closed_trades < 50:
        reasons.append("fewer than 50 closed final-OOS trades retained")
    if candidate.setup_retention is None or candidate.setup_retention < .25:
        reasons.append("fewer than 25% of baseline eligible setups retained")
    if len(comparisons) < 3:
        reasons.append("fewer than 3 non-overlapping OOS windows")
    if sum(item.economically_improved for item in comparisons) < 2:
        reasons.append("fewer than 2 economically improved OOS windows")
    aggregate = compare_candidate(baseline, candidate, window_role="aggregate", window_start_ms=0, window_end_ms=0)
    if not aggregate.economically_improved:
        reasons.append("aggregate economics do not satisfy required conditions")
    return CandidateEvidence(False, tuple(reasons), final_oos.candidate.closed_trades, candidate.setup_retention, len(comparisons), sum(item.economically_improved for item in comparisons), None if baseline.total_r == 0 else candidate.total_r / baseline.total_r)
