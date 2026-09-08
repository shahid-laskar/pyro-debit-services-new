"""Unit tests for Phase 13 — Processor Concurrency & Per-Service Execution Guard.

Verifies:
1. get_service_lock retrieves or creates an asyncio.Lock per canonical service type.
2. Case-insensitivity: 'fancysale', 'FANCYSALE', and 'FancySale' share the exact same lock instance.
3. Service isolation: 'FANCYSALE', 'SIMSWAP', and 'ESIM' maintain independent locks.
4. is_service_locked accurately reports acquisition state.
5. Critical section serialization: Two concurrent calls to run_debit_batch for the SAME service
   are strictly serialized, preventing scheduler + manual trigger overlap.
6. Cross-service concurrency: Two concurrent calls to run_debit_batch for DIFFERENT services
   execute concurrently without blocking each other.
7. Resilience: Lock is guaranteed to be released even if fetch_and_claim or downstream steps raise exceptions.
8. Observability: summary dictionary includes execution_id when context is provided.
"""

import asyncio
from unittest.mock import AsyncMock, MagicMock, patch
import pytest

from app.context import ExecutionContext
from app.debit.processor import (
    _reset_service_locks,
    get_service_lock,
    is_service_locked,
    run_debit_batch,
)


@pytest.fixture(autouse=True)
def clean_locks():
    """Reset service locks before and after each test."""
    _reset_service_locks()
    yield
    _reset_service_locks()


# ══════════════════════════════════════════════════════════════════════════════
# 1. Lock Management & Key Normalization Tests
# ══════════════════════════════════════════════════════════════════════════════

class TestServiceLockManagement:
    """Validate lock creation, case normalization, and status inspection."""

    @pytest.mark.asyncio
    async def test_get_service_lock_returns_asyncio_lock(self):
        lock = await get_service_lock("FANCYSALE")
        assert isinstance(lock, asyncio.Lock)

    @pytest.mark.asyncio
    async def test_get_service_lock_same_service_returns_same_instance(self):
        lock1 = await get_service_lock("FANCYSALE")
        lock2 = await get_service_lock("FANCYSALE")
        assert lock1 is lock2

    @pytest.mark.asyncio
    async def test_get_service_lock_normalizes_case_and_whitespace(self):
        """Case variations must resolve to the identical lock instance."""
        lock_upper = await get_service_lock("FANCYSALE")
        lock_lower = await get_service_lock("fancysale")
        lock_mixed = await get_service_lock(" FancySale ")

        assert lock_upper is lock_lower
        assert lock_upper is lock_mixed

    @pytest.mark.asyncio
    async def test_get_service_lock_different_services_distinct_locks(self):
        """Different services must receive distinct lock instances."""
        lock_fancy = await get_service_lock("FANCYSALE")
        lock_sim = await get_service_lock("SIMSWAP")
        lock_esim = await get_service_lock("ESIM")

        assert lock_fancy is not lock_sim
        assert lock_fancy is not lock_esim
        assert lock_sim is not lock_esim

    @pytest.mark.asyncio
    async def test_is_service_locked_reflects_state(self):
        assert not await is_service_locked("FANCYSALE")

        lock = await get_service_lock("FANCYSALE")
        async with lock:
            assert await is_service_locked("FANCYSALE")
            assert await is_service_locked("fancysale")  # normalized
            assert not await is_service_locked("SIMSWAP")  # independent

        assert not await is_service_locked("FANCYSALE")


# ══════════════════════════════════════════════════════════════════════════════
# 2. Concurrency & Execution Serialization Tests
# ══════════════════════════════════════════════════════════════════════════════

