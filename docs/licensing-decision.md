# Licensing decision

**Status: awaiting a decision from the project owner. No license has been selected.**

Until a license is chosen, this repository is **source-available, not open source**. Under default
copyright, no rights are granted: others have no legal permission to use, copy, modify or
redistribute the work, even though they can read it. That is a defensible position, but it should be
a choice rather than an oversight.

The rest of the public-release preparation is complete and does not depend on this decision. The
repository is otherwise technically ready to publish.

## What the choice affects

AutoFund is unusual in that two of its strongest assets are not code:

- **The code** — a research, risk, economics and execution platform.
- **The research record** — a documented series of negative results that other people can learn from,
  and a methodology for rejecting a hypothesis.

A license that forbids commercial use would block the second from being reused by a researcher at a
company. A permissive license allows someone to take the architecture and run it against their own
account without contributing anything back, which is permitted and generally considered acceptable.

## Option 1 — MIT

**Summary.** The shortest permissive license in common use. Anyone may use, copy, modify, merge,
publish, distribute, sublicense and sell, provided the copyright notice and license text are
retained. Comes with no warranty.

| | |
|---|---|
| **Length** | ~170 words |
| **Patent grant** | **No** — silent on patents |
| **Trademark grant** | No |
| **Attribution required** | Yes, in copies |
| **Copyleft** | None |
| **Compatibility** | Compatible with almost everything |

**Arguments for AutoFund.** It is the most familiar license in the Python and JavaScript ecosystems,
so a reader does not have to think about it. It maximises the chance that the methodology gets reused
and improved. For a project whose main output is a research record rather than a product, maximum
reuse is the point.

**Arguments against.** It is silent on patents. If a contributor were ever to hold a patent on a
technique used here, MIT would not grant a license to it, leaving downstream users exposed. For a
solo project with no patent portfolio that is largely theoretical, but it is the standard reason to
prefer Apache-2.0.

**Best if** you want the least friction and care most about the research being reusable.

## Option 2 — Apache-2.0

**Summary.** A permissive license in the same family as MIT, with substantially more legal
machinery. Adds an explicit patent grant and a patent-retaliation clause, requires that modified
files be marked, and requires notices to be preserved.

| | |
|---|---|
| **Length** | ~10,000 words |
| **Patent grant** | **Yes** — explicit, with retaliation if the licensee sues over patents |
| **Trademark grant** | No — explicitly excludes trademark rights |
| **Attribution required** | Yes, plus NOTICE file and change notices |
| **Copyleft** | None |
| **Compatibility** | Widely compatible; some friction with GPLv2-only projects |

**Arguments for AutoFund.** The patent grant is a real protection for downstream users, and the
explicit trademark exclusion is useful for a project that may become known by its name. It is the
standard choice for projects with corporate contributors or expectations of one.

**Arguments against.** The notice and change-marking requirements are overhead, and for a project
whose value is in the research rather than the code those obligations may discourage a casual
reuser. It is also long enough that contributors rarely read it.

**Best if** you want explicit patent protection and expect contributors or corporate users, or you
want the trademark question settled in writing.

## Comparison

| Question | MIT | Apache-2.0 |
|---|---|---|
| Can someone run AutoFund on their own account? | Yes | Yes |
| Can someone sell a service built on it? | Yes | Yes |
| Must they publish their changes? | No | No |
| Must they keep the copyright notice? | Yes | Yes |
| Must they mark modified files? | No | Yes |
| Patent license granted explicitly? | No | Yes |
| Trademark rights implied? | Unclear | Explicitly excluded |
| Typical ecosystem familiarity | Highest | High |

**Neither license prevents someone from losing money with this software.** Both are provided without
warranty, which matters more here than in most projects: AutoFund is experimental software that can
place real orders.

## Considerations specific to this project

1. **The README currently describes the project as source-available, not open source.** That wording
   must change once a license is added, and should not change before.
2. **The safety language in `README.md` and `SECURITY.md` should be kept regardless of license.** A
   disclaimer of warranty in a license is not the same thing as telling a reader clearly that the
   software has no certified strategy and must not be trusted with capital without understanding it.
3. **Third-party assets.** The repository contains no third-party datasets, images or vendored code.
   Frontend and Python dependencies are consumed from their package managers and are not
   redistributed here, so a license choice does not need to account for bundled third-party work.
   See `docs/public-boundary.md`.
4. **The research record.** The `docs/research-history.md` findings are published regardless of the
   code license. If you want those conclusions to be citable while the code is restricted, that is
   achievable with either option plus an explicit statement; no license here restricts reading them.

## How to apply a decision

Once chosen, add the license text at `LICENSE`, add a short **License** section to `README.md`
replacing the current one, update the wording in this document to record the decision and the date,
and note the choice in `CONTRIBUTING.md`.

No license file has been added, because choosing for the owner would be the one thing this milestone
should not do on their behalf.
