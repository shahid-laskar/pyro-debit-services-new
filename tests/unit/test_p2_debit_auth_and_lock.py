"""P2 debit fixes.

#9  Token / auth failures (HTTP 401, 5000 'Invalid token', token unavailable) and
    'Pyro unreachable' release the record instead of marking it 'R'; one re-auth per
    batch, then the rest of the batch is released. get_access_token falls back to a
    full login and never returns a rejected token.
#11 A PostgreSQL advisory lock per service serialises debit batches and stuck cleanup
    across processes/replicas; busy or unreachable -> skip, never wait.
"""

from contextlib import contextmanager
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from app.auth.token_manager import PyroAuthService
from app.debit import dist_lock
from app.debit.processor import _is_auth_failure, run_debit_batch
from app.debit.services.esim import EsimAdapter
from app.debit.services.fancysale import FancySaleAdapter
from app.debit.services.simswap import SimswapAdapter


INVALID_TOKEN_401 = {"statusCode": 5000, "status": "FAILED", "message": "Invalid token",
                     "_request_sent": True, "_http_status": 401}
SUCCESS = {"statusCode": 2000, "status": "SUCCESS", "message": "Transaction successful",
           "data": {"pyroId": 89311635}, "_request_sent": True, "_http_status": 200}


# ══════════════════════════════════════════════════════════════════════════════
# #9 classification
# ══════════════════════════════════════════════════════════════════════════════

@pytest.mark.parametrize("response,expected", [
    (INVALID_TOKEN_401, True),
    ({"statusCode": 5000, "message": "Invalid access token", "_http_status": 200}, True),
    ({"statusCode": 5000, "message": "Invalid credintials", "_http_status": 200}, True),
    ({"statusCode": -1, "message": "Failed to obtain access token", "_request_sent": False}, True),
    ({"statusCode": 5000, "message": "Invalid mpin for source msisdn: 8903696189", "_http_status": 200}, False),
    ({"statusCode": 5000, "message": "Insufficient funds", "_http_status": 200}, False),
    ({"statusCode": -1, "message": "Connection failed: refused", "_request_sent": False}, False),
    (SUCCESS, False),
])
def test_is_auth_failure(response, expected):
    assert _is_auth_failure(response) is expected


# ══════════════════════════════════════════════════════════════════════════════
# #9 processor
# ══════════════════════════════════════════════════════════════════════════════

def _adapter(n, reauth_ok=True):
    adapter = MagicMock()
    adapter.service_type = "FANCYSALE"
    adapter.enabled = True
    adapter.batch_size = 10
    adapter.token_manager = MagicMock()
    adapter.token_manager.authenticate = AsyncMock(return_value=reauth_ok)
    adapter.fetch_and_claim.return_value = [{"refid": f"FS-{i}"} for i in range(n)]
    adapter.get_record_ref.side_effect = lambda r: r["refid"]
    adapter.map_to_pyro_params.return_value = {
        "client_id": "CLI", "source_msisdn": "1", "dest_msisdn": "2",
        "amount": 1.0, "mpin": "12345", "remarks": "FANCYSALE",
    }
    return adapter


