"""Unit tests for Phase 16 — Security Hardening.

Covers:
1. require_admin_api_key constant-time comparison via compare_digest (mitigates timing attacks).
2. Status code enforcement (403 forbidden on invalid/missing key, 503 on unconfigured key).
3. Sanitization of sensitive fields (MPIN, password, API key, JWT, encryption secrets) in debit txn logging.
4. Protection of GET /debit/status and all operational mutation/inspection endpoints.
"""

import json
from unittest.mock import patch

import pytest
from fastapi import FastAPI, HTTPException
from starlette.testclient import TestClient

from app.config import settings
from app.db.debit_log import _mask_debit_body
from app.debit.router import router
from app.security import require_admin_api_key


class TestRequireAdminApiKeySecurity:
    """Test require_admin_api_key security enforcement and compare_digest behavior."""

    @pytest.mark.asyncio
    async def test_missing_header_raises_403(self):
        with patch.object(settings, "admin_api_key", "secret-admin-pass"):
            with pytest.raises(HTTPException) as exc_info:
                await require_admin_api_key(x_admin_api_key=None)
            assert exc_info.value.status_code == 403
            assert exc_info.value.detail == "Invalid admin API key"

    @pytest.mark.asyncio
    async def test_empty_string_header_raises_403(self):
        with patch.object(settings, "admin_api_key", "secret-admin-pass"):
            with pytest.raises(HTTPException) as exc_info:
                await require_admin_api_key(x_admin_api_key="")
            assert exc_info.value.status_code == 403
            assert exc_info.value.detail == "Invalid admin API key"

    @pytest.mark.asyncio
    async def test_wrong_key_raises_403(self):
        with patch.object(settings, "admin_api_key", "secret-admin-pass"):
            with pytest.raises(HTTPException) as exc_info:
                await require_admin_api_key(x_admin_api_key="wrong-pass")
            assert exc_info.value.status_code == 403
            assert exc_info.value.detail == "Invalid admin API key"

    @pytest.mark.asyncio
    async def test_unconfigured_admin_key_raises_503(self):
        with patch.object(settings, "admin_api_key", ""):
            with pytest.raises(HTTPException) as exc_info:
                await require_admin_api_key(x_admin_api_key="any-pass")
            assert exc_info.value.status_code == 503
            assert "not configured" in exc_info.value.detail.lower()

    @pytest.mark.asyncio
    async def test_valid_key_succeeds(self):
        with patch.object(settings, "admin_api_key", "correct-admin-key"):
            # Should not raise
            await require_admin_api_key(x_admin_api_key="correct-admin-key")

    @pytest.mark.asyncio
    async def test_compare_digest_is_used(self):
        """Ensure secrets.compare_digest is called for constant-time comparison."""
        with patch.object(settings, "admin_api_key", "correct-key"), \
             patch("app.security.compare_digest", return_value=True) as mock_compare:
            await require_admin_api_key(x_admin_api_key="correct-key")
            mock_compare.assert_called_once_with("correct-key", "correct-key")


class TestSensitiveDataMasking:
    """Test sanitization of sensitive fields in audit logs."""

    def test_mask_debit_body_masks_mpin(self):
        payload = {"clientId": "1001", "mpin": "1234", "amount": 100.0}
        masked = json.loads(_mask_debit_body(payload))
        assert masked["mpin"] == "***"
        assert masked["clientId"] == "1001"
        assert masked["amount"] == 100.0

    def test_mask_debit_body_masks_passwords_and_keys(self):
        payload = {
            "serviceType": "fancysale",
            "password": "super-secret-pwd",
            "api_key": "raw-api-key-value",
            "jwt": "eyJhbGciOi...",
            "token": "bearer-token-val",
            "access_token": "token-12345",
            "session_token": "session-67890",
            "secret_key": "des-3des-key",
            "sourceMsisdn": "9412345678",
        }
        masked = json.loads(_mask_debit_body(payload))
        assert masked["password"] == "***"
        assert masked["api_key"] == "***"
        assert masked["jwt"] == "***"
        assert masked["token"] == "***"
        assert masked["access_token"] == "***"
        assert masked["session_token"] == "***"
        assert masked["secret_key"] == "***"
        assert masked["sourceMsisdn"] == "9412345678"
        assert masked["serviceType"] == "fancysale"

    def test_mask_debit_body_case_insensitive(self):
        payload = {"MPIN": "9876", "PassWord": "secret", "Amount": 50.0}
        masked = json.loads(_mask_debit_body(payload))
        assert masked["MPIN"] == "***"
        assert masked["PassWord"] == "***"
        assert masked["Amount"] == 50.0


class TestEndpointsAdminProtection:
    """Verify all admin operational endpoints enforce require_admin_api_key."""

    def setup_method(self):
        self.app = FastAPI()
        self.app.include_router(router)
        self.client = TestClient(self.app)
        self.api_key = "secure-admin-pass-xyz"

    def test_debit_status_requires_admin_key(self):
        with patch.object(settings, "admin_api_key", self.api_key):
            resp_no_auth = self.client.get("/debit/status")
            assert resp_no_auth.status_code == 403

            resp_wrong = self.client.get("/debit/status", headers={"X-Admin-API-Key": "wrong"})
            assert resp_wrong.status_code == 403

            resp_ok = self.client.get("/debit/status", headers={"X-Admin-API-Key": self.api_key})
            assert resp_ok.status_code == 200

    def test_admin_zones_requires_admin_key(self):
        with patch.object(settings, "admin_api_key", self.api_key):
            resp_no_auth = self.client.get("/admin/zones")
            assert resp_no_auth.status_code == 403

            resp_ok = self.client.get("/admin/zones", headers={"X-Admin-API-Key": self.api_key})
            assert resp_ok.status_code == 200

    def test_trigger_debit_requires_admin_key(self):
        with patch.object(settings, "admin_api_key", self.api_key):
            resp_no_auth = self.client.post("/admin/trigger-debit/FANCYSALE")
            assert resp_no_auth.status_code == 403

    def test_reset_stuck_requires_admin_key(self):
        with patch.object(settings, "admin_api_key", self.api_key):
            resp_no_auth = self.client.post("/admin/reset-stuck-debit/FANCYSALE")
            assert resp_no_auth.status_code == 403
