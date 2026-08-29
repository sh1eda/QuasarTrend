from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

import pytest

from quasartrend.replay import HistoricalBar, Timeframe
from quasartrend.research.xm_gold_compatibility import (
    COMPARISON_START_MS,
    M1_MS,
    XmM1Bar,
    _agreement,
    _bias_transition_markers,
    _h4_replay_input,
    _indicator_report,
    _m15_replay_input,
    _slope_direction_at_common_timestamps,
    _trade_economics,
    _verified_source_manifest,
    AggregatedBar,
    aggregate_m1,
    audit_xm_m1,
    match_setups,
    match_trades,
    parse_xm_m1_csv,
    xm_gold_compatibility_json,
)


def _row(timestamp_ms: int, price: float = 100.0) -> XmM1Bar:
    return XmM1Bar(timestamp_ms, price, price + 2, price - 1, price + .5, 1, 3, 0)


def _csv(rows: list[XmM1Bar]) -> bytes:
    lines = ["time_epoch,time_utc,open,high,low,close,tick_volume,spread,real_volume"]
    for item in rows:
        stamp = datetime.fromtimestamp(item.open_time / 1000, UTC).isoformat().replace("T", " ")
        lines.append(f"{item.open_time // 1000},{stamp},{item.open},{item.high},{item.low},{item.close},{item.tick_volume},{item.spread_points},{item.real_volume}")
    return ("\n".join(lines) + "\n").encode()


def test_raw_schema_utc_and_duplicate_rejection() -> None:
    rows = (_row(1_772_406_000_000), _row(1_772_406_060_000, 101))
    parsed = parse_xm_m1_csv(_csv(list(rows)))
    assert [item.raw_open_time for item in parsed] == [item.open_time for item in rows]
    assert [item.open_time for item in parsed] == [item.open_time - 2 * 60 * 60 * 1000 for item in rows]
    duplicated = _csv([rows[0], rows[0]])
    with pytest.raises(ValueError, match="strictly increasing"):
        parse_xm_m1_csv(duplicated)
    non_utc = _csv([rows[0]]).replace(b"+00:00", b"+03:00")
    with pytest.raises(ValueError, match="not UTC|disagree"):
        parse_xm_m1_csv(non_utc)


def test_m15_and_h4_aggregation_are_server_clock_aligned_and_canonical() -> None:
    start = 1_772_409_600_000  # 2026-03-02T00:00:00 server wall clock, a 4H boundary
    m15 = [_row(start + index * M1_MS, float(index)) for index in range(15)]
    result = aggregate_m1(m15, timeframe=Timeframe.MINUTES_15)
    assert len(result) == 1
    bar = result[0]
    assert (bar.bar.open_time, bar.bar.open, bar.bar.high, bar.bar.low, bar.bar.close) == (start - 2 * 60 * 60 * 1000, 0.0, 16.0, -1.0, 14.5)
    assert (bar.source_count, bar.source_contiguous, bar.complete) == (15, True, True)
    h4 = aggregate_m1([_row(start + index * M1_MS, float(index)) for index in range(240)], timeframe=Timeframe.HOURS_4)
    assert len(h4) == 1
    assert h4[0].bar.open_time == start - 2 * 60 * 60 * 1000
    assert h4[0].actual_finalized_at == h4[0].bar.finalized_at
    assert h4[0].source_count == 240 and h4[0].complete is True


def test_incomplete_aggregation_is_retained_without_synthetic_gap_fill() -> None:
    start = 1_772_409_600_000
    rows = [_row(start + index * M1_MS) for index in range(15) if index != 7]
    result = aggregate_m1(rows, timeframe=Timeframe.MINUTES_15)
    assert len(result) == 1
    assert result[0].source_count == 14
    assert result[0].source_contiguous is False
    assert result[0].complete is False
    assert result[0].bar.close == rows[-1].close
    # The missing source minute stays missing; the observed native bucket is
    # still a replay input with its first/last observed OHLC unchanged.
    assert _m15_replay_input(result) == (result[0].bar,)


def test_setup_matching_is_one_to_one_and_uses_predeclared_tie_breaking() -> None:
    reference = [{"timestamp": 100, "direction": "long"}, {"timestamp": 200, "direction": "long"}]
    candidate = [{"timestamp": 0, "direction": "long"}, {"timestamp": 200, "direction": "long"}, {"timestamp": 300, "direction": "short"}]
    match = match_setups(reference, candidate, tolerance_ms=100)
    assert [(item.reference_index, item.candidate_index, item.delta_ms) for item in match["matches"]] == [(0, 0, -100), (1, 1, 0)]
    assert match["exact_timestamp_matches"] == 1
    assert match["plus_minus_one_bar_matches"] == 1
    assert match["direction_mismatch_reference_count"] == 1
    assert match["candidate_unmatched_indexes"] == [2]


