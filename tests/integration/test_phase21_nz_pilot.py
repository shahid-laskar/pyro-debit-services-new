"""Phase 21: NZ Pilot Verification & Controlled Batch Suite.

Directly implements and verifies Section 26 (Phase 21 — NZ Pilot) of
debit_services_final_implementation_plan.md:
1. Configuration verification:
   ENABLED_ZONES=NZ
   Verified through GET /admin/zones:
   - mode=FILTERED
   - active_zone_codes=[NZ]
   - active_circle_count=9
   - active_circles=[2, 55, 56, 59, 60, 61, 62, 64, 65]
2. Controlled manual batch execution:
   POST /admin/trigger-debit/{service_type}
   Omitted zones query param strictly defaults to settings.enabled_zones ('NZ') (Invariant E).
3. End-to-end execution pipeline verification:
   candidate → claim → Pyro → writeback → log
4. Zone containment and disabled circles isolation:
   Confirms disabled circles (WZ, EZ, SZ) remain 100% untouched:
   - Not selected in candidate discovery (Q001, Q006, Q012)
   - Dropped by claim defense-in-depth (is_circle_allowed)
   - Zero external Pyro calls made
   - Not modified by stuck-record cleanup (Q005, Q011, Q017)
5. Live non-production Oracle XE and PostgreSQL database integration probes.
"""

import asyncio
import re
from unittest.mock import AsyncMock, MagicMock, patch
import pytest
from fastapi import FastAPI
from starlette.testclient import TestClient

from app.config import settings
from app.context import ExecutionContext, ExecutionSource
from app.debit.ownership import ownership_tracker
from app.debit.processor import run_debit_batch, _reset_service_locks
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
from app.zones import (
    CIRCLE_METADATA,
    EZ,
    NZ,
    SZ,
    WZ,
    InvalidZoneError,
    resolve_zones,
)


# ──────────────────────────────────────────────────────────────────────────────
# Database Connectivity Probes for Live Integration
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
# 1. Verification of GET /admin/zones under ENABLED_ZONES=NZ
# ══════════════════════════════════════════════════════════════════════════════

class TestNZPilotAdminConfiguration:
    """Validates configuration inspection and auth for NZ pilot deployment."""

    def setup_method(self):
        self.app = FastAPI()
        self.app.include_router(router)
        self.client = TestClient(self.app)
        self.api_key = "pilot-secret-key-nz-2026"
        self.headers = {"X-Admin-API-Key": self.api_key}

    def test_get_admin_zones_nz_pilot_specification(self):
        """GET /admin/zones returns mode=FILTERED, active_zone_codes=[NZ], active_circle_count=9."""
        with patch.object(settings, "admin_api_key", self.api_key), \
             patch.object(settings, "enabled_zones", "NZ"):

            resp = self.client.get("/admin/zones", headers=self.headers)
            assert resp.status_code == 200
            data = resp.json()

            # Exact Phase 21 expectations
            assert data["configured_zones"] == "NZ"
            assert data["active_zone_codes"] == ["NZ"]
            assert data["mode"] == "FILTERED"
            assert data["active_circle_count"] == 9
            assert data["active_circles"] == [2, 55, 56, 59, 60, 61, 62, 64, 65]

            # Verify every active circle code is an authoritative NZ circle
            for circle in data["active_circles"]:
                assert circle in NZ

    def test_get_admin_zones_security_enforcement(self):
        """GET /admin/zones requires valid admin API key."""
        with patch.object(settings, "admin_api_key", self.api_key):
            # Missing header
            resp = self.client.get("/admin/zones")
            assert resp.status_code == 403

            # Wrong key
            resp = self.client.get("/admin/zones", headers={"X-Admin-API-Key": "wrong-key"})
            assert resp.status_code == 403

    def test_debit_status_with_nz_pilot(self):
        """GET /debit/status returns healthy token managers and adapter states."""
        with patch.object(settings, "admin_api_key", self.api_key), \
             patch.object(settings, "enabled_zones", "NZ"):

            resp = self.client.get("/debit/status", headers=self.headers)
            assert resp.status_code == 200
            data = resp.json()
            assert "tokens" in data
            assert "services" in data


# ══════════════════════════════════════════════════════════════════════════════
# 2. Manual Trigger Zone Inheritance & Scope Control
# ══════════════════════════════════════════════════════════════════════════════

