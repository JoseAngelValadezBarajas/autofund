"""Strict, minimal ``.env`` loading for the two Alpaca market-data variables.

Only the two documented names are read and every other line is ignored. Values are never printed,
logged or included in an exception message, and nothing is written into ``os.environ``.
"""

from pathlib import Path

from .client import KEY_ENV, SECRET_ENV, AlpacaCredentials
from .errors import AlpacaAuthenticationError

_NAMES = frozenset({KEY_ENV, SECRET_ENV})


def parse_env_file(text: str) -> dict[str, str]:
    found: dict[str, str] = {}
    for raw in text.splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        name, value = line.split("=", 1)
        name, value = name.strip(), value.strip()
        if name not in _NAMES:
            continue
        if name in found:
            raise AlpacaAuthenticationError(f"{name} is defined more than once")
        if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
            value = value[1:-1]
        found[name] = value
    return found


def load_credentials(path: str | Path = ".env") -> AlpacaCredentials:
    try:
        text = Path(path).read_text(encoding="utf-8")
    except OSError:
        raise AlpacaAuthenticationError("could not read the credentials file") from None
    return AlpacaCredentials.from_env(parse_env_file(text))