def test_trade_matching_requires_matched_setup_and_direction() -> None:
    setups_a = [{"timestamp": 100, "direction": "long"}, {"timestamp": 200, "direction": "short"}]
    setups_b = [{"timestamp": 100, "direction": "long"}, {"timestamp": 210, "direction": "short"}]
    setup_matches = match_setups(setups_a, setups_b, tolerance_ms=10)["matches"]
    left = [{"setup_timestamp": 100, "direction": "long", "entry_timestamp": 120}, {"setup_timestamp": 200, "direction": "short", "entry_timestamp": 220}]
    right = [{"setup_timestamp": 100, "direction": "long", "entry_timestamp": 121}, {"setup_timestamp": 210, "direction": "long", "entry_timestamp": 221}]
    result = match_trades(left, right, setup_matches, setups_a, setups_b)
    assert [(item.reference_index, item.candidate_index) for item in result["matches"]] == [(0, 0)]
    assert result["reference_unmatched_indexes"] == [1]


def test_indicator_and_economic_metrics_are_explicit_on_empty_and_nonempty_populations() -> None:
    agreement = _agreement({1: "long", 2: "short"}, {1: "long", 2: "long"})
    assert agreement["agreement_rate"] == .5
    assert agreement["confusion"] == {"long->long": 1, "short->long": 1}
    economics = _trade_economics([
        {"outcome": "closed", "r": 2.0, "stop_hit": False},
        {"outcome": "closed", "r": -1.0, "stop_hit": True},
        {"outcome": "censored", "r": None, "stop_hit": None},
    ], eligible=4)
    assert economics["total_r"] == 1.0
    assert economics["profit_factor"] == 2.0
    assert economics["r_per_setup"] == .25
    assert economics["censored_trades"] == 1
    assert economics["positive_r_mean"] == economics["positive_r_median"] == 2.0
    assert economics["negative_r_magnitude_mean"] == economics["negative_r_magnitude_median"] == 1.0
    assert economics["top_5_winner_positive_r_share"] == 1.0


def test_hema_slope_definition_uses_preceding_common_timestamp_without_tolerance() -> None:
    left, right = _slope_direction_at_common_timestamps(
        {1: 1.0, 2: 1.0, 3: 2.0, 4: None},
        {1: 3.0, 2: 2.0, 3: 2.0, 4: 1.0},
    )
    assert left == {1: None, 2: "flat", 3: "up", 4: None}
    assert right == {1: None, 2: "down", 3: "flat", 4: "down"}
    assert _agreement(left, right, exclude_unavailable=True)["comparable_count"] == 2
    assert _bias_transition_markers({1: None, 2: "long", 3: "long", 4: "short"}) == {1: None, 2: None, 3: None, 4: "short"}


def test_h4_bias_is_compared_as_of_common_m15_not_by_h4_open_timestamp() -> None:
    row = lambda bias: {"hema_direction": "long", "kalman_direction": "long", "hema_cross": None,
                        "kalman_transition": None, "hema_fast_value": 1.0, "hema_slow_value": 1.0,
                        "htf_bias_as_of_m15": bias}
    report = _indicator_report(
        {"indicator_ltf": {100: row("long"), 200: row("short")}, "indicator_htf": {0: "long"}},
        {"indicator_ltf": {100: row("long"), 200: row("long")}, "indicator_htf": {10: "long"}},
    )
    bias = report["h4_bias_as_of_common_m15"]
    assert "no H4 timestamp shift" in bias["definition"]
    assert bias["agreement"]["comparable_count"] == 2
    assert bias["agreement"]["agreement_rate"] == .5


def test_h4_replay_excludes_only_boundary_and_dst_variable_duration_buckets() -> None:
    first, cutoff = 1_000_000, 100_000_000

    def aggregate(open_time: int, actual_finalized_at: int | None = None) -> AggregatedBar:
        bar = HistoricalBar("GOLD", Timeframe.HOURS_4, open_time, 1, 2, 0, 1)
        return AggregatedBar(
            bar, 180, True, False,
            bar.finalized_at if actual_finalized_at is None else actual_finalized_at,
        )

    before = aggregate(first - 1)
    good_incomplete = aggregate(first)
    after = aggregate(first + 1, cutoff + 1)
    dst_variable = aggregate(
        first + 2, first + 2 + Timeframe.HOURS_4.duration_ms - 60 * 60 * 1000,
    )
    included, exclusions = _h4_replay_input(
        (before, good_incomplete, after, dst_variable), first_m15_open=first,
        final_m15_cutoff=cutoff,
    )
    assert included == (good_incomplete.bar,)
    assert exclusions == {
        "after_final_m15_cutoff": 1,
        "before_comparison_start": 1,
        "dst_variable_duration_not_representable": 1,
    }


