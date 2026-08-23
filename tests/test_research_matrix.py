"""Focused regression matrix for Phase 7 research invariants."""
from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from quasartrend.replay import HistoricalBar, Timeframe
from quasartrend.strategy import Direction
from quasartrend.research import (
    AdrStatus,
    ChronologicalWindow,
    compare_candidate,
    evaluate_candidate_evidence,
    event_identity,
    fingerprint,
    setup_identity,
    validate_canonical_15m_bars,
    validate_entry_feature_selectors,
    validate_windows,
    walk_forward_windows,
)
from quasartrend.research import calculate_excursions, parse_tradingview_export, source_open_utc_features
from quasartrend.research.adr import adr_context_for_date, daily_ranges, utc_date
from quasartrend.research.dataset import _session
from quasartrend.research.models import ResearchMetrics
from quasartrend.research.models import SetupRow, TradeRow, schema_map
from dataclasses import fields


def _bar(timestamp: int, *, symbol: str = "BTCUSDT") -> HistoricalBar:
    return HistoricalBar(symbol, Timeframe.MINUTES_15, timestamp, 100, 102, 99, 101)


def _days(count: int) -> tuple[HistoricalBar, ...]:
    start = int(datetime(2026, 1, 1, tzinfo=UTC).timestamp() * 1000)
    return tuple(_bar(start + day * 86_400_000 + slot * 900_000) for day in range(count) for slot in range(96))


def _metrics(*, total: float = 10.0, expectancy: float = 1.0, r_setup: float = 1.0, pf: float = 2.0, closed: int = 60) -> ResearchMetrics:
    return ResearchMetrics(
        eligible_setups=100, opened_trades=closed, closed_trades=closed,
        retained_setups=50, retained_trades=closed, setup_retention=.5,
        trade_retention=1.0, total_r=total, expectancy_r=expectancy,
        r_per_setup=r_setup, profit_factor=pf, stop_rate=.2, win_rate=.5,
        mean_r=expectancy, median_r=expectancy, mae_observation_count=closed,
        mfe_observation_count=closed, mean_mae_r=.5, mean_mfe_r=1.5,
        mean_duration_ms=900_000,
    )


def test_adr_warmup_before_fourteen_prior_dates() -> None:
    ranges = daily_ranges(_days(14))
    assert adr_context_for_date("2026-01-14", ranges).status is AdrStatus.WARMUP


def test_adr_exactly_fourteen_complete_dates_is_available() -> None:
    bars = _days(15)
    context = adr_context_for_date(utc_date(bars[-1].open_time), daily_ranges(bars))
    assert context.status is AdrStatus.AVAILABLE and context.adr == 3


def test_adr_excludes_incomplete_current_day() -> None:
    bars = _days(15) + (_bar(int(datetime(2026, 1, 16, tzinfo=UTC).timestamp() * 1000)),)
    ranges = daily_ranges(bars)
    assert ranges["2026-01-16"] is None
    assert adr_context_for_date("2026-01-16", ranges).adr == 3


def test_adr_partial_prior_day_is_unavailable() -> None:
    bars = _days(16)
    partial = bars[:96 * 3 + 95] + bars[96 * 3 + 96:]
    assert adr_context_for_date("2026-01-16", daily_ranges(partial)).adr is None


def test_adr_utc_midnight_boundary_uses_open_time_date() -> None:
    bars = _days(15)
    assert utc_date(bars[0].open_time) == "2026-01-01"
    assert utc_date(bars[95].open_time) == "2026-01-01"
    assert utc_date(bars[96].open_time) == "2026-01-02"


def test_bad_ohlc_envelope_is_rejected() -> None:
    bar = _bar(0)
    object.__setattr__(bar, "low", 101.5)
    with pytest.raises(ValueError, match="envelope"):
        validate_canonical_15m_bars((bar,))


def test_misaligned_15m_open_is_rejected() -> None:
    with pytest.raises(ValueError, match="aligned"):
        validate_canonical_15m_bars((_bar(1),))


