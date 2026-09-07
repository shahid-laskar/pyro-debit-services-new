"""Unit tests for Phase 9 — Cleanup Zone Isolation.

Verifies:
1. Q005 (FancySale cleanup), Q011 (SimSwap cleanup), and Q017 (ESIM cleanup) query builders:
   - Filtered mode: injects AND CIRCLE_CODE IN (:c_0, ...) with correct circle code bindings.
   - ALL mode: omits circle filtering to preserve nationwide behavior.
   - None context: defaults to no circle predicate.
   - Active refs exclusion: injects REFID/ID NOT IN (:act_0, ...) to prevent race conditions.
   - Reconciliation-required protection: retains guard against resetting failed writebacks.
   - Module type protection: SimSwap and ESIM retain strict MODULE_TYPE predicates.
2. Adapter reset_stuck_processing:
   - Inherits configured settings.enabled_zones when context is omitted (never silently expands to ALL).
   - Honors explicitly provided ExecutionContext.
3. Scheduler jobs:
   - _debit_job creates and passes ExecutionContext(source=SCHEDULED).
   - _stuck_cleanup_job creates and passes ExecutionContext(source=SCHEDULED).
4. Admin endpoints:
   - POST /admin/trigger-debit/{service_type}:
     - Inherits configured settings.enabled_zones when zones is omitted.
     - Accepts explicit zones query parameter.
     - Returns requested_zones, effective_zones, and configured_zones.
     - Returns HTTP 400 on invalid zone codes.
     - Requires admin API key.
   - POST /admin/reset-stuck-debit/{service_type}:
     - Inherits configured settings.enabled_zones when zones is omitted.
     - Accepts explicit zones query parameter.
     - Returns requested_zones, effective_zones, and configured_zones.
     - Returns HTTP 400 on invalid zone codes.
     - Requires admin API key.
"""

from unittest.mock import AsyncMock, MagicMock, patch
import pytest
from fastapi import FastAPI
from starlette.testclient import TestClient

from app.config import settings
from app.context import ExecutionContext, ExecutionSource
from app.debit.ownership import ownership_tracker
from app.debit.router import router
from app.debit.services.esim import EsimAdapter, build_esim_cleanup_query
from app.debit.services.fancysale import FancySaleAdapter, build_fancysale_cleanup_query
from app.debit.services.simswap import SimswapAdapter, build_simswap_cleanup_query
from app.scheduler import _debit_job, _stuck_cleanup_job
from app.zones import ZONE_MAP


# ══════════════════════════════════════════════════════════════════════════════
# 1. FancySale Q005 Query Builder Tests
# ══════════════════════════════════════════════════════════════════════════════

class TestFancySaleQ005CleanupQuery:
    """Validate Q005 stuck-record cleanup query construction."""

    def test_q005_filtered_single_zone(self):
        ctx = ExecutionContext.create(
            source=ExecutionSource.SCHEDULED,
            zones_str="NZ",
            service_type="fancysale",
        )
        sql, params = build_fancysale_cleanup_query(stuck_minutes=15, context=ctx)

        assert "UPDATE CAF_ADMIN.VANITYSALE_FRANCH_DATA" in sql
        assert "CAF_ENTRY_DONE = 'N'" in sql
        assert "CAF_ENTRY_DONE  = 'P'" in sql
        assert "CAF_ENTRY_DATE  < SYSDATE - (:stuck_minutes / 1440)" in sql
        assert "PYRO_REMARKS NOT LIKE 'RECONCILIATION_REQUIRED%'" in sql
        assert "PYRO_REMARKS IS NULL" in sql
        assert "AND CIRCLE_CODE IN (" in sql

        nz_circles = ZONE_MAP["NZ"]
        assert len(nz_circles) == 9
        for idx, code in enumerate(ctx.circle_codes):
            assert f":c_{idx}" in sql
            assert params[f"c_{idx}"] == code

        assert params["stuck_minutes"] == 15

    def test_q005_filtered_multi_zone(self):
        ctx = ExecutionContext.create(
            source=ExecutionSource.MANUAL_API,
            zones_str="NZ,WZ",
            service_type="fancysale",
        )
        sql, params = build_fancysale_cleanup_query(stuck_minutes=20, context=ctx)

        expected_circles = tuple(sorted(set(ZONE_MAP["NZ"] + ZONE_MAP["WZ"])))
        assert len(expected_circles) == 14
        assert ctx.circle_codes == expected_circles
        for idx, code in enumerate(expected_circles):
            assert f":c_{idx}" in sql
            assert params[f"c_{idx}"] == code

    def test_q005_all_mode_omits_circle_predicate(self):
        ctx = ExecutionContext.create(
            source=ExecutionSource.SCHEDULED,
            zones_str="ALL",
            service_type="fancysale",
        )
        sql, params = build_fancysale_cleanup_query(stuck_minutes=10, context=ctx)

        assert "CIRCLE_CODE IN" not in sql
        assert "c_0" not in params
        assert params == {"stuck_minutes": 10}

    def test_q005_none_context_omits_circle_predicate(self):
        sql, params = build_fancysale_cleanup_query(stuck_minutes=10, context=None)

        assert "CIRCLE_CODE IN" not in sql
        assert "c_0" not in params
        assert params == {"stuck_minutes": 10}

    def test_q005_active_refs_exclusion(self):
        ctx = ExecutionContext.create(
            source=ExecutionSource.SCHEDULED,
            zones_str="NZ",
            service_type="fancysale",
        )
        sql, params = build_fancysale_cleanup_query(
            stuck_minutes=10,
            context=ctx,
            active_refs={101, 102},
        )

        assert "REFID NOT IN (:act_0, :act_1)" in sql
        assert params["act_0"] == 101
        assert params["act_1"] == 102
        assert "CIRCLE_CODE IN" in sql


