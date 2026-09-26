# Research history

AutoFund's research record is mostly negative. That is the point, and it is published deliberately:
the ability to reject a hypothesis before it risks capital is the system's most important property.

Each entry states the question, the method and the finding. Conclusions are published; the private
datasets behind them are not. Where a number is quoted it is a scientific aggregate, not account
state.

No result here has been revised to look better than it was, and no negative finding has been
removed. Where an earlier conclusion was later corrected, both the original and the correction are
recorded.

---

## 0.1.1 — Can an authorised session trade and stop cleanly?

**Question.** Is the session lifecycle and its accounting sound enough to trust?

**Finding.** Yes, at the level of mechanics: one session ran to a normal duration stop with zero
signals, zero orders and a reconciled ledger. This milestone established that the *plumbing* works.
It says nothing at all about whether trading would be profitable, and the fixture used for
regression testing has zero orders by design.

**Evidence quality.** A single real session with no trades.

---

## 0.1.3 — Does fee-aware admission change what is admissible?

**Question.** The strategies were producing entries with a gross target smaller than the round-trip
cost of executing them. Should the system admit them anyway?

**Finding.** No. Admitting a trade whose gross target cannot cover friction is not a strategy, it is
a donation. The fee-aware economic gate (`EconomicEdgeGuard`) was introduced here.

The consequence was immediate and large: the guard rejected roughly **99.97%** of economically
admissible entries in later milestones. That is the single most important number in this history,
because it reframed every subsequent question from "which strategy is best" to "does any strategy
clear friction at all".

---

## 0.2 — Are other MXN markets better than the incumbent?

**Question.** BTC/MXN was inherited rather than chosen. Do other MXN markets offer better economics?

**Finding.** No. All four markets evaluated (BTC, ETH, SOL, XRP against MXN) failed, for the same
reason: friction. Market selection did not rescue the thesis, because friction is a property of the
venue and the envelope rather than of the pair.

---

## 0.2.1 — Are the rejections an artefact of the modelling?

**Question.** A fair objection to 0.1.3 and 0.2: perhaps the strategies were rejected because the
simulation was wrong, not because the economics fail.

**Finding.** The rejections are real. This milestone hardened the evidence: 30 days of development
and 30 days of independent holdout on real Bitso candles. Every pair still failed.

One methodological error was found and corrected *in the direction of more caution*: a near-100% win
rate on a stop-less profile was a tell that realized drawdown was structurally zero. The drawdown
check was changed to use `max(realized, unrealized)` excursion, which then correctly failed the
mean-reversion profiles. The corrected result is worse than the original, and the correction is
recorded here rather than quietly.

**Evidence quality.** Real historical development and holdout, properly separated.

---

## 0.2.2 — Does a narrower risk boundary change the reward/risk geometry?

**Question.** At 173 bps round-trip friction, a 1.0 reward/risk ratio needs
`target_bps - stop_bps >= 346`. A useful volatility-scaled stop is 400–800 bps wide. Can a tighter,
better-placed boundary make the geometry work?

**Finding.** No. The gate rejected ~99.97% of entries; only about 2 genuine opportunities per 20,000
evaluations per market survived. The one pair that reached a holdout (XRP) produced 5 round trips and
lost money.

The structural finding is that the risk gate *requires* a gross move of at least
`friction + ratio × (risk + friction)`, so the required move in bps is large while the available
volatility at these horizons is small. This is a property of the fee structure and the envelope, not
a modelling defect.

---

## 0.2.3 — Would maker orders fix it?

**Question.** The obvious next lever: taker fees dominate the cost. The account's maker fee had never
been read — `parsing.fees()` discarded it.

**Finding.** Partly, and not enough.

- The maker fee was verified GET-only from the account as **0.6000%**, against a taker fee of
  **0.7800%**.
- Fee-only round-trip floors, computed with correct fee-currency semantics (the buy fee is charged in
  base, the sell fee in quote) rather than by summing rates: taker/taker **157.84 bps**, maker/taker
  **139.45 bps**, maker/maker **121.09 bps**. Moving both legs to maker saves **36.76 bps**.
- Under the *unachievable* ceiling — maker fees **and** unconditional passive fills — **12 of 16
  trading pairs still lost money**. Only 4 pairs were positive at the ceiling, all from the same
  volatility-mean-reversion family.

The conclusion: for most pairs the obstacle is the absence of edge, not the fee. Maker execution
reduced the losses by about 24% on losing pairs and flipped nothing.

---

## 0.2.4 — Does a coarser horizon reduce friction's share of the opportunity?

**Question.** ATR scales with horizon; friction does not. A 250 bps target is ~53 ATR away at 1
minute but only ~2.4 ATR at 1 hour. Does that change the economics?

**Finding.** The mechanism is real, and it is still not enough.

Measured friction as a share of the gross opportunity, at each horizon:

