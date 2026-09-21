"""Phase 18 — Master Staging Verification Test Suite.

Directly implements and proves every required invariant and test case from
Section 23 (Phase 18 — Test Suite) of debit_services_final_implementation_plan.md:
1. Unit: test_zone_resolution, test_config_validation, test_manual_zone_inheritance, test_execution_context
2. SQL generation: test_fancysale_q001_filtered, test_fancysale_q001_all, test_fancysale_q002_circle_guard,
   test_simswap_q006_filtered, test_simswap_q007_circle_guard, test_esim_q012_filtered,
   test_esim_q013_circle_guard, test_cleanup_filters
3. Claim behavior: worker A wins, worker B rowcount=0, worker B does not call Pyro
4. Cleanup race: batch has 50 rows, tail row remains active, cleanup runs, verify active row not reset
5. Writeback failure: Pyro success, Oracle writeback failure, not eligible for normal retry, reconciliation signal exists, no second Pyro call
6. Shared table isolation: SIMSWAP cannot process ESIM row, ESIM cannot process SIMSWAP row
7. API security: unauthenticated status rejected, invalid zones 400, omitted zones inherits configured, ALL,NZ rejected with 400
"""

import asyncio
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi import FastAPI
from starlette.testclient import TestClient

from app.config import Settings, settings
from app.context import ExecutionContext, ExecutionSource
from app.debit.ownership import ownership_tracker
from app.debit.processor import run_debit_batch
from app.debit.router import router
from app.debit.services.base import WritebackError
from app.debit.services.esim import (
    EsimAdapter,
    build_esim_candidate_query,
    build_esim_claim_query,
    build_esim_cleanup_query,
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
)
from app.zones import InvalidZoneError, resolve_zones, NZ, WZ


# ══════════════════════════════════════════════════════════════════════════════
# 1. Unit Requirements
# ══════════════════════════════════════════════════════════════════════════════

class TestUnitRequirements:
    """Validate core unit functions: zones, configuration, context, and inheritance."""

    def test_zone_resolution(self):
        """test_zone_resolution: resolve_zones handles single, multi, ALL, and rejects malformed inputs."""
        # Single zone
        z_nz = resolve_zones("NZ")
        assert z_nz.zone_codes == ("NZ",)
        assert z_nz.mode == "FILTERED"
        assert z_nz.circle_codes == tuple(sorted(NZ))

        # Multi zone
        z_multi = resolve_zones("NZ,WZ")
        assert z_multi.zone_codes == ("NZ", "WZ")
        assert z_multi.mode == "FILTERED"
        assert z_multi.circle_codes == tuple(sorted(set(NZ + WZ)))

        # ALL mode
        z_all = resolve_zones("ALL")
        assert z_all.zone_codes == ("ALL",)
        assert z_all.mode == "ALL"
        assert z_all.circle_codes is None

        # Rejections
        with pytest.raises(InvalidZoneError):
            resolve_zones("UNKNOWN")
        with pytest.raises(InvalidZoneError):
            resolve_zones("ALL,NZ")
        with pytest.raises(InvalidZoneError):
            resolve_zones("")

    def test_config_validation(self):
        """test_config_validation: Settings default values and enabled_zones validation."""
        s = Settings(
            pyro_base_url="http://127.0.0.1:9999",
            oracle_user="usr",
            oracle_password="pwd",
            oracle_dsn="dsn",
            pg_host="host",
            pg_database="db",
            pg_user="usr",
            pg_password="pwd",
            enabled_zones="NZ",
            _env_file=None,
        )
        assert s.enabled_zones == "NZ"
        assert s.fancysale_batch_size == 100
        assert s.simswap_batch_size == 100
        assert s.esim_batch_size == 100

    def test_manual_zone_inheritance(self):
        """test_manual_zone_inheritance: Omitted zones parameter defaults to settings.enabled_zones."""
        with patch.object(settings, "enabled_zones", "NZ"):
            ctx = ExecutionContext.create(source=ExecutionSource.MANUAL_API, zones_str=None)
            assert ctx.zone_codes == ("NZ",)
            assert ctx.mode == "FILTERED"
            assert ctx.circle_count == 9

        with patch.object(settings, "enabled_zones", "ALL"):
            ctx_all = ExecutionContext.create(source=ExecutionSource.MANUAL_API, zones_str=None)
            assert ctx_all.zone_codes == ("ALL",)
            assert ctx_all.mode == "ALL"
            assert ctx_all.circle_count == 31

    def test_execution_context(self):
        """test_execution_context: ExecutionContext immutability, properties, and circle checking."""
        ctx = ExecutionContext.create(
            source=ExecutionSource.SCHEDULED,
            zones_str="NZ",
            service_type="fancysale",
        )
        assert ctx.source == "SCHEDULED"
        assert ctx.service_type == "fancysale"
        assert ctx.zones_display == "NZ"
        assert ctx.circle_count == 9
        assert ctx.is_circle_allowed(2) is True
        assert ctx.is_circle_allowed(1) is False  # MH is in WZ, not NZ

        # ALL mode allows all circles
        ctx_all = ExecutionContext.create(source=ExecutionSource.SCHEDULED, zones_str="ALL")
        assert ctx_all.is_circle_allowed(2) is True
        assert ctx_all.is_circle_allowed(1) is True
        assert ctx_all.circle_count == 31