# ══════════════════════════════════════════════════════════════════════════════
# 2. SimSwap Q011 Query Builder Tests
# ══════════════════════════════════════════════════════════════════════════════

class TestSimSwapQ011CleanupQuery:
    """Validate Q011 stuck-record cleanup query construction."""

    def test_q011_filtered_single_zone(self):
        ctx = ExecutionContext.create(
            source=ExecutionSource.SCHEDULED,
            zones_str="NZ",
            service_type="simswap",
        )
        sql, params = build_simswap_cleanup_query(stuck_minutes=15, context=ctx)

        assert "UPDATE CAF_ADMIN.SIMSWAP_AMOUNT_DEDUCT_REQUESTS" in sql
        assert "AMOUNT_DEDUCT_FLAG    = 'N'" in sql
        assert "AMOUNT_DEDUCT_FLAG    = 'P'" in sql
        assert "MODULE_TYPE           = 'SIMSWAP'" in sql
        assert "AMOUNT_DEDUCT_REMARKS NOT LIKE 'RECONCILIATION_REQUIRED%'" in sql
        assert "AMOUNT_DEDUCT_REMARKS IS NULL" in sql
        assert "AND CIRCLE_CODE IN (" in sql

        nz_circles = ZONE_MAP["NZ"]
        for idx, code in enumerate(ctx.circle_codes):
            assert f":c_{idx}" in sql
            assert params[f"c_{idx}"] == code

        assert params["stuck_minutes"] == 15

    def test_q011_all_mode_omits_circle_predicate(self):
        ctx = ExecutionContext.create(
            source=ExecutionSource.SCHEDULED,
            zones_str="ALL",
            service_type="simswap",
        )
        sql, params = build_simswap_cleanup_query(stuck_minutes=10, context=ctx)

        assert "CIRCLE_CODE IN" not in sql
        assert "MODULE_TYPE           = 'SIMSWAP'" in sql
        assert params == {"stuck_minutes": 10}

    def test_q011_active_refs_exclusion(self):
        sql, params = build_simswap_cleanup_query(
            stuck_minutes=10,
            context=None,
            active_refs={501, 502},
        )

        assert "ID NOT IN (:act_0, :act_1)" in sql
        assert params["act_0"] == 501
        assert params["act_1"] == 502


# ══════════════════════════════════════════════════════════════════════════════
# 3. ESIM Q017 Query Builder Tests
# ══════════════════════════════════════════════════════════════════════════════

