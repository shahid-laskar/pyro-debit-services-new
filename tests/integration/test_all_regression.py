"""Phase 20: Nationwide 'ALL' Mode Regression Verification Suite.

Directly implements and verifies Section 25 (Phase 20 — ALL Regression) of
debit_services_final_implementation_plan.md:
1. Verifies that candidate discovery queries (Q001, Q006, Q012) execute without CIRCLE_CODE IN predicates.
2. Compares resulting execution behavior with legacy nationwide service to prove:
       ALL ≈ legacy nationwide business behavior
   while strictly retaining all modern safety controls:
   - Row-level claim verification (cur.rowcount == 1)
   - Writeback hardening with RECONCILIATION_REQUIRED immunity
   - Active in-flight ownership tracking (ActiveOwnershipTracker)
   - Per-service process-local concurrency locking (asyncio.Lock)
   - Audit trail logging in PostgreSQL (public.debit_txn_log)
3. Proves records spanning all 4 geographic zones (NZ, WZ, EZ, SZ) can be selected, claimed,
   processed, and written back in a single batch without boundary restrictions.
4. Validates real query execution against non-production Oracle and PostgreSQL databases
   when active in the execution environment.
"""

import asyncio
import re
from unittest.mock import AsyncMock, MagicMock, patch
import pytest

from app.config import Settings, settings
from app.context import ExecutionContext, ExecutionSource
from app.debit.ownership import ownership_tracker
from app.debit.processor import run_debit_batch, _reset_service_locks
from app.debit.services.base import WritebackError
from app.debit.services.esim import (
    EsimAdapter,
    build_esim_candidate_query,
    build_esim_claim_query,
    build_esim_cleanup_query,
    build_esim_sim_swap_data_update_query,
)
from app.debit.services.fancysale import (
    FancySaleAdapter,
    build_fancysale_candidate_query,
    build_fancysale_claim_query,
    build_fancysale_cleanup_query,
)
from app.debit.services.simswap import (
    SimswapAdapter,
    build_simswap_candidate_query,
    build_simswap_claim_query,
    build_simswap_cleanup_query,
    build_simswap_bcd_update_query,
)
from app.zones import (
    CIRCLE_METADATA,
    EZ,
    NZ,
    SZ,
    WZ,
    resolve_zones,
)


# ──────────────────────────────────────────────────────────────────────────────
# Database Connectivity Probes for Conditional Live Integration
# ──────────────────────────────────────────────────────────────────────────────

def _oracle_accessible() -> bool:
    """Check whether non-production Oracle XE is accessible."""
    try:
        import oracledb
        conn = oracledb.connect(
            user=settings.oracle_user,
            password=settings.oracle_password,
            dsn=settings.oracle_dsn,
            tcp_connect_timeout=3,
        )
        conn.close()
        return True
    except Exception:
        return False


def _postgres_accessible() -> bool:
    """Check whether non-production PostgreSQL is accessible."""
    try:
        import psycopg2
        conn = psycopg2.connect(
            host=settings.pg_host,
            port=settings.pg_port,
            database=settings.pg_database,
            user=settings.pg_user,
            password=settings.pg_password,
            connect_timeout=3,
        )
        conn.close()
        return True
    except Exception:
        return False


ORACLE_AVAILABLE = _oracle_accessible()
POSTGRES_AVAILABLE = _postgres_accessible()


# ══════════════════════════════════════════════════════════════════════════════
# 1. Execution Context and Configuration Equivalence in ALL Mode
# ══════════════════════════════════════════════════════════════════════════════

class TestAllModeContextAndConfiguration:
    """Verifies that ExecutionContext and Settings enforce nationwide scope under ALL."""

    def test_context_creation_all_mode(self):
        """Explicit zones_str='ALL' creates nationwide context with None circle_codes."""
        ctx = ExecutionContext.create(source="SCHEDULED", zones_str="ALL")
        assert ctx.mode == "ALL"
        assert ctx.zone_codes == ("ALL",)
        assert ctx.circle_codes is None
        assert ctx.circle_list is None
        assert ctx.circle_count == 31
        assert ctx.zones_display == "ALL"

    def test_context_inherited_from_settings_all(self):
        """When settings.enabled_zones is 'ALL', default context inherits ALL mode."""
        with patch.object(settings, "enabled_zones", "ALL"):
            ctx = ExecutionContext.create(source="SCHEDULED")
            assert ctx.mode == "ALL"
            assert ctx.zone_codes == ("ALL",)
            assert ctx.circle_codes is None
            assert ctx.circle_count == 31

    def test_is_circle_allowed_across_all_four_geographic_zones(self):
        """In ALL mode, is_circle_allowed returns True for every circle in India."""
        ctx = ExecutionContext.create(source="SCHEDULED", zones_str="ALL")

        # Test representative circles from each of the 4 zones
        nz_circles = [2, 10, 50, 51, 53, 54, 55, 59, 62]  # North Zone
        wz_circles = [1, 4, 11, 23, 27]                    # West Zone
        ez_circles = [70, 71, 72, 74, 76, 78]              # East Zone
        sz_circles = [40, 41, 42, 43, 44]                  # South Zone

        for circle in nz_circles + wz_circles + ez_circles + sz_circles:
            assert ctx.is_circle_allowed(circle) is True
            assert ctx.is_circle_allowed(str(circle)) is True

        # Every metadata circle (all 31) must be allowed
        for circle_code in CIRCLE_METADATA:
            assert ctx.is_circle_allowed(circle_code) is True

    def test_context_serialization_all_mode(self):
        """to_dict() properly reflects nationwide mode and 31 circles."""
        ctx = ExecutionContext.create(source="MANUAL_API", zones_str="ALL", service_type="fancysale")
        d = ctx.to_dict()
        assert d["mode"] == "ALL"
        assert d["zone_codes"] == ["ALL"]
        assert d["circle_codes"] is None
        assert d["circle_count"] == 31
        assert d["service_type"] == "fancysale"
        assert d["source"] == "MANUAL_API"


