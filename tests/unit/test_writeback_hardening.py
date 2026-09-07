"""Unit tests for Phase 7 — Success/Failure Writeback Hardening.

Verifies:
1. mark_success across all adapters (FancySale, SimSwap, ESIM) does NOT swallow exceptions
   and raises WritebackError on rowcount == 0 or Oracle DB failure.
2. mark_reconciliation_required updates remarks in Oracle to flag records for reconciliation.
3. reset_stuck_processing across all adapters ignores records marked with RECONCILIATION_REQUIRED.
4. run_debit_batch in processor handles writeback failures durably:
   - logs critical operational alert;
   - inserts RECONCILIATION_REQUIRED record in PostgreSQL debit_txn_log with is_success='Y', is_perm_failure='Y';
   - calls mark_reconciliation_required on adapter;
   - does NOT convert record into retryable 'N' or call mark_failed;
   - returns accurate summary with reconciliation_required count.
"""

from unittest.mock import AsyncMock, MagicMock, patch
import pytest

from app.debit.processor import run_debit_batch
from app.debit.services.base import WritebackError
from app.debit.services.esim import EsimAdapter
from app.debit.services.fancysale import FancySaleAdapter
from app.debit.services.simswap import SimswapAdapter


# ══════════════════════════════════════════════════════════════════════════════
# FancySale Writeback Hardening Tests
# ══════════════════════════════════════════════════════════════════════════════

class TestFancySaleWritebackHardening:
    """Test writeback failure handling in FancySaleAdapter."""

    def test_mark_success_raises_writeback_error_when_rowcount_zero(self):
        """When rowcount is 0 (record not in 'P'), raise WritebackError."""
        adapter = FancySaleAdapter(token_manager=MagicMock(), enabled=True)
        record = {"refid": 12345}

        mock_cur = MagicMock()
        mock_cur.rowcount = 0
        mock_conn = MagicMock()
        mock_conn.cursor.return_value = mock_cur

        with patch("app.debit.services.fancysale.get_oracle_conn") as mock_get_conn:
            mock_get_conn.return_value.__enter__.return_value = mock_conn
            with pytest.raises(WritebackError, match="rowcount=0"):
                adapter.mark_success(record, pyro_txn_id="PYRO123", remarks="OK")

    def test_mark_success_raises_writeback_error_on_db_exception(self):
        """When Oracle raises an exception, do NOT swallow it — raise WritebackError."""
        adapter = FancySaleAdapter(token_manager=MagicMock(), enabled=True)
        record = {"refid": 12345}

        mock_cur = MagicMock()
        mock_cur.execute.side_effect = RuntimeError("Oracle connection dropped")
        mock_conn = MagicMock()
        mock_conn.cursor.return_value = mock_cur

        with patch("app.debit.services.fancysale.get_oracle_conn") as mock_get_conn:
            mock_get_conn.return_value.__enter__.return_value = mock_conn
            with pytest.raises(WritebackError, match="Oracle connection dropped"):
                adapter.mark_success(record, pyro_txn_id="PYRO123", remarks="OK")

    def test_mark_success_succeeds_when_rowcount_one(self):
        """When rowcount is 1, mark_success commits cleanly without error."""
        adapter = FancySaleAdapter(token_manager=MagicMock(), enabled=True)
        record = {"refid": 12345}

        mock_cur = MagicMock()
        mock_cur.rowcount = 1
        mock_conn = MagicMock()
        mock_conn.cursor.return_value = mock_cur

        with patch("app.debit.services.fancysale.get_oracle_conn") as mock_get_conn:
            mock_get_conn.return_value.__enter__.return_value = mock_conn
            adapter.mark_success(record, pyro_txn_id="PYRO123", remarks="OK")
            mock_conn.commit.assert_called_once()

    def test_mark_reconciliation_required_updates_pyro_remarks(self):
        """Emergency method marks PYRO_REMARKS with RECONCILIATION_REQUIRED."""
        adapter = FancySaleAdapter(token_manager=MagicMock(), enabled=True)
        record = {"refid": 12345}

        mock_cur = MagicMock()
        mock_conn = MagicMock()
        mock_conn.cursor.return_value = mock_cur

        with patch("app.debit.services.fancysale.get_oracle_conn") as mock_get_conn:
            mock_get_conn.return_value.__enter__.return_value = mock_conn
            adapter.mark_reconciliation_required(record, pyro_txn_id="TXN999", error_detail="DB timeout")

            mock_conn.commit.assert_called_once()
            sql_called = mock_cur.execute.call_args[0][0]
            params_called = mock_cur.execute.call_args[0][1]
            assert "PYRO_REMARKS" in sql_called
            assert "CAF_ENTRY_DONE = 'P'" in sql_called
            assert "RECONCILIATION_REQUIRED" in params_called["remarks"]
            assert params_called["refid"] == 12345

    def test_reset_stuck_processing_ignores_reconciliation_records(self):
        """reset_stuck_processing query must exclude rows with RECONCILIATION_REQUIRED."""
        adapter = FancySaleAdapter(token_manager=MagicMock(), enabled=True)

        mock_cur = MagicMock()
        mock_cur.rowcount = 0
        mock_conn = MagicMock()
        mock_conn.cursor.return_value = mock_cur

        with patch("app.debit.services.fancysale.get_oracle_conn") as mock_get_conn:
            mock_get_conn.return_value.__enter__.return_value = mock_conn
            adapter.reset_stuck_processing(stuck_minutes=60)

            sql_called = mock_cur.execute.call_args[0][0]
            assert "RECONCILIATION_REQUIRED%" in sql_called
            assert "NOT LIKE 'RECONCILIATION_REQUIRED%'" in sql_called