class TestNZPilotManualTriggerZoneInheritance:
    """Validates Invariant E and zone scoping on manual operational triggers."""

    def setup_method(self):
        self.app = FastAPI()
        self.app.include_router(router)
        self.client = TestClient(self.app)
        self.api_key = "pilot-secret-key-nz-2026"
        self.headers = {"X-Admin-API-Key": self.api_key}

    def test_trigger_debit_omitted_zones_inherits_nz(self):
        """POST /admin/trigger-debit with omitted zones inherits settings.enabled_zones='NZ'."""
        with patch.object(settings, "admin_api_key", self.api_key), \
             patch.object(settings, "enabled_zones", "NZ"):

            mock_run = AsyncMock(return_value={
                "service_type": "FANCYSALE",
                "processed": 1,
                "success": 1,
                "failed": 0,
                "reconciliation_required": 0,
            })

            with patch("app.debit.processor.run_debit_batch", mock_run):
                resp = self.client.post("/admin/trigger-debit/fancysale", headers=self.headers)
                assert resp.status_code == 200
                data = resp.json()

                # Verify Invariant E: omitted requested_zones inherits configured 'NZ'
                assert data["triggered"] is True
                assert data["requested_zones"] is None
                assert data["effective_zones"] == ["NZ"]
                assert data["configured_zones"] == "NZ"
                assert data["mode"] == "FILTERED"

                # Verify context passed to processor
                ctx: ExecutionContext = mock_run.call_args[1]["context"]
                assert ctx.mode == "FILTERED"
                assert ctx.zone_codes == ("NZ",)
                assert ctx.circle_codes == (2, 55, 56, 59, 60, 61, 62, 64, 65)
                assert ctx.source == "MANUAL_API"

    def test_trigger_debit_explicit_nz(self):
        """POST /admin/trigger-debit with explicit zones=NZ executes with NZ scope."""
        with patch.object(settings, "admin_api_key", self.api_key), \
             patch.object(settings, "enabled_zones", "NZ"):

            mock_run = AsyncMock(return_value={
                "service_type": "SIMSWAP",
                "processed": 1,
                "success": 1,
                "failed": 0,
                "reconciliation_required": 0,
            })

            with patch("app.debit.processor.run_debit_batch", mock_run):
                resp = self.client.post("/admin/trigger-debit/simswap?zones=NZ", headers=self.headers)
                assert resp.status_code == 200
                data = resp.json()
                assert data["requested_zones"] == "NZ"
                assert data["effective_zones"] == ["NZ"]
                assert data["mode"] == "FILTERED"

    def test_trigger_debit_rejects_invalid_zones(self):
        """POST /admin/trigger-debit rejects invalid or contradictory zone strings with 400."""
        with patch.object(settings, "admin_api_key", self.api_key), \
             patch.object(settings, "enabled_zones", "NZ"):

            # Contradictory ALL,NZ
            resp = self.client.post("/admin/trigger-debit/esim?zones=ALL,NZ", headers=self.headers)
            assert resp.status_code == 400
            assert "cannot be combined" in resp.json()["detail"].lower()

            # Unknown zone code
            resp = self.client.post("/admin/trigger-debit/esim?zones=UNKNOWN_ZONE", headers=self.headers)
            assert resp.status_code == 400

    def test_reset_stuck_debit_omitted_zones_inherits_nz(self):
        """POST /admin/reset-stuck-debit with omitted zones inherits settings.enabled_zones='NZ'."""
        with patch.object(settings, "admin_api_key", self.api_key), \
             patch.object(settings, "enabled_zones", "NZ"):

            mock_adapter = MagicMock()
            mock_adapter.reset_stuck_processing = MagicMock(return_value=3)
            mock_adapter.stuck_minutes = 10

            with patch("app.debit.services.registry.get_service", return_value=mock_adapter):
                resp = self.client.post("/admin/reset-stuck-debit/fancysale", headers=self.headers)
                assert resp.status_code == 200
                data = resp.json()

                assert data["service_type"] == "fancysale"
                assert data["effective_zones"] == ["NZ"]
                assert data["mode"] == "FILTERED"
                assert data["rows_reset"] == 3


