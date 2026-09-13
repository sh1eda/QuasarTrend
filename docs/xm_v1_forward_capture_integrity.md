# XM V1 forward capture integrity — implementation and acceptance record

Bounded passive capture is now **AUTHORIZED** for the next Windows VDS smoke-test
stage. The final native closure and synchronization/restart evidence resolves the
two authorization blockers; see the
[final acceptance freeze](xm_v1_forward_authorization_blockers.md). This does not
start capture or authorize broker orders. Unexplained absences still block
advancement, and the frozen recovery, chronology, warmup, and writer invariants
remain mandatory.

Baseline: `codex/xm-v1-demo-server9-preflight`,
`6adb55da4da0377f1e443df72c34b7e8b4003904`.
Implementation branch: `codex/xm-v1-forward-capture-integrity`.
Canonical V1 remains `c58e18ef545909184267342eff712dd08bf47dda`.
The agent-infrastructure and original-forward commits remain ancestors.

## Recovery contract

`inputs/observations.jsonl` is the authoritative write-ahead log. Each observation
contains normalized source ticks and finalized native M1/M15/H4 rows, retrieval
bounds/counts/status, the original activation time, the acquisition cutoff and
the completed observation time. A private candidate state is validated first;
no published state, output or checkpoint can precede a complete input frame
being flushed and fsynced. A persistence exception poisons the service instance.
It must close and restart rather than retry using uncertain in-memory progress.

Startup reconstructs the frozen ReplayEngine from genesis and the full canonical
input sequence. It checks the saved checkpoint's *entire* replay/safety state
against the corresponding input sequence number and chain digest. The checkpoint
is an acceleration opportunity for future work, not present authority for
skipping inputs. Current recovery deliberately replays the full history.

Ticks, M1, processed strategy bars, gap transitions, V1 signals and Family-1
shadow records are deterministic projections. Every existing projection must be
an exact prefix of the reconstructed output. All prefixes are validated before
any missing suffix is repaired. Extra outputs, orphan shadows, modified payloads,
and a checkpoint ahead of durable input fail closed. A crash after a signal and
before its shadow is repaired from the original input and observation time.
Catch-up opportunities first observed more than 60 seconds after finalization
remain stale; recovery never reclassifies already committed timely observations.
Slow MT5 calls use their completion time for that classification, separately from
the fixed cutoff used to filter forming bars.

The equivalence invariant is scoped to the **same committed observation stream**.
After a crash before input commitment, re-fetching at a later time can legitimately
produce a different observation; unseen or non-durable broker responses cannot
be reconstructed. Tests re-deliver identical inputs for that boundary.

## History and chronology

The frozen ordering key is `(native open + fixed duration, priority)`, with H4
priority 0 and M15 priority 1. A coincident H4 update changes bias before the M15
decision; an earlier M15 never sees a still-forming H4. Native H4 phase is derived
from the observed source openings, never guessed by flooring UTC to midnight.
A changed/ambiguous grid fails closed instead of retiming frozen bars.

Finalized inputs may arrive in any order or repeat exactly. They are buffered
independently of committed strategy chronology. Conflicting finalized revisions
or a genuinely new candle behind the committed cursor are rejected before the
input log can be contaminated. Any unresolved required M15/H4 slot blocks the
entire new strategy batch. Later inputs remain durable and replayable. When the
missing candle arrives, canonical processing resumes deterministically.

Ticks and lower-timeframe bars can prove activity inside a missing candle; absence
of such evidence does not prove a closure. M1-only gaps are diagnostic: they are
journaled, revisited from their earliest unresolved overlap, and do not block
otherwise complete native strategy inputs. Tick pagination repeats the boundary
millisecond inclusively; an unexhaustible same-millisecond page fails closed.
Non-success or unknown immediate Python history status also blocks ingestion.

Initialization selects the latest **600 finalized candles per strategy timeframe
at the original activation cutoff**, after validation, deduplication and forming
row removal. Requests overfetch 601, 1202, 2404, 4808 and at most 9616 raw rows.
Insufficient history remains a durable blocker; later current bars cannot move
the warmup cutoff or substitute for missing initialization history. Older history
can be obtained on a later request. This bounded transport policy does not change
any V1 indicator parameter or transition.

## Storage integrity and exclusion

Every JSONL frame has a strict canonical binary UTF-8/LF representation, sequence
number, prior-frame digest and SHA-256 digest covering its payload. Even the first
and last newline-terminated records must validate. Exact duplicate deliveries
are idempotent; conflicting IDs or duplicate IDs in stored history fail closed.
Legacy unframed journals are not silently migrated.

Only an unterminated final suffix can be quarantined and truncated. The exact
suffix is first durably published through a temporary file and atomic replacement;
an existing quarantine must match. Interrupted quarantine/truncation recovery is
repeatable. Corruption in a terminated record is never discarded. A complete
trailing-record deletion is detected when a checkpoint or derived output anchors
it; a hash chain alone cannot detect wholesale rollback of *all* evidence and
anchors. This is not an adversarial tamper-proof archive.

