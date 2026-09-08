"""Unit tests for Phase 11 — ESIM Q015 Secondary Writeback Hardening.

Verifies:
1. Q015 query builder (build_esim_sim_swap_data_update_query):
   - Constructs exact row targeting on CAF_ADMIN.SIM_SWAP_DATA using primary key (ID)
     and composite lookup index beginning with (GSMNUMBER, NEW_SIM, CAF_SERIAL_NO, SWAP_DATE).
   - Injects AND ID = :sim_swap_id when sim_swap_id (or swap_id) is confirmed present.
   - Injects AND CAF_SERIAL_NO = :caf_serial_no when caf_serial_no (or caf_no) is present.
   - Injects AND NEW_SIM = :new_sim when new_sim is present.
   - Injects AND CIRCLE_CODE = :circle_code when present in source record, ensuring an ESIM
     success does not modify unrelated records for the same GSM in other circles or unreleased zones.
   - Preserves status guard AND ACTIVATION_STATUS = 'IF'.
   - Gracefully handles absent or empty optional parameters without failing.
   - Rejects records missing or having empty gsmnumber with clear ValueError.
2. EsimAdapter.mark_success Phase 2 integration:
   - Successfully executes secondary writeback to CAF_ADMIN.SIM_SWAP_DATA with hardened predicates.
   - Handles rowcount == 1 (expected single-row update).
   - Handles rowcount > 1 (logs warning for operator visibility).
   - Handles rowcount == 0 (logs warning, does not fail already-committed primary).
   - Handles secondary Oracle DB exception non-fatally with manual remediation guidance.
"""

from unittest.mock import MagicMock, patch
import pytest

from app.debit.services.base import WritebackError
from app.debit.services.esim import (
    EsimAdapter,
    build_esim_sim_swap_data_update_query,
)


# ══════════════════════════════════════════════════════════════════════════════
# 1. Q015 Query Builder Unit Tests
# ══════════════════════════════════════════════════════════════════════════════

