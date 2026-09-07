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

Forward capture remains deliberately blocked. Audit found unresolved recovery,
missing-candle, bootstrap-depth, and delayed-H4 integrity failures that can
change recursive V1 state. The production constant remains fail closed until a
separately authorized repair has regression evidence. Demo order submission is
also separately unauthorized and no `order_send` path exists.

Use a fresh evidence root for this policy version. Capability snapshots are
immutable and include the implementation hash, so a directory written by the
older server-18-only implementation must not be overwritten or reused.
Evidence is under `forward/xm/GOLD`; `golden/GOLD_ticks.csv` is never opened for
writing.

The service calls official MT5 `initialize()` once before capability discovery.
`--terminal-path` only selects an already interactively logged-in terminal; it
is not authentication, and the CLI intentionally has no credential parameters.
Every forward journal is provenance-bound to the exact audited server, broker
policy version, runtime source SHA-256, and
frozen V1 commit `c58e18ef545909184267342eff712dd08bf47dda` / manifest SHA-256
`a6b02c8056c9996eb3bcac64a18588251f9a7c741f6c208eacbdc82de15f3e6d`.
An existing evidence directory with a different binding fails closed.
Before capability discovery the service also verifies the actual frozen V1 and
Pine source bytes under `--repo-root` (default: current directory); any drift
fails closed before capture.
The Windows VDS validation should run the focused MT5 forward tests with the
official NumPy-backed MetaTrader5 package installed, in addition to audit-only
terminal verification.

If the independent capture-integrity gate is repaired and authorized, M1 is
capture evidence. Only completed native M15 and H4 bars may flow into the
existing frozen replay, ordered H4 before a coincident M15 decision. V1
`TradeOpened` evidence is fsync-journaled before the independent Family-1
`long_only` shadow record. A long is admitted by the shadow stream; a short is
recorded as rejected. The shadow component has no broker-order API.

On capture restart, historical bars may warm the frozen replay state, but a V1
opportunity is journaled only when its finalized decision bar is no more than
60 seconds old (and after persisted capture activation). Older catch-up events
are counted as stale missed opportunities; they never become prospective V1 or
shadow execution candidates.

No broker order can be sent in this phase. The execution adapter records:

`DEMO ORDER SUBMISSION BLOCKED — ORDER LIFECYCLE AUTHORIZATION NOT GRANTED`

It has an orders/deals read-only reconciliation API for ambiguous acknowledgements
and never blindly retries. Requested and observed execution fields, including
swap/reason/timestamps and gross versus observed net R, remain nullable rather
than inferred. Holdout aggregate economics are blocked from
2026-08-28T20:58:00Z through 2027-03-01T23:00:00Z; individual prospective
records remain permitted.
