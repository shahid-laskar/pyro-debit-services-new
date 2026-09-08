"""Unit tests for Phase 12 — Shared SimSwap/ESIM Table Safety.

Verifies:
1. Because both SimSwap and ESIM share table CAF_ADMIN.SIMSWAP_AMOUNT_DEDUCT_REQUESTS,
   all operations across both services explicitly preserve MODULE_TYPE:
   - Candidate selection
   - Row claim
   - Primary success writeback
   - Emergency reconciliation writeback
   - Failure writeback
   - Stuck processing cleanup
2. SimSwap operations strictly bind/filter MODULE_TYPE = 'SIMSWAP'.
3. ESIM operations strictly bind/filter MODULE_TYPE = 'ESIM'.
4. Cross-service isolation: SimSwap queries never target ESIM rows, and ESIM queries
   never target SimSwap rows, even if IDs collide or are shared.
"""

from unittest.mock import MagicMock, patch
import pytest

from app.context import ExecutionContext
from app.debit.services.esim import (
    ESIM_CLAIM_SQL,
    ESIM_FAILURE_SQL,
    ESIM_PRIMARY_SUCCESS_SQL,
    ESIM_RECONCILIATION_SQL,
    EsimAdapter,
    build_esim_candidate_query,
    build_esim_claim_query,
    build_esim_cleanup_query,
)
from app.debit.services.simswap import (
    SIMSWAP_CLAIM_SQL,
    SIMSWAP_FAILURE_SQL,
    SIMSWAP_PRIMARY_SUCCESS_SQL,
    SIMSWAP_RECONCILIATION_SQL,
    SimswapAdapter,
    build_simswap_candidate_query,
    build_simswap_claim_query,
    build_simswap_cleanup_query,
)


# ══════════════════════════════════════════════════════════════════════════════
# 1. SimSwap Shared-Table Safety Tests
# ══════════════════════════════════════════════════════════════════════════════

