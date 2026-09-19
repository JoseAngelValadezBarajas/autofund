# F5 — Controlled Production Micro-Live

Baseline: `91e6927 feat: add live runtime observability`.

This phase prepares one manually initiated BTC/MXN SPOT MARKET BUY. It is
execution infrastructure certification, not strategy profitability evidence.
No real POST or order is performed in the implementation cycle. F5 cannot be
reported PASS until a later human order is observed and audited.

## Production boundary and credentials

`BitsoProductionLiveTransport` pins `https://bitso.com`, disables proxy
environment inheritance and redirects, and performs no automatic HTTP retries.
The CLI offers no base URL override. Tests inject a fake transport.

GET allowlist:

- `/api/v3/balance`
- `/api/v3/fees`
- `/api/v3/available_books`
- `/api/v3/order_book?book=btc_mxn`
- `/api/v3/orders?origin_ids=<known af-live origin>`
- `/api/v3/order_trades?origin_id=<known af-live origin>`

The only application mutation is `POST /api/v3/orders`. It requires a
single-use submission permit bound to the exact request body, created after
the operator gate and durable SUBMITTING. Only the exact spot MARKET BUY
fields are accepted. Amount must be positive and no greater than the configured F5-A cap, currently 11 MXN;
slippage must be explicit, finite and between 0 and 100 percent.
PUT, PATCH, DELETE, other POSTs, arbitrary hosts, additional query fields,
unscoped order listings and unrelated books are rejected locally.

No cancellation, modification, SELL, withdrawal, transfer, margin, futures,
leverage, security or personal-profile client methods exist. Certified common
HMAC SHA-256, Nonce v2, parsing, fill accounting and fsync/locking primitives
are reused; Stage execution behavior is not imported.

Runtime credentials are exclusively the live environment variables:
`AUTOFUND_BITSO_LIVE_API_KEY`, `AUTOFUND_BITSO_LIVE_API_SECRET`, and
`AUTOFUND_BITSO_LIVE_PERMISSIONS_CONFIRMED=true`. There is no implicit F4
environment fallback and the application does not read `keys.json`.

Application capabilities and accepted exchange-key risk:

| Capability/risk | State |
| --- | --- |
| Physical key reuse | ACCEPTED_BY_OPERATOR |
| Exchange least privilege | WAIVED_BY_OPERATOR / ACCEPTED_RISK |
| AutoFund balance GET | PRESENT |
| AutoFund gated order placement | POST `/orders` ONLY |
| AutoFund withdrawals | NOT PRESENT |
| AutoFund transfers/security actions | NOT PRESENT |

Successful GET authentication does **not** prove order permission or inspect
these individual permission settings. Attestation is recorded as operator
confirmed, never API verified. PROD and LIVE keep separate environment-variable names, but the operator has explicitly approved identical physical values. Physical separation and exchange-level least privilege are `WAIVED_BY_OPERATOR / ACCEPTED_RISK`; this does not block readiness. The application still never falls back automatically between PROD and LIVE variables. The credential can have more exchange authority than AutoFund exposes. If stolen and used outside AutoFund, application allowlists cannot protect the account. F4's application transport remains GET-only.

## Financial policy and GET-only preflight

All financial inputs and outputs use Decimal, serialized as strings; UTC is
mandatory. Initial explicit policy is allocation 50 MXN, deployment fraction
0.50, initial deployment ceiling 25 MXN, and single-order cap 11 MXN. F5-A
cannot silently expand these ceilings. Full exchange balances never fund or
size AutoFund inventory.

Every preflight freshly reads private balances and account fees, public market
limits and a two-sided uncrossed order book. It checks all min/max amount,
value and price limits, retains tick size in provenance, spread <= 100 bps,
snapshot age 0..15 seconds, and ask liquidity within the selected slippage
bound. Future timestamps are evaluated against the independent, bounded clock
skew policy described below. The operator selected
`slippage_tolerance=0.5` (0.5 percent). No default optimal slippage is inferred.

The cap is interpreted conservatively as **total possible quote debit including
fees**. Before the fee currency of actual fills is confirmed, the preflight
reserves a quote fee using the actual taker rate. Candidate minor budget is
rounded down to eight decimal places from `available / (1 + taker_fee)`.
An explicit requested minor budget must satisfy the same debit cap. This may
reject an order that the exchange could execute with a base-denominated fee;
it intentionally does not assume fee currency to widen the envelope.
RiskEngine and CapitalManager independently check the isolated F0 wallet.
Current marked deployment, original ceiling and wallet cash constrain sizing.

