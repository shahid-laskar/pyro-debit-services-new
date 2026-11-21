"""P3 falsy-vs-null fixes (debit).

#13 A null/missing pyroId is stored as NULL, never as the string "None";
    a pyroId of 0 is kept.
#14 Missing balances are not reported as 0.0.
#15 Error responses with "data": null keep their real status code instead of
    becoming -1 (which would now park the record as 'outcome unknown').
"""

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from app.debit.processor import run_debit_batch
from app.debit.pyro_client import wallet_adjustment


def _tm(resp):
    tm = MagicMock()
    tm.api_key = "k"
    tm.session_token = "s"
    tm.secret_key = "74163581669665286567"
    tm.get_access_token = AsyncMock(return_value="acc")
    client = AsyncMock()
    client.post.return_value = resp
    tm.get_http_client.return_value = client
    return tm


def _resp(payload, http=200):
    resp = MagicMock()
    resp.json.return_value = payload
    resp.text = "x"
    resp.status_code = http
    return resp


async def _call(tm):
    with patch("app.debit.pyro_client.async_insert_debit_txn_log", new_callable=AsyncMock) as log:
        result = await wallet_adjustment(
            oracle_ref_id="R1", service_type="FANCYSALE", client_id="C1",
            source_msisdn="1", dest_msisdn="2", amount=1.0, mpin="1234",
            remarks="FANCYSALE", token_manager=tm,
        )
    return result, log


@pytest.mark.asyncio
class TestWalletAdjustmentNulls:

    async def test_error_with_null_data_keeps_status_code(self):
        result, log = await _call(_tm(_resp(
            {"statusCode": 5000, "status": "FAILED", "message": "Insufficient funds", "data": None})))
        assert result["statusCode"] == 5000
        assert log.call_args.kwargs["pyro_txn_id"] is None

    @pytest.mark.parametrize("pyro_id,expected", [(None, None), ("", None), (0, "0"), (89311635, "89311635")])
    async def test_pyro_id_logging(self, pyro_id, expected):
        _, log = await _call(_tm(_resp(
            {"statusCode": 2000, "status": "SUCCESS", "data": {"pyroId": pyro_id}})))
        assert log.call_args.kwargs["pyro_txn_id"] == expected


@pytest.mark.asyncio
@pytest.mark.parametrize("data,expected_id", [
    ({"pyroId": None}, None),
    ({}, None),
    ({"pyroId": 0}, "0"),
    ({"pyroId": 53909662, "balanceBefore": 292.28, "balanceAfter": 290.28}, "53909662"),
])
async def test_success_pyro_id_never_becomes_string_none(data, expected_id):
    adapter = MagicMock()
    adapter.service_type = "FANCYSALE"
    adapter.enabled = True
    adapter.batch_size = 1
    adapter.fetch_and_claim.return_value = [{"refid": "FS-1"}]
    adapter.get_record_ref.side_effect = lambda r: r["refid"]
    adapter.map_to_pyro_params.return_value = {
        "client_id": "C", "source_msisdn": "1", "dest_msisdn": "2",
        "amount": 1.0, "mpin": "12345", "remarks": "FANCYSALE",
    }
    resp = {"statusCode": 2000, "status": "SUCCESS", "message": "Transaction successful",
            "data": data, "_request_sent": True, "_http_status": 200}
    with patch("app.debit.processor.wallet_adjustment", new_callable=AsyncMock, return_value=resp):
        summary = await run_debit_batch(adapter)
    assert summary["success"] == 1
    assert adapter.mark_success.call_args.args[1] == expected_id
    remarks = adapter.mark_success.call_args.args[2]
    if "balanceBefore" not in data:
        assert "balBefore=0.0" not in remarks
