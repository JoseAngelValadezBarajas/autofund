"""Where the lab keeps personal data. Always outside what Git publishes."""

import os
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[3]
RESEARCH_ENV = "AUTOFUND_RESEARCH_DIR"


def research_root() -> Path:
    """The private research directory (git-ignored). Overridable for tests and other machines."""
    override = os.environ.get(RESEARCH_ENV)
    return Path(override) if override else REPO_ROOT / "research"


def env_file() -> Path:
    return REPO_ROOT / ".env"
