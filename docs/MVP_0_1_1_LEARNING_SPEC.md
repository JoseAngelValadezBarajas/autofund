# AutoFund MVP 0.1.1 — Learning, Postmortem & Market Opportunity Discovery

Baseline `b277a5b`. This milestone adds evidence, postmortem and read-only market
research. It does **not** change live trading behavior, the Champion parameters,
capital/risk semantics, the execution safety model or any hard bound.

## Decision evidence

Every strategy evaluation persists the deterministic inputs and decision margin
the Champion actually used. The Champion decision has exactly one implementation
(`autofund.mvp.champion.evaluate_champion`); the runner calls it to trade and the
same call returns the evidence telemetry serializes, so telemetry never
recalculates a decision and the recorded evidence cannot drift from the trade.

`STRATEGY_EVALUATED` and `NO_SIGNAL` carry: `profile_id`, `strategy_id`,
`strategy_version`, `evidence_version`, `strategy_fingerprint`, `market`,
`market_regime`, `closed_candles`, `position_open`, `eligible`, `features`,
`thresholds`, `decision`, `reason_code`, `distance_to_signal`,
`distance_to_signal_bps`, `distance_semantics`, `distance_units`,
`distance_version` and `near_signal`. Features and thresholds are the real values
of this strategy (mean, boundaries, average cost), not invented ones. No feature
the strategy does not use is recorded, and no explanation is generated.

Reason codes are canonical to this implementation: `NO_CLOSED_CANDLE`,
`INSUFFICIENT_HISTORY`, `ENTRY_CONDITION_NOT_MET`, `EXIT_CONDITION_NOT_MET`,
`SIGNAL_BUY`, `SIGNAL_SELL`.

## distance_to_signal semantics

Version `autofund.distance-to-signal.v1`. Definition:

```
distance = (close - boundary) / boundary
```

`boundary` is the decision boundary the active branch actually uses:
`mean * (1 - entry_threshold)` when flat, `average_cost * (1 + exit_threshold)`
when holding. Semantics
(`NEGATIVE_BOUNDARY_EXCEEDED_ZERO_AT_BOUNDARY_POSITIVE_REMAINING`):

| value | meaning |
| --- | --- |
| negative | the boundary has been exceeded; the condition is met and a signal fires |
| zero | exactly at the boundary |
| positive | remaining distance before the condition can be met |

Units are a fraction of the boundary price
(`FRACTION_OF_DECISION_BOUNDARY_PRICE`); `distance_to_signal_bps` is the same
value in basis points. It is `null` when the evaluation is not eligible (no
closed candle, or insufficient history). `near_signal` uses the documented
reporting threshold `0.001`, which never influences a trading decision.

## Post-session semantics

A completed session persists `started_at`, `ended_at` and a canonical
`stop_reason` (`MAX_SESSION_DURATION_REACHED`, `MAX_SESSION_ORDERS_REACHED`,
`MAX_SESSION_LOSS_REACHED`, `OPERATOR_STOP`, `KILL_SWITCH_ACTIVATED`,
`EXECUTION_FAILURE`). `actual_runtime_seconds = ended_at - started_at` and is
frozen: once terminal, elapsed time never advances again. The backend is
authoritative and the UI never infers a stop reason from elapsed time.

Health is separated into four independent concepts:

- **application/control plane** — `ONLINE` while the process serves;
- **trading runtime** — `RUNNING`, or `INACTIVE` when deliberately stopped;
- **market stream** — `ACTIVE`, or `INACTIVE` when deliberately stopped;
- **historical last-known market state** — retained as a fact.

A normally stopped session reports runtime `INACTIVE`, market stream `INACTIVE`
and the final observed market quality, and is **not** `STALE`, `INVALID` or
`DEGRADED`. `OBSERVABILITY DEGRADED` applies only to a RUNNING session that loses
both internal heartbeat and operator publication.

## Pipeline semantics

