# Roadmap

Direction of travel, not a commitment. Nothing here promises profitability, and no version number
will be advanced because of a research result.

## Where the project is

AutoFund can research a hypothesis, validate it out of sample, test whether it can pay for itself,
refuse it if it cannot, and show all of that in a read model an operator can inspect. It has **no
certified strategy**, and that is a truthful statement about the research rather than about the
engineering.

The open question is structural. At 173 bps of round-trip friction, a measured 2.51 bps edge cannot
be monetized by any venue change: the cheapest verified retail floor in the comparison was 7.97× the
budget. So the constraint is neither the venue nor the strategy family, and the next work is about
the two things that could actually move it — a materially larger information source, or a
fundamentally cheaper execution model.

## Released

### 0.3.0 — Research Control Center ✅

The productization milestone. Eleven milestones of research capability became an inspectable
product: registries for alpha sources, strategies and experiments; evidence with provenance; the
separation of engineering state, research evidence, production authorization and current action; and
public-safe demo mode. No new strategy, and no change to any economic or risk policy.

## Planned

### 0.3.1 — Experiment Engine

Make an experiment a first-class, reproducible object rather than a script per milestone. Declared
hypotheses, frozen protocol, declared windows, recorded outcomes, and a registry that a run cannot
quietly escape. The point is not more research; it is making it harder to run research that flatters
its own result.

### 0.3.2 — Research automation

Schedule the accumulation that currently happens by hand: captures that run to their predeclared
coverage, marks that record themselves, and a registry that reports accumulating evidence where it
exists. Explicitly *not* automated decision-making — accumulation is data collection, and collecting
more data must not be able to authorize anything.

### 0.4 — Strategy lifecycle automation

Formalize the path a strategy takes from research to certification, including what evidence each
transition requires and which transitions are permitted to be automatic. Promotion to production
stays a human decision; this milestone is about making the evidence for it auditable rather than
about removing the human.

### 0.5 — Autonomous operations

Long-running operation with recovery from interruption, so an unattended session can run for days
without a supervising process. Every existing safety boundary is retained: the capital envelope, the
economic and risk gates, the single-use permit, and the rule that an unresolved order halts rather
than retrying.

### 1.0 — Stable research and execution platform

The marker for a stable interface and a platform that has operated reliably long enough to trust,
**not** for a profitable strategy. A `1.0` with no certified strategy is a legitimate release if the
platform is honest about it.

## Explicit non-goals

These are listed because they are commonly assumed of a project like this and are not going to
happen:

- **Promising or targeting a return.** No revenue, APR or profit figure appears in this
  documentation, and none will be published without evidence that supports it.
- **A language model in the financial path.** Market selection, sizing, admission and certification
  are deterministic. No model influences a financial decision.
- **Making real-money operation easier.** Convenience in this direction is a risk, not a feature.
  The friction in the Production path exists on purpose.
- **Widening the capital envelope by configuration.** The limits are code constants with tests
  around them.
- **Withdrawal, transfer, cancel or replace capability.** The API key does not need those
  permissions and the code does not request them.
- **Removing negative results from the record.** The ability to reject a hypothesis on evidence is
  the project's most defensible property.

## Known limitations

- No certified strategy, and no economically usable alpha source.
- Cross-venue dislocation research is **unmeasured**, not refuted: the capture covered 0.176 of the
  72 predeclared hours, so it could not have observed a rare event.
- Maker execution is researched and modelled but not implemented as a live execution mode.
- The microstructure capture is too short to measure markouts.
- MAE and MFE are reported as `NOT_RECORDED` where no capture exists that could produce them.
- A single exchange adapter, for a single MXN-quoted venue.
