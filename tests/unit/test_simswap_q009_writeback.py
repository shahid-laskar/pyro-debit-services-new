"""Unit tests for Phase 10 — SimSwap Q009 Secondary Writeback.

Verifies:
1. Q009 query builder (build_simswap_bcd_update_query):
   - Constructs exact row targeting using confirmed BCD identity (GSMNUMBER, CAF_SERIAL_NO).
   - Injects AND CAF_SERIAL_NO = :caf_serial_no when confirmed present in source record.
   - Injects AND CIRCLE_CODE = :circle_code when present in source record, ensuring a SimSwap
     success does not modify unrelated BCD records for the same GSM.
   - Preserves status guard AND ACTIVATION_STATUS = 'IF'.
   - Gracefully handles absent or empty caf_serial_no without failing.
   - Rejects records missing or having empty gsmnumber with clear ValueError.
2. SimswapAdapter.mark_success Phase 2 integration:
   - Successfully executes secondary writeback to CAF_ADMIN.BCD with hardened predicates.
   - Handles rowcount == 1 (expected single-row update).
   - Handles rowcount > 1 (logs warning for operator visibility).
   - Handles rowcount == 0 (logs warning, does not fail already-committed primary).
   - Handles secondary Oracle DB exception non-fatally with manual remediation guidance.
"""

from unittest.mock import MagicMock, patch
import pytest

from app.debit.services.base import WritebackError
from app.debit.services.simswap import (
    SimswapAdapter,
    build_simswap_bcd_update_query,
)


# ══════════════════════════════════════════════════════════════════════════════
# 1. Q009 Query Builder Unit Tests
# ══════════════════════════════════════════════════════════════════════════════

class TestSimSwapQ009QueryBuilder:
    """Validate construction of secondary writeback query Q009 for CAF_ADMIN.BCD."""

    def test_q009_with_gsm_only(self):
        """When only GSM is provided, updates by GSM and status guard."""
        record = {"id": 1001, "gsmnumber": "9400012345"}
        sql, params = build_simswap_bcd_update_query(record)

        assert "UPDATE CAF_ADMIN.BCD" in sql
        assert "SET    ACTIVATION_STATUS = 'AI'" in sql
        assert "WHERE  GSMNUMBER         = :gsmnumber" in sql
        assert "AND  ACTIVATION_STATUS = 'IF'" in sql
        assert "CAF_SERIAL_NO" not in sql
        assert "CIRCLE_CODE" not in sql
        assert params == {"gsmnumber": "9400012345"}

    def test_q009_with_gsm_and_circle_code(self):
        """When circle_code is provided (standard Q006 output), CIRCLE_CODE guard is injected."""
        record = {"id": 1002, "gsmnumber": "9400012345", "circle_code": 55}
        sql, params = build_simswap_bcd_update_query(record)

        assert "UPDATE CAF_ADMIN.BCD" in sql
        assert "WHERE  GSMNUMBER         = :gsmnumber" in sql
        assert "AND  CIRCLE_CODE       = :circle_code" in sql
        assert "AND  ACTIVATION_STATUS = 'IF'" in sql
        assert "CAF_SERIAL_NO" not in sql
        assert params == {"gsmnumber": "9400012345", "circle_code": 55}

    def test_q009_with_gsm_and_caf_serial_no(self):
        """When caf_serial_no is present, CAF_SERIAL_NO exact PK predicate is injected."""
        record = {
            "id": 1003,
            "gsmnumber": "9400012345",
            "caf_serial_no": "CAF-99214-DEL",
        }
        sql, params = build_simswap_bcd_update_query(record)

        assert "WHERE  GSMNUMBER         = :gsmnumber" in sql
        assert "AND  CAF_SERIAL_NO     = :caf_serial_no" in sql
        assert "AND  ACTIVATION_STATUS = 'IF'" in sql
        assert "CIRCLE_CODE" not in sql
        assert params == {
            "gsmnumber": "9400012345",
            "caf_serial_no": "CAF-99214-DEL",
        }

    def test_q009_with_gsm_caf_and_circle_code(self):
        """Full composite identity: GSM, CAF_SERIAL_NO, and CIRCLE_CODE all bound."""
        record = {
            "id": 1004,
            "gsmnumber": "9400012345",
            "caf_serial_no": "CAF-99214-DEL",
            "circle_code": 2,
        }
        sql, params = build_simswap_bcd_update_query(record)

        assert "WHERE  GSMNUMBER         = :gsmnumber" in sql
        assert "AND  CAF_SERIAL_NO     = :caf_serial_no" in sql
        assert "AND  CIRCLE_CODE       = :circle_code" in sql
        assert "AND  ACTIVATION_STATUS = 'IF'" in sql
        assert params == {
            "gsmnumber": "9400012345",
            "caf_serial_no": "CAF-99214-DEL",
            "circle_code": 2,
        }

    def test_q009_with_alternate_key_caf_no(self):
        """Recognizes 'caf_no' as alternative key for CAF serial number."""
        record = {
            "id": 1005,
            "gsmnumber": "9400099999",
            "caf_no": "CAF-ALT-12345",
            "circle_code": 10,
        }
        sql, params = build_simswap_bcd_update_query(record)

        assert "AND  CAF_SERIAL_NO     = :caf_serial_no" in sql
        assert "AND  CIRCLE_CODE       = :circle_code" in sql
        assert params["caf_serial_no"] == "CAF-ALT-12345"
        assert params["circle_code"] == 10

    def test_q009_ignores_blank_or_whitespace_caf(self):
        """Whitespace-only caf_serial_no is ignored rather than bound as blank."""
        record = {
            "id": 1006,
            "gsmnumber": "9400012345",
            "caf_serial_no": "   ",
            "circle_code": 60,
        }
        sql, params = build_simswap_bcd_update_query(record)

        assert "CAF_SERIAL_NO" not in sql
        assert "AND  CIRCLE_CODE       = :circle_code" in sql
        assert params == {"gsmnumber": "9400012345", "circle_code": 60}

    def test_q009_raises_value_error_missing_gsmnumber(self):
        """Record without gsmnumber raises ValueError."""
        record = {"id": 1007, "circle_code": 55}
        with pytest.raises(ValueError, match="missing required 'gsmnumber'"):
            build_simswap_bcd_update_query(record)

    def test_q009_raises_value_error_empty_gsmnumber(self):
        """Record with empty gsmnumber raises ValueError."""
        record = {"id": 1008, "gsmnumber": "   ", "circle_code": 55}
        with pytest.raises(ValueError, match="empty 'gsmnumber'"):
            build_simswap_bcd_update_query(record)


