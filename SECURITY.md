# Security Policy

## Scope

AutoFund is experimental software that can place real orders on a cryptocurrency exchange when it is
configured for production. This document covers:

- credential handling
- the production safety boundary
- what is in scope for a security report
- how to report a vulnerability
- the publication policy for this repository

## Reporting a vulnerability

**Use GitHub's private vulnerability reporting** on this repository (Security → Report a
vulnerability). That channel is private, it is the project owner's chosen mechanism, and it avoids
either party publishing an exploitable detail before a fix exists.

If private reporting is unavailable, open a minimal issue that states only that you have a security
finding and asks to be contacted. **Do not include the details in a public issue.**

Please include:

- what the component is and where it lives
- the impact you believe it has, especially whether it could cause an unintended order, an
  unintended withdrawal, or the disclosure of credentials
- the smallest reproduction you can manage, with all secrets replaced by placeholders

**Never include real credentials, API keys, mnemonic phrases, or the contents of a `.env` file in a
report.** If a credential may have been exposed, say *which variable* and *where*, not what its
value is. A report that quotes a secret is a second copy of the secret.

### What to expect

This is a personal project rather than a staffed product, so no formal response-time commitment is
offered. Reports about the production boundary are prioritised over everything else.

## Supported versions

| Version | Supported |
|---|---|
| `0.3.x` | Yes — current |
| `0.2.x` and earlier | No |

## Credential handling

### What AutoFund asks for

Production credentials come from environment variables:

| Variable | Purpose |
|---|---|
| `AUTOFUND_BITSO_LIVE_API_KEY` / `_API_SECRET` | Production trading |
| `AUTOFUND_BITSO_LIVE_PERMISSIONS_CONFIRMED` | Explicit acknowledgement that the key is real-money |
| `AUTOFUND_BITSO_STAGE_API_KEY` / `_API_SECRET` | Stage execution certification |
| `AUTOFUND_BITSO_PROD_API_KEY` / `_API_SECRET` | Read-only account and market observation |

### Requested API permissions

Use a key with the **minimum** permissions the task needs. AutoFund:

- **never** needs withdrawal permission
- **never** needs transfer permission
- it has no code path that withdraws or transfers, and no UI control that exposes one

A key with those permissions disabled protects you even if the application is compromised.

### Handling rules

- **Credentials are never written to disk by AutoFund.** They are read from the environment at
  startup and held in memory. They are not written to the journal, telemetry, artifacts or logs.
- **Credential objects refuse to render themselves.** `LiveCredentials` and `ProductionCredentials`
  define `__repr__` returning `key=***, secret=***`, so an exception traceback or a debug log cannot
  leak them.
- **A missing or malformed credential is fatal, never downgraded.** Startup raises rather than
  continuing in a degraded state, because "credential problem" must not be indistinguishable from
  "ready".
- **Demo mode removes credentials from the process.** Entering Demo deletes every credential and
  transport variable from the environment for the duration and restores them afterwards. If you
  export real keys in your shell, `autofund demo` still cannot use them.
- **`# nosec` is on the flagged line itself.** The one place AutoFund calls `urlopen` asserts an
  HTTPS scheme and carries the suppression on the statement, rather than suppressing the surrounding
  block.

### Where credentials must never go

`.gitignore` excludes, and CI checks for:

```text
.env, .env.*            (except .env.example)
keys.json, *.secret, *credential*, *.pem, *.key, *.p12, *.pfx
*.clixml, *.cred.xml    (Windows DPAPI stores written by Export-Clixml)
```

If you keep credentials in a file for local convenience, keep it outside the repository directory
entirely. Ignoring a file is a weaker protection than not having it there, because a later
`git add -f` or a `.gitignore` edit can undo it, and because **deleting a file in a later commit does
not remove it from Git history** — it stays recoverable in the packfile.

## Production safety boundary

AutoFund requires several independent conditions before it can place a real order. None of them is a
configuration setting that can be flipped by accident.

| Control | Behaviour |
|---|---|
| **Default mode** | Demo. A clone cannot reach an exchange. |
| **Mode parsing** | An unrecognised `AUTOFUND_MODE` raises. AutoFund refuses to guess. |
| **Operator confirmation** | Each session requires a typed confirmation phrase. |
| **Session authorization** | An allowlist: only the `RUNNING` state authorizes. Every unknown state is unauthorized. |
| **Capital envelope** | 50 / 25 / 11 MXN, fixed as code constants. No environment variable widens them. |
| **Economic gate** | `EconomicEdgeGuard` refuses entries whose expected net edge cannot clear friction. |
| **Risk gate** | `RiskEngine` enforces drawdown, single-trade risk and reward/risk policy. |
| **Submission permit** | Single-use, created only after the operator gate, consumed even if the network call fails. |
| **Order safety** | An unresolved order halts rather than retrying, because a retry could double a position. |
| **Mutation allowlist** | The only mutating HTTP routes are `POST /api/v1/control/{start,stop,kill}`. |
| **No order endpoint** | There is no browser-reachable route that submits, cancels or replaces an order. |
| **Host and origin checks** | Mutating requests require a matching `Host` and `Origin` plus a per-process control token. |

## Reporting a data-exposure concern

If you find account-specific data in this repository — a balance, a position size, a cost basis, an
order identifier, or a personal path — please report it. It is treated as a security issue rather
than a documentation issue, because the data is not reproducible and cannot be rotated.

`docs/public-boundary.md` records what is published and what is not, and the sanitization applied
before publication.

## Publication policy

- **Demo mode requires no credentials, makes no authenticated request, and cannot place an order.**
  This is asserted by tests and by `scripts/demo_smoke.py` on every CI run.
- **The history secret audit must pass** before publication. It reads every blob in every reachable
  commit, not just the current checkout, because a credential deleted in a later commit is still
  recoverable by anyone who clones.
- **A confirmed credential in Git history is a publication blocker**, and remediation is history
  rewriting plus credential rotation — not a deletion in a new commit.
