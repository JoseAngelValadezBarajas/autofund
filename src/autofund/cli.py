"""Dispatch new commands without changing the frozen F2 CLI."""

import sys


def main() -> int:
    if len(sys.argv) > 1 and sys.argv[1] == "bitso-stage":
        from autofund.exchanges.bitso.cli import main as stage_main

        return stage_main(sys.argv[2:])
    from autofund.market.cli import main as market_main

    return market_main()


if __name__ == "__main__":
    raise SystemExit(main())
