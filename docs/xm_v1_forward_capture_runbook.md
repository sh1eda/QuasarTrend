# XM V1 bounded passive forward-capture activation

This procedure activates observation only on the validated Windows VDS. It does
not authorize or exercise demo/live execution, order submission, modification,
cancellation, or sizing. The runner defaults to audit and capture requires an
explicit finite successful-poll bound.

## Fixed identity and source preflight

Use the official MT5 Python environment and an interactively logged-in DEMO
terminal. Pin `XM Global Limited`, `XMGlobal-MT5 9`, `GOLD`, and terminal build
6182. Do not record the raw login. Set repo-local Git line endings before the
validated checkout (`core.autocrlf=false`, `core.eol=lf`). The accepted frozen
identity is commit `c58e18ef545909184267342eff712dd08bf47dda`, manifest SHA-256
`a6b02c8056c9996eb3bcac64a18588251f9a7c741f6c208eacbdc82de15f3e6d`,
and passive runtime implementation SHA-256
`e5fdbd3bc8f9f82b8b32e4ad03e05613f6baa4f4c5670d3f949ece4024d47f46`.

From PowerShell in the accepted authorization-freeze checkout:

```powershell
$Repo = "D:\QuasarTrend"
$Evidence = "D:\QuasarTrendEvidence\XMGlobal-MT5-9-bounded-passive-v1-closure-cert6-20260915"
$Terminal = "C:\Program Files\MetaTrader 5\terminal64.exe"
Set-Location $Repo
if ((git status --porcelain --untracked-files=no)) { throw "tracked worktree is dirty" }
git merge-base --is-ancestor c58e18ef545909184267342eff712dd08bf47dda HEAD
if ($LASTEXITCODE -ne 0) { throw "frozen V1 is not an ancestor" }
$FrozenPaths = @(python -c "from quasartrend.research.xm_gold_historical_validation import FROZEN_PRODUCTION_SOURCE_SHA256 as A, FROZEN_PINESCRIPT_SOURCE_SHA256 as B; print(*A, *B, sep='\n')")
git diff --exit-code c58e18ef545909184267342eff712dd08bf47dda -- $FrozenPaths
if ($LASTEXITCODE -ne 0) { throw "current frozen bytes differ from canonical Git" }
python -c "from pathlib import Path; from quasartrend.research.xm_gold_historical_validation import verify_frozen_production_sources; from quasartrend.forward.mt5 import implementation_hash; assert len(verify_frozen_production_sources(Path('.'))) == 22; assert implementation_hash() == 'e5fdbd3bc8f9f82b8b32e4ad03e05613f6baa4f4c5670d3f949ece4024d47f46'; print('SOURCE IDENTITY PASS')"
python tools\run_xm_v1_forward.py --root $Evidence --repo-root $Repo --terminal-path $Terminal --mode audit --execution-mode none
```

The failed pre-certificate activation root
`D:\QuasarTrendEvidence\XMGlobal-MT5-9-bounded-passive-v1` and the failed first
certificate activation root
`D:\QuasarTrendEvidence\XMGlobal-MT5-9-bounded-passive-v1-closure-cert-20260914`
and the failed second certificate activation root
`D:\QuasarTrendEvidence\XMGlobal-MT5-9-bounded-passive-v1-closure-cert2-20260914`
and the failed third certificate activation root
`D:\QuasarTrendEvidence\XMGlobal-MT5-9-bounded-passive-v1-closure-cert3-20260914`
and the failed fourth certificate activation root
`D:\QuasarTrendEvidence\XMGlobal-MT5-9-bounded-passive-v1-closure-cert4-20260914`
and the failed fifth certificate activation root
`D:\QuasarTrendEvidence\XMGlobal-MT5-9-bounded-passive-v1-closure-cert5-20260914`
are immutable and remain bound to their earlier `implementation_sha256` values;
do not reuse, migrate, edit, or delete any of them. In particular, `closure-cert5`
is an immutable failed activation root bound to implementation SHA-256
`626df7f555229f11b06cf7ed787286216317c0c727680fb35c586ba8d15516e4`.
The post-review retry must use the new `closure-cert6` evidence root shown above.
A provenance mismatch when
opening any earlier root is an intentional fail-closed result.

