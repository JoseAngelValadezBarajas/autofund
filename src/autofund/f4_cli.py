"""Additive F4 routing; historical F3/F2 entry point is unchanged."""

import sys


def main() -> int:
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
