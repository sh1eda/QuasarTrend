# QuasarTrend Project Rules

## Mission and priorities

QuasarTrend is an XAU / XM GOLD-only automated trading system. The product is a
real, reliable, economically viable bot; validation and reproducibility are the
means to make sound engineering and trading decisions, not the product itself.
BTC is outside the active roadmap. Descriptive status documents do not override
these safety rules or authorize trading or strategy changes; report stale
documentation rather than following it as authority.

Practical priority order:

1. Prevent unsafe or unintended trading behavior.
2. Preserve known-good frozen behavior.
3. Make real-broker execution reliable.
4. Establish trustworthy forward evidence.
5. Improve economic performance through isolated V2 research.
6. Improve maintainability or performance when evidence supports it.
7. Progress toward a production-capable XAU bot.

Do not create research, optimization, or refactoring work merely because it is
possible. Prioritize work that changes a decision about correctness, execution
safety, forward reliability, profitability, overfitting, deployment readiness,
or evidenced maintainability/performance. Profile meaningful performance
bottlenecks and preserve behavior; do not perform performance theater. Do not
call alpha successful solely from aggregate backtests: separate discovery,
selection, OOS/forward evidence, and production authorization. V2 profitability
research is allowed only when explicitly authorized and isolated.

## Frozen V1 and V2 isolation

Canonical frozen V1 commit: `c58e18ef545909184267342eff712dd08bf47dda`.
V1 behavior is frozen: HEMA 20/40 on 15m and 4H; Kalman period 21, alpha .01,
beta .1, factor 1; ATR 7; 1 ATR stop; stateful either-order confirmation; 4H
bias; armed/immediate semantics; entry mechanics; bias-reversal exit; long/short
eligibility; timing; thresholds; risk semantics; fresh-flip/bias-epoch behavior;
and no same-bar reversal. Do not silently clean up, simplify, optimize, modernize,
or refactor it unless behavioral equivalence is explicitly requested and proven.

Strategy changes belong in an explicitly authorized, isolated V2 path. A V2
result is not production authorization. Family 1's `long_only` candidate is not
production V2; never remove V1 short behavior as collateral work.

The protected V1 holdout is `[2026-08-28T20:58:00Z, 2027-03-01T23:00:00Z)` under
protocol `897e20265cafe69a82d405ae65dc67f9f2f61125`. Do not use its aggregate
economics for V2 optimization or V1 directional aggregate analysis. Record
prospective prices, signals, broker costs, and execution evidence only as the
protocol permits. Historical V1 spread, swap, and slippage are UNKNOWN, not zero:
true quote evidence does not overlap the frozen trade population. Do not project
current broker costs into historical periods without explicit authorization.

## Broker and execution safety

Authorized XM demo servers are `XMGlobal-MT5 9` and `XMGlobal-MT5 18`; the latest
accepted target-environment observation is `XMGlobal-MT5 9`. Current observed XM
metadata, not historical constants: symbol `GOLD`, digits 2, point .01, contract
100, swap mode `POINTS`, long swap -96.61 points, short swap +13.11 points,
triple-swap weekday Wednesday. Exact rollover clock is unresolved until evidenced.

Live trading is unauthorized. Production/live sizing is UNDEFINED and
unauthorized. For demo execution probes only,
`DEMO_EXECUTION_VOLUME_POLICY = SYMBOL_VOLUME_MIN`; it is not production sizing,
strategy logic, a V2 parameter, or a live risk model. Any unknown or conflicting
semantic that could affect monetary exposure or order behavior blocks execution
and fails closed. Unknowns affecting only reporting must be recorded and
investigated without blocking harmless read-only work or useful unrelated
engineering. Changes that can send, resize, duplicate, reverse, or close broker
positions require narrow, explicit authorization; never enable a live order path
by inference. The current XM forward framework has no `order_send` path; do not
introduce one without explicit authorization.

## Engineering evidence

PineScript source is authoritative. Preserve source execution order,
initialization, `na`/`nz`, equality branches, and recursive state. For a
TradingView mismatch, find the first divergent candle; compare source OHLC, prior
recursive state, then the exact divergent branch before changing code. Never use
tolerance to conceal a direction, crossover, transition, entry, or exit mismatch.

Every behavioral bug fix needs a regression test. Recursive indicators require
intermediate-state checks; strategy tests must cover transitions, not only PnL.
Protect no-lookahead/repaint behavior and deterministic transitions. Investigate
suspected defects by locating the path, establishing expected versus actual
behavior, gathering concrete mismatch evidence, and determining blast radius
before an authorized modification.

Keep V1, V2, and XM forward execution logically isolated. Do not rewrite history,
retag or change canonical baselines, merge branches merely for an agent task, or
silently weaken an acceptance criterion. Add scoped `AGENTS.md` files only when a
subtree has genuinely different durable rules; avoid duplicate or conflicting
instruction layers. Detailed mutable phase state belongs in task or branch
documentation, not these permanent instructions.

## Lead and delegation

The lead agent owns architecture, semantics, acceptance criteria, phase gates,
final review, and conflict resolution. The user or host selects the interactive
root model, and repository configuration must not pin it. Choose faster or less
expensive supported subagent models when scope and risk allow. For Windows/VDS
work, prefer preparing bounded PowerShell/CLI commands for manual user execution
when GUI interaction is unnecessary; use Computer Use only when direct GUI
interaction is materially required. Use specialist subagents for independent,
read-heavy exploration when that improves evidence or latency:
codebase impact, strategy integrity, XM execution, sizing/broker semantics,
recovery, data/time, tests/invariants, logs, and adversarial review. For
high-risk execution work: explore in parallel, synthesize and decide centrally,
modify narrowly, then verify independently. Do not parallelize overlapping
write-heavy production work. Delegated conclusions are evidence, never automatic
acceptance; the lead resolves conflicts and grants final approval.

For bounded implementation, use the lead or a built-in implementation worker
with an explicit file/behavior scope and acceptance criteria. Specialists in
`.codex/agents/` are read-only auditors, not implementation authorization.

Delegated work must state scope, evidence inspected, severity-ranked findings,
reproducible conditions or concrete evidence for each BLOCKER/HIGH, uncertainty,
and a recommended action. Implementers additionally report files inspected and
changed, tests run and results, and behavioral changes. No delegate advances a
gate or grants final acceptance.