# ══════════════════════════════════════════════════════════════════════════════
# 2. SimswapAdapter mark_success Phase 2 Integration Tests
# ══════════════════════════════════════════════════════════════════════════════

class TestSimSwapMarkSuccessPhase2Execution:
    """Validate mark_success Phase 2 secondary writeback execution behavior."""

    def test_mark_success_secondary_updates_with_circle_and_caf(self):
        """When both circle_code and caf_serial_no exist, secondary writeback binds both."""
        adapter = SimswapAdapter(token_manager=MagicMock(), enabled=True)
        record = {
            "id": 5001,
            "gsmnumber": "9400011111",
            "caf_serial_no": "CAF-5001-KRL",
            "circle_code": 50,
        }

        mock_cur = MagicMock()
        mock_cur.rowcount = 1
        mock_conn = MagicMock()
        mock_conn.cursor.return_value = mock_cur

        with patch("app.debit.services.simswap.get_oracle_conn") as mock_get_conn:
            mock_get_conn.return_value.__enter__.return_value = mock_conn

            adapter.mark_success(record, pyro_txn_id="PYRO-SUCCESS-01", remarks="OK")

            # Check that two queries were executed (Phase 1 primary, Phase 2 secondary)
            assert mock_cur.execute.call_count == 2

            primary_call = mock_cur.execute.call_args_list[0]
            secondary_call = mock_cur.execute.call_args_list[1]

            # Primary verification
            assert "UPDATE CAF_ADMIN.SIMSWAP_AMOUNT_DEDUCT_REQUESTS" in primary_call[0][0]
            assert primary_call[0][1]["id"] == 5001

            # Secondary verification
            assert "UPDATE CAF_ADMIN.BCD" in secondary_call[0][0]
            assert "CAF_SERIAL_NO     = :caf_serial_no" in secondary_call[0][0]
            assert "CIRCLE_CODE       = :circle_code" in secondary_call[0][0]
            assert secondary_call[0][1] == {
                "gsmnumber": "9400011111",
                "caf_serial_no": "CAF-5001-KRL",
                "circle_code": 50,
            }

            # Both phases committed
            assert mock_conn.commit.call_count == 2

    def test_mark_success_secondary_updates_with_circle_only(self):
        """When caf_serial_no is absent, secondary writeback safely binds circle_code."""
        adapter = SimswapAdapter(token_manager=MagicMock(), enabled=True)
        record = {
            "id": 5002,
            "gsmnumber": "9400022222",
            "circle_code": 51,
        }

        mock_cur = MagicMock()
        mock_cur.rowcount = 1
        mock_conn = MagicMock()
        mock_conn.cursor.return_value = mock_cur

        with patch("app.debit.services.simswap.get_oracle_conn") as mock_get_conn:
            mock_get_conn.return_value.__enter__.return_value = mock_conn

            adapter.mark_success(record, pyro_txn_id="PYRO-SUCCESS-02", remarks="OK")

            secondary_call = mock_cur.execute.call_args_list[1]
            assert "UPDATE CAF_ADMIN.BCD" in secondary_call[0][0]
            assert "CIRCLE_CODE       = :circle_code" in secondary_call[0][0]
            assert "CAF_SERIAL_NO" not in secondary_call[0][0]
            assert secondary_call[0][1] == {
                "gsmnumber": "9400022222",
                "circle_code": 51,
            }

    def test_mark_success_secondary_handles_rowcount_zero(self):
        """When secondary BCD rowcount is 0, warning is logged and no WritebackError is raised."""
        adapter = SimswapAdapter(token_manager=MagicMock(), enabled=True)
        record = {
            "id": 5003,
            "gsmnumber": "9400033333",
            "circle_code": 53,
        }

        # Primary has rowcount=1, secondary has rowcount=0
        mock_cur = MagicMock()
        mock_cur.rowcount = 1
        mock_conn = MagicMock()
        mock_conn.cursor.return_value = mock_cur

        with patch("app.debit.services.simswap.get_oracle_conn") as mock_get_conn:
            mock_get_conn.return_value.__enter__.return_value = mock_conn

            # Set rowcount to 0 for second call
            def side_effect_exec(sql, params):
                if "CAF_ADMIN.BCD" in sql:
                    mock_cur.rowcount = 0
                else:
                    mock_cur.rowcount = 1

            mock_cur.execute.side_effect = side_effect_exec

            # Must not raise WritebackError (primary was already committed)
            adapter.mark_success(record, pyro_txn_id="PYRO-SUCCESS-03", remarks="OK")
            assert mock_cur.execute.call_count == 2
            assert mock_conn.commit.call_count == 2

    def test_mark_success_secondary_handles_rowcount_multiple(self):
        """When secondary BCD rowcount > 1, warning is logged and transaction completes."""
        adapter = SimswapAdapter(token_manager=MagicMock(), enabled=True)
        record = {
            "id": 5004,
            "gsmnumber": "9400044444",
            "circle_code": 54,
        }

        mock_cur = MagicMock()
        mock_conn = MagicMock()
        mock_conn.cursor.return_value = mock_cur

        with patch("app.debit.services.simswap.get_oracle_conn") as mock_get_conn:
            mock_get_conn.return_value.__enter__.return_value = mock_conn

            def side_effect_exec(sql, params):
                if "CAF_ADMIN.BCD" in sql:
                    mock_cur.rowcount = 2
                else:
                    mock_cur.rowcount = 1

            mock_cur.execute.side_effect = side_effect_exec

            adapter.mark_success(record, pyro_txn_id="PYRO-SUCCESS-04", remarks="OK")
            assert mock_cur.execute.call_count == 2
            assert mock_conn.commit.call_count == 2

    def test_mark_success_secondary_db_exception_non_fatal(self):
        """When secondary update fails with DB exception, it does not raise WritebackError."""
        adapter = SimswapAdapter(token_manager=MagicMock(), enabled=True)
        record = {
            "id": 5005,
            "gsmnumber": "9400055555",
            "circle_code": 55,
        }

        mock_cur = MagicMock()
        mock_conn = MagicMock()
        mock_conn.cursor.return_value = mock_cur

        with patch("app.debit.services.simswap.get_oracle_conn") as mock_get_conn:
            mock_get_conn.return_value.__enter__.return_value = mock_conn

            def side_effect_exec(sql, params):
                if "CAF_ADMIN.BCD" in sql:
                    raise RuntimeError("ORA-00060: deadlock detected while waiting for resource")
                mock_cur.rowcount = 1

            mock_cur.execute.side_effect = side_effect_exec

            # Primary succeeds, secondary throws — must complete without raising WritebackError
            adapter.mark_success(record, pyro_txn_id="PYRO-SUCCESS-05", remarks="OK")
            assert mock_cur.execute.call_count == 2