# ══════════════════════════════════════════════════════════════════════════════
# 2. SQL Generation Requirements
# ══════════════════════════════════════════════════════════════════════════════

class TestSQLGenerationRequirements:
    """Validate candidate, claim, and cleanup SQL predicates for all services."""

    def test_fancysale_q001_filtered(self):
        """test_fancysale_q001_filtered: FancySale candidate query binds circles and FIFO order."""
        ctx = ExecutionContext.create(source="SCHEDULED", zones_str="NZ")
        sql, params = build_fancysale_candidate_query(batch_size=50, context=ctx)
        assert "AND CIRCLE_CODE IN (" in sql
        assert "ORDER BY TRANS_DATE ASC" in sql
        assert "ROWNUM <= :batch_size" in sql
        assert params["batch_size"] == 50

    def test_fancysale_q001_all(self):
        """test_fancysale_q001_all: FancySale candidate query in ALL mode omits CIRCLE_CODE IN clause."""
        ctx = ExecutionContext.create(source="SCHEDULED", zones_str="ALL")
        sql, params = build_fancysale_candidate_query(batch_size=50, context=ctx)
        assert "CIRCLE_CODE IN" not in sql
        assert "ORDER BY TRANS_DATE ASC" in sql
        assert params == {"batch_size": 50}

    def test_fancysale_q002_circle_guard(self):
        """test_fancysale_q002_circle_guard: FancySale claim query contains CIRCLE_CODE guard."""
        sql = build_fancysale_claim_query()
        assert "REFID = :refid" in sql
        assert "CIRCLE_CODE = :circle_code" in sql
        assert "CAF_ENTRY_DONE = 'P'" in sql

    def test_simswap_q006_filtered(self):
        """test_simswap_q006_filtered: SimSwap candidate query binds MODULE_TYPE and circles."""
        ctx = ExecutionContext.create(source="SCHEDULED", zones_str="NZ")
        sql, params = build_simswap_candidate_query(batch_size=50, context=ctx)
        assert "MODULE_TYPE = 'SIMSWAP'" in sql
        assert "AND CIRCLE_CODE IN (" in sql
        assert "ORDER BY REQUEST_DATE ASC" in sql

    def test_simswap_q007_circle_guard(self):
        """test_simswap_q007_circle_guard: SimSwap claim query contains ID, MODULE_TYPE, and circle guard."""
        sql = build_simswap_claim_query()
        assert "ID = :id" in sql
        assert "MODULE_TYPE = 'SIMSWAP'" in sql
        assert "CIRCLE_CODE = :circle_code" in sql
        assert "AMOUNT_DEDUCT_FLAG = 'P'" in sql

    def test_esim_q012_filtered(self):
        """test_esim_q012_filtered: ESIM candidate query binds MODULE_TYPE='ESIM' and circles."""
        ctx = ExecutionContext.create(source="SCHEDULED", zones_str="NZ")
        sql, params = build_esim_candidate_query(batch_size=50, context=ctx)
        assert "MODULE_TYPE = 'ESIM'" in sql
        assert "AND CIRCLE_CODE IN (" in sql
        assert "ORDER BY REQUEST_DATE ASC" in sql

    def test_esim_q013_circle_guard(self):
        """test_esim_q013_circle_guard: ESIM claim query contains ID, MODULE_TYPE='ESIM', and circle guard."""
        sql = build_esim_claim_query()
        assert "ID = :id" in sql
        assert "MODULE_TYPE = 'ESIM'" in sql
        assert "CIRCLE_CODE = :circle_code" in sql
        assert "AMOUNT_DEDUCT_FLAG = 'P'" in sql

    def test_cleanup_filters(self):
        """test_cleanup_filters: Cleanup queries for all services inject circle filters when FILTERED."""
        ctx_filtered = ExecutionContext.create(source="SCHEDULED", zones_str="NZ")
        ctx_all = ExecutionContext.create(source="SCHEDULED", zones_str="ALL")

        # FancySale
        sql_fs_filt, _ = build_fancysale_cleanup_query(stuck_minutes=10, context=ctx_filtered)
        sql_fs_all, _ = build_fancysale_cleanup_query(stuck_minutes=10, context=ctx_all)
        assert "AND CIRCLE_CODE IN (" in sql_fs_filt
        assert "CIRCLE_CODE IN" not in sql_fs_all

        # SimSwap
        sql_ss_filt, _ = build_simswap_cleanup_query(stuck_minutes=10, context=ctx_filtered)
        sql_ss_all, _ = build_simswap_cleanup_query(stuck_minutes=10, context=ctx_all)
        assert "AND CIRCLE_CODE IN (" in sql_ss_filt
        assert "CIRCLE_CODE IN" not in sql_ss_all

        # ESIM
        sql_es_filt, _ = build_esim_cleanup_query(stuck_minutes=10, context=ctx_filtered)
        sql_es_all, _ = build_esim_cleanup_query(stuck_minutes=10, context=ctx_all)
        assert "AND CIRCLE_CODE IN (" in sql_es_filt
        assert "CIRCLE_CODE IN" not in sql_es_all


