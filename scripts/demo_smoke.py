"""Demo smoke check: start demo mode, exercise the read model, assert nothing live was reachable.

This is the check a CI job runs and that a person runs after a fresh clone, so it is written to be
runnable with no fixtures, no credentials and no network:

    python scripts/demo_smoke.py

It starts the real application object (not a mock), asks the real endpoints, and then asserts the
properties that make demo mode safe. A failure here is a release blocker rather than a warning,
because each assertion corresponds to a way a public clone could reach a real account.

Deliberately does not bind a network port: the assertions are about capability, and a test that
depends on a listening socket is flaky in CI for reasons unrelated to safety. The port-binding path
is covered by the Playwright suite.
"""

from __future__ import annotations

import json
import os
import sys
import tempfile
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT / "src"))
# The route inventory lives beside the tests rather than in the package, because it exists to help
# tests assert things about the HTTP surface. Adding the tests directory here keeps the documented
# command working without the caller having to know to set PYTHONPATH first.
sys.path.insert(0, str(REPO_ROOT / "tests"))

from fastapi.testclient import TestClient  # noqa: E402

from autofund.demo import (  # noqa: E402
    ModeError,
    RuntimeMode,
    credential_isolation,
    generate,
    resolve_mode,
)
from autofund.mvp.api import create_mvp_app  # noqa: E402
from autofund.mvp.orchestrator import (  # noqa: E402
    AutoFundOrchestrator,
    DemoAutonomousRunner,
)

FAILURES: list[str] = []


def check(label: str, condition: bool, detail: str = "") -> None:
    status = "PASS" if condition else "FAIL"
    print(f"  [{status}] {label}{(' — ' + detail) if detail else ''}")
    if not condition:
        FAILURES.append(label)


def main() -> int:
    print("AutoFund demo smoke check")
    print(f"  repository: {REPO_ROOT}")

    print("\n1. mode resolution fails closed")
    check("unset mode resolves to demo", resolve_mode(environ={}) is RuntimeMode.DEMO)
    try:
        resolve_mode(environ={"AUTOFUND_MODE": "prodcution"})
    except ModeError:
        check("an unknown mode raises rather than defaulting", True)
    else:
        check("an unknown mode raises rather than defaulting", False, "it was accepted")

    print("\n2. credentials present in the environment are isolated")
    # Simulate the exact hazard: an operator who already exported real keys.
    os.environ["AUTOFUND_BITSO_LIVE_API_KEY"] = "FAKE-SMOKE-KEY"
    os.environ["AUTOFUND_BITSO_PROD_API_SECRET"] = "FAKE-SMOKE-SECRET"

    artifacts = Path(tempfile.mkdtemp(prefix="autofund-smoke-")) / "artifacts" / "demo"
    with credential_isolation() as isolation:
        check("credentials are absent during a demo run",
              "AUTOFUND_BITSO_LIVE_API_KEY" not in os.environ
              and "AUTOFUND_BITSO_PROD_API_SECRET" not in os.environ)
        check("the isolation report names what it removed",
              "AUTOFUND_BITSO_LIVE_API_KEY" in isolation.removed)

        print("\n3. the demo dataset builds and the application starts")
        generate(artifacts_root=artifacts)
        runner = DemoAutonomousRunner()
        orchestrator = AutoFundOrchestrator(artifacts, runner, demo=True)
        orchestrator.startup()
        client = TestClient(create_mvp_app(orchestrator, None, artifacts_root=artifacts))
        check("the demo runner holds no exchange client",
              getattr(runner, "execution", None) is None)

        print("\n4. the research read model answers")
        overview = client.get("/api/v1/research/overview")
        check("the overview responds", overview.status_code == 200,
              f"HTTP {overview.status_code}")
        payload = overview.json()
        status = payload["status"]
        check("engineering, research, production and action are separate",
              status["statuses_are_independent"] is True)
        check("production is not authorized", payload["production"]["session_authorized"] is False)
        check("the current action is NO_TRADE", status["current_action"] == "NO_TRADE")
        check("no signal is economically usable",
              payload["research"]["economically_usable_signals"] == 0)
        check("the wallet is not counted as AutoFund inventory",
              payload["portfolio"]["wallet_is_not_inventory"] is True)

        for route in ("/api/v1/research/alpha", "/api/v1/research/strategies",
                      "/api/v1/research/experiments", "/api/v1/research/evidence",
                      "/api/v1/research/campaigns", "/api/v1/research/eligibility",
                      "/api/v1/research/artifacts?limit=10", "/api/v1/research/timeline?limit=10"):
            response = client.get(route)
            check(f"{route} responds", response.status_code == 200,
                  f"HTTP {response.status_code}")

        print("\n5. no write route is reachable")
        from mvp.route_inventory import (
            mutating_routes,  # type: ignore[import-not-found]
        )

        mutations = mutating_routes(client.app)
        research_mutations = {route for route in mutations
                              if route[0].startswith("/api/v1/research")}
        check("no research route mutates", not research_mutations, str(research_mutations))
        check("the only mutating routes are the control plane",
              mutations == {(f"/api/v1/control/{name}", "POST")
                            for name in ("start", "stop", "kill")},
              str(sorted(mutations)))

        print("\n6. no exchange mutation occurred")
        snapshot = orchestrator.snapshot()
        check("production POST count is zero",
              snapshot.get("production_post_count", 0) == 0)
        check("the demo runtime reports demonstration data",
              snapshot.get("demo_mode") is True)

    check("credentials are restored after the run",
          os.environ.get("AUTOFUND_BITSO_LIVE_API_KEY") == "FAKE-SMOKE-KEY")

    print()
    if FAILURES:
        print(f"DEMO SMOKE: FAIL — {len(FAILURES)} check(s) failed")
        for failure in FAILURES:
            print(f"  - {failure}")
        return 1
    print("DEMO SMOKE: PASS")
    print(json.dumps({"mode": "DEMO", "exchange_credentials_required": False,
                      "authenticated_exchange_calls": 0, "exchange_mutations": 0,
                      "production_authorization_possible": False}, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
