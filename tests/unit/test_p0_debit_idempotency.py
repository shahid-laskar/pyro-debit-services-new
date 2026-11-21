"""P0 debit idempotency and unknown-outcome handling.

Covers:
1. classify_debit_attempts: SUCCESS / UNKNOWN / NONE from debit_txn_log history.
2. run_debit_batch never calls Pyro for a ref with a prior SUCCESS or UNKNOWN attempt,
   fails closed when the history is unreadable, and requires a durable intent row.
3. Ambiguous outcomes (timeout, transport error, parse failure, HTTP 5xx, success code
   without SUCCESS) are parked for reconciliation, never marked 'R'.
4. Stuck cleanup never resets a 'P' row whose debit may already have reached Pyro,
   and skips the reset entirely when the history cannot be read.
5. wallet_adjustment reports connection failures as "not sent".
"""

from unittest.mock import AsyncMock, MagicMock, patch

import httpx
import pytest

from app.db.debit_log import (
    OUTCOME_NONE,
    OUTCOME_SUCCESS,
    OUTCOME_UNKNOWN,
    classify_debit_attempts,
)
from app.debit.processor import _is_outcome_unknown, run_debit_batch
from app.debit.pyro_client import wallet_adjustment
from app.debit.services.esim import EsimAdapter, build_esim_cleanup_query
from app.debit.services.fancysale import FancySaleAdapter, build_fancysale_cleanup_query
from app.debit.services.simswap import SimswapAdapter, build_simswap_cleanup_query
from app.debit.stuck_guard import derive_stuck_select


def _row(stage, is_success="N", status=None, http=None, pyro_id=None, err=None):
    return {
        "api_stage": stage, "is_success": is_success, "pyro_status_code": status,
        "response_http_code": http, "pyro_txn_id": pyro_id, "error_class": err,
    }


# ══════════════════════════════════════════════════════════════════════════════
# 1. History classification
# ══════════════════════════════════════════════════════════════════════════════