def test_source_manifest_is_explicit_and_refuses_identity_mismatch(tmp_path: Path) -> None:
    audit = {"raw_sha256": "3817558149502888bcee711fb803e6085c5d48cbf36d33901d85ac85c0c0d81a", "row_count": 1_000_000}
    path15, path4 = Path("exports/xauusd_pending/XAUUSD_15m.csv"), Path("exports/xauusd_pending/XAUUSD_4h.csv")
    raw15, raw4 = path15.read_bytes(), path4.read_bytes()
    manifest = _verified_source_manifest(
        xm_path=Path("exports/xm/XM_GOLD_M1_raw.csv"), xm_audit=audit,
        tradingview_15m_path=path15, tradingview_15m_raw=raw15,
        tradingview_4h_path=path4, tradingview_4h_raw=raw4,
    )
    assert manifest["xm"] == {
        "provider": "XM", "server": "XMGlobal-MT5 18", "symbol": "GOLD",
        "path_identity": r"Derivatives\Spot Metals\GOLD", "digits": 2, "point": .01,
        "raw_path_label": "exports/xm/XM_GOLD_M1_raw.csv", "raw_sha256": audit["raw_sha256"],
        "raw_sha256_verified": True, "row_count": 1_000_000,
    }
    with pytest.raises(ValueError, match="15m source identity mismatch"):
        _verified_source_manifest(
            xm_path=tmp_path / "xm.csv", xm_audit=audit,
            tradingview_15m_path=tmp_path / "bad.csv", tradingview_15m_raw=b"bad",
            tradingview_4h_path=path4, tradingview_4h_raw=raw4,
        )


def test_raw_audit_selects_no_pre_march_strategy_rows_and_artifact_is_byte_deterministic(tmp_path: Path) -> None:
    # These are raw server-wall-clock labels: 01:00 converts to the 23:00 UTC
    # comparison boundary in winter.
    before = _row(1_772_413_140_000)
    start = _row(1_772_413_200_000)
    path = tmp_path / "xm.csv"
    path.write_bytes(_csv([before, start]))
    original = path.read_bytes()
    audit, selected = audit_xm_m1(path, comparison_start_ms=1_772_406_000_000, comparison_end_ms=1_772_406_000_000)
    assert audit["comparison_source_selection"]["pre_march_strategy_data_selected"] is False
    assert [item.open_time for item in selected] == [1_772_406_000_000]
    assert path.read_bytes() == original
    gapped = tmp_path / "gapped.csv"
    gapped.write_bytes(_csv([start, _row(start.open_time + 2 * M1_MS)]))
    gap_audit, _ = audit_xm_m1(gapped, comparison_start_ms=COMPARISON_START_MS, comparison_end_ms=COMPARISON_START_MS)
    assert gap_audit["gap_evidence"]["short_irregular_gap_count"] == 1
    assert gap_audit["daily_maintenance_gap_count"] == 0
    with pytest.raises(ValueError, match="pre-March"):
        audit_xm_m1(path, comparison_start_ms=COMPARISON_START_MS - M1_MS, comparison_end_ms=COMPARISON_START_MS)
    payload = {"schema_version": "test", "pre_march_strategy_analysis": {"prohibited": True}}
    assert xm_gold_compatibility_json(payload) == xm_gold_compatibility_json(payload)
    assert xm_gold_compatibility_json(payload).endswith(b"\n")


def test_documented_server_timezone_conversion_handles_winter_summer_and_rejects_dst_wall_time_gaps() -> None:
    winter_raw = int(datetime(2026, 3, 2, 0, 0, tzinfo=UTC).timestamp() * 1000)
    summer_raw = int(datetime(2026, 3, 30, 0, 0, tzinfo=UTC).timestamp() * 1000)
    winter, summer = parse_xm_m1_csv(_csv([_row(winter_raw)]))[0], parse_xm_m1_csv(_csv([_row(summer_raw)]))[0]
    assert winter.open_time == winter_raw - 2 * 60 * 60 * 1000
    assert summer.open_time == summer_raw - 3 * 60 * 60 * 1000
    nonexistent_raw = int(datetime(2026, 3, 29, 3, 30, tzinfo=UTC).timestamp() * 1000)
    ambiguous_raw = int(datetime(2026, 10, 25, 3, 30, tzinfo=UTC).timestamp() * 1000)
    with pytest.raises(ValueError, match="nonexistent"):
        parse_xm_m1_csv(_csv([_row(nonexistent_raw)]))
    with pytest.raises(ValueError, match="ambiguous"):
        parse_xm_m1_csv(_csv([_row(ambiguous_raw)]))