@pytest.mark.asyncio
class TestAuthFailureHandling:

    async def test_token_rejected_releases_and_reauths_then_continues(self):
        adapter = _adapter(3)
        responses = [INVALID_TOKEN_401, SUCCESS, SUCCESS]
        with patch("app.debit.processor.wallet_adjustment", new_callable=AsyncMock,
                   side_effect=responses) as pyro:
            summary = await run_debit_batch(adapter)
        assert pyro.await_count == 3
        adapter.mark_failed.assert_not_called()
        adapter.release_claim.assert_called_once()
        assert adapter.release_claim.call_args.args[0] == {"refid": "FS-0"}
        adapter.token_manager.invalidate.assert_called_once()
        adapter.token_manager.authenticate.assert_awaited_once()
        assert summary["success"] == 2 and summary["deferred"] == 1 and summary["failed"] == 0

    async def test_reauth_failure_releases_rest_of_batch(self):
        adapter = _adapter(3, reauth_ok=False)
        with patch("app.debit.processor.wallet_adjustment", new_callable=AsyncMock,
                   return_value=INVALID_TOKEN_401) as pyro:
            summary = await run_debit_batch(adapter)
        assert pyro.await_count == 1
        assert adapter.release_claim.call_count == 3
        adapter.mark_failed.assert_not_called()
        assert summary["deferred"] == 3

    async def test_second_auth_failure_stops_batch(self):
        adapter = _adapter(4)
        with patch("app.debit.processor.wallet_adjustment", new_callable=AsyncMock,
                   side_effect=[INVALID_TOKEN_401, INVALID_TOKEN_401, SUCCESS, SUCCESS]) as pyro:
            summary = await run_debit_batch(adapter)
        assert pyro.await_count == 2
        adapter.token_manager.authenticate.assert_awaited_once()
        assert adapter.release_claim.call_count == 4
        assert summary["deferred"] == 4

    async def test_token_unavailable_is_released_not_rejected(self):
        adapter = _adapter(1)
        resp = {"statusCode": -1, "status": "ERROR", "message": "Failed to obtain access token",
                "_request_sent": False, "_http_status": None}
        with patch("app.debit.processor.wallet_adjustment", new_callable=AsyncMock, return_value=resp):
            await run_debit_batch(adapter)
        adapter.release_claim.assert_called_once()
        adapter.mark_failed.assert_not_called()

    async def test_pyro_unreachable_releases_rest_without_reauth(self):
        adapter = _adapter(3)
        resp = {"statusCode": -1, "status": "ERROR", "message": "Connection failed: refused",
                "_request_sent": False, "_http_status": None}
        with patch("app.debit.processor.wallet_adjustment", new_callable=AsyncMock,
                   return_value=resp) as pyro:
            summary = await run_debit_batch(adapter)
        assert pyro.await_count == 1
        assert adapter.release_claim.call_count == 3
        adapter.token_manager.authenticate.assert_not_called()
        adapter.mark_failed.assert_not_called()
        assert summary["deferred"] == 3

    async def test_business_5000_is_still_rejected(self):
        adapter = _adapter(1)
        resp = {"statusCode": 5000, "status": "FAILED", "message": "Insufficient funds",
                "_request_sent": True, "_http_status": 200}
        with patch("app.debit.processor.wallet_adjustment", new_callable=AsyncMock, return_value=resp):
            await run_debit_batch(adapter)
        adapter.mark_failed.assert_called_once()
        adapter.release_claim.assert_not_called()


@pytest.mark.parametrize("adapter_cls,module,key,module_guard", [
    (FancySaleAdapter, "fancysale", "refid", None),
    (SimswapAdapter, "simswap", "id", "SIMSWAP"),
    (EsimAdapter, "esim", "id", "SIMSWAP-ESIM"),
])
def test_release_claim_sql(adapter_cls, module, key, module_guard):
    cur = MagicMock()
    conn = MagicMock()
    conn.cursor.return_value = cur
    with patch(f"app.debit.services.{module}.get_oracle_conn") as get_conn:
        get_conn.return_value.__enter__.return_value = conn
        adapter_cls(token_manager=MagicMock(), enabled=True).release_claim({key: 7}, "x" * 3000)
    sql, params = cur.execute.call_args.args
    flat = " ".join(sql.split())
    assert "= 'N'" in flat and "= 'P'" in flat           # only P -> N
    if module_guard:
        assert f"MODULE_TYPE = '{module_guard}'" in flat
    assert params[key] == 7 and len(params["remarks"]) <= 2000
    conn.commit.assert_called_once()


# ══════════════════════════════════════════════════════════════════════════════
# #9 token manager
# ══════════════════════════════════════════════════════════════════════════════

