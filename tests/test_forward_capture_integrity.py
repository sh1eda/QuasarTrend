"""Fault and chronology invariants for the production capture implementation.

Synthetic continuous native history deliberately avoids assuming GOLD sessions.
Tests of closure ambiguity assert a blocker, never a guessed no-trade interval.
"""
from __future__ import annotations

from copy import deepcopy
from hashlib import sha256
import json
import math
import os
from pathlib import Path
import subprocess
import sys

import pytest

from test_forward_mt5 import FakeMT5, Record
import quasartrend.forward.mt5 as forward
from quasartrend.forward.capture import CaptureBlocked, CaptureMachine, DURATIONS, historical
from quasartrend.forward.durable import EvidenceLock, JsonlJournal, canonical
from quasartrend.persistence import encode_replay_state
from quasartrend.replay import ReplayEngine

H4, M15 = DURATIONS['h4'], DURATIONS['m15']
START = 600 * H4


def rate(ms: int, price: float = 100.0) -> Record:
    return Record(time=ms // 1000, open=price, high=price + 1, low=price - 1,
                  close=price, tick_volume=1, spread=10, real_volume=1)


class SeriesMT5(FakeMT5):
    def __init__(self, now_ms: int = START, *, wave: bool = False):
        super().__init__()
        self.now_ms = now_ms
        self.reverse = False
        self.omit: dict[int, set[int]] = {1: set(), 15: set(), 240: set()}
        self.all_rates = {
            240: [rate(i * H4, 100 + 10 * math.sin(i / 5) if wave else 100) for i in range(655)],
            15: [rate(i * M15, 100 + 10 * math.sin(i / 80) + 5 * math.sin(i / 7) if wave else 100)
                 for i in range(600 * 16 - 600, 655 * 16)],
            1: [rate(i * 60_000) for i in range(START // 60_000 - 600, START // 60_000 + 55 * 240)],
        }
        self.tick_rows = []

    def visible_rates(self, timeframe):
        return [row for row in self.all_rates[timeframe]
                if row.time * 1000 <= self.now_ms and row.time * 1000 not in self.omit[timeframe]]

    def copy_rates_from_pos(self, symbol, timeframe, start, count):
        self.rate_requests.append(count)
        rows = self.visible_rates(timeframe)
        rows = rows[max(0, len(rows) - start - count):len(rows) - start]
        return list(reversed(rows)) if self.reverse else rows

    def copy_rates_range(self, symbol, timeframe, start, end):
        rows = [row for row in self.visible_rates(timeframe) if start.timestamp() <= row.time <= end.timestamp()]
        return list(reversed(rows)) if self.reverse else rows


@pytest.fixture
def service_factory(monkeypatch, tmp_path):
    monkeypatch.setattr(forward, 'FORWARD_CAPTURE_INTEGRITY_AUTHORIZED', True)
    services = []

    def create(name='run', api=None, **kwargs):
        api = api or SeriesMT5()
        service = forward.XMForwardService(tmp_path / name, mt5=api, now=lambda: api.now_ms / 1000, **kwargs)
        services.append(service)
        return service

    yield create
    for service in services:
        service.close()


def evidence(service):
    return {
        'inputs': service.inputs.path.read_bytes(),
        'projections': {name: journal.path.read_bytes() if journal.path.exists() else b'' for name, journal in service.projections.items()},
        'checkpoint': service.checkpoint.read_bytes(),
        'state': service.machine.snapshot(),
    }


def test_exact_finalized_bootstrap_and_clean_replay(service_factory):
    api = SeriesMT5()
    service = service_factory(api=api)
    processed = service.poll_once()
    assert len(processed) == 1200
    assert len(service.machine.known['m15']) == len(service.machine.known['h4']) == 600
    assert all(request == 601 for request in api.rate_requests)
    expected = ReplayEngine().run(processed)
    assert service.state == expected.state
    before = evidence(service)
    service.close()
    resumed = service_factory(api=api)
    assert evidence(resumed) == before
    assert resumed.poll_once() == ()
    assert resumed.state == expected.state
    assert api.sent == 0


@pytest.mark.parametrize('forming_count', [1, 3, 610])
def test_overfetch_validates_finalized_count_after_forming_tail(service_factory, forming_count):
    api = SeriesMT5()

    def tail(symbol, tf, start, count):
        duration = {1: 60_000, 15: M15, 240: H4}[tf]
        closed = [row for row in api.visible_rates(tf) if row.time * 1000 + duration <= START]
        # Still-forming invalid OHLC cannot count toward initialization.
        forming = [Record(time=(START + i * duration) // 1000, open=float('nan')) for i in range(forming_count)]
        rows = closed + forming
        api.rate_requests.append(count)
        return rows[-count:]

    api.copy_rates_from_pos = tail
    service = service_factory(api=api)
    assert len(service.poll_once()) == 1200
    assert len(service.machine.known['m15']) == len(service.machine.known['h4']) == 600
    if forming_count > 1:
        assert 1202 in api.rate_requests


def test_insufficient_warmup_is_durable_blocker_and_can_obtain_older_history(service_factory):
    api = SeriesMT5()
    hidden = api.all_rates[240].pop(0)
    service = service_factory(api=api)
    with pytest.raises(CaptureBlocked, match='incomplete_finalized_warmup'):
        service.poll_once()
    assert service.state.chronology_cursor is None and service.bars.count == 0
    service.close()
    api.all_rates[240].insert(0, hidden)
    resumed = service_factory(api=api)
    assert resumed.machine.pending_gaps
    assert len(resumed.poll_once()) == 1200
    assert not resumed.machine.pending_gaps


@pytest.mark.parametrize('timeframe,duration', [(15, M15), (240, H4)])
def test_gap_blocks_all_later_bars_until_filled_across_restart(service_factory, timeframe, duration):
    api = SeriesMT5()
    api.tick_rows = [Record(time_msc=START - 1, bid=100., ask=100.1)]
    service = service_factory(api=api)
    service.poll_once()
    prior = service.state
    api.now_ms = START + 2 * H4
    api.omit[timeframe].add(START)
    api.tick_rows = [Record(time_msc=START + 1000, bid=100., ask=100.1)]
    # Cursor must include this activity; initial poll contained no later quote.
    with pytest.raises(CaptureBlocked, match='activity_proven_missing_candle'):
        service.poll_once()
    assert service.state == prior and service.bars.count == 1200
    blocked = evidence(service)
    service.close()
    resumed = service_factory(api=api)
    assert evidence(resumed) == blocked
    api.omit[timeframe].clear()
    processed = resumed.poll_once()
    assert len(processed) == 34
    canonical_bars = [historical(row) for row in resumed.bars.rows]
    assert resumed.state == ReplayEngine().run(canonical_bars).state
    assert not resumed.machine.pending_gaps
    assert resumed.gaps.rows[-1]['status'] == 'resolved'


def test_unexplained_session_gap_is_not_inferred_from_empty_ticks(service_factory):
    api = SeriesMT5()
    api.omit[240].add(300 * H4)
    service = service_factory(api=api)
    with pytest.raises(CaptureBlocked, match='incomplete_finalized_warmup'):
        service.poll_once()
    assert any(gap['reason'] == 'unresolved_candle_or_session_closure' for gap in service.machine.pending_gaps)
    assert service.state.chronology_cursor is None


def test_delayed_h4_coincident_bias_and_live_replay_equivalence(service_factory):
    api = SeriesMT5()
    service = service_factory(api=api)
    service.poll_once()
    api.now_ms += H4 - M15
    service.poll_once()
    earlier = service.state
    assert earlier.latest_htf_bias is None
    api.now_ms += M15
    api.omit[240].add(START)
    # Exact first divergent H4 from baseline audit: all prior HEMA values 100.
    api.all_rates[240][600] = rate(START, 110.)
    with pytest.raises(CaptureBlocked):
        service.poll_once()
    assert service.state == earlier
    api.omit[240].clear()
    api.reverse = True
    processed = service.poll_once()
    assert [bar.timeframe.value for bar in processed] == ['4h', '15m']
    assert service.state.latest_htf_bias.value == 'long'
    assert service.state.chronology_cursor == (START + H4, 1)
    assert service.state == ReplayEngine().run([historical(row) for row in service.bars.rows]).state
    # Duplicate H4 and reverse-ordered retrieval are idempotent.
    assert service.poll_once() == ()


def test_native_h4_phase_is_preserved_and_phase_change_fails_closed(service_factory):
    api = SeriesMT5()
    shift = 2 * 60 * 60_000
    for rows in api.all_rates.values():
        for row in rows:
            row.time += shift // 1000
    api.now_ms += shift
    service = service_factory(api=api)
    service.poll_once()
    assert service.bars.latest(timeframe='h4')['open_time'] % H4 == shift
    api.now_ms += H4 + 3_600_000
    api.all_rates[240][600].time += 3600
    before = service.inputs.count
    with pytest.raises(ValueError, match='grid changed'):
        service.poll_once()
    assert service.inputs.count == before


def test_conflicting_rate_rejected_before_wal_and_retry_does_not_lose_state(service_factory):
    api = SeriesMT5()
    service = service_factory(api=api)
    service.poll_once()
    before = evidence(service)
    row = api.all_rates[15][599]
    old = row.close
    row.close += .5
    with pytest.raises(ValueError, match='conflicting duplicate'):
        service.poll_once()
    assert evidence(service) == before
    row.close = old
    assert service.poll_once() == ()


class Crash(BaseException):
    pass


@pytest.mark.parametrize('stream,point', [
    ('inputs', 'before_append'), ('inputs', 'during_append'),
    ('inputs', 'after_append_before_fsync'), ('inputs', 'after_fsync'),
    ('bars', 'before_append'), ('bars', 'during_append'),
    ('bars', 'after_append_before_fsync'), ('bars', 'after_fsync'),
    ('checkpoint', 'before_checkpoint_write'), ('checkpoint', 'during_checkpoint_write'),
    ('checkpoint', 'before_checkpoint_replace'), ('checkpoint', 'after_checkpoint_replace'),
    ('checkpoint', 'after_checkpoint_before_next_event'),
])
def test_crash_restart_matches_uninterrupted_at_every_persistence_boundary(service_factory, stream, point):
    baseline_api = SeriesMT5()
    baseline = service_factory('baseline', baseline_api)
    baseline.poll_once()
    baseline_api.now_ms += H4
    baseline.poll_once()
    expected = evidence(baseline)
    api = SeriesMT5()
    service = service_factory('crashed', api)
    service.poll_once()
    api.now_ms += H4
    triggered = []

    def fault(actual, path):
        target = 'checkpoints' if stream == 'checkpoint' else stream
        if actual == point and target in path.parts and not triggered:
            triggered.append(True)
            raise Crash()

    service.fault = fault
    for journal in service.projections.values():
        journal.fault = fault
    service.inputs.fault = fault
    with pytest.raises(Crash):
        service.poll_once()
    assert triggered
    with pytest.raises(RuntimeError, match='requires restart'):
        service.poll_once()
    service.close()
    resumed = service_factory('crashed', api)
    if resumed.inputs.count == 1:
        resumed.poll_once()
    assert evidence(resumed) == expected


def make_journal(path, **kwargs):
    return JsonlJournal(path, schema='test/v1', provenance={'source': 'fixture'}, time_field='time', id_field='id', **kwargs)


@pytest.mark.parametrize('tail', [b'{', b'{"format":"xm-capture-frame/v1"}', b'\xff\x00'])
def test_unterminated_tail_is_quarantined_exactly_and_recovery_is_repeatable(tmp_path, tail):
    path = tmp_path / 'journal.jsonl'
    journal = make_journal(path)
    journal.append({'id': 'one', 'time': 1, 'price': 100})
    good = path.read_bytes()
    path.write_bytes(good + tail)
    recovered = make_journal(path)
    assert recovered.count == 1 and path.read_bytes() == good
    assert path.with_name(path.name + '.tail-' + sha256(tail).hexdigest()).read_bytes() == tail
    assert make_journal(path).rows == recovered.rows


@pytest.mark.parametrize('row_index', [0, 1, 2])
def test_any_terminated_corrupt_record_fails_closed_including_first_and_final(tmp_path, row_index):
    path = tmp_path / 'journal.jsonl'
    journal = make_journal(path)
    for i in range(3):
        journal.append({'id': str(i), 'time': i, 'price': 100})
    lines = path.read_bytes().splitlines(keepends=True)
    lines[row_index] = lines[row_index].replace(b'"price":100', b'"price":101')
    path.write_bytes(b''.join(lines))
    before = path.read_bytes()
    with pytest.raises(ValueError, match='committed journal row'):
        make_journal(path)
    assert path.read_bytes() == before


def test_duplicate_ids_and_noncanonical_framing_fail_closed(tmp_path):
    path = tmp_path / 'journal.jsonl'
    journal = make_journal(path)
    assert journal.append({'id': 'one', 'time': 1})
    assert not journal.append({'id': 'one', 'time': 1})
    with pytest.raises(ValueError, match='conflicting duplicate'):
        journal.append({'id': 'one', 'time': 2})
    frame = json.loads(path.read_bytes())
    frame.update(sequence=2, previous=journal.tip)
    frame['digest'] = sha256(canonical({k: v for k, v in frame.items() if k != 'digest'}).encode()).hexdigest()
    path.write_bytes(path.read_bytes() + (canonical(frame) + '\n').encode())
    with pytest.raises(ValueError, match='duplicate journal identity'):
        make_journal(path)


@pytest.mark.parametrize('tamper', ['state', 'ahead', 'output', 'orphan_shadow', 'empty_inputs'])
def test_recovery_rejects_validly_framed_but_unreconciled_evidence(service_factory, tamper):
    service = service_factory()
    service.poll_once()
    if tamper == 'state':
        saved = json.loads(service.checkpoint.read_bytes())
        saved['snapshot']['missed_stale_signals'] = 1
        service.checkpoint.write_text(canonical(saved) + '\n')
    elif tamper == 'ahead':
        service.inputs.path.write_bytes(b'')
    elif tamper == 'empty_inputs':
        service.inputs.path.write_bytes(b'')
        service.checkpoint.unlink()
    elif tamper == 'output':
        row = dict(service.bars.rows[-1])
        row['bar_id'] += ':orphan'
        service.bars.append(row)
    else:
        service.shadow.journal.append({'shadow_id': 'orphan', 'decision_timestamp': START})
    service.close()
    with pytest.raises(ValueError, match='checkpoint|prefix'):
        service_factory()


def locked_root_snapshot(root):
    # Windows locks byte [0, 1) mandatorily. Read every evidence file while
    # held; compare the permanent lock inode/size now and its bytes after release.
    lock_path = root / '.xm-forward.lock'
    stat = lock_path.stat()
    return {
        'lock_identity': (stat.st_dev, stat.st_ino, stat.st_size),
        'evidence': {path.relative_to(root): path.read_bytes()
                     for path in root.rglob('*') if path.is_file() and path != lock_path},
    }


@pytest.mark.parametrize('lock_bytes', [b'', b'lock-sentinel'], ids=['empty-lock', 'nonempty-lock'])
def test_root_single_writer_release_crash_and_rejected_writer_does_not_mutate(tmp_path, lock_bytes):
    root = tmp_path / 'root'
    root.mkdir()
    lock_path = root / '.xm-forward.lock'
    lock_path.write_bytes(lock_bytes)
    assert lock_path.read_bytes() == lock_bytes
    lock = EvidenceLock(root)
    (root / 'evidence').write_bytes(b'preserve')
    before = locked_root_snapshot(root)
    code = 'from pathlib import Path; from quasartrend.forward.durable import EvidenceLock; lock = EvidenceLock(Path(__import__("sys").argv[1]))'
    denied = subprocess.run([sys.executable, '-c', code, str(root)], capture_output=True, timeout=10)
    assert denied.returncode != 0 and b'already has a writer' in denied.stderr
    assert locked_root_snapshot(root) == before
    lock.close()
    assert lock_path.read_bytes() == lock_bytes
    holder = subprocess.Popen([sys.executable, '-c', code + '; print(__import__("os").getpid(), flush=True); input()', str(root)], stdout=subprocess.PIPE, stdin=subprocess.PIPE, text=True)
    owner_handle = None
    try:
        owner_pid = int(holder.stdout.readline().strip())
        assert owner_pid > 0 and holder.poll() is None
        if os.name == 'nt':
            import ctypes
            from ctypes import wintypes
            kernel = ctypes.WinDLL('kernel32', use_last_error=True)
            kernel.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
            kernel.OpenProcess.restype = wintypes.HANDLE
            kernel.WaitForSingleObject.argtypes = [wintypes.HANDLE, wintypes.DWORD]
            kernel.WaitForSingleObject.restype = wintypes.DWORD
            kernel.TerminateProcess.argtypes = [wintypes.HANDLE, wintypes.UINT]
            kernel.TerminateProcess.restype = wintypes.BOOL
            kernel.CloseHandle.argtypes = [wintypes.HANDLE]
            kernel.CloseHandle.restype = wintypes.BOOL
            # The venv launcher can exit before the interpreter holding the lock.
            # Retain the actual owner's process handle before killing the launcher.
            owner_handle = kernel.OpenProcess(0x00100000 | 0x0001, False, owner_pid)
            assert owner_handle
            assert kernel.WaitForSingleObject(owner_handle, 0) == 258  # still running
        else:
            assert owner_pid == holder.pid
        with pytest.raises(PermissionError, match='writer'):
            EvidenceLock(root)
        holder.kill()
        holder.wait(timeout=10)
        if owner_handle:
            assert kernel.WaitForSingleObject(owner_handle, 10_000) == 0
        else:
            assert holder.poll() is not None
        # The actual owner is proven dead before the single recovery attempt.
        recovered = EvidenceLock(root)
        recovered.close()
        assert locked_root_snapshot(root) == before
        assert lock_path.read_bytes() == lock_bytes
    finally:
        try:
            if owner_handle:
                if kernel.WaitForSingleObject(owner_handle, 0) == 258:
                    assert kernel.TerminateProcess(owner_handle, 1)
                    assert kernel.WaitForSingleObject(owner_handle, 10_000) == 0
        finally:
            if owner_handle:
                kernel.CloseHandle(owner_handle)
            if holder.poll() is None:
                holder.kill()
                holder.wait(timeout=10)
            holder.stdin.close()
            holder.stdout.close()


def test_constructor_failure_releases_lock_and_audit_does_not_activate(service_factory):
    audit = service_factory(activate=False)
    assert audit.activation_ms is None and audit.inputs.count == 0
    with pytest.raises(PermissionError, match='audit-only'):
        audit.poll_once()
    audit.close()
    capture = service_factory()
    capture.poll_once()
    capture.checkpoint.write_text('{}')
    capture.close()
    for _ in range(2):
        with pytest.raises(ValueError, match='checkpoint'):
            service_factory()


def test_tick_page_boundary_is_inclusive_and_saturation_fails_closed(service_factory):
    api = SeriesMT5()
    now = START
    api.tick_rows = [Record(time_msc=now - 2000, bid=100., ask=100.1, flags=i) for i in range(9999)]
    api.tick_rows += [Record(time_msc=now - 1000, bid=100., ask=100.1, flags=i) for i in range(2)]
    api.tick_rows += [Record(time_msc=now, bid=100., ask=100.1, flags=0)]
    service = service_factory(api=api)
    rows, request = service._tick_rows(now)
    assert len(rows) == 10002
    assert len([row for row in rows if row['time_msc'] == now - 1000]) == 2
    assert request['pages'][1]['cursor_ms'] == now - 1000
    api.tick_rows = [Record(time_msc=now, bid=100., ask=100.1, flags=i) for i in range(10001)]
    with pytest.raises(RuntimeError, match='saturated'):
        service.poll_once()
    assert service.inputs.count == 0


@pytest.mark.parametrize('stream,point', [
    ('v1.jsonl', 'before_append'), ('v1.jsonl', 'during_append'),
    ('v1.jsonl', 'after_append_before_fsync'), ('v1.jsonl', 'after_fsync'),
    ('family1_long_only.jsonl', 'before_append'), ('family1_long_only.jsonl', 'during_append'),
    ('family1_long_only.jsonl', 'after_fsync'),
])
def test_real_signal_and_shadow_recover_exactly_after_crash_and_later_restart_clock(service_factory, stream, point):
    def setup(name):
        api = SeriesMT5(wave=True)
        service = service_factory(name, api)
        service.poll_once()
        api.now_ms = START + 33 * M15
        service.poll_once()
        assert service.signals.count == 0
        api.now_ms += M15
        return service, api

    baseline, _ = setup('baseline')
    baseline.poll_once()
    assert baseline.signals.count == baseline.shadow.journal.count == 1
    expected = evidence(baseline)
    service, api = setup('crashed')

    def fault(actual, path):
        if path.name == stream and actual == point:
            raise Crash()

    service.signals.fault = service.shadow.journal.fault = fault
    with pytest.raises(Crash):
        service.poll_once()
    service.close()
    api.now_ms += 120_000  # Recovery must not reclassify a durable fresh signal.
    resumed = service_factory('crashed', api)
    assert evidence(resumed) == expected
    assert resumed.signals.rows[0]['signal_timestamp'] == START + 34 * M15


def test_real_long_short_events_match_frozen_intermediate_states_and_shadow(service_factory):
    api = SeriesMT5(wave=True)
    service = service_factory(api=api)
    service.poll_once()
    for step in (34, 276):
        api.now_ms = START + step * M15
        service.poll_once()
    signals = service.signals.rows
    assert [signal['direction'] for signal in signals] == ['long', 'short']
    assert [row['decision'] for row in service.shadow.journal.rows] == ['admit', 'reject']
    engine = ReplayEngine()
    state = engine.initial_state('GOLD')
    by_timestamp = {signal['finalized_m15']['finalized_at']: signal for signal in signals}
    latest_h4 = None
    checked = 0
    for row in service.bars.rows:
        bar = historical(row)
        result = engine.step(state, bar)
        state = result.state
        if bar.timeframe.value == '4h':
            latest_h4 = bar
        elif bar.finalized_at in by_timestamp:
            signal = by_timestamp[bar.finalized_at]
            trade = state.strategy_state.trade
            assert any(event.type.value == 'trade_opened' for event in result.trace.events)
            assert signal['signal_id'] == sha256(f'v1:{bar.processing_key}:{trade.trade_id}'.encode()).hexdigest()
            assert signal['state_hash'] == sha256(canonical(json.loads(encode_replay_state(state, expected_config=engine.config))).encode()).hexdigest()
            assert signal['direction'] == trade.side.value
            assert signal['theoretical_entry'] == trade.entry_price
            assert signal['intended_initial_stop'] == trade.stop_price
            assert signal['armed_or_immediate'] == ('immediate' if trade.setup_origin_timestamp == bar.finalized_at else 'armed')
            assert signal['finalized_h4_bias']['finalized_at'] == latest_h4.finalized_at <= bar.finalized_at
            checked += 1
    assert checked == 2 and service.state == state
    assert service.machine.missed_stale_signals > 0
    for shadow, signal in zip(service.shadow.journal.rows, signals):
        assert shadow['source_signal_id'] == signal['signal_id']
        assert shadow['context']['state_hash'] == signal['state_hash']
    assert api.sent == 0


def test_slow_acquisition_uses_completed_observation_time_for_stale_signals(service_factory):
    api = SeriesMT5(wave=True)
    service = service_factory(api=api)
    service.poll_once()
    api.now_ms = START + 33 * M15
    service.poll_once()
    api.now_ms += M15
    original = api.copy_rates_range

    def slow(symbol, tf, start, end):
        result = original(symbol, tf, start, end)
        if tf == 240:
            api.now_ms += 61_000
        return result

    api.copy_rates_range = slow
    service.poll_once()
    assert service.signals.count == 0 and service.machine.missed_stale_signals == 1
    observation = service.inputs.rows[-1]
    assert observation['observed_at'] - observation['cutoff_ms'] == 61_000


def test_m1_gap_is_reported_and_revisited_without_blocking_complete_strategy(service_factory):
    api = SeriesMT5()
    missing = START - 120_000
    api.omit[1].add(missing)
    api.tick_rows = [Record(time_msc=START - 119_000, bid=100., ask=100.1)]
    service = service_factory(api=api)
    # First tick request is only the previous minute; M1 source gap alone still
    # warrants a diagnostic and revisit, independent of strategy advancement.
    service.poll_once()
    assert service.machine.initialized and service.machine.m1_gaps
    assert service.machine.pending_gaps == []
    prior = service.state
    api.omit[1].clear()
    assert service.poll_once() == ()
    assert not service.machine.m1_gaps and service.state == prior
    assert service.m1.contains(f'm1:{missing}')


def test_warmup_cutoff_does_not_move_when_new_bars_arrive(service_factory):
    api = SeriesMT5()
    oldest = api.all_rates[240].pop(0)
    service = service_factory(api=api)
    with pytest.raises(CaptureBlocked):
        service.poll_once()
    api.now_ms += H4
    with pytest.raises(CaptureBlocked, match='incomplete_finalized_warmup'):
        service.poll_once()
    assert service.state.chronology_cursor is None
    api.all_rates[240].insert(0, oldest)
    service.poll_once()
    assert len([row for row in service.machine.known['h4'].values() if row['finalized_at'] <= START]) == 600
    assert service.state == ReplayEngine().run([historical(row) for row in service.bars.rows]).state


@pytest.mark.parametrize('point', ['during_quarantine_write', 'after_quarantine_fsync', 'after_tail_truncate'])
def test_crash_during_tail_recovery_preserves_forensics_and_converges(tmp_path, point):
    path = tmp_path / 'journal.jsonl'
    journal = make_journal(path)
    journal.append({'id': 'one', 'time': 1})
    good, tail = path.read_bytes(), b'{"unfinished":true'
    path.write_bytes(good + tail)

    def fault(actual, _):
        if actual == point:
            raise Crash()

    with pytest.raises(Crash):
        make_journal(path, fault=fault)
    recovered = make_journal(path)
    assert recovered.count == 1 and path.read_bytes() == good
    assert path.with_name(path.name + '.tail-' + sha256(tail).hexdigest()).read_bytes() == tail


def test_mismatched_existing_quarantine_never_discards_source_tail(tmp_path):
    path = tmp_path / 'journal.jsonl'
    journal = make_journal(path)
    journal.append({'id': 'one', 'time': 1})
    tail = b'{"unfinished":true'
    path.write_bytes(path.read_bytes() + tail)
    path.with_name(path.name + '.tail-' + sha256(tail).hexdigest()).write_bytes(tail[:4])
    before = path.read_bytes()
    with pytest.raises(ValueError, match='quarantine evidence differs'):
        make_journal(path)
    assert path.read_bytes() == before


def test_history_error_with_nonempty_partial_payload_cannot_commit(service_factory):
    api = SeriesMT5()
    api.last_error = lambda: (-10005, 'IPC timeout')
    service = service_factory(api=api)
    with pytest.raises(RuntimeError, match='history result/status'):
        service.poll_once()
    assert service.inputs.count == service.bars.count == 0


def test_genuine_immediate_entry_serialization_uses_frozen_transitions(service_factory):
    api = SeriesMT5()
    api.all_rates[240] = [rate(i * H4, 90.0 if i < 599 else 100.0) for i in range(655)]
    api.all_rates[15][600] = rate(START, 130.0)
    service = service_factory(api=api)
    service.poll_once()
    prior = service.state
    assert prior.strategy_state.trade is None
    assert prior.strategy_state.pending_direction is None
    assert prior.latest_htf_bias.value == 'long'
    api.now_ms += M15
    processed = service.poll_once()
    independent = ReplayEngine().step(prior, processed[0])
    assert independent.state == service.state
    assert independent.trace.strategy_bar.hema_flip.value == 'long'
    assert independent.trace.strategy_bar.kalman_transition.value == 'long'
    signal = service.signals.rows[0]
    assert signal['armed_or_immediate'] == 'immediate'
    assert signal['setup_origin_timestamp'] == START + M15
    assert signal['signal_id'] == '3e20aba0d5c50f9504629426293d9de70d8733afef423befb2593d0d91e79455'
    assert signal['intended_initial_stop'] == independent.state.strategy_state.trade.stop_price
    assert service.shadow.journal.rows[0]['decision'] == 'admit'


def test_forming_h4_ohlc_cannot_affect_earlier_m15_state_or_evidence(service_factory):
    services = []
    for name, future_price in [('first', 101.0), ('second', 900.0)]:
        api = SeriesMT5()
        api.all_rates[240][600] = rate(START, future_price)
        service = service_factory(name, api)
        service.poll_once()
        api.now_ms += H4 - M15
        service.poll_once()
        services.append(service)
    assert services[0].state == services[1].state
    assert evidence(services[0]) == evidence(services[1])


def test_capture_output_has_no_reserved_window_aggregate_economics(service_factory):
    api = SeriesMT5(wave=True)
    shift = forward.HOLDOUT_START_MS
    for rows in api.all_rates.values():
        for row in rows:
            row.time += shift // 1000
    api.now_ms += shift
    service = service_factory(api=api)
    service.poll_once()
    api.now_ms += 34 * M15
    service.poll_once()
    assert service.signals.count == 1
    forbidden = {'pnl', 'r', 'gross_strategy_r', 'net_observed_r', 'profit_factor', 'win_rate', 'total_return', 'realized'}

    def inspect(value):
        if isinstance(value, dict):
            assert not forbidden.intersection(value)
            for item in value.values():
                inspect(item)
        elif isinstance(value, (list, tuple)):
            for item in value:
                inspect(item)

    inspect(service.health())
    inspect(service.machine.snapshot())
    for journal in service.projections.values():
        inspect(journal.rows)


@pytest.mark.parametrize('lock_bytes', [b'', b'lock-sentinel'], ids=['empty-lock', 'nonempty-lock'])
def test_rejected_second_service_does_not_mutate_evidence(service_factory, tmp_path, lock_bytes):
    root = tmp_path / 'run'
    root.mkdir()
    lock_path = root / '.xm-forward.lock'
    lock_path.write_bytes(lock_bytes)
    assert lock_path.read_bytes() == lock_bytes
    service = service_factory()
    service.poll_once()
    before = locked_root_snapshot(service.root)
    with pytest.raises(PermissionError, match='already has a writer'):
        service_factory()
    after = locked_root_snapshot(service.root)
    assert after == before
    service.close()
    assert lock_path.read_bytes() == lock_bytes