| status | meaning |
| --- | --- |
| `WAITING` | an active RUNNING pipeline may still reach this stage |
| `PASS` / `REJECT` / `FAIL` | the stage was actually executed |
| `NOT_APPLICABLE` | a completed decision path terminated before this stage was needed |
| `UNKNOWN` | genuine inability to determine state |

On completion, stages after the last executed stage become `NOT_APPLICABLE`. A
completed `NO_SIGNAL` path therefore reports `CAPITAL`, `RISK`,
`FINAL_MARKET_CHECK`, `ORDER`, `FILL`, `RECONCILIATION` and `LEDGER` as
`NOT_APPLICABLE`, never `UNKNOWN`.

## Postmortem artifacts

`report.json` (`autofund.session-report.v2`) is a complete deterministic record:
identity, time (with `stop_reason` and actual/configured runtime), market (events,
candles, quality transitions, latency, spread), strategy (evaluations, eligible
evaluations, reason distribution, distance summary, near-signal count, regime
distribution), execution, risk, financial and operations, plus a `scanner`
namespace.

`checkpoint_summary.json` (`autofund.checkpoint-summary.v2`) aggregates by event,
component and level, including session lifecycle, strategy reason distribution,
execution lifecycle with an `execution_exercised` flag, risk rejections, market
quality transitions, learning checkpoints, latency and stop reason.

`handoff.json` (`autofund.handoff.v2`) carries structured sections:
`what_happened`, `stop_reason`, `strategy_observations`,
`execution_observations`, `risk_observations`, `market_observations`,
`telemetry_observations`, `learning_observations` and
`candidate_improvement_signals`.

`candidate_improvement_signals` are deterministic facts, never advice:
`ZERO_SIGNALS_ACROSS_ELIGIBLE_EVALUATIONS`,
`N_EVALUATIONS_WITHIN_DISTANCE_OF_ENTRY`, `EXECUTION_PATH_NOT_EXERCISED`,
`MARKET_DATA_DEGRADATIONS`, `MARKET_SCANNER_EVIDENCE_UNAVAILABLE`. No
natural-language speculation is emitted.

## Learning lifecycle

At session completion, session evidence flows to `AdaptiveEngine.observe_session`
and is checkpointed as `LEARNING_OBSERVATION`. The engine consumes strategy
evidence, market context, risk outcomes, P&L, fees, distance statistics and
scanner evidence.

**Insufficient evidence is a valid result.** With fewer than
`MIN_SESSIONS_FOR_CHALLENGER` (5) observed sessions and no signals, the engine
classifies `INSUFFICIENT_EVIDENCE` and creates no challenger. It never fabricates
one.

The AdaptiveEngine **cannot** change: authorized capital, max deployment,
single-order cap, loss-limit hard bound, kill switch, Bitso permissions, the
exchange mutation allowlist, RiskEngine hard limits, accounting, the journal, or
the no-blind-retry invariant. No LLM decides BUY/SELL and no generated code
controls real trading.

## Champion / Challenger

The Champion is the Production-certified profile and is fixed for the duration of
a session; it can never change mid-session. Challengers are research/shadow only
and may never trade real money. Auto-promotion is `OFF`, promotion is `MANUAL` and
between sessions only.

## Market Opportunity Scanner (read-only, shadow only)

The scanner discovers the MXN universe dynamically from the exchange's own
available-books response, filtered to `*_mxn` because AutoFund's envelope and
ledger are MXN-denominated. Nothing is hardcoded and no cross-currency accounting
is introduced.

It uses the F4 strict read-only observer (`GET` only). The live trading client is
deliberately left locked to `btc_mxn` so widening research capability cannot erode
the Production trading boundary.

### Eligibility filters

Applied before ranking, so a volatile but untradable market cannot rank highly:
`INVALID_DATA`, `STALE_DATA`, `INELIGIBLE_MINIMUM`, `INELIGIBLE_CAP`
(minimum order above the 11 MXN single-order cap — the cap is never raised to make
a market eligible), `INELIGIBLE_SPREAD` (>100 bps), `INELIGIBLE_DEPTH`
(<11 MXN top-of-book), `MISSING_FEE_DATA`, `UNSUPPORTED_ACCOUNTING`.