class TestSimSwapSharedTableSafety:
    """Ensure all SimSwap operations against SIMSWAP_AMOUNT_DEDUCT_REQUESTS guard MODULE_TYPE='SIMSWAP'."""

    def test_simswap_candidate_query_has_module_type(self):
        sql, _ = build_simswap_candidate_query(batch_size=50)
        assert "FROM CAF_ADMIN.SIMSWAP_AMOUNT_DEDUCT_REQUESTS" in sql
        assert "MODULE_TYPE = 'SIMSWAP'" in sql
        assert "ESIM" not in sql

    def test_simswap_claim_query_has_module_type(self):
        sql = build_simswap_claim_query()
        assert "UPDATE CAF_ADMIN.SIMSWAP_AMOUNT_DEDUCT_REQUESTS" in sql
        assert "MODULE_TYPE = 'SIMSWAP'" in sql
        assert "ESIM" not in sql

    def test_simswap_claim_constant_has_module_type(self):
        assert "MODULE_TYPE = 'SIMSWAP'" in SIMSWAP_CLAIM_SQL

    def test_simswap_primary_success_sql_has_module_type(self):
        assert "UPDATE CAF_ADMIN.SIMSWAP_AMOUNT_DEDUCT_REQUESTS" in SIMSWAP_PRIMARY_SUCCESS_SQL
        assert "MODULE_TYPE           = 'SIMSWAP'" in SIMSWAP_PRIMARY_SUCCESS_SQL or \
               "MODULE_TYPE = 'SIMSWAP'" in SIMSWAP_PRIMARY_SUCCESS_SQL
        assert "ESIM" not in SIMSWAP_PRIMARY_SUCCESS_SQL

    def test_simswap_reconciliation_sql_has_module_type(self):
        assert "UPDATE CAF_ADMIN.SIMSWAP_AMOUNT_DEDUCT_REQUESTS" in SIMSWAP_RECONCILIATION_SQL
        assert "MODULE_TYPE           = 'SIMSWAP'" in SIMSWAP_RECONCILIATION_SQL or \
               "MODULE_TYPE = 'SIMSWAP'" in SIMSWAP_RECONCILIATION_SQL
        assert "ESIM" not in SIMSWAP_RECONCILIATION_SQL

    def test_simswap_failure_sql_has_module_type(self):
        assert "UPDATE CAF_ADMIN.SIMSWAP_AMOUNT_DEDUCT_REQUESTS" in SIMSWAP_FAILURE_SQL
        assert "MODULE_TYPE           = 'SIMSWAP'" in SIMSWAP_FAILURE_SQL or \
               "MODULE_TYPE = 'SIMSWAP'" in SIMSWAP_FAILURE_SQL
        assert "ESIM" not in SIMSWAP_FAILURE_SQL

    def test_simswap_cleanup_query_has_module_type(self):
        sql, _ = build_simswap_cleanup_query(stuck_minutes=15)
        assert "UPDATE CAF_ADMIN.SIMSWAP_AMOUNT_DEDUCT_REQUESTS" in sql
        assert "MODULE_TYPE           = 'SIMSWAP'" in sql or "MODULE_TYPE = 'SIMSWAP'" in sql
        assert "ESIM" not in sql

    def test_simswap_mark_success_executes_module_guard(self):
        """SimswapAdapter.mark_success executes primary query containing MODULE_TYPE = 'SIMSWAP'."""
        adapter = SimswapAdapter(token_manager=MagicMock(), enabled=True)
        record = {"id": 1101, "gsmnumber": "9400011111", "circle_code": 2}

        mock_cur = MagicMock()
        mock_cur.rowcount = 1
        mock_conn = MagicMock()
        mock_conn.cursor.return_value = mock_cur

        with patch("app.debit.services.simswap.get_oracle_conn") as mock_get_conn:
            mock_get_conn.return_value.__enter__.return_value = mock_conn
            adapter.mark_success(record, pyro_txn_id="TXN1", remarks="OK")

            primary_sql = mock_cur.execute.call_args_list[0][0][0]
            assert "SIMSWAP_AMOUNT_DEDUCT_REQUESTS" in primary_sql
            assert "MODULE_TYPE" in primary_sql
            assert "'SIMSWAP'" in primary_sql

    def test_simswap_mark_reconciliation_executes_module_guard(self):
        """SimswapAdapter.mark_reconciliation_required executes query containing MODULE_TYPE = 'SIMSWAP'."""
        adapter = SimswapAdapter(token_manager=MagicMock(), enabled=True)
        record = {"id": 1102}

        mock_cur = MagicMock()
        mock_conn = MagicMock()
        mock_conn.cursor.return_value = mock_cur

        with patch("app.debit.services.simswap.get_oracle_conn") as mock_get_conn:
            mock_get_conn.return_value.__enter__.return_value = mock_conn
            adapter.mark_reconciliation_required(record, pyro_txn_id="TXN2", error_detail="Fail")

            sql = mock_cur.execute.call_args[0][0]
            assert "SIMSWAP_AMOUNT_DEDUCT_REQUESTS" in sql
            assert "MODULE_TYPE" in sql
            assert "'SIMSWAP'" in sql

    def test_simswap_mark_failed_executes_module_guard(self):
        """SimswapAdapter.mark_failed executes query containing MODULE_TYPE = 'SIMSWAP'."""
        adapter = SimswapAdapter(token_manager=MagicMock(), enabled=True)
        record = {"id": 1103}

        mock_cur = MagicMock()
        mock_conn = MagicMock()
        mock_conn.cursor.return_value = mock_cur

        with patch("app.debit.services.simswap.get_oracle_conn") as mock_get_conn:
            mock_get_conn.return_value.__enter__.return_value = mock_conn
            adapter.mark_failed(record, remarks="Rejected")

            sql = mock_cur.execute.call_args[0][0]
            assert "SIMSWAP_AMOUNT_DEDUCT_REQUESTS" in sql
            assert "MODULE_TYPE" in sql
            assert "'SIMSWAP'" in sql


