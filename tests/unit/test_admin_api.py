"""Unit tests for Phase 15 — Admin API Endpoints and Zone Scoping.

Covers:
1. GET /debit/status (Protected with admin authentication)
2. GET /admin/zones (Active zone configuration, circle resolution, and modes)
3. POST /admin/trigger-debit/{service_type} (Zone inheritance, overrides, configuration immutability)
4. POST /admin/reset-stuck-debit/{service_type} (Zone inheritance, overrides, configuration immutability)
5. Security enforcement (403 forbidden, 503 unconfigured key)
"""

from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi import FastAPI
from starlette.testclient import TestClient

from app.config import settings
from app.context import ExecutionContext
from app.debit.router import router
from app.zones import CIRCLE_METADATA, ZONE_MAP


class TestDebitStatusAuth:
    """Validate GET /debit/status security and responses."""

    def setup_method(self):
        self.app = FastAPI()
        self.app.include_router(router)
        self.client = TestClient(self.app)
        self.api_key = "test-secret-key-12345"
        self.headers = {"X-Admin-API-Key": self.api_key}

    def test_status_missing_api_key_returns_403(self):
        with patch.object(settings, "admin_api_key", self.api_key):
            resp = self.client.get("/debit/status")
            assert resp.status_code == 403
            assert resp.json()["detail"] == "Invalid admin API key"

    def test_status_wrong_api_key_returns_403(self):
        with patch.object(settings, "admin_api_key", self.api_key):
            resp = self.client.get("/debit/status", headers={"X-Admin-API-Key": "wrong-key"})
            assert resp.status_code == 403
            assert resp.json()["detail"] == "Invalid admin API key"

    def test_status_unconfigured_api_key_returns_503(self):
        with patch.object(settings, "admin_api_key", ""):
            resp = self.client.get("/debit/status", headers=self.headers)
            assert resp.status_code == 503
            assert "not configured" in resp.json()["detail"]

    def test_status_success_with_valid_key(self):
        with patch.object(settings, "admin_api_key", self.api_key):
            resp = self.client.get("/debit/status", headers=self.headers)
            assert resp.status_code == 200
            data = resp.json()
            assert "tokens" in data
            assert "services" in data
            assert isinstance(data["tokens"], list)
            assert isinstance(data["services"], list)


class TestAdminZonesEndpoint:
    """Validate GET /admin/zones active configuration inspection."""

    def setup_method(self):
        self.app = FastAPI()
        self.app.include_router(router)
        self.client = TestClient(self.app)
        self.api_key = "test-secret-key-12345"
        self.headers = {"X-Admin-API-Key": self.api_key}

    def test_admin_zones_missing_auth_returns_403(self):
        with patch.object(settings, "admin_api_key", self.api_key):
            resp = self.client.get("/admin/zones")
            assert resp.status_code == 403

    def test_admin_zones_single_zone_nz(self):
        with patch.object(settings, "admin_api_key", self.api_key), \
             patch.object(settings, "enabled_zones", "NZ"):

            resp = self.client.get("/admin/zones", headers=self.headers)
            assert resp.status_code == 200
            data = resp.json()

            assert data["configured_zones"] == "NZ"
            assert data["active_zone_codes"] == ["NZ"]
            assert data["mode"] == "FILTERED"
            assert data["active_circle_count"] == 9
            assert data["active_circles"] == [2, 55, 56, 59, 60, 61, 62, 64, 65]

    def test_admin_zones_multi_zone_nz_wz(self):
        with patch.object(settings, "admin_api_key", self.api_key), \
             patch.object(settings, "enabled_zones", "NZ,WZ"):

            resp = self.client.get("/admin/zones", headers=self.headers)
            assert resp.status_code == 200
            data = resp.json()

            assert data["configured_zones"] == "NZ,WZ"
            assert data["active_zone_codes"] == ["NZ", "WZ"]
            assert data["mode"] == "FILTERED"
            assert data["active_circle_count"] == 14
            expected_circles = sorted(set(ZONE_MAP["NZ"]) | set(ZONE_MAP["WZ"]))
            assert data["active_circles"] == expected_circles

    def test_admin_zones_all_mode(self):
        with patch.object(settings, "admin_api_key", self.api_key), \
             patch.object(settings, "enabled_zones", "ALL"):

            resp = self.client.get("/admin/zones", headers=self.headers)
            assert resp.status_code == 200
            data = resp.json()

            assert data["configured_zones"] == "ALL"
            assert data["active_zone_codes"] == ["ALL"]
            assert data["mode"] == "ALL"
            assert data["active_circle_count"] == len(CIRCLE_METADATA)
            assert data["active_circle_count"] == 31
            assert data["active_circles"] == sorted(CIRCLE_METADATA.keys())

    def test_admin_zones_invalid_configuration_returns_500(self):
        with patch.object(settings, "admin_api_key", self.api_key), \
             patch.object(settings, "enabled_zones", "INVALID_ZONE"):

            resp = self.client.get("/admin/zones", headers=self.headers)
            assert resp.status_code == 500
            assert "invalid" in resp.json()["detail"].lower()


