# Project status

A snapshot of where AutoFund is. It is updated at each release rather than continuously, and it
describes capability and evidence — never performance.

## Status

| Dimension | State |
|---|---|
| **Engineering** | `HEALTHY` |
| **Research platform** | `OPERATIONAL` |
| **Validated information signals** | **1** |
| **Economically usable signals** | **0** |
| **Production-certified strategies** | **0** |
| **Current action** | `NO_TRADE` |
| **Live sessions authorized** | None |

## What those numbers mean

**Engineering `HEALTHY`** says the system runs correctly. It is a statement about the software and
says nothing about whether anything is worth trading. The registry reports this explicitly as
`says_nothing_about_profitability: true`, because a green process status is the single easiest thing
to misread as a good trading result.

**One validated information signal.** A cross-market lead-lag relationship predicts short-horizon
direction. It was validated on real historical data with a separated holdout, and it replicated on a
second instrument with the same sign. It is a real finding.

**Zero economically usable signals.** That same signal measures **2.51 bps** of conditional movement
against **173 bps** of round-trip friction at the incumbent venue. Its economic headroom is
**−170.49 bps**. It predicts, and it cannot pay for itself. Both facts are reported, in separate
fields, because either one alone would be misleading.

**Zero production-certified strategies.** The price-only indicator families produced **0 profitable
configurations out of 64** tested on development data. The risk-adjusted challengers reached a
holdout and lost money. No strategy has passed certification, so none is certified.

**`NO_TRADE`.** The current action is a decision, not an empty state. The gates are working.

## Research position

| Question | Answer |
|---|---|
| Does predictive information exist in this market? | **Yes** — measured, validated, replicated |
| Can it pay for itself at retail friction? | **No** — ~70× too small |
| Is there a venue where it could? | **No** — the cheapest verified floor is 7.97× the budget |
| Is the risk geometry the problem? | **No** — fixing it revealed no edge |
| Do tradeable cross-venue dislocations exist? | **Unmeasured** — the capture could not have seen a rare event |
| Is any strategy certified? | **No — zero** |

The honest summary: AutoFund has demonstrated that it can reject a trading idea on evidence, and has
not yet found one it cannot reject. See [research-history.md](research-history.md) for the milestone
by milestone record, including the mistakes that were caught and corrected.

## Production boundaries

| Control | State |
|---|---|
| Default mode | `DEMO` |
| Session authorization | `DISABLED` — no session is authorized |
| Certified opportunities | `0` |
| Multi-market production | `DISABLED` |
| Automatic promotion | `DISABLED` |
| Exchange mutations during this milestone | `0` |
| Capital envelope | 50 / 25 / 11 MXN, unchanged |

These are constants in code with tests around them, not settings. There is no environment variable
that widens the envelope or enables promotion.

## Platform capabilities

| Capability | State |
|---|---|
| Deterministic research and replay | Operational |
| Research, alpha, strategy and experiment registries | Operational |
| Evidence provenance including synthetic vs measured | Operational |
| Economic and risk gates | Operational |
| Execution with uncertain-order reconciliation | Operational |
| Financial ledger and accounting | Operational |
| Research Control Center | Operational |
| Demo mode | Operational |
| Experiment Engine | **Not implemented** — planned for 0.3.1 |

## How to verify these claims yourself

Every number above is reproducible from a clone with no credentials:

```bash
autofund demo
```

Then open the **Control Center**, **Alpha Registry**, **Strategy Registry** and **Evidence** pages.
Demo mode generates synthetic data that has the same scientific shape as the real position — one
predictive signal that is not economic, strategies that pass engineering and fail economics, a
campaign that ran healthily and produced no usable conclusion, and a `NO_TRADE` action — so the
architecture can be inspected without access to any private artifact.

Synthetic values are labelled `SYNTHETIC_DEMO` throughout, and the demo dataset is not a backtest.