class TestClassifyDebitAttempts:

    def test_no_history_is_none(self):
        assert classify_debit_attempts([]) == (OUTCOME_NONE, None)

    def test_success_is_success_with_pyro_id(self):
        rows = [_row("DEBIT_INTENT"), _row("DEBIT", "Y", 2000, 200, "89311635")]
        assert classify_debit_attempts(rows) == (OUTCOME_SUCCESS, "89311635")

    def test_intent_without_result_is_unknown(self):
        assert classify_debit_attempts([_row("DEBIT_INTENT")])[0] == OUTCOME_UNKNOWN

    def test_timeout_is_unknown(self):
        rows = [_row("DEBIT_INTENT"), _row("DEBIT", "N", -1, None, err="httpx.TimeoutException")]
        assert classify_debit_attempts(rows)[0] == OUTCOME_UNKNOWN

    def test_legacy_timeout_without_intent_is_unknown(self):
        assert classify_debit_attempts([_row("DEBIT", "N", -1)])[0] == OUTCOME_UNKNOWN

    def test_http_5xx_is_unknown(self):
        rows = [_row("DEBIT_INTENT"), _row("DEBIT", "N", 5000, 502)]
        assert classify_debit_attempts(rows)[0] == OUTCOME_UNKNOWN

    def test_definitive_failure_is_none(self):
        rows = [_row("DEBIT_INTENT"), _row("DEBIT", "N", 5000, 200)]
        assert classify_debit_attempts(rows)[0] == OUTCOME_NONE

    def test_qb_repick_after_definitive_failure_is_none(self):
        rows = [
            _row("DEBIT_INTENT"), _row("DEBIT", "N", 5000, 200),   # Insufficient funds
            _row("DEBIT_INTENT"), _row("DEBIT", "N", 5000, 200),   # re-picked from QB
        ]
        assert classify_debit_attempts(rows)[0] == OUTCOME_NONE

    @pytest.mark.parametrize("err", ["TokenRefreshFailed", "RequestNotSent"])
    def test_not_sent_closes_intent(self, err):
        rows = [_row("DEBIT_INTENT"), _row("DEBIT", "N", -1, None, err=err)]
        assert classify_debit_attempts(rows)[0] == OUTCOME_NONE

    def test_mapping_failure_row_is_none(self):
        assert classify_debit_attempts([_row("DEBIT", "N", None)])[0] == OUTCOME_NONE

    def test_writeback_failure_reconciliation_row_is_success(self):
        rows = [
            _row("DEBIT_INTENT"), _row("DEBIT", "Y", 2000, 200, "P1"),
            _row("RECONCILIATION_REQUIRED", "Y", 2000, 200, "P1"),
        ]
        assert classify_debit_attempts(rows) == (OUTCOME_SUCCESS, "P1")

    def test_ambiguous_reconciliation_row_is_unknown(self):
        rows = [_row("DEBIT_INTENT"), _row("RECONCILIATION_REQUIRED", "N", -1)]
        assert classify_debit_attempts(rows)[0] == OUTCOME_UNKNOWN

    def test_success_not_undone_by_later_failure(self):
        rows = [_row("DEBIT", "Y", 2000, 200, "P1"), _row("DEBIT_INTENT"), _row("DEBIT", "N", 5000, 200)]
        assert classify_debit_attempts(rows) == (OUTCOME_SUCCESS, "P1")

    def test_manual_reconciled_not_debited_reopens(self):
        rows = [_row("DEBIT_INTENT"), _row("DEBIT", "N", -1), _row("MANUAL_RECONCILED", "N")]
        assert classify_debit_attempts(rows)[0] == OUTCOME_NONE

    def test_manual_reconciled_debited_is_success(self):
        rows = [_row("DEBIT_INTENT"), _row("MANUAL_RECONCILED", "Y", pyro_id="P9")]
        assert classify_debit_attempts(rows) == (OUTCOME_SUCCESS, "P9")

    def test_idempotent_skip_rows_are_ignored(self):
        rows = [_row("DEBIT", "Y", 2000, 200, "P1"), _row("IDEMPOTENT_SKIP", "N")]
        assert classify_debit_attempts(rows) == (OUTCOME_SUCCESS, "P1")


# ══════════════════════════════════════════════════════════════════════════════
# 2–3. Processor
# ══════════════════════════════════════════════════════════════════════════════

def _adapter(n=1):
    adapter = MagicMock()
    adapter.service_type = "FANCYSALE"
    adapter.enabled = True
    adapter.batch_size = 10
    adapter.token_manager = MagicMock()
    records = [{"refid": f"FS-{i}"} for i in range(n)]
    adapter.fetch_and_claim.return_value = records
    adapter.get_record_ref.side_effect = lambda r: r["refid"]
    adapter.map_to_pyro_params.return_value = {
        "client_id": "CLI", "source_msisdn": "9400000001", "dest_msisdn": "9400000002",
        "amount": 1.0, "mpin": "12345", "remarks": "FANCYSALE",
    }
    return adapter