A permanent root lock file uses `fcntl.flock` on POSIX and a nonblocking one-byte
`msvcrt.locking` lock on Windows. Its inode is never deleted for stale-PID cleanup.
The OS releases it on process death. Constructor recovery, quarantine, audit,
checkpoint and all journal writes occur under this lifetime lock. Clean close,
constructor failure and abrupt subprocess death are tested. A rejected second
writer changes no evidence bytes.

The tested failure model is process interruption on a functioning filesystem.
File fsync and atomic replacement are used, with POSIX leaf-directory fsync.
Machine power loss, new ancestor-directory durability, network filesystems and
Windows filesystem behavior have not been certified. The Windows implementation
must run the tests there before acceptance; do not infer that POSIX tests prove it.

## Acceptance evidence

| Requirement | Current evidence / outcome |
| --- | --- |
| Crash/checkpoint equivalence | Faults before/during append, before/after fsync, checkpoint half-write/replacement and after checkpoint; exact state, journal bytes, IDs, gap state and checkpoint comparison. |
| Real signal/shadow recovery | Genuine frozen entry, failures at V1 and shadow boundaries, restart clock more than 60 seconds later; outputs remain byte-identical. |
| Gap recovery | Missing M15/H4 with later candles and activity; no advancement; restart and deterministic fill; unexplained closures remain blocked. |
| Cross-timeframe integrity | Delayed H4 changes actual recursive bias correctly; H4-first boundary, reordered/duplicate delivery, forming-H4 invariance, canonical ReplayEngine equality. |
| Warmup | Exact 600; 599 after filtering; multiple invalid forming tails; bounded overfetch; incomplete history later obtained; fixed original cutoff. |
| Writer exclusion | Separate-process rejection, clean release, abrupt process death and unchanged rejected-writer evidence; Windows run outstanding. |
| Journal integrity | First/middle/final terminated corruption, torn tails, recovery interruption, duplicate IDs, orphan output, checkpoint mismatch/ahead. |
| Frozen V1 | All 22 protected production/Pine files match the frozen hash manifest and canonical git bytes; replay/strategy/golden regressions pass. |
| Family-1 | Real long and short V1 entries persist; independent shadow admits long and rejects short; actual armed and immediate entries tested. |
| Holdout | End-to-end synthetic reserved-window capture emits no aggregate economics; universal future reporting-consumer enforcement is a separate gate. |
| Broker permissions | Server-9/server-18 exact policy preserved; audit allowed on the verified fake capability profile; capture false; execution false. No callable order submission introduced. |

Validation commands (local synthetic tests, not a VDS capture command):

Final selected regression result: **240 passed, no skips, in 29.36 seconds**.
This includes all **75 forward tests**, with NumPy-backed MT5 records enabled.
Independent specialist review reproduced the original defects, checked the
repaired paths, and separately ran real signal/recovery and temporal probes.
Lead review retains the blocked authorization decision.

```sh
.venv/bin/python -m pytest tests/test_forward_capture_integrity.py tests/test_forward_mt5.py tests/test_forward_xm.py -q
.venv/bin/python -m pytest tests/test_forward_capture_integrity.py tests/test_forward_mt5.py tests/test_forward_xm.py tests/test_replay.py tests/test_strategy_engine.py tests/test_persistence.py tests/test_checkpoints.py tests/test_batch_incremental.py tests/test_xauusd_golden.py tests/test_tradingview_golden.py -q
```

NumPy was installed only into the local test environment to exercise structured
MT5 records; no production dependency or project configuration was changed.

Changed implementation: `forward/mt5.py`, new `forward/capture.py`, new
`forward/durable.py`, runner cleanup, forward tests and these documents. No V1,
V2, broker allowlist, sizing or order-lifecycle implementation was modified.
The unrelated untracked `golden/`, `tests/test_export_mt5_gold_ticks.py` and
`tools/export_mt5_gold_ticks.py` were not edited or staged.

## Authorization resolution and next gate

The Python wrapper documents UTC timestamps and generic success/error responses,
but does not establish that Python success certifies completion of native history
synchronization. Native `CopyTicksRange` explicitly permits partial results after
history synchronization timeout. Consequently an empty Python tick range, even
with bracketing ticks and generic success, is insufficient here to classify a
required GOLD interval as a proven no-bar session closure. See the primary
[Python tick-range contract](https://www.mql5.com/en/docs/python_metatrader5/mt5copyticksrange_py),
[Python error contract](https://www.mql5.com/en/docs/python_metatrader5/mt5lasterror_py)
and [native range partial-result contract](https://www.mql5.com/en/docs/series/copyticksrange).

The accepted native evidence directly bounds the Sep 4–7 closure and observes
cold synchronization, interruption with no returned rows, restart, and exact
post-restart payload equivalence. It does not turn a weekly schedule into a
universal future closure certificate. Repeated empty responses or an invented
calendar remain insufficient; any unresolved warmup or prospective gap stops the
bounded smoke test. The operational procedure is in
[xm_v1_forward_capture_runbook.md](xm_v1_forward_capture_runbook.md).

Windows CRLF freeze verification is unchanged: the established repo-local
`core.autocrlf=false`, `core.eol=lf` checkout procedure remains necessary. No
platform-independent source-identity change was mixed into this task.
After a successful passive smoke test, broker-order reconciliation and demo
execution lifecycle validation are a separate gate. Demo submission, live
trading, and production sizing remain unauthorized.
