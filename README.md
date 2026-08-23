# QuasarTrend — Phases 1–2

This repository currently contains only incremental, dependency-free Python ports
of the supplied TradingView PineScript indicators:

- HEMA Trend
- Kalman Step Signals (Kalman filter plus its custom Supertrend)

The indicator implementation preserves Pine initialization, `na`/`nz`, recursive
state, call-site isolation, and crossover semantics.

Phase 2 adds a dependency-free, pure historical `StrategyEngine`. Callers retain
one immutable `StrategyState` per symbol and pass each finalized 15m
`StrategyBar` (including its finalized 4H bias snapshot) to
`StrategyEngine(config).step(state, bar)`. The result contains the next state and
deterministic domain events; it has no exchange, network, filesystem, database,
async, scheduler, or execution dependencies.

The default and only implemented confirmation behavior is
`STATEFUL_EITHER_ORDER`: a fresh HEMA flip in the active 4H bias epoch is always
required, while Kalman can already agree or agree later through a pending setup.
`SAME_CANDLE` and `HEMA_THEN_KALMAN_EVENT` are exposed configuration enum values
for the public interface but are intentionally rejected by the Phase 2 engine.
This avoids silently assigning semantics to modes outside the approved scope.

Run the local parity tests with:

```bash
uv run pytest -q
```

The TradingView CSV comparison is an external acceptance gate. See
`tests/golden/README.md` and `tools/compare_tradingview.py`.