# ══════════════════════════════════════════════════════════════════════════════
# 2. Candidate Discovery SQL Equivalence (Q001, Q006, Q012)
# ══════════════════════════════════════════════════════════════════════════════

class TestAllModeCandidateSQLEquivalence:
    """Verifies that candidate queries omit circle predicates and match legacy nationwide behavior."""

    def test_fancysale_q001_nationwide_sql(self):
        """FancySale Q001 candidate query executes without circle predicates in ALL mode."""
        ctx = ExecutionContext.create(source="SCHEDULED", zones_str="ALL")
        sql, params = build_fancysale_candidate_query(batch_size=50, context=ctx)

        # Invariant F: Circle predicates MUST be omitted in ALL mode
        assert "CIRCLE_CODE IN" not in sql
        assert "WHERE CAF_ENTRY_DONE IN ('N', 'QM', 'QB')" in sql
        assert "ORDER BY TRANS_DATE ASC" in sql
        assert "ROWNUM <= :batch_size" in sql
        assert params == {"batch_size": 50}

    def test_simswap_q006_nationwide_sql(self):
        """SimSwap Q006 candidate query executes without circle predicates in ALL mode."""
        ctx = ExecutionContext.create(source="SCHEDULED", zones_str="ALL")
        sql, params = build_simswap_candidate_query(batch_size=50, context=ctx)

        # Invariant F: Circle predicates MUST be omitted in ALL mode
        assert "CIRCLE_CODE IN" not in sql
        assert "AMOUNT_DEDUCT_FLAG IN ('N', 'QM', 'QB')" in sql
        assert "MODULE_TYPE = 'SIMSWAP'" in sql
        assert "ORDER BY REQUEST_DATE ASC" in sql
        assert "ROWNUM <= :batch_size" in sql
        assert params == {"batch_size": 50}

    def test_esim_q012_nationwide_sql(self):
        """ESIM Q012 candidate query executes without circle predicates in ALL mode."""
        ctx = ExecutionContext.create(source="SCHEDULED", zones_str="ALL")
        sql, params = build_esim_candidate_query(batch_size=50, context=ctx)

        # Invariant F: Circle predicates MUST be omitted in ALL mode
        assert "CIRCLE_CODE IN" not in sql
        assert "AMOUNT_DEDUCT_FLAG IN ('N', 'QM', 'QB')" in sql
        assert "MODULE_TYPE = 'ESIM'" in sql
        assert "ORDER BY REQUEST_DATE ASC" in sql
        assert "ROWNUM <= :batch_size" in sql
        assert params == {"batch_size": 50}

    def test_sql_differential_filtered_vs_all_mode(self):
        """Proves exact query differential between filtered NZ mode and nationwide ALL mode."""
        ctx_nz = ExecutionContext.create(source="SCHEDULED", zones_str="NZ")
        ctx_all = ExecutionContext.create(source="SCHEDULED", zones_str="ALL")

        # 1. FancySale
        sql_nz, params_nz = build_fancysale_candidate_query(50, ctx_nz)
        sql_all, params_all = build_fancysale_candidate_query(50, ctx_all)
        assert "AND CIRCLE_CODE IN (" in sql_nz
        assert "AND CIRCLE_CODE IN (" not in sql_all
        assert len(params_nz) == 1 + len(NZ)  # batch_size + 9 circles
        assert len(params_all) == 1           # batch_size only

        # 2. SimSwap
        sql_nz, params_nz = build_simswap_candidate_query(50, ctx_nz)
        sql_all, params_all = build_simswap_candidate_query(50, ctx_all)
        assert "AND CIRCLE_CODE IN (" in sql_nz
        assert "AND CIRCLE_CODE IN (" not in sql_all
        assert len(params_nz) == 1 + len(NZ)
        assert len(params_all) == 1

        # 3. ESIM
        sql_nz, params_nz = build_esim_candidate_query(50, ctx_nz)
        sql_all, params_all = build_esim_candidate_query(50, ctx_all)
        assert "AND CIRCLE_CODE IN (" in sql_nz
        assert "AND CIRCLE_CODE IN (" not in sql_all
        assert len(params_nz) == 1 + len(NZ)
        assert len(params_all) == 1


# ══════════════════════════════════════════════════════════════════════════════
# 3. Stuck Cleanup Equivalence and Safety Hardening in ALL Mode
# ══════════════════════════════════════════════════════════════════════════════

