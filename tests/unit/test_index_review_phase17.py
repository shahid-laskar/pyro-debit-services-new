"""Unit tests for Phase 17: Database Performance & Index Review.

Validates that query predicate shapes and execution paths across FancySale, SimSwap,
and ESIM align with confirmed database metadata and index review decisions.
"""

import os
import re
from unittest.mock import MagicMock, patch

import pytest

from app.context import ExecutionContext, ExecutionSource
from app.debit.services.fancysale import (
    FancySaleAdapter,
    build_fancysale_candidate_query,
    build_fancysale_claim_query,
)
from app.debit.services.simswap import (
    SimswapAdapter,
    build_simswap_candidate_query,
    build_simswap_claim_query,
)
from app.debit.services.esim import (
    EsimAdapter,
    build_esim_candidate_query,
    build_esim_claim_query,
)


class TestFancySaleIndexAlignment:
    """Validate FancySale query shapes align with index review specifications."""

    def test_q001_filtered_shape_and_trans_date_order(self):
        """Q001 filtered must include CAF_ENTRY_DONE, dynamic CIRCLE_CODE, and ORDER BY TRANS_DATE ASC."""
        ctx = ExecutionContext.create(
            source=ExecutionSource.SCHEDULED,
            zones_str="NZ",
            service_type="fancysale",
        )
        sql, params = build_fancysale_candidate_query(batch_size=50, context=ctx)

        assert "CAF_ENTRY_DONE IN ('N', 'QM', 'QB')" in sql
        assert "CIRCLE_CODE IN (" in sql
        assert "ORDER BY TRANS_DATE ASC" in sql
        assert "ROWNUM <= :batch_size" in sql
        assert "batch_size" in params
        assert params["batch_size"] == 50
        assert len(params) == 1 + len(ctx.circle_codes)

    def test_q001_all_mode_shape_and_trans_date_order(self):
        """Q001 in ALL mode omits CIRCLE_CODE filter while maintaining FIFO ordering."""
        ctx = ExecutionContext.create(
            source=ExecutionSource.SCHEDULED,
            zones_str="ALL",
            service_type="fancysale",
        )
        sql, params = build_fancysale_candidate_query(batch_size=50, context=ctx)

        assert "CAF_ENTRY_DONE IN ('N', 'QM', 'QB')" in sql
        assert "CIRCLE_CODE IN" not in sql
        assert "ORDER BY TRANS_DATE ASC" in sql
        assert "ROWNUM <= :batch_size" in sql
        assert params == {"batch_size": 50}

    def test_q002_claim_uses_refid_and_circle_code_guard(self):
        """Q002 claim query must use REFID and CIRCLE_CODE scalar guard."""
        claim_sql = build_fancysale_claim_query()

        assert "CAF_ADMIN.VANITYSALE_FRANCH_DATA" in claim_sql
        assert "WHERE REFID = :refid" in claim_sql
        assert "CIRCLE_CODE = :circle_code" in claim_sql
        assert "CAF_ENTRY_DONE = 'P'" in claim_sql


class TestSimSwapIndexAlignment:
    """Validate SimSwap query shapes align with index review specifications."""

    def test_q006_filtered_shape_and_request_date_order(self):
        """Q006 filtered must include AMOUNT_DEDUCT_FLAG, MODULE_TYPE, CIRCLE_CODE, and REQUEST_DATE order."""
        ctx = ExecutionContext.create(
            source=ExecutionSource.SCHEDULED,
            zones_str="NZ",
            service_type="simswap",
        )
        sql, params = build_simswap_candidate_query(batch_size=50, context=ctx)

        assert "AMOUNT_DEDUCT_FLAG IN ('N', 'QM', 'QB')" in sql
        assert "MODULE_TYPE = 'SIMSWAP'" in sql
        assert "CIRCLE_CODE IN (" in sql
        assert "ORDER BY REQUEST_DATE ASC" in sql
        assert "ROWNUM <= :batch_size" in sql
        assert params["batch_size"] == 50

    def test_q007_claim_uses_id_primary_key_and_circle_code(self):
        """Q007 claim query must match on PK ID, MODULE_TYPE, and CIRCLE_CODE guard."""
        claim_sql = build_simswap_claim_query()

        assert "CAF_ADMIN.SIMSWAP_AMOUNT_DEDUCT_REQUESTS" in claim_sql
        assert "WHERE ID = :id" in claim_sql
        assert "CIRCLE_CODE = :circle_code" in claim_sql
        assert "MODULE_TYPE = 'SIMSWAP'" in claim_sql
        assert "AMOUNT_DEDUCT_FLAG = 'P'" in claim_sql

    def test_q009_secondary_bcd_uses_composite_pk(self):
        """Q009 secondary writeback utilizes confirmed composite PK (GSMNUMBER, CAF_SERIAL_NO)."""
        adapter = SimswapAdapter(MagicMock())
        record = {
            "id": 101,
            "gsmnumber": "9412345678",
            "caf_serial_no": "CAF999",
            "circle_code": 2,
        }

        with patch("app.debit.services.simswap.get_oracle_conn") as mock_conn:
            mock_cur = MagicMock()
            mock_conn.return_value.__enter__.return_value.cursor.return_value = mock_cur
            mock_cur.rowcount = 1

            adapter.mark_success(record, pyro_txn_id="PYRO123", remarks="SUCCESS")

            assert mock_cur.execute.call_count == 2
            bcd_call = mock_cur.execute.call_args_list[1]
            bcd_sql = bcd_call[0][0]
            bcd_params = bcd_call[0][1]

            assert "CAF_ADMIN.BCD" in bcd_sql
            assert "ACTIVATION_STATUS = 'AI'" in bcd_sql
            assert re.search(r"GSMNUMBER\s*=\s*:gsmnumber", bcd_sql)
            assert re.search(r"CAF_SERIAL_NO\s*=\s*:caf_serial_no", bcd_sql)
            assert bcd_params["gsmnumber"] == "9412345678"
            assert bcd_params["caf_serial_no"] == "CAF999"
            assert bcd_params["circle_code"] == 2


