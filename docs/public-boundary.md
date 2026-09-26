# Public / private boundary

This repository is prepared for public release. This document is the contract for what belongs in
Git and what must stay on the machine that produced it. It is enforced by `.gitignore`, by the
header and history secret scanners, and by tests.

The reason it needs to be written down rather than assumed: an artifact directory that is
convenient during development contains account-specific financial records, and "we will remember
not to commit it" stops working the first time someone runs `git add -A`.

## Classification

| Class | In Git | Examples |
|---|---|---|
| `PUBLIC_CODE` | **Yes** | Source, tests, certification scripts, CI configuration |
| `PUBLIC_SCHEMAS` | **Yes** | Data contracts under `schemas/` |
| `PUBLIC_DOCUMENTATION` | **Yes** | `README.md`, `docs/`, `CONTRIBUTING.md`, `SECURITY.md` |
| `PUBLIC_CONFIG_EXAMPLES` | **Yes** | `.env.example` (placeholder values only) |
| `PUBLIC_SYNTHETIC_DATA` | **Yes** | Generated demo dataset, synthetic fixtures |
| `PUBLIC_SAMPLE_ARTIFACTS` | **Yes** | Small sanitized examples under `examples/` |
| `PUBLIC_SCREENSHOTS` | **Yes** | Visual baselines containing demo or sanitized state |
| `PRIVATE_CREDENTIALS` | **No** | API keys, secrets, `.env`, `keys.json`, Clixml stores |
| `PRIVATE_RUNTIME_DATA` | **No** | `artifacts/` journals, ledgers, telemetry, captures |
| `PRIVATE_FINANCIAL_DATA` | **No** | Journals, ledgers, fills, balances, position cost basis |
| `PRIVATE_LARGE_DATASETS` | **No** | Microstructure and cross-venue captures, datasets |
| `PRIVATE_RESEARCH_ARTIFACTS` | **No** | Real certifications containing account-specific state |

## Why runtime artifacts are private

`artifacts/` is not merely large. It is a financial record:

- `artifacts/live/execution.jsonl` is the durable execution journal. It contains order identifiers,
  fills and the reconstructed ledger for a real account.
- Certification artifacts record the verified fee schedule of a specific account, along with the
  position sizes, cost bases and realized P&L that were live when the milestone ran.
- Telemetry records contain session identifiers tied to a real account.

None of these are needed to understand or reproduce the project's engineering. All of them are
personal financial state.

## What is published instead

Where the research record needed to be visible, it was published in sanitized form:

- **`docs/research-history.md`** states each milestone's question and finding. The scientific
  conclusions are published; the datasets behind them are not.
- **Demo mode** generates a synthetic dataset at run time, so the Research Control Center can be
  explored without any private artifact. It is labelled `SYNTHETIC_DEMO` and is not a backtest.
- **`examples/`** holds small sanitized samples showing the artifact shape, not real evidence.

## Sanitization applied for this release

Concrete steps taken, recorded so the boundary is auditable rather than asserted:

1. The real position (`0.00000727 BTC` at a cost basis of `10.91486406 MXN`) was published in test
   fixtures and two specification documents. It was replaced with synthetic figures sized to
   preserve the properties the tests depend on.
2. A live round trip's exchange `origin_id`, order `oid` and `trade_id` were published in a
   specification document and a recovery script. The document now uses placeholders and states why;
   the script takes the origin id as an argument.
3. A real account's deployed and cash balances were used as constants in a certification script.
   They were replaced with synthetic values that respect the same caps.
4. `docs/F5_CONTROLLED_MICRO_LIVE_SPEC.md` retains the value `10.91486406 MXN`. That figure is
   **arithmetic derived from the product's own published 11 MXN single-order cap and 78 bps taker
   fee**, not a recovered account value, and it is the number that makes the document's calculation
   checkable. It is retained deliberately.

## Enforcement

| Control | What it catches |
|---|---|
| `.gitignore` | Credentials, `artifacts/`, journals, ledgers, captures, large data, editor state |
| `scripts/secret_scan.py` | Credential-shaped values in the working tree |
| `scripts/history_secret_audit.py` | Credential values in **any** reachable Git blob, including deleted ones |
| `tests/public/test_public_boundary.py` | Ignore rules, config examples, tracked-path rules |
| CI | Runs all of the above on every push and pull request |

## If you fork this repository

If you run AutoFund against a real account, your `artifacts/` directory becomes private financial
data. It is already gitignored, and it should stay that way. Before publishing a fork, run:

```bash
python scripts/history_secret_audit.py --known-secrets-from path/to/your/keys.json
```

Deleting a file in a later commit does not remove it from Git history. A file committed once and
deleted afterwards is still recoverable by anyone who clones, which is why the history audit reads
every blob rather than the current checkout.