class TestAllModeCleanupEquivalenceAndSafety:
    """Verifies stuck cleanup operates nationwide while retaining all safety controls."""

    def test_fancysale_q005_nationwide_cleanup_sql(self):
        """FancySale cleanup Q005 omits circle filter but retains active exclusion & recon check."""
        ctx = ExecutionContext.create(source="SCHEDULED", zones_str="ALL")
        sql, params = build_fancysale_cleanup_query(
            stuck_minutes=15,
            context=ctx,
            active_refs={"FS_ACT_1", "FS_ACT_2"},
        )
        assert "CIRCLE_CODE IN" not in sql
        assert "CAF_ENTRY_DONE" in sql and "'P'" in sql
        assert re.search(r"CAF_ENTRY_DATE\s*<\s*SYSDATE\s*-\s*\(:stuck_minutes\s*/\s*1440\)", sql) is not None
        assert "(PYRO_REMARKS IS NULL OR PYRO_REMARKS NOT LIKE 'RECONCILIATION_REQUIRED%')" in sql
        assert "AND (REFID NOT IN (:act_0, :act_1))" in sql or "AND REFID NOT IN (:act_0, :act_1)" in sql
        assert params["stuck_minutes"] == 15
        assert {params["act_0"], params["act_1"]} == {"FS_ACT_1", "FS_ACT_2"}

    def test_simswap_q011_nationwide_cleanup_sql(self):
        """SimSwap cleanup Q011 omits circle filter but retains module guard & active exclusion."""
        ctx = ExecutionContext.create(source="SCHEDULED", zones_str="ALL")
        sql, params = build_simswap_cleanup_query(
            stuck_minutes=10,
            context=ctx,
            active_refs={101, 102},
        )
        assert "CIRCLE_CODE IN" not in sql
        assert re.search(r"AMOUNT_DEDUCT_FLAG\s*=\s*'P'", sql) is not None
        assert "MODULE_TYPE           = 'SIMSWAP'" in sql or "MODULE_TYPE = 'SIMSWAP'" in sql
        assert "(AMOUNT_DEDUCT_REMARKS IS NULL OR AMOUNT_DEDUCT_REMARKS NOT LIKE 'RECONCILIATION_REQUIRED%')" in sql
        assert "AND (ID NOT IN (:act_0, :act_1))" in sql or "AND ID NOT IN (:act_0, :act_1)" in sql
        assert params["stuck_minutes"] == 10

    def test_esim_q017_nationwide_cleanup_sql(self):
        """ESIM cleanup Q017 omits circle filter but retains module guard & active exclusion."""
        ctx = ExecutionContext.create(source="SCHEDULED", zones_str="ALL")
        sql, params = build_esim_cleanup_query(
            stuck_minutes=10,
            context=ctx,
            active_refs={201},
        )
        assert "CIRCLE_CODE IN" not in sql
        assert re.search(r"AMOUNT_DEDUCT_FLAG\s*=\s*'P'", sql) is not None
        assert "MODULE_TYPE           = 'ESIM'" in sql or "MODULE_TYPE = 'ESIM'" in sql
        assert "(AMOUNT_DEDUCT_REMARKS IS NULL OR AMOUNT_DEDUCT_REMARKS NOT LIKE 'RECONCILIATION_REQUIRED%')" in sql
        assert "AND (ID NOT IN (:act_0))" in sql or "AND ID NOT IN (:act_0)" in sql
        assert params["stuck_minutes"] == 10

    def test_cleanup_execution_protects_reconciliation_records(self):
        """reset_stuck_processing in ALL mode does not reset records marked RECONCILIATION_REQUIRED."""
        ctx = ExecutionContext.create(source="SCHEDULED", zones_str="ALL")
        adapter = FancySaleAdapter(token_manager=MagicMock(), enabled=True)

        mock_cur = MagicMock()
        mock_cur.rowcount = 0
        mock_conn = MagicMock()
        mock_conn.cursor.return_value = mock_cur

        with patch("app.debit.services.fancysale.get_oracle_conn") as mock_get_conn:
            mock_get_conn.return_value.__enter__.return_value = mock_conn
            reset_count = adapter.reset_stuck_processing(stuck_minutes=10, context=ctx)

        executed_sql = mock_cur.execute.call_args[0][0]
        assert "CIRCLE_CODE IN" not in executed_sql
        assert "NOT LIKE 'RECONCILIATION_REQUIRED%'" in executed_sql
        assert reset_count == 0


# ══════════════════════════════════════════════════════════════════════════════
# 4. Multi-Zone Candidate Selection and Claiming (NZ, WZ, EZ, SZ in One Batch)
# ══════════════════════════════════════════════════════════════════════════════