# ══════════════════════════════════════════════════════════════════════════════
# SimSwap Writeback Hardening Tests
# ══════════════════════════════════════════════════════════════════════════════

class TestSimSwapWritebackHardening:
    """Test writeback failure handling in SimSwapAdapter."""

    def test_mark_success_raises_writeback_error_when_primary_rowcount_zero(self):
        """When primary rowcount is 0, raise WritebackError."""
        adapter = SimswapAdapter(token_manager=MagicMock(), enabled=True)
        record = {"id": 54321, "gsmnumber": "9400012345"}

        mock_cur = MagicMock()
        mock_cur.rowcount = 0
        mock_conn = MagicMock()
        mock_conn.cursor.return_value = mock_cur

        with patch("app.debit.services.simswap.get_oracle_conn") as mock_get_conn:
            mock_get_conn.return_value.__enter__.return_value = mock_conn
            with pytest.raises(WritebackError, match="rowcount=0"):
                adapter.mark_success(record, pyro_txn_id="PYRO555", remarks="OK")

    def test_mark_success_raises_writeback_error_on_primary_db_exception(self):
        """When primary Oracle update raises DB exception, raise WritebackError."""
        adapter = SimswapAdapter(token_manager=MagicMock(), enabled=True)
        record = {"id": 54321, "gsmnumber": "9400012345"}

        mock_cur = MagicMock()
        mock_cur.execute.side_effect = RuntimeError("ORA-03113 end-of-file on communication channel")
        mock_conn = MagicMock()
        mock_conn.cursor.return_value = mock_cur

        with patch("app.debit.services.simswap.get_oracle_conn") as mock_get_conn:
            mock_get_conn.return_value.__enter__.return_value = mock_conn
            with pytest.raises(WritebackError, match="ORA-03113"):
                adapter.mark_success(record, pyro_txn_id="PYRO555", remarks="OK")

    def test_mark_reconciliation_required_updates_remarks(self):
        """Emergency method marks AMOUNT_DEDUCT_REMARKS with RECONCILIATION_REQUIRED."""
        adapter = SimswapAdapter(token_manager=MagicMock(), enabled=True)
        record = {"id": 54321}

        mock_cur = MagicMock()
        mock_conn = MagicMock()
        mock_conn.cursor.return_value = mock_cur

        with patch("app.debit.services.simswap.get_oracle_conn") as mock_get_conn:
            mock_get_conn.return_value.__enter__.return_value = mock_conn
            adapter.mark_reconciliation_required(record, pyro_txn_id="TXN777", error_detail="Writeback timeout")

            mock_conn.commit.assert_called_once()
            sql_called = mock_cur.execute.call_args[0][0]
            params_called = mock_cur.execute.call_args[0][1]
            assert "AMOUNT_DEDUCT_REMARKS" in sql_called
            assert "AMOUNT_DEDUCT_FLAG    = 'P'" in sql_called
            assert "RECONCILIATION_REQUIRED" in params_called["remarks"]
            assert params_called["id"] == 54321

    def test_reset_stuck_processing_ignores_reconciliation_records(self):
        """reset_stuck_processing query must exclude rows with RECONCILIATION_REQUIRED."""
        adapter = SimswapAdapter(token_manager=MagicMock(), enabled=True)

        mock_cur = MagicMock()
        mock_cur.rowcount = 0
        mock_conn = MagicMock()
        mock_conn.cursor.return_value = mock_cur

        with patch("app.debit.services.simswap.get_oracle_conn") as mock_get_conn:
            mock_get_conn.return_value.__enter__.return_value = mock_conn
            adapter.reset_stuck_processing(stuck_minutes=60)

            sql_called = mock_cur.execute.call_args[0][0]
            assert "RECONCILIATION_REQUIRED%" in sql_called
            assert "NOT LIKE 'RECONCILIATION_REQUIRED%'" in sql_called


