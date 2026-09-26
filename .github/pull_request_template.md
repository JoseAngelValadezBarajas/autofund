name: Pull request

<!--
  The questions below encode this project's safety culture. Most are ordinary; eight of them ask
  whether you have changed something that spends or protects money, and for those the answers are
  the review rather than a formality.
-->

## What this changes

<!-- One or two sentences. What was wrong or missing before, and what does this do about it? -->

## Why

<!--
  The reasoning a reviewer cannot recover from the diff. If this fixes a defect, say how the defect
  presented - the symptom matters more than the fix.
-->

## Safety questions

Answer every one. "No" is the expected answer for most pull requests.

- [ ] Does this change **financial semantics** (fees, friction, cost models, economic admission)?
- [ ] Does this change **`RiskEngine`** (drawdown policy, sizing, reward/risk rules)?
- [ ] Does this change **`EconomicEdgeGuard`**?
- [ ] Does this change the **capital limits** (50 / 25 / 11 MXN) or `DRAWDOWN_WITHIN_POLICY`?
- [ ] Does this alter **research evidence** or any certification threshold?
- [ ] Does this add an **exchange mutation** (a new POST, cancel, replace, withdraw or transfer)?
- [ ] Does this change a **frozen strategy** or its fingerprint?
- [ ] Does this **weaken a safety boundary** (demo isolation, authorization, provenance labelling)?

**If you answered yes to any of the above**, include in the description:

1. the before/after numbers, measured rather than reasoned about
2. the evidence that the new behaviour is correct
3. what would now be admitted, certified or authorized that was not before

A change that makes a strategy pass a gate it previously failed is not an improvement until it is
shown to be correct. The gate exists because something was measured.

## Evidence

<!--
  For research changes: the hypothesis declared before running, the decision rule, whether the result
  is development or holdout evidence, and what a negative result would have looked like. Commit the
  negative result too.
-->

## Data and privacy

- [ ] No credentials, API keys, `.env` contents or credential stores are included
- [ ] No real account data is included (balances, position sizes, cost bases, order identifiers)
- [ ] No large datasets or runtime artifacts from `artifacts/` are included
- [ ] Any sample data added is synthetic or explicitly redistribution-safe, and labelled

<!-- See docs/public-boundary.md. If you are unsure whether a value is personal financial state, ask
     in the pull request rather than guessing. -->

## Tests

Commands run, with results:

```
pytest:
ruff:
mypy --strict:
vitest:
playwright:
```

- [ ] Tests added or updated for the behaviour change
- [ ] All quality gates pass locally
- [ ] If this touches the execution path, the relevant certification script was run and its result is
      stated above

## Not run

<!-- List anything you could not verify, and why. An acknowledged gap is fine; an unstated one is
     not. -->