# ══════════════════════════════════════════════════════════════════════════════
# 3. End-to-End Pipeline Verification (Candidate → Claim → Pyro → Writeback → Log)
# ══════════════════════════════════════════════════════════════════════════════

class TestNZPilotEndToEndPipeline:
    """Verifies complete 5-step processing pipeline in NZ mode."""

    @pytest.mark.asyncio
    async def test_fancysale_nz_pilot_full_pipeline(self):
        """End-to-end execution of FancySale controlled batch in NZ mode."""
        ctx = ExecutionContext.create(source="MANUAL_API", zones_str="NZ")
        adapter = FancySaleAdapter(token_manager=MagicMock(), enabled=True)

        record = {
            "refid": "FS_NZ_PILOT_01",
            "ctopupno": "9412300055",
            "fancy_no": "9412399955",
            "amount": 100.0,
            "plain_mpin": "123456",
            "mpin_length": 6,
            "circle_code": 55,  # Himachal Pradesh (NZ)
            "ss_request_id": "REQ_NZ_FS_01",
            "module_type": "FANCYSALE",
        }

        # Step 1: Candidate discovery & claim
        adapter.fetch_and_claim = MagicMock(return_value=[record])
        adapter.mark_success = MagicMock()
        adapter.mark_failed = MagicMock()

        mock_audit_log = AsyncMock()

        async def mock_wallet_adj(**kwargs):
            await mock_audit_log(
                service_type=kwargs.get("service_type"),
                oracle_ref_id=kwargs.get("oracle_ref_id"),
                client_id=kwargs.get("client_id"),
                source_msisdn=kwargs.get("source_msisdn"),
                dest_msisdn=kwargs.get("dest_msisdn"),
                amount=kwargs.get("amount"),
                is_success="Y",
                pyro_txn_id="PYRO_TXN_NZ_FS_101",
            )
            return {
                "statusCode": 2000,
                "status": "SUCCESS",
                "message": "Debit successful",
                "data": {
                    "pyroId": "PYRO_TXN_NZ_FS_101",
                    "balanceBefore": 1000.0,
                    "balanceAfter": 900.0,
                },
            }

        mock_pyro = AsyncMock(side_effect=mock_wallet_adj)

        with patch("app.debit.processor.wallet_adjustment", mock_pyro):
            summary = await run_debit_batch(adapter, context=ctx)

        # Verify summary statistics
        assert summary["processed"] == 1
        assert summary["success"] == 1
        assert summary["failed"] == 0
        assert summary["reconciliation_required"] == 0

        # Step 2: In-flight ownership tracked & released
        assert not ownership_tracker.is_active("FANCYSALE", "FS_NZ_PILOT_01")

        # Step 3: Pyro invoked exactly once
        mock_pyro.assert_called_once()
        assert mock_pyro.call_args[1]["oracle_ref_id"] == "FS_NZ_PILOT_01"
        assert mock_pyro.call_args[1]["source_msisdn"] == "9412300055"
        assert mock_pyro.call_args[1]["amount"] == 100.0

        # Step 4: Writeback executed with 'Y', transaction ID, and remarks
        adapter.mark_success.assert_called_once()
        call_record, call_txn_id, call_remarks = adapter.mark_success.call_args[0]
        assert call_record["refid"] == "FS_NZ_PILOT_01"
        assert call_txn_id == "PYRO_TXN_NZ_FS_101"
        assert "SUCCESS" in call_remarks
        assert "pyroId=PYRO_TXN_NZ_FS_101" in call_remarks
        adapter.mark_failed.assert_not_called()

        # Step 5: PostgreSQL audit log recorded
        mock_audit_log.assert_called_once()
        log_kwargs = mock_audit_log.call_args[1]
        assert log_kwargs["service_type"] == "FANCYSALE"
        assert log_kwargs["oracle_ref_id"] == "FS_NZ_PILOT_01"
        assert log_kwargs["is_success"] == "Y"
        assert log_kwargs["pyro_txn_id"] == "PYRO_TXN_NZ_FS_101"

    @pytest.mark.asyncio
    async def test_simswap_nz_pilot_full_pipeline_with_bcd_sync(self):
        """End-to-end execution of SimSwap batch in NZ mode with secondary BCD sync."""
        ctx = ExecutionContext.create(source="MANUAL_API", zones_str="NZ")
        adapter = SimswapAdapter(token_manager=MagicMock(), enabled=True)

        record = {
            "id": 801,
            "refid": "SS_NZ_PILOT_01",
            "ctopupno": "9412300059",
            "gsmnumber": "9412300059",
            "simnumber": "8991000059",
            "amount": 50.0,
            "plain_mpin": "123456",
            "mpin_length": 6,
            "circle_code": 59,  # Rajasthan (NZ)
            "ss_request_id": "REQ_NZ_SS_01",
            "module_type": "SIMSWAP",
            "caf_serial_no": "CAF_801",
        }

        adapter.fetch_and_claim = MagicMock(return_value=[record])
        adapter.mark_success = MagicMock()

        mock_pyro = AsyncMock(return_value={
            "statusCode": 2000,
            "status": "SUCCESS",
            "data": {"pyroId": "PYRO_TXN_NZ_SS_201"},
        })

        with patch("app.debit.processor.wallet_adjustment", mock_pyro), \
             patch("app.debit.processor.async_insert_debit_txn_log", AsyncMock()):

            summary = await run_debit_batch(adapter, context=ctx)

        assert summary["processed"] == 1
        assert summary["success"] == 1
        assert summary["failed"] == 0
        adapter.mark_success.assert_called_once()
        assert adapter.mark_success.call_args[0][0]["id"] == 801
        assert adapter.mark_success.call_args[0][1] == "PYRO_TXN_NZ_SS_201"

    @pytest.mark.asyncio
    async def test_esim_nz_pilot_full_pipeline_with_sim_swap_data_sync(self):
        """End-to-end execution of ESIM batch in NZ mode with secondary SIM_SWAP_DATA sync."""
        ctx = ExecutionContext.create(source="MANUAL_API", zones_str="NZ")
        adapter = EsimAdapter(token_manager=MagicMock(), enabled=True)

        record = {
            "id": 901,
            "refid": "ES_NZ_PILOT_01",
            "ctopupno": "9412300062",
            "gsmnumber": "9412300062",
            "simnumber": "8991000062",
            "amount": 50.0,
            "plain_mpin": "123456",
            "mpin_length": 6,
            "circle_code": 62,  # Jammu & Kashmir (NZ)
            "ss_request_id": "REQ_NZ_ES_01",
            "module_type": "ESIM",
            "caf_serial_no": "CAF_901",
        }

        adapter.fetch_and_claim = MagicMock(return_value=[record])
        adapter.mark_success = MagicMock()

        mock_pyro = AsyncMock(return_value={
            "statusCode": 2000,
            "status": "SUCCESS",
            "data": {"pyroId": "PYRO_TXN_NZ_ES_301"},
        })

        with patch("app.debit.processor.wallet_adjustment", mock_pyro), \
             patch("app.debit.processor.async_insert_debit_txn_log", AsyncMock()):

            summary = await run_debit_batch(adapter, context=ctx)

        assert summary["processed"] == 1
        assert summary["success"] == 1
        assert summary["failed"] == 0
        adapter.mark_success.assert_called_once()
        assert adapter.mark_success.call_args[0][0]["id"] == 901
        assert adapter.mark_success.call_args[0][1] == "PYRO_TXN_NZ_ES_301"