| Horizon | Friction / opportunity |
|---|---|
| 1 minute | 0.4201 |
| 15 minutes | 0.3416 |
| 1 hour | 0.3176 |

So the hypothesis's first clause is **confirmed**: coarser horizons do shrink friction's relative
share (42% → 32%), and gross targets scale as predicted. But no configuration reached a holdout: in
every pair, 0 of 4 configurations cleared `MIN_ROUND_TRIPS >= 5`. The hypothesis was not refuted — it
was never carried to a test. Both facts are reported.

A separate, genuinely useful result: a bounded forward microstructure capture was built and 450
events were stored with fingerprints and retention, establishing the ingestion path for later
research.

---

## 0.2.5 — Does an entry near a real invalidation boundary help?

**Question.** The 0.2.2 finding suggested the geometry, not just the level, was wrong. Would a narrow
invalidation boundary placed at a *structural* level improve the reward/risk shape?

**Finding.** The risk *shape* was fixed and the edge still does not exist.

- 64 configurations tested, **4,374 round trips, aggregate net −757.91 MXN, 0 of 64 profitable**.
- Risk-shape failures: **0 of 64**, down from 100% previously. The boundary genuinely fixed the shape.
- But median MFE was **0** on every meaningful configuration, the target was reached **2.35%** of the
  time and the invalidation was hit **75.6%** of the time.

The entries are systematically wrong about direction. That is an alpha problem, and no change to risk
geometry can reach it. The holdout was never inspected, because the development result settled it.

---

## 0.2.6 — Does any independent information source exist?

**Question.** After five milestones of strategy work, stop writing strategies and ask the prior
question: is there *any* measurable predictive content in this market, before trying to trade it?

**Finding.** **Yes — and it is roughly 70× too small.**

A cross-market lead-lag relationship was found and survived proper testing: a follower market that
has moved far from the basket subsequently **reverses** (the direction is inverted relative to
momentum). Development rank −0.1163, validation rank −0.1083, monotone decreasing, stable across 4/4
subwindows, and it replicated on a second instrument with the same sign.

- Conditional movement in the extreme bucket: **2.51 bps**.
- Round-trip friction: **173 bps**.
- Economic headroom: **−170.49 bps**.

So the milestone answered its question positively — information does exist — and the real constraint
is unchanged and decisive. This is the finding the whole product is built to produce: *predictive is
not profitable*, measured rather than asserted.

**Methodological note.** Two measurement errors were caught here and are worth recording, because
both made the result look *better* than it was:

- A 1-minute horizon had ≥50% of forward returns at exactly zero, so an injected future leak was
  undetected in 28/28 measurements. Any 1-minute verdict is uninformative, and the system now treats
  an insensitive measurement as `INSUFFICIENT_SAMPLE` rather than reporting a verdict from it.
- A "predictive" rate of 25% under a null was traced to a fixed 0.03 association floor that had been
  chosen *after* seeing the answer. It was removed as circular.

---

## 0.2.7 — Is the product thesis feasible at any retail venue?

**Question.** If friction is the obstacle, is there a venue where it is not? Binance, Kraken and
Coinbase Advanced were compared against Bitso.

**Finding.** **`NEW_ALPHA_SOURCE_REQUIRED`.** No venue change can monetize a 2.51 bps edge.

| Venue | Taker fee | Verified fee-only floor | Finding |
|---|---|---|---|
| Bitso | 0.780% | 157.84 bps | STRUCTURALLY_UNTRADEABLE |
| Binance | 0.100% | **20.03 bps** | STRUCTURALLY_UNTRADEABLE — `minNotional` 150 MXN vs an 11 MXN cap |
| Kraken | 0.800% | 161.94 bps | STRUCTURALLY_UNTRADEABLE — zero MXN pairs |
| Coinbase Advanced | unknown | unknown | INSUFFICIENT_COST_EVIDENCE — fees require sign-in |

The decisive arithmetic, which is worth memorizing because it governs everything: a 2.5136 bps
movement needs all-in friction below **2.5136 bps at 100% capture** (1.8852 at 75%, 1.2568 at 50%,
0.6284 at 25%). The incumbent's 173.00 bps is **91.8×** that 75% budget. Even the cheapest verified
retail floor in the world — Binance's 20.03 bps — is **7.97×** the 100% budget.

Two methodological corrections were made here. Both are recorded because each would have produced a
more favourable-looking answer:

- The early-fail check tested "are all costs known?" *before* the fee floor, so a venue whose known
  floor already exceeded the ceiling was reported as `INSUFFICIENT_COST_EVIDENCE` rather than
  `STRUCTURALLY_UNTRADEABLE`. Since spread and slippage are non-negative they can only make a failing
  venue worse, so the verdict was already settled.
- Two fee-doubling conventions existed and disagreed by **1.8444 bps**: the geometric convention
  (`1/((1−buy)(1−sell)) − 1` = 157.8444) that accounts for the fee currency, and the doubled-rate
  convention (`2 × rate` = 156.0) that the historical 173 bps all-in figure used. Both are now
  selectable explicitly and both reproduce exactly.

