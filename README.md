# QuasarTrend

QuasarTrend is a deterministic multi-timeframe trading signal engine combining
HEMA Trend, Kalman Step Signals, higher-timeframe bias, lower-timeframe
execution, and ATR-based risk logic. TradingView/PineScript parity for the
indicator layer has been externally verified.

## Strategy overview

The Phase 2 `StrategyEngine` consumes finalized 15-minute bars together with
their finalized indicator snapshots and 4-hour bias snapshot.

- The 4H HEMA direction establishes the active `LONG` or `SHORT` bias.
- A fresh 15m HEMA flip in that bias direction creates an execution setup.
- Kalman direction confirms the setup; it may already agree or agree while the
  setup remains pending.
- An opposite 15m HEMA flip is an indicator exit.
- ATR defines a fixed stop when the trade opens.

Multiple valid trades may occur during one 4H bias, but every new trade
requires its own fresh HEMA flip. The implemented confirmation mode is
`STATEFUL_EITHER_ORDER`; other exposed mode values are interface-only and are
rejected by the Phase 2 engine.

## Current project status

### Phase 1 — PASS

The indicator layer includes Pine-compatible EMA, RMA/ATR, HEMA, a recursive
Kalman filter, and custom Supertrend. TradingView golden parity has been
verified for 15m and 4H reference exports, and the implementations are tested
for batch, incremental, and checkpoint determinism.

### Phase 2 — PASS

The dependency-free historical `StrategyEngine` provides deterministic state
transitions with bias epochs, pending setups, fresh-flip entry requirements,
and fixed ATR stops. It defines exit priority, prevents same-bar reversal,
handles duplicate and out-of-order timestamps, isolates state by symbol, and
supports deterministic replay.

The verified full test suite result is **147 passed** with local 15m and 4H
golden CSV exports present. Fresh clones skip export-dependent checks until the
datasets are generated as described in
[`tests/golden/README.md`](tests/golden/README.md).

### Phase 3 — PASS

Phase 3 adds a dependency-free chronological replay layer and an event-only
historical backtester. `HistoricalBar.open_time` is explicitly the source bar's
epoch-millisecond open time; its legal decision time is
`open_time + timeframe duration`. The initial topology supports only 15m
execution bars and 4H bias bars. Inputs must already be strictly ordered by
`(finalized_at, priority)`, where a 4H bar is processed before a 15m bar at an
equal finalization instant. Gaps are accepted but never synthesized.

Replay updates the existing incremental HEMA/Kalman implementations and passes
the resulting finalized `StrategyBar` to the existing `StrategyEngine`. A 4H
HEMA snapshot is retained only after its candle finalizes, so it cannot affect
earlier 15m decisions. `ReplayState` holds strict-JSON indicator checkpoints,
the Phase 2 state, the latest legal 4H bias, and chronology cursor, allowing
prefix/resume replay without mutable global state.

The backtester consumes replay `TRADE_OPENED` and `TRADE_CLOSED` events only;
it does not recreate entries, exits, or stops. It applies fixed quantity
(default `1.0`), deterministic adverse bps slippage, and bps fees to canonical
Phase 2 event prices. Its equity curve is cumulative realized PnL, updated on
close only with no initial-capital or unrealized-MTM convention. Consequently,
max drawdown percentage is intentionally `None`. Gross PnL is calculated from
execution-adjusted fills before fees, and each trade's fractional return is net
PnL divided by execution entry notional.

The verified full test suite result after Phase 3 is **180 passed**.

### Phase 4 — Deterministic persistence and recovery

Phase 4 adds a narrow SQLite adapter around immutable `ReplayState`; replay,
strategy, and backtest behavior remain persistence-agnostic. A checkpoint is
identified by its symbol, fixed 15m/4h topology (including duration and equal-
boundary priority), every `ReplayConfig` field, and every `StrategyConfig`
field. The identity is canonical UTF-8 JSON (sorted keys, compact separators,
no non-finite values) hashed with SHA-256. `BacktestConfig` is intentionally
not part of the recovery identity.

```python
from quasartrend.persistence import PersistenceIdentity, SQLiteCheckpointStore

identity = PersistenceIdentity("BINANCE:BTCUSDT.P", replay_config, strategy_config)
store = SQLiteCheckpointStore("var/checkpoints.db")  # construction has no I/O
store.save_checkpoint(identity, replay_state)
recovered = SQLiteCheckpointStore("var/checkpoints.db").load_checkpoint(identity)
```

