"""Additive F4 routing; historical F3/F2 entry point is unchanged."""

import os
import sys


def _hydrate(names: tuple[str, ...]) -> None:
    """Load the secrets a command needs from the credential store into this process only."""
    from autofund import secrets_store

    secrets_store.hydrate_environment(names)


def _app_wants_credentials(args: list[str]) -> bool:
    """True only for an explicit shadow/production run; demo never receives credentials."""
    mode = os.environ.get("AUTOFUND_MODE", "demo")
    for i, arg in enumerate(args):
        if arg == "--mode" and i + 1 < len(args):
            mode = args[i + 1]
        elif arg.startswith("--mode="):
            mode = arg.split("=", 1)[1]
        elif arg == "--demo":
            mode = "demo"
    return mode.lower() in ("shadow", "production")


def main() -> int:
    # Safety invariants in this codebase are `assert` statements. `python -O` removes every one of
    # them, so a trading application must refuse to start in that mode rather than run with its
    # checks silently gone.
    if sys.flags.optimize:
        print("AutoFund refuses to run with python -O / PYTHONOPTIMIZE: safety assertions would be removed.")
        return 2
    if len(sys.argv) > 1 and sys.argv[1] == "secrets":
        from autofund.secrets_cli import main as secrets_main

        return secrets_main(sys.argv[2:])
    if len(sys.argv) > 1 and sys.argv[1] == "app":
        from autofund import secrets_store
        from autofund.mvp.app import main as app_main

        if _app_wants_credentials(sys.argv[2:]):
            _hydrate(secrets_store.BITSO_NAMES)
        return app_main(sys.argv[2:])
    if len(sys.argv) > 1 and sys.argv[1] == "demo":
        # The public entry point. It is `app --mode demo` with the safe defaults already applied,
        # so the shortest command a new reader types is also the safest one. There is no separate
        # demo application: the same code path runs, with the mode deciding what it is allowed to
        # reach.
        from autofund.mvp.app import main as app_main

        return app_main(["--mode", "demo", *sys.argv[2:]], prog="autofund demo")
    if len(sys.argv) > 1 and sys.argv[1] == "lab":
        # Paper-only laboratory. Separate from demo so demo's "no credentials, no live capability"
        # guarantees are never weakened to make room for paper keys.
        from autofund import secrets_store
        from autofund.lab.app import main as lab_main

        _hydrate(secrets_store.ALPACA_NAMES)
        return lab_main(sys.argv[2:])
    if len(sys.argv) > 1 and sys.argv[1] == "live":
        from autofund import secrets_store
        from autofund.live.cli import main as live_main

        _hydrate(secrets_store.BITSO_NAMES)
        return live_main(sys.argv[2:])
    if len(sys.argv) > 1 and sys.argv[1] == "dashboard":
        from autofund.dashboard.cli import main as dashboard_main

        return dashboard_main(sys.argv[2:])
    if len(sys.argv) > 1 and sys.argv[1] == "bitso-prod":
        from autofund import secrets_store
        from autofund.observer.cli import main as observer_main

        _hydrate(secrets_store.BITSO_NAMES)
        return observer_main(sys.argv[2:])
    if len(sys.argv) > 1 and sys.argv[1] == "shadow":
        from autofund.shadow.cli import main as shadow_main

        return shadow_main(sys.argv[2:])
    from autofund.cli import main as historical_main

    return historical_main()


if __name__ == "__main__":
    raise SystemExit(main())