class TestAdminTriggerDebitEndpoint:
    """Validate POST /admin/trigger-debit/{service_type}."""

    def setup_method(self):
        self.app = FastAPI()
        self.app.include_router(router)
        self.client = TestClient(self.app)
        self.api_key = "test-secret-key-12345"
        self.headers = {"X-Admin-API-Key": self.api_key}

    def test_trigger_debit_inherits_enabled_zones_and_preserves_config(self):
        mock_adapter = MagicMock()
        mock_adapter.service_type = "SIMSWAP"

        with patch.object(settings, "admin_api_key", self.api_key), \
             patch.object(settings, "enabled_zones", "NZ"), \
             patch("app.debit.services.registry.get_service", return_value=mock_adapter), \
             patch("app.debit.processor.run_debit_batch", new_callable=AsyncMock) as mock_run:

            mock_run.return_value = {"processed": 3, "success": 3, "failed": 0}

            resp = self.client.post("/admin/trigger-debit/SIMSWAP", headers=self.headers)
            assert resp.status_code == 200
            data = resp.json()

            assert data["triggered"] is True
            assert "execution_id" in data
            assert data["requested_zones"] is None
            assert data["effective_zones"] == ["NZ"]
            assert data["configured_zones"] == "NZ"
            assert data["mode"] == "FILTERED"
            assert settings.enabled_zones == "NZ"

            ctx: ExecutionContext = mock_run.call_args[1]["context"]
            assert ctx.source == "MANUAL_API"
            assert ctx.zone_codes == ("NZ",)
            assert ctx.circle_count == 9

    def test_trigger_debit_override_does_not_mutate_config(self):
        mock_adapter = MagicMock()
        mock_adapter.service_type = "ESIM"

        with patch.object(settings, "admin_api_key", self.api_key), \
             patch.object(settings, "enabled_zones", "NZ"), \
             patch("app.debit.services.registry.get_service", return_value=mock_adapter), \
             patch("app.debit.processor.run_debit_batch", new_callable=AsyncMock) as mock_run:

            mock_run.return_value = {"processed": 1, "success": 1, "failed": 0}

            resp = self.client.post(
                "/admin/trigger-debit/ESIM?zones=WZ",
                headers=self.headers,
            )
            assert resp.status_code == 200
            data = resp.json()

            assert data["requested_zones"] == "WZ"
            assert data["effective_zones"] == ["WZ"]
            assert data["configured_zones"] == "NZ"
            assert data["mode"] == "FILTERED"

            # Strict Invariant E verification: configuration must NOT be mutated
            assert settings.enabled_zones == "NZ"

            ctx: ExecutionContext = mock_run.call_args[1]["context"]
            assert ctx.zone_codes == ("WZ",)
            assert ctx.circle_count == 5

    def test_trigger_debit_invalid_zone_returns_400(self):
        mock_adapter = MagicMock()
        mock_adapter.service_type = "FANCYSALE"

        with patch.object(settings, "admin_api_key", self.api_key), \
             patch("app.debit.services.registry.get_service", return_value=mock_adapter):

            resp = self.client.post(
                "/admin/trigger-debit/FANCYSALE?zones=BAD_ZONE",
                headers=self.headers,
            )
            assert resp.status_code == 400
            assert "Invalid zone code" in resp.json()["detail"]

    def test_trigger_debit_unknown_service_returns_404(self):
        with patch.object(settings, "admin_api_key", self.api_key):
            resp = self.client.post(
                "/admin/trigger-debit/NONEXISTENT",
                headers=self.headers,
            )
            assert resp.status_code == 404