@pytest.mark.asyncio
class TestProcessorIdempotency:

    async def test_prior_success_marks_success_without_calling_pyro(self, _default_debit_history):
        _default_debit_history["fetch_outcomes"].return_value = {"FS-0": (OUTCOME_SUCCESS, "89311635")}
        adapter = _adapter()
        with patch("app.debit.processor.wallet_adjustment", new_callable=AsyncMock) as pyro, \
             patch("app.debit.processor.async_insert_debit_txn_log", new_callable=AsyncMock):
            summary = await run_debit_batch(adapter)
        pyro.assert_not_called()
        _default_debit_history["insert_intent"].assert_not_called()
        adapter.mark_success.assert_called_once()
        assert adapter.mark_success.call_args[0][1] == "89311635"
        adapter.mark_failed.assert_not_called()
        assert summary["success"] == 1

    async def test_prior_unknown_parks_without_calling_pyro(self, _default_debit_history):
        _default_debit_history["fetch_outcomes"].return_value = {"FS-0": (OUTCOME_UNKNOWN, None)}
        adapter = _adapter()
        with patch("app.debit.processor.wallet_adjustment", new_callable=AsyncMock) as pyro, \
             patch("app.debit.processor.async_insert_debit_txn_log", new_callable=AsyncMock):
            summary = await run_debit_batch(adapter)
        pyro.assert_not_called()
        adapter.mark_reconciliation_required.assert_called_once()
        adapter.mark_failed.assert_not_called()
        adapter.mark_success.assert_not_called()
        assert summary["reconciliation_required"] == 1

    async def test_unreadable_history_fails_closed(self, _default_debit_history):
        _default_debit_history["fetch_outcomes"].side_effect = RuntimeError("pg down")
        adapter = _adapter(3)
        with patch("app.debit.processor.wallet_adjustment", new_callable=AsyncMock) as pyro:
            summary = await run_debit_batch(adapter)
        pyro.assert_not_called()
        adapter.mark_failed.assert_not_called()
        assert summary["deferred"] == 3

    async def test_intent_write_failure_skips_pyro_call(self, _default_debit_history):
        _default_debit_history["insert_intent"].side_effect = RuntimeError("pg down")
        adapter = _adapter()
        with patch("app.debit.processor.wallet_adjustment", new_callable=AsyncMock) as pyro:
            summary = await run_debit_batch(adapter)
        pyro.assert_not_called()
        adapter.mark_failed.assert_not_called()
        assert summary["deferred"] == 1

    async def test_intent_written_before_pyro_call(self, _default_debit_history):
        order = []
        _default_debit_history["insert_intent"].side_effect = lambda **kw: order.append("intent")

        async def fake_pyro(**kwargs):
            order.append("pyro")
            return {"statusCode": 2000, "status": "SUCCESS", "data": {"pyroId": 1},
                    "_request_sent": True, "_http_status": 200}

        adapter = _adapter()
        with patch("app.debit.processor.wallet_adjustment", side_effect=fake_pyro):
            await run_debit_batch(adapter)
        assert order == ["intent", "pyro"]
        kw = _default_debit_history["insert_intent"].call_args.kwargs
        assert kw["oracle_ref_id"] == "FS-0" and kw["client_id"] == "CLI"

    @pytest.mark.parametrize("response", [
        {"statusCode": -1, "status": "ERROR", "message": "Request timed out",
         "_request_sent": True, "_http_status": None},
        {"statusCode": -1, "status": "ERROR", "message": "Response parse failed",
         "_request_sent": True, "_http_status": 200},
        {"statusCode": 5000, "status": "FAILED", "message": "Something went wrong",
         "_request_sent": True, "_http_status": 502},
        {"statusCode": 2000, "status": "PENDING", "message": "?",
         "_request_sent": True, "_http_status": 200},
        {"status": "FAILED", "message": "no status code",
         "_request_sent": True, "_http_status": 200},
    ])
    async def test_ambiguous_outcome_is_parked_not_rejected(self, response):
        adapter = _adapter()
        with patch("app.debit.processor.wallet_adjustment", new_callable=AsyncMock, return_value=response), \
             patch("app.debit.processor.async_insert_debit_txn_log", new_callable=AsyncMock) as log:
            summary = await run_debit_batch(adapter)
        adapter.mark_failed.assert_not_called()
        adapter.mark_reconciliation_required.assert_called_once()
        assert summary["reconciliation_required"] == 1
        assert summary["failed"] == 0
        stages = [c.kwargs["api_stage"] for c in log.call_args_list]
        assert "RECONCILIATION_REQUIRED" in stages
        recon = next(c.kwargs for c in log.call_args_list if c.kwargs["api_stage"] == "RECONCILIATION_REQUIRED")
        assert recon["is_success"] == "N"

    async def test_park_survives_oracle_failure(self):
        adapter = _adapter()
        adapter.mark_reconciliation_required.side_effect = RuntimeError("ORA-03113")
        resp = {"statusCode": -1, "message": "Request timed out", "_request_sent": True}
        with patch("app.debit.processor.wallet_adjustment", new_callable=AsyncMock, return_value=resp), \
             patch("app.debit.processor.async_insert_debit_txn_log", new_callable=AsyncMock):
            summary = await run_debit_batch(adapter)
        adapter.mark_failed.assert_not_called()
        assert summary["reconciliation_required"] == 1

    @pytest.mark.parametrize("response", [
        {"statusCode": 5000, "status": "FAILED", "message": "Insufficient funds",
         "_request_sent": True, "_http_status": 200},
        {"statusCode": 5000, "status": "FAILED", "message": "Invalid mpin for source msisdn: 8903696189",
         "_request_sent": True, "_http_status": 200},
        # Token / connection failures that never reached Pyro are released, not
        # rejected: see test_p2_debit_auth_and_lock.py
    ])
    async def test_definitive_failure_still_marks_rejected(self, response):
        adapter = _adapter()
        with patch("app.debit.processor.wallet_adjustment", new_callable=AsyncMock, return_value=response):
            summary = await run_debit_batch(adapter)
        adapter.mark_failed.assert_called_once()
        adapter.mark_reconciliation_required.assert_not_called()
        assert summary["failed"] == 1


