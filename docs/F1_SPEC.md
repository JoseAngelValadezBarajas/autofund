# AutoFund 0.2 ? F1: Deterministic Historical Market Replay

F1 IS NOT A PROFITABLE TRADING SYSTEM.

## Scope and frozen baseline

Financial core baseline: `066a54c feat: bootstrap AutoFund F0 financial core`.
F0 source, its 76 tests, F0_SPEC and its demo remain unchanged. F1 lives in
`src/autofund/replay/`. Existing financial contracts are reused, not reimplemented.
No F0 bug fix was required for F1.

Included: local CSV candles, validation, canonical identities, replay clock,
past-only strategy protocol, one dummy strategy, pending intents, immediate F0
fills, immutable trace/result, metrics, JSON artifact, fixtures and golden test.
No exchanges, networking, external data downloads, credentials, real money,
parameter optimization, multiprocessing, database, web application or frontend.
Only a single BASE/MXN market per replay; all state stays in memory.

## Dataset schema and time

CSV columns, exactly in this order:
`timestamp,open,high,low,close,volume`.
UTF-8, six fields per row, strict CSV. Blank/malformed rows and extra columns
are rejected. Decimal tokens accept decimal/scientific notation, not whitespace,
underscores, floats, NaN or infinity. Decimal bounds are inherited from F0.
File-open failures remain normal filesystem exceptions; malformed data raises
DatasetValidationError with a row number where available. No sort, deduplication,
forward fill, default value, timezone guess or other silent data repair.

Candle is frozen: aware datetime plus Decimal OHLCV. Prices >0, volume >=0,
low <= open <= high, low <= close <= high. Dataset is frozen and nonempty, stores
a tuple, validates strict timestamp order and no duplicate instants. Gaps are
allowed; the next available candle is the next execution opportunity. No volume
or liquidity model is implied by the volume column.

**Timestamp denotes a candle label, not a claim about its physical close time.**
Input format is ISO-8601 `YYYY-MM-DDTHH:MM:SS[.ffffff]Z` or a numeric offset.
Fractions beyond microsecond precision are rejected rather than truncated.
Offsets normalize to UTC; naive datetimes are forbidden. Two ordered phases
exist at each label: OPEN, then CLOSE. At CLOSE, the entire candle becomes
observable. A signal is stamped with its source candle label and executes only
at a strictly later candle label, at that candle's OPEN. No fixed interval or
physical candle duration is inferred from the CSV. These labels plus phases are
the event timeline; no intrabar timestamps, interpolated ticks or elapsed-time
returns are fabricated. This convention works across gaps and machines.

ReplayClock advances once per row to that row's UTC timestamp. It rejects
backwards/duplicate transitions and reading before initialization. No wall-clock
time participates. Strategies receive only the current label, never the clock.

## Canonical JSON and identities

Canonical representation: keys sorted, UTF-8, no insignificant whitespace,
Decimal as fixed-point strings without trailing fractional zeros; every signed
zero becomes `"0"`. No Decimal.normalize() that could inherit ambient precision.
Aware datetimes become UTC ISO-8601 with exactly six fractional digits and Z.
Enums serialize to stable values, tuples/lists to arrays, dataclasses to their
fields. Mapping keys must be strings. Float, arbitrary objects and non-finite
Decimal are rejected. No repr, pickle, random ID or object address.

All fingerprints are full SHA-256 of this representation:

- Dataset: schema `autofund.candles.v1`, market and every OHLCV/timestamp row.
  File paths, CSV spelling differences such as `100` vs `100.00`, line endings,
  timezone offsets representing the same instant and filesystem metadata do not
  affect it. Changed prices, volume, market or timeline do affect it.
- Config: the complete immutable ReplayConfig including schema, currency,
  initial_equity, deployment fraction, minimum, fee, slippage and both policies.
- Strategy: schema `autofund.strategy.v1`, identifier, version and named scalar
  parameters, sorted by name. Duplicate names/mutable values/floats are rejected.
- Run ID: `run-` + hash of input identities and run schema. This is a logical run
  identity, deliberately identical for identical inputs, not a unique invocation.
- Result: every deterministic BenchmarkResult field except run_id and
  result_fingerprint itself. Includes the complete dataset/config/strategy,
  metadata, signals, dispositions, fills, ledger, positions, equity curve,
  closed episodes and metrics. Same inputs with changed behavior may have the
  same logical run ID but a different result fingerprint.

Only the documented execution/end policies are supported. Unsupported policies
are rejected, never silently mapped to the defaults. New policies will need a
versioned implementation; their names are already part of the config hash.

## Strategy contract and isolation

