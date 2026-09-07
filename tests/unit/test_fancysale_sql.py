"""Unit tests for FancySale Q001/Q002 SQL generation and claim behavior (Phase 4)."""

from unittest.mock import MagicMock, patch
import pytest

from app.context import ExecutionContext
from app.debit.services.fancysale import (
    FancySaleAdapter,
    build_fancysale_candidate_query,
    build_fancysale_claim_query,
)
from app.zones import NZ, WZ


class TestFancySaleQ001CandidateQuery:
    """Validate Q001 candidate query generation."""

    def test_fancysale_q001_filtered(self):
        """Q001 must include CIRCLE_CODE IN (:c_0, ...) and preserve FIFO ordering when filtered."""
        ctx = ExecutionContext.create(source="SCHEDULED", zones_str="NZ")
        sql, params = build_fancysale_candidate_query(batch_size=200, context=ctx)

        assert "AND CIRCLE_CODE IN (" in sql
        assert "ORDER BY TRANS_DATE ASC" in sql
        assert "ROWNUM <= :batch_size" in sql
        assert "WHERE CAF_ENTRY_DONE IN ('N', 'QM', 'QB')" in sql

        # Check bound parameters
        assert params["batch_size"] == 200
        bound_circles = [v for k, v in params.items() if k.startswith("c_")]
        assert sorted(bound_circles) == sorted(NZ)
        assert len(bound_circles) == 9

    def test_fancysale_q001_filtered_multi_zone(self):
        """Q001 with multi-zone selection binds all circles across zones."""
        ctx = ExecutionContext.create(source="MANUAL_API", zones_str="NZ,WZ")
        sql, params = build_fancysale_candidate_query(batch_size=50, context=ctx)

        assert "AND CIRCLE_CODE IN (" in sql
        assert params["batch_size"] == 50
        bound_circles = [v for k, v in params.items() if k.startswith("c_")]
        expected = sorted(set(NZ + WZ))
        assert sorted(bound_circles) == expected
        assert len(bound_circles) == 14

    def test_fancysale_q001_all(self):
        """Q001 must omit CIRCLE_CODE predicate when mode is ALL, preserving nationwide legacy behavior."""
        ctx = ExecutionContext.create(source="SCHEDULED", zones_str="ALL")
        sql, params = build_fancysale_candidate_query(batch_size=200, context=ctx)

        assert "CIRCLE_CODE IN" not in sql
        assert "ORDER BY TRANS_DATE ASC" in sql
        assert "ROWNUM <= :batch_size" in sql
        assert params == {"batch_size": 200}

    def test_fancysale_q001_none_context_omits_predicate(self):
        """Passing context=None omits circle filtering."""
        sql, params = build_fancysale_candidate_query(batch_size=100, context=None)

        assert "CIRCLE_CODE IN" not in sql
        assert params == {"batch_size": 100}


class TestFancySaleQ002ClaimQuery:
    """Validate Q002 optimistic claim query structure and circle guard."""

    def test_fancysale_q002_circle_guard(self):
        """Q002 must contain exact row identity (REFID), status guard, and CIRCLE_CODE guard."""
        claim_sql = build_fancysale_claim_query()

        assert "UPDATE CAF_ADMIN.VANITYSALE_FRANCH_DATA" in claim_sql
        assert "CAF_ENTRY_DONE = 'P'" in claim_sql
        assert "CAF_ENTRY_DATE = SYSDATE" in claim_sql
        assert "PYRO_REMARKS = 'Processing started'" in claim_sql
        assert "WHERE REFID = :refid" in claim_sql
        assert "AND CAF_ENTRY_DONE IN ('N','QM','QB')" in claim_sql
        assert "AND CIRCLE_CODE = :circle_code" in claim_sql


class TestFancySaleFetchAndClaimLogic:
    """Validate fetch_and_claim execution logic with mocked Oracle connection."""

    def test_fetch_and_claim_adapter_disabled(self):
        mock_tm = MagicMock()
        adapter = FancySaleAdapter(token_manager=mock_tm, enabled=False)
        claimed = adapter.fetch_and_claim(batch_size=10)
        assert claimed == []

    @patch("app.debit.services.fancysale.get_oracle_conn")
    def test_fetch_and_claim_claims_eligible_rows_with_circle_guard(self, mock_get_conn):
        mock_conn = MagicMock()
        mock_cur = MagicMock()
        mock_get_conn.return_value.__enter__.return_value = mock_conn
        mock_conn.cursor.return_value = mock_cur

        # Candidate columns
        cols = ["refid", "ctopupno", "fancy_no", "amount", "plain_mpin",
                "mpin_length", "ss_request_id", "csccode", "circle_code",
                "trans_date", "module_type", "caf_entry_done"]
        mock_cur.description = [(c, None, None, None, None, None, None) for c in cols]

        # Row 1: circle 2 (NZ) -> claimed (rowcount=1)
        # Row 2: circle 55 (NZ) -> lost race (rowcount=0)
        # Row 3: circle 1 (WZ) -> filtered out by defense-in-depth in NZ mode
        # Row 4: circle 56 (NZ) -> anomaly (rowcount=2)
        mock_cur.fetchall.return_value = [
            ("ref-1", 9876543210, 9999999999, 100.0, "1234", 4, "ss-1", "csc", 2, "2026-09-07", "FANCYSALE", "N"),
            ("ref-2", 9876543211, 9999999998, 100.0, "1234", 4, "ss-2", "csc", 55, "2026-09-07", "FANCYSALE", "N"),
            ("ref-3", 9876543212, 9999999997, 100.0, "1234", 4, "ss-3", "csc", 1, "2026-09-07", "FANCYSALE", "N"),
            ("ref-4", 9876543213, 9999999996, 100.0, "1234", 4, "ss-4", "csc", 56, "2026-09-07", "FANCYSALE", "N"),
        ]

        # Cursor rowcount returns for each claim attempt
        mock_cur.rowcount = 1
        claim_rowcounts = [1, 0, 2]

        def exec_side_effect(sql, params=None, **kwargs):
            if "UPDATE" in sql and claim_rowcounts:
                mock_cur.rowcount = claim_rowcounts.pop(0)

        mock_cur.execute.side_effect = exec_side_effect

        mock_tm = MagicMock()
        adapter = FancySaleAdapter(token_manager=mock_tm, enabled=True)
        ctx = ExecutionContext.create(source="SCHEDULED", zones_str="NZ")

        claimed = adapter.fetch_and_claim(batch_size=10, context=ctx)

        # Only ref-1 satisfies rowcount == 1 and circle allowed
        assert len(claimed) == 1
        assert claimed[0]["refid"] == "ref-1"
        assert claimed[0]["circle_code"] == 2

        # Verify commit was called
        mock_conn.commit.assert_called_once()
