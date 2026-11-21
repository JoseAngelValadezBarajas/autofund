"""A1 certification: read real daily bars from Alpaca and record their provenance.

Reads credentials from ``.env`` (or the environment), fetches a fixed ETF set twice (raw and fully
adjusted), and writes ``research/results/a1_certification.json``. It prints counts and
fingerprints only, never a key, a secret or a bar.
"""

import json
import sys
from datetime import date
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from autofund.exchanges.alpaca.client import (  # noqa: E402
    AlpacaCredentials,
    AlpacaDataClient,
)
from autofund.exchanges.alpaca.env import load_credentials  # noqa: E402
from autofund.exchanges.alpaca.errors import AlpacaError  # noqa: E402
from autofund.lab.paths import research_root  # noqa: E402

SYMBOLS = ["SPY", "QQQ", "TLT", "GLD"]
START, END = date(2016, 1, 4), date(2024, 12, 31)
OUT = research_root() / "results" / "a1_certification.json"


def credentials() -> AlpacaCredentials:
    env_file = ROOT / ".env"
    if env_file.exists():
        return load_credentials(env_file)
    return AlpacaCredentials.from_env()


def main() -> int:
    try:
        client = AlpacaDataClient(credentials())
        report: dict[str, object] = {"symbols": SYMBOLS, "start": START.isoformat(),
                                     "end": END.isoformat(), "series": []}
        for adjustment in ("raw", "all"):
            for series in client.get_daily_bars(
                SYMBOLS, START, END, adjustment=adjustment, feed="iex"
            ).values():
                report["series"].append(series.telemetry())  # type: ignore[union-attr]
                print(f"{series.symbol:4} {adjustment:4} bars={len(series.candles):5} "
                      f"gaps={series.gaps} unclassified={series.unclassified_weekdays} "
                      f"fp={series.fingerprint[:16]}")
    except AlpacaError as exc:
        print(f"FAILED: {exc}")
        return 1
    OUT.parent.mkdir(parents=True, exist_ok=True)
    OUT.write_text(json.dumps(report, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    print(f"wrote {OUT.relative_to(research_root())}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