The audit must report `audit_allowed=true`, `capture_allowed=true`, and
`execution_allowed=false`, with exact company/server/DEMO/GOLD identity and the
expected terminal build. Use a fresh evidence root for the first activation;
never reuse an older root with different provenance. Algo/Python trading
permission flags are reporting-only for passive capture and need not be enabled.

## Bounded smoke window

Choose an open-market window that crosses the next natural H4 finalization when
practical. Do not manufacture an H4 event. Run at most 120 successful polls at a
60-second interval (about two hours after any cold synchronization):

```powershell
python tools\run_xm_v1_forward.py --root $Evidence --repo-root $Repo --terminal-path $Terminal --mode capture --execution-mode none --interval-seconds 60 --max-polls 120
```

Then restart against the same evidence root for at most 30 more successful polls:

```powershell
python tools\run_xm_v1_forward.py --root $Evidence --repo-root $Repo --terminal-path $Terminal --mode capture --execution-mode none --interval-seconds 60 --max-polls 30
```

The first window must show real finalized M15 ingestion, durable input/checkpoint
progress, no duplicate canonical advancement, no unresolved-gap advancement,
Family-1 as shadow-only, and execution count zero. The restart must resume the
same journal/checkpoint identity and deterministically reconstruct the same
prefix before advancing. Accept H4 only if a real finalized H4 closes naturally;
if none closes, record that fact and schedule one additional bounded window that
crosses a natural H4 close. Do not weaken chronology checks or extend the run to
re-prove profitability.

Acceptance requires H4-before-coincident-M15 chronology, no forming-H4 leakage,
600 finalized candles per strategy timeframe, exact duplicate idempotency,
fsync-before-progress, atomic/checksummed checkpoint recovery, torn-tail recovery,
and deterministic restart/replay. Compare journal counts and SHA-256 values before
and immediately after restart; the reconstructed prefix must be byte-identical.

## STOP conditions

Stop and preserve the evidence root on any company/account/server/DEMO/symbol or
terminal identity mismatch; frozen source, runtime hash, or tracked-worktree
drift; persistence/fsync/atomic-replace failure; unresolved history gap; corrupt
journal/checkpoint or mismatched checksum; chronology or forming-H4 violation;
duplicate advancement; non-deterministic restart/replay; unexpected execution,
order/reconciliation API invocation; nonzero execution journal count; Family-1
leaving shadow-only status; or any ambiguous state that could corrupt the
prospective sample. Do not delete, edit, truncate, or manually repair evidence.

The Sep 4 00:00–01:00 UTC daily closure and Sep 5 00:00–Sep 7 01:00 UTC
weekend closure evidence, plus the Sep 7 21:30–Sep 8 01:00 UTC early-closure
evidence, the Sep 9 00:00–01:00 UTC daily closure evidence, and the Sep 10
00:00–01:00 UTC closure evidence, and the Sep 11 00:00–01:00 UTC closure
evidence, plus the Sep 12 00:00–Sep 14 01:00 UTC weekend closure evidence, are specific to
those exact half-open intervals on XMGlobal-MT5 9 / GOLD. The Sep 7 certificate
excludes the existing 21:15 candle and the 01:00 reopen. The Sep 9 certificate
excludes the existing 23:45 candle and the 01:00 reopen. The Sep 10 M15
certificate excludes Sep 9 23:45 and Sep 10 01:00. The Sep 11 M15 certificate
excludes Sep 10 23:45 and Sep 11 01:00. The Sep 12–14 M15 certificate excludes
Sep 11 23:45 and the Sep 14 01:00 reopen. These certificates are
not permission to classify another gap as a closure. If the runtime stops on a
different warmup or prospective gap, the smoke test is rejected until that exact
gap is independently explained; do not bypass the barrier.