class TestEsimQ017CleanupQuery:
    """Validate Q017 stuck-record cleanup query construction."""

    def test_q017_filtered_single_zone(self):
        ctx = ExecutionContext.create(
            source=ExecutionSource.SCHEDULED,
            zones_str="NZ",
            service_type="esim",
        )
        sql, params = build_esim_cleanup_query(stuck_minutes=15, context=ctx)

        assert "UPDATE CAF_ADMIN.SIMSWAP_AMOUNT_DEDUCT_REQUESTS" in sql
        assert "AMOUNT_DEDUCT_FLAG    = 'N'" in sql
        assert "AMOUNT_DEDUCT_FLAG    = 'P'" in sql
        assert "MODULE_TYPE           = 'ESIM'" in sql
        assert "AMOUNT_DEDUCT_REMARKS NOT LIKE 'RECONCILIATION_REQUIRED%'" in sql
        assert "AMOUNT_DEDUCT_REMARKS IS NULL" in sql
        assert "AND CIRCLE_CODE IN (" in sql

        nz_circles = ZONE_MAP["NZ"]
        for idx, code in enumerate(ctx.circle_codes):
            assert f":c_{idx}" in sql
            assert params[f"c_{idx}"] == code

        assert params["stuck_minutes"] == 15

    def test_q017_all_mode_omits_circle_predicate(self):
        ctx = ExecutionContext.create(
            source=ExecutionSource.SCHEDULED,
            zones_str="ALL",
            service_type="esim",
        )
        sql, params = build_esim_cleanup_query(stuck_minutes=10, context=ctx)

        assert "CIRCLE_CODE IN" not in sql
        assert "MODULE_TYPE           = 'ESIM'" in sql
        assert params == {"stuck_minutes": 10}

    def test_q017_active_refs_exclusion(self):
        sql, params = build_esim_cleanup_query(
            stuck_minutes=10,
            context=None,
            active_refs={601, 602},
        )

        assert "ID NOT IN (:act_0, :act_1)" in sql
        assert params["act_0"] == 601
        assert params["act_1"] == 602


# ══════════════════════════════════════════════════════════════════════════════
# 4. Adapter reset_stuck_processing Zone Scoping Tests
# ══════════════════════════════════════════════════════════════════════════════

class TestAdapterResetStuckZoneScoping:
    """Validate that adapters inherit configured zones or respect explicit contexts."""

    def setup_method(self):
        ownership_tracker.clear()

    def teardown_method(self):
        ownership_tracker.clear()

    def test_fancysale_reset_inherits_enabled_zones_when_context_is_none(self):
        adapter = FancySaleAdapter(token_manager=MagicMock(), enabled=True)

        mock_cur = MagicMock()
        mock_cur.rowcount = 3
        mock_conn = MagicMock()
        mock_conn.cursor.return_value = mock_cur

        with patch.object(settings, "enabled_zones", "NZ"), \
             patch("app.debit.services.fancysale.get_oracle_conn") as mock_get_conn:
            mock_get_conn.return_value.__enter__.return_value = mock_conn

            count = adapter.reset_stuck_processing(stuck_minutes=15)
            assert count == 3

            sql_executed = mock_cur.execute.call_args[0][0]
            params_executed = mock_cur.execute.call_args[0][1]

            assert "AND CIRCLE_CODE IN (" in sql_executed
            assert params_executed["c_0"] == ZONE_MAP["NZ"][0]
            assert params_executed["stuck_minutes"] == 15

    def test_simswap_reset_uses_explicit_context(self):
        adapter = SimswapAdapter(token_manager=MagicMock(), enabled=True)

        mock_cur = MagicMock()
        mock_cur.rowcount = 1
        mock_conn = MagicMock()
        mock_conn.cursor.return_value = mock_cur

        ctx = ExecutionContext.create(
            source=ExecutionSource.MANUAL_API,
            zones_str="WZ",
            service_type="simswap",
        )

        with patch("app.debit.services.simswap.get_oracle_conn") as mock_get_conn:
            mock_get_conn.return_value.__enter__.return_value = mock_conn

            count = adapter.reset_stuck_processing(stuck_minutes=10, context=ctx)
            assert count == 1

            sql_executed = mock_cur.execute.call_args[0][0]
            params_executed = mock_cur.execute.call_args[0][1]

            assert "AND CIRCLE_CODE IN (" in sql_executed
            wz_circles = ZONE_MAP["WZ"]
            for idx, code in enumerate(wz_circles):
                assert params_executed[f"c_{idx}"] == code

    def test_esim_reset_uses_explicit_context(self):
        adapter = EsimAdapter(token_manager=MagicMock(), enabled=True)

        mock_cur = MagicMock()
        mock_cur.rowcount = 2
        mock_conn = MagicMock()
        mock_conn.cursor.return_value = mock_cur

        ctx = ExecutionContext.create(
            source=ExecutionSource.MANUAL_API,
            zones_str="EZ",
            service_type="esim",
        )

        with patch("app.debit.services.esim.get_oracle_conn") as mock_get_conn:
            mock_get_conn.return_value.__enter__.return_value = mock_conn

            count = adapter.reset_stuck_processing(stuck_minutes=12, context=ctx)
            assert count == 2

            sql_executed = mock_cur.execute.call_args[0][0]
            params_executed = mock_cur.execute.call_args[0][1]

            assert "AND CIRCLE_CODE IN (" in sql_executed
            ez_circles = ZONE_MAP["EZ"]
            for idx, code in enumerate(ez_circles):
                assert params_executed[f"c_{idx}"] == code


