# Contributing

Thanks for looking. This document explains how to work on AutoFund and, more importantly, what is
different about contributing to a system that can spend money.

## Development setup

```bash
python -m venv .venv
.venv/bin/python -m pip install --upgrade pip
.venv/bin/python -m pip install -e ".[dev]"

cd frontend && npm ci && cd ..
```

On Windows use `.venv\Scripts\python` and `.venv\Scripts\` in place of the Unix paths.

## Running the checks

Run all of these before opening a pull request. They are the same commands CI runs.

```bash
# Backend
.venv/bin/python -m pytest tests -m "not live and not stage"
.venv/bin/python -m ruff check src/ tests/ scripts/
.venv/bin/python -m mypy --strict src/autofund
.venv/bin/python -m bandit -r src/ -q
.venv/bin/python scripts/secret_scan.py

# Frontend
cd frontend
npx tsc --noEmit -p tsconfig.json
npx vitest run
npm run build
npx playwright test
```

Playwright requires `npm run build` first, because the backend serves the built frontend.

### Tests that need real infrastructure

Tests marked `live` or `stage` reach real public endpoints or a real authenticated account. They are
deselected by default and are **not** part of CI. A skip is not a pass: if you change anything in the
execution path, run the relevant certification script and say so in the pull request.

```bash
.venv/bin/python -m pytest tests -m live        # public network, no credentials
.venv/bin/python -m pytest tests -m stage       # requires Stage credentials
```

## The part that is different: financial semantics

Most of this codebase is ordinary software. Three parts are not, and changes to them need evidence
rather than approval.

A pull request that changes any of the following **must** include the reasoning, the tests, and the
before/after numbers:

| Area | Examples |
|---|---|
| **Risk semantics** | `RiskEngine`, drawdown policy, position sizing, reward/risk rules |
| **Economic admission** | `EconomicEdgeGuard`, fee handling, friction arithmetic, cost models |
| **Capital limits** | Authorized capital, maximum deployment, single-order cap |
| **Execution path** | Order construction, submission, reconciliation, recovery |
| **Strategy parameters** | Any change to a frozen profile's behaviour |
| **Certification thresholds** | Anything that makes a strategy easier to certify |

For these, the pull request template asks specific questions and the answers are the review. A
change that makes a strategy pass a gate it previously failed is not an improvement until it is
shown to be correct — the gate exists because something was measured, and loosening it discards that
measurement.

### Invariants that must not be weakened

These hold today and a pull request that breaks one will be rejected unless it argues convincingly
that the invariant itself was wrong:

1. **Authorization fails closed.** Only the `RUNNING` state authorizes a session. Any new state must
   be unauthorized by default.
2. **Demo cannot reach an exchange.** No credentials, no authenticated request, no mutation.
3. **Engineering health says nothing about profitability.** Four separate statuses are never combined
   into one.
4. **The wallet is not the AutoFund book.** Exchange balances and AutoFund inventory are rendered
   separately and never summed.
5. **Only confirmed fills move the ledger.** An acknowledged order with no fill evidence changes no
   financial state.
6. **No blind retry.** An unresolved order halts rather than resubmitting.
7. **A missing measurement is reported as missing.** `UNKNOWN`, `NOT_RECORDED` or `NOT_APPLICABLE`,
   never a plausible default.
8. **The research read model never writes.** Research API routes are GET-only.
9. **Provenance is preserved.** Synthetic evidence must never be presented as measured.

### Research changes

If you add or alter research, state:

- the hypothesis, **before** running anything
- the decision rule, declared in advance
- whether the result is development evidence or holdout evidence, and how they were separated
- what a negative result would look like

Commit the negative result. This project's research record is mostly negative and that is a feature;
a milestone that "found nothing" has often been the most informative one.

## Pull request expectations

- **Keep it reviewable.** Small, focused pull requests get reviewed; large ones do not.
- **Explain the why.** The code shows what changed; the description should say what was wrong before.
- **Add tests** for behaviour changes. A bug fix without a regression test will be asked for one.
- **Do not reformat unrelated files.** It hides the real change.
- **Do not commit generated or private data.** See `docs/public-boundary.md`.
- **Never commit a credential.** If you do, rotate it immediately — deleting it in a new commit does
  not remove it from history, and the value must be treated as public.

### Commit messages

Imperative mood, a short summary line, and detail in the body where it is non-obvious:

```text
fix: refuse recovery retry when an order outcome is unknown

A retry after an unresolved order could open a second position. The
recovery path now halts and surfaces the uncertain order instead.
```

## Style

- Python: Ruff with the repository's configuration, `mypy --strict` clean.
- TypeScript: strict mode, no `any` in new code where a type can be expressed.
- Comments explain **why**, not what. A comment that restates the code is noise; a comment recording
  why an obvious alternative is wrong is the most valuable kind.
- Docstrings on public functions should say what the function guarantees and, where a defect shaped
  the design, what went wrong.

There is no line-length pedantry and no preference expressed here about tabs, because the formatter
decides.

## Reporting bugs and requesting features

Use the issue templates. For anything in the financial path, the correctness issue template asks for
the reproduction details that make a report actionable instead of anecdotal.

## Code of conduct

Participation is covered by [CODE_OF_CONDUCT.md](CODE_OF_CONDUCT.md).

## License

AutoFund is licensed under the [Apache License 2.0](LICENSE). By contributing, you agree that your
contribution is licensed under the same terms — that is what section 5 of the license means by a
contribution "intentionally submitted for inclusion in the Work".

Two consequences worth knowing before you contribute:

- **Apache-2.0 section 4(b) requires modified files to carry a notice stating that they were
  changed.** Git history normally satisfies this for source files; keep commits focused and their
  messages accurate, and do not strip existing notices.
- **Changing the license later requires the agreement of every copyright holder**, which includes
you once your contribution is merged. If you have a reason to object to Apache-2.0, raise it before
contributing rather than after.

Copyright 2026 AutoFund contributors.
