"""Public-safe Demo mode: a first-class runtime mode with a hard capability boundary.

A public repository has to be safe for someone who has never run it before, on a machine we know
nothing about. The specific hazard is a developer who *does* have exchange credentials exported in
their shell — or a `.env` left over from following the production instructions — and who then runs
a command expecting a harmless demonstration. If Demo mode inherits those credentials, the
demonstration is one code path away from being real.

So Demo mode is not "production with the dials turned down". It is a mode that **refuses to own the
capability at all**, enforced at three independent layers:

1. **Resolution fails closed.** The mode comes from an explicit argument or `AUTOFUND_MODE`. An
   unrecognised value raises rather than falling back, and the *absence* of any setting selects
   Demo. Production is never the implicit default, because the failure mode of guessing wrong in
   that direction is spending real money.

2. **Credentials are removed, not merely ignored.** Entering Demo strips every exchange credential
   variable from the process environment for the duration, restoring them on exit. Ignoring them
   would be enough for code we wrote today; removing them also covers a library that reads the
   environment itself, and a subprocess spawned from Demo.

3. **Live capability is asserted absent.** After startup, Demo asserts that no exchange client was
   constructed and that no authenticated request was made. This is the check that would catch a
   future refactor that quietly wired a real client back in.

The three are deliberately redundant. Each one alone is a single point of failure for a property
whose violation costs real money.
"""

from __future__ import annotations

import contextlib
import os
from collections.abc import Iterator
from dataclasses import dataclass
from enum import StrEnum

MODE_ENV_VAR = "AUTOFUND_MODE"

# Every variable that can carry exchange credentials. Demo removes all of them.
CREDENTIAL_ENV_VARS: tuple[str, ...] = (
    "AUTOFUND_BITSO_STAGE_API_KEY",
    "AUTOFUND_BITSO_STAGE_API_SECRET",
    "AUTOFUND_BITSO_LIVE_API_KEY",
    "AUTOFUND_BITSO_LIVE_API_SECRET",
    "AUTOFUND_BITSO_LIVE_PERMISSIONS_CONFIRMED",
    "AUTOFUND_BITSO_PROD_API_KEY",
    "AUTOFUND_BITSO_PROD_API_SECRET",
)

# Variables that alter how a client authenticates or where it points.
TRANSPORT_ENV_VARS: tuple[str, ...] = (
    "AUTOFUND_BITSO_API_BASE",
    "AUTOFUND_BITSO_PROD_API_BASE",
    "AUTOFUND_LIVE_JOURNAL",
    "AUTOFUND_HTTP_PROXY",
    "HTTPS_PROXY",
)


class ModeError(RuntimeError):
    """The requested runtime mode is not one this build recognises."""


class DemoIsolationError(RuntimeError):
    """A live capability was present or reachable while running in Demo mode."""


class RuntimeMode(StrEnum):
    """The three modes, in increasing order of consequence."""

    DEMO = "DEMO"
    SHADOW = "SHADOW"
    PRODUCTION = "PRODUCTION"

    @property
    def may_mutate_exchange(self) -> bool:
        """Only Production may ever mutate the exchange, and only when authorised."""
        return self is RuntimeMode.PRODUCTION

    @property
    def requires_credentials(self) -> bool:
        return self is not RuntimeMode.DEMO

    @property
    def is_public_safe(self) -> bool:
        """True when this mode is safe to run with no setup and no account."""
        return self is RuntimeMode.DEMO


# Accepted spellings, including the ones a person is likely to type. Anything else raises:
# a mode name we do not understand must not silently become one we do.
_ALIASES: dict[str, RuntimeMode] = {
    "demo": RuntimeMode.DEMO,
    "public": RuntimeMode.DEMO,
    "synthetic": RuntimeMode.DEMO,
    "shadow": RuntimeMode.SHADOW,
    "readonly": RuntimeMode.SHADOW,
    "production": RuntimeMode.PRODUCTION,
    "prod": RuntimeMode.PRODUCTION,
    "live": RuntimeMode.PRODUCTION,
}

#: The mode used when nothing at all is specified.
DEFAULT_MODE = RuntimeMode.DEMO