# ══════════════════════════════════════════════════════════════════════════════
# 5. Scheduler Context Propagation Tests
# ══════════════════════════════════════════════════════════════════════════════

class TestSchedulerContextPropagation:
    """Validate that scheduled debit and cleanup jobs pass proper ExecutionContext."""

    @pytest.mark.asyncio
    async def test_debit_job_passes_scheduled_context(self):
        mock_adapter = MagicMock()
        mock_adapter.enabled = True
        mock_adapter.service_type = "FANCYSALE"

        with patch("app.debit.services.registry.SERVICE_REGISTRY", {"FANCYSALE": mock_adapter}), \
             patch("app.debit.processor.run_debit_batch", new_callable=AsyncMock) as mock_run_batch, \
             patch.object(settings, "enabled_zones", "NZ"):

            mock_run_batch.return_value = {"processed": 0}
            await _debit_job("FANCYSALE")

            assert mock_run_batch.called
            call_kwargs = mock_run_batch.call_args[1]
            ctx: ExecutionContext = call_kwargs["context"]

            assert ctx.source == "SCHEDULED"
            assert ctx.zone_codes == ("NZ",)
            assert ctx.mode == "FILTERED"
            assert ctx.service_type == "fancysale"

    @pytest.mark.asyncio
    async def test_stuck_cleanup_job_passes_scheduled_context(self):
        mock_adapter = MagicMock()
        mock_adapter.service_type = "FANCYSALE"
        mock_adapter.stuck_minutes = 15
        mock_adapter.reset_stuck_processing.return_value = 2

        with patch("app.debit.services.registry.get_enabled_services", return_value=[mock_adapter]), \
             patch.object(settings, "enabled_zones", "NZ,WZ"):

            await _stuck_cleanup_job()

            assert mock_adapter.reset_stuck_processing.called
            call_kwargs = mock_adapter.reset_stuck_processing.call_args[1]
            ctx: ExecutionContext = call_kwargs["context"]

            assert ctx.source == "SCHEDULED"
            assert ctx.zone_codes == ("NZ", "WZ")
            assert ctx.mode == "FILTERED"
            assert ctx.service_type == "fancysale"


# ══════════════════════════════════════════════════════════════════════════════
# 6. Admin Endpoints Zone Scoping Tests
# ══════════════════════════════════════════════════════════════════════════════