### MarketOpportunityScore

Version `autofund.market-opportunity.v1`. Scores tradability-adjusted opportunity,
never raw volatility. Components are bounded in [0, 1] and produced with Decimal
arithmetic (no binary floats for financial or score values):

```
opportunity = 100 * ( w_movement*movement      + w_volatility*volatility
                    + w_liquidity*liquidity    + w_depth*depth
                    + w_spread*spread_cost     + w_fee*fee_cost
                    + w_slippage*slippage      + w_quality*data_quality )
              / sum(weights)
```

| weight | value |
| --- | --- |
| movement | 0.20 |
| volatility | 0.10 |
| liquidity | 0.10 |
| depth | 0.15 |
| spread_cost | 0.20 |
| fee_cost | 0.10 |
| slippage | 0.05 |
| data_quality | 0.10 |

Friction components are penalties: their score falls as cost rises. Weights are
transparent, conservative constants that were deliberately **not** tuned on any
observed session, and may only change with a version bump.

Round-trip friction (`autofund.round-trip-friction.v1`) estimates spread cost,
taker fee on both legs, entry and exit slippage, and exposes its assumptions
(`BOTH_LEGS_CROSS_TOP_OF_BOOK`, `TAKER_FEE_BOTH_LEGS`,
`SLIPPAGE_ESTIMATED_FROM_TOB_DEPTH`, `CHARGES_FOR_ONE_ROUND_TRIP`).
`friction_coverage` reports observed movement divided by estimated round-trip
friction; below 1 the movement cannot pay for a round trip.

### Shadow evaluation and strategy compatibility

Top eligible candidates feed shadow evaluation and record candles, evaluations,
signals, fees, slippage, gross/net simulated P&L, drawdown, signal rate and
distance statistics. The Champion is certified only for `btc_mxn`
(`CHAMPION_SUPPORTED_MARKETS`); every other market is explicitly
`RESEARCH_ONLY`, never presented as comparable live evidence.

### Scanner vs Production isolation

The scanner is research functionality beside Production:

- it cannot create a Production order intent;
- it cannot call `POST /api/v3/orders`;
- it cannot change the live market, the strategy market or capital routing;
- it cannot change the Champion;
- a shadow candidate cannot reach the live ExecutionEngine;
- a scanner failure cannot modify the ledger and cannot halt a valid live session.

Scanner telemetry: `MARKET_SCAN_STARTED`, `MARKET_SCAN_COMPLETED`,
`MARKET_CANDIDATE_ELIGIBLE`, `MARKET_CANDIDATE_REJECTED`, `MARKET_SHADOW_STARTED`,
`MARKET_SHADOW_EVALUATED`, `MARKET_SCANNER_DEGRADED`. Refresh cadence is
configurable (`--scan-interval`, default 300s) and a failure reports
`MARKET_SCANNER_DEGRADED` while Production stays isolated.

### Market Scanner page

For each candidate the page shows rank, book, last/bid/ask, movement, realized
volatility, high-low range, spread bps, volume, depth, minimum order, estimated
fee, estimated round-trip friction, data quality, current-cap executability,
opportunity score, individual components and status/rejection reason. It shows no
BUY/SELL and no recommendation language, and ranks research candidates, not
financial actions.

## Real-money market immutability

The MVP 0.1.1 Production market remains **BTC/MXN**. Automatic market rotation,
scanner-to-live-order paths and dynamic Production asset switching are
**DISABLED**. Promotion of another market to real-money eligibility is
`DISABLED` / a future manual milestone. **Multi-market real trading is not
enabled.**

## Determinism

Identical market data, configuration, strategy and scanner configuration produce
identical strategy evidence, eligibility, scores, ranking, shadow decisions,
fingerprints and reports. Research computation has no hidden wall-clock
dependence beyond explicit market timestamps and runtime provenance.