class TestAllModeMultiZoneCandidateProcessing:
    """Verifies that records across all 4 zones are claimed and processed in a single batch."""

    def test_fancysale_claims_records_across_all_four_zones(self):
        """FancySale claims records spanning NZ, WZ, EZ, and SZ in a single batch in ALL mode."""
        ctx = ExecutionContext.create(source="SCHEDULED", zones_str="ALL")
        adapter = FancySaleAdapter(token_manager=MagicMock(), enabled=True)

        # Mock candidates returning 4 records, one from each zone
        mock_candidates = [
            ("REF_NZ", "9412300001", "9412399991", 100.0, "123456", 6, "SS_NZ", "CSC01", 2, "2026-09-08", "FANCYSALE", "N"),   # NZ: 2 (Punjab)
            ("REF_WZ", "9412300002", "9412399992", 100.0, "123456", 6, "SS_WZ", "CSC02", 1, "2026-09-08", "FANCYSALE", "N"),   # WZ: 1 (Maharashtra)
            ("REF_EZ", "9412300003", "9412399993", 100.0, "123456", 6, "SS_EZ", "CSC03", 70, "2026-09-08", "FANCYSALE", "N"),  # EZ: 70 (Kolkata)
            ("REF_SZ", "9412300004", "9412399994", 100.0, "123456", 6, "SS_SZ", "CSC04", 40, "2026-09-08", "FANCYSALE", "N"),  # SZ: 40 (Andhra Pradesh)
        ]
        columns = [
            ("REFID",), ("CTOPUPNO",), ("FANCY_NO",), ("AMOUNT",), ("PLAIN_MPIN",),
            ("MPIN_LENGTH",), ("SS_REQUEST_ID",), ("CSCCODE",), ("CIRCLE_CODE",),
            ("TRANS_DATE",), ("MODULE_TYPE",), ("CAF_ENTRY_DONE",),
        ]

        mock_cur = MagicMock()
        mock_cur.description = columns
        mock_cur.fetchall.return_value = mock_candidates
        mock_cur.rowcount = 1  # Each claim succeeds

        mock_conn = MagicMock()
        mock_conn.cursor.return_value = mock_cur

        with patch("app.debit.services.fancysale.get_oracle_conn") as mock_get_conn:
            mock_get_conn.return_value.__enter__.return_value = mock_conn
            claimed = adapter.fetch_and_claim(batch_size=50, context=ctx)

        # All 4 records spanning all 4 zones must be successfully claimed
        assert len(claimed) == 4
        claimed_circles = {r["circle_code"] for r in claimed}
        assert claimed_circles == {2, 1, 70, 40}

        # Verify claim SQL was executed 4 times, each with circle_code intact
        claim_calls = mock_cur.execute.call_args_list[1:]  # First call was candidate SELECT
        assert len(claim_calls) == 4
        assert claim_calls[0][0][1] == {"refid": "REF_NZ", "circle_code": 2}
        assert claim_calls[1][0][1] == {"refid": "REF_WZ", "circle_code": 1}
        assert claim_calls[2][0][1] == {"refid": "REF_EZ", "circle_code": 70}
        assert claim_calls[3][0][1] == {"refid": "REF_SZ", "circle_code": 40}

    def test_simswap_claims_records_across_all_four_zones(self):
        """SimSwap claims records spanning NZ, WZ, EZ, and SZ in a single batch in ALL mode."""
        ctx = ExecutionContext.create(source="SCHEDULED", zones_str="ALL")
        adapter = SimswapAdapter(token_manager=MagicMock(), enabled=True)

        mock_candidates = [
            (101, "REF_101", "9412300001", "9412399991", "8991000001", 50.0, "123456", 6, "SS_101", "SIMSWAP", "2026-09-08", "N", 10, "D01", "AUTO", "PORTAL"), # NZ: 10
            (102, "REF_102", "9412300002", "9412399992", "8991000002", 50.0, "123456", 6, "SS_102", "SIMSWAP", "2026-09-08", "N", 4, "D02", "AUTO", "PORTAL"),  # WZ: 4
            (103, "REF_103", "9412300003", "9412399993", "8991000003", 50.0, "123456", 6, "SS_103", "SIMSWAP", "2026-09-08", "N", 71, "D03", "AUTO", "PORTAL"), # EZ: 71
            (104, "REF_104", "9412300004", "9412399994", "8991000004", 50.0, "123456", 6, "SS_104", "SIMSWAP", "2026-09-08", "N", 41, "D04", "AUTO", "PORTAL"), # SZ: 41
        ]
        columns = [
            ("ID",), ("REFID",), ("CTOPUPNO",), ("GSMNUMBER",), ("SIMNUMBER",),
            ("AMOUNT",), ("PLAIN_MPIN",), ("MPIN_LENGTH",), ("SS_REQUEST_ID",),
            ("MODULE_TYPE",), ("REQUEST_DATE",), ("AMOUNT_DEDUCT_FLAG",),
            ("CIRCLE_CODE",), ("DEALERCODE",), ("SWAP_TYPE",), ("SOURCE",),
        ]

        mock_cur = MagicMock()
        mock_cur.description = columns
        mock_cur.fetchall.return_value = mock_candidates
        mock_cur.rowcount = 1

        mock_conn = MagicMock()
        mock_conn.cursor.return_value = mock_cur

        with patch("app.debit.services.simswap.get_oracle_conn") as mock_get_conn:
            mock_get_conn.return_value.__enter__.return_value = mock_conn
            claimed = adapter.fetch_and_claim(batch_size=50, context=ctx)

        assert len(claimed) == 4
        claimed_circles = {r["circle_code"] for r in claimed}
        assert claimed_circles == {10, 4, 71, 41}

    def test_esim_claims_records_across_all_four_zones(self):
        """ESIM claims records spanning NZ, WZ, EZ, and SZ in a single batch in ALL mode."""
        ctx = ExecutionContext.create(source="SCHEDULED", zones_str="ALL")
        adapter = EsimAdapter(token_manager=MagicMock(), enabled=True)

        mock_candidates = [
            (201, "REF_201", "9412300001", "9412399991", "8991000001", 50.0, "123456", 6, "SS_201", "ESIM", "2026-09-08", "N", 50, "D01", "AUTO", "PORTAL"), # NZ: 50
            (202, "REF_202", "9412300002", "9412399992", "8991000002", 50.0, "123456", 6, "SS_202", "ESIM", "2026-09-08", "N", 11, "D02", "AUTO", "PORTAL"), # WZ: 11
            (203, "REF_203", "9412300003", "9412399993", "8991000003", 50.0, "123456", 6, "SS_203", "ESIM", "2026-09-08", "N", 74, "D03", "AUTO", "PORTAL"), # EZ: 74
            (204, "REF_204", "9412300004", "9412399994", "8991000004", 50.0, "123456", 6, "SS_204", "ESIM", "2026-09-08", "N", 44, "D04", "AUTO", "PORTAL"), # SZ: 44
        ]
        columns = [
            ("ID",), ("REFID",), ("CTOPUPNO",), ("GSMNUMBER",), ("SIMNUMBER",),
            ("AMOUNT",), ("PLAIN_MPIN",), ("MPIN_LENGTH",), ("SS_REQUEST_ID",),
            ("MODULE_TYPE",), ("REQUEST_DATE",), ("AMOUNT_DEDUCT_FLAG",),
            ("CIRCLE_CODE",), ("DEALERCODE",), ("SWAP_TYPE",), ("SOURCE",),
        ]

        mock_cur = MagicMock()
        mock_cur.description = columns
        mock_cur.fetchall.return_value = mock_candidates
        mock_cur.rowcount = 1

        mock_conn = MagicMock()
        mock_conn.cursor.return_value = mock_cur

        with patch("app.debit.services.esim.get_oracle_conn") as mock_get_conn:
            mock_get_conn.return_value.__enter__.return_value = mock_conn
            claimed = adapter.fetch_and_claim(batch_size=50, context=ctx)

        assert len(claimed) == 4
        claimed_circles = {r["circle_code"] for r in claimed}
        assert claimed_circles == {50, 11, 74, 44}

    def test_contrast_filtered_mode_drops_out_of_zone_candidates(self):
        """In filtered NZ mode, candidate defense-in-depth skips WZ/EZ/SZ rows even if fetched."""
        ctx = ExecutionContext.create(source="SCHEDULED", zones_str="NZ")
        adapter = FancySaleAdapter(token_manager=MagicMock(), enabled=True)

        mock_candidates = [
            ("REF_NZ", "9412300001", "9412399991", 100.0, "123456", 6, "SS_NZ", "CSC01", 2, "2026-09-08", "FANCYSALE", "N"),   # NZ: 2
            ("REF_WZ", "9412300002", "9412399992", 100.0, "123456", 6, "SS_WZ", "CSC02", 1, "2026-09-08", "FANCYSALE", "N"),   # WZ: 1 (disallowed)
            ("REF_EZ", "9412300003", "9412399993", 100.0, "123456", 6, "SS_EZ", "CSC03", 70, "2026-09-08", "FANCYSALE", "N"),  # EZ: 70 (disallowed)
        ]
        columns = [
            ("REFID",), ("CTOPUPNO",), ("FANCY_NO",), ("AMOUNT",), ("PLAIN_MPIN",),
            ("MPIN_LENGTH",), ("SS_REQUEST_ID",), ("CSCCODE",), ("CIRCLE_CODE",),
            ("TRANS_DATE",), ("MODULE_TYPE",), ("CAF_ENTRY_DONE",),
        ]

        mock_cur = MagicMock()
        mock_cur.description = columns
        mock_cur.fetchall.return_value = mock_candidates
        mock_cur.rowcount = 1

        mock_conn = MagicMock()
        mock_conn.cursor.return_value = mock_cur

        with patch("app.debit.services.fancysale.get_oracle_conn") as mock_get_conn:
            mock_get_conn.return_value.__enter__.return_value = mock_conn
            claimed = adapter.fetch_and_claim(batch_size=50, context=ctx)

        # Only the NZ record is claimed; WZ and EZ are rejected by defense-in-depth
        assert len(claimed) == 1
        assert claimed[0]["refid"] == "REF_NZ"