class TestAdminEndpointsZoneScoping:
    """Validate /admin/trigger-debit and /admin/reset-stuck-debit endpoints."""

    def setup_method(self):
        self.app = FastAPI()
        self.app.include_router(router)
        self.client = TestClient(self.app)
        self.api_key = "test-secret-admin-key"
        self.headers = {"X-Admin-API-Key": self.api_key}

    def test_auth_failure_when_missing_api_key(self):
        with patch.object(settings, "admin_api_key", self.api_key):
            resp = self.client.post("/admin/trigger-debit/FANCYSALE")
            assert resp.status_code == 403

            resp2 = self.client.post("/admin/reset-stuck-debit/FANCYSALE")
            assert resp2.status_code == 403

    def test_trigger_debit_inherits_enabled_zones_when_omitted(self):
        mock_adapter = MagicMock()
        mock_adapter.service_type = "FANCYSALE"

        with patch.object(settings, "admin_api_key", self.api_key), \
             patch.object(settings, "enabled_zones", "NZ"), \
             patch("app.debit.services.registry.get_service", return_value=mock_adapter), \
             patch("app.debit.processor.run_debit_batch", new_callable=AsyncMock) as mock_run:

            mock_run.return_value = {"processed": 5, "success": 5, "failed": 0}

            resp = self.client.post(
                "/admin/trigger-debit/FANCYSALE",
                headers=self.headers,
            )
            assert resp.status_code == 200
            data = resp.json()

            assert data["triggered"] is True
            assert data["requested_zones"] is None
            assert data["effective_zones"] == ["NZ"]
            assert data["configured_zones"] == "NZ"
            assert data["mode"] == "FILTERED"

            assert mock_run.called
            ctx: ExecutionContext = mock_run.call_args[1]["context"]
            assert ctx.source == "MANUAL_API"
            assert ctx.zone_codes == ("NZ",)

    def test_trigger_debit_explicit_zones_override(self):
        mock_adapter = MagicMock()
        mock_adapter.service_type = "FANCYSALE"

        with patch.object(settings, "admin_api_key", self.api_key), \
             patch.object(settings, "enabled_zones", "NZ"), \
             patch("app.debit.services.registry.get_service", return_value=mock_adapter), \
             patch("app.debit.processor.run_debit_batch", new_callable=AsyncMock) as mock_run:

            mock_run.return_value = {"processed": 2, "success": 2, "failed": 0}

            resp = self.client.post(
                "/admin/trigger-debit/FANCYSALE?zones=WZ",
                headers=self.headers,
            )
            assert resp.status_code == 200
            data = resp.json()

            assert data["requested_zones"] == "WZ"
            assert data["effective_zones"] == ["WZ"]
            assert data["configured_zones"] == "NZ"
            assert data["mode"] == "FILTERED"

            ctx: ExecutionContext = mock_run.call_args[1]["context"]
            assert ctx.zone_codes == ("WZ",)

    def test_trigger_debit_invalid_zone_returns_400(self):
        mock_adapter = MagicMock()
        mock_adapter.service_type = "FANCYSALE"

        with patch.object(settings, "admin_api_key", self.api_key), \
             patch("app.debit.services.registry.get_service", return_value=mock_adapter):

            resp = self.client.post(
                "/admin/trigger-debit/FANCYSALE?zones=INVALID_ZONE",
                headers=self.headers,
            )
            assert resp.status_code == 400
            assert "Invalid zone code" in resp.json()["detail"]

    def test_trigger_debit_unknown_service_returns_404(self):
        with patch.object(settings, "admin_api_key", self.api_key):
            resp = self.client.post(
                "/admin/trigger-debit/UNKNOWN_SERVICE",
                headers=self.headers,
            )
            assert resp.status_code == 404

    def test_reset_stuck_debit_inherits_enabled_zones_when_omitted(self):
        mock_adapter = MagicMock()
        mock_adapter.service_type = "FANCYSALE"
        mock_adapter.stuck_minutes = 10
        mock_adapter.reset_stuck_processing.return_value = 4

        with patch.object(settings, "admin_api_key", self.api_key), \
             patch.object(settings, "enabled_zones", "NZ"), \
             patch("app.debit.services.registry.get_service", return_value=mock_adapter):

            resp = self.client.post(
                "/admin/reset-stuck-debit/FANCYSALE",
                headers=self.headers,
            )
            assert resp.status_code == 200
            data = resp.json()

            assert data["service_type"] == "FANCYSALE"
            assert data["stuck_minutes_used"] == 10
            assert data["requested_zones"] is None
            assert data["effective_zones"] == ["NZ"]
            assert data["configured_zones"] == "NZ"
            assert data["mode"] == "FILTERED"
            assert data["rows_reset"] == 4

            assert mock_adapter.reset_stuck_processing.called
            ctx: ExecutionContext = mock_adapter.reset_stuck_processing.call_args[1]["context"]
            assert ctx.source == "MANUAL_API"
            assert ctx.zone_codes == ("NZ",)

    def test_reset_stuck_debit_explicit_zones_and_minutes(self):
        mock_adapter = MagicMock()
        mock_adapter.service_type = "SIMSWAP"
        mock_adapter.stuck_minutes = 10
        mock_adapter.reset_stuck_processing.return_value = 7

        with patch.object(settings, "admin_api_key", self.api_key), \
             patch.object(settings, "enabled_zones", "NZ"), \
             patch("app.debit.services.registry.get_service", return_value=mock_adapter):

            resp = self.client.post(
                "/admin/reset-stuck-debit/SIMSWAP?stuck_minutes=30&zones=EZ",
                headers=self.headers,
            )
            assert resp.status_code == 200
            data = resp.json()

            assert data["service_type"] == "SIMSWAP"
            assert data["stuck_minutes_used"] == 30
            assert data["requested_zones"] == "EZ"
            assert data["effective_zones"] == ["EZ"]
            assert data["configured_zones"] == "NZ"
            assert data["rows_reset"] == 7

            ctx: ExecutionContext = mock_adapter.reset_stuck_processing.call_args[1]["context"]
            assert ctx.zone_codes == ("EZ",)

    def test_reset_stuck_debit_invalid_zone_returns_400(self):
        mock_adapter = MagicMock()
        mock_adapter.service_type = "FANCYSALE"

        with patch.object(settings, "admin_api_key", self.api_key), \
             patch("app.debit.services.registry.get_service", return_value=mock_adapter):

            resp = self.client.post(
                "/admin/reset-stuck-debit/FANCYSALE?zones=FAKE",
                headers=self.headers,
            )
            assert resp.status_code == 400
            assert "Invalid zone code" in resp.json()["detail"]

    def test_reset_stuck_debit_unknown_service_returns_404(self):
        with patch.object(settings, "admin_api_key", self.api_key):
            resp = self.client.post(
                "/admin/reset-stuck-debit/NON_EXISTENT",
                headers=self.headers,
            )
            assert resp.status_code == 404
