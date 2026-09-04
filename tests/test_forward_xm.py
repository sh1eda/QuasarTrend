from __future__ import annotations

from pathlib import Path

import pytest

from quasartrend.forward.xm import (
    DemoOnlyExecutionGuard, SignalJournal, TickJournal, config_hash,
    separate_gross_and_net,
)


def test_tick_journal_append_restart_duplicate_order_and_gap(tmp_path: Path) -> None:
    path = tmp_path / "ticks.jsonl"
    environment_id = "e" * 64
    journal = TickJournal(path, source_server="XMGlobal-MT5 18", symbol="GOLD", environment_id=environment_id, capture_version="v1", max_gap_ms=10)
    assert journal.append(time_msc=1_000, bid=1.0, ask=1.1).appended
    assert journal.append(time_msc=1_000, bid=1.0, ask=1.1).duplicate
    assert journal.append(time_msc=1_000, bid=1.01, ask=1.11).appended
    with pytest.raises(ValueError, match="UTC offset"):
        journal.append(time_msc=1_200, time_utc="1970-01-01T03:00:01.200+03:00", bid=1.0, ask=1.1)
    with pytest.raises(ValueError, match="flags"):
        journal.append(time_msc=1_200, bid=1.0, ask=1.1, flags=True)
    assert journal.append(time_msc=1_100, bid=1.0, ask=1.1).gap_ms == 100
    restarted = TickJournal(path, source_server="XMGlobal-MT5 18", symbol="GOLD", environment_id=environment_id, capture_version="v1", max_gap_ms=10)
    assert restarted.provenance_snapshot()["last_time_msc"] == 1_100
    assert restarted.provenance_snapshot()["environment_id"] == environment_id
    with pytest.raises(ValueError, match="regression"):
        restarted.append(time_msc=1_099, bid=1.0, ask=1.1)


def test_signal_journal_idempotency_schema_and_chronology(tmp_path: Path) -> None:
    environment_id = "e" * 64
    journal = SignalJournal(tmp_path / "signals.jsonl", source_server="XMGlobal-MT5 18", symbol="GOLD", environment_id=environment_id)
    row = {"schema_version": journal.schema_version, "source_server": "XMGlobal-MT5 18", "symbol": "GOLD", "environment_id": environment_id, "signal_id": "s1", "strategy_version": "v1", "config_hash": config_hash({"b": 2, "a": 1}), "bar_timestamp": 1, "signal_timestamp": 2, "direction": "long", "theoretical_bid_entry": 100.0, "stop": 99.0, "intended_exit_logic": "hema", "frozen_state": {"bias": "long"}}
    assert journal.append(row)
    assert not journal.append(row)
    changed = dict(row, direction="short")
    with pytest.raises(ValueError, match="conflicting"):
        journal.append(changed)
    with pytest.raises(ValueError, match="schema"):
        journal.append(dict(row, signal_id="s2", pnl=1))
    with pytest.raises(ValueError, match="provenance"):
        journal.append(dict(row, signal_id="s2", environment_id="f" * 64))
    assert config_hash({"a": 1, "b": 2}) == row["config_hash"]


def test_gross_net_is_never_imputed_and_demo_guard_fails_closed() -> None:
    partial = separate_gross_and_net(gross_strategy_r=2.0, observed_spread_r=0.1, observed_commission_r=None, observed_slippage_r=0.1, observed_swap_r=0.0)
    assert partial.net_observed_r is None
    complete = separate_gross_and_net(gross_strategy_r=2.0, observed_spread_r=0.1, observed_commission_r=0.1, observed_slippage_r=0.1, observed_swap_r=0.0)
    assert complete.gross_strategy_r == 2.0 and complete.net_observed_r == pytest.approx(1.7)
    with pytest.raises(PermissionError):
        DemoOnlyExecutionGuard("LIVE").place_order()
    with pytest.raises(RuntimeError):
        DemoOnlyExecutionGuard("DEMO").place_order()
