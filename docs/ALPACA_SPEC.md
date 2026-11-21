# AutoFund A0 — Alpaca: low-turnover equity research, paper-only

Status: **DRAFT specification. No code exists for any section below.**

## Why this exists

Every economic conclusion so far was reached against one venue. The validated lead-lag signal
measures ~2.51 bps of conditional movement against ~173 bps of round-trip friction at the incumbent
venue (`status.md`, `venue_economics.py`). The signal is real and cannot pay for itself there.

That is a statement about the *venue*, not only about the signal. The question this milestone asks
is therefore narrow and falsifiable:

> Is there a hypothesis, at a horizon and on a venue where friction is a small fraction of the
> expected movement, that survives AutoFund's gates?

The expected answer is **no**, and `NO_TRADE` remains a valid and complete outcome. This milestone
succeeds if it produces trustworthy evidence either way.

## Non-goals

- No live trading. No production endpoint. No order submission to anything but the paper API.
- No new alpha claims. This milestone adds a data source, a venue profile and pre-registered
  hypotheses. It does not add a strategy that is presumed to work.
- No relaxing of any existing gate to make a result pass.
- No intraday strategies. They are explicitly deferred (see *Deferred*).

## Scope and phases

Each phase ends in something verifiable and is independently useful. A later phase must not start
until the earlier one is certified.

| Phase | Deliverable | May it mutate anything? |
|---|---|---|
| **A1** | Historical daily bars → `Candle`, GET-only data client, provenance | No |
| **A2** | Alpaca venue-economics profile, measured spreads | No |
| **A3** | Pre-registered hypotheses evaluated through the existing gates | No |
| **A4** | Paper-only forward capture and shadow journal | Paper account only |

## Security boundary

Mirrors the Bitso client's posture and is enforced in code, not by convention.

- **Fixed hosts.** A1/A2 permit only the Alpaca market-data host. A4 additionally permits only the
  paper trading host. The live trading host is **not representable**: it is not in the allowlist and
  not a configuration value. Adding it is a separate, reviewed change.
- **Endpoint allowlist.** A regex allowlist of path shapes per method, as in
  `exchanges/bitso/client.py`. A1/A2 allow `GET` only. A4 additionally allows the minimum paper order
  endpoints, and nothing else.
- **No float.** Prices and quantities are parsed from the response string into `Decimal`.
- **Sanitized errors.** Keys, secrets, account identifiers and raw response bodies never appear in
  exceptions, logs, journals or artifacts.
- **Credentials from the environment only.** Never from a tracked file. `.env.example` documents the
  variable names. A secret-scan test must keep passing.
- **Separate credentials.** Paper keys cannot be used against the live host and the client must fail
  closed if it detects a mismatch.

## A1 — Historical data

### Contract

`exchanges/alpaca/` provides `client.py`, `parsing.py`, `models.py`, `errors.py`, laid out like
`exchanges/bitso/`.

- Daily bars for an explicit symbol list and date range, paginated, deterministic ordering.
- Output is `CandleSeries` with a new source constant `ALPACA_HISTORICAL` in
  `mvp/historical.py`. It is real market data, so `is_real_market_data` is true, and the dataset
  fingerprint covers symbol, adjustment mode, feed and every bar.
- **Adjustment is explicit, never defaulted.** Each request states raw, split-adjusted or
  split-and-dividend-adjusted. Provenance records which. A series fetched with one mode must not be
  silently merged with another.
- **Feed is recorded.** The free IEX feed covers only part of consolidated volume; the consolidated
  SIP feed is paid. Provenance records the feed, and results obtained on IEX are labelled as such.
- **Gaps are never interpolated.** A missing trading day is counted in `gaps`, matching the existing
  captured-candle policy. No external calendar is used. A weekday that some requested symbol traded but this one lacks is a
  confirmed gap; a weekday no requested symbol traded is reported separately as
  `unclassified_weekdays` and is never counted as a gap. A single-symbol request therefore cannot
  claim a gap, which is the honest limit of the evidence.
- `market/models.py` currently accepts only `1s` and `1m` intervals and the F2 symbol rules. A1 does
  **not** widen that module; daily bars take their own path so the F2 guarantees stay untouched.

### Required tests

- Recorded fixture responses; **no network in the test suite.**
- Pagination, empty range, partial range, duplicate bar, out-of-order bars.
- Split and dividend fixture proving adjusted and raw series differ and carry different fingerprints.
- Allowlist rejects every non-GET method and every unlisted path.
- Error text never contains the key, the secret or the response body.
- Fingerprint is stable across runs and changes when any bar changes.

### Certification

A1 is certified when a fresh clone with **zero credentials** passes the full suite, and a
credentialed read of one real symbol reproduces the fixture-derived fingerprint for an overlapping
window.

## A2 — Venue economics

Adds an Alpaca profile to `mvp/venue_economics.py` using the existing provenance vocabulary.

| Component | Value | Provenance |
|---|---|---|
| Commission | per Alpaca's published schedule for the account type | `PUBLISHED_BASELINE`, or `ACCOUNT_RATE_UNKNOWN` until confirmed |
| Regulatory fees on sells | published SEC/FINRA fees | `PUBLISHED_BASELINE` |
| Half-spread | **measured** from recorded quotes per symbol and per time of day | measured, with sample count |
| Slippage | declared assumption, scaled by order size vs. displayed size | `UNVERIFIED` until measured against paper fills |

Rules:

- An unknown component propagates as unknown. It is never replaced by zero.
- Spread is measured, not quoted from memory, and the measurement window and sample count are stored.
- The capture ladder (100/75/50/25 %) and both fee conventions are reused unchanged.
- Eligibility and any account-type fee differences for a Mexico-resident account are verified against
  current official Alpaca documentation and recorded with a retrieval date. They are not assumed.

## A3 — Pre-registered hypotheses

The failure mode this milestone must guard against is searching until something passes. Therefore:

1. **Register before looking.** `docs/` gets a registration record per hypothesis fixing the universe,
   signal definition, parameter grid, holding period, cost model, split dates and pass thresholds.
   The record is committed *before* any result is computed. Its commit hash is stored in the
   evaluation artifact.
2. **Count every trial.** The number of configurations tried is recorded and used to adjust
   significance, as the earlier "0 of 64" accounting did. A hypothesis with a large grid pays for it.
3. **Holdout stays sealed.** The holdout window is declared in the registration and evaluated once.
4. **Net of costs, always.** The headline metric is net of the A2 cost model at each capture level.
   Gross results may be reported but never as the pass criterion.
5. **Separate the two facts.** As in `venue_economics.py`, predictive information and tradeable
   return are separate fields.

### Candidate hypotheses

Chosen because they are low-turnover, widely documented, and cheap to test honestly. None is
presumed to work.

| ID | Hypothesis | Turnover |
|---|---|---|
| H-A1 | Time-series momentum on a small liquid ETF set (e.g. broad equity, long bonds, gold) with a long lookback | Monthly |
| H-A2 | Trend filter with volatility targeting on the same set | Weekly–monthly |
| H-A3 | Sector-ETF rotation by trailing relative strength | Monthly |

A buy-and-hold benchmark and a random-signal control with the same turnover are evaluated under the
identical cost model. A hypothesis that does not beat the random control does not pass.

### Gate outcome

Results flow through the existing certification gates. Valid terminal states are the existing ones,
including `NO_TRADE`. A passing hypothesis earns **only** the right to proceed to A4 paper capture,
never to live.

## A4 — Paper forward capture

- Runs against the Alpaca paper API only, through a client with a paper-host-only allowlist.
- Signals are generated on closed daily bars. Orders are submitted only to the paper account.
- Every signal, order, fill and reconciliation event is journaled using the existing journal and
  reconciliation patterns. The MVP 0.1.2 lesson applies: **a completed order's evidence may lag**, so
  reconciliation is a bounded window, not an attempt count.
- Paper fills are optimistic relative to reality (no queue position, no market impact). They are
  therefore compared against the A2 slippage assumption and reported as a *lower bound* on real cost,
  never as proof of capturability.
- Forward capture must run for a pre-declared minimum duration before evaluation. Peeking early
  invalidates the run.
- Kill and halt semantics reuse the existing control plane.

## Operational constraints to verify (not assumed)

Recorded here so they are checked against official sources during A2, not discovered later.

- Whether the account type available to the operator can trade US equities and ETFs, and from which
  jurisdictions.
- Pattern-day-trader rules and minimum equity. These do not apply to a monthly or weekly strategy
  but constrain anything intraday.
- Tax treatment of US-source gains and dividends for the operator's residence. This is outside
  AutoFund's scope and is flagged so the net-of-cost figures are not mistaken for after-tax figures.
- Market-data licensing terms for storing and redistributing bars. Captured data in `artifacts/` must
  not be republished if the terms forbid it.

## Evidence and provenance

Every artifact produced under this spec carries: data source, feed, adjustment mode, dataset
fingerprint, hypothesis registration commit, trial count, cost-model version and
`is_real_market_data`. Synthetic fixtures remain labelled `SYNTHETIC_FIXTURE` and are never evidence
about profitability.

## Deferred

- Intraday and any high-turnover strategy.
- Crypto on Alpaca (different fee structure; revisit only with a hypothesis that needs it).
- Any live session. It would require its own spec, a new allowlist entry, and explicit human
  authorization, consistent with the existing session-authorization model.
- Options, shorting and margin.

## Definition of done

1. A1 certified from a fresh clone with zero credentials.
2. A2 profile reports per-symbol friction in bps with provenance for each component.
3. A3 registrations committed before results; every evaluation reports trial count and net-of-cost
   metrics; the random control and benchmark are present.
4. The honest outcome is written to `docs/status.md` and `docs/research-history.md`, including a
   negative one.
5. The suite, ruff, mypy and the secret scan pass in CI.

## Implementation status

| Phase | State |
|---|---|
| A1 | Implemented: GET-only data client, provenance fingerprints, `.env` loader (paper keys only) |
| A2 | Implemented: `mvp/alpaca_economics.py`, `scripts/alpaca_measure_spreads.py` (run during US market hours) |
| A3 | Implemented: `mvp/daily_momentum.py`, `scripts/alpaca_evaluate.py <id>` (one holdout run per registration) |
| A4 | Implemented as the paper lab: `autofund lab` (paper client, planned and idempotent orders, journal, kill) |

**Data-plan facts learned from real reads.** The free IEX feed returns daily history only from
mid-2020. The free plan serves consolidated SIP *history* from 2016 but refuses SIP data from the
last day or so (HTTP 403, "subscription does not permit querying recent SIP data"); the lab steps
back a day at a time until the plan accepts, and the bars returned state the date they run to.

**Where personal data goes.** Registrations, evaluation results, captured quotes and paper journals
live in the git-ignored `research/` directory, never in this repository (see `public-boundary.md`).

**Enforced in code.** `scripts/alpaca_evaluate.py` refuses to run unless the registration is tracked
and unmodified in the private research repository, and refuses to overwrite an existing result.