---

## 0.2.8 — Do rare cross-venue dislocations exist that are large enough to trade?

**Question.** The last alpha source under the current thesis: rare, large, convergent dislocations
between venues.

**Finding.** **`INSUFFICIENT_CROSS_VENUE_EVIDENCE`** — not "the signal is not economic", because the
capture could not have answered the question.

A real executable order-book capture was run: 306 observations over 12.7 minutes at a median clock
skew of 0.398 s. It found **0 of 306 attributable dislocations** on any of three pairs. But the
capture covered **0.176 of the 72 hours** the protocol predeclared as the requirement, so a rare
event could not have been observed in it. The coverage gate correctly **overrode** the premature
negative conclusion.

What the capture did establish is a methodological finding worth more than the null: a 45-day candle
screen had appeared to show ETH/MXN dislocations at p99 of 171.9 bps, above the 160.39 bps
threshold. **It was an artifact.** Binance's MXN books are so thin and wide that the measured
"dislocation" sat entirely inside the reference's own bid-ask spread. ETH's reference spread was
**36.57×** Bitso's and SOL's was **399.91×**. Real best-buy headroom was 2.55 bps (BTC), 9.14 (ETH)
and 2.34 (SOL) — none within 17× of the 160.39 bps required.

Two implementation defects were caught here, the first of which was decisive:

- **A sign inversion.** The first implementation measured a Bitso buy as favourable when Bitso was
  *above* the reference, which is a bet on mean-reversion *downward*. Cross-venue **lag** alpha
  requires the opposite: the reference moves first, Bitso lags, so you buy Bitso cheap and hold for
  upward convergence. The direction is now enforced by the property's name rather than by a comment.
- **Convergence was undetectable**, because a data-integrity guard and a claim guard had been merged
  into one field. They are now separate, so a gap that *had* narrowed is not excluded.

---

## 0.2.9 (productization) — Where does this leave the product?

**Question.** Eleven milestones produced capability and evidence, most of it negative, held in
artifacts rather than in a product. What is the industrial answer?

**Finding.** The research platform is the product. What was missing was observability: an accountable
way to see what has been measured, what it means and what is authorized.

This milestone added no strategy and no alpha investigation. It added the **Research Control Center**
and established the invariant that engineering state, research evidence, production authorization and
current action are four separate facts that are never combined.

Three defects were found while building it, each of which would have misreported state:

- **Authorization failed open.** The production view used a *denylist* of bad states, so `BOOTING`,
  `RECOVERING`, `HALTED`, `ERROR` and any state a future version might add all counted as authorized.
  Replaced with an allowlist: only `RUNNING` authorizes.
- **The read model indexed its own output**, so the registry digest could never converge. This
  presents as a repository that appears to change on every refresh, which destroys the value of
  having a digest at all.
- **A safety test had gone blind.** A newer FastAPI wraps included routers in an object with no
  `path`, so an existing "no write route under this prefix" assertion had silently stopped examining
  every included route. Demonstrated by adding a POST through an included router and confirming the
  old scan could not see it.

---

## Where this stands

| Finding | Status |
|---|---|
| A predictive information source exists | **Yes** — cross-market lead-lag, two instruments, same sign |
| It is economically usable at retail friction | **No** — ~70× too small |
| Any venue makes it viable | **No** — cheapest verified floor is still 7.97× the budget |
| Any strategy is production-certified | **No — zero** |
| Price-only indicator families contain edge | **No** — 0 of 64 configurations profitable |
| Risk geometry was the limiting factor | **No** — fixing the shape revealed no edge |
| Tradeable cross-venue dislocations exist | **Unmeasured** — the capture could not have seen a rare event |

The honest summary: **AutoFund has demonstrated that it can reject a trading idea on evidence, and
has not yet found one it cannot reject.**

The next question is therefore structural rather than strategic. If a 2.5 bps edge cannot pay a
20 bps floor, the constraint is not the venue and not the strategy family. Either a materially larger
and more durable information source is required, or an execution model with a fundamentally different
cost structure is. That is what the roadmap addresses.

## Method notes

Properties this project tries to hold, learned by violating several of them:

- **A negative control is a gate, not decoration.** If an injected leak is not detected, the
  measurement is uninformative — not reassuring.
- **Development and holdout must be separated in time, and the holdout must be reachable.** A split
  whose second half contains no data is broken, not conservative.
- **A threshold chosen after seeing the answer is circular** and must be removed even when it makes
  the result more interesting.
- **Measuring the wrong quantity is worse than measuring nothing**, because it produces a plausible
  number. The MAE-from-peak and sign-inversion defects both did this.
- **The gate may only ever weaken a conclusion.** The coverage rule in 0.2.8 exists because a short
  capture cannot support a market verdict in either direction.