No private full balance is printed or exposed in the dashboard. Public
preflight output includes authentication/risk/limits decisions, fee source
`CONFIRMED_ACCOUNT_FEE`, timestamp, market provenance, selected economic
policy, failures and readiness. GET-only preflight never creates an order
intent or a submission permit.

```powershell
autofund live preflight --book btc_mxn --allocated-capital 50 --max-deployment 0.50 --single-order-cap 11 --slippage-tolerance 0.5 --max-local-snapshot-age 1
```

The previous 10 MXN cap was not executable with the observed 10 MXN minimum and fee-inclusive policy. The operator explicitly raised only the single-order cap to 11 MXN; allocated capital and maximum deployment remain unchanged. Every preflight still re-reads limits and fees, and never raises the cap automatically.

### Market time semantics

The preflight records exchange `updated_at`, local wall-clock receipt time immediately after the order-book GET, monotonic receipt time and local validation time. Local monotonic snapshot age is the primary hard freshness gate. Exchange event staleness is a separate hard gate:

- A local snapshot older than `max_local_snapshot_age` blocks as `LOCAL_SNAPSHOT_STALE`.
- A past exchange timestamp older than 15 seconds blocks as `STALE_MARKET`.
- `updated_at` is a sanity signal rather than an authoritative remote clock. A future offset up to and including two seconds is `PASS_WITH_WARNING`; a larger offset blocks as `EXCHANGE_TIMESTAMP_ANOMALY_GT_2S`. The fixed ceiling is not a freshness threshold and is never dynamically increased.
- An increasing sequence passes, an unchanged sequence is allowed while the local snapshot remains fresh, and a regression blocks as `ORDERBOOK_SEQUENCE_REGRESSION`.

After exact operator confirmation, AutoFund performs a final GET-only preflight and revalidates local freshness, exchange timestamp sanity, sequence, spread, depth, limits, data quality and slippage before it can construct the one-use POST permit.

The GET-only Production preflight on 2026-09-19 retrieved minimum value 10 MXN,
minimum amount 0.0000006 BTC, tick size 10 MXN, maker fee 0.006 and taker fee
0.0078. The 11 MXN fee-inclusive cap produced maximum candidate notional
10.91486406 MXN, estimated fee 0.085135939668 MXN and estimated maximum debit
10.999999999668 MXN, so the capital/market-limit calculation was executable.
Repeated Production observations showed small positive `updated_at` offsets while local freshness otherwise passed. Under the final policy these are timestamp warnings up to the fixed two-second sanity ceiling and do not represent stale market data.

## Durable execution, recovery and accounting

One installation uses one fixed absolute live journal in
`artifacts/live/execution.jsonl`, independent of CLI working directory. An OS
exclusive writer lock covers preflight, intent creation, submission and
recovery. The hash-chained append-only journal flushes and fsyncs before
external writes. Its live namespace cannot load a Stage journal.

An allocation record initializes the isolated F0 wallet. LiveOrderIntent
contains UUID intent ID, one `af-live-` + 32 hexadecimal origin (40 characters),
UTC creation time, exact budget, envelope, fee/limits/risk/market snapshot and
fingerprint, with `intent_source=OPERATOR_CERTIFICATION`. Origins are never
reused, including after terminal orders; exchange origin uniqueness only
among active orders is insufficient for local provenance.

Lifecycle: INTENT_CREATED, PREFLIGHT_PASS, AWAITING_OPERATOR, SUBMITTING,
SUBMITTED, ACKNOWLEDGED, OUTCOME_UNKNOWN, PARTIALLY_FILLED, FILLED, RECONCILED,
HALTED. At most one unresolved intent exists. SUBMITTING is a durable
submission reservation: a crash even before actual HTTP is treated as
uncertain, and restart never sends that intent again.

Timeout, reset, rejected/malformed response or lost ACK becomes OUTCOME_UNKNOWN.
There is no blind POST retry or new origin for the same intent. GET recovery
queries known origin order lookup and always trade lookup even if the order
is absent. Three bounded query rounds are used. Missing open order does not
prove nonexistence. Known fills whose accumulated gross equals the entire
minor budget prove completion even with no open lookup result. Residual
partial outcomes reconcile only when a typed remote CANCELLED state proves
termination; its confirmed partial fills remain the complete ledger truth.
Other partial or unknown outcomes remain unresolved and
HALTED_UNCERTAIN_ORDER, requiring human investigation. No heuristic
near-equality releases capital.

```powershell
autofund live recover --slippage-tolerance 0.5
```

