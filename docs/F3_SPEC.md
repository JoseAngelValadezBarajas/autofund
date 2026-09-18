# AutoFund 0.4 / F3: Bitso Stage execution and reconciliation

## Baseline and scope

Baseline `3facf25` was clean with 269 offline tests passing before implementation.
F0 (`066a54c`), F1 (`2781c71`) and F2 source, tests, fixtures, demos and specifications
remain unchanged. F3 adds an isolated adapter and a root CLI dispatcher. The
financial core does not parse exchange JSON. No additional dependency is required.

F3 supports manual, explicitly confirmed Spot LIMIT/MARKET execution in Bitso
Stage, account reads, individual owned-order cancellation, confirmed fill
accounting, bounded reconciliation and durable restart recovery. It does not
connect a strategy to execution. F4 is prolonged shadow trading and operational
certification; production, real MXN and autonomous strategy execution remain out
of scope. No withdrawal, transfer, margin, futures, leverage, order replacement,
cancel-all, alternative settlement or production configuration is implemented.

## Documentation gate (reviewed 2026-09-18)

The following current official pages were consulted before private endpoint and
authentication implementation:

| Topic | Official source | Adapter decision |
| --- | --- | --- |
| Testing environment | [Testing setup](https://docs.bitso.com/bitso-api/docs/set-up-your-testing-environment) | Stage-only account and credentials |
| Signing | [Signed requests](https://docs.bitso.com/bitso-api/docs/create-signed-requests) | HMAC-SHA256 of nonce, method, exact path/query and exact body bytes |
| Nonce | [Nonce v2](https://docs.bitso.com/bitso-api/docs/nonce-v2-rollout) | 13-digit epoch milliseconds plus six random digits; no v1 |
| Metadata | [Available books](https://docs.bitso.com/bitso-api/docs/list-available-books) | Parse live limits and tick; never use sample values as runtime constants |
| Depth | [Order book](https://docs.bitso.com/bitso-api/docs/list-order-book) | Two-sided fresh depth for the certification price |
| Account | [Balance](https://docs.bitso.com/bitso-api/docs/get-account-balance) | Exact total minus locked equals available |
| Fees | [Account fees](https://docs.bitso.com/bitso-api/docs/list-fees) | Maker/taker decimal fields, never public fee-tier guesses |
| Submission | [Place order](https://docs.bitso.com/bitso-api/docs/place-an-order) | Spot limit/market; major or minor; postonly for limit certification |
| Order lookup | [Look up orders](https://docs.bitso.com/bitso-api/docs/look-up-orders) | oid or origin_ids; disappearance is not proof of nonexistence |
| Open orders | [Open orders](https://docs.bitso.com/bitso-api/docs/list-open-orders) | Complete account view; fail closed at response limit |
| Order fills | [Order trades](https://docs.bitso.com/bitso-api/docs/list-user-trades) | oid or origin_id, signed settlements and actual fees |
| Account trades | [User trades](https://docs.bitso.com/bitso-api/docs/user-trades) | Bounded pagination, marker and descending order |
| Cancellation | [Cancel order](https://docs.bitso.com/bitso-api/docs/cancel-an-order) | Only DELETE orders/{known oid}; verify returned identity |
| Rate limits | [General concepts](https://docs.bitso.com/bitso-api/docs/general-concepts) | Public 60/min and private 300/min for verified accounts; use <=60/min locally |
| Auth errors | [Authentication errors](https://docs.bitso.com/bitso-api/docs/authentication-error-02-http-401) | Auth/nonce/IP/permission failures stop execution |
| Validation errors | [Validation errors](https://docs.bitso.com/bitso-api/docs/validation-errors-03-http-400) | Preserve safe numeric code, discard remote error prose |
| Limits | [System limits](https://docs.bitso.com/bitso-api/docs/system-limit-errors-04-http-400) | Exchange constraint failures are not blind-retry candidates |
| Throttling | [Throttling errors](https://docs.bitso.com/bitso-api/docs/throttling-errors-08-http-420) | Respect cooldown, no write retry loop |

Documentation inconsistencies are explicit: some links say Stage but target
Sandbox, the general page also mentions api-sandbox, and signing examples still
show old nonce generation. The dedicated Nonce v2 specification takes precedence.
The order lookup text says terminal orders disappear, while its field table
mentions a short terminal-state retention window; recovery never relies on that
window. Error 0418 has conflicting descriptions across pages; it remains a
generic validation failure, without guessing its meaning.

Actual connectivity on 2026-09-18: `https://stage.bitso.com/api/v3/available_books`
returned HTTP 200. `https://sandbox.bitso.com/api/v3/available_books` returned 301;
the redirect was not followed. The sole runtime host is centralized as
`STAGE_BASE_URL = https://stage.bitso.com`. Redirects and environment proxies are
disabled. Production and all alternative base URLs are rejected before I/O.

The public Stage response contained `btc_mxn`, with minimum amount 0.0000006 BTC,
maximum amount 600 BTC, minimum price 100000 MXN/BTC, maximum price 40000000,
minimum value 10 MXN, maximum value 200000000 MXN and tick 10 MXN. These are dated
observations, not configuration constants. Account fees and balances were not
retrieved because authorized Stage credentials were unavailable.

## Authentication and secrets

Only `AUTOFUND_BITSO_STAGE_API_KEY` and `AUTOFUND_BITSO_STAGE_API_SECRET` are read.
No production names, credential CLI flags, dotenv loader or JSON credential
loader exist. `.env.example` contains empty names; `.env`, `*.secret`, `keys.json`
and `artifacts/` are ignored. The user's local `keys.json` was explicitly excluded
from authorization and was not used for authentication or sent to an exchange.

Expected account permissions are place orders and view balances, without broader
permissions. Credential and signed-request reprs are redacted. Network exceptions
are translated without chains or remote messages. The CLI prints error class
names, not exception text. No raw headers, signatures or credential values are
persisted. Synthetic test-only strings and the public RFC 4231 HMAC vector are
not account credentials.

Nonce v2 uses `time.time_ns() // 1_000_000` and `secrets.randbelow(1_000_000)`.
Clock/entropy are injectable; a process-wide lock and used-nonce registry cover
all providers and threads. Collisions get 32 attempts, then fail closed. The
registry is process-local and grows for the process lifetime. Independent
processes rely on cryptographic entropy, not a distributed uniqueness claim.

Requests are serialized once to compact sorted JSON bytes. Those same bytes
are signed and supplied as httpx `content`; GET/DELETE have empty bodies. Query
encoding happens before signing. Explicit connect/read timeouts are 5/10 seconds.
Reads have at most three attempts. Every request is spaced at least 1.05 seconds
within a client, including cancellation. A throttle returns immediately and sets
a cooldown of at least 60 seconds (or a longer Retry-After) for subsequent calls.
The policy is not an account-wide limiter for unrelated clients/processes.

## Domain, constraints and precision

Frozen models contain Decimal financial values and aware timestamps. JSON
financial fields must be strings; floats, nonfinite values, duplicate JSON keys,
invalid currency/sign relationships and impossible balances are rejected.
Balance equality is exact; no unspecified tolerance repairs remote accounting.
Metadata has explicit amount, price and value ranges plus tick size. Account
fee schedules use separate maker/taker decimal rates and current volume.

F1 `OrderIntent` is reused. `ExecutionInstruction` wraps it with execution type,
limit price and post-only selection; it does not introduce a competing strategy
intent. `BitsoOrderRequest` is the exchange representation after validation.

Limit prices round down for BUY and up for SELL to the live tick. Both requested
and normalized values, the tick, fee reserve and conversion decisions are in the
journal. BUY budgets include fees. A limit BUY derives major quantity from the
budget using the maximum current maker/taker rate as an estimate; a market BUY
sends a quote-denominated `minor` budget directly, not BTC calculated from a mark.
Derived values round down to eight decimal places as a conservative local
representation policy. This is not a claim that Bitso metadata publishes a lot
step or a universal amount-precision rule. Requested SELL major quantities are
preserved. Undocumented precision rules remain server-authoritative; rejection
does not trigger an adjusted resubmission.

Limit validation checks all six ranges, notional and tick. Market major requests
validate amount limits; minor requests validate value limits. No synthetic market
execution price is inserted to claim exchange constraints have been satisfied.
F0 risk uses an explicitly provided current reference mark. Slippage tolerance
is optional policy, omitted by default; when configured it uses Bitso's percentage
units (`0.5` means 0.5 percent), range 0..100, market only. No hidden tolerance.

## Capital and admission

The local Wallet starts with an allocated 50 MXN, regardless of Stage account
wealth. CapitalManager and RiskEngine remain unchanged. Deployment fraction is
at most 0.50 and reference capital at most 50; F3 additionally caps admission to
the original allocation envelope, so gains do not silently expand authorization.
The standard envelope is 25 MXN. An independent single-order cap defaults to
10 MXN. The optional integration test explicitly uses 11 MXN so a 10 MXN book
minimum can potentially fit fees; it still stops if the actual minimum plus
fees/precision exceeds this cap. No automatic cap increase or deposit request.

Only one unresolved order per journal is admitted. Threads on one engine are
serialized; independent processes opening the same journal are blocked by an
OS lock. Risk validates BUY budget and SELL ownership, exchange validation
checks the normalized request, and the account's available balance must cover
it. Prepare/dry-run cannot submit. Submit requires `confirmed_stage=True`;
the CLI requires `--confirm-stage`. Market SELL value checks are pretrade checks
at the provided mark, not guarantees of the eventual market execution price.

## Lifecycle, origin identity and uncertainty

States are SUBMITTED, ACKNOWLEDGED, OPEN, PARTIALLY_FILLED, COMPLETED, CANCELLED,
REJECTED and UNKNOWN_SUBMISSION_OUTCOME. HTTP success is acknowledgement, not a
fill. Both origin_id and exchange oid are retained and checked for contradictions.
Generated origins use `af-stage-` plus 31 UUID hex characters, length 40; the
allowed character set is checked. Logical ID equals origin_id in this phase.

The journal commits a submission record and fsyncs before POST. A timeout,
unusable acknowledgement or uncertain write becomes UNKNOWN. POST is NEVER
automatically repeated, even with the same ID: the exchange documents uniqueness
only for active orders. Up to three reconciliation passes use the same origin,
with two-second polling between unsuccessful passes. If an order or its fills
are found, identity is recovered. If not, execution remains halted. A missing
open-order entry or expired lookup is never evidence authorizing a fresh order.
There is no automatic resend path for any previously journaled logical order.

Only journal-known orders with matching namespace/oid may be cancelled. Foreign
orders are neither adopted nor cancelled. A positive DELETE response proves the
requested cancellation was accepted; reconciliation still checks fills, balance
and the remaining order view. An ambiguous cancellation leaves execution halted.
Terminal absence can be resolved by durable cancellation evidence or fills
exhausting the requested amount/value; otherwise it remains uncertain.

## Confirmed fills and F0

Order trades are the principal per-order source. Account trades discover unseen
fills and unknown AutoFund namespace activity. Fill identity is tid; identical
duplicates do nothing and contradictory duplicates halt. Original settlement
signs, side, currencies, price, time, maker status and confirmed fees are parsed.
The remote minor settlement is authoritative for cash, rather than recomputing
it from rounded displayed price. Missing actual fee never becomes an estimate
in the ledger. Positive fee rebates and third-currency fees halt as unsupported.

`ConfirmedFillAccounting` is the only F3 caller of Wallet's existing fill boundary.
Quote-currency fees affect cash; base-currency fees adjust delivered/consumed
quantity and are valued at the fill price for the ledger's MXN fee diagnostic.
Cost basis reflects actual cash paid, and SELL removes proportional existing
basis through F0. A real confirmed fee overrun is booked if F0 invariants allow,
then produces ORDER_BUDGET_EXCEEDED and halts further orders. If a fill cannot be
represented safely by F0, accounting halts instead of fabricating a balance.

Each partial fill updates accounting immediately. A candidate copy of Wallet
validates the movement before a durable fill record with expected ledger hash is
written; the original Wallet is then updated. Restart creates a fresh allocated
Wallet and replays each persisted confirmed fill once, checking its ledger hash.
A crash between append and application therefore neither loses nor duplicates
the movement. Completion is also reconstructed from durable fills if a crash
occurred before the state-transition record.

The frozen F0 private fill method retains its original `immediate paper fill`
ledger note. The F3 journal supplies the remote identity and actual execution
provenance; no claim is made that this legacy note identifies a remote trade.
F0 Wallet/Ledger remains the accounting source of truth; the journal supplies
execution evidence and deterministic reconstruction, not alternate balances.

## Journal and reconciliation

Append-only JSONL stores session policy, initial balances, requested/normalized
orders, authorization source, capital/risk evidence, limits, fees, attempt,
acknowledgement, transitions, fills, ledger hashes, halts and reconciliation.
Each record includes sequence, UTC time and a chained SHA256. Writes flush/fsync.
A truncated tail, broken hash or policy mismatch blocks startup; F3 does not
silently discard a possibly submitted order. This is accidental-corruption
detection, not an authenticated signature against malicious local rewriting.

Startup always queries remote state before new execution. Reconciliation compares
known orders, all open orders, per-order trades, account trades and balances.
Findings include MATCH, REMOTE_ORDER_UNKNOWN_LOCALLY, LOCAL_ORDER_MISSING_REMOTELY,
UNSEEN_REMOTE_FILL, DUPLICATE_FILL, BALANCE_MISMATCH, AMBIGUOUS_ORDER_STATE and
amount/state/identity contradictions. A missing fee or accounting inconsistency
produces ACCOUNTING_HALT. Dangerous discrepancies prevent new orders.

The balance method compares initial exchange totals plus the signed deltas from
our confirmed fills and fees against current totals. It never equates the full
exchange balance with the allocated Wallet. F3 conservatively checks every
currency in the snapshots: unrelated external movement can halt execution, but
is not adopted as bot capital or silently repaired. This deliberately favors
safety when attribution is ambiguous. Snapshot calls are not an atomic exchange
transaction; transient inconsistent views may halt and require another pass.

Open-order results reaching 500 entries and user-trade history exceeding ten
100-item pages are treated as incomplete, not MATCH. This bounded full-history
scan is suitable for a small Stage account, not a busy production account.
All historical AutoFund namespace activity must belong to the retained journal.
Do not run distinct journals concurrently against one account; F3 has no
distributed account lock.

MANUAL_HALT, RECONCILIATION_HALT, AUTH_HALT and ACCOUNTING_HALT block admission.
Reconciliation, lookup and safe owned cancellation remain available. A complete
matching reconciliation clears only RECONCILIATION_HALT. Other halts remain
sticky for operator investigation; this phase has no blind CLI reset command.
Auth failures also close the client's private-request circuit. A fresh client
can be created after fixing credentials for read/recovery investigation.

## CLI and certification

Install the updated editable package to refresh the root entry point:

```powershell
.venv\Scripts\python -m pip install -e . --no-deps
autofund bitso-stage market-info btc_mxn
autofund bitso-stage status
autofund bitso-stage balances
autofund bitso-stage fees btc_mxn
autofund bitso-stage order-test --single-order-cap 11
autofund bitso-stage order-test --single-order-cap 11 --confirm-stage
autofund bitso-stage reconcile --single-order-cap 11
autofund bitso-stage cancel af-stage-EXISTING_ID --single-order-cap 11
```

Credential values must be set locally in the two Stage environment variables;
never paste them into commands, documents or this repository. F2 commands still
dispatch to the untouched F2 implementation.

The finite test snapshots balances, open orders, actual account fees and market
limits. A fresh two-sided depth response determines a non-crossing BUY price,
aligned to tick. It requests the smallest valid locally representable amount
with a fee-inclusive budget, uses postonly, verifies lookup, attempts cleanup in
a finally block, and reconciles cancellation, fills and balance. Missing/stale
depth, insufficient capital or missing fees stops the test. A market fill is
optional and is not forced by this certification command.

`artifacts/f3_stage_certification.json` is sanitized. It includes schema, Stage
environment, IDs, intent/normalized request, limits, fees, states, fills, balance
deltas, reconciliation and result when available. Missing credentials produce
an explicit SKIPPED artifact with no fictitious IDs or lifecycle. Failed attempts
produce FAIL evidence. SHA256 covers semantic content, excluding local start/end
capture times; paths and hostnames are absent. Remote IDs, actual fill timestamps
and results are semantic, so distinct Stage runs need not share a fingerprint.

For this implementation run: public connectivity/metadata PASS; private account
reads and place/lookup/cancel SKIPPED because authorized Stage credentials were
not available. The user explicitly instructed not to use local `keys.json`.
Filled-order live certification SKIPPED. F3 overall remains BLOCKED on real
authenticated Stage certification, even though offline implementation passes.

## Validation and known operational limits

Default pytest excludes `live` and `stage`; F3 offline fixtures disable DNS and
remove the Stage environment variables. `pytest -m stage` is explicit opt-in
and skips without both Stage variables. Existing F0/F1/F2 tests remain intact.
Synthetic fixtures prove five golden scenarios: clean place/open/cancel;
partial A/duplicate A/B/complete; ambiguous POST found; ambiguous POST absent;
and unknown remote AutoFund order. A 50 MXN golden Wallet ends with cash 39.90,
0.010 BTC, cost basis 10.10, two unique fills and three ledger entries.

Tests cover signing bytes, nonce v2/thread reuse, host rejection, unsafe endpoints,
auth/permission/nonce errors, throttling, market/limit constraints, precision,
fees, exact balances, capital allocation, hard caps, manual orders, journal locks,
tampering, crash recovery, concurrent submissions, fee accounting, CLI dry run
and artifact fingerprints. During F3 review, two new-code defects were first
reproduced by failing tests and then fixed: unchecked remote order size and
missing completion reconstruction after a durable-fill/process-crash boundary.
No frozen phase needed a bug fix.

Final validation for this commit: 378 offline tests passed, zero failures;
three network tests deselected. The explicit Stage selection skipped its one
integration test because Stage credentials were absent. Ruff lint/format passed
and strict mypy passed on 40 F1/F2/F3 source files. Secret-pattern and executable
capability audits passed; `keys.json` is ignored and not tracked. Runtime
dependencies are unchanged. Editable installation and the root CLI entry point
were checked successfully. Build isolation uses the pre-existing setuptools
build requirement, not a new runtime dependency.

Known limits: Stage-only; MXN-quoted F0 execution; one unresolved order; explicit
operator commands; no automatic resubmission; no distributed account lock;
bounded history scan; sticky non-reconciliation halts; account-level external
activity conservatively halts; undocumented exchange amount precision can reject
a prepared order; no live market-fill certification in this run. Python APIs
are not a sandbox against malicious code modifying private internals.
