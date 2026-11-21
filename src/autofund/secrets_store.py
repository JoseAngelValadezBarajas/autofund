"""Where AutoFund's secrets live: the operating system's credential store, not plain files.

Credentials used to sit in ``.env`` and ``keys.json`` in plain text. This module is the replacement:

1. **The OS credential store** (Windows Credential Manager, macOS Keychain, Secret Service) when the
   optional ``keyring`` package is installed and has a usable backend.
2. **A private file** (``~/.autofund/secrets``, mode 0600 inside a 0700 directory) when it is not,
   so a machine without a keyring still never needs a world-readable ``.env``.

Environment variables still win when set, so CI and containers keep working. Nothing here ever
prints, logs or returns a secret in an error message: failures name the secret, never its value.

At start-up the CLI calls :func:`hydrate_environment`, which copies the secrets a command needs from
the store into *that process's* environment. Existing code that reads ``os.getenv`` therefore keeps
working unchanged, and the values never touch disk in plain text.
"""

import hashlib
import os
import re
import secrets as _secrets
from collections.abc import Iterable, MutableMapping
from pathlib import Path
from typing import Any

from autofund.demo.mode import CREDENTIAL_ENV_VARS

SERVICE = "autofund"
BACKEND_ENV = "AUTOFUND_SECRETS_BACKEND"  # "file" forces the private-file backend
DIR_ENV = "AUTOFUND_SECRETS_DIR"
JOURNAL_KEY_NAME = "AUTOFUND_JOURNAL_KEY"

ALPACA_NAMES: tuple[str, ...] = ("AUTOFUND_ALPACA_DATA_API_KEY", "AUTOFUND_ALPACA_DATA_API_SECRET")
BITSO_NAMES: tuple[str, ...] = tuple(CREDENTIAL_ENV_VARS)
KNOWN_NAMES: tuple[str, ...] = (*BITSO_NAMES, *ALPACA_NAMES, JOURNAL_KEY_NAME)

_NAME = re.compile(r"[A-Z][A-Z0-9_]{2,63}\Z")


class SecretsError(Exception):
    """Messages name a secret, never its value."""


def _check_name(name: str) -> str:
    if not isinstance(name, str) or not _NAME.fullmatch(name):
        raise SecretsError("secret names are upper-case letters, digits and underscores")
    return name


# ---- backends ----

def _keyring_module() -> Any:
    if os.environ.get(BACKEND_ENV) == "file":
        return None
    try:
        import keyring
        from keyring.backends import fail
    except Exception:  # keyring is optional
        return None
    backend = keyring.get_keyring()
    if isinstance(backend, fail.Keyring) or getattr(backend, "priority", 0) <= 0:
        return None
    return keyring


def secrets_dir() -> Path:
    override = os.environ.get(DIR_ENV)
    return Path(override) if override else Path.home() / ".autofund" / "secrets"


def backend_name() -> str:
    kr = _keyring_module()
    if kr is not None:
        backend = kr.get_keyring()
        return f"os-keyring:{type(backend).__name__}"
    return "private-file"


def _file_path(name: str) -> Path:
    return secrets_dir() / _check_name(name)


def _store_get(name: str) -> str | None:
    kr = _keyring_module()
    if kr is not None:
        try:
            value = kr.get_password(SERVICE, _check_name(name))
            return str(value) if value is not None else None
        except Exception:
            raise SecretsError(f"could not read {name} from the OS credential store") from None
    path = _file_path(name)
    try:
        value = path.read_text(encoding="utf-8").strip()
    except FileNotFoundError:
        return None
    except OSError:
        raise SecretsError(f"could not read {name} from the private secrets file") from None
    return value or None