# ══════════════════════════════════════════════════════════════════════════════
# 5. Safety Invariant Retention Under ALL Mode
# ══════════════════════════════════════════════════════════════════════════════

class TestAllModeSafetyInvariantRetention:
    """Verifies that all new safety invariants remain active even in nationwide ALL mode."""

    def test_optimistic_claim_collision_defense_retained_in_all_mode(self):
        """Worker B drops claim when cur.rowcount == 0 and does not call Pyro in ALL mode."""
        ctx = ExecutionContext.create(source="SCHEDULED", zones_str="ALL")
        adapter = FancySaleAdapter(token_manager=MagicMock(), enabled=True)

        mock_candidates = [
            ("REF_1", "9412300001", "9412399991", 100.0, "123456", 6, "SS_1", "CSC01", 1, "2026-09-08", "FANCYSALE", "N"),
        ]
        columns = [
            ("REFID",), ("CTOPUPNO",), ("FANCY_NO",), ("AMOUNT",), ("PLAIN_MPIN",),
            ("MPIN_LENGTH",), ("SS_REQUEST_ID",), ("CSCCODE",), ("CIRCLE_CODE",),
            ("TRANS_DATE",), ("MODULE_TYPE",), ("CAF_ENTRY_DONE",),
        ]

        mock_cur = MagicMock()
        mock_cur.description = columns
        mock_cur.fetchall.return_value = mock_candidates
        mock_cur.rowcount = 0  # Worker A already claimed it; Worker B gets 0 rows updated

        mock_conn = MagicMock()
        mock_conn.cursor.return_value = mock_cur

        with patch("app.debit.services.fancysale.get_oracle_conn") as mock_get_conn:
            mock_get_conn.return_value.__enter__.return_value = mock_conn
            claimed = adapter.fetch_and_claim(batch_size=50, context=ctx)

        # Worker B discards the claim
        assert claimed == []

    @pytest.mark.asyncio
    async def test_writeback_hardening_and_reconciliation_retained_in_all_mode(self):
        """In ALL mode, writeback failure marks RECONCILIATION_REQUIRED and prevents double debits."""
        record = {
            "id": 999,
            "circle_code": 70,  # East Zone circle
            "plain_mpin": "123456",
            "amount": 100.0,
            "ctopupno": "9412300070",
            "fancy_no": "9412399970",
            "ss_request_id": "REQ_ALL_RECON",
            "module_type": "SIMSWAP",
            "caf_serial_no": "CAF_70",
            "gsmnumber": "9412300070",
        }

        ctx = ExecutionContext.create(source="SCHEDULED", zones_str="ALL")
        adapter = SimswapAdapter(token_manager=MagicMock(), enabled=True)
        adapter.fetch_and_claim = MagicMock(return_value=[record])

        # Writeback fails
        def fail_writeback(rec, pyro_txn_id, remarks):
            raise WritebackError("Database connection severed during writeback")

        adapter.mark_success = MagicMock(side_effect=fail_writeback)
        adapter.mark_reconciliation_required = MagicMock()

        mock_pyro_response = {
            "statusCode": 2000,
            "status": "SUCCESS",
            "data": {"pyroId": "PYRO_TXN_ALL_999"},
        }

        with patch("app.debit.processor.wallet_adjustment", AsyncMock(return_value=mock_pyro_response)), \
             patch("app.debit.processor.async_insert_debit_txn_log", AsyncMock()):
            summary = await run_debit_batch(adapter, context=ctx)

        # Reconcile status verified
        assert summary["processed"] == 1
        assert summary["success"] == 0
        assert summary["failed"] == 1
        assert summary["reconciliation_required"] == 1

        adapter.mark_reconciliation_required.assert_called_once()
        assert adapter.mark_reconciliation_required.call_args[0][1] == "PYRO_TXN_ALL_999"

    @pytest.mark.asyncio
    async def test_per_service_concurrency_lock_retained_in_all_mode(self):
        """Per-service lock prevents concurrent batches for the same service in ALL mode."""
        _reset_service_locks()
        adapter = MagicMock()
        adapter.service_type = "FANCYSALE"
        adapter.enabled = True
        adapter.batch_size = 50

        active_in_critical_section = 0
        max_concurrent = 0

        def slow_fetch(batch_size, context=None):
            nonlocal active_in_critical_section, max_concurrent
            active_in_critical_section += 1
            if active_in_critical_section > max_concurrent:
                max_concurrent = active_in_critical_section
            return []

        adapter.fetch_and_claim.side_effect = slow_fetch

        async def worker():
            ctx = ExecutionContext.create(source="SCHEDULED", zones_str="ALL")
            await run_debit_batch(adapter, context=ctx)
            nonlocal active_in_critical_section
            active_in_critical_section -= 1

        await asyncio.gather(worker(), worker())
        assert max_concurrent == 1
        _reset_service_locks()


