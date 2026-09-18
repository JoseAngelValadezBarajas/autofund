"""Best-effort local dashboard launcher; it has no financial authority."""

import subprocess
import sys
import time
import webbrowser
from pathlib import Path

import httpx

URL = "http://127.0.0.1:8000"


def ensure_dashboard(session: Path, *, open_browser: bool = True) -> str:
    """Reuse or start localhost dashboard; failures never block a shadow session."""
    if not _ready():
        try:
            subprocess.Popen(
                [
                    sys.executable,
                    "-m",
                    "autofund.f4_cli",
                    "dashboard",
                    "--session",
                    str(session),
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


def _ready() -> bool:
    try:
        return httpx.get(URL + "/api/v1/health", timeout=0.2).status_code == 200
    except httpx.HTTPError:
        return False