def _store_set(name: str, value: str) -> None:
    if not isinstance(value, str) or not value.strip() or not value.isascii() or "\n" in value:
        raise SecretsError(f"{name} must be a non-empty single-line ASCII value")
    kr = _keyring_module()
    if kr is not None:
        try:
            kr.set_password(SERVICE, _check_name(name), value.strip())
        except Exception:
            raise SecretsError(f"could not write {name} to the OS credential store") from None
        return
    directory = secrets_dir()
    try:
        directory.mkdir(parents=True, exist_ok=True)
        try:
            os.chmod(directory, 0o700)
        except OSError:
            pass  # Windows ACLs are inherited from the user profile
        fd = os.open(_file_path(name), os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as stream:
            stream.write(value.strip() + "\n")
    except OSError:
        raise SecretsError(f"could not write {name} to the private secrets file") from None


def _store_delete(name: str) -> bool:
    kr = _keyring_module()
    if kr is not None:
        try:
            if kr.get_password(SERVICE, _check_name(name)) is None:
                return False
            kr.delete_password(SERVICE, name)
            return True
        except Exception:
            raise SecretsError(f"could not remove {name} from the OS credential store") from None
    try:
        _file_path(name).unlink()
        return True
    except FileNotFoundError:
        return False
    except OSError:
        raise SecretsError(f"could not remove {name} from the private secrets file") from None


# ---- public API ----

def resolve(name: str, environ: MutableMapping[str, str] | None = None) -> str | None:
    """The environment first (CI, containers), then the store."""
    env = os.environ if environ is None else environ
    return env.get(_check_name(name)) or _store_get(name)


def set_secret(name: str, value: str) -> None:
    _store_set(name, value)


def delete_secret(name: str) -> bool:
    return _store_delete(name)


def status(names: Iterable[str] = KNOWN_NAMES) -> list[dict[str, object]]:
    """Which secrets are available and from where. Values are never included."""
    rows = []
    for name in names:
        in_env = bool(os.environ.get(name))
        try:
            in_store = _store_get(name) is not None
        except SecretsError:
            in_store = False
        rows.append({"name": name, "in_environment": in_env, "in_store": in_store})
    return rows


def hydrate_environment(names: Iterable[str], environ: MutableMapping[str, str] | None = None) -> list[str]:
    """Copy stored secrets into this process's environment. Returns the names that were added."""
    env = os.environ if environ is None else environ
    added = []
    for name in names:
        if env.get(name):
            continue
        try:
            value = _store_get(name)
        except SecretsError:
            continue
        if value:
            env[name] = value
            added.append(name)
    return added


def journal_key(environ: MutableMapping[str, str] | None = None) -> bytes:
    """The key that authenticates the execution journal. Created once, then reused.

    It is generated from the operating system's CSPRNG on first use and kept in the same store as
    the other secrets, so a copy of the journal files alone cannot be re-signed.
    """
    existing = resolve(JOURNAL_KEY_NAME, environ)
    if existing:
        try:
            return bytes.fromhex(existing)
        except ValueError:
            return existing.encode()
    fresh = _secrets.token_hex(32)
    _store_set(JOURNAL_KEY_NAME, fresh)
    return bytes.fromhex(fresh)


def parse_env_file(text: str, names: Iterable[str] = KNOWN_NAMES) -> dict[str, str]:
    """Read ``NAME=value`` lines for known secret names only; everything else is ignored."""
    wanted = set(names)
    found: dict[str, str] = {}
    for raw in text.splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        name, value = (part.strip() for part in line.split("=", 1))
        if name in wanted:
            if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
                value = value[1:-1]
            found[name] = value
    return found


# ---- journal authentication marker ----
#
# The journal file cannot be trusted to say whether it was ever authenticated: someone who can edit it
# can strip the MACs and delete the anchor, and what is left looks exactly like an old journal that
# predates authentication. So the fact "this journal has been authenticated" is kept *here*, in the
# credential store, which is out of reach of anyone who can only edit files in the journal directory.

def journal_marker_name(journal_path: Path) -> str:
    """The marker is keyed by the path the application is *configured* to use, never a resolved one.

    ``Path.resolve()`` follows symlinks and junctions, so redirecting the journal directory elsewhere
    would change the key, hide the marker, and make an empty directory look like a brand-new journal.
    The configured absolute path does not move when a link is swapped in underneath it, so a
    redirected journal still finds its marker and is refused as missing. Case is folded only where the
    filesystem itself ignores it (Windows).
    """
    identity = os.path.normcase(os.path.abspath(journal_path))
    digest = hashlib.sha256(identity.encode()).hexdigest()[:24].upper()
    return f"AUTOFUND_JOURNAL_STATE_{digest}"


def journal_was_authenticated(journal_path: Path) -> bool:
    return resolve(journal_marker_name(journal_path), {}) == "AUTHENTICATED"


def mark_journal_authenticated(journal_path: Path) -> None:
    _store_set(journal_marker_name(journal_path), "AUTHENTICATED")


def clear_journal_marker(journal_path: Path) -> bool:
    """Deliberate operator reset (a new journal on purpose). Returns whether a marker existed."""
    return _store_delete(journal_marker_name(journal_path))
