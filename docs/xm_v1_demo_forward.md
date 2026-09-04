# XM GOLD V1 demo forward capture

The service is a prospective evidence collector for the frozen V1 strategy on
`XMGlobal-MT5 18` / `GOLD`. It never accepts, stores, or prints credentials.
Log into the intended **demo** account interactively in the official MT5
terminal before running it. In MT5, confirm the account is Demo, GOLD is
visible, and both Algo Trading and Python/API trading are enabled.

From PowerShell on the Windows VDS (after activating the project environment):

```powershell
python -m pip install MetaTrader5
python tools\run_xm_v1_forward.py --root D:\QuasarTrendEvidence --mode audit
python tools\run_xm_v1_forward.py --root D:\QuasarTrendEvidence --mode capture --execution-mode demo --interval-seconds 5
python tools\run_xm_v1_forward.py --root D:\QuasarTrendEvidence --terminal-path "C:\Program Files\MetaTrader 5\terminal64.exe" --mode audit
python tools\run_xm_v1_forward.py --root D:\QuasarTrendEvidence --repo-root D:\QuasarTrend --mode audit
```

Audit-only is the default. `--execution-mode demo` is necessary but never
sufficient: the process additionally requires terminal connection, exact server
and symbol, `ACCOUNT_TRADE_MODE_DEMO`, account/terminal/expert trade flags, and
a login-derived SHA-256 pseudonym. Live, contest, unknown, and missing flags
are blocked. Evidence is written under `forward/xm/GOLD`; the immutable
`golden/GOLD_ticks.csv` is never opened for writing.
Capture mode refuses to start unless that complete broker-side demo proof and
the explicit `--execution-mode demo` agree; audit-only may safely report an
unproven account without starting capture.

The service calls official MT5 `initialize()` once before capability discovery.
`--terminal-path` only selects an already interactively logged-in terminal; it
is not authentication, and the CLI intentionally has no credential parameters.
Every forward journal is provenance-bound to the runtime source SHA-256 and
frozen V1 commit `c58e18ef545909184267342eff712dd08bf47dda` / manifest SHA-256
`a6b02c8056c9996eb3bcac64a18588251f9a7c741f6c208eacbdc82de15f3e6d`.
An existing evidence directory with a different binding fails closed.
Before capability discovery the service also verifies the actual frozen V1 and
Pine source bytes under `--repo-root` (default: current directory); any drift
fails closed before capture.
The Windows VDS validation should run the focused MT5 forward tests with the
official NumPy-backed MetaTrader5 package installed, in addition to audit-only
terminal verification.

M1 is capture evidence. Only completed native M15 and H4 bars flow into the
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

`DEMO ORDER SUBMISSION BLOCKED — POSITION SIZE SEMANTICS REQUIRE SOL/MAIN DECISION`

It has an orders/deals read-only reconciliation API for ambiguous acknowledgements
and never blindly retries. Requested and observed execution fields, including
swap/reason/timestamps and gross versus observed net R, remain nullable rather
than inferred. Holdout aggregate economics are blocked from
2026-08-28T20:58:00Z through 2027-03-01T23:00:00Z; individual prospective
records remain permitted.
