# XM V1 forward capture authorization — final acceptance freeze

`XM V1 FORWARD CAPTURE AUTHORIZATION: PASS`

This PASS authorizes only the next bounded passive forward-capture activation
stage on the validated XM demo environment. It does not start capture and does
not assert that the smoke test will pass or that capture is operationally ready.
It does not authorize demo or live orders, order modification/cancellation, execution,
or production/live sizing. Audit is allowed, bounded passive capture is allowed,
and execution remains hard-disabled. No broker order was created during this
authorization work.

## Repository identity and scope

| Field | Evidence |
| --- | --- |
| Operational pre-task baseline | `661e49be81469c86d5df8411ec86992a971d1a17` |
| Prior authorization evidence checkpoint | `5e14e60bc6c2a6f5b9713b1d3aac4d013f230bf5` |
| Local branch | `codex/xm-v1-forward-capture-integrity` |
| Resulting commit | The commit containing this report records the accepted work; the final task response records its resulting hash. No delegate commit was made. |
| Canonical frozen V1 | `c58e18ef545909184267342eff712dd08bf47dda` |
| Previous server-9 preflight | `6adb55da4da0377f1e443df72c34b7e8b4003904` |
| Previous-phase reference | `docs/xm_v1_forward_capture_integrity.md` |

No frozen V1 strategy, indicator, Pine, sizing, or order-lifecycle behavior is
changed by this freeze. The passive-capture authorization gate is true, while
the production CLI remains audit-only by default and requires an explicit finite
poll bound for capture. `execution_allowed` remains false and the runtime has no
`order_send` path.

Files added by this workstream:

- `tests/test_forward_authorization_blockers.py`
- `tests/test_forward_validation_evidence.py`
- `tests/test_forward_history_probe.py`
- `tools/validate_xm_forward_recovery.py`
- `tools/probe_xm_forward_history.py`
- `docs/xm_v1_forward_authorization_blockers.md`
- `docs/evidence/xm_v1_forward_authorization/local.json`
- Returned Windows evidence, import receipts and reviewed diagnostic source
  snapshots under `docs/evidence/xm_v1_forward_authorization/`

Existing unrelated untracked files are outside this change and must not be staged.

Test-only portability changes also update `tests/test_forward_capture_integrity.py`
and `tests/test_persistence.py`. The writer tests compare evidence bytes while
holding the lock and verify the lock file's bytes after release, avoiding a read
of Windows' mandatorily locked byte. Empty and nonempty lock files are both
covered. The SQLite test explicitly closes its connection before removing the
database. On Windows, the crash-release test also opens and waits on the actual
lock owner’s process handle, because the Python virtual-environment launcher can
exit before its child releases the lock. The correction is supported by the
controlled observations below. Production persistence and lock behavior are unchanged.

## Final evidence accepted on 2026-09-13

The accepted target identity is the real Windows VDS and official MT5 Python
environment at `XM Global Limited`, `XMGlobal-MT5 9`, terminal build 6182,
`GOLD`, DEMO. Raw account/login identifiers are neither recorded nor exposed.
The durable allowlist remains `XMGlobal-MT5 9` or `XMGlobal-MT5 18`; this
acceptance observation was on server 9.

### Blocker 1 — GOLD session-closure completeness: resolved

A read-only MQL5 script using `SymbolInfoSessionQuote` and
`SymbolInfoSessionTrade` compiled with 0 errors and 0 warnings. It observed
quote sessions Monday–Thursday 01:00–23:59, Friday 01:00–23:58, and none on
Saturday/Sunday; trade sessions Monday–Friday 01:02–23:58 and none on
Saturday/Sunday. At the probe, `TimeTradeServer - TimeGMT = +3h`; `TimeCurrent`
was stale during the weekend while server time continued.

Independent broker-native history directly bounded the Sep 4–7 target gap:

- M15 bars continued through Friday 2026-09-04 23:45 UTC; the first Monday M15
  bar was 2026-09-07 01:00 UTC.