# ══════════════════════════════════════════════════════════════════════════════
# ESIM Writeback Hardening Tests
# ══════════════════════════════════════════════════════════════════════════════

class TestESIMWritebackHardening:
    """Test writeback failure handling in ESIMAdapter."""

    def test_mark_success_raises_writeback_error_when_primary_rowcount_zero(self):
        """When primary rowcount is 0, raise WritebackError."""
        adapter = EsimAdapter(token_manager=MagicMock(), enabled=True)
        record = {"id": 88888, "gsmnumber": "9400088888"}

        mock_cur = MagicMock()
        mock_cur.rowcount = 0
        mock_conn = MagicMock()
        mock_conn.cursor.return_value = mock_cur

        with patch("app.debit.services.esim.get_oracle_conn") as mock_get_conn:
            mock_get_conn.return_value.__enter__.return_value = mock_conn
            with pytest.raises(WritebackError, match="rowcount=0"):
                adapter.mark_success(record, pyro_txn_id="PYRO888", remarks="OK")

    def test_mark_success_raises_writeback_error_on_primary_db_exception(self):
        """When primary Oracle update raises DB exception, raise WritebackError."""
        adapter = EsimAdapter(token_manager=MagicMock(), enabled=True)
        record = {"id": 88888, "gsmnumber": "9400088888"}

        mock_cur = MagicMock()
        mock_cur.execute.side_effect = RuntimeError("ORA-01033 ORACLE initialization or shutdown in progress")
        mock_conn = MagicMock()
        mock_conn.cursor.return_value = mock_cur

        with patch("app.debit.services.esim.get_oracle_conn") as mock_get_conn:
            mock_get_conn.return_value.__enter__.return_value = mock_conn
            with pytest.raises(WritebackError, match="ORA-01033"):
                adapter.mark_success(record, pyro_txn_id="PYRO888", remarks="OK")

    def test_mark_reconciliation_required_updates_remarks(self):
        """Emergency method marks AMOUNT_DEDUCT_REMARKS with RECONCILIATION_REQUIRED."""
        adapter = EsimAdapter(token_manager=MagicMock(), enabled=True)
        record = {"id": 88888}

        mock_cur = MagicMock()
        mock_conn = MagicMock()
        mock_conn.cursor.return_value = mock_cur

        with patch("app.debit.services.esim.get_oracle_conn") as mock_get_conn:
            mock_get_conn.return_value.__enter__.return_value = mock_conn
            adapter.mark_reconciliation_required(record, pyro_txn_id="TXN888", error_detail="Writeback deadlock")

            mock_conn.commit.assert_called_once()
            sql_called = mock_cur.execute.call_args[0][0]
            params_called = mock_cur.execute.call_args[0][1]
            assert "AMOUNT_DEDUCT_REMARKS" in sql_called
            assert "AMOUNT_DEDUCT_FLAG    = 'P'" in sql_called
            assert "RECONCILIATION_REQUIRED" in params_called["remarks"]
            assert params_called["id"] == 88888

    def test_reset_stuck_processing_ignores_reconciliation_records(self):
        """reset_stuck_processing query must exclude rows with RECONCILIATION_REQUIRED."""
        adapter = EsimAdapter(token_manager=MagicMock(), enabled=True)

        mock_cur = MagicMock()
        mock_cur.rowcount = 0
        mock_conn = MagicMock()
        mock_conn.cursor.return_value = mock_cur

        with patch("app.debit.services.esim.get_oracle_conn") as mock_get_conn:
            mock_get_conn.return_value.__enter__.return_value = mock_conn
            adapter.reset_stuck_processing(stuck_minutes=60)

            sql_called = mock_cur.execute.call_args[0][0]
            assert "RECONCILIATION_REQUIRED%" in sql_called
            assert "NOT LIKE 'RECONCILIATION_REQUIRED%'" in sql_called