The database has one `checkpoints` row per `(symbol, execution_timeframe,
htf_timeframe)` slot. Its `PRAGMA user_version` is schema version `1`, and its
strict JSON replay envelope has checkpoint version `1`. Saves serialize and
validate the full replay state before a `BEGIN IMMEDIATE` transaction; SQLite
uses `synchronous=FULL` and the default rollback journal (WAL is not enabled).
The successful transaction atomically initializes an actually empty database
when needed and replaces the one active slot row for its compatible identity.
A failed write rolls back, leaving the previous valid checkpoint available.

An occupied slot cannot be saved under a different configuration fingerprint:
call `delete_checkpoint` with the original identity before creating a checkpoint
for the replacement configuration. `saved_at_ms` is wall-clock metadata only;
it is never part of replay equality, fingerprinting, chronology, or ordering.

`load_checkpoint` and `delete_checkpoint` never create a missing database or
directory. They return absence only for a missing/empty database or missing
slot. Schema, checkpoint-version, JSON/recursive-state corruption, symbol
mismatch, configuration mismatch, and chronology regression are explicit
persistence errors; a corrupted checkpoint is never silently treated as a
fresh replay state. The persisted chronology cursor is the replay cursor, not
the wall-clock `saved_at_ms` metadata, so the existing duplicate, ordering, and
same-boundary rules continue unchanged after recovery.

Recovery tests compare uninterrupted replay with the full concatenation of
prefix and resumed-suffix replay traces, then run the existing event-only
backtest over both complete trace sequences. Backtest accounting/results are
not persisted by Phase 4.

Later planned work includes Telegram integration and production hardening.

### Phase 6 — deterministic paper execution

Phase 6 adds a separate, **paper/simulated-only** execution boundary.  It has
no authenticated exchange client, credentials, private REST calls, leverage or
margin controls, or real-order path.  A future real-execution integration is a
separate phase and must not reuse this adapter as authorization to trade.

`TRADE_OPENED` becomes an entry intent and `TRADE_CLOSED` becomes an exit
intent (or stop intent when its reason is `EXIT_STOP`). `STOP_HIT` remains a
diagnostic event, so a stop produces exactly one close intent. Intents include
the symbol, Phase 4 configuration fingerprint, source processing key, event
ordinal/type, and strategy trade ID. Their IDs, orders, fills, and position
IDs are domain-separated canonical-JSON SHA-256 values; neither wall-clock
time nor random UUIDs participates in replay behavior.

The default `finalized-event-v1` paper model uses only the finalized canonical
`StrategyEvent.price`: an order is accepted then completely filled on that
same deterministic transition. The optional test policy may leave it accepted
and unfilled or reject it. There are no partial fills. Slippage is adverse
(long entries higher, long exits/stops lower; reversed for shorts), and fee is
`execution_price * quantity * fee_bps / 10_000`. `PaperExecutionConfig` is a
new, distinct configuration; Phase 3 `BacktestConfig` and its historical
accounting remain unchanged.

The paper adapter is exchange-independent and keeps its own immutable adapter
state plus typed `ACCEPTED`, `REJECTED`, and `FILLED` events. `DEFER` (durable
`NEW`) and `ACCEPT_ONLY` (durable `ACCEPTED`) are deterministic simulation/test
behaviors, not exchange connectivity or a real-order capability. Outstanding
orders block later candle advancement; a durable rejection is retained for
diagnostics and causes later advancement to fail closed. Adapter events may be
applied atomically after restart, so an accepted paper order can later fill
without a replayed strategy decision.

The Phase 6 `SQLiteExecutionStore` is separate from the Phase 4 checkpoint
schema. For each finalized source bar the runtime invokes its combined
`save_transition(identity, prior_state, ReplayStepResult)` before making the
candidate replay state visible. One `BEGIN IMMEDIATE` / `synchronous=FULL`
transaction persists the replay state, execution ledger, and paper-adapter
snapshot, eliminating the execution-ahead/lost-close window. A persistence
failure leaves both durable and in-memory state at the prior candle. A missing
combined checkpoint may bootstrap only from the initial replay state; restoring
an arbitrary noninitial replay state without its execution ledger is rejected.

On load, the store reconciles its durable ledger with its durable adapter
snapshot (orders/statuses, fills, and position). `reconcile(observed)` is also
available for deterministic external snapshot checks; any missing, extra, or
conflicting state raises a typed mismatch and is never repaired silently.
Rows and all execution IDs are symbol scoped, so interleaved symbols do not
share positions or orders.

### Phase 5 — PASS

Phase 5 adds a narrow, dependency-free REST polling boundary for Binance USD-M
public klines. `BinanceUSDMClient` calls `GET /fapi/v1/klines` with no API key
or authenticated trading capability. It maps an explicitly configured exchange
symbol (for example `BTCUSDT`) back to the durable QuasarTrend domain symbol
(`BINANCE:BTCUSDT.P`) and returns only validated `HistoricalBar` values. The
live runtime depends on the generic `MarketDataClient` protocol rather than on
Binance HTTP details.