def test_duplicate_and_out_of_order_bars_are_rejected() -> None:
    with pytest.raises(ValueError, match="strict chronological"):
        validate_canonical_15m_bars((_bar(900_000), _bar(0)))
    with pytest.raises(ValueError, match="strict chronological"):
        validate_canonical_15m_bars((_bar(0), _bar(0)))


def test_mixed_symbol_bars_are_rejected() -> None:
    with pytest.raises(ValueError, match="one symbol"):
        validate_canonical_15m_bars((_bar(0), _bar(900_000, symbol="ETHUSDT")))


def test_session_complete_prefix_exposes_extrema() -> None:
    bars = [_bar(0), _bar(900_000)]
    status, count, opening, high, low = _session(bars, bars[-1])
    assert status.value == "complete_prefix" and (count, opening, high, low) == (2, 100, 102, 99)


def test_session_incomplete_prefix_hides_extrema() -> None:
    bars = [_bar(0), _bar(1_800_000)]
    status, count, opening, high, low = _session(bars, bars[-1])
    assert status.value == "incomplete_prefix" and count == 2
    assert (opening, high, low) == (None, None, None)


def test_session_2345_source_open_has_next_day_decision() -> None:
    source = int(datetime(2026, 1, 1, 23, 45, tzinfo=UTC).timestamp() * 1000)
    assert datetime.fromtimestamp((source + 900_000) / 1000, UTC).date().isoformat() == "2026-01-02"


def test_setup_identity_is_deterministic() -> None:
    first = setup_identity(symbol="BTCUSDT", bias_epoch=1, direction="long", setup_origin_timestamp=1, source_processing_key=(1, 1), strategy_fingerprint="a" * 64)
    assert first == setup_identity(symbol="BTCUSDT", bias_epoch=1, direction="long", setup_origin_timestamp=1, source_processing_key=(1, 1), strategy_fingerprint="a" * 64)


def test_event_identity_changes_with_ordinal() -> None:
    common = dict(symbol="BTCUSDT", source_processing_key=(1, 1), event_type="trade_opened", trade_id="BTCUSDT:1", strategy_fingerprint="a" * 64)
    assert event_identity(ordinal=0, **common) != event_identity(ordinal=1, **common)


def test_fingerprint_is_order_sensitive_for_source_streams() -> None:
    assert fingerprint((_bar(0), _bar(900_000))) != fingerprint((_bar(900_000), _bar(0)))


def test_unknown_feature_selector_is_rejected() -> None:
    with pytest.raises(ValueError):
        validate_entry_feature_selectors("trade", ("not_a_field",))


def test_duplicate_feature_selector_is_rejected() -> None:
    with pytest.raises(ValueError):
        validate_entry_feature_selectors("trade", ("direction", "direction"))


def test_setup_resolution_is_not_predictive() -> None:
    with pytest.raises(ValueError):
        validate_entry_feature_selectors("setup", ("resolution_reasons",))


def test_duplicate_window_roles_are_rejected() -> None:
    with pytest.raises(ValueError, match="duplicate window role"):
        validate_windows((ChronologicalWindow("x", "2026-01-01", "2026-01-01"), ChronologicalWindow("x", "2026-01-03", "2026-01-03")))


def test_overlapping_windows_are_rejected() -> None:
    with pytest.raises(ValueError, match="non-overlapping"):
        validate_windows((ChronologicalWindow("a", "2026-01-01", "2026-01-02"), ChronologicalWindow("b", "2026-01-02", "2026-01-03")))


def test_walk_forward_windows_do_not_overlap() -> None:
    windows = walk_forward_windows(start_date="2026-01-01", train_days=2, test_days=2, count=3)
    assert windows[0][1].end_ms < windows[1][0].start_ms < windows[1][1].end_ms


def test_evidence_comparison_requires_economic_improvement_not_win_rate() -> None:
    baseline = _metrics()
    candidate = _metrics(total=9, expectancy=.9, r_setup=.9, pf=2.1)
    result = compare_candidate(baseline, candidate, window_role="oos", window_start_ms=1, window_end_ms=2)
    assert not result.economically_improved


