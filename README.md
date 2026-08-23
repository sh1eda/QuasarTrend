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

### Phase 3 — Next

Planned Phase 3 work is chronological multi-timeframe replay, no-lookahead
verification, historical backtesting, a fill model, fees/slippage, and metrics.

Later planned work includes SQLite persistence and recovery, live market data,
Telegram integration, and production hardening. These capabilities are not
implemented in the current repository.

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
│       └── strategy/           # Deterministic Phase 2 strategy state machine
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
