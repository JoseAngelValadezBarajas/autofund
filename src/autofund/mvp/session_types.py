"""Session constants and the small types the orchestrator and its runners share.

Split out of ``orchestrator.py`` so the runners (which place and reconcile orders) and the
orchestrator (which owns the session state machine) can each live in their own module without
importing one another.
"""

from dataclasses import dataclass
from decimal import Decimal
from enum import StrEnum
from typing import Any, Protocol

from autofund.decimal_utils import decimal
from autofund.live.models import LiveError

from .economics import (
    EconomicPolicy,
)
from .observability import MvpObservability

AUTHORIZED_CAPITAL = Decimal("50")
MAX_DEPLOYMENT = Decimal("25")
SINGLE_ORDER_CAP = Decimal("11")

# The account-confirmed taker rate, used wherever a research calculation needs the real fee but a
# runner may not be attached. A fallback of zero would make every derived threshold look trivially
# achievable, which is the failure this constant exists to prevent: the executable-dislocation
# threshold derived from a zero fee came out at 5 bps instead of 160.39, and 5 bps is a number a
# reader could mistake for a real requirement.
ACCOUNT_CONFIRMED_TAKER_RATE = Decimal("0.0078")
PREFLIGHT_PASS, PREFLIGHT_FAIL, PREFLIGHT_NOT_RUN = "PASS", "FAIL", "NOT_RUN"

# Canonical stop reasons. The backend is authoritative; the UI never infers a
# reason from elapsed time.
STOP_OPERATOR = "OPERATOR_STOP"
STOP_MAX_DURATION = "MAX_SESSION_DURATION_REACHED"
STOP_MAX_ORDERS = "MAX_SESSION_ORDERS_REACHED"
STOP_LOSS_LIMIT = "MAX_SESSION_LOSS_REACHED"
STOP_KILL_SWITCH = "KILL_SWITCH_ACTIVATED"
STOP_RUNNER_FAILURE = "EXECUTION_FAILURE"
STOP_BLOCKED_RECOVERY = "BLOCKED_RECOVERY_UNRESOLVED_ORDER"

# Expected slippage in basis points assumed by economic admission. AutoFund has
# no measured slippage history, so the honest default is zero and unmodelled
# friction is covered by the versioned `minimum_net_edge_bps` policy buffer.
# It is deliberately NOT derived from `slippage_tolerance`: that is an upper
# bound the order may not exceed, not a forecast of what it will pay.
DEFAULT_SLIPPAGE_BPS = Decimal("0")

# Internal liveness watchdog. Kept short so a stalled process is noticed quickly;
# a gap larger than the threshold means no execution was happening.
WATCHDOG_INTERVAL_SECONDS = 5.0
WATCHDOG_GAP_THRESHOLD_SECONDS = 60.0
# Operator-actionable notes for blocker names that are not self-explanatory. The
# gate itself is unchanged; these only describe what the exact blocker means.
BLOCKER_GUIDANCE: dict[str, str] = {
    "PERMISSIONS_FAIL": "Exchange key permissions are not attested; the exact F5 attestation variable must be "
                        "set to true by the operator out of band. This is never defaulted by the application.",
    "SLIPPAGE_POLICY_FAIL": "No explicit slippage policy is configured for the session.",
    "AUTHENTICATION_FAIL": "Production credentials were rejected; no trading is possible.",
    "EXCHANGE_AVAILABLE_FAIL": "Exchange MXN is below the single-order cap; the order is not executable.",
    "VALUE_LIMITS_FAIL": "Single-order cap is below the exchange minimum value; not executable.",
    "LIVE_CREDENTIALS_MISSING_OR_INVALID": "Production credentials are absent or invalid; set the F5 live "
                                          "credential environment variables before starting the application.",
    "PRODUCTION_PREFLIGHT_UNAVAILABLE": "The Production preflight could not read the exchange; no trading is possible.",
    "UNRESOLVED_ORDER": "A previous order is unresolved; reconciliation must complete before trading.",
    "EXECUTION_JOURNAL_IN_USE": "Another AutoFund process owns the live execution journal. Only one "
                                "instance may trade at a time; close the other instance and retry.",
}


class SessionStartBlocked(LiveError):
    """Recoverable readiness blocker found before a session is authorized.

    This is an ordinary "not ready" outcome, not an execution failure: no
    exchange write happened, automatic execution stays disabled and the
    application returns to STOPPED so the operator can retry once the blocker
    clears. HALTED stays reserved for genuinely unsafe states.
    """

    def __init__(self, blockers: tuple[str, ...]) -> None:
        self.blockers = tuple(blockers) or ("PRODUCTION_PREFLIGHT_BLOCKED",)
        super().__init__("PRODUCTION_PREFLIGHT_BLOCKED: " + ",".join(self.blockers))


class AppState(StrEnum):
    BOOTING = "BOOTING"
    RECOVERING = "RECOVERING"
    STOPPED = "STOPPED"
    STARTING = "STARTING"
    RUNNING = "RUNNING"
    STOPPING = "STOPPING"
    HALTED = "HALTED"
    ERROR = "ERROR"


@dataclass(frozen=True)
class SessionConfig:
    max_session_loss_mxn: Decimal = Decimal("10")
    max_session_duration_seconds: int = 3600
    max_orders_per_session: int = 10

    def __post_init__(self) -> None:
        loss = decimal(self.max_session_loss_mxn, "max_session_loss_mxn")
        if not Decimal("0") < loss <= AUTHORIZED_CAPITAL:
            raise ValueError("max_session_loss_mxn must be within (0, 50]")
        if not 60 <= self.max_session_duration_seconds <= 86_400:
            raise ValueError("max_session_duration_seconds must be within [60, 86400]")
        if not 1 <= self.max_orders_per_session <= 100:
            raise ValueError("max_orders_per_session must be within [1, 100]")


class TradingRunner(Protocol):
    observability: MvpObservability

    def startup(self) -> dict[str, Any]: ...
    def start(self, config: SessionConfig, event: Any) -> None: ...
    def stop(self) -> None: ...
    def kill(self) -> None: ...
    def snapshot(self) -> dict[str, Any]: ...
    @property
    def economic_policy(self) -> EconomicPolicy: ...
    def production_preflight(self) -> dict[str, Any]: ...
    def refresh_production_preflight(self, event: Any = None) -> dict[str, Any]: ...