# ══════════════════════════════════════════════════════════════════════════════
# Processor Writeback Hardening & Reconciliation Logging Tests
# ══════════════════════════════════════════════════════════════════════════════

class TestProcessorWritebackHardening:
    """Test run_debit_batch handling of writeback errors in app/debit/processor.py."""

    @pytest.mark.asyncio
    async def test_successful_debit_and_writeback(self):
        """Happy path: Pyro succeeds, mark_success succeeds."""
        adapter = MagicMock()
        adapter.service_type = "FANCYSALE"
        adapter.enabled = True
        adapter.batch_size = 10
        adapter.token_manager = MagicMock()

        record = {"refid": "FS-101"}
        adapter.fetch_and_claim.return_value = [record]
        adapter.get_record_ref.return_value = "FS-101"
        adapter.map_to_pyro_params.return_value = {
            "client_id": "CLI-1",
            "source_msisdn": "9400011111",
            "dest_msisdn": "9400022222",
            "amount": 100.0,
            "mpin": "12345",
            "remarks": "FANCYSALE",
        }

        pyro_resp = {
            "statusCode": 200,
            "status": "SUCCESS",
            "message": "Debit successful",
            "data": {"pyroId": "PYRO-OK-1", "balanceBefore": 500.0, "balanceAfter": 400.0},
        }

        with patch("app.debit.processor.wallet_adjustment", new_callable=AsyncMock) as mock_pyro:
            mock_pyro.return_value = pyro_resp

            summary = await run_debit_batch(adapter)

            assert summary["processed"] == 1
            assert summary["success"] == 1
            assert summary["failed"] == 0
            assert summary["reconciliation_required"] == 0
            adapter.mark_success.assert_called_once()
            adapter.mark_failed.assert_not_called()
            adapter.mark_reconciliation_required.assert_not_called()

    @pytest.mark.asyncio
    async def test_writeback_failure_triggers_reconciliation_logging(self):
        """Critical path: Pyro debit succeeds, but adapter.mark_success raises WritebackError.

        Must:
        1. Log critical operational alert.
        2. Insert RECONCILIATION_REQUIRED into Postgres debit_txn_log with is_success='Y', is_perm_failure='Y'.
        3. Call adapter.mark_reconciliation_required.
        4. NOT call adapter.mark_failed.
        5. Return summary with failed=1 and reconciliation_required=1.
        """
        adapter = MagicMock()
        adapter.service_type = "FANCYSALE"
        adapter.enabled = True
        adapter.batch_size = 10
        adapter.token_manager = MagicMock()

        record = {"refid": "FS-102"}
        adapter.fetch_and_claim.return_value = [record]
        adapter.get_record_ref.return_value = "FS-102"
        adapter.map_to_pyro_params.return_value = {
            "client_id": "CLI-2",
            "source_msisdn": "9400033333",
            "dest_msisdn": "9400044444",
            "amount": 250.0,
            "mpin": "12345",
            "remarks": "FANCYSALE",
        }

        # mark_success fails with WritebackError
        adapter.mark_success.side_effect = WritebackError("DB writeback failed for REFID=FS-102: timeout")

        pyro_resp = {
            "statusCode": 200,
            "status": "SUCCESS",
            "message": "Debit successful",
            "data": {"pyroId": "PYRO-SUCCESS-999", "balanceBefore": 1000.0, "balanceAfter": 750.0},
        }

        with patch("app.debit.processor.wallet_adjustment", new_callable=AsyncMock) as mock_pyro, \
             patch("app.debit.processor.async_insert_debit_txn_log", new_callable=AsyncMock) as mock_insert_log, \
             patch("app.debit.processor.logger.critical") as mock_critical:

            mock_pyro.return_value = pyro_resp

            summary = await run_debit_batch(adapter)

            # Summary verification
            assert summary["processed"] == 1
            assert summary["success"] == 0
            assert summary["failed"] == 1
            assert summary["reconciliation_required"] == 1

            # Critical log emitted
            mock_critical.assert_called()
            critical_msg = mock_critical.call_args_list[0][0][0]
            assert "CRITICAL" in critical_msg
            assert "reconciliation" in critical_msg.lower()

            # Durable log written to PostgreSQL
            mock_insert_log.assert_called_once()
            log_kwargs = mock_insert_log.call_args[1]
            assert log_kwargs["service_type"] == "FANCYSALE"
            assert log_kwargs["oracle_ref_id"] == "FS-102"
            assert log_kwargs["api_stage"] == "RECONCILIATION_REQUIRED"
            assert log_kwargs["pyro_txn_id"] == "PYRO-SUCCESS-999"
            assert log_kwargs["is_success"] == "Y"
            assert log_kwargs["is_perm_failure"] == "Y"
            assert log_kwargs["error_class"] == "WritebackError"
            assert "DB writeback failed" in log_kwargs["error_detail"]

            # Emergency reconciliation tag in Oracle called
            adapter.mark_reconciliation_required.assert_called_once_with(
                record, "PYRO-SUCCESS-999", "DB writeback failed for REFID=FS-102: timeout"
            )

            # Crucial: mark_failed was NOT called (prevents setting 'R' or resetting to 'N')
            adapter.mark_failed.assert_not_called()

    @pytest.mark.asyncio
    async def test_reconciliation_required_marks_when_mark_reconciliation_fails(self):
        """Even if mark_reconciliation_required fails (e.g. Oracle totally dead), processor does not crash."""
        adapter = MagicMock()
        adapter.service_type = "SIMSWAP"
        adapter.enabled = True
        adapter.batch_size = 10
        adapter.token_manager = MagicMock()

        record = {"id": 9999}
        adapter.fetch_and_claim.return_value = [record]
        adapter.get_record_ref.return_value = "9999"
        adapter.map_to_pyro_params.return_value = {
            "client_id": "CLI-3",
            "source_msisdn": "9400055555",
            "dest_msisdn": "9400066666",
            "amount": 50.0,
            "mpin": "12345",
            "remarks": "SIMSWAP",
        }

        adapter.mark_success.side_effect = WritebackError("Primary update failed")
        adapter.mark_reconciliation_required.side_effect = RuntimeError("Oracle down completely")

        pyro_resp = {
            "statusCode": 200,
            "status": "SUCCESS",
            "message": "Debit successful",
            "data": {"pyroId": "PYRO-SIM-123", "balanceBefore": 200.0, "balanceAfter": 150.0},
        }

        with patch("app.debit.processor.wallet_adjustment", new_callable=AsyncMock) as mock_pyro, \
             patch("app.debit.processor.async_insert_debit_txn_log", new_callable=AsyncMock) as mock_insert_log, \
             patch("app.debit.processor.logger.critical") as mock_critical:

            mock_pyro.return_value = pyro_resp

            summary = await run_debit_batch(adapter)

            assert summary["processed"] == 1
            assert summary["success"] == 0
            assert summary["failed"] == 1
            assert summary["reconciliation_required"] == 1

            # Two critical logs: one for writeback failure, one for mark_reconciliation_required failure
            assert mock_critical.call_count >= 2
            mock_insert_log.assert_called_once()
            assert mock_insert_log.call_args[1]["api_stage"] == "RECONCILIATION_REQUIRED"
