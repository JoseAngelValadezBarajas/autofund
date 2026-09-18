"""Best-effort local dashboard launcher; it has no financial authority."""

import subprocess
import sys
import time
import webbrowser
from pathlib import Path

import httpx

URL = "http://127.0.0.1:8000"
_owned: subprocess.Popen[bytes] | None = None


def ensure_dashboard(session: Path, *, open_browser: bool = True) -> str:
    """Reuse or start localhost dashboard; failures never block a shadow session."""
    global _owned
    if not _ready():
        try:
            _owned = subprocess.Popen(
                [
                    sys.executable,
                    "-m",
                    "autofund.f4_cli",
                    "dashboard",
                    "--session",
                    str(session),
                    "--live-runtime",
                ],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
            )
            for _ in range(25):
                if _ready():
                    break
                time.sleep(0.1)
        except OSError:
            return URL
    if open_browser:
        try:
            webbrowser.open(URL, new=2)
        except Exception:
            pass
    return URL


def close_owned_dashboard() -> None:
    """Never terminate a server that this execution did not start."""
    global _owned
    process, _owned = _owned, None
    if process and process.poll() is None:
        try:
            process.terminate()
            process.wait(timeout=3)
        except (OSError, subprocess.TimeoutExpired):
            pass


def _ready() -> bool:
    try:
        response = httpx.get(URL + "/api/v1/health", timeout=0.2)
        data = response.json()
        return response.status_code == 200 and isinstance(data, dict) and data.get("application") == "AutoFund" and data.get("ready") is True and data.get("demo_mode") is False
    except (httpx.HTTPError, ValueError):
        return False