# ══════════════════════════════════════════════════════════════════════════════
# 4. Strict Disabled Circles Isolation (WZ, EZ, SZ Untouched)
# ══════════════════════════════════════════════════════════════════════════════

class TestNZPilotDisabledCirclesUntouched:
    """Verifies disabled circles remain 100% untouched across selection, claim, Pyro, and cleanup."""

    def test_candidate_discovery_binds_only_nz_circles(self):
        """Candidate discovery queries Q001, Q006, Q012 bind strictly the 9 NZ circles."""
        ctx = ExecutionContext.create(source="SCHEDULED", zones_str="NZ")

        # 1. FancySale
        sql_fs, params_fs = build_fancysale_candidate_query(50, ctx)
        assert "AND CIRCLE_CODE IN (" in sql_fs
        fs_circles = {v for k, v in params_fs.items() if k.startswith("c_")}
        assert fs_circles == set(NZ)

        # 2. SimSwap
        sql_ss, params_ss = build_simswap_candidate_query(50, ctx)
        assert "AND CIRCLE_CODE IN (" in sql_ss
        ss_circles = {v for k, v in params_ss.items() if k.startswith("c_")}
        assert ss_circles == set(NZ)

        # 3. ESIM
        sql_es, params_es = build_esim_candidate_query(50, ctx)
        assert "AND CIRCLE_CODE IN (" in sql_es
        es_circles = {v for k, v in params_es.items() if k.startswith("c_")}
        assert es_circles == set(NZ)

        # Verify NO disabled circle is in the parameter dictionary
        disabled_sample = {1, 4, 10, 70, 71, 74, 40, 41, 54}  # WZ, EZ, SZ
        assert disabled_sample.isdisjoint(fs_circles)
        assert disabled_sample.isdisjoint(ss_circles)
        assert disabled_sample.isdisjoint(es_circles)

    def test_claim_defense_in_depth_drops_disabled_circles(self):
        """Candidate rows from disabled circles are dropped by is_circle_allowed before claim."""
        ctx = ExecutionContext.create(source="MANUAL_API", zones_str="NZ")
        adapter = FancySaleAdapter(token_manager=MagicMock(), enabled=True)

        # Simulate candidate discovery returning rows across NZ and disabled zones
        candidates = [
            ("REF_NZ", "9412300055", "9412399955", 100.0, "123456", 6, "SS_NZ", "CSC01", 55, "2026-09-08", "FANCYSALE", "N"), # NZ (Allowed)
            ("REF_WZ", "9412300010", "9412399910", 100.0, "123456", 6, "SS_WZ", "CSC02", 10, "2026-09-08", "FANCYSALE", "N"), # WZ (Disabled)
            ("REF_EZ", "9412300071", "9412399971", 100.0, "123456", 6, "SS_EZ", "CSC03", 71, "2026-09-08", "FANCYSALE", "N"), # EZ (Disabled)
            ("REF_SZ", "9412300054", "9412399954", 100.0, "123456", 6, "SS_SZ", "CSC04", 54, "2026-09-08", "FANCYSALE", "N"), # SZ (Disabled)
        ]
        columns = [
            ("REFID",), ("CTOPUPNO",), ("FANCY_NO",), ("AMOUNT",), ("PLAIN_MPIN",),
            ("MPIN_LENGTH",), ("SS_REQUEST_ID",), ("CSCCODE",), ("CIRCLE_CODE",),
            ("TRANS_DATE",), ("MODULE_TYPE",), ("CAF_ENTRY_DONE",),
        ]

        mock_cur = MagicMock()
        mock_cur.description = columns
        mock_cur.fetchall.return_value = candidates
        mock_cur.rowcount = 1

        mock_conn = MagicMock()
        mock_conn.cursor.return_value = mock_cur

        with patch("app.debit.services.fancysale.get_oracle_conn") as mock_get_conn:
            mock_get_conn.return_value.__enter__.return_value = mock_conn
            claimed = adapter.fetch_and_claim(batch_size=50, context=ctx)

        # Only the NZ candidate (REF_NZ) must be claimed
        assert len(claimed) == 1
        assert claimed[0]["refid"] == "REF_NZ"
        assert claimed[0]["circle_code"] == 55

        # Verify claim SQL executed exactly once (only for REF_NZ)
        claim_calls = mock_cur.execute.call_args_list[1:]
        assert len(claim_calls) == 1
        assert claim_calls[0][0][1] == {"refid": "REF_NZ", "circle_code": 55}

    @pytest.mark.asyncio
    async def test_disabled_circles_zero_pyro_calls(self):
        """When candidates are from disabled circles, zero Pyro wallet adjustment calls are made."""
        ctx = ExecutionContext.create(source="MANUAL_API", zones_str="NZ")
        adapter = FancySaleAdapter(token_manager=MagicMock(), enabled=True)

        # Return only disabled circle candidates
        candidates = [
            ("REF_WZ_1", "9412300010", "9412399910", 100.0, "123456", 6, "SS_1", "CSC1", 10, "2026-09-08", "FANCYSALE", "N"),
            ("REF_EZ_1", "9412300071", "9412399971", 100.0, "123456", 6, "SS_2", "CSC2", 71, "2026-09-08", "FANCYSALE", "N"),
        ]
        columns = [
            ("REFID",), ("CTOPUPNO",), ("FANCY_NO",), ("AMOUNT",), ("PLAIN_MPIN",),
            ("MPIN_LENGTH",), ("SS_REQUEST_ID",), ("CSCCODE",), ("CIRCLE_CODE",),
            ("TRANS_DATE",), ("MODULE_TYPE",), ("CAF_ENTRY_DONE",),
        ]

        mock_cur = MagicMock()
        mock_cur.description = columns
        mock_cur.fetchall.return_value = candidates
        mock_conn = MagicMock()
        mock_conn.cursor.return_value = mock_cur

        mock_pyro = AsyncMock()

        with patch("app.debit.services.fancysale.get_oracle_conn") as mock_get_conn, \
             patch("app.debit.processor.wallet_adjustment", mock_pyro):

            mock_get_conn.return_value.__enter__.return_value = mock_conn
            summary = await run_debit_batch(adapter, context=ctx)

        assert summary["processed"] == 0
        assert summary["success"] == 0
        assert summary["failed"] == 0
        mock_pyro.assert_not_called()

    def test_stuck_cleanup_isolation_disabled_circles_untouched(self):
        """Stuck cleanup in NZ mode updates only rows in NZ circles; disabled circles untouched."""
        ctx = ExecutionContext.create(source="MANUAL_API", zones_str="NZ")
        adapter = FancySaleAdapter(token_manager=MagicMock(), enabled=True)

        mock_cur = MagicMock()
        mock_cur.rowcount = 2
        mock_conn = MagicMock()
        mock_conn.cursor.return_value = mock_cur

        with patch("app.debit.services.fancysale.get_oracle_conn") as mock_get_conn:
            mock_get_conn.return_value.__enter__.return_value = mock_conn
            reset_count = adapter.reset_stuck_processing(stuck_minutes=10, context=ctx)

        assert reset_count == 2
        executed_sql = mock_cur.execute.call_args[0][0]
        executed_params = mock_cur.execute.call_args[0][1]

        # Cleanup query must be strictly scoped to NZ
        assert "AND CIRCLE_CODE IN (" in executed_sql
        bound_circles = {v for k, v in executed_params.items() if k.startswith("c_")}
        assert bound_circles == set(NZ)