# ══════════════════════════════════════════════════════════════════════════════
# 2. ESIM Shared-Table Safety Tests
# ══════════════════════════════════════════════════════════════════════════════

class TestEsimSharedTableSafety:
    """Ensure all ESIM operations against SIMSWAP_AMOUNT_DEDUCT_REQUESTS guard MODULE_TYPE='ESIM'."""

    def test_esim_candidate_query_has_module_type(self):
        sql, _ = build_esim_candidate_query(batch_size=50)
        assert "FROM CAF_ADMIN.SIMSWAP_AMOUNT_DEDUCT_REQUESTS" in sql
        assert "MODULE_TYPE = 'ESIM'" in sql
        assert "'SIMSWAP'" not in sql

    def test_esim_claim_query_has_module_type(self):
        sql = build_esim_claim_query()
        assert "UPDATE CAF_ADMIN.SIMSWAP_AMOUNT_DEDUCT_REQUESTS" in sql
        assert "MODULE_TYPE = 'ESIM'" in sql
        assert "'SIMSWAP'" not in sql

    def test_esim_claim_constant_has_module_type(self):
        assert "MODULE_TYPE = 'ESIM'" in ESIM_CLAIM_SQL

    def test_esim_primary_success_sql_has_module_type(self):
        assert "UPDATE CAF_ADMIN.SIMSWAP_AMOUNT_DEDUCT_REQUESTS" in ESIM_PRIMARY_SUCCESS_SQL
        assert "MODULE_TYPE           = 'ESIM'" in ESIM_PRIMARY_SUCCESS_SQL or \
               "MODULE_TYPE = 'ESIM'" in ESIM_PRIMARY_SUCCESS_SQL
        assert "'SIMSWAP'" not in ESIM_PRIMARY_SUCCESS_SQL

    def test_esim_reconciliation_sql_has_module_type(self):
        assert "UPDATE CAF_ADMIN.SIMSWAP_AMOUNT_DEDUCT_REQUESTS" in ESIM_RECONCILIATION_SQL
        assert "MODULE_TYPE           = 'ESIM'" in ESIM_RECONCILIATION_SQL or \
               "MODULE_TYPE = 'ESIM'" in ESIM_RECONCILIATION_SQL
        assert "'SIMSWAP'" not in ESIM_RECONCILIATION_SQL

    def test_esim_failure_sql_has_module_type(self):
        assert "UPDATE CAF_ADMIN.SIMSWAP_AMOUNT_DEDUCT_REQUESTS" in ESIM_FAILURE_SQL
        assert "MODULE_TYPE           = 'ESIM'" in ESIM_FAILURE_SQL or \
               "MODULE_TYPE = 'ESIM'" in ESIM_FAILURE_SQL
        assert "'SIMSWAP'" not in ESIM_FAILURE_SQL

    def test_esim_cleanup_query_has_module_type(self):
        sql, _ = build_esim_cleanup_query(stuck_minutes=15)
        assert "UPDATE CAF_ADMIN.SIMSWAP_AMOUNT_DEDUCT_REQUESTS" in sql
        assert "MODULE_TYPE           = 'ESIM'" in sql or "MODULE_TYPE = 'ESIM'" in sql
        assert "'SIMSWAP'" not in sql

    def test_esim_mark_success_executes_module_guard(self):
        """EsimAdapter.mark_success executes primary query containing MODULE_TYPE = 'ESIM'."""
        adapter = EsimAdapter(token_manager=MagicMock(), enabled=True)
        record = {"id": 2201, "gsmnumber": "9400022222", "circle_code": 55}

        mock_cur = MagicMock()
        mock_cur.rowcount = 1
        mock_conn = MagicMock()
        mock_conn.cursor.return_value = mock_cur

        with patch("app.debit.services.esim.get_oracle_conn") as mock_get_conn:
            mock_get_conn.return_value.__enter__.return_value = mock_conn
            adapter.mark_success(record, pyro_txn_id="TXN3", remarks="OK")

            primary_sql = mock_cur.execute.call_args_list[0][0][0]
            assert "SIMSWAP_AMOUNT_DEDUCT_REQUESTS" in primary_sql
            assert "MODULE_TYPE" in primary_sql
            assert "'ESIM'" in primary_sql

    def test_esim_mark_reconciliation_executes_module_guard(self):
        """EsimAdapter.mark_reconciliation_required executes query containing MODULE_TYPE = 'ESIM'."""
        adapter = EsimAdapter(token_manager=MagicMock(), enabled=True)
        record = {"id": 2202}

        mock_cur = MagicMock()
        mock_conn = MagicMock()
        mock_conn.cursor.return_value = mock_cur

        with patch("app.debit.services.esim.get_oracle_conn") as mock_get_conn:
            mock_get_conn.return_value.__enter__.return_value = mock_conn
            adapter.mark_reconciliation_required(record, pyro_txn_id="TXN4", error_detail="Fail")

            sql = mock_cur.execute.call_args[0][0]
            assert "SIMSWAP_AMOUNT_DEDUCT_REQUESTS" in sql
            assert "MODULE_TYPE" in sql
            assert "'ESIM'" in sql

    def test_esim_mark_failed_executes_module_guard(self):
        """EsimAdapter.mark_failed executes query containing MODULE_TYPE = 'ESIM'."""
        adapter = EsimAdapter(token_manager=MagicMock(), enabled=True)
        record = {"id": 2203}

        mock_cur = MagicMock()
        mock_conn = MagicMock()
        mock_conn.cursor.return_value = mock_cur

        with patch("app.debit.services.esim.get_oracle_conn") as mock_get_conn:
            mock_get_conn.return_value.__enter__.return_value = mock_conn
            adapter.mark_failed(record, remarks="Rejected")

            sql = mock_cur.execute.call_args[0][0]
            assert "SIMSWAP_AMOUNT_DEDUCT_REQUESTS" in sql
            assert "MODULE_TYPE" in sql
            assert "'ESIM'" in sql