Startup reads and rebuilds the journal before new intents; the operational
command attempts GET recovery before preflight. An abandoned pre-POST intent
can halt safely. Any submitted unresolved intent blocks new orders until
durable reconciliation. Journal corruption, incomplete tail, lock contention
or allocation mismatch stops operation; no silent repair occurs.

Confirmed fills must match book, BUY direction, creation time, origin when
present, known order ID and economic budget. `tid` is applied once; any
contradictory duplicate halts. Validated fill records are committed before F0
application. Restart replays those committed fills with the same certified
ConfirmedFillAccounting, including recovery from crash between commit and
wallet application. Partial fills apply incrementally. Only confirmed fees
are booked. No expected fill or personal balance delta creates ledger entries.

Private before/after balance snapshots are journal evidence only. External
manual activity is not attributed to AutoFund. AutoFund BTC inventory derives
only from its confirmed fills. Future SELL safety uses RiskEngine owned
inventory; no real SELL surface is implemented. Live provenance hashes intent,
economic request fields represented by intent, market/limits/fee snapshots,
confirmed fills and F0 ledger, excluding credentials, Authorization and browser.
Historical F4 result fingerprints are unchanged.

## Human gate and monitoring

The later certification command requires an explicit exact minor budget,
`--confirm-real-money`, interactive stdin **and** stdout, and exact typed
`CONFIRM <origin_id>`. Blank, yes, wrong origin, noninteractive input, CI,
pytest, Playwright, Codex automation and auto-confirm environment markers
cannot submit. The summary shows REAL MONEY, Production, BUY, all economic
policy and snapshot age before prompting. The displayed snapshot expires at
15 seconds; expiry does not silently refresh under an old confirmation.
Browser opening and best-effort monitoring cannot bypass any gate.

No real certification command is run by the coding agent in this cycle.
Tests use injected fake exchanges and fake terminals only. A trusted operator
must initiate the first order in a later normal terminal session, after a
new passing preflight.

Operational live commands publish to the shared atomic runtime snapshot,
start/reuse the loopback dashboard, wait health and open the default browser
once. `--no-open-dashboard` suppresses browser opening. Browser/monitoring
failures never change accounting or execution rules. A separate heartbeat
publishes process liveness without inventing fresh market events; stale market
and disconnected producer semantics remain backend-defined.

RuntimeView gains mode MICRO-LIVE and a typed privacy-allowlisted own accounting
projection. Health reflects MICRO-LIVE. The SPA reads runtime and SSE directly
in this mode, never loads shadow financial data as live. It shows REAL MONEY,
AUTO EXECUTION DISABLED, terminal-only confirmation, allocation/deployment/cap,
own inventory/cash/P&L, lifecycle, ledger, session and activity. No strategy
signal or open/closed candle is fabricated. Dashboard endpoints remain GET/SSE,
without trading controls or access to the live client. DEMO DATA remains visible
when deterministic micro-live fixtures are used.

## Threat model and remaining risk

The application enforces allowlists, conservative financial limits, durable
reservation, TTY gates and read-only monitoring for trusted local Python code.
It is not a sandbox against hostile local code that imports private objects,
changes environment variables, modifies the process, steals credentials or
rewrites journal hash chains. Hash chaining detects accidental corruption,
not a malicious administrator with full filesystem access. Protect the host,
live key, journal directory and private evidence. No endpoint implementation
can remove exchange outage, quote changes, partial execution, future-clock
skew, uncertain settlement or external account activity risk.

Bitso [officially recommends its testing environment](https://docs.bitso.com/bitso-api/docs/set-up-your-testing-environment).
The operator chose not to use Stage. The selected process is deterministic
offline certification + GET-only Production preflight + a later human micro
order. This does not eliminate production risk.

Protocol references: [Place an Order](https://docs.bitso.com/bitso-api/docs/place-an-order),
[List Fees](https://docs.bitso.com/bitso-api/docs/list-fees),
[List Order Trades](https://docs.bitso.com/bitso-api/docs/list-user-trades),
[API changes](https://docs.bitso.com/bitso-api/docs/history-of-changes).

Offline gates: full pytest, ruff check ., mypy src examples; frontend npm ci,
test, lint, build and Playwright Chromium. Micro demo needs no keys or market
Internet. Two minimal micro-live goldens cover desktop and narrow; existing
F4 goldens are retained. No tolerance is widened. Traces/screenshots on failure
use the existing Playwright defaults.

F5-B autonomous strategy execution, SELL, cancellation and operator GUI commands
are outside this phase. Final readiness is determined only by a fresh GET-only Production preflight after these policies are applied.