class TestAdminResetStuckDebitEndpoint:
    """Validate POST /admin/reset-stuck-debit/{service_type}."""

    def setup_method(self):
        self.app = FastAPI()
        self.app.include_router(router)
        self.client = TestClient(self.app)
        self.api_key = "test-secret-key-12345"
        self.headers = {"X-Admin-API-Key": self.api_key}

    def test_reset_stuck_inherits_enabled_zones_and_preserves_config(self):
        mock_adapter = MagicMock()
        mock_adapter.service_type = "FANCYSALE"
        mock_adapter.stuck_minutes = 15
        mock_adapter.reset_stuck_processing.return_value = 2

        with patch.object(settings, "admin_api_key", self.api_key), \
             patch.object(settings, "enabled_zones", "NZ"), \
             patch("app.debit.services.registry.get_service", return_value=mock_adapter):

            resp = self.client.post("/admin/reset-stuck-debit/FANCYSALE", headers=self.headers)
            assert resp.status_code == 200
            data = resp.json()

            assert data["service_type"] == "FANCYSALE"
            assert "execution_id" in data
            assert data["stuck_minutes_used"] == 15
            assert data["requested_zones"] is None
            assert data["effective_zones"] == ["NZ"]
            assert data["configured_zones"] == "NZ"
            assert data["mode"] == "FILTERED"
            assert data["rows_reset"] == 2

            # Strict Invariant E verification: configuration must NOT be mutated
            assert settings.enabled_zones == "NZ"

            ctx: ExecutionContext = mock_adapter.reset_stuck_processing.call_args[1]["context"]
            assert ctx.source == "MANUAL_API"
            assert ctx.zone_codes == ("NZ",)
            assert ctx.circle_count == 9

    def test_reset_stuck_override_does_not_mutate_config(self):
        mock_adapter = MagicMock()
        mock_adapter.service_type = "SIMSWAP"
        mock_adapter.stuck_minutes = 15
        mock_adapter.reset_stuck_processing.return_value = 6

        with patch.object(settings, "admin_api_key", self.api_key), \
             patch.object(settings, "enabled_zones", "NZ"), \
             patch("app.debit.services.registry.get_service", return_value=mock_adapter):

            resp = self.client.post(
                "/admin/reset-stuck-debit/SIMSWAP?stuck_minutes=5&zones=EZ",
                headers=self.headers,
            )
            assert resp.status_code == 200
            data = resp.json()

            assert data["service_type"] == "SIMSWAP"
            assert data["stuck_minutes_used"] == 5
            assert data["requested_zones"] == "EZ"
            assert data["effective_zones"] == ["EZ"]
            assert data["configured_zones"] == "NZ"
            assert data["mode"] == "FILTERED"
            assert data["rows_reset"] == 6

            # Strict Invariant E verification: configuration must NOT be mutated
            assert settings.enabled_zones == "NZ"

            ctx: ExecutionContext = mock_adapter.reset_stuck_processing.call_args[1]["context"]
            assert ctx.zone_codes == ("EZ",)
            assert ctx.circle_count == 11