def test_is_outcome_unknown_legacy_response_without_metadata():
    # Responses without the metadata keys are treated as "sent".
    assert _is_outcome_unknown({"statusCode": -1, "message": "x"}) is True
    assert _is_outcome_unknown({"statusCode": 5000, "message": "Invalid mpin"}) is False


# ══════════════════════════════════════════════════════════════════════════════
# 4. Stuck cleanup guard
# ══════════════════════════════════════════════════════════════════════════════

class TestDeriveStuckSelect:

    @pytest.mark.parametrize("builder,pk", [
        (build_fancysale_cleanup_query, "REFID"),
        (build_simswap_cleanup_query, "ID"),
        (build_esim_cleanup_query, "ID"),
    ])
    def test_select_keeps_exact_where_clause(self, builder, pk):
        sql, _ = builder(stuck_minutes=10, context=None, active_refs={"7"})
        select = derive_stuck_select(sql, pk)
        assert select.startswith(f"SELECT {pk}\nFROM ")
        assert "SET" not in select.upper().split("WHERE")[0]
        # identical predicates, character for character
        assert select.split("WHERE", 1)[1] == sql.split("WHERE", 1)[1]

    def test_rejects_unexpected_sql(self):
        with pytest.raises(ValueError):
            derive_stuck_select("DELETE FROM x WHERE 1=1", "ID")


def _oracle(stuck_rows):
    cur = MagicMock()
    cur.fetchall.return_value = stuck_rows
    cur.rowcount = 0
    conn = MagicMock()
    conn.cursor.return_value = cur
    factory = MagicMock()
    factory.return_value.__enter__.return_value = conn
    return factory, cur


