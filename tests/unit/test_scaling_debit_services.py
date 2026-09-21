"""Unit tests for Phase Scaling & Performance Enhancements (Option B).

Verifies:
1. PyroAuthService dedicated persistent HTTP client creation and connection reuse.
2. Distinct, isolated HTTP clients between different service token managers (Option B).
3. Graceful lifecycle closing via PyroAuthService.close().
4. wallet_adjustment utilization of token_manager.get_http_client().
5. Scaling configuration defaults (100 batch size, 2 min interval, 15.0s timeout).
"""

from unittest.mock import AsyncMock, MagicMock, patch
import httpx
import pytest

from app.auth.token_manager import PyroAuthService
from app.config import Settings
from app.debit.pyro_client import wallet_adjustment


class TestScalingConfigurations:
    """Validate tuned performance defaults for 10k records in 12 hours."""

    def test_tuned_settings_defaults(self):
        s = Settings(
            pyro_base_url="http://127.0.0.1:9999",
            oracle_user="usr",
            oracle_password="pwd",
            oracle_dsn="dsn",
            pg_host="host",
            pg_database="db",
            pg_user="usr",
            pg_password="pwd",
            _env_file=None,
        )
        assert s.pyro_request_timeout_seconds == 15.0
        assert s.fancysale_batch_size == 100
        assert s.fancysale_interval_minutes == 2
        assert s.simswap_batch_size == 100
        assert s.simswap_interval_minutes == 2
        assert s.esim_batch_size == 100
        assert s.esim_interval_minutes == 2


class TestTokenManagerPersistentClient:
    """Validate Option B dedicated persistent HTTP client per token manager."""

    def test_get_http_client_returns_async_client(self):
        tm = PyroAuthService(
            api_key="key",
            login_id="user",
            password="pwd",
            secret_key="secret",
            base_url="http://127.0.0.1:9999",
            label="TEST",
        )
        client = tm.get_http_client()
        assert isinstance(client, httpx.AsyncClient)
        assert not client.is_closed

    def test_get_http_client_reuses_same_instance(self):
        tm = PyroAuthService(
            api_key="key",
            login_id="user",
            password="pwd",
            secret_key="secret",
            base_url="http://127.0.0.1:9999",
            label="TEST",
        )
        client1 = tm.get_http_client()
        client2 = tm.get_http_client()
        assert client1 is client2

    def test_different_services_have_distinct_clients(self):
        """Option B: each service token manager maintains its own separate connection pool."""
        tm_fancy = PyroAuthService(
            api_key="key1",
            login_id="user1",
            password="pwd",
            secret_key="secret",
            base_url="http://127.0.0.1:9999",
            label="FANCYSALE",
        )
        tm_sim = PyroAuthService(
            api_key="key2",
            login_id="user2",
            password="pwd",
            secret_key="secret",
            base_url="http://127.0.0.1:9999",
            label="SIMSWAP",
        )
        client_fancy = tm_fancy.get_http_client()
        client_sim = tm_sim.get_http_client()
        assert client_fancy is not client_sim

    @pytest.mark.asyncio
    async def test_close_shuts_down_client(self):
        tm = PyroAuthService(
            api_key="key",
            login_id="user",
            password="pwd",
            secret_key="secret",
            base_url="http://127.0.0.1:9999",
            label="TEST",
        )
        client = tm.get_http_client()
        assert not client.is_closed

        await tm.close()
        assert tm._client is None
        assert client.is_closed

        # Calling get_http_client again creates a new open client
        new_client = tm.get_http_client()
        assert not new_client.is_closed
        assert new_client is not client
        await tm.close()


class TestWalletAdjustmentClientReuse:
    """Validate wallet_adjustment utilizes token_manager.get_http_client()."""

    @pytest.mark.asyncio
    async def test_wallet_adjustment_calls_tm_http_client(self):
        mock_tm = MagicMock()
        mock_tm.api_key = "test_key"
        mock_tm.session_token = "sess_123"
        mock_tm.secret_key = "74163581669665286567"
        mock_tm.get_access_token = AsyncMock(return_value="acc_token_456")

        mock_client = AsyncMock()
        mock_resp = MagicMock()
        mock_resp.text = '{"statusCode": 200, "status": "SUCCESS", "message": "Debited", "data": {"pyroId": "P999"}}'
        mock_resp.json.return_value = {
            "statusCode": 200,
            "status": "SUCCESS",
            "message": "Debited",
            "data": {"pyroId": "P999"},
        }
        mock_resp.status_code = 200
        mock_client.post.return_value = mock_resp
        mock_tm.get_http_client.return_value = mock_client

        with patch("app.debit.pyro_client.async_insert_debit_txn_log", new_callable=AsyncMock):
            result = await wallet_adjustment(
                oracle_ref_id="REF100",
                service_type="FANCYSALE",
                client_id="SS_100",
                source_msisdn="9400000001",
                dest_msisdn="9400000002",
                amount=100.0,
                mpin="1234",
                remarks="FANCYSALE",
                token_manager=mock_tm,
            )

        assert mock_tm.get_http_client.called
        assert mock_client.post.called
        assert result.get("statusCode") == 200
        assert result.get("status") == "SUCCESS"
