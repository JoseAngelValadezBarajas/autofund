"""Evaluate a registered daily-momentum hypothesis. The holdout is evaluated exactly once.

    python scripts/alpaca_evaluate.py H-A2

Refuses unless ``research/registrations/<id>.json`` is tracked and unmodified in git, and refuses to
overwrite an existing result for that id.
"""

import json
import subprocess
import sys
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
from autofund.mvp.daily_momentum import (  # noqa: E402
    RegistrationError,
    build_panel,
    evaluate,
    load_registration,
)


def committed(path: Path) -> bool:
    """True when the registration is tracked and unmodified in the private research repository."""
    base = research_root()
    rel = path.relative_to(base).as_posix()
    tracked = subprocess.run(["git", "ls-files", "--error-unmatch", rel], cwd=base,
                             capture_output=True).returncode == 0
    clean = subprocess.run(["git", "status", "--porcelain", "--", rel], cwd=base,
                           capture_output=True, text=True).stdout.strip() == ""
    return tracked and clean


def main() -> int:
    if len(sys.argv) != 2 or not sys.argv[1].replace("-", "").isalnum():
        print("usage: alpaca_evaluate.py <registration-id>")
        return 2
    hid = sys.argv[1]
    registration = research_root() / "registrations" / f"{hid}.json"
    out = research_root() / "results" / f"{hid}_evaluation.json"
    if out.exists():
        print("REFUSED: the holdout was already evaluated. A new hypothesis needs a new registration.")
        return 1
    reg = load_registration(registration)
    if not committed(registration):
        print(f"REFUSED: commit research/registrations/{hid}.json (in the private research repo) first, unchanged, then rerun.")
        return 1
    env_file = ROOT / ".env"
    creds = load_credentials(env_file) if env_file.exists() else AlpacaCredentials.from_env()
    try:
        series = AlpacaDataClient(creds).get_daily_bars(
            list(reg.symbols), reg.start, reg.end, adjustment=reg.adjustment, feed=reg.feed)
        panel = build_panel(series, reg)
        result = evaluate(panel, reg, registration_committed=True)
    except (AlpacaError, RegistrationError) as exc:
        print(f"FAILED: {exc}")
        return 1
    result["data_fingerprints"] = {s: v.fingerprint for s, v in series.items()}
    result["common_trading_days"] = len(panel.dates)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(result, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    print(json.dumps({k: result[k] for k in ("selected_lookback", "holdout", "gates", "decision")},
                     indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
