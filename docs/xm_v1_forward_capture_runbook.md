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
`d1c86de15e8fe67744a6340e88905bb659de413e28d38de73f6228a06309d32a`.

From PowerShell in the accepted authorization-freeze checkout:

```powershell
$Repo = "D:\QuasarTrend"
$Evidence = "D:\QuasarTrendEvidence\XMGlobal-MT5-9-bounded-passive-v1-closure-cert3-20260914"
$Terminal = "C:\Program Files\MetaTrader 5\terminal64.exe"
Set-Location $Repo
if ((git status --porcelain --untracked-files=no)) { throw "tracked worktree is dirty" }
git merge-base --is-ancestor c58e18ef545909184267342eff712dd08bf47dda HEAD
if ($LASTEXITCODE -ne 0) { throw "frozen V1 is not an ancestor" }
$FrozenPaths = @(python -c "from quasartrend.research.xm_gold_historical_validation import FROZEN_PRODUCTION_SOURCE_SHA256 as A, FROZEN_PINESCRIPT_SOURCE_SHA256 as B; print(*A, *B, sep='\n')")
git diff --exit-code c58e18ef545909184267342eff712dd08bf47dda -- $FrozenPaths
if ($LASTEXITCODE -ne 0) { throw "current frozen bytes differ from canonical Git" }
python -c "from pathlib import Path; from quasartrend.research.xm_gold_historical_validation import verify_frozen_production_sources; from quasartrend.forward.mt5 import implementation_hash; assert len(verify_frozen_production_sources(Path('.'))) == 22; assert implementation_hash() == 'd1c86de15e8fe67744a6340e88905bb659de413e28d38de73f6228a06309d32a'; print('SOURCE IDENTITY PASS')"
python tools\run_xm_v1_forward.py --root $Evidence --repo-root $Repo --terminal-path $Terminal --mode audit --execution-mode none
```

The failed pre-certificate activation root
`D:\QuasarTrendEvidence\XMGlobal-MT5-9-bounded-passive-v1` and the failed first
certificate activation root
`D:\QuasarTrendEvidence\XMGlobal-MT5-9-bounded-passive-v1-closure-cert-20260914`
and the failed second certificate activation root
`D:\QuasarTrendEvidence\XMGlobal-MT5-9-bounded-passive-v1-closure-cert2-20260914`
are immutable and remain bound to their earlier `implementation_sha256` values;
do not reuse, migrate, edit, or delete any of them. The post-review retry must use
the new `closure-cert3` evidence root shown above. A provenance mismatch when
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
evidence and the Sep 9 00:00–01:00 UTC daily closure evidence, are specific to
those exact half-open intervals on XMGlobal-MT5 9 / GOLD. The Sep 7 certificate
excludes the existing 21:15 candle and the 01:00 reopen. The Sep 9 certificate
excludes the existing 23:45 candle and the 01:00 reopen. These certificates are
not permission to classify another gap as a closure. If the runtime stops on a
different warmup or prospective gap, the smoke test is rejected until that exact
gap is independently explained; do not bypass the barrier.

## Next separate gate

Only after this bounded passive smoke test is accepted may a separate phase begin:
`XM DEMO EXECUTION LIFECYCLE / RECONCILIATION VALIDATION`. Its established probe
volume is `GOLD 0.01 lot = SYMBOL_VOLUME_MIN`; this is not production sizing and
does not authorize an order in the current phase. Live execution remains
unauthorized.