class TestEsimQ015QueryBuilder:
    """Validate construction of secondary writeback query Q015 for CAF_ADMIN.SIM_SWAP_DATA."""

    def test_q015_with_gsm_only(self):
        """When only GSM is provided, updates by GSM and status guard."""
        record = {"id": 2001, "gsmnumber": "9400012345"}
        sql, params = build_esim_sim_swap_data_update_query(record)

        assert "UPDATE CAF_ADMIN.SIM_SWAP_DATA" in sql
        assert "SET    ACTIVATION_STATUS = 'AI'" in sql
        assert "WHERE  GSMNUMBER         = :gsmnumber" in sql
        assert "AND  ACTIVATION_STATUS = 'IF'" in sql
        assert "ID" not in sql
        assert "CAF_SERIAL_NO" not in sql
        assert "NEW_SIM" not in sql
        assert "CIRCLE_CODE" not in sql
        assert params == {"gsmnumber": "9400012345"}

    def test_q015_with_gsm_and_circle_code(self):
        """When circle_code is provided (standard Q012 output), CIRCLE_CODE guard is injected."""
        record = {"id": 2002, "gsmnumber": "9400012345", "circle_code": 55}
        sql, params = build_esim_sim_swap_data_update_query(record)

        assert "UPDATE CAF_ADMIN.SIM_SWAP_DATA" in sql
        assert "WHERE  GSMNUMBER         = :gsmnumber" in sql
        assert "AND  CIRCLE_CODE       = :circle_code" in sql
        assert "AND  ACTIVATION_STATUS = 'IF'" in sql
        assert "CAF_SERIAL_NO" not in sql
        assert params == {"gsmnumber": "9400012345", "circle_code": 55}

    def test_q015_with_gsm_and_sim_swap_id(self):
        """When sim_swap_id is present, ID exact PK predicate is injected."""
        record = {
            "id": 2003,
            "gsmnumber": "9400012345",
            "sim_swap_id": 998877,
        }
        sql, params = build_esim_sim_swap_data_update_query(record)

        assert "WHERE  GSMNUMBER         = :gsmnumber" in sql
        assert "AND  ID                = :sim_swap_id" in sql
        assert "AND  ACTIVATION_STATUS = 'IF'" in sql
        assert params == {
            "gsmnumber": "9400012345",
            "sim_swap_id": 998877,
        }

    def test_q015_with_alternate_key_swap_id(self):
        """Recognizes 'swap_id' as alternative key for SIM_SWAP_DATA ID."""
        record = {
            "id": 2004,
            "gsmnumber": "9400012345",
            "swap_id": "887766",
        }
        sql, params = build_esim_sim_swap_data_update_query(record)

        assert "AND  ID                = :sim_swap_id" in sql
        assert params["sim_swap_id"] == 887766

    def test_q015_with_gsm_and_caf_serial_no(self):
        """When caf_serial_no is present, CAF_SERIAL_NO exact predicate is injected."""
        record = {
            "id": 2005,
            "gsmnumber": "9400012345",
            "caf_serial_no": "CAF-ESIM-99214",
        }
        sql, params = build_esim_sim_swap_data_update_query(record)

        assert "WHERE  GSMNUMBER         = :gsmnumber" in sql
        assert "AND  CAF_SERIAL_NO     = :caf_serial_no" in sql
        assert "AND  ACTIVATION_STATUS = 'IF'" in sql
        assert params == {
            "gsmnumber": "9400012345",
            "caf_serial_no": "CAF-ESIM-99214",
        }

    def test_q015_with_alternate_key_caf_no(self):
        """Recognizes 'caf_no' as alternative key for CAF serial number."""
        record = {
            "id": 2006,
            "gsmnumber": "9400099999",
            "caf_no": "CAF-ALT-999",
            "circle_code": 10,
        }
        sql, params = build_esim_sim_swap_data_update_query(record)

        assert "AND  CAF_SERIAL_NO     = :caf_serial_no" in sql
        assert "AND  CIRCLE_CODE       = :circle_code" in sql
        assert params["caf_serial_no"] == "CAF-ALT-999"
        assert params["circle_code"] == 10

    def test_q015_with_new_sim(self):
        """When new_sim is provided, NEW_SIM predicate is injected."""
        record = {
            "id": 2007,
            "gsmnumber": "9400012345",
            "new_sim": "8991000999999999999",
        }
        sql, params = build_esim_sim_swap_data_update_query(record)

        assert "WHERE  GSMNUMBER         = :gsmnumber" in sql
        assert "AND  NEW_SIM           = :new_sim" in sql
        assert "AND  ACTIVATION_STATUS = 'IF'" in sql
        assert params == {
            "gsmnumber": "9400012345",
            "new_sim": "8991000999999999999",
        }

    def test_q015_with_all_fields(self):
        """Full composite identity: GSM, sim_swap_id, CAF_SERIAL_NO, NEW_SIM, and CIRCLE_CODE all bound."""
        record = {
            "id": 2008,
            "gsmnumber": "9400012345",
            "sim_swap_id": 554433,
            "caf_serial_no": "CAF-ESIM-FULL",
            "new_sim": "8991000111111111111",
            "circle_code": 2,
        }
        sql, params = build_esim_sim_swap_data_update_query(record)

        assert "WHERE  GSMNUMBER         = :gsmnumber" in sql
        assert "AND  ID                = :sim_swap_id" in sql
        assert "AND  CAF_SERIAL_NO     = :caf_serial_no" in sql
        assert "AND  NEW_SIM           = :new_sim" in sql
        assert "AND  CIRCLE_CODE       = :circle_code" in sql
        assert "AND  ACTIVATION_STATUS = 'IF'" in sql
        assert params == {
            "gsmnumber": "9400012345",
            "sim_swap_id": 554433,
            "caf_serial_no": "CAF-ESIM-FULL",
            "new_sim": "8991000111111111111",
            "circle_code": 2,
        }

    def test_q015_ignores_blank_or_whitespace_optional_fields(self):
        """Whitespace-only optional fields are ignored rather than bound as blanks."""
        record = {
            "id": 2009,
            "gsmnumber": "9400012345",
            "sim_swap_id": "   ",
            "caf_serial_no": "   ",
            "new_sim": "   ",
            "circle_code": 60,
        }
        sql, params = build_esim_sim_swap_data_update_query(record)

        assert "ID" not in sql
        assert "CAF_SERIAL_NO" not in sql
        assert "NEW_SIM" not in sql
        assert "AND  CIRCLE_CODE       = :circle_code" in sql
        assert params == {"gsmnumber": "9400012345", "circle_code": 60}

    def test_q015_raises_value_error_missing_gsmnumber(self):
        """Record without gsmnumber raises ValueError."""
        record = {"id": 2010, "circle_code": 55}
        with pytest.raises(ValueError, match="missing required 'gsmnumber'"):
            build_esim_sim_swap_data_update_query(record)

    def test_q015_raises_value_error_empty_gsmnumber(self):
        """Record with empty gsmnumber raises ValueError."""
        record = {"id": 2011, "gsmnumber": "   ", "circle_code": 55}
        with pytest.raises(ValueError, match="empty 'gsmnumber'"):
            build_esim_sim_swap_data_update_query(record)