All runtime timestamps are UTC epoch milliseconds. A source bar is legal only
when `bar.open_time + bar.timeframe.duration_ms <= clock.now_ms()`; equality is
legal. Poll requests include the aligned current/open bucket so a returned
exchange kline is fully validated, but that optional unfinished bar is excluded
before the frozen `ReplayEngine` sees it; its absence is not a cadence gap.
Each accepted bar goes through `ReplayEngine.step`
and is checkpointed before runtime memory and output advance. No direct live
`StrategyEngine` path exists.

A fresh runtime fetches the exact finalized suffix configured for each stream:
600 15-minute bars and 600 4-hour bars by default. The 4-hour count retains
margin above the observed full cold-convergence start at row 507. A recovered
Phase 4 checkpoint is loaded once and resumes from its existing recursive
state; it fetches one consumed overlap where available and suppresses all
processing keys at or before the stored cursor. Catch-up is paginated with a
default page size of 1,000 and a maximum of 10,000 candles per timeframe.
Every aligned, legally finalized open time in a requested range must be present:
a missing page, hole, duplicate, or out-of-order response raises an explicit
market-data gap/error rather than advancing the cursor.

The runtime merges independently validated 15m and 4h responses by the frozen
processing key. At a shared finalization boundary, 4h is always processed before
15m. Public HTTP 408, 429, and 5xx responses plus connection/timeout failures
are transient and get at most four deterministic attempts with 0.2s, 0.4s,
and 0.8s base delays (respecting a larger `Retry-After`). Other 4xx and malformed
responses are permanent and receive no retry. Callers receive canonical
processed bars, replay traces, strategy events, and the current replay state.
`polling_loop` is cooperative: it checks a caller-provided stop callback at poll
boundaries and exits without an additional fetch or sleep once stopped.

This phase only produces deterministic signal-domain events. It does not place
orders, maintain exchange positions, use websockets, send Telegram/alerts, add
ADR context, or optimize entries; those concerns remain outside Phase 5.

The Phase 5 gate is verified at **250 passed**. The local TradingView golden
audits remain unchanged: 10,452 15m rows and 8,480 4H rows, with zero source,
OHLC, seeded-recurrence, or cold-start-convergence mismatches.

## Architecture

```text
QuasarTrend/
├── .codex/
│   ├── agents/                 # Agent role configuration
│   └── config.toml
├── AGENTS.md                   # Correctness and collaboration policy
├── README.md
├── pyproject.toml
├── uv.lock
├── references/
│   └── pinescript/             # Authoritative PineScript references/export harness
├── src/
│   └── quasartrend/
│       ├── indicators/         # Pine-compatible indicator implementations
│       ├── strategy/           # Deterministic Phase 2 strategy state machine
│       ├── replay/             # Closed-candle chronological MTF replay
│       ├── backtest/           # Event-only deterministic accounting/metrics
│       ├── persistence/        # SQLite replay checkpoints and strict codec
│       ├── marketdata/         # Validated public exchange data boundary
│       └── runtime/            # Closed-candle polling and checkpoint timing
├── tests/
│   ├── golden/                 # Tracked export instructions; CSV exports stay local
│   └── test_*.py
└── tools/
    └── compare_tradingview.py  # CSV parity comparison utility
```

## Correctness principles

1. PineScript behavior is authoritative.
2. Decisions use closed candles only.
3. No lookahead or future-data leakage is permitted.
4. Recursive state transitions must be deterministic.
5. Backtest and live workflows should share strategy semantics.
6. Every trade requires a fresh execution setup.
7. State is isolated per symbol.
8. Decisions must be reproducible and explainable.

## Development

Install the locked development environment and run the verification suite:

```bash
uv sync
uv run pytest -q
uv run python -m compileall src tools
```

TradingView golden CSV exports are intentionally local and ignored by Git.
[`tests/golden/README.md`](tests/golden/README.md) describes how to produce
them from the PineScript export harness. When local exports are available, the
comparison utility can audit them:

```bash
uv run python tools/compare_tradingview.py --golden tests/golden/tradingview_15m.csv --golden tests/golden/tradingview_4h.csv
```

## Instruments

`BINANCE:BTCUSDT.P` (BTCUSDT perpetual) is the initial TradingView parity
reference. The `StrategyEngine` is instrument-agnostic. Gold/XAUUSD is an
intended future target market, but live Gold support is not implemented.

## Disclaimer

QuasarTrend is currently research and signal-generation software. It does not
execute real-money orders.
