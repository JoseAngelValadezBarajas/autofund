"""Secrets live in a credential store, are never printed, and never override explicit environment."""

import os
import stat
import sys

import pytest

from autofund import secrets_cli
from autofund import secrets_store as store

SECRET = "FAKE-value-that-must-never-be-printed-123456"
NAME = "AUTOFUND_ALPACA_DATA_API_SECRET"


# ---- the private-file backend (the suite forces it; see tests/conftest.py) ----

def test_round_trip_and_delete():
    assert store.backend_name() == "private-file"
    assert store.resolve(NAME, {}) is None
    store.set_secret(NAME, SECRET)
    assert store.resolve(NAME, {}) == SECRET
    assert store.delete_secret(NAME) is True and store.delete_secret(NAME) is False
    assert store.resolve(NAME, {}) is None


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX permission bits")
def test_files_are_private():
    store.set_secret(NAME, SECRET)
    path = store.secrets_dir() / NAME
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    assert stat.S_IMODE(store.secrets_dir().stat().st_mode) == 0o700


@pytest.mark.parametrize("bad", ["", "   ", "two\nlines", "non-ascii-é"])
def test_bad_values_are_refused_without_echoing_them(bad):
    with pytest.raises(store.SecretsError) as info:
        store.set_secret(NAME, bad)
    assert bad.strip() not in str(info.value) or not bad.strip()


@pytest.mark.parametrize("name", ["lower", "../escape", "A", "HAS SPACE", "X" * 80, "a/b"])
def test_names_cannot_escape_the_store(name):
    with pytest.raises(store.SecretsError):
        store.set_secret(name, "value")


def test_environment_wins_over_the_store(monkeypatch):
    store.set_secret(NAME, "from-store")
    assert store.resolve(NAME, {NAME: "from-env"}) == "from-env"
    assert store.resolve(NAME, {}) == "from-store"


def test_hydration_fills_only_missing_names_and_never_overrides():
    store.set_secret("AUTOFUND_ALPACA_DATA_API_KEY", "PKSTORE")
    store.set_secret(NAME, "stored-secret")
    env = {NAME: "explicit"}
    added = store.hydrate_environment(store.ALPACA_NAMES, env)
    assert added == ["AUTOFUND_ALPACA_DATA_API_KEY"]
    assert env == {NAME: "explicit", "AUTOFUND_ALPACA_DATA_API_KEY": "PKSTORE"}


def test_status_lists_names_and_locations_but_never_values():
    store.set_secret(NAME, SECRET)
    rows = store.status()
    assert {"name": NAME, "in_environment": False, "in_store": True} in rows
    assert SECRET not in repr(rows)


def test_journal_key_is_created_once_and_reused():
    first = store.journal_key({})
    assert len(first) == 32 and store.journal_key({}) == first
    assert store.journal_key({store.JOURNAL_KEY_NAME: "aa" * 32}) == bytes.fromhex("aa" * 32)
    assert store.journal_key({store.JOURNAL_KEY_NAME: "not-hex!"}) == b"not-hex!"


def test_env_file_parsing_reads_known_names_only():
    text = f"# c\nOTHER=1\n{NAME}='quoted'\nAUTOFUND_ALPACA_DATA_API_KEY=PKX\nnoise\n"
    assert store.parse_env_file(text) == {NAME: "quoted", "AUTOFUND_ALPACA_DATA_API_KEY": "PKX"}


# ---- an OS keyring backend, faked in memory ----

@pytest.fixture
def fake_keyring(monkeypatch):
    keyring = pytest.importorskip("keyring")
    from keyring.backend import KeyringBackend

    class Memory(KeyringBackend):
        priority = 5  # type: ignore[assignment]

        def __init__(self):
            super().__init__()
            self.data = {}

        def set_password(self, service, username, password):
            self.data[(service, username)] = password

        def get_password(self, service, username):
            return self.data.get((service, username))

        def delete_password(self, service, username):
            self.data.pop((service, username), None)

    previous = keyring.get_keyring()
    backend = Memory()
    keyring.set_keyring(backend)
    monkeypatch.delenv("AUTOFUND_SECRETS_BACKEND", raising=False)
    yield backend
    keyring.set_keyring(previous)