The Sep 10 certificate uses the user-supplied native-history probe from the
validated XM Global Limited / XMGlobal-MT5 9 / GOLD target: M15 bars at Sep 9
23:30 and 23:45 and Sep 10 01:00, 01:15, and 01:30, with no M15 bars at
00:00, 00:15, 00:30, or 00:45. Reported tick endpoints are
`2026-09-09T23:58:59.589Z` and `2026-09-10T01:00:02.253Z`
(gap 3662.664 seconds). The M1 probe reported 60 rows, first 23:30 and last
01:30, and no activity during 00:00–01:00. Its stated half-open request ends
at 01:30 despite that reported last row; this endpoint ambiguity is preserved,
not used to infer closure from row count. Only the exact supplied
`[2026-09-10T00:00:00Z, 2026-09-10T01:00:00Z)` M15 interval is admitted.
No recurring weekday, schedule, timezone, DST, or future closure is inferred.
Contradictory tick/M1 activity still prevents closure admission; late activity
contradicting an already committed closure rejects the observation before
state, journal, or checkpoint mutation. Repository verification uses synthetic
fixtures only; this change does not constitute a new broker probe or authorize
a retry before review.

The Sep 11 certificate uses the user-supplied target-native probe from the
validated XM Global Limited / XMGlobal-MT5 9 / GOLD target. It showed existing
M15 bars at Sep 10 23:30 and 23:45; no M15 bars at Sep 11 00:00, 00:15, 00:30,
or 00:45; and existing M15 bars at Sep 11 01:00, 01:15, and 01:30. It showed no
M1 activity inside the exact 00:00–01:00 interval. The M1 row count does not
itself prove closure. The last pre-closure tick was
`2026-09-10T23:58:59.229000Z`; the first reopen tick was
`2026-09-11T01:00:02.005000Z`; and the largest observed tick gap was 3662.776
seconds. Only the exact supplied
`[2026-09-11T00:00:00Z, 2026-09-11T01:00:00Z)` M15 interval is admitted. It
does not establish a recurring daily or weekday schedule, timezone or DST rule,
or any future closure. Contradictory tick/M1 activity still prevents closure
admission; late activity contradicting an already committed closure rejects the
observation before state, journal, or checkpoint mutation. Repository
verification uses synthetic fixtures only; this change does not constitute a
new broker probe or authorize a retry before review.

The Sep 12–14 certificate uses the user-supplied target-native probe from the
validated XM Global Limited / XMGlobal-MT5 9 / GOLD target. It showed M15 bars
at Sep 11 23:30 and 23:45; no M15 bars inside the exact Sep 12 00:00–Sep 14
01:00 candidate interval; and M15 bars at Sep 14 01:00, 01:15, and 01:30. It
showed `M1_INSIDE_CANDIDATE = 0`; the total M1 row count does not itself prove
closure. The last pre-closure tick was `2026-09-11T23:57:59.682000Z`; the first
reopen tick was `2026-09-14T01:00:02.124000Z`; and the largest observed tick gap
was 176522.442 seconds. Only the exact supplied half-open
`[2026-09-12T00:00:00Z, 2026-09-14T01:00:00Z)` M15 interval is admitted. The
earlier Sep 5–7 certificate is not authority for this interval. This evidence
does not establish any recurring weekend, daily, weekday, Saturday/Sunday,
Friday-close, Monday-reopen, broker-schedule, timezone, DST, historical-session,
or future rule. Contradictory tick/M1 activity still prevents closure admission;
late activity contradicting an already committed closure rejects the observation
before state, journal, or checkpoint mutation. Repository verification uses
synthetic fixtures only; this change does not constitute a new broker probe or
authorize a retry before review.

## Next separate gate

Only after this bounded passive smoke test is accepted may a separate phase begin:
`XM DEMO EXECUTION LIFECYCLE / RECONCILIATION VALIDATION`. Its established probe
volume is `GOLD 0.01 lot = SYMBOL_VOLUME_MIN`; this is not production sizing and
does not authorize an order in the current phase. Live execution remains
unauthorized.
