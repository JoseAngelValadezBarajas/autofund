# AutoFund 0.5 / F4: Production read-only and shadow trading

## Scope and baseline

Baseline `253a372` passed 378 offline tests before implementation. F0–F3 source,
tests, fixtures and specifications remain frozen. F4 adds `observer`, `shadow`
and an entry-point dispatcher without changing Stage execution. No Stage request
was made in this certification. F4 does not implement real execution.

## Official API documentation gate

Reviewed on 2026-09-18 before implementation:

| Contract | Official source and decision |
| --- | --- |
| Production host | [Environment](https://docs.bitso.com/bitso-api/docs/set-up-your-testing-environment): fixed `https://bitso.com` |
| Permissions | [Credentials](https://docs.bitso.com/bitso-api/docs/2-generate-your-api-credentials): dedicated View balances key; exclude endpoints requiring Place orders |
| Signing | [Signed requests](https://docs.bitso.com/bitso-api/docs/create-signed-requests): nonce + method + exact path/query + empty GET body |
| Nonce | [Nonce v2](https://docs.bitso.com/bitso-api/docs/nonce-v2-rollout): reuse isolated F3 authentication primitives |
| Balance | [Account balance](https://docs.bitso.com/bitso-api/docs/get-account-balance): authenticated GET, hidden by default |
| Fees | [List fees](https://docs.bitso.com/bitso-api/docs/list-fees): account taker decimal when available |
| Metadata | [Available books](https://docs.bitso.com/bitso-api/docs/list-available-books): limits and conservative maximum public taker tier |
| Trades | [Public trades](https://docs.bitso.com/bitso-api/docs/list-trades): numeric tid, UTC timestamp, paginated reads |
| Depth | [Order book](https://docs.bitso.com/bitso-api/docs/list-order-book): complete aggregated snapshots, top 50 levels per side |
| Limits | [General concepts](https://docs.bitso.com/bitso-api/docs/general-concepts): paced reads, bounded retries and cooldown |
| WebSocket | [General](https://docs.bitso.com/bitso-api/docs/general), [trades](https://docs.bitso.com/bitso-api/docs/trades-channel), [orders](https://docs.bitso.com/bitso-api/docs/orders-channel), [diff orders](https://docs.bitso.com/bitso-api/docs/diff-orders-channel): reviewed; optional WS not implemented |

## Security boundary

`ReadOnlyBitsoTransport` validates method, host, endpoint and query before opening
HTTPX. It exposes GET requests only, uses finite timeouts, disables redirects and
environment proxies, and rejects POST/PUT/PATCH/DELETE locally. The public
allowlist is available_books/order_book/trades; the private allowlist is
balance/fees. Authentication errors close the private circuit. HTTP diagnostics
are sanitized; credentials and Authorization headers are never serialized.

Production and shadow CLI imports cannot reach Stage client, execution models or
execution ports. Only HMAC/nonce primitives are shared. Shadow execution has no
network client. There are no Production order, cancellation, withdrawal,
transfer, margin or leverage methods. Frozen Stage commands remain separate.

Private reads require all three process environment variables:

```text
AUTOFUND_BITSO_PROD_API_KEY
AUTOFUND_BITSO_PROD_API_SECRET
AUTOFUND_BITSO_PROD_READONLY_CONFIRMED=true
```

Confirmation means operator attestation: View balances enabled, Place orders,
Make withdrawals and Perform security actions disabled. Permission inspection
via API is not claimed. Private trades/open orders are excluded. A denied fees
read never triggers a permission escalation. Public shadow trading requires no
credentials. No product JSON credential loader exists. For this certification,
the operator explicitly authorized a local ignored JSON file; a temporary
launcher loaded it in memory and set credentials only within its child process.

Real balances are hidden unless `--show-balances` is supplied. They never fund
the virtual wallet or enter captures/reports. API keys, secrets, account IDs and
private transaction identifiers are excluded from shadow artifacts.

## Deterministic market and candle policy

REST polling obtains complete order-book snapshots and public trades. The first
poll seeds recent trades; later polls paginate ascending from the committed tid.
The cursor advances only after both reads succeed. Stuck/truncated pagination
fails closed. Full snapshot sequence jumps are allowed; regression or conflicting
content at the same sequence invalidates the session. No incremental WS
continuity is claimed.

Candles are UTC 1m intervals, left-closed/right-open, ordered by `(timestamp,
numeric tid)`. Duplicate IDs with identical content are ignored; contradictions
invalidate. Out-of-order arrivals within an open minute are sorted and degrade
quality. A new trade for an already closed minute invalidates. Capture begins at
the next complete minute and uses a three-second closing delay. Missing minutes
are recorded as gaps; no synthetic candles are emitted.

The unchanged `SimpleMeanReversionV0()` sees closed candles only. An intent from
candle N cannot execute in the frame that creates it. It requires a later frame,
a newer snapshot sequence and a snapshot timestamp at or after candle close.
Backlogged older signals are not executable. Strategy parameters are not tuned.

## Virtual execution and accounting

Defaults: 50 MXN initial virtual equity, maximum deployment 50% (25 MXN),
single-order budget 10 MXN including fees, extra slippage zero, maximum spread
100 bps and book age 15 seconds. F0 Wallet, CapitalManager and RiskEngine enforce
accounting and allocation. There is no short selling or exchange balance use.

BUY consumes asks; SELL consumes bids. Depth-weighted fills require sufficient
visible liquidity for the entire simulated order. Stale, wide, invalid or
insufficient books reject execution. Actual quantity/notional and metadata
limits are checked. BUY quantity is conservatively floored to eight decimal
places as a local representation policy, not an asserted exchange lot rule.
Level prices respect ticks; the weighted average need not be a tick multiple.

Fee provenance is `CONFIRMED_ACCOUNT_FEE`, `PUBLIC_FEE_SCHEDULE`, or
`CONFIGURED_ESTIMATE`. Confirmed means the rate came from an authenticated fees
read; simulated MXN fees remain estimates, not exchange fill confirmations.
Spread and depth impact are already reflected in execution prices and are not
charged twice. Gross trading P&L is before fees and after execution impact;
net P&L includes fees. Additional configured slippage is separately reported.

The default 10 MXN fee-inclusive cap may fail a 10 MXN exchange minimum
notional. This produces a non-executable micro-order; the cap is never raised
to manufacture fills.

## Persistence, replay and reports

Each bundle contains `session.jsonl`, hash-chained `market.jsonl`, derived
`shadow_journal.jsonl`, `report.json` and `manifest.json`. Writes flush/fsync;
report/manifest replacement is atomic, and an OS writer lock prevents concurrent
writers. Configuration, limits, fee provenance and strategy identity are frozen
in the session header. Network failures and invalid payload notices are durable
inputs as well as market frames.

Resume rebuilds the wallet, strategy, dedup state and pending intent from durable
inputs, validates existing derived records and recovers a missing derived tail.
Corrupt or truncated records fail closed. Ctrl+C finalizes available inputs.
Replay verifies hashes, identities, every derived record and the full semantic
report. Fingerprints cover strategy/config/data/results; operational arrival
latency is distinguished from deterministic financial state.

Session reports include virtual equity, net/gross P&L, fees, spread/depth impact,
drawdown, deployment, signals/fills/rejections, quality and parity. Daily/weekly
aggregation requires compatible book, strategy, configuration and fee source;
duplicate, overlapping or boundary-crossing sessions are rejected. Independent
session allocations are summed, not represented as compounded capital.

## CLI

```powershell
.venv\Scripts\python -m pip install -e . --no-deps
autofund bitso-prod status
autofund bitso-prod market-info --book btc_mxn
autofund bitso-prod balances
autofund bitso-prod fees --book btc_mxn
autofund shadow run --book btc_mxn --duration 1800 --closed-candles 10 --output artifacts/f4/session
autofund shadow replay artifacts/f4/session
autofund shadow report artifacts/f4/session
autofund shadow report artifacts/f4 --period weekly
```

Use `--account-fees` on run for an authorized account fee read; otherwise public
fees are used. `--estimated-fee` explicitly labels an estimate. `--resume`
continues an existing bundle; existing bundles are never silently overwritten.
Duration is seconds. The first of time limit, candle target or interruption ends
capture. Invalid data halts the session and remains invalid in replay.

## Certification on 2026-09-18

Production authentication and balances/fees reads passed after explicit operator
permission confirmation. Observed outbound methods were GET only. Write methods
were tested against local guards, never sent to Production.

The real public session `artifacts/f4/live_session` observed 103 frames and 141
trade rows from 14:11:06.239622Z to 14:22:03.754451Z (657.514829 seconds). It
closed ten candles (14:12–14:21 UTC), emitted zero intents and zero fills, and
retained 50 MXN equity, zero net P&L, zero drawdown and zero deployment. Account
fee rate was 0.0078, source CONFIRMED_ACCOUNT_FEE.

Quality is **DEGRADED**, not a benchmark candidate: 73 stale order-book snapshots
and three out-of-order trades; zero candle gaps, duplicates or contradictions.
No thresholds were relaxed. Replay is exact, result fingerprint:

```text
4042fc0265f9f6cbd21484349e838fb713d04ae06796bdc70f1edbaaf5e1ce7c
```

The frozen synthetic golden supplies both BUY and SELL with known depth and
fees: 50 MXN becomes 49.78239602 MXN, net -0.21760398 and fees 0.1978218.
It validates accounting and replay, not strategy profitability. Offline tests
also cover candle ordering, dedup, gaps, stale/wide books, insufficient depth,
write rejection, import isolation, persistence, recovery and privacy.

## Limitations and next phase

REST depth is finite and three-second candle closure assumes bounded arrival;
late contradictions halt rather than rewrite history. Metadata/fees are fixed
per session. Capture retains history in memory and journals cumulative derived
state, so storage grows with session length. No scheduler or optional 30–60
minute soak was certified. Zero live fills provide no live execution evidence;
fixture fills exercise the virtual engine. F4 proves operational read-only and
shadow behavior, not profitability or readiness to trade real money.

Final offline certification: 456 passed, zero failed, three opt-in network tests
deselected. Ruff lint and new-file formatting pass; strict mypy passes all 65
source files. Baseline tracked source/tests/specifications are unchanged. The
candidate-file secret-pattern scan passes; credentials and local certification
artifacts remain ignored and untracked. The installed 0.5.0 CLI reproduces the
live capture fingerprint above with parity PASS.

F5 may design controlled Production micro-live as a separate, explicitly
authorized phase. F4 does not enable Place orders or implement its execution path.
