"""Additive F4 routing; historical F3/F2 entry point is unchanged."""

import sys


def main() -> int:
    # Safety invariants in this codebase are `assert` statements. `python -O` removes every one of
    # them, so a trading application must refuse to start in that mode rather than run with its
    # checks silently gone.
    if sys.flags.optimize:
        print("AutoFund refuses to run with python -O / PYTHONOPTIMIZE: safety assertions would be removed.")
        return 2
    if len(sys.argv) > 1 and sys.argv[1] == "app":
        from autofund.mvp.app import main as app_main

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
        from autofund.lab.app import main as lab_main

        return lab_main(sys.argv[2:])
    if len(sys.argv) > 1 and sys.argv[1] == "live":
        from autofund.live.cli import main as live_main

        return live_main(sys.argv[2:])
    if len(sys.argv) > 1 and sys.argv[1] == "dashboard":
        from autofund.dashboard.cli import main as dashboard_main

        return dashboard_main(sys.argv[2:])
    if len(sys.argv) > 1 and sys.argv[1] == "bitso-prod":
        from autofund.observer.cli import main as observer_main

        return observer_main(sys.argv[2:])
    if len(sys.argv) > 1 and sys.argv[1] == "shadow":
        from autofund.shadow.cli import main as shadow_main

        return shadow_main(sys.argv[2:])
    from autofund.cli import main as historical_main

    return historical_main()


if __name__ == "__main__":
    raise SystemExit(main())