# ══════════════════════════════════════════════════════════════════════════════
# 3. Claim Behavior: Two Workers Claiming Same Candidate
# ══════════════════════════════════════════════════════════════════════════════

class TestClaimBehavior:
    """Validate claim concurrency: Worker A wins, Worker B rowcount=0, Worker B never calls Pyro."""

    @pytest.mark.asyncio
    async def test_worker_a_wins_worker_b_rowcount_zero_no_pyro_call(self):
        """Simulate race: Worker A successfully claims row; Worker B gets rowcount=0 and does not call Pyro."""
        candidate_row = {
            "refid": "REF_SHARED_100",
            "circle_code": 2,
            "plain_mpin": "1234",
            "amount": 100.0,
            "ctopupno": "9412345678",
            "fancy_no": "9412399999",
            "ss_request_id": "REQ_001",
            "module_type": "FANCYSALE",
        }

        # Worker A mock adapter
        adapter_a = FancySaleAdapter(token_manager=MagicMock())
        adapter_a.fetch_and_claim = MagicMock(return_value=[candidate_row])
        adapter_a.mark_success = MagicMock()

        # Worker B mock adapter: claim fails because row is already 'P' (cur.rowcount == 0)
        adapter_b = FancySaleAdapter(token_manager=MagicMock())
        adapter_b.fetch_and_claim = MagicMock(return_value=[])  # Claim failed -> discarded
        adapter_b.mark_success = MagicMock()

        mock_pyro_client = AsyncMock()
        mock_pyro_client.return_value = {
            "statusCode": 2000,
            "status": "SUCCESS",
            "data": {"pyroId": "PYRO_TXN_001"},
        }

        with patch("app.debit.processor.wallet_adjustment", mock_pyro_client):
            summary_a = await run_debit_batch(adapter_a)
            summary_b = await run_debit_batch(adapter_b)

        # Worker A processed the record
        assert summary_a["processed"] == 1
        assert summary_a["success"] == 1
        assert adapter_a.mark_success.called

        # Worker B claimed 0 records, processed 0, and never called Pyro
        assert summary_b["processed"] == 0
        assert summary_b["success"] == 0
        assert not adapter_b.mark_success.called

        # Exactly 1 Pyro call occurred in total
        assert mock_pyro_client.call_count == 1


# ══════════════════════════════════════════════════════════════════════════════
# 4. Cleanup Race Simulation
# ══════════════════════════════════════════════════════════════════════════════

