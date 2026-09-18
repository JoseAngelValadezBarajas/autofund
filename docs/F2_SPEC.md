# AutoFund 0.3 - F2: Public Market Data + Capture/Replay Parity

**F2 HAS NO ORDER CAPABILITY.**

## Baseline and scope

Certified before any code change: HEAD `2781c71`, clean workspace,
182 tests passing. F0 (`066a54c`) and F1 source, tests, specifications and demos
remain unchanged. All new runtime code lives in `autofund.market`.
No frozen-contract bug fix or refactor was needed.

One Binance Spot symbol and one UTC interval per session. Supported F2 intervals
are deliberately limited to `1s` and `1m`. Public metadata and kline streams,
quality validation, raw/normalized capture, offline playback and shadow decisions
are included. Authentication, accounts, balances, exchange orders, exchange fills,
trade execution, testnet orders, FX conversion and financial state are excluded.

## Official documentation gate (reviewed 2026-09-18)

Only Spot documentation was used, not Margin, Futures, Options or unofficial SDKs.
The implementation was preceded by reading these official sources:

- [Public market-data-only URLs](https://developers.binance.com/en/docs/products/spot/faqs/market_data_only):
  data-api.binance.vision and data-stream.binance.vision are unauthenticated,
  public-data services; user data streams are unavailable on the stream host.
- [Spot exchange information](https://developers.binance.com/en/docs/catalog/core-trading-spot-trading/api/rest-api/general):
  GET /api/v3/exchangeInfo, symbol parameter, current Spot availability and filters.
  Documented request weight is 20; the adapter caches metadata for a session.
- [Spot symbol filters](https://developers.binance.com/en/docs/products/spot/filters):
  PRICE_FILTER tickSize, LOT_SIZE stepSize/minQty/maxQty, MIN_NOTIONAL and NOTIONAL.
- [Spot UTC kline contract](https://developers.binance.com/en/docs/catalog/core-trading-spot-trading/api/ws-streams/~):
  raw subscription path /ws/{lowercase_symbol}@kline_{interval}; UTC `1s` and `1m`
  are supported. Update cadence is 1 second for 1s, 2 seconds for other intervals.
  Financial fields are strings; x distinguishes a closed candle. Default timestamp
  unit is milliseconds, and no microsecond query option is requested.
- [Stream lifecycle and limits](https://developers.binance.com/en/docs/products/spot/web-socket-streams):
  expect a disconnect after 24 hours and reconnect on serverShutdown. Server Ping
  every 20 seconds requires a matching Pong within one minute. The documented
  incoming-message limit is 5/second (control frames included); connection attempts
  are limited to 300 per 5 minutes per IP. F2 uses one raw URL subscription and
  bounded retries, without application SUBSCRIBE messages or client Ping traffic.
- [Spot REST limits](https://developers.binance.com/en/docs/products/spot/rest-api):
  HTTP 429 requires backoff; 418 indicates a ban. Retry-After supplies seconds.
  F2 never shortens this cooldown; a cooldown beyond its configured wait budget
  stops the request instead of retrying early. Other applications sharing the IP
  are outside this single-process observer's control.

Library contracts were checked against official
[HTTPX async documentation](https://www.python-httpx.org/async/) and
[websockets asyncio client documentation](https://websockets.readthedocs.io/en/stable/reference/asyncio/client.html).
The latter owns WebSocket control-frame responses and closes connections on
context-manager exit. The F2 wrapper exposes receive only.

## Public interfaces, availability and dependencies

The complete production network surface is:

- `GET https://data-api.binance.vision/api/v3/exchangeInfo?symbol=<validated symbol>`.
- `wss://data-stream.binance.vision:443/ws/<lowercase symbol>@kline_<interval>`.

URLs are constants, not user-provided endpoints. Symbols are validated uppercase
ASCII alphanumeric tokens. There is no generic HTTP request method exposed to
callers, no JSON WebSocket sending method, no auth/header/credential configuration,
no signing function and no secret environment-file loader. HTTP redirects and
environment proxies are disabled; TLS certificate validation remains enabled.

A real public lookup on the implementation date returned BTCMXN, TRADING,
base BTC, quote MXN and isSpotTradingAllowed=true. Therefore BTC/MXN was retained;
no fallback or currency relabeling was needed for certification. Metadata is
verified again on each new source instance; availability is never assumed from
this document. Unknown/non-trading/non-Spot symbols fail fast. If BTCMXN becomes
unavailable, an operator may explicitly choose another verified Spot symbol;
F2 never silently switches a requested market during a session.

Direct dependencies added: HTTPX `>=0.28,<0.29` (tested 0.28.1) for bounded async
HTTP, and websockets `>=16,<18` (tested 17.1) for WebSocket framing/TLS/control
frames. No Binance SDK, CCXT, dotenv or extra test framework was added.
Installed transitive runtime packages: anyio 4.15.1, httpcore 1.0.9, h11 0.16.0,
certifi 2026.7.22, idna 3.20; typing_extensions 4.16.0 was already installed.
The F0/F1 modules do not import these network packages.

## Architecture and metadata

PublicTransport (fixed GET + receive-only socket) -> BinancePublicMarketDataSource
-> Binance normalizer -> SourceItem domain envelope -> ObservationPipeline.
PublicMarketDataSource exposes get_market_info and stream_candles. A future source
can implement these without changing the strategy. RecordedMarketDataSource uses
the same interface and requires no network connection.

Only binance_normalizer interprets exchange JSON. Metadata is an immutable
MarketInfo with symbol, base, quote, status, price tick, quantity step, quantity
bounds and notional bounds. All numbers are finite Decimal, never floats. Missing
filters/fields stay None; explicit zero stays Decimal zero. If MIN_NOTIONAL and
NOTIONAL both provide a lower bound, the reported minimum is their maximum.
Other filters are not interpreted as execution permissions; raw metadata remains
in the audit bundle. F2 does not enforce order filters because it cannot trade.
Malformed required metadata, duplicate filters and wrong/non-Spot symbols fail fast.

LiveCandleUpdate contains market, interval, open/close times, Decimal OHLCV,
is_closed and source_event_time. Financial bounds reuse F1 Candle validation.
Its opening must align to the interval; its close boundary must be
open + interval - 1 millisecond. Event time cannot precede the opening, or the
closing boundary for a closed candle. Both outer and nested symbols must match.
NaN, infinities, floats/numeric JSON financial fields, malformed JSON and duplicate
JSON keys are rejected. Unexpected control messages do not become candles.
The documented serverShutdown message is captured and triggers reconnection.

## Time, partial updates and sequence validation

Exchange millisecond integers become aware UTC datetimes without float conversion.
Local received_at is separate from source_event_time and is never a candle label.
Clock supplies now, monotonic and async sleep for operational logic; SystemClock
contains the only real-clock calls. Tests use a FakeClock and no long sleeps.

All received application messages are raw-captured. Every successfully normalized
update, including partials and duplicates, is also captured in arrival order.
Only a newly accepted CLOSED candle reaches the strategy. The test with 100
partials plus one close delivers exactly one strategy candle.

Identity = (market, interval, open_time). Classification:

| Condition | Policy | Research quality |
| --- | --- | --- |
| Coherent ordered update | Observe; deliver only if closed | VALID |
| Identical repeated close | Count duplicate, do not redeliver | VALID |
| Same closed identity, changed OHLCV/boundary | Stop; no overwrite | INVALID |
| Wrong market or interval | Stop immediately | INVALID |
| New candle opening or source timestamp regresses | Stop, count out_of_order | INVALID |
| Partial arriving after its accepted close | Count late/out-of-order, do not deliver | DEGRADED |
| Missing closed interval(s) | Record MarketDataGap; no repair, continue | DEGRADED |
| Isolated malformed message | Capture raw, count/log reason, continue | DEGRADED |
| Invalid message threshold (default 3) reached | Stop | INVALID |
| Disconnect/reconnect/stale observation | Record operational issue | DEGRADED |
| Retry budget exhausted/permanent stream error | Stop | INVALID |

For an identical close, a different emission timestamp alone is not contradictory.
Deduplication takes precedence over timestamp checks so legitimate reconnect
re-emissions are harmless. Other timestamp regressions are fatal. Gaps are based
on consecutive accepted closed openings: 12:00, 12:01, 12:03 records one missing
12:02 candle. There is no implied continuity before the first observed close.
No interpolation, forward fill, backfill or sorting is performed.

## Staleness, retries and shutdown

NetworkPolicy defaults: stale_after=15 seconds, connect_timeout=10,
initial_delay=1, maximum_delay=30, multiplier=2, max_reconnects=5,
metadata_attempts=3. Delay sequence is bounded exponential, no jitter. Counters
are session-wide, not reset after a short-lived successful connection.

Staleness measures monotonic elapsed time without a valid advancing exchange
event, not event-to-local-clock subtraction. Repeated copies of an old event and
malformed frames cannot keep a stalled stream alive. This rule has a regression
test: the original F2 implementation incorrectly refreshed on duplicates, the
test demonstrated it, and the new-source-only fix refreshes on advancing E.
It does not infer missing prices from local clock skew or manufacture candles.

Transient metadata errors, network resets, disconnects and receive timeouts have
bounded retries. Configuration errors do not. REST Retry-After is honored as above.
WebSocket Ping/Pong is implemented by websockets; ping_interval=None disables
client periodic pings, not automatic responses to server Ping frames.
A dropped connection or serverShutdown closes its context before the next URL
subscription. Deduplication state lives in the runner, across reconnections.

--closed-candles N stops after exactly N accepted unique closes, not N frames.
--max-seconds bounds stream consumption; metadata has its own bounded policy.
The first Ctrl+C cancellation closes the stream, flushes files and produces an
interrupted partial-session manifest. Quality describes observed data, while
stop_reason separately distinguishes target reached, interruption, time budget,
source exhaustion or invalid session. Hardware failure/kill -9/disk failure cannot
promise finalization; a missing manifest/footer is rejected as incomplete.

## Shadow strategy contract and F1 interoperability

Exactly F1 SimpleMeanReversionV0 is called, with unchanged parameters/rules.
History contains only accepted closed F1 Candle values (opening as label).
F2 source does not directly import or instantiate Wallet, Ledger, RiskEngine,
PaperExecutionEngine or ReplayRunner. Python may load existing F1 package exports
while resolving submodules, but F2 never constructs or invokes financial objects.
No candidate reaches any executor. This is a code capability boundary, not a
sandbox against arbitrary Python introspection.

F2 supplies a fixed, neutral PortfolioSnapshot of all zeros because there is no
account or simulated portfolio. The observer retains only the candidate's side:
WOULD_BUY, WOULD_SELL or NO_ACTION. Amounts are discarded and never represented
as executable budgets. All decisions, including NO_ACTION, are auditable;
strategy_signal_count counts only actionable directions. Under the neutral flat
snapshot, this strategy cannot emit WOULD_SELL and does not pretend a BUY changed
a position. This is signal observation, not a trading simulation.

For a non-MXN market, the original price units and market remain in normalized
records and decisions. A SHADOW/MXN syntactic sentinel satisfies F1's existing
OrderIntent validation inside the bridge only; it is not an FX conversion or a
financial market claim, and no amount leaves the bridge. BTC/MXN needs no sentinel.

result.to_f1_dataset() reuses the exact closed Candle sequence for an MXN market.
INVALID sessions are always refused; DEGRADED requires explicit allow_degraded.
Non-MXN conversion is refused with UnsupportedQuote, never relabeled or converted.
This is a handoff for a separately invoked OFFLINE F1 experiment, not live paper
trading. F2 data replay and F1 financial replay remain separate responsibilities.

## Capture schema and integrity

For `session.jsonl`, the bundle is:

- `session.jsonl`: header, ordered SourceItem records, end footer. Header contains
  schema autofund.capture.v1, source, full MarketInfo, interval, session ID,
  start time, strategy identity/parameters and invalid-message threshold. Items
  contain sequence ID, local receipt, normalized event and/or operational notice.
- `session.raw.jsonl`: raw metadata header, every application-message text with
  receipt and corresponding item ID, then a raw-count footer. This includes partial,
  malformed and shutdown messages. WebSocket control frames handled by the library
  are not application messages; they are not included in raw_message count.
- `session.manifest.json`: summary report, quality, first/last event, counts,
  normalized/closed/signal fingerprints, per-file byte hashes and bundle hash.

Existing bundles are never overwritten. Each complete JSON line is flushed;
normal shutdown writes both footers, closes files and then writes the manifest.
Canonical JSON is F1's existing serializer: sorted keys, UTF-8, Decimal strings,
UTC ISO-8601, stable enum values. No second canonicalization scheme is introduced.
Raw payload text is retained as an opaque string, not interpreted by the runner.

capture_file_fingerprint hashes the two content SHA-256 digests of raw and normal
files. It includes receipt times, session ID and raw spelling through those files;
it is a byte-integrity identity. It deliberately does not hash itself or the
manifest recursively. Paths are not part of this hash.

normalized_session_fingerprint hashes schema autofund.normalized-session.v1,
market, interval and ALL normalized updates in observed order, including partials,
duplicates and exchange event times. Local receipt, operational notices, session
ID, reconnect delays and paths are excluded. Separate closed_candle_fingerprint
and signals_fingerprint identify exactly what the strategy saw and decided.
Source provenance is stored in the report/header, not substituted for event content.

The reader validates file hashes, schema, sequence IDs, both footers/counts,
raw-to-normal links, typed events, normalized fingerprint and counts before
emission. replay_capture also recomputes every report field except the destination
path, including quality, closed-candle identity and decisions. A copied bundle at
another path has identical semantic fingerprints. Coherently rewriting all files
and hashes is not detected as forgery; these are integrity hashes, not signatures.

## Parity and golden test

PARITY PASS means exact equality of normalized event values/canonical JSON,
strategy-visible candles, decisions, fingerprints and deterministic report fields.
Recorded operational notices reproduce quality observations; they do not claim
network latency or reconnect schedules were simulated deterministically.

Golden fixtures are hand-authored Binance-shaped Spot messages, based on the
reviewed schema, not represented as historical trades. Six 1m candles have closes
1,000,000; 1,000,000; 800,000; 1,000,000; 1,000,000; 800,000. Each has a partial
and a final update; the third close is deliberately repeated. Expected:
13 raw messages, 13 normalized updates, 6 accepted closed candles, 1 duplicate,
0 gaps and 2 WOULD_BUY signals. The literal golden normalized fingerprint is:
`a7a1f4f5fbe6c568687b53c1755e192d2a2f0a2b75e831ee1b4909126ee5e519`.
The test reads `tests/fixtures/binance/golden_expected.json`; it never updates it.
A future intentional normalization change requires an explained schema/fixture
review, not automatic acceptance of a changed fingerprint.

## Live certification evidence

Command run without credentials:

```powershell
.venv\Scripts\autofund market info --symbol BTCMXN
.venv\Scripts\autofund market capture --symbol BTCMXN --interval 1s --closed-candles 5 --max-seconds 45 --output artifacts/f2_live_sample.jsonl
.venv\Scripts\autofund market replay artifacts/f2_live_sample.jsonl
```

Captured UTC openings: 2026-09-18 07:26:38 through 07:26:42.
5 raw application messages, 5 normalized updates, 5 closed candles, 0 duplicates,
0 gaps, 0 out-of-order, 0 invalid messages, 0 reconnects; VALID quality.
Five NO_ACTION decisions, no actionable signal. Offline replay: PARITY PASS.
Normalized fingerprint:
`3ced355b030f04308b684e7374d4accc42119587f79259b2a7e96d24163f331f`.
Artifacts remain local/ignored; synthetic fixtures independently test nonempty
signal parity and fault handling. No real-capture file is needed by normal tests.

## Tests, security audit and limitations

Normal `pytest` excludes live markers and uses fake transport/clock. F2 offline
tests block network resolution. `pytest -m live` explicitly enables separately
marked integration tests for public metadata and five-candle parity.
Existing pytest, Ruff and strict mypy tooling are reused. No async pytest plugin.
The audit verifies absolute imports do not reach financial execution and that F2
contains no HTTP mutation calls or WebSocket application sending interface.
A behavioral test forbids constructing Wallet/PaperExecutionEngine during capture.
No API key, secret, .env, credential manager or external telemetry is implemented.

Limits: one symbol, ASCII asset names, 1s/1m, finite in-memory normalization history,
application-message capture only, no automatic gap repair, no signed provenance,
no market liquidity/order simulation. Freshness thresholds are operational policy,
not an exchange SLA. Trust the quality report before research conversion.
F0/F1 deterministic golden fixtures remain frozen and passing.

## F3 boundary

F3 - authenticated Binance Spot Testnet execution requires a separate explicit
specification and authorization. It must add account/auth/order capabilities in a
new boundary, not turn this receive-only adapter into an implicit execution client.
