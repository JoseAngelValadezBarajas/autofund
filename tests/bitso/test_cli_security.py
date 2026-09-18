import ast
import json
import sys
from dataclasses import replace
from datetime import UTC, datetime
from decimal import Decimal as D
from pathlib import Path

import pytest

from autofund.exchanges.bitso.auth import BitsoCredentials
from autofund.exchanges.bitso.certification import certify
from autofund.exchanges.bitso.cli import main, parser
from autofund.exchanges.bitso.errors import BitsoValidationError
from autofund.exchanges.bitso.models import ExecutionPolicy
from autofund.replay.serialization import fingerprint


@pytest.mark.parametrize("args", [["status"], ["balances"], ["fees", "btc_mxn"]])
def test_missing_credentials_do_not_connect(args, capsys):
    assert main(args) == 2
    output = capsys.readouterr().out
    assert json.loads(output)["result"] == "BLOCKED"
    assert "secret" not in output.lower()


def test_missing_credentials_produce_honest_artifact(tmp_path, capsys):
    output = tmp_path / "certification.json"
    assert main(["order-test", "--output", str(output)]) == 2
    data = json.loads(output.read_text())
    assert data["result"] == "SKIPPED" and data["order_lifecycle"] == "NOT_EXECUTED"
    semantic = {
        k: v for k, v in data.items() if k not in ("timestamp", "semantic_fingerprint")
    }
    assert fingerprint(semantic) == data["semantic_fingerprint"]


def test_cli_has_no_host_secret_or_production_options():
    for args in (
        ["status", "--base-url", "https://bitso.com"],
        ["status", "--api-key", "synthetic"],
        ["production"],
    ):
        with pytest.raises(SystemExit):
            parser().parse_args(args)


def test_root_dispatch_preserves_f2(monkeypatch):
    import autofund.market.cli
    from autofund.cli import main as root_main

    monkeypatch.setattr(sys, "argv", ["autofund", "market", "info"])
    monkeypatch.setattr(autofund.market.cli, "main", lambda: 7)
    assert root_main() == 7


def test_no_dangerous_execution_methods_or_env_names():
    root = Path(__file__).parents[2] / "src/autofund/exchanges"
    banned = {
        "withdraw",
        "withdrawal",
        "transfer",
        "margin",
        "futures",
        "leverage",
        "replace_order",
        "cancel_all",
    }
    for path in root.rglob("*.py"):
        tree = ast.parse(path.read_text())
        for node in ast.walk(tree):
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                assert not any(name in node.name for name in banned)
            if (
                isinstance(node, ast.Call)
                and isinstance(node.func, ast.Attribute)
                and node.func.attr == "getenv"
            ):
                assert node.args[0].value in {
                    "AUTOFUND_BITSO_STAGE_API_KEY",
                    "AUTOFUND_BITSO_STAGE_API_SECRET",
                }


def test_no_real_file_credential_loader(monkeypatch):
    monkeypatch.setenv("AUTOFUND_BITSO_STAGE_API_KEY", "synthetic-stage-key")
    monkeypatch.setenv("AUTOFUND_BITSO_STAGE_API_SECRET", "synthetic-stage-secret")
    credentials = BitsoCredentials.from_environment()
    assert credentials.api_key == "synthetic-stage-key"
    assert "synthetic" not in repr(credentials)


def test_certification_dry_run_and_confirmed_artifacts(
    tmp_path, exchange, book, fees, monkeypatch
):
    # A complete offline CLI service exercise with real risk/journal/reconciliation.
    exchange.get_book = lambda _: book
    exchange.get_fees = lambda _: fees
    exchange.get_depth = lambda _: {
        "bids": [{"price": "1000"}],
        "asks": [{"price": "1020"}],
        "updated_at": datetime.now(UTC).isoformat(),
    }
    policy = ExecutionPolicy(single_order_cap_mxn=D("12"))
    result = certify(
        exchange,
        book_name="btc_mxn",
        journal_path=tmp_path / "dry.jsonl",
        output=tmp_path / "dry.json",
        policy=policy,
        confirm_stage=False,
    )
    assert result["result"] == "DRY_RUN" and not exchange.posts
    result = certify(
        exchange,
        book_name="btc_mxn",
        journal_path=tmp_path / "live.jsonl",
        output=tmp_path / "live.json",
        policy=policy,
        confirm_stage=True,
    )
    assert (
        result["result"] == "PASS"
        and len(exchange.posts) == 1
        and len(exchange.cancels) == 1
    )
    artifact = json.loads((tmp_path / "live.json").read_text())
    semantic = {
        k: v
        for k, v in artifact.items()
        if k not in ("started_at", "finished_at", "semantic_fingerprint")
    }
    assert artifact["semantic_fingerprint"] == fingerprint(semantic)
    assert "authorization" not in json.dumps(artifact).lower()


def test_certification_does_not_raise_cap_to_force_order(
    tmp_path, exchange, book, fees
):
    exchange.get_book = lambda _: replace(book, minimum_value=D("100"))
    exchange.get_fees = lambda _: fees
    exchange.get_depth = lambda _: {
        "bids": [{"price": "1000"}],
        "asks": [{"price": "1020"}],
        "updated_at": datetime.now(UTC).isoformat(),
    }
    with pytest.raises(BitsoValidationError):
        certify(
            exchange,
            book_name="btc_mxn",
            journal_path=tmp_path / "cap.jsonl",
            output=tmp_path / "cap.json",
            policy=ExecutionPolicy(),
            confirm_stage=True,
        )
    assert not exchange.posts
    assert json.loads((tmp_path / "cap.json").read_text())["result"] == "FAIL"
