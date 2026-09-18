import os
from decimal import Decimal

import pytest

from autofund.exchanges.bitso.auth import BitsoCredentials
from autofund.exchanges.bitso.certification import certify
from autofund.exchanges.bitso.client import BitsoClient
from autofund.exchanges.bitso.models import ExecutionPolicy

pytestmark = [pytest.mark.stage, pytest.mark.integration]


def test_stage_limit_lifecycle(tmp_path):
    if not all(
        os.getenv(k)
        for k in ("AUTOFUND_BITSO_STAGE_API_KEY", "AUTOFUND_BITSO_STAGE_API_SECRET")
    ):
        pytest.skip("Stage credentials absent")
    client = BitsoClient(BitsoCredentials.from_environment())
    result = certify(
        client,
        book_name="btc_mxn",
        journal_path=tmp_path / "stage.jsonl",
        output=tmp_path / "stage.json",
        policy=ExecutionPolicy(single_order_cap_mxn=Decimal("11")),
        confirm_stage=True,
    )
    assert result["result"] == "PASS"