def test_evidence_rejects_repeated_windows() -> None:
    baseline = _metrics()
    candidate = _metrics(total=11, expectancy=1.1, r_setup=1.1, pf=2.1)
    item = compare_candidate(baseline, candidate, window_role="oos", window_start_ms=1, window_end_ms=2)
    with pytest.raises(ValueError, match="repeated"):
        evaluate_candidate_evidence(baseline=baseline, candidate=candidate, final_oos=item, oos_comparisons=(item, item))


def test_evidence_never_promotes_without_lineage_runner() -> None:
    baseline = _metrics()
    candidate = _metrics(total=11, expectancy=1.1, r_setup=1.1, pf=2.1)
    items = tuple(compare_candidate(baseline, candidate, window_role=f"oos-{index}", window_start_ms=index * 10, window_end_ms=index * 10 + 5) for index in range(3))
    evidence = evaluate_candidate_evidence(baseline=baseline, candidate=candidate, final_oos=items[-1], oos_comparisons=items)
    assert not evidence.production_eligible
    assert "production promotion requires a lineage-bound registered experiment" in evidence.reasons


def test_parser_rejects_duplicate_ohlc_mismatch() -> None:
    raw = b"time,open,high,low,close,timestamp,open,high,low,close\n0,1,2,.5,1.5,0,2,2,.5,1.5\n"
    with pytest.raises(ValueError, match="duplicate OHLC"):
        parse_tradingview_export(raw, declared_symbol="BTCUSDT", timeframe=Timeframe.MINUTES_15)


def test_parser_rejects_4h_misalignment_and_bad_envelope() -> None:
    raw = b"time,open,high,low,close,timestamp,open,high,low,close\n900,1,2,1.5,1.5,900000,1,2,1.5,1.5\n"
    with pytest.raises(ValueError):
        parse_tradingview_export(raw, declared_symbol="BTCUSDT", timeframe=Timeframe.HOURS_4)


def test_long_and_short_excursion_formulas_exclude_entry_bar() -> None:
    bars = (_bar(900_000), _bar(1_800_000))
    assert calculate_excursions(direction=Direction.LONG, entry_price=100, stop_price=98, subsequent_bars=bars) == (1, 2, .5, 1)
    assert calculate_excursions(direction=Direction.SHORT, entry_price=100, stop_price=102, subsequent_bars=bars) == (2, 1, 1, .5)


def test_source_open_utc_feature_helper_uses_source_not_decision_time() -> None:
    timestamp = int(datetime(2026, 1, 1, 23, 45, tzinfo=UTC).timestamp() * 1000)
    hour, weekday, bucket, offset = source_open_utc_features(timestamp)
    assert (hour, bucket, offset) == (23, 3, 85_500_000)


def test_parser_accepts_valid_dual_ohlc_stream() -> None:
    raw = b"time,open,high,low,close,timestamp,open,high,low,close\n0,1,2,.5,1.5,0,1,2,.5,1.5\n"
    assert len(parse_tradingview_export(raw, declared_symbol="BTCUSDT", timeframe=Timeframe.MINUTES_15)) == 1


def test_calculate_excursions_rejects_invalid_inputs() -> None:
    with pytest.raises(TypeError):
        calculate_excursions(direction="long", entry_price=100, stop_price=99, subsequent_bars=())
    with pytest.raises(ValueError):
        calculate_excursions(direction=Direction.LONG, entry_price=100, stop_price=100, subsequent_bars=())
    with pytest.raises(TypeError):
        calculate_excursions(direction=Direction.LONG, entry_price=100, stop_price=99, subsequent_bars=(object(),))


def test_schema_map_classifies_every_field_and_rejects_every_non_entry_selector() -> None:
    mapping = schema_map()
    for table, row_type in (("setup", SetupRow), ("trade", TradeRow)):
        assert set(mapping[table]) == {field.name for field in fields(row_type)}
        for name, classification in mapping[table].items():
            if classification.value != "entry_time_feature":
                with pytest.raises(ValueError):
                    validate_entry_feature_selectors(table, (name,))


def test_setup_rows_have_origin_only_schema_no_entry_or_accounting_fields() -> None:
    names = {field.name for field in fields(SetupRow)}
    assert "canonical_entry_price" not in names
    assert "execution_entry_price" not in names
    assert "net_pnl" not in names