# ══════════════════════════════════════════════════════════════════════════════
# 3. Cross-Service Isolation Tests
# ══════════════════════════════════════════════════════════════════════════════

class TestCrossServiceIsolation:
    """Verify strict mutual exclusivity between SimSwap and ESIM operations on the shared table."""

    def test_simswap_and_esim_query_sets_mutually_exclusive(self):
        simswap_queries = [
            SIMSWAP_CLAIM_SQL,
            SIMSWAP_PRIMARY_SUCCESS_SQL,
            SIMSWAP_RECONCILIATION_SQL,
            SIMSWAP_FAILURE_SQL,
            build_simswap_candidate_query(10)[0],
            build_simswap_cleanup_query(10)[0],
        ]
        esim_queries = [
            ESIM_CLAIM_SQL,
            ESIM_PRIMARY_SUCCESS_SQL,
            ESIM_RECONCILIATION_SQL,
            ESIM_FAILURE_SQL,
            build_esim_candidate_query(10)[0],
            build_esim_cleanup_query(10)[0],
        ]

        for sq in simswap_queries:
            assert "MODULE_TYPE = 'SIMSWAP'" in sq or "MODULE_TYPE           = 'SIMSWAP'" in sq
            assert "MODULE_TYPE = 'ESIM'" not in sq
            assert "MODULE_TYPE           = 'ESIM'" not in sq

        for eq in esim_queries:
            assert "MODULE_TYPE = 'ESIM'" in eq or "MODULE_TYPE           = 'ESIM'" in eq
            assert "MODULE_TYPE = 'SIMSWAP'" not in eq
            assert "MODULE_TYPE           = 'SIMSWAP'" not in eq
