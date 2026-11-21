"""A2: sample real top-of-book quotes and measure half-spreads per symbol.

Run during US market hours (about 15:00-20:00 UTC). Quotes outside the regular session are stored
but excluded from the headline figure. Writes ``research/results/a2_spreads.json``; prints only
symbols and basis points.

    python scripts/alpaca_measure_spreads.py [samples] [seconds_between]
"""

import json
import sys
import time
from datetime import UTC, datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from decimal import Decimal  # noqa: E402

from autofund.exchanges.alpaca.client import (  # noqa: E402
    AlpacaCredentials,
    AlpacaDataClient,
)
from autofund.exchanges.alpaca.env import load_credentials  # noqa: E402
from autofund.exchanges.alpaca.errors import AlpacaError  # noqa: E402
from autofund.lab.paths import research_root  # noqa: E402
from autofund.mvp.alpaca_economics import (  # noqa: E402
    ECONOMICS_VERSION,
    measure_spread,
    round_trip_friction,
)

SYMBOLS = ["SPY", "QQQ", "TLT", "GLD"]
OUT = research_root() / "results" / "a2_spreads.json"
SLIPPAGE_BPS_PER_SIDE = Decimal("1")  # declared assumption, UNVERIFIED until compared to fills


def main() -> int:
    samples = int(sys.argv[1]) if len(sys.argv) > 1 else 30
    pause = float(sys.argv[2]) if len(sys.argv) > 2 else 10
    env_file = ROOT / ".env"
    creds = load_credentials(env_file) if env_file.exists() else AlpacaCredentials.from_env()
    client = AlpacaDataClient(creds)
    recorded: dict[str, list] = {s: [] for s in SYMBOLS}
    try:
        for i in range(samples):
            for symbol, quote in client.get_latest_quotes(SYMBOLS, feed="iex").items():
                recorded[symbol].append(quote)
            if i + 1 < samples:
                time.sleep(pause)
    except AlpacaError as exc:
        print(f"FAILED: {exc}")
        return 1
    report: dict[str, object] = {"version": ECONOMICS_VERSION, "feed": "iex",
                                 "taken_at": datetime.now(UTC).isoformat(), "symbols": {}}
    for symbol, quotes in recorded.items():
        # A repeated identical quote is one observation, not several.
        unique = list({(q.at, q.bid, q.ask): q for q in quotes}.values())
        spread = measure_spread(symbol, unique)
        entry: dict[str, object] = {
            "quotes_total": len(quotes), "quotes_unique": len(unique),
            "in_session_samples": spread.samples,
            "excluded_out_of_session": spread.excluded_out_of_session}
        if spread.usable:
            friction = round_trip_friction(spread, slippage_bps_per_side=SLIPPAGE_BPS_PER_SIDE)
            entry.update(
                median_half_spread_bps=str(spread.median_half_spread_bps),
                round_trip_friction_bps_lower_bound=str(friction.total_bps),
                unknown_components=list(friction.unknown),
                components=[[c.name, None if c.bps is None else str(c.bps), c.provenance]
                            for c in friction.components])
            print(f"{symbol:4} half-spread {spread.median_half_spread_bps:.2f} bps | "
                  f"round trip >= {friction.total_bps:.2f} bps | n={spread.samples}")
        else:
            print(f"{symbol:4} UNMEASURED: no in-session quotes "
                  f"({spread.excluded_out_of_session} outside the regular session)")
        report["symbols"][symbol] = entry  # type: ignore[index]
    OUT.parent.mkdir(parents=True, exist_ok=True)
    OUT.write_text(json.dumps(report, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    print(f"wrote {OUT.relative_to(research_root())}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