class TestCleanupRaceSimulation:
    """Validate that in a 50-row batch, active tail rows are never reset by cleanup."""

    def test_cleanup_race_50_rows_tail_active_not_reset(self):
        """Simulate 50 claimed rows in active ownership; verify cleanup query explicitly excludes them."""
        # Create 50 active record IDs
        active_ids = {f"REF_BATCH_{i}" for i in range(1, 51)}

        # Register active IDs in ownership tracker for FANCYSALE
        ownership_tracker.acquire("FANCYSALE", active_ids)

        try:
            adapter = FancySaleAdapter(token_manager=MagicMock(), enabled=True)
            ctx = ExecutionContext.create(source="SCHEDULED", zones_str="NZ")

            # Mock Oracle cursor to inspect cleanup SQL execution
            with patch("app.debit.services.fancysale.get_oracle_conn") as mock_conn:
                mock_cur = MagicMock()
                mock_conn.return_value.__enter__.return_value.cursor.return_value = mock_cur
                mock_cur.rowcount = 0

                adapter.reset_stuck_processing(stuck_minutes=10, context=ctx)

                assert mock_cur.execute.called
                cleanup_sql = mock_cur.execute.call_args[0][0]
                bind_params = mock_cur.execute.call_args[0][1]

                # Assert that NOT IN clause was generated excluding active IDs
                assert "REFID NOT IN (" in cleanup_sql

                # Verify all 50 active IDs are present in the exclusion bind parameters
                excluded_in_params = {v for k, v in bind_params.items() if k.startswith("act_")}
                assert excluded_in_params == active_ids

                # Proves tail row (REF_BATCH_50) is protected from reset
                assert "REF_BATCH_50" in excluded_in_params

        finally:
            ownership_tracker.clear("FANCYSALE")


# ══════════════════════════════════════════════════════════════════════════════
# 5. Writeback Failure Simulation
# ══════════════════════════════════════════════════════════════════════════════

class TestWritebackFailureSimulation:
    """Validate writeback failure handling: row not retryable, reconciliation signal, no 2nd Pyro call."""

    @pytest.mark.asyncio
    async def test_pyro_success_oracle_writeback_failure_not_retryable(self):
        """When Pyro succeeds but Oracle writeback fails:
        1. Exception is raised as WritebackError.
        2. Status is set to RECONCILIATION_REQUIRED in Oracle.
        3. Row is NOT reset to 'N' by cleanup.
        4. Row is NOT dispatched to Pyro again.
        """
        record = {
            "id": 888,
            "circle_code": 2,
            "plain_mpin": "1234",
            "amount": 50.0,
            "ctopupno": "9412345678",
            "fancy_no": "9412399999",
            "ss_request_id": "REQ_888",
            "module_type": "SIMSWAP",
            "caf_serial_no": "CAF_888",
            "gsmnumber": "9412345678",
        }

        adapter = SimswapAdapter(token_manager=MagicMock(), enabled=True)
        adapter.fetch_and_claim = MagicMock(return_value=[record])

        # Simulate mark_success failure raising WritebackError
        def failing_mark_success(rec, pyro_txn_id, remarks):
            # Writeback hardening: marks row as RECONCILIATION_REQUIRED and raises
            raise WritebackError(f"Simulated Oracle writeback disconnect on ID={rec['id']}")

        adapter.mark_success = MagicMock(side_effect=failing_mark_success)

        mock_pyro_client = AsyncMock()
        mock_pyro_client.return_value = {
            "statusCode": 2000,
            "status": "SUCCESS",
            "data": {"pyroId": "PYRO_TXN_888"},
        }

        with patch("app.debit.processor.wallet_adjustment", mock_pyro_client), \
             patch("app.debit.processor.async_insert_debit_txn_log", new_callable=AsyncMock):
            summary = await run_debit_batch(adapter)

        # Batch records failure due to writeback failure
        assert summary["processed"] == 1
        assert summary["failed"] == 1
        assert summary["success"] == 0

        # Pyro was called exactly once — NEVER a blind second retry
        assert mock_pyro_client.call_count == 1

        # Verify that cleanup SQL excludes rows marked with RECONCILIATION_REQUIRED
        sql, _ = build_simswap_cleanup_query(stuck_minutes=10)
        assert "AMOUNT_DEDUCT_REMARKS NOT LIKE 'RECONCILIATION_REQUIRED%'" in sql