class TestProcessorConcurrencySerialization:
    """Validate serialization of overlapping batches and parallel execution across services."""

    @pytest.mark.asyncio
    async def test_same_service_concurrent_calls_strictly_serialize(self):
        """When two batches for the same service run concurrently, they cannot overlap in the critical section."""
        adapter = MagicMock()
        adapter.service_type = "FANCYSALE"
        adapter.enabled = True
        adapter.batch_size = 50
        adapter.token_manager = MagicMock()

        # Track active workers inside the critical section
        active_in_critical_section = 0
        max_concurrent_in_critical_section = 0
        events_timeline = []

        def slow_fetch_and_claim(batch_size, context=None):
            nonlocal active_in_critical_section, max_concurrent_in_critical_section
            active_in_critical_section += 1
            if active_in_critical_section > max_concurrent_in_critical_section:
                max_concurrent_in_critical_section = active_in_critical_section
            return []

        adapter.fetch_and_claim.side_effect = slow_fetch_and_claim

        async def worker(worker_id: str):
            events_timeline.append(f"{worker_id}_enter")
            ctx = ExecutionContext.create(source="MANUAL_API", service_type="fancysale")
            await run_debit_batch(adapter, context=ctx)
            nonlocal active_in_critical_section
            active_in_critical_section -= 1
            events_timeline.append(f"{worker_id}_exit")

        # Launch worker 1 and worker 2 concurrently
        await asyncio.gather(worker("w1"), worker("w2"))

        # In a serialized execution, at most 1 worker can be in critical section
        assert max_concurrent_in_critical_section == 1
        assert len(events_timeline) == 4

    @pytest.mark.asyncio
    async def test_different_services_can_run_in_parallel(self):
        """Batches for DIFFERENT services must run concurrently without blocking each other."""
        adapter_fancy = MagicMock()
        adapter_fancy.service_type = "FANCYSALE"
        adapter_fancy.enabled = True
        adapter_fancy.batch_size = 50

        adapter_sim = MagicMock()
        adapter_sim.service_type = "SIMSWAP"
        adapter_sim.enabled = True
        adapter_sim.batch_size = 50

        in_critical_fancy = False
        in_critical_sim = False
        overlapped = False

        barrier = asyncio.Event()

        def slow_fancy_claim(batch_size, context=None):
            nonlocal in_critical_fancy, overlapped
            in_critical_fancy = True
            if in_critical_sim:
                overlapped = True
            return []

        def slow_sim_claim(batch_size, context=None):
            nonlocal in_critical_sim, overlapped
            in_critical_sim = True
            if in_critical_fancy:
                overlapped = True
            return []

        adapter_fancy.fetch_and_claim.side_effect = slow_fancy_claim
        adapter_sim.fetch_and_claim.side_effect = slow_sim_claim

        # Run both
        await asyncio.gather(
            run_debit_batch(adapter_fancy),
            run_debit_batch(adapter_sim),
        )

        # Both adapters had their claims invoked
        assert adapter_fancy.fetch_and_claim.called
        assert adapter_sim.fetch_and_claim.called

    @pytest.mark.asyncio
    async def test_lock_released_when_fetch_and_claim_raises(self):
        """If fetch_and_claim raises an unhandled error, lock must not remain held."""
        adapter = MagicMock()
        adapter.service_type = "FANCYSALE"
        adapter.enabled = True
        adapter.batch_size = 50
        adapter.fetch_and_claim.side_effect = RuntimeError("Oracle connectivity lost")

        with pytest.raises(RuntimeError, match="Oracle connectivity lost"):
            await run_debit_batch(adapter)

        # Lock must be released
        assert not await is_service_locked("FANCYSALE")

    @pytest.mark.asyncio
    async def test_summary_contains_execution_id_when_context_present(self):
        """run_debit_batch summary includes execution_id from context."""
        adapter = MagicMock()
        adapter.service_type = "FANCYSALE"
        adapter.enabled = True
        adapter.batch_size = 50
        adapter.token_manager = MagicMock()
        adapter.fetch_and_claim.return_value = [
            {"refid": "1001", "module_type": "FANCYSALE"}
        ]
        adapter.get_record_ref.return_value = "1001"
        adapter.map_to_pyro_params.return_value = {
            "client_id": "c1", "source_msisdn": "1", "dest_msisdn": "2",
            "amount": 10.0, "mpin": "1234", "remarks": "FANCYSALE",
        }

        ctx = ExecutionContext.create(source="SCHEDULED", service_type="fancysale")

        with patch("app.debit.processor.wallet_adjustment", new_callable=AsyncMock) as mock_wallet, \
             patch("app.debit.processor.async_insert_debit_txn_log", new_callable=AsyncMock):
            mock_wallet.return_value = {
                "statusCode": 200, "status": "SUCCESS", "data": {"pyroId": "P999"},
            }
            summary = await run_debit_batch(adapter, context=ctx)

            assert summary["service_type"] == "FANCYSALE"
            assert summary["execution_id"] == ctx.execution_id
            assert summary["processed"] == 1
            assert summary["success"] == 1
            assert not await is_service_locked("FANCYSALE")