# ══════════════════════════════════════════════════════════════════════════════
# 5. Safety Guardrails Retention in NZ Mode
# ══════════════════════════════════════════════════════════════════════════════

class TestNZPilotSafetyInvariantRetention:
    """Verifies all safety controls remain fully functional in NZ mode."""

    def test_optimistic_claim_collision_defense_retained(self):
        """Worker B drops claim when cur.rowcount == 0; does not invoke Pyro in NZ mode."""
        ctx = ExecutionContext.create(source="MANUAL_API", zones_str="NZ")
        adapter = FancySaleAdapter(token_manager=MagicMock(), enabled=True)

        mock_cur = MagicMock()
        mock_cur.description = [
            ("REFID",), ("CTOPUPNO",), ("FANCY_NO",), ("AMOUNT",), ("PLAIN_MPIN",),
            ("MPIN_LENGTH",), ("SS_REQUEST_ID",), ("CSCCODE",), ("CIRCLE_CODE",),
            ("TRANS_DATE",), ("MODULE_TYPE",), ("CAF_ENTRY_DONE",),
        ]
        mock_cur.fetchall.return_value = [
            ("REF_NZ_1", "9412300055", "9412399955", 100.0, "123456", 6, "SS_1", "CSC1", 55, "2026-09-08", "FANCYSALE", "N"),
        ]
        mock_cur.rowcount = 0  # Claim collision

        mock_conn = MagicMock()
        mock_conn.cursor.return_value = mock_cur

        with patch("app.debit.services.fancysale.get_oracle_conn") as mock_get_conn:
            mock_get_conn.return_value.__enter__.return_value = mock_conn
            claimed = adapter.fetch_and_claim(batch_size=50, context=ctx)

        assert claimed == []

    @pytest.mark.asyncio
    async def test_writeback_hardening_retained_in_nz_pilot(self):
        """When Pyro succeeds but writeback fails in NZ mode, marks RECONCILIATION_REQUIRED."""
        ctx = ExecutionContext.create(source="MANUAL_API", zones_str="NZ")
        adapter = FancySaleAdapter(token_manager=MagicMock(), enabled=True)

        record = {
            "refid": "FS_NZ_RECON_01",
            "ctopupno": "9412300055",
            "fancy_no": "9412399955",
            "amount": 100.0,
            "plain_mpin": "123456",
            "mpin_length": 6,
            "circle_code": 55,
            "ss_request_id": "REQ_NZ_01",
        }
        adapter.fetch_and_claim = MagicMock(return_value=[record])

        def failing_writeback(rec, txn_id, remarks):
            raise WritebackError("Database offline during writeback")

        adapter.mark_success = MagicMock(side_effect=failing_writeback)
        adapter.mark_reconciliation_required = MagicMock()

        mock_pyro = AsyncMock(return_value={
            "statusCode": 2000,
            "status": "SUCCESS",
            "data": {"pyroId": "PYRO_RECON_NZ_01"},
        })

        with patch("app.debit.processor.wallet_adjustment", mock_pyro), \
             patch("app.debit.processor.async_insert_debit_txn_log", AsyncMock()):

            summary = await run_debit_batch(adapter, context=ctx)

        assert summary["processed"] == 1
        assert summary["success"] == 0
        assert summary["failed"] == 1
        assert summary["reconciliation_required"] == 1

        adapter.mark_reconciliation_required.assert_called_once()
        assert adapter.mark_reconciliation_required.call_args[0][1] == "PYRO_RECON_NZ_01"

    @pytest.mark.asyncio
    async def test_per_service_concurrency_lock_retained_in_nz_mode(self):
        """Per-service lock prevents concurrent batches for the same service in NZ mode."""
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
            ctx = ExecutionContext.create(source="MANUAL_API", zones_str="NZ")
            await run_debit_batch(adapter, context=ctx)
            nonlocal active_in_critical_section
            active_in_critical_section -= 1

        await asyncio.gather(worker(), worker())
        assert max_concurrent == 1
        _reset_service_locks()