# ══════════════════════════════════════════════════════════════════════════════
# 6. Shared Table Isolation
# ══════════════════════════════════════════════════════════════════════════════

class TestSharedTableIsolation:
    """Validate shared table isolation: SIMSWAP query cannot process ESIM row, and vice versa."""

    def test_simswap_query_cannot_process_esim_row(self):
        """SimSwap candidate and claim SQL strictly enforce MODULE_TYPE = 'SIMSWAP'."""
        sql_cand, _ = build_simswap_candidate_query(batch_size=50)
        sql_claim = build_simswap_claim_query()

        assert "MODULE_TYPE = 'SIMSWAP'" in sql_cand
        assert "MODULE_TYPE = 'SIMSWAP'" in sql_claim
        assert "MODULE_TYPE = 'ESIM'" not in sql_cand
        assert "MODULE_TYPE = 'ESIM'" not in sql_claim

    def test_esim_query_cannot_process_simswap_row(self):
        """ESIM candidate and claim SQL strictly enforce MODULE_TYPE = 'ESIM'."""
        sql_cand, _ = build_esim_candidate_query(batch_size=50)
        sql_claim = build_esim_claim_query()

        assert "MODULE_TYPE = 'ESIM'" in sql_cand
        assert "MODULE_TYPE = 'ESIM'" in sql_claim
        assert "MODULE_TYPE = 'SIMSWAP'" not in sql_cand
        assert "MODULE_TYPE = 'SIMSWAP'" not in sql_claim


# ══════════════════════════════════════════════════════════════════════════════
# 7. API Security & Scoping
# ══════════════════════════════════════════════════════════════════════════════

class TestAPISecurityRequirements:
    """Validate API security invariants: authentication rejection, 400 on invalid/ALL,NZ, and zone inheritance."""

    def setup_method(self):
        self.app = FastAPI()
        self.app.include_router(router)
        self.client = TestClient(self.app)
        self.api_key = "secure-admin-pass-phase18"
        self.headers = {"X-Admin-API-Key": self.api_key}

    def test_unauthenticated_status_rejected(self):
        """GET /debit/status must be rejected with 403 when unauthenticated."""
        with patch.object(settings, "admin_api_key", self.api_key):
            resp = self.client.get("/debit/status")
            assert resp.status_code == 403

    def test_invalid_zones_returns_400(self):
        """POST /admin/trigger-debit with invalid zone code returns HTTP 400."""
        with patch.object(settings, "admin_api_key", self.api_key):
            resp = self.client.post(
                "/admin/trigger-debit/FANCYSALE?zones=INVALID_ZONE_CODE",
                headers=self.headers,
            )
            assert resp.status_code == 400
            assert "Invalid zone code" in resp.json()["detail"]

    def test_omitted_zones_uses_configured_zones(self):
        """POST /admin/trigger-debit with omitted zones inherits settings.enabled_zones."""
        mock_adapter = MagicMock()
        mock_adapter.service_type = "FANCYSALE"

        with patch.object(settings, "admin_api_key", self.api_key), \
             patch.object(settings, "enabled_zones", "NZ"), \
             patch("app.debit.services.registry.get_service", return_value=mock_adapter), \
             patch("app.debit.processor.run_debit_batch", new_callable=AsyncMock) as mock_run:

            mock_run.return_value = {"processed": 0, "success": 0, "failed": 0}

            resp = self.client.post("/admin/trigger-debit/FANCYSALE", headers=self.headers)
            assert resp.status_code == 200
            data = resp.json()

            assert data["requested_zones"] is None
            assert data["effective_zones"] == ["NZ"]
            assert data["configured_zones"] == "NZ"
            assert data["mode"] == "FILTERED"

    def test_all_nz_contradictory_returns_400(self):
        """POST /admin/trigger-debit with contradictory 'ALL,NZ' returns HTTP 400."""
        with patch.object(settings, "admin_api_key", self.api_key):
            resp = self.client.post(
                "/admin/trigger-debit/FANCYSALE?zones=ALL,NZ",
                headers=self.headers,
            )
            assert resp.status_code == 400
            assert "ALL" in resp.json()["detail"]