def test_the_os_keyring_is_used_when_available_and_no_file_is_written(fake_keyring):
    assert store.backend_name().startswith("os-keyring")
    store.set_secret(NAME, SECRET)
    assert fake_keyring.data[("autofund", NAME)] == SECRET
    assert store.resolve(NAME, {}) == SECRET
    assert not (store.secrets_dir() / NAME).exists()  # never touched the filesystem
    assert store.delete_secret(NAME) is True
    assert store.resolve(NAME, {}) is None


def test_file_backend_can_be_forced_even_when_a_keyring_exists(fake_keyring, monkeypatch):
    monkeypatch.setenv("AUTOFUND_SECRETS_BACKEND", "file")
    assert store.backend_name() == "private-file"


# ---- the CLI ----

def test_cli_set_reads_stdin_and_prints_no_value(monkeypatch, capsys):
    import io
    monkeypatch.setattr("sys.stdin", io.StringIO(SECRET + "\n"))
    assert secrets_cli.main(["set", NAME, "--stdin"]) == 0
    out = capsys.readouterr().out
    assert NAME in out and SECRET not in out
    assert secrets_cli.main(["status"]) == 0
    assert SECRET not in capsys.readouterr().out


def test_cli_refuses_unknown_secret_names(capsys):
    assert secrets_cli.main(["set", "SOME_OTHER_NAME", "--stdin"]) == 2


def test_import_env_moves_values_and_can_remove_the_plaintext_file(tmp_path, capsys):
    env_file = tmp_path / ".env"
    env_file.write_text(f"{NAME}={SECRET}\nAUTOFUND_ALPACA_DATA_API_KEY=PKFAKEKEY1\nUNRELATED=keep\n")
    assert secrets_cli.main(["import-env", str(env_file)]) == 0
    out = capsys.readouterr().out
    assert SECRET not in out and "still holds the values in plain text" in out
    assert store.resolve(NAME, {}) == SECRET and env_file.exists()
    assert secrets_cli.main(["import-env", str(env_file), "--delete-source"]) == 0
    assert not env_file.exists()


def test_import_env_with_no_known_names_changes_nothing(tmp_path):
    env_file = tmp_path / ".env"
    env_file.write_text("UNRELATED=1\n")
    assert secrets_cli.main(["import-env", str(env_file)]) == 1 and env_file.exists()


# ---- start-up hydration ----

@pytest.mark.parametrize("args,expected", [
    ([], False), (["--mode", "demo"], False), (["--demo"], False), (["--mode", "production"], True),
    (["--mode=shadow"], True), (["--mode", "production", "--demo"], False),
])
def test_only_explicit_real_modes_receive_credentials(monkeypatch, args, expected):
    from autofund.f4_cli import _app_wants_credentials
    monkeypatch.delenv("AUTOFUND_MODE", raising=False)
    assert _app_wants_credentials(args) is expected


def test_the_lab_command_hydrates_alpaca_secrets_from_the_store(monkeypatch):
    store.set_secret("AUTOFUND_ALPACA_DATA_API_KEY", "PKHYDRATED1")
    store.set_secret(NAME, "hydrated-secret")
    monkeypatch.delenv("AUTOFUND_ALPACA_DATA_API_KEY", raising=False)
    monkeypatch.delenv(NAME, raising=False)
    seen = {}

    def fake_lab(argv):
        seen["key"] = os.environ.get("AUTOFUND_ALPACA_DATA_API_KEY")
        return 0
    monkeypatch.setattr("autofund.lab.app.main", fake_lab)
    monkeypatch.setattr(sys, "argv", ["autofund", "lab"])
    from autofund.f4_cli import main
    assert main() == 0 and seen["key"] == "PKHYDRATED1"
    monkeypatch.delenv("AUTOFUND_ALPACA_DATA_API_KEY", raising=False)


def test_demo_never_hydrates(monkeypatch):
    store.set_secret("AUTOFUND_BITSO_PROD_API_KEY", "should-not-appear")
    monkeypatch.delenv("AUTOFUND_BITSO_PROD_API_KEY", raising=False)
    seen = {}
    monkeypatch.setattr("autofund.mvp.app.main", lambda argv, **k: seen.update(env=os.environ.get("AUTOFUND_BITSO_PROD_API_KEY")) or 0)
    monkeypatch.setattr(sys, "argv", ["autofund", "demo"])
    from autofund.f4_cli import main
    assert main() == 0 and seen["env"] is None