@pytest.mark.parametrize("adapter_cls,module,pk_key", [
    (FancySaleAdapter, "fancysale", "refid"),
    (SimswapAdapter, "simswap", "id"),
    (EsimAdapter, "esim", "id"),
])
class TestStuckCleanupGuard:

    def test_unsafe_rows_marked_and_excluded_from_reset(self, adapter_cls, module, pk_key, _default_debit_history):
        _default_debit_history["guard_outcomes"].return_value = {
            "101": (OUTCOME_SUCCESS, "P1"),
            "102": (OUTCOME_UNKNOWN, None),
            "103": (OUTCOME_NONE, None),
        }
        adapter = adapter_cls(token_manager=MagicMock(), enabled=True)
        factory, cur = _oracle([(101,), (102,), (103,)])
        with patch(f"app.debit.services.{module}.get_oracle_conn", factory), \
             patch.object(adapter_cls, "mark_reconciliation_required") as mark:
            adapter.reset_stuck_processing(10, active_refs=set())

        marked = sorted(c.args[0][pk_key] for c in mark.call_args_list)
        assert marked == [101, 102]
        select_sql, select_params = cur.execute.call_args_list[0].args
        assert select_sql.lstrip().upper().startswith("SELECT")
        assert select_params["stuck_minutes"] == 9
        update_sql, update_params = cur.execute.call_args_list[-1].args
        assert update_sql.lstrip().upper().startswith("UPDATE")
        assert update_params["stuck_minutes"] == 10
        excluded = {v for k, v in update_params.items() if k.startswith("act_")}
        assert {101, 102} <= excluded and 103 not in excluded

    def test_unreadable_history_skips_reset(self, adapter_cls, module, pk_key, _default_debit_history):
        _default_debit_history["guard_outcomes"].side_effect = RuntimeError("pg down")
        adapter = adapter_cls(token_manager=MagicMock(), enabled=True)
        factory, cur = _oracle([(101,)])
        with patch(f"app.debit.services.{module}.get_oracle_conn", factory):
            assert adapter.reset_stuck_processing(10, active_refs=set()) == 0
        executed = [c.args[0].lstrip().upper()[:6] for c in cur.execute.call_args_list]
        assert executed == ["SELECT"]

    def test_no_stuck_rows_resets_normally(self, adapter_cls, module, pk_key, _default_debit_history):
        adapter = adapter_cls(token_manager=MagicMock(), enabled=True)
        factory, cur = _oracle([])
        with patch(f"app.debit.services.{module}.get_oracle_conn", factory):
            adapter.reset_stuck_processing(10, active_refs=set())
        _default_debit_history["guard_outcomes"].assert_not_called()
        assert cur.execute.call_args_list[-1].args[0].lstrip().upper().startswith("UPDATE")


# ══════════════════════════════════════════════════════════════════════════════
# 5. wallet_adjustment transport metadata
# ══════════════════════════════════════════════════════════════════════════════

def _tm(post_side_effect=None, post_return=None):
    tm = MagicMock()
    tm.api_key = "k"
    tm.session_token = "s"
    tm.secret_key = "74163581669665286567"
    tm.get_access_token = AsyncMock(return_value="acc")
    client = AsyncMock()
    if post_side_effect is not None:
        client.post.side_effect = post_side_effect
    else:
        client.post.return_value = post_return
    tm.get_http_client.return_value = client
    return tm


async def _call(tm):
    return await wallet_adjustment(
        oracle_ref_id="R1", service_type="FANCYSALE", client_id="C1",
        source_msisdn="1", dest_msisdn="2", amount=1.0, mpin="1234",
        remarks="FANCYSALE", token_manager=tm,
    )


@pytest.mark.asyncio
class TestWalletAdjustmentMetadata:

    @pytest.mark.parametrize("exc", [
        httpx.ConnectError("refused"), httpx.ConnectTimeout("slow"), httpx.PoolTimeout("pool"),
    ])
    async def test_connection_failures_are_not_sent(self, exc):
        with patch("app.debit.pyro_client.async_insert_debit_txn_log", new_callable=AsyncMock) as log:
            result = await _call(_tm(post_side_effect=exc))
        assert result["_request_sent"] is False
        assert log.call_args.kwargs["error_class"] == "RequestNotSent"

    async def test_read_timeout_is_sent(self):
        with patch("app.debit.pyro_client.async_insert_debit_txn_log", new_callable=AsyncMock):
            result = await _call(_tm(post_side_effect=httpx.ReadTimeout("slow")))
        assert result["_request_sent"] is True and result["statusCode"] == -1

    async def test_response_carries_http_status(self):
        resp = MagicMock()
        resp.text = '{"statusCode":5000,"status":"FAILED","message":"Invalid token"}'
        resp.json.return_value = {"statusCode": 5000, "status": "FAILED", "message": "Invalid token"}
        resp.status_code = 401
        with patch("app.debit.pyro_client.async_insert_debit_txn_log", new_callable=AsyncMock):
            result = await _call(_tm(post_return=resp))
        assert result["_http_status"] == 401 and result["_request_sent"] is True

    async def test_token_failure_is_not_sent(self):
        tm = _tm()
        tm.get_access_token = AsyncMock(return_value=None)
        with patch("app.debit.pyro_client.async_insert_debit_txn_log", new_callable=AsyncMock):
            result = await _call(tm)
        assert result["_request_sent"] is False