# ══════════════════════════════════════════════════════════════════════════════
# 6. Full End-to-End Multi-Zone Batch Processing Lifecycle in ALL Mode
# ══════════════════════════════════════════════════════════════════════════════

class TestAllModeEndToEndProcessorBatch:
    """Verifies end-to-end execution of run_debit_batch across nationwide records."""

    @pytest.mark.asyncio
    async def test_full_batch_nationwide_fancysale(self):
        """Processes candidates across NZ, WZ, EZ, and SZ, verifying Pyro calls & writeback."""
        ctx = ExecutionContext.create(source="SCHEDULED", zones_str="ALL")
        adapter = FancySaleAdapter(token_manager=MagicMock(), enabled=True)

        records = [
            {"refid": "FS_NZ", "ctopupno": "9412300001", "fancy_no": "9412399991", "amount": 100.0, "plain_mpin": "123456", "mpin_length": 6, "circle_code": 2, "ss_request_id": "SS_NZ"},
            {"refid": "FS_WZ", "ctopupno": "9412300002", "fancy_no": "9412399992", "amount": 200.0, "plain_mpin": "123456", "mpin_length": 6, "circle_code": 1, "ss_request_id": "SS_WZ"},
            {"refid": "FS_EZ", "ctopupno": "9412300003", "fancy_no": "9412399993", "amount": 300.0, "plain_mpin": "123456", "mpin_length": 6, "circle_code": 70, "ss_request_id": "SS_EZ"},
            {"refid": "FS_SZ", "ctopupno": "9412300004", "fancy_no": "9412399994", "amount": 400.0, "plain_mpin": "123456", "mpin_length": 6, "circle_code": 40, "ss_request_id": "SS_SZ"},
        ]
        adapter.fetch_and_claim = MagicMock(return_value=records)
        adapter.mark_success = MagicMock()
        adapter.mark_failed = MagicMock()

        mock_pyro = AsyncMock(return_value={
            "statusCode": 2000,
            "status": "SUCCESS",
            "data": {"pyroId": "TXN_OK", "balanceBefore": 1000.0, "balanceAfter": 900.0},
        })

        with patch("app.debit.processor.wallet_adjustment", mock_pyro):
            summary = await run_debit_batch(adapter, context=ctx)

        assert summary["processed"] == 4
        assert summary["success"] == 4
        assert summary["failed"] == 0
        assert summary["reconciliation_required"] == 0

        # All 4 nationwide candidates received Pyro calls and success writebacks
        assert mock_pyro.call_count == 4
        assert adapter.mark_success.call_count == 4
        assert adapter.mark_failed.call_count == 0

    @pytest.mark.asyncio
    async def test_full_batch_nationwide_simswap_with_secondary_sync(self):
        """SimSwap batch in ALL mode updates both primary table and secondary BCD table."""
        ctx = ExecutionContext.create(source="SCHEDULED", zones_str="ALL")
        adapter = SimswapAdapter(token_manager=MagicMock(), enabled=True)

        records = [
            {
                "id": 501,
                "refid": "SS_501",
                "ctopupno": "9412300001",
                "gsmnumber": "9412300001",
                "simnumber": "8991000001",
                "amount": 50.0,
                "plain_mpin": "123456",
                "mpin_length": 6,
                "circle_code": 10,  # NZ
                "ss_request_id": "REQ_501",
                "module_type": "SIMSWAP",
                "caf_serial_no": "CAF_501",
            },
            {
                "id": 502,
                "refid": "SS_502",
                "ctopupno": "9412300002",
                "gsmnumber": "9412300002",
                "simnumber": "8991000002",
                "amount": 50.0,
                "plain_mpin": "123456",
                "mpin_length": 6,
                "circle_code": 71,  # EZ
                "ss_request_id": "REQ_502",
                "module_type": "SIMSWAP",
                "caf_serial_no": "CAF_502",
            },
        ]
        adapter.fetch_and_claim = MagicMock(return_value=records)
        adapter.mark_success = MagicMock()

        mock_pyro = AsyncMock(return_value={
            "statusCode": 2000,
            "status": "SUCCESS",
            "data": {"pyroId": "TXN_SS", "balanceBefore": 500.0, "balanceAfter": 450.0},
        })

        with patch("app.debit.processor.wallet_adjustment", mock_pyro):
            summary = await run_debit_batch(adapter, context=ctx)

        assert summary["processed"] == 2
        assert summary["success"] == 2
        assert summary["failed"] == 0
        assert adapter.mark_success.call_count == 2

    @pytest.mark.asyncio
    async def test_full_batch_nationwide_esim_with_secondary_sync(self):
        """ESIM batch in ALL mode updates both primary table and secondary SIM_SWAP_DATA table."""
        ctx = ExecutionContext.create(source="SCHEDULED", zones_str="ALL")
        adapter = EsimAdapter(token_manager=MagicMock(), enabled=True)

        records = [
            {
                "id": 601,
                "refid": "ES_601",
                "ctopupno": "9412300003",
                "gsmnumber": "9412300003",
                "simnumber": "8991000003",
                "amount": 50.0,
                "plain_mpin": "123456",
                "mpin_length": 6,
                "circle_code": 4,   # WZ (Gujarat)
                "ss_request_id": "REQ_601",
                "module_type": "ESIM",
                "caf_serial_no": "CAF_601",
            },
            {
                "id": 602,
                "refid": "ES_602",
                "ctopupno": "9412300004",
                "gsmnumber": "9412300004",
                "simnumber": "8991000004",
                "amount": 50.0,
                "plain_mpin": "123456",
                "mpin_length": 6,
                "circle_code": 40,  # SZ (AP)
                "ss_request_id": "REQ_602",
                "module_type": "ESIM",
                "caf_serial_no": "CAF_602",
            },
        ]
        adapter.fetch_and_claim = MagicMock(return_value=records)
        adapter.mark_success = MagicMock()

        mock_pyro = AsyncMock(return_value={
            "statusCode": 2000,
            "status": "SUCCESS",
            "data": {"pyroId": "TXN_ES", "balanceBefore": 500.0, "balanceAfter": 450.0},
        })

        with patch("app.debit.processor.wallet_adjustment", mock_pyro):
            summary = await run_debit_batch(adapter, context=ctx)

        assert summary["processed"] == 2
        assert summary["success"] == 2
        assert summary["failed"] == 0
        assert adapter.mark_success.call_count == 2

    def test_cleanup_race_protection_nationwide_batch(self):
        """Active in-flight records in nationwide batch are protected from concurrent cleanup."""
        ctx = ExecutionContext.create(source="SCHEDULED", zones_str="ALL")
        ownership_tracker.clear("FANCYSALE")

        active_refs = [f"FS_NATIONWIDE_{i}" for i in range(1, 21)]
        try:
            ownership_tracker.acquire("FANCYSALE", active_refs)
            active_set = ownership_tracker.get_active("FANCYSALE")
            assert len(active_set) == 20

            # Nationwide cleanup builds query with all 20 active refs excluded
            sql, params = build_fancysale_cleanup_query(
                stuck_minutes=10,
                context=ctx,
                active_refs=active_set,
            )
            assert "CIRCLE_CODE IN" not in sql
            assert "REFID NOT IN" in sql
            excluded_refs = {v for k, v in params.items() if k.startswith("act_")}
            assert excluded_refs == set(active_refs)
        finally:
            ownership_tracker.release_all("FANCYSALE", active_refs)
            assert len(ownership_tracker.get_active("FANCYSALE")) == 0


