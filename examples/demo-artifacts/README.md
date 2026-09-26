# Demo artifacts

This directory holds **small, sanitized examples** of AutoFund's artifact shapes, so a reader can see
what the certification documents look like without access to any private dataset.

They are illustrative only. They are not evidence, not a backtest, and not a measurement of any
market.

## What is here

| File | Demonstrates |
|---|---|
| `experiment-certification.example.json` | A milestone certification: predeclared windows, outcome, reason codes |
| `alpha-source.example.json` | An information source that predicts and is not economic |
| `evidence-lineage.example.json` | The link from an alpha source through an experiment to an artifact |

Every document carries `"provenance": "SYNTHETIC_DEMO"` and a `demo_notice`. The values are chosen to
be obviously synthetic — the fingerprint is a repeated character, the windows are round numbers —
so nothing here can be mistaken for a real capture.

## The larger, runnable dataset

These files are static examples meant to be read. The dataset the Research Control Center actually
renders is generated at run time by `autofund demo` into `artifacts/demo/`, and it is richer: it
includes JSONL capture streams, a campaign with insufficient coverage, and the full set of registry
records.

That generated dataset is not committed, because it is runtime output rather than source. It is
regenerated deterministically on every demo start and is covered by tests, so a clone always has a
working Control Center without a fixture file in Git.

## Schemas

The field-by-field contracts for these documents are in [`../../schemas/`](../../schemas/), generated
from the domain models by `scripts/export_schemas.py`. If you are integrating against AutoFund, read
the schemas rather than these examples: the examples show one instance, the schemas show the shape.