Strategy supplies immutable StrategyIdentity and a pure callback:
`on_candle(StrategyContext) -> OrderIntent | None`.
All behavior-affecting parameters must be declared in the identity; semantic code
changes require a version change. The runner rejects identity changes during a
run. The provided dummy has no mutable per-run state and can be reused.

Context has only timestamp, market, a tuple of already closed Candles, and a
PortfolioSnapshot of cash, quantity, cost, equity and realized P&L. No dataset,
future iterator, dataset length/end, wallet, ledger, execution or runner handle
is passed. Past tuples and frozen objects retained by a callback never grow or
acquire future data. Unit tests attempt public future indexing and mutations;
changing future candles preserves every preceding context/signal/fill.

The callback has its own local Decimal context; modifying it cannot affect
execution or metric arithmetic. This is API isolation for trusted Python code,
not a sandbox against introspection, external I/O, globals or malicious plugins.
Custom callbacks must be pure, deterministic and disclose their parameters;
F1 does not prove arbitrary user code deterministic.

Exactly one production dummy exists: SimpleMeanReversionV0, identifier
`simple_mean_reversion`, version `0`. Defaults: window=3, entry_threshold=0.05,
allocation_fraction=0.20. After window observations, the average includes the
current close and the preceding window-1 closes. If flat and current close is
strictly below mean*(1-threshold), request a BUY budget equal to close-marked
equity*allocation_fraction. If holding and close>=mean, request sale of all held
quantity. Otherwise emit nothing. Parameters are not optimized; warmup emits
nothing. Requested allocation is never treated as capital approval.

OrderIntent separates an unapproved candidate from a Fill: market, Side and
exactly one BUY budget or SELL quantity. Negative/zero amounts can be represented
so F0 RiskEngine rejects them at execution time. Invalid shape, float or foreign
market aborts as StrategyContractError. A signal is the timestamped record of an
intent. At most one intent per candle in F1; no order replacement or concurrency.

## Lifecycle, ordering and risk

ReplayRunner holds immutable config and creates fresh state for every call.
ReplayEngine follows this exact order:

1. Deposit initial_equity into a fresh F0 Wallet; create CapitalManager,
   RiskEngine and PaperExecutionEngine from config, and an empty ReplayClock.
2. For each candle, advance clock to its label.
3. Consume the single pending intent, clearing it before attempting execution.
   Call F0 buy/sell using ONLY this candle's open as the reference price. Risk
   checks therefore use the current open-marked portfolio, not the signal close.
4. Record a TradeRecord and link its actual ledger_entry_id on a fill. On
   RiskRejected (including InsufficientFunds), record the rejection and continue;
   no fill/cash mutation and no automatic retry. Other errors abort the run.
5. Sample equity at OPEN_AFTER_EXECUTION using open as mark.
6. Sample equity at CLOSE using close as mark. No high/low valuation is fabricated.
7. Extend past history with this candle; call strategy with value-only snapshots.
   Record any intent as pending for the next row. Assert F0 invariants.
8. After the last row, cancel remaining intent explicitly. Keep open positions,
   mark them at the final close and report realized/unrealized P&L separately.
   No forced liquidation and no execution of the last signal at a fictional price.

TradeRecord is a projection: signal ID/timestamp, execution timestamp, reference
price, immutable F0 Fill, estimated slippage, ledger ID. It does not own or mutate
balances. Trace stores all signals and one outcome per signal, all fills, complete
ledger (including deposit), final positions, curve and closed episodes.

## Deployment invariant and market drift

F0 admits BUY only when budget <= min(cash, max(0, equity*fraction-deployed)),
recomputed at the execution open. Budget includes fees. Intents are not clipped
or preapproved by the strategy. Tests cover a request affordable at signal time
that is rejected after the next open changes the available capital.

A marked deployment fraction is **not a permanent upper bound**: a price rise
can push an existing position over it without any new buy. F1 preserves F0 and
does not silently liquidate/rebalance. Tests check admission on flat prices,
market-driven excess, refusal of further buys and accurate maximum reporting.
Claiming that every marked point always stays below the fraction would be
incorrect under the frozen contract. The max metric deliberately exposes excess.

## Precision and metrics

Financial calculations use F0's isolated 50-digit HALF_EVEN context; F0 BUY's
explicit ROUND_DOWN residual remains in fee and is visible in the fill/ledger.
No exchange ticks, cent rounding or approximate comparisons are introduced.
Finite fee totals, slippage totals and episode P&L totals are added using enough
local Decimal precision to preserve every digit of the recorded component values.
Ratios and individual cost/valuation operations use precision 50.

Equity curve has two points per candle, keyed by (timestamp, phase), with cash,
deployed market value, equity, realized P&L, unrealized P&L. It is not a tick-level
curve. Drawdown sees gaps at OPEN and marks at CLOSE; high/low paths and liquidity
within a candle are unknown. Maxima refer only to these sampled events.