def resolve_mode(explicit: str | None = None, *,
                 environ: dict[str, str] | None = None) -> RuntimeMode:
    """Resolve the runtime mode, failing closed.

    Precedence is explicit argument, then `AUTOFUND_MODE`, then `DEFAULT_MODE`. An empty or
    unrecognised value raises rather than degrading: the whole point of this function is that a
    typo cannot become a live session.
    """
    source = os.environ if environ is None else environ
    raw = explicit if explicit is not None else source.get(MODE_ENV_VAR)
    if raw is None:
        return DEFAULT_MODE
    token = raw.strip().lower()
    if not token:
        raise ModeError(
            f"{MODE_ENV_VAR} is set but empty; use one of "
            f"{sorted(mode.value.lower() for mode in RuntimeMode)} or unset it to get "
            f"{DEFAULT_MODE.value}")
    mode = _ALIASES.get(token)
    if mode is None:
        raise ModeError(
            f"unrecognised {MODE_ENV_VAR}={raw!r}; expected one of "
            f"{sorted(mode.value.lower() for mode in RuntimeMode)}. Refusing to guess, because "
            f"guessing wrong could be a live session.")
    return mode


@dataclass
class IsolationReport:
    """What Demo removed, and what it verified. Contains no credential values."""

    removed: tuple[str, ...]
    restored: bool
    live_clients_constructed: int
    authenticated_requests: int

    @property
    def clean(self) -> bool:
        return not self.live_clients_constructed and not self.authenticated_requests

    def public(self) -> dict[str, object]:
        return {"removed_credential_variables": list(self.removed),
                "restored_on_exit": self.restored,
                "live_clients_constructed": self.live_clients_constructed,
                "authenticated_requests": self.authenticated_requests,
                "clean": self.clean}


@contextlib.contextmanager
def credential_isolation(*, environ: dict[str, str] | None = None) -> Iterator[IsolationReport]:
    """Remove every credential and transport override for the duration of a Demo run.

    A machine with valid credentials exported is still safe: the values are not merely unused, they
    are absent, so no library — ours or a dependency's — can pick them up, and neither can a
    subprocess. They are restored afterwards so a Demo run inside a larger shell does not surprise
    the operator.

    The report records variable *names* only. A cleanup routine that logged the values it removed
    would have copied the secret into a log file, which is the exact outcome it exists to prevent.
    """
    target = os.environ if environ is None else environ
    saved: dict[str, str] = {}
    removed: list[str] = []
    for name in (*CREDENTIAL_ENV_VARS, *TRANSPORT_ENV_VARS):
        if name in target:
            saved[name] = target.pop(name)
            removed.append(name)
    report = IsolationReport(removed=tuple(removed), restored=False,
                             live_clients_constructed=0, authenticated_requests=0)
    try:
        yield report
    finally:
        target.update(saved)
        report.restored = True


def assert_demo_is_isolated(*, report: IsolationReport,
                            snapshot: dict[str, object] | None = None) -> None:
    """Refuse to continue if Demo acquired a live capability.

    Called after startup. Either condition is a real defect rather than a warning: a Demo run that
    constructed a production client, or that performed an authenticated request, has crossed the
    boundary this module exists to hold.
    """
    if report.live_clients_constructed:
        raise DemoIsolationError(
            f"DEMO constructed {report.live_clients_constructed} live exchange client(s); "
            "demo must not own this capability")
    if report.authenticated_requests:
        raise DemoIsolationError(
            f"DEMO made {report.authenticated_requests} authenticated exchange request(s); "
            "demo must not reach an account")
    if snapshot is not None:
        posts = snapshot.get("production_post_count")
        if isinstance(posts, int) and posts:
            raise DemoIsolationError(f"DEMO reported {posts} exchange mutation(s); expected 0")


def demo_environment(environ: dict[str, str] | None = None) -> dict[str, str]:
    """The environment a Demo session should see: credentials provably absent."""
    source = os.environ if environ is None else environ
    return {name: value for name, value in source.items()
            if name not in CREDENTIAL_ENV_VARS}