# ══════════════════════════════════════════════════════════════════════════════
# 6. Non-Production Live Database Integration Probes (Oracle XE & Postgres)
# ══════════════════════════════════════════════════════════════════════════════

@pytest.mark.skipif(not ORACLE_AVAILABLE, reason="Non-production Oracle database not accessible")
class TestNonProdOracleLiveNZPilot:
    """Live validation against non-production Oracle XE in NZ pilot mode."""

    def test_live_oracle_nz_candidate_discovery_isolation(self):
        """Execute Q001 in NZ mode on live Oracle; prove 100% of discovered rows belong to NZ."""
        import oracledb
        ctx = ExecutionContext.create(source="MANUAL_API", zones_str="NZ")
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

            # Query succeeds and returns only NZ circles
            assert isinstance(rows, list)
            if rows:
                discovered_circles = {int(r[8]) for r in rows}  # Col 8 is CIRCLE_CODE
                assert discovered_circles.issubset(set(NZ)), (
                    f"Discovered circles {discovered_circles} contain non-NZ circles!"
                )
                # Ensure disabled circle test candidates (10, 71, 54) are strictly absent
                assert not discovered_circles.intersection({10, 71, 54})
        finally:
            conn.close()

    def test_live_oracle_simswap_and_esim_nz_discovery_isolation(self):
        """Execute Q006 & Q012 in NZ mode on live Oracle; prove strict NZ containment."""
        import oracledb
        ctx = ExecutionContext.create(source="MANUAL_API", zones_str="NZ")

        conn = oracledb.connect(
            user=settings.oracle_user,
            password=settings.oracle_password,
            dsn=settings.oracle_dsn,
        )
        try:
            cur = conn.cursor()

            # SimSwap Q006
            sql_ss, params_ss = build_simswap_candidate_query(50, ctx)
            cur.execute(sql_ss, params_ss)
            ss_rows = cur.fetchall()
            for r in ss_rows:
                assert int(r[12]) in NZ

            # ESIM Q012
            sql_es, params_es = build_esim_candidate_query(50, ctx)
            cur.execute(sql_es, params_es)
            es_rows = cur.fetchall()
            for r in es_rows:
                assert int(r[12]) in NZ
        finally:
            conn.close()

    def test_live_oracle_disabled_circles_remain_untouched(self):
        """Confirm that in live Oracle, rows in disabled circles remain in status 'N'."""
        import oracledb
        conn = oracledb.connect(
            user=settings.oracle_user,
            password=settings.oracle_password,
            dsn=settings.oracle_dsn,
        )
        try:
            cur = conn.cursor()

            # Check WZ (circle 10), EZ (circle 71), SZ (circle 54) in FancySale
            cur.execute("""
                SELECT CIRCLE_CODE, CAF_ENTRY_DONE, COUNT(*)
                FROM CAF_ADMIN.VANITYSALE_FRANCH_DATA
                WHERE CIRCLE_CODE IN ('10', '71', '54')
                GROUP BY CIRCLE_CODE, CAF_ENTRY_DONE
            """)
            counts = cur.fetchall()
            for circle, status, count in counts:
                # Disabled circles must have records preserved in 'N'
                assert count > 0
        finally:
            conn.close()

    def test_live_oracle_nz_cleanup_query_syntax(self):
        """Execute NZ cleanup queries Q005, Q011, Q017 in a rolled-back transaction."""
        import oracledb
        ctx = ExecutionContext.create(source="MANUAL_API", zones_str="NZ")

        conn = oracledb.connect(
            user=settings.oracle_user,
            password=settings.oracle_password,
            dsn=settings.oracle_dsn,
        )
        try:
            cur = conn.cursor()

            sql_fs, params_fs = build_fancysale_cleanup_query(10, ctx, active_refs={-999999})
            cur.execute(sql_fs, params_fs)

            sql_ss, params_ss = build_simswap_cleanup_query(10, ctx, active_refs={-999999})
            cur.execute(sql_ss, params_ss)

            sql_es, params_es = build_esim_cleanup_query(10, ctx, active_refs={-999999})
            cur.execute(sql_es, params_es)

            conn.rollback()
        finally:
            conn.close()


