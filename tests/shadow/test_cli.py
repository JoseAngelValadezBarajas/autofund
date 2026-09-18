import json
import subprocess
import sys
from decimal import Decimal

import pytest

from autofund.observer import cli
from autofund.observer.models import ObservedBalance


def test_missing_operator_confirmation_exact_message(capsys):
    assert cli.main(["status"]) == 2
    result = json.loads(capsys.readouterr().out)
    assert (
        result["reason"]
        == "Dedicated Bitso production read-only API key must be confirmed."
    )


def test_balances_hidden_unless_explicit(monkeypatch, capsys):
    monkeypatch.setenv("AUTOFUND_BITSO_PROD_API_KEY", "synthetic")
    monkeypatch.setenv("AUTOFUND_BITSO_PROD_API_SECRET", "synthetic")
    monkeypatch.setenv("AUTOFUND_BITSO_PROD_READONLY_CONFIRMED", "true")
    balance = ObservedBalance(
        "mxn", Decimal("12345.67"), Decimal("0"), Decimal("12345.67")
    )
    monkeypatch.setattr(
        cli.BitsoProductionReadOnlyClient, "balances", lambda _: (balance,)
    )
    assert cli.main(["balances"]) == 0
    assert "12345" not in capsys.readouterr().out
    assert cli.main(["balances", "--show-balances"]) == 0
    assert "12345.67" in capsys.readouterr().out


@pytest.mark.parametrize(
    "args",
    [
        ["status", "--base-url", "https://stage.bitso.com"],
        ["status", "--api-key", "synthetic"],
        ["place-order"],
        ["cancel"],
    ],
)
def test_no_execution_or_url_cli_surface(args):
    with pytest.raises(SystemExit):
        cli.main(args)


def test_prod_shadow_import_graph_excludes_stage_surface():
    code = "import sys; import autofund.observer.cli; import autofund.shadow.cli; assert 'autofund.exchanges.bitso.client' not in sys.modules; assert 'autofund.exchanges.bitso.execution' not in sys.modules; assert 'autofund.exchanges.bitso.models' not in sys.modules"
    completed = subprocess.run(
        [sys.executable, "-c", code], capture_output=True, text=True
    )
    assert completed.returncode == 0, completed.stderr
