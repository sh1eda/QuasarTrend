# XM GOLD V1 demo forward pre-flight

The service audits the exact broker environment intended for prospective frozen
V1 evidence. Demo servers `XMGlobal-MT5 9` and `XMGlobal-MT5 18` are the only
explicitly authorized server strings. Server membership alone is insufficient:
the account must report `XM Global Limited`, `ACCOUNT_TRADE_MODE_DEMO`, and the
strict `GOLD` contract profile described below. The service never accepts,
stores, or prints credentials.
Log into the intended **demo** account interactively in the official MT5
terminal before running it. For capability audit, confirm the account is Demo
and GOLD is visible. Algo Trading and Python/API trading settings are reported
as execution-readiness evidence but need not be enabled for audit.

From PowerShell on the Windows VDS (after activating the project environment):

```powershell
python -m pip install MetaTrader5
python tools\run_xm_v1_forward.py --root D:\QuasarTrendEvidence\XMGlobal-MT5-9-policy-v2 --repo-root D:\QuasarTrend --terminal-path "C:\Program Files\MetaTrader 5\terminal64.exe" --mode audit
```

Audit-only is the default and returns success only when the capability policy
passes. It requires a connected terminal, a login-derived SHA-256 environment
pseudonym, exact demo mode, exact company and allowlisted server, visible `GOLD`,
and exact structural contract checks: digits 2; point and tick size 0.01; tick
value 1; contract size 100; volume min/step 0.01 and max 50; POINTS swap mode;
Wednesday triple swap; USD base/profit/margin currencies; bank `XM`; and
description `GOLD`. Current swap rates and additional execution metadata are
captured as evidence but are not treated as durable historical constants.

Account, terminal, Expert Advisor, symbol-trading, and Python trading-API flags
are execution-readiness facts. They are reported independently and do not block
capability-only audit because audit sends no order. `--execution-mode demo` only
requests an execution-readiness assessment; it cannot authorize submission.
Live, contest, unknown company, unauthorized server, and incompatible product
specifications fail closed with distinct messages.

Bounded passive capture is authorized for the next Windows VDS smoke-test stage;
it is not automatically active. Process-crash recovery, strict journals,
single-writer exclusion, finalized warmup and cross-timeframe barriers remain
mandatory. Any unresolved history gap stalls the runtime and fails the smoke
test. Follow the [bounded activation runbook](xm_v1_forward_capture_runbook.md).
Demo order submission remains separately unauthorized and no `order_send` path
exists.

Use a fresh evidence root for this policy version. Capability snapshots are
immutable and include the implementation hash, so a directory written by the
older server-18-only implementation must not be overwritten or reused.
Evidence is under `forward/xm/GOLD`; `golden/GOLD_ticks.csv` is never opened for
writing.

The service calls official MT5 `initialize()` once before capability discovery.
`--terminal-path` only selects an already interactively logged-in terminal; it
is not authentication, and the CLI intentionally has no credential parameters.
Every forward journal is provenance-bound to the exact audited server, broker
policy version, combined transport/state-machine/storage source SHA-256, and
frozen V1 commit `c58e18ef545909184267342eff712dd08bf47dda` / manifest SHA-256
`a6b02c8056c9996eb3bcac64a18588251f9a7c741f6c208eacbdc82de15f3e6d`.
An existing evidence directory with a different binding fails closed.
Before capability discovery the service also verifies the actual frozen V1 and
Pine source bytes under `--repo-root` (default: current directory); any drift
fails closed before capture.
The Windows VDS validation should run the focused MT5 forward tests with the
official NumPy-backed MetaTrader5 package installed, in addition to audit-only
terminal verification.

The v2 capture runtime uses a canonical observation log and deterministic replay
to reconstruct all derived journals and verify checkpoints. It preserves the
frozen H4-before-coincident-M15 order and requires 600 finalized startup candles
per strategy timeframe at a fixed activation cutoff. Unresolved required history
stops strategy advancement. M1 gaps remain diagnostic evidence and are revisited.

Only prospective V1 opportunities first observed within 60 seconds of their
finalized decision bar can produce signals. Recovery retains the original
completed observation time. Family-1 `long_only` remains a separate shadow:
long signals are admitted and short signals recorded as rejected. V1 shorts are
preserved. The shadow component has no broker-order API.

No broker order can be sent in this phase. The execution adapter records:

`DEMO ORDER SUBMISSION BLOCKED — ORDER LIFECYCLE AUTHORIZATION NOT GRANTED`

It has an orders/deals read-only reconciliation API for ambiguous acknowledgements
and never blindly retries. Requested and observed execution fields, including
swap/reason/timestamps and gross versus observed net R, remain nullable rather
than inferred. Holdout aggregate economics are blocked from
2026-08-28T20:58:00Z through 2027-03-01T23:00:00Z; individual prospective
records remain permitted.