- The last pre-gap tick was `2026-09-04T23:57:59.841000+00:00` and the first
  post-gap tick was `2026-09-07T01:00:02.019000+00:00`.
- The largest observed tick gap was 176,522.178 seconds (about 49h 02m
  02.178s), matching the native Friday 23:58 to Monday 01:00 quote closure.

This is direct target-period evidence, not an inference from a generic current
schedule. The lack of an archived holiday-exception document is therefore not
an absolute blocker for this specific gap. This certificate must not be
generalized to arbitrary future gaps: every future unexplained absence still
fails closed unless independently explained, and contradictory activity
invalidates a closure claim.

### Blocker 2 — native MT5 synchronization/recovery: resolved

On isolated portable MT5 instances, a clean GOLD history cache grew from about
15 KB to about 2.49 MB during a cold request. The bounded 2026-08-15 through
2026-09-11 request returned:

| Stream | Cold result | SHA-256 | Post-restart result |
| --- | --- | --- | --- |
| M15 | ~129s; 1,830 rows; 2026-08-17 01:00 through 2026-09-11 23:45 UTC | `95c3a36e72d10c7086697d584e2f6f729516dbd46c3d1f9f9789f34825b72f8e` | 0.004s; 1,830 rows; identical hash |
| H4 | 120 rows; 2026-08-17 00:00 through 2026-09-11 20:00 UTC | `3bd448459f5dd7fce1a87215c98d6f73f8ad1aba4bebd6324c5602fd5e57b569` | 0.007s; 120 rows; identical hash |

Subsequent pre-restart requests were immediate and byte-normalized to the same
row counts/hashes. In a separate disposable instance, terminating the terminal
while a native request remained active after five seconds returned
`LAST_ERROR = (-10002, 'IPC recv failed')`, `ROWS = NONE`, and `SHA256 = NONE`.
No partial-row payload was observed; the induced failure returned no rows and
failed closed. Restart then reproduced the deterministic canonical payload.
Existing synthetic recovery tests establish that non-success/incomplete payloads
cannot advance canonical state. The attempted 2018 probe was below the available
broker-history floor and is not authorization evidence.

## Historical pre-acceptance investigation record

The sections below preserve the evidence and limitations recorded before the
two final native observations were accepted. Where their disposition says
BLOCKED or UNRESOLVED, the final acceptance evidence above and the authorization
decision at the end of this document supersede that prior gate status; the
underlying conservative runtime behavior remains unchanged.

## Delegated work and evidence ownership

| Workstream | Responsibility and evidence |
| --- | --- |
| Broker history/session investigation | Investigate positive session evidence and distinguish source/API success from completeness; no accepted closure certificate exists |
| Windows recovery investigation | Inspect target platform and source identity; lead completed bounded validation in an isolated accepted-baseline checkout |
| Adversarial validation | Inspect capture/MT5/durable code and baseline tests; add synthetic history/recovery regressions and actual subprocess-loss cases |
| Independent acceptance review | Independently reproduced empty ticks with advancing complete rates; final combined independent review is complete |

Delegate implementation results are supporting evidence, not gate acceptance.
The lead owns source/diff review, final test integration and authorization.

## GOLD session-classification protocol and unresolved evidence

OBSERVED in source and synthetic tests: the implementation has no positive
session-closure certificate or accepted session calendar. Missing required
M15/H4 slots are either `activity_proven_missing_candle` when ticks/lower-timeframe
bars demonstrate activity, or `unresolved_candle_or_session_closure` otherwise.
Both remain blocked. Insufficient finalized warmup also remains blocked.

The requested external classification `UNRESOLVED_HISTORY_GAP` therefore maps
to these unresolved conditions; this report does not introduce a new persisted
schema or claim that the existing reason strings are closure certificates.
`LEGITIMATE_SESSION_CLOSURE` has no implemented acceptance path.