- Initial equity: exact configured deposit. Final equity: final CLOSE valuation.
- Net P&L: final equity - initial equity, under F0 decimal policy.
- Realized P&L: F0's accumulated realized P&L. Unrealized: final deployed value
  minus remaining cost basis (which includes BUY fees).
- accounting_rounding_mxn: net minus realized minus unrealized, an explicit
  finite-precision reconciliation residual, never a concealed tolerance.
- total_fees_mxn: exact finite sum of ledger fees, including explicit BUY residual.
- Slippage: BUY `(execution-reference)*quantity`; SELL
  `(reference-execution)*quantity`, at precision 50. Total is the exact finite sum.
- **cost_addback_pnl_mxn**: exact finite sum of net P&L + total fees + estimated
  slippage. This is a cost-addback bridge for the actual traded quantities and
  terminal portfolio. It is not a separately simulated frictionless strategy,
  not a claim that removing costs would preserve quantities/signals, and not
  mislabeled as an independently reconstructed gross trading P&L. No ambiguous
  gross_pnl metric is emitted.
- return_fraction = final/initial - 1; return_pct = return_fraction*100.
- fill_count counts executions. closed_trade_count counts flat-to-flat position
  episodes, including every intervening addition/partial sale. The episode P&L
  is the exact sum of its ledger realized P&L. Wins/losses use its sign; zero is
  separately breakeven. An unclosed episode with partial profits is not a win.
- Drawdown: seed peak at initial equity; update peak from equity (never cash),
  then track max(peak-equity) and max((peak-equity)/peak*100). The absolute and
  percentage maxima need not belong to the same peak/trough pair.
- max_deployed_mxn: max sampled marked position value. max_deployed_fraction:
  max(deployed/equity) over samples. Initial equity must be positive.

## Artifact and reproducibility

BenchmarkResult and all objects produced within it are frozen and recursively
use tuples/frozen values. `to_json()` returns canonical text; optional path
writes it as UTF-8 with one terminal newline. The path never enters the artifact
or a hash. Dataset rows are included so all prices, signals and results can be
audited without the original filesystem. No wall-clock timestamp/host/secrets.
The artifact is content-addressed for reproducibility, not digitally signed.
The exporter does not claim to detect a coherently forged result.

## Fixtures and golden replay

Three hand-readable 24-row datasets: flat at 100, uptrend 100..123, and a flat
100 market with a single close at 80 on row 3. Mean reversion produces one
round-trip; the flat/uptrend fixtures emit none. Invalid duplicate-timestamp
and invalid-OHLC fixtures are also included. All data is synthetic and local.

Golden: mean_reversion_market.csv, initial=50, deployment=0.5, minimum=1,
fee=0.01, slippage=100 bps, window=3, threshold=0.05, allocation=0.20402.
No tuning for profit: the intentionally cost-bearing round-trip loses money.

Manual oracle: signal row 3 requests 10.201. BUY row 4 at reference 100,
execution 101, quantity 0.1, gross 10.1, fee 0.101, total 10.201.
SELL signal row 4 executes row 5 at reference 100, execution 99,
gross 9.9, fee 0.099, proceeds 9.801. Final cash/equity=49.600;
fees=0.200; slippage=0.200; net/realized=-0.400; cost-addback=0;
2 fills, 1 losing closed trade, 3 ledger entries, no remaining position.

Frozen identities and expected results are in
`tests/fixtures/golden_mean_reversion.json`. The test reads literal expectations;
it never rewrites or regenerates them. The result fingerprint is:
`eeb552bfabfb588d82df30d5191d02d1bbb60d6dddff0aa8e5498da216755bd5`.
An intentional future behavior/schema change must review the financial oracle,
version the applicable contract, explain the change and explicitly update the
fixture. A failing golden is not fixed by blindly accepting a new hash.

## Acceptance and next boundary

All 76 F0 tests must remain passing. F1 tests cover dataset/decimal/UTC validation,
canonical identities independent of paths and spellings, strategy isolation,
future-prefix invariance, next-open timing, exactly-once execution, risk and
capital rechecks, immutable outputs, end cancellation and open marking, fees,
slippage, partial episodes, drawdown, exact aggregation, context independence,
full artifact hash recomputation and golden literal expectations. Two runs must
have exactly equal signals/fills/ledger/curve/metrics/result hash, including runs
in separate processes with different hash seeds, paths and timezone settings.

F1 adds no further trading phase automatically. A later phase can specify richer
market data/replay coverage and execution constraints without changing these
financial or no-lookahead contracts silently.