# ══════════════════════════════════════════════════════════════════════════════
# 2. EsimAdapter mark_success Phase 2 Integration Tests
# ══════════════════════════════════════════════════════════════════════════════

class TestEsimMarkSuccessPhase2Execution:
    """Validate mark_success Phase 2 secondary writeback execution behavior."""

    def test_mark_success_secondary_updates_with_circle_and_caf(self):
        """When both circle_code and caf_serial_no exist, secondary writeback binds both."""
        adapter = EsimAdapter(token_manager=MagicMock(), enabled=True)
        record = {
            "id": 501,
            "gsmnumber": "9400011111",
            "circle_code": 2,
            "caf_serial_no": "CAF-ESIM-202",
        }

        mock_cur = MagicMock()
        mock_cur.rowcount = 1
        mock_conn = MagicMock()
        mock_conn.cursor.return_value = mock_cur

        with patch("app.debit.services.esim.get_oracle_conn") as mock_get_conn:
            mock_get_conn.return_value.__enter__.return_value = mock_conn
            adapter.mark_success(record, pyro_txn_id="PYRO-ESIM-1", remarks="Success")

            # Must have executed 2 queries: primary on SIMSWAP_AMOUNT_DEDUCT_REQUESTS, secondary on SIM_SWAP_DATA
            assert mock_cur.execute.call_count == 2

            primary_call = mock_cur.execute.call_args_list[0]
            secondary_call = mock_cur.execute.call_args_list[1]

            # Primary verification
            assert "SIMSWAP_AMOUNT_DEDUCT_REQUESTS" in primary_call[0][0]
            assert primary_call[0][1]["id"] == 501

            # Secondary verification
            secondary_sql = secondary_call[0][0]
            secondary_params = secondary_call[0][1]
            assert "CAF_ADMIN.SIM_SWAP_DATA" in secondary_sql
            assert "AND  CAF_SERIAL_NO     = :caf_serial_no" in secondary_sql
            assert "AND  CIRCLE_CODE       = :circle_code" in secondary_sql
            assert secondary_params["gsmnumber"] == "9400011111"
            assert secondary_params["caf_serial_no"] == "CAF-ESIM-202"
            assert secondary_params["circle_code"] == 2

            assert mock_conn.commit.call_count == 2

    def test_mark_success_secondary_updates_with_circle_and_swap_id(self):
        """When circle_code and sim_swap_id exist, secondary writeback binds both."""
        adapter = EsimAdapter(token_manager=MagicMock(), enabled=True)
        record = {
            "id": 502,
            "gsmnumber": "9400022222",
            "circle_code": 55,
            "sim_swap_id": 123456,
        }

        mock_cur = MagicMock()
        mock_cur.rowcount = 1
        mock_conn = MagicMock()
        mock_conn.cursor.return_value = mock_cur

        with patch("app.debit.services.esim.get_oracle_conn") as mock_get_conn:
            mock_get_conn.return_value.__enter__.return_value = mock_conn
            adapter.mark_success(record, pyro_txn_id="PYRO-ESIM-2", remarks="Success")

            assert mock_cur.execute.call_count == 2
            secondary_call = mock_cur.execute.call_args_list[1]
            secondary_sql = secondary_call[0][0]
            secondary_params = secondary_call[0][1]

            assert "CAF_ADMIN.SIM_SWAP_DATA" in secondary_sql
            assert "AND  ID                = :sim_swap_id" in secondary_sql
            assert "AND  CIRCLE_CODE       = :circle_code" in secondary_sql
            assert secondary_params["gsmnumber"] == "9400022222"
            assert secondary_params["sim_swap_id"] == 123456
            assert secondary_params["circle_code"] == 55

    def test_mark_success_secondary_updates_with_circle_only(self):
        """When CAF and swap ID are absent, secondary writeback binds CIRCLE_CODE guard."""
        adapter = EsimAdapter(token_manager=MagicMock(), enabled=True)
        record = {
            "id": 503,
            "gsmnumber": "9400033333",
            "circle_code": 61,
        }

        mock_cur = MagicMock()
        mock_cur.rowcount = 1
        mock_conn = MagicMock()
        mock_conn.cursor.return_value = mock_cur

        with patch("app.debit.services.esim.get_oracle_conn") as mock_get_conn:
            mock_get_conn.return_value.__enter__.return_value = mock_conn
            adapter.mark_success(record, pyro_txn_id="PYRO-ESIM-3", remarks="Success")

            assert mock_cur.execute.call_count == 2
            secondary_call = mock_cur.execute.call_args_list[1]
            secondary_sql = secondary_call[0][0]
            secondary_params = secondary_call[0][1]

            assert "CAF_ADMIN.SIM_SWAP_DATA" in secondary_sql
            assert "AND  CIRCLE_CODE       = :circle_code" in secondary_sql
            assert "CAF_SERIAL_NO" not in secondary_sql
            assert "NEW_SIM" not in secondary_sql
            assert secondary_params == {
                "gsmnumber": "9400033333",
                "circle_code": 61,
            }

    def test_mark_success_secondary_rowcount_zero_logs_warning(self):
        """Secondary rowcount == 0 logs a warning but does not raise WritebackError (primary already committed)."""
        adapter = EsimAdapter(token_manager=MagicMock(), enabled=True)
        record = {
            "id": 504,
            "gsmnumber": "9400044444",
            "circle_code": 2,
        }

        mock_cur = MagicMock()
        # Call 1 (primary) -> rowcount = 1; Call 2 (secondary) -> rowcount = 0
        mock_cur.rowcount = 1
        mock_conn = MagicMock()
        mock_conn.cursor.return_value = mock_cur

        def exec_side_effect(sql, params=None):
            if "SIM_SWAP_DATA" in sql:
                mock_cur.rowcount = 0
            else:
                mock_cur.rowcount = 1

        mock_cur.execute.side_effect = exec_side_effect

        with patch("app.debit.services.esim.get_oracle_conn") as mock_get_conn, \
             patch("app.debit.services.esim.logger.warning") as mock_warn:
            mock_get_conn.return_value.__enter__.return_value = mock_conn
            # Does not raise WritebackError
            adapter.mark_success(record, pyro_txn_id="PYRO-ESIM-4", remarks="Success")

            # Warning must be logged mentioning SIM_SWAP_DATA and ID=504
            mock_warn.assert_called_once()
            warn_format_str = mock_warn.call_args[0][0]
            assert "SIM_SWAP_DATA" in warn_format_str
            assert "no row updated" in warn_format_str

    def test_mark_success_secondary_rowcount_gt_one_logs_warning(self):
        """Secondary rowcount > 1 logs a warning indicating multiple rows updated."""
        adapter = EsimAdapter(token_manager=MagicMock(), enabled=True)
        record = {
            "id": 505,
            "gsmnumber": "9400055555",
            "circle_code": 2,
        }

        mock_cur = MagicMock()
        mock_conn = MagicMock()
        mock_conn.cursor.return_value = mock_cur

        def exec_side_effect(sql, params=None):
            if "SIM_SWAP_DATA" in sql:
                mock_cur.rowcount = 2
            else:
                mock_cur.rowcount = 1

        mock_cur.execute.side_effect = exec_side_effect

        with patch("app.debit.services.esim.get_oracle_conn") as mock_get_conn, \
             patch("app.debit.services.esim.logger.warning") as mock_warn:
            mock_get_conn.return_value.__enter__.return_value = mock_conn
            adapter.mark_success(record, pyro_txn_id="PYRO-ESIM-5", remarks="Success")

            mock_warn.assert_called_once()
            warn_format_str = mock_warn.call_args[0][0]
            assert "expected at most 1 row" in warn_format_str

    def test_mark_success_secondary_db_failure_non_fatal(self):
        """Secondary DB exception is caught non-fatally and logged with manual remediation instructions."""
        adapter = EsimAdapter(token_manager=MagicMock(), enabled=True)
        record = {
            "id": 506,
            "gsmnumber": "9400066666",
            "circle_code": 59,
        }

        mock_cur = MagicMock()
        mock_conn = MagicMock()
        mock_conn.cursor.return_value = mock_cur

        def exec_side_effect(sql, params=None):
            if "SIM_SWAP_DATA" in sql:
                raise RuntimeError("ORA-00060 deadlock detected while waiting for resource")
            mock_cur.rowcount = 1

        mock_cur.execute.side_effect = exec_side_effect

        with patch("app.debit.services.esim.get_oracle_conn") as mock_get_conn, \
             patch("app.debit.services.esim.logger.error") as mock_error:
            mock_get_conn.return_value.__enter__.return_value = mock_conn

            # Does NOT raise WritebackError because primary is already safely committed
            adapter.mark_success(record, pyro_txn_id="PYRO-ESIM-6", remarks="Success")

            mock_error.assert_called_once()
            error_fmt = mock_error.call_args[0][0]
            assert "MANUAL FIX REQUIRED" in error_fmt
            assert "CAF_ADMIN.SIM_SWAP_DATA" in error_fmt
            assert "Primary record ID=%s is already Y" in error_fmt