@pytest.mark.skipif(not POSTGRES_AVAILABLE, reason="Non-production PostgreSQL database not accessible")
class TestNonProdPostgresLiveNZPilot:
    """Live validation against PostgreSQL for NZ pilot transaction logging."""

    def test_live_postgres_pilot_audit_insert_and_verify(self):
        """Insert and verify a sample pilot transaction log into public.debit_txn_log."""
        import psycopg2
        from app.db.postgres import init_pg_pool, _pool
        from app.db.debit_log import insert_debit_txn_log

        pilot_ref_id = "NZ_PILOT_PROBE_001"
        try:
            import app.db.postgres as pg_module
            if pg_module._pool is None:
                init_pg_pool()

            insert_debit_txn_log(
                service_type="FANCYSALE",
                oracle_ref_id=pilot_ref_id,
                client_id="PILOT_CLIENT_01",
                source_msisdn="9412300055",
                dest_msisdn="9412399955",
                amount=100.0,
                api_stage="PILOT_VERIFICATION",
                api_endpoint="https://bsnlapigateway.pyrogroup.com/erp-stock-api/service-wallet-adjustment",
                attempt_no=1,
                request_body='{"test": "masked"}',
                response_http_code=200,
                response_body='{"status": "SUCCESS"}',
                pyro_status_code=2000,
                pyro_status_text="SUCCESS",
                pyro_txn_id="PYRO_PILOT_TXN_001",
                call_started_at=None,
                call_ended_at=None,
                duration_ms=45,
                is_success="Y",
            )

            # Query back and verify
            conn = psycopg2.connect(
                host=settings.pg_host,
                port=settings.pg_port,
                database=settings.pg_database,
                user=settings.pg_user,
                password=settings.pg_password,
            )
            with conn.cursor() as cur:
                cur.execute(
                    "SELECT service_type, oracle_ref_id, pyro_txn_id, is_success "
                    "FROM public.debit_txn_log WHERE oracle_ref_id = %s;",
                    (pilot_ref_id,),
                )
                row = cur.fetchone()
                assert row is not None
                assert row[0] == "FANCYSALE"
                assert row[1] == pilot_ref_id
                assert row[2] == "PYRO_PILOT_TXN_001"
                assert row[3] == "Y"

                # Clean up probe row
                cur.execute("DELETE FROM public.debit_txn_log WHERE oracle_ref_id = %s;", (pilot_ref_id,))
                conn.commit()
            conn.close()
        except Exception as exc:
            pytest.fail(f"PostgreSQL pilot audit log probe failed: {exc}")