@pytest.mark.asyncio
class TestGetAccessToken:

    def _tm(self):
        tm = PyroAuthService(api_key="k", login_id="l", password="p",
                             secret_key="74163581669665286567", base_url="https://x", label="T")
        tm.access_token = "stale"
        tm.session_token = "sess"
        tm._access_token_exp = 0          # expired
        return tm

    async def test_refresh_failure_falls_back_to_authenticate(self):
        tm = self._tm()

        async def fake_auth(client=None):
            tm.access_token, tm._access_token_exp = "fresh", 9e12
            return True
        with patch.object(tm, "refresh_access_token", AsyncMock(return_value=False)), \
             patch.object(tm, "authenticate", side_effect=fake_auth) as auth:
            assert await tm.get_access_token() == "fresh"
        auth.assert_called_once()

    async def test_never_returns_rejected_token(self):
        tm = self._tm()
        with patch.object(tm, "refresh_access_token", AsyncMock(return_value=False)), \
             patch.object(tm, "authenticate", AsyncMock(return_value=False)):
            assert await tm.get_access_token() is None

    async def test_invalidate_forces_full_login(self):
        tm = self._tm()
        tm.invalidate()
        assert tm.access_token is None and tm.session_token is None


# ══════════════════════════════════════════════════════════════════════════════
# #11 advisory lock
# ══════════════════════════════════════════════════════════════════════════════

def _fake_pool(acquired=True, fail=False):
    cur = MagicMock()
    if fail:
        cur.execute.side_effect = RuntimeError("pg down")
    cur.fetchone.return_value = (acquired,)
    conn = MagicMock()
    conn.cursor.return_value.__enter__.return_value = cur
    pool = MagicMock()
    pool.getconn.return_value = conn
    return pool, conn, cur


@contextmanager
def _pool_ready(pool):
    with patch("app.db.postgres._pool", pool), \
         patch("app.db.postgres.is_pg_pool_ready", return_value=True):
        yield


@pytest.mark.asyncio
class TestServiceLock:

    async def test_acquire_and_release(self):
        pool, conn, cur = _fake_pool(True)
        with _pool_ready(pool):
            async with dist_lock.service_lock("fancysale", "test") as got:
                assert got is True
                pool.putconn.assert_not_called()          # session held while locked
        sqls = [c.args[0] for c in cur.execute.call_args_list]
        assert "pg_try_advisory_lock" in sqls[0] and "pg_advisory_unlock" in sqls[-1]
        assert cur.execute.call_args_list[0].args[1] == ("pyro-debit:FANCYSALE",)
        pool.putconn.assert_called_once_with(conn)

    async def test_busy_lock_skips(self):
        pool, conn, _ = _fake_pool(False)
        with _pool_ready(pool):
            async with dist_lock.service_lock("FANCYSALE", "test") as got:
                assert got is False
        pool.putconn.assert_called_once_with(conn)

    async def test_db_error_fails_closed(self):
        pool, conn, _ = _fake_pool(fail=True)
        with _pool_ready(pool):
            async with dist_lock.service_lock("FANCYSALE", "test") as got:
                assert got is False
        pool.putconn.assert_called_once_with(conn, close=True)

    async def test_no_pool_is_not_enforced(self):
        with patch("app.db.postgres.is_pg_pool_ready", return_value=False):
            async with dist_lock.service_lock("FANCYSALE", "test") as got:
                assert got is True

    async def test_batch_skips_when_lock_held_elsewhere(self):
        pool, _, _ = _fake_pool(False)
        adapter = _adapter(2)
        with _pool_ready(pool), \
             patch("app.debit.processor.wallet_adjustment", new_callable=AsyncMock) as pyro:
            summary = await run_debit_batch(adapter)
        adapter.fetch_and_claim.assert_not_called()
        pyro.assert_not_called()
        assert summary["skipped"] == "lock held elsewhere"

    async def test_cleanup_skips_when_lock_held_elsewhere(self):
        pool, _, _ = _fake_pool(False)
        adapter = MagicMock()
        adapter.service_type = "FANCYSALE"
        with _pool_ready(pool):
            assert await dist_lock.locked_reset_stuck(adapter, 10) == 0
        adapter.reset_stuck_processing.assert_not_called()

    async def test_cleanup_runs_under_lock(self):
        pool, _, _ = _fake_pool(True)
        adapter = MagicMock()
        adapter.service_type = "FANCYSALE"
        adapter.reset_stuck_processing.return_value = 4
        with _pool_ready(pool):
            assert await dist_lock.locked_reset_stuck(adapter, 10, context="ctx") == 4
        adapter.reset_stuck_processing.assert_called_once_with(10, context="ctx")
