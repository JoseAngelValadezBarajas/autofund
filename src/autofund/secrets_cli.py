"""``autofund secrets``: manage credentials without ever printing them.

    autofund secrets status                      which secrets exist, and where (never the values)
    autofund secrets set NAME                    type the value at a hidden prompt
    autofund secrets delete NAME
    autofund secrets import-env [PATH]           move NAME=value lines from a .env into the store
    autofund secrets import-env PATH --delete-source   ... and then overwrite and remove the file
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