class TestESIMIndexAlignment:
    """Validate ESIM query shapes align with index review specifications."""

    def test_q012_filtered_shape_and_request_date_order(self):
        """Q012 filtered must include AMOUNT_DEDUCT_FLAG, MODULE_TYPE='ESIM', and CIRCLE_CODE."""
        ctx = ExecutionContext.create(
            source=ExecutionSource.SCHEDULED,
            zones_str="NZ",
            service_type="esim",
        )
        sql, params = build_esim_candidate_query(batch_size=50, context=ctx)

        assert "AMOUNT_DEDUCT_FLAG IN ('N', 'QM', 'QB')" in sql
        assert "MODULE_TYPE = 'ESIM'" in sql
        assert "CIRCLE_CODE IN (" in sql
        assert "ORDER BY REQUEST_DATE ASC" in sql
        assert "ROWNUM <= :batch_size" in sql

    def test_q013_claim_uses_id_primary_key_and_circle_code(self):
        """Q013 claim query must match on PK ID, MODULE_TYPE='ESIM', and CIRCLE_CODE guard."""
        claim_sql = build_esim_claim_query()

        assert "CAF_ADMIN.SIMSWAP_AMOUNT_DEDUCT_REQUESTS" in claim_sql
        assert "WHERE ID = :id" in claim_sql
        assert "CIRCLE_CODE = :circle_code" in claim_sql
        assert "MODULE_TYPE = 'ESIM'" in claim_sql
        assert "AMOUNT_DEDUCT_FLAG = 'P'" in claim_sql

    def test_q015_secondary_sim_swap_data_uses_gsmnumber_leading_edge(self):
        """Q015 secondary writeback utilizes GSMNUMBER leading edge of existing index."""
        adapter = EsimAdapter(MagicMock())
        record = {
            "id": 202,
            "gsmnumber": "9498765432",
            "circle_code": 60,
        }

        with patch("app.debit.services.esim.get_oracle_conn") as mock_conn:
            mock_cur = MagicMock()
            mock_conn.return_value.__enter__.return_value.cursor.return_value = mock_cur
            mock_cur.rowcount = 1

            adapter.mark_success(record, pyro_txn_id="PYRO456", remarks="SUCCESS")

            assert mock_cur.execute.call_count == 2
            sim_swap_call = mock_cur.execute.call_args_list[1]
            sim_swap_sql = sim_swap_call[0][0]
            sim_swap_params = sim_swap_call[0][1]

            assert "CAF_ADMIN.SIM_SWAP_DATA" in sim_swap_sql
            assert "ACTIVATION_STATUS = 'AI'" in sim_swap_sql
            assert re.search(r"GSMNUMBER\s*=\s*:gsmnumber", sim_swap_sql)
            assert sim_swap_params["gsmnumber"] == "9498765432"
            assert sim_swap_params["circle_code"] == 60


class TestPostgresAuditLogSchemaCompliance:
    """Validate PostgreSQL audit log schema and index definitions."""

    def test_debit_txn_log_sql_contains_operational_indexes(self):
        """sql/debit_txn_log.sql must contain necessary lookup indexes without unique constraint."""
        sql_path = os.path.join(os.path.dirname(__file__), "..", "..", "sql", "debit_txn_log.sql")
        sql_path = os.path.abspath(sql_path)
        assert os.path.exists(sql_path), f"File {sql_path} does not exist"

        with open(sql_path, "r", encoding="utf-8") as f:
            content = f.read()

        assert "CREATE TABLE IF NOT EXISTS public.debit_txn_log" in content
        assert "debit_txn_log_svc_ref" in content
        assert "(service_type, oracle_ref_id)" in content
        assert "debit_txn_log_created" in content
        assert "debit_txn_log_pyrotxn" in content
        # Ensure no unique constraint on oracle_ref_id
        assert "UNIQUE (service_type, oracle_ref_id)" not in content