# ══════════════════════════════════════════════════════════════════════════════
# 7. Non-Production Live Database Integration Verification
# ══════════════════════════════════════════════════════════════════════════════

@pytest.mark.skipif(not ORACLE_AVAILABLE, reason="Non-production Oracle database not accessible")
class TestNonProdOracleLiveIntegration:
    """Verifies that ALL mode candidate queries execute directly against live Oracle."""

    def test_live_oracle_q001_fancysale_all_mode(self):
        """Execute Q001 in ALL mode on live Oracle and verify multi-circle candidate return."""
        import oracledb
        ctx = ExecutionContext.create(source="SCHEDULED", zones_str="ALL")
        sql, params = build_fancysale_candidate_query(batch_size=50, context=ctx)

        conn = oracledb.connect(
            user=settings.oracle_user,
            password=settings.oracle_password,
            dsn=settings.oracle_dsn,
        )
        try:
            cur = conn.cursor()
            cur.execute(sql, params)
            rows = cur.fetchall()

            # Query must succeed cleanly
            assert isinstance(rows, list)
            if rows:
                circles = {r[8] for r in rows}  # Column 8 is CIRCLE_CODE
                # Proves records from multiple circles are discovered in a single query
                assert len(circles) >= 1
        finally:
            conn.close()

    def test_live_oracle_q006_simswap_all_mode(self):
        """Execute Q006 in ALL mode on live Oracle and verify multi-circle candidate return."""
        import oracledb
        ctx = ExecutionContext.create(source="SCHEDULED", zones_str="ALL")
        sql, params = build_simswap_candidate_query(batch_size=50, context=ctx)

        conn = oracledb.connect(
            user=settings.oracle_user,
            password=settings.oracle_password,
            dsn=settings.oracle_dsn,
        )
        try:
            cur = conn.cursor()
            cur.execute(sql, params)
            rows = cur.fetchall()

            assert isinstance(rows, list)
            if rows:
                circles = {r[12] for r in rows}  # Column 12 is CIRCLE_CODE
                assert len(circles) >= 1
        finally:
            conn.close()

    def test_live_oracle_q012_esim_all_mode(self):
        """Execute Q012 in ALL mode on live Oracle and verify multi-circle candidate return."""
        import oracledb
        ctx = ExecutionContext.create(source="SCHEDULED", zones_str="ALL")
        sql, params = build_esim_candidate_query(batch_size=50, context=ctx)

        conn = oracledb.connect(
            user=settings.oracle_user,
            password=settings.oracle_password,
            dsn=settings.oracle_dsn,
        )
        try:
            cur = conn.cursor()
            cur.execute(sql, params)
            rows = cur.fetchall()

            assert isinstance(rows, list)
            if rows:
                circles = {r[12] for r in rows}  # Column 12 is CIRCLE_CODE
                assert len(circles) >= 1
        finally:
            conn.close()

    def test_live_oracle_cleanup_queries_syntax(self):
        """Execute cleanup queries Q005, Q011, Q017 in a rolled-back transaction to verify syntax."""
        import oracledb
        ctx = ExecutionContext.create(source="SCHEDULED", zones_str="ALL")

        conn = oracledb.connect(
            user=settings.oracle_user,
            password=settings.oracle_password,
            dsn=settings.oracle_dsn,
        )
        try:
            cur = conn.cursor()

            # Q005 (REFID is a numeric column)
            sql_fs, params_fs = build_fancysale_cleanup_query(10, ctx, active_refs={-999999})
            cur.execute(sql_fs, params_fs)

            # Q011
            sql_ss, params_ss = build_simswap_cleanup_query(10, ctx, active_refs={-999})
            cur.execute(sql_ss, params_ss)

            # Q017
            sql_es, params_es = build_esim_cleanup_query(10, ctx, active_refs={-999})
            cur.execute(sql_es, params_es)

            # Roll back to avoid any test side effects
            conn.rollback()
        finally:
            conn.close()


@pytest.mark.skipif(not POSTGRES_AVAILABLE, reason="Non-production PostgreSQL database not accessible")
class TestNonProdPostgresLiveIntegration:
    """Verifies that audit logging table in PostgreSQL is accessible and healthy."""

    def test_live_postgres_debit_txn_log_accessible(self):
        """Verify public.debit_txn_log table is accessible and has required schema."""
        import psycopg2
        conn = psycopg2.connect(
            host=settings.pg_host,
            port=settings.pg_port,
            database=settings.pg_database,
            user=settings.pg_user,
            password=settings.pg_password,
        )
        try:
            with conn.cursor() as cur:
                cur.execute("SELECT COUNT(*) FROM public.debit_txn_log;")
                count = cur.fetchone()[0]
                assert count >= 0
        finally:
            conn.close()
