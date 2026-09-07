"""Unit tests for SimSwap Q006/Q007 SQL generation and claim behavior (Phase 5)."""

from unittest.mock import MagicMock, patch
import pytest

from app.context import ExecutionContext
from app.debit.services.simswap import (
    SimswapAdapter,
    build_simswap_candidate_query,
    build_simswap_claim_query,
)
from app.zones import NZ, WZ


class TestSimSwapQ006CandidateQuery:
    """Validate Q006 candidate query generation."""

    def test_simswap_q006_filtered(self):
        """Q006 must include CIRCLE_CODE IN (:c_0, ...) and preserve FIFO ordering when filtered."""
        ctx = ExecutionContext.create(source="SCHEDULED", zones_str="NZ")
        sql, params = build_simswap_candidate_query(batch_size=200, context=ctx)

        assert "AND CIRCLE_CODE IN (" in sql
        assert "ORDER BY REQUEST_DATE ASC" in sql
        assert "ROWNUM <= :batch_size" in sql
        assert "MODULE_TYPE = 'SIMSWAP'" in sql
        assert "AMOUNT_DEDUCT_FLAG IN ('N', 'QM', 'QB')" in sql

        # Check bound parameters
        assert params["batch_size"] == 200
        bound_circles = [v for k, v in params.items() if k.startswith("c_")]
        assert sorted(bound_circles) == sorted(NZ)
        assert len(bound_circles) == 9

    def test_simswap_q006_filtered_multi_zone(self):
        """Q006 with multi-zone selection binds all circles across zones."""
        ctx = ExecutionContext.create(source="MANUAL_API", zones_str="NZ,WZ")
        sql, params = build_simswap_candidate_query(batch_size=50, context=ctx)

        assert "AND CIRCLE_CODE IN (" in sql
        assert params["batch_size"] == 50
        bound_circles = [v for k, v in params.items() if k.startswith("c_")]
        expected = sorted(set(NZ + WZ))
        assert sorted(bound_circles) == expected
        assert len(bound_circles) == 14

    def test_simswap_q006_all(self):
        """Q006 must omit CIRCLE_CODE predicate when mode is ALL, preserving nationwide legacy behavior."""
        ctx = ExecutionContext.create(source="SCHEDULED", zones_str="ALL")
        sql, params = build_simswap_candidate_query(batch_size=200, context=ctx)

        assert "CIRCLE_CODE IN" not in sql
        assert "ORDER BY REQUEST_DATE ASC" in sql
        assert "ROWNUM <= :batch_size" in sql
        assert "MODULE_TYPE = 'SIMSWAP'" in sql
        assert params == {"batch_size": 200}

    def test_simswap_q006_none_context_omits_predicate(self):
        """Passing context=None omits circle filtering."""
        sql, params = build_simswap_candidate_query(batch_size=100, context=None)

        assert "CIRCLE_CODE IN" not in sql
        assert params == {"batch_size": 100}


class TestSimSwapQ007ClaimQuery:
    """Validate Q007 optimistic claim query structure and circle guard."""

    def test_simswap_q007_circle_guard(self):
        """Q007 must contain exact PK identity (ID), status guard, module guard, and CIRCLE_CODE guard."""
        claim_sql = build_simswap_claim_query()

        assert "UPDATE CAF_ADMIN.SIMSWAP_AMOUNT_DEDUCT_REQUESTS" in claim_sql
        assert "AMOUNT_DEDUCT_FLAG = 'P'" in claim_sql
        assert "AMOUNT_DEDUCT_DATE = SYSDATE" in claim_sql
        assert "AMOUNT_DEDUCT_REMARKS = 'Processing started'" in claim_sql
        assert "WHERE ID = :id" in claim_sql
        assert "AND CIRCLE_CODE = :circle_code" in claim_sql
        assert "AND MODULE_TYPE = 'SIMSWAP'" in claim_sql
        assert "AND AMOUNT_DEDUCT_FLAG IN ('N', 'QM', 'QB')" in claim_sql


class TestSimSwapFetchAndClaimLogic:
    """Validate fetch_and_claim execution logic with mocked Oracle connection."""

    def test_fetch_and_claim_adapter_disabled(self):
        mock_tm = MagicMock()
        adapter = SimswapAdapter(token_manager=mock_tm, enabled=False)
        claimed = adapter.fetch_and_claim(batch_size=10)
        assert claimed == []

    @patch("app.debit.services.simswap.get_oracle_conn")
    def test_fetch_and_claim_claims_eligible_rows_with_circle_guard(self, mock_get_conn):
        mock_conn = MagicMock()
        mock_cur = MagicMock()
        mock_get_conn.return_value.__enter__.return_value = mock_conn
        mock_conn.cursor.return_value = mock_cur

        # Candidate columns
        cols = ["id", "refid", "ctopupno", "gsmnumber", "simnumber", "amount",
                "plain_mpin", "mpin_length", "ss_request_id", "module_type",
                "request_date", "amount_deduct_flag", "circle_code", "dealercode",
                "swap_type", "source"]
        mock_cur.description = [(c, None, None, None, None, None, None) for c in cols]

        # Row 1: circle 2 (NZ) -> claimed (rowcount=1)
        # Row 2: circle 55 (NZ) -> lost race (rowcount=0)
        # Row 3: circle 1 (WZ) -> filtered out by defense-in-depth in NZ mode
        # Row 4: circle 56 (NZ) -> anomaly (rowcount=2)
        mock_cur.fetchall.return_value = [
            (101, "ref-1", 9876543210, 9999999999, 8991000000000000001, 50.0, "1234", 4, "ss-1", "SIMSWAP", "2026-09-07", "N", 2, "D1", "SWAP", "APP"),
            (102, "ref-2", 9876543211, 9999999998, 8991000000000000002, 50.0, "1234", 4, "ss-2", "SIMSWAP", "2026-09-07", "N", 55, "D2", "SWAP", "APP"),
            (103, "ref-3", 9876543212, 9999999997, 8991000000000000003, 50.0, "1234", 4, "ss-3", "SIMSWAP", "2026-09-07", "N", 1, "D3", "SWAP", "APP"),
            (104, "ref-4", 9876543213, 9999999996, 8991000000000000004, 50.0, "1234", 4, "ss-4", "SIMSWAP", "2026-09-07", "N", 56, "D4", "SWAP", "APP"),
        ]

        # Cursor rowcount returns for each claim attempt
        claim_rowcounts = [1, 0, 2]

        def exec_side_effect(sql, params=None, **kwargs):
            if "UPDATE" in sql and claim_rowcounts:
                mock_cur.rowcount = claim_rowcounts.pop(0)

        mock_cur.execute.side_effect = exec_side_effect

        mock_tm = MagicMock()
        adapter = SimswapAdapter(token_manager=mock_tm, enabled=True)
        ctx = ExecutionContext.create(source="SCHEDULED", zones_str="NZ")

        claimed = adapter.fetch_and_claim(batch_size=10, context=ctx)

        # Only row id=101 satisfies rowcount == 1 and circle allowed
        assert len(claimed) == 1
        assert claimed[0]["id"] == 101
        assert claimed[0]["circle_code"] == 2

        # Verify commit was called
        mock_conn.commit.assert_called_once()
