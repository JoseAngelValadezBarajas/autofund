"""``autofund secrets``: manage credentials without ever printing them.

    autofund secrets status                      which secrets exist, and where (never the values)
    autofund secrets set NAME                    type the value at a hidden prompt
    autofund secrets delete NAME
    autofund secrets import-env [PATH]           move NAME=value lines from a .env into the store
    autofund secrets import-env PATH --delete-source   ... and then overwrite and remove the file
    autofund secrets migrate-journal [PATH]      authenticate a live journal that predates authentication
    autofund secrets reset-journal [PATH]        forget that a journal was authenticated (new journal)
"""

import argparse
import getpass
import sys
from pathlib import Path

from autofund import secrets_store as store


def _overwrite_and_remove(path: Path) -> None:
    size = path.stat().st_size
    with path.open("r+b") as stream:
        stream.write(b"\0" * size)
        stream.flush()
    path.unlink()


def _read_value(name: str, *, from_stdin: bool) -> str:
    """One value, from a hidden prompt or standard input, without its line ending."""
    if from_stdin:
        return sys.stdin.readline().strip("\r\n")
    return getpass.getpass(f"{name} (hidden): ")


MIGRATE_PHRASE = "MIGRATE JOURNAL"
RESET_PHRASE = "RESET JOURNAL"


def _confirmed(args: argparse.Namespace, phrase: str) -> bool:
    typed = args.confirm if args.confirm is not None else input(f"Type {phrase} to continue: ")
    return typed == phrase


def _journal_command(args: argparse.Namespace) -> int:
    from datetime import UTC, datetime

    from autofund.live.journal import DEFAULT_JOURNAL, LiveExecutionJournal
    from autofund.live.models import LiveError

    path = Path(args.path) if args.path else DEFAULT_JOURNAL
    if args.command == "reset-journal":
        print(f"This forgets that {path} was ever authenticated. Only do it when you are deliberately")
        print("starting a new journal; anyone who stripped the authentication would want the same.")
        if not _confirmed(args, RESET_PHRASE):
            print("not confirmed; nothing changed")
            return 1
        print("marker cleared" if store.clear_journal_marker(path) else "there was no marker")
        return 0
    if not path.is_file():
        print(f"no journal at {path}; a new one is authenticated from its first record")
        return 0
    try:
        with LiveExecutionJournal(path) as journal:  # already authenticated: opens normally
            print(f"{path} is already authenticated ({len(journal.records)} records)")
            return 0
    except LiveError as exc:
        if str(exc) != "LIVE_JOURNAL_NEEDS_MIGRATION":
            print(f"error: {exc}")
            return 1
    with LiveExecutionJournal(path, migrate=True) as journal:
        tip = str(journal.records[-1].get("sequence")) if journal.records else "0"
        print(f"{path}: {len(journal.records)} records, last sequence {tip}, hash chain intact.")
        print("Migrating signs everything above as it stands now. Check that it is the history you")
        print("expect: if it was edited before this moment, those edits become the authenticated record.")
        if not _confirmed(args, MIGRATE_PHRASE):
            print("not confirmed; nothing changed")
            return 1
        journal.append("LIVE_JOURNAL_AUTHENTICATION_STARTED",
                       {"records_before": len(journal.records), "migrated_at": datetime.now(UTC).isoformat()})
        print("journal is now authenticated and anchored")
    return 0


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(prog="autofund secrets")
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("status")
    set_p = sub.add_parser("set")
    set_p.add_argument("name")
    set_p.add_argument("--stdin", action="store_true", help="read the value from standard input")
    del_p = sub.add_parser("delete")
    del_p.add_argument("name")
    imp = sub.add_parser("import-env")
    imp.add_argument("path", nargs="?", default=".env")
    imp.add_argument("--delete-source", action="store_true")
    for name in ("migrate-journal", "reset-journal"):
        jp = sub.add_parser(name)
        jp.add_argument("path", nargs="?")
        jp.add_argument("--confirm", default=None, help="the confirmation phrase, instead of typing it")
    args = parser.parse_args(argv)

    try:
        if args.command == "status":
            print(f"backend: {store.backend_name()}")
            for row in store.status():
                where = [w for w, on in (("environment", row["in_environment"]), ("store", row["in_store"])) if on]
                print(f"  {row['name']:48} {', '.join(where) if where else '-'}")
            return 0
        if args.command == "set":
            if args.name not in store.KNOWN_NAMES:
                print(f"unknown secret name; known: {', '.join(store.KNOWN_NAMES)}")
                return 2
            value = _read_value(args.name, from_stdin=args.stdin)
            store.set_secret(args.name, value)
            print(f"stored {args.name} in {store.backend_name()}")
            return 0
        if args.command == "delete":
            print(("deleted " if store.delete_secret(args.name) else "not present: ") + args.name)
            return 0
        if args.command in ("migrate-journal", "reset-journal"):
            return _journal_command(args)
        path = Path(args.path)
        if not path.is_file():
            print(f"no such file: {path}")
            return 2
        found = store.parse_env_file(path.read_text(encoding="utf-8"))
        if not found:
            print("no known secret names found in that file; nothing imported")
            return 1
        for name, value in found.items():
            store.set_secret(name, value)
            print(f"imported {name}")
        if args.delete_source:
            _overwrite_and_remove(path)
            print(f"overwrote and removed {path}")
        else:
            print(f"{path} still holds the values in plain text; rerun with --delete-source to remove it")
        return 0
    except store.SecretsError as exc:
        print(f"error: {exc}")
        return 1
