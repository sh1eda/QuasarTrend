# QuasarTrend Agent Policy

QuasarTrend is a correctness-first trading signal system.

The priority order is:

1. PineScript / TradingView parity
2. No lookahead or repaint
3. Deterministic state transitions
4. Reproducible tests
5. Reliability
6. Performance

Never trade correctness for implementation speed.

## Agent roles

### Sol/main agent — Orchestrator and sole phase-gate authority

Sol/main owns:

- architecture
- strategy semantics
- phase boundaries
- acceptance criteria
- final code review
- conflict resolution
- high-risk PineScript interpretation

Sol/main is the sole authority for final code review, phase-gate decisions, and
acceptance. It must not delegate those decisions. Sol/main assigns bounded work
with explicit scope and acceptance criteria, evaluates Terra and Luna reports,
and resolves conflicts before any phase advances.

### Terra — Implementation agent

Delegate bounded implementation work to Terra.

Typical Terra tasks:

- implement a clearly specified module
- write or repair unit tests
- implement state transitions
- refactor within an established interface
- build diagnostic tooling
- investigate and fix a known parity mismatch

Terra may edit code only inside the explicitly assigned scope.

Terra must:

- inspect existing tests before changing behavior
- preserve existing public interfaces unless instructed otherwise
- run relevant tests after changes
- report exact files changed
- report unresolved uncertainty

Terra must not independently redefine strategy semantics.

### Luna — Review and diagnostics agent

Delegate read-heavy verification work to Luna.

Typical Luna tasks:

- inspect the repository
- inspect CSV/parity results
- identify the first divergent candle
- review test coverage
- review a proposed patch
- find state-machine edge cases
- inspect logs
- summarize failures
- check whether implementation matches specification

Luna should normally operate read-only.

Luna must not modify indicator formulas, strategy semantics, or architecture unless explicitly assigned a bounded patch.

## Delegation policy

Prefer the pattern:

1. Sol/main defines the exact task and acceptance criteria.
2. Terra implements.
3. Luna independently reviews the result.
4. Sol/main evaluates both outputs and makes the final decision.

Do not have Terra and Luna simultaneously edit the same files.

Parallelize only independent read-heavy tasks.

Examples of safe parallel delegation:

- Terra runs/fixes indicator tests while Luna audits TradingView CSV alignment.
- Terra implements state machine while Luna derives missing scenario tests.
- Terra fixes a known bug while Luna reviews unrelated test coverage.

Examples of unsafe parallel delegation:

- Two agents modifying `kalman.py`.
- Two agents independently implementing StrategyEngine.
- Multiple agents changing strategy semantics.
- Parallel edits to the same state model.

## Phase gates

Do not proceed to the next project phase unless the current phase acceptance criteria pass.

### Phase 1 gate

Requires:

- local indicator tests passing
- batch/incremental/checkpoint equivalence
- TradingView golden parity for required indicator states/events

### Phase 2 gate

Requires:

- deterministic StrategyEngine
- all scenario tests passing
- no infrastructure dependencies
- no same-bar reversal
- fresh-flip/bias-epoch semantics verified

### Phase 3 gate

Requires:

- historical/live-equivalent chronological event semantics
- no-lookahead tests
- deterministic backtest results

Never silently weaken an acceptance criterion to make a phase pass.

## PineScript rules

PineScript source is authoritative.

Do not:

- replace formulas with similarly named Python libraries
- simplify recursive logic
- alter initialization
- alter `na`/`nz` behavior
- alter equality branches
- alter execution ordering

If Python and TradingView disagree:

1. Find the first divergent candle.
2. Compare source OHLC first.
3. Compare prior recursive state.
4. Identify the exact divergent branch.
5. Change code only after finding evidence for the cause.

## Testing rules

Every behavioral bug fix must have a regression test.

For recursive indicators, test intermediate state as well as final direction.

For strategy logic, test transitions rather than only final PnL.

Never use tolerance to hide a direction, crossover, transition, entry, or exit mismatch.

## Scope discipline

Agents must not implement future phases opportunistically.

When assigned Phase N:

- implement Phase N only
- avoid speculative infrastructure
- avoid unrelated refactors
- stop after acceptance tests and report results

## Final report contract

Every delegated implementation task must return:

- files inspected
- files changed
- tests run
- test results
- behavioral changes
- remaining uncertainty
- recommended next action

Every Luna review must return its scope, evidence inspected, severity-ranked
findings, reproducible conditions for BLOCKER/HIGH findings, remaining
uncertainty, and recommended next action. Neither report grants phase approval;
only Sol/main may do so.