A defensible future certificate would need broker/server/GOLD-specific positive
evidence covering the exact missing interval; validity dates and exceptions;
explicit server/UTC and daylight-saving semantics; and a proven distinction
between trading-session closure, quote absence, unavailable history and delayed
synchronization. A gap spanning closure and activity must be partitioned without
silently accepting its active portion. Contradictory activity invalidates a claim
of complete inactivity. Generic XAU hours, surrounding bars, repeated empty
responses and successful API status are insufficient by themselves.

UNRESOLVED: no such complete certificate has been established for the required
XMGlobal-MT5 9 GOLD history. Daily/weekend-shaped tests use synthetic durations;
they demonstrate conservative rejection, not observation of legitimate XM
closures. This blocker cannot be marked resolved from these tests.

### Primary-source constraints

Official MetaQuotes documentation inspected on 2026-09-09 describes an MQL5
`CopyRates` request returning the portion ready when its wait expires, while
download continues and a later request can return more. A nonempty response is
therefore insufficient to prove history completeness.
[MQL5 CopyRates](https://www.mql5.com/en/docs/series/copyrates).

The MQL5 `CopyTicksRange` documentation explicitly allows partial tick delivery
with synchronization timeout or insufficient buffer. These MQL5 contracts justify
testing partial-return failure modes; they do not establish the exact error-code
mapping or observed behavior of this Windows Python integration.
[MQL5 CopyTicksRange](https://www.mql5.com/en/docs/series/copyticksrange).

The official session functions distinguish quoting sessions from trading sessions.
Both describe symbol/day-of-week intervals, with endpoints expressed as seconds
from midnight and the date component ignored. Inference: trading-session absence
alone cannot certify missing quote history, and these weekly endpoints still need
broker-specific dates, exceptions and time conversion evidence before historical
closure classification.
[SymbolInfoSessionQuote](https://www.mql5.com/en/docs/marketinformation/symbolinfosessionquote),
[SymbolInfoSessionTrade](https://www.mql5.com/en/docs/marketinformation/symbolinfosessiontrade).

`TimeCurrent` reflects the most recent quote's server time; outside `OnTick` that
quote may be for another Market Watch symbol. It is not an independently advancing
GOLD synchronization clock. Inference: its apparent staleness or agreement with a
local clock cannot by itself prove closure or an exhaustive history response.
[TimeCurrent](https://www.mql5.com/en/docs/dateandtime/timecurrent).

Python range requests use UTC timestamps; rates are selected by bar opening
time. Those transport bounds do not certify that each returned bar has finalized.
The probe's raw returned-rate counts must therefore remain separate from the
capture machine's finalized-candle evidence.
[Python copy_rates_range](https://www.mql5.com/en/docs/python_metatrader5/mt5copyratesrange_py),
[Python copy_ticks_range](https://www.mql5.com/en/docs/python_metatrader5/mt5copyticksrange_py).

## History synchronization and tick absence

OBSERVED in code and tests: every history request checks its immediate Python
status. A timeout with a nonempty payload or a missing payload rejects the entire
acquisition before canonical input commitment. Previously successful streams do
not partially advance recursive state. Successful but incomplete warmup is
recorded as blocked and later retries retain the original activation cutoff.
Missing native candles remain blocked across restart and resume deterministically
when supplied. Failed reconnect and account identity change preserve prior
canonical evidence.

OBSERVED in the missing-ticks regression: successful empty ticks with complete
native rates allow internal replay to initialize with 1,200 bars and then advance
one M15 bar. There are zero persisted ticks, zero gap events and no pending
strategy gap; health reports `stale_quote=true`. This is existing diagnostic-only
behavior behind the globally disabled capture gate. It proves neither exhaustive
tick retrieval nor legitimate session closure. The lead’s scope decision is that tick absence alone is reporting-only when
required strategy rates are complete. Tick-dependent cost or quote reporting
must retain its missing-evidence status. Missing required M15/H4 history remains
a strategy blocker; complete rates are not presented as complete ticks.

UNRESOLVED: synthetic errors do not establish the behavior of the actual MT5
terminal during synchronization, restart, server delay or success-with-partial
responses. A clean reconnect is not a history-completeness certificate.

## Adversarial cases and recovery experiments

| Case | Evidence and scope |
| --- | --- |
| Ordinary continuous history | Existing exact finalized bootstrap/replay tests; synthetic |
| Daily/weekend-shaped absence | New one-H4 and twelve-H4 all-stream gaps remain blocked; no calendar inferred |
| Mixed closure-shaped absence/activity | New tick inside one part of missing span yields both activity-proven and unresolved reasons; entire strategy batch remains blocked |
| Missing M15 or H4 | Existing gap/restart tests plus new partial-warmup cases preserve cursor and deterministic fill |
| Missing ticks with complete rates | New characterization records diagnostic-only behavior, not closure or tick completeness |
| Partial or missing history | Eight per-stream payload/status tests prove no canonical mutation before retry/restart |
| Synchronization timeout/restart | Synthetic per-stream error then service restart and successful fill; actual terminal behavior not yet validated |
| Disconnect/reconnect | New failed reconnect and changed account identity block before canonical mutation |
| Delayed history | New partial warmup and gap-fill cases reconstruct exact ReplayEngine state |
| Duplicate/overlapping retries | Projection counts and strategy state stay unchanged after repeated successful retry |
| Abrupt process loss | Five separate subprocesses invoke `os._exit(73)` at input partial append, input fsync, bars fsync, checkpoint before replacement and checkpoint after replacement |
| Clean/repeated recovery | Each abrupt-loss case compares exact input/projection/checkpoint bytes and state with uninterrupted processing, then repeats restart |
| Persistence failure, torn tail, checkpoint disagreement | Preserved baseline injected-failure, quarantine, corruption and checkpoint/prefix tests |
| Coincident H4/M15 and forming H4 | Preserved baseline chronology and future-isolation tests |
| Writer exclusion | Preserved baseline second-process exclusion and lock release after actual process death |

Abrupt-loss subprocesses do not unwind exceptions or gracefully close the
service. Each has a 30-second bound and must exit at the requested fault hook.
The interrupted input case re-fetches the same synthetic observation before
comparison. Equivalence applies to the same committed observation sequence;
unseen broker responses are not recoverable evidence.

Local process-loss results alone do not establish Windows execution. The returned
Windows selection below separately proves these synthetic scenarios on the target
platform. Neither run establishes host power-loss durability, network filesystem
behavior or native MT5 recovery.

## Test and source-integrity results

| Check | Status |
| --- | --- |
| Selected accepted baseline | OBSERVED: 240 passed, no failures or skips |
| Wider tracked suite before additions | OBSERVED: 696 passed, 5 failed, no skips; exact failures disclosed below |
| New history/recovery suite | OBSERVED: 21 passed, including explicit missing-tick characterization |
| Initial integrated selected suite | OBSERVED: 261 passed, no failures or skips; retained as the run preceding test portability changes |
| Final integrated selected suite | OBSERVED: 263 passed, no failures or skips; includes 98 forward cases and two additional empty/nonempty lock variants |
| Final diagnostic suites | OBSERVED: 22 passed, no failures or skips, 0.27s; 10 collector invariants plus 12 probe unit tests |
| Native-history probe unit tests | OBSERVED by lead: 12 passed, no failures or skips, 0.18s; synthetic API tests, with compileall zero exit |
| Frozen 22-file verifier | OBSERVED before and after final collector run: all 22 match canonical hashes |
| Source/import identity | OBSERVED: 86 source/config/test files recorded and LF-verified; imported runtime confined to the expected checkout; pre/post identity unchanged; capture gate false |
| Compile/static sanity checks | OBSERVED zero exit: compileall over production package and the three diagnostic Python files |

Machine-readable [local evidence](evidence/xm_v1_forward_authorization/local.json)
preserves the final collector's actual UTC run timestamps, source hashes, runtime,
baseline HEAD/branch, pre/post checks and test counts. It includes every unique
JUnit test identity, outcome and duration for the 240-test baseline, wider
701-test baseline, initial 261-test selection and initial 10 collector tests. A
separate final portability supplement records the 263-test selected run, all
22 diagnostic tests, updated file hashes and full pre/post collector identity.
Original
collector/JUnit SHA256 values bind the underlying run artifacts. Unrelated
untracked names, hostname, workstation paths and raw tracebacks are deliberately
omitted; redaction counts and summaries are explicit. The wider baseline is not
reported as passing merely because the narrower selection passes.

The local evidence includes a separately dated native-history probe supplement
with its tool/test hashes and 12 unique JUnit cases and outcomes. These two files
were added after the collector run and are not retroactively included in its
86-file source manifest or 261-test selection. The new test files add **43 tests**:
21 history/recovery cases, 10 collector invariants and 12 probe unit tests.
Two additional lock variants in existing tests bring the net increase to
**45 cases**. Independent identity comparison confirms **238** unchanged baseline
identities, with exactly two original writer tests expanded to four lock-file
variants and 21 new history/recovery identities: **240 − 2 + 4 + 21 = 263**.
No original coverage was dropped. Final observed local coverage is the main
**263-test selection plus 22 separate diagnostic tests**. The final collector enforces the updated count,
and incomplete/skipped/failed selections remain invalid. Probe unit-test success
is synthetic evidence; six actual native-history observations are reported below. The corrected Windows
regression run has passed; final independent review is complete.

The five wider-suite baseline failures were:

1. `tests.test_research_xau_real_broker_cost_calibration_result::test_actual_committed_protocol_guard_binds_full_commit_and_canonical_provenance`
   — `origin/main is not the canonical starting SHA`.
2. `tests.test_research_xm_gold_historical_validation::test_actual_canonical_git_raw_and_compatibility_identities_without_strategy_evaluation`
   — `canonical Git provenance mismatch`.
3. `tests.test_research_xm_gold_historical_validation::test_stage_b_guard_never_invokes_evaluator_when_any_required_identity_fails[verify_frozen_production_sources]`.
4. `tests.test_research_xm_gold_historical_validation::test_stage_b_guard_never_invokes_evaluator_when_any_required_identity_fails[verify_xm_raw_source_identity]`.
5. `tests.test_research_xm_gold_historical_validation::test_stage_b_guard_never_invokes_evaluator_when_any_required_identity_fails[verify_compatibility_artifact_identity]`.

The last three expected `synthetic identity mismatch`, but the earlier canonical
Git provenance guard raised `canonical Git provenance mismatch`. These failures
were observed on the accepted baseline before this phase's additions. They bind
historical research phases to their canonical Git starting state, which differs
from the current forward branch. No guard, canonical ref, frozen source or
acceptance criterion was changed to make them pass. Their pre-existing status
limits the claim to the relevant selected regression suite; this phase does not
claim a green wider suite or repaired research-phase provenance.

Reproduce the added synthetic coverage with
`.venv/bin/python -m pytest tests/test_forward_authorization_blockers.py -q`.
On Windows use that checkout's verified Python interpreter. The bounded offline
collector accepts a fresh external evidence directory and runs the exact selected
suite, with child-interpreter import checks and a timeout, then re-verifies source
identity. An explicitly requested terminal path adds only a metadata audit.
Neither mode starts capture; successful synthetic validation still emits
`authorization=BLOCKED`, `execution=false`, native synchronization/restart
`NOT_OBSERVED` and session completeness `UNRESOLVED`.

## Windows, XM and source identity

The returned [Windows evidence tree](evidence/xm_v1_forward_authorization/windows-observed-20260910/import-receipt.json)
is now imported and verified against all 12 members of its export manifest. The
lead verified archive SHA256
`6df4185215e3abcad7ca19a3cfab6b379e55345a6c56d3ca34fd69a37f84398c`.
The original manifest retains Windows-original and exported hashes; the import
receipt separately binds repository copies after additional workstation-path and
hostname redaction. JSON account identity is pseudonymized; process IDs remain
because they are necessary causal evidence. Earlier visual-only summaries remain
historical records and are superseded by the returned artifacts, not erased.

The [export metadata](evidence/xm_v1_forward_authorization/windows-observed-20260910/export-metadata.json)
records Windows Server 2019 build 17763, Python 3.14.7, MetaTrader5 package 5.0.6180,
NumPy 2.5.3 and pytest 9.1.1. It records the isolated detached accepted baseline
`08c1e0dc0e981a641c60fb49d800988fcb3816c2`, exact diagnostic and source hashes,
expected runtime imports, LF verification and all 22 frozen hashes. Pre/post
export source snapshots match. These are export-time observations and must be
paired with each earlier run's own recorded source identity. The earlier original
Windows checkout at `6adb55d` remains distinct; its initial remote fetch could
not obtain `08c1e0d`, so the accepted baseline was transferred separately.

The initial Windows result **239 passed, 7 failed, 11 skipped, 257 total** remains
incomplete. The later [263-case run](evidence/xm_v1_forward_authorization/windows-observed-20260910/final263run/evidence.json)
completed with **261 passed, 2 failed, 0 skipped**, return code 1,
`source_unchanged=true`, stage `synthetic_tests` and `INVALID_OR_INCOMPLETE`.
Both failures are the empty/nonempty variants of
`test_root_single_writer_release_crash_and_rejected_writer_does_not_mutate`:
reacquisition fails at test line 393 after launcher `kill()`/`wait()`, through
`msvcrt.locking` at `durable.py:71` and the writer-denial exception at line 77.
These failures are retained rather than relabeled as passing.

The returned [controlled lock-owner probe](evidence/xm_v1_forward_authorization/windows-observed-20260910/lock-owner-observation-v1/evidence.json)
now supports the wrong-process-wait explanation in two controlled cases. In both,
the actual owner's PID differs from `Popen.pid`; its process handle remains
unsignaled after waiting for the killed launcher. Lock acquisition is denied
while that owner is alive and immediately after launcher wait, then succeeds
after a bounded wait for the owner's handle to signal. The owner wait took about
3.5–3.9 ms. Evidence bytes, lock identity and final lock bytes were unchanged;
`durable.py` hashes match before/after. Interpretation: launcher termination alone
was insufficient to establish lock-owner termination in these tests. This is not
evidence of a production lock-release defect, and it does not certify other
crash or power-loss scenarios. The probe artifact itself retains its original
`TWO_CASES_OBSERVED_NO_CAUSAL_VERDICT` status.

The [diagnostic source manifest](evidence/xm_v1_forward_authorization/diagnostic-sources/manifest.json)
retains the reviewed controlled-owner probe and its 60-second wrapper, plus the
bounded diagnostic-test runner and evidence exporter. Their bytes and SHA256
values are recorded for reproducibility. These are diagnostic source snapshots;
they neither enable capture nor certify platform recovery. The exporter snapshot
includes the corrected-run allowlist and is distinguished from earlier exports.

A test-only correction now waits on the actual owner's process handle. The
updated capture-integrity test SHA256 is
`b2cb0dbf8dcf2e94dc944b7a1188b83b83db2a32f775ba1d70fd111c0ca5c8ca`.
The separately recorded local rerun passes all **263** selected tests with
unchanged source identity, at 2026-09-10 21:14:09–21:14:56 UTC. Production locking
is unchanged. The returned [corrected Windows run](evidence/xm_v1_forward_authorization/windows-final-20260913/final263-ownerwait-v2/evidence.json)
now independently records **263 passed, 0 failed, 0 skipped**, with unchanged
pre/post source identity and all 22 canonical frozen hashes, on September 12,
2026, 10:57:18–11:03:44 UTC. Its original JUnit SHA256 is
`542262dcd5b4b7307fc3f2a76d2e761d5db368269dfe6dd59ce82b80d6670ba6`.
The [Windows diagnostic suites](evidence/xm_v1_forward_authorization/windows-final-20260913/diagnostic22.json)
separately record **22 passed, 0 failed, 0 skipped**, with unchanged source identity;
their original JUnit SHA256 is
`e99b6e8a86d13e432b04b7ace4a60b763b1328d11e1890d76f139e8188b09b12`.
Both JUnit hashes match their run records and export manifest. All 17 exported
payload members were hash/size verified; the [final import receipt](evidence/xm_v1_forward_authorization/windows-final-20260913/import-receipt.json)
binds repository copies after explicit additional redactions. The lead verified
returned archive SHA256
`4acefece9af724cb9f8705a834cb2a0b886e39acd6a9c7b0d8ddf7ddf11d05cc`.
This establishes bounded Windows synthetic recovery coverage. The same collector
retains native synchronization/restart `NOT_OBSERVED`, closure `UNRESOLVED` and
capture/execution disabled.

### Native bounded history observations

The returned [native report](evidence/xm_v1_forward_authorization/windows-observed-20260910/native-observation-v1/evidence.json)
records six actual requests on 2026-09-10 03:52:34–03:52:36 UTC. Before/after
metadata proves the same pseudonymous demo identity at XM Global Limited,
`XMGlobal-MT5 9`, `GOLD`, connected terminal build **6182**, with capture and
execution disabled. GOLD digits 2, point 0.01, contract size 100, POINTS swap and
Wednesday triple swap are observed; they are current metadata, not historical
constants. The package version 5.0.6180 comes from export metadata and is distinct
from the terminal build.

| Native request, UTC inclusive bounds | Returned payload count | Interpretation |
| --- | --- | --- |
| Recent M15 rates: Sep 9 15:45 to Sep 10 03:45 | 45 | Successful raw rate response; not finalized-candle or complete-grid proof |
| Recent H4 rates: same bounds | 3 | Open-time bounds 16:00 through 00:00; latest bar may still be forming at acquisition |
| Weekend-candidate M15: Sep 5 00:00 to Sep 7 00:00 | 0 | Successful empty response; legitimate closure NOT PROVEN |
| Weekend-candidate H4: same bounds | 1 | Its opening is exactly Sep 7 00:00, the included upper endpoint; not evidence of trading inside the preceding weekend interval |
| Recent ticks: Sep 10 03:30–03:45 | 3,467 | Successful returned payload; observed bounds 03:30:02.241–03:44:59.623 do not prove exhaustiveness |
| Weekend-candidate ticks: Sep 5 12:00–12:15 | 0 | Successful empty response; missing-history and closure remain indistinguishable |

All six immediate status codes are 1. The report explicitly retains
`completeness=UNRESOLVED` and `closure_certificate=false`. Python session-quote,
session-trade, series-info and symbol-synchronization accessors were not callable
on the observed API surface. No actual synchronization timeout, disconnected
terminal recovery or terminal restart was induced by this finite probe. Raw
payload summaries do not establish legitimate weekend closure or historical
session exceptions. Bounded Windows synthetic recovery tests have passed; combined independent review
is complete. Native synchronization/restart remains outside those observations.

## Independent adversarial review matrix

Independent reviews considered the accepted baseline, the 43 tests in new files and the
new diagnostics. The native probe's 12 tests were independently reviewed; no new
HIGH or BLOCKER defect was reported in that bounded diagnostic code. This is not
a finding that the outstanding authorization blockers are resolved. Final independent acceptance review inspected all 17 returned export members,
the exact 263 local/Windows test identities and 22 diagnostic outcomes, unchanged
pre/post identity, 41 runtime hashes and all 22 canonical Git source hashes. It
reported no HIGH or BLOCKER engineering defect in the accepted changes and
supported the scientific BLOCKED disposition. The lead retains final acceptance;
this review does not supply missing native synchronization or closure evidence.

| Required question | Evidence and present disposition |
| --- | --- |
| 1. Can partial MT5 history masquerade as legitimate GOLD closure? | No closure acceptance path exists: missing strategy slots remain blocked in synthetic tests. Positive closure completeness is still UNRESOLVED; successful empty/nonempty responses are insufficient. |
| 2. Can synchronization timeout accidentally advance strategy state? | Tested non-success status with nonempty payload and missing payload preserves all prior canonical bytes/state across every stream. Actual native synchronization behavior remains unobserved here; API success is not a completeness certificate. |
| 3. Can a Windows crash publish progress without durable observations? | Local persistence-fault and actual subprocess-exit tests reconstruct only committed input. The same 263-case selection now passes on Windows, including five actual subprocess-loss boundaries; this does not establish host power-loss durability. |
| 4. Can reconstruction differ from uninterrupted processing? | Exact local and Windows synthetic input/projection/checkpoint bytes and recursive state match for the same committed observation stream, including repeated recovery. Later, previously uncommitted broker observations are outside that equivalence claim. |
| 5. Can duplicate history produce duplicate decisions? | Local and Windows duplicate/retry and signal/shadow recovery tests preserve logical identities and projection counts; conflicting duplicates fail closed. |
| 6. Can H4/M15 ordering differ after restart? | Preserved baseline coincident chronology tests enforce H4 before M15 and replay-state equality. These cases also pass in the returned Windows selection. |
| 7. Can forming H4 influence an earlier M15 decision? | Preserved forming-H4 isolation tests compare exact earlier evidence/state. The native probe returns raw rate summaries, which are not asserted to be finalized strategy candles. |
| 8. Can two writers become authoritative? | Existing local cross-process lock tests pass. Two earlier Windows cases fail after launcher termination; neither demonstrates simultaneous writers. The returned controlled probe distinguishes launcher and actual owner lifetime and observes acquisition only after owner exit. The corrected full Windows regression run passes both variants after waiting for the actual owner process. |
| 9. Has any frozen V1 source changed? | Final local collector verifies all 22 canonical hashes before/after, with unchanged recorded source identity. Diagnostic source files are additive; the returned Windows export also records the accepted checkout, runtime imports, source hashes and 22 unchanged frozen files. Each run remains bound to its own source identity. |
| 10. Has an inference been presented as an observation? | Synthetic/local observations, primary-document contracts and inferences are labeled. Calendar-shaped gaps, payload hashes, API success, terminal metadata and source portability are not promoted to closure or native-recovery proof. |
| 11. Does platform-specific uncertainty remain that could corrupt the forward sample? | Yes: actual terminal synchronization/restart and complete GOLD closure semantics remain unresolved. The returned Windows harness results cover synthetic recovery; host power-loss/network-filesystem durability is not established by process-exit testing. |

The probe records normalized payload hashes, row counts, dtypes and timestamp
bounds rather than retained raw rows. Those summaries identify returned payloads
but cannot independently establish omitted-row completeness or reconstruct the
raw population for a new audit. Returned recent rates may include forming bars;
their counts are raw transport observations. The explicit terminal PID/image
check occurs before the probe, so it does not demonstrate continuous PID identity
through every request or rule out a restart between checks. Account/terminal
metadata checks narrow that risk without proving process continuity. Pair each
native report with the lead's transfer hashes, checkout/import identity and
process/run records; the native report alone is not a full source provenance
certificate. These limits are retained rather than hidden by passing tests.

## Final authorization disposition

The target-period GOLD closure is directly bounded by native tick and M15
history, and native cold synchronization, induced IPC failure with no rows,
restart, and deterministic post-restart payload equivalence have now been
observed. The two authorization blockers are resolved for the bounded passive
activation scope.

The accepted permission state is:

| Permission | State |
| --- | --- |
| Audit | Allowed |
| Passive capture | Authorized only for the next bounded smoke-test stage |
| Demo execution | Not yet granted |
| Live execution | Not granted |

The PASS does not silently admit any other history gap. During activation, an
unresolved warmup or prospective gap is a STOP condition, not permission to
invent a candle or weaken the chronology barrier. See the
[bounded Windows VDS runbook](xm_v1_forward_capture_runbook.md).

`BLOCKER 1: RESOLVED`

`BLOCKER 2: RESOLVED`

`XM V1 FORWARD CAPTURE AUTHORIZATION: PASS`

`DEMO EXECUTION AUTHORIZATION: NOT YET GRANTED`

`LIVE EXECUTION AUTHORIZATION: NOT GRANTED`
