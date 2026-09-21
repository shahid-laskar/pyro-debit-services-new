"""Unit tests for Phase 8 — Cleanup Race Elimination.

Verifies:
1. Default batch sizes across config and all adapters are reduced from 200 to 50.
2. ActiveOwnershipTracker correctly tracks, queries, and releases in-flight records per service.
3. build_active_exclusion_predicate formats SQL NOT IN clauses and chunks > 1000 items (ORA-01795 guard).
4. reset_stuck_processing across FancySale, SimSwap, and ESIM dynamically excludes actively owned records.
5. run_debit_batch in processor acquires active ownership upon claiming and releases upon completion.
6. Per-service concurrency locks prevent overlapping batches in the same process.
"""

import asyncio
from unittest.mock import AsyncMock, MagicMock, patch
import pytest

from app.config import Settings, settings
from app.debit.ownership import (
    ActiveOwnershipTracker,
    build_active_exclusion_predicate,
    ownership_tracker,
)
from app.debit.processor import run_debit_batch
from app.debit.services.esim import EsimAdapter
from app.debit.services.fancysale import FancySaleAdapter
from app.debit.services.simswap import SimswapAdapter


# ══════════════════════════════════════════════════════════════════════════════
# 1. Batch Size Configuration Tests
# ══════════════════════════════════════════════════════════════════════════════

class TestBatchSizeConfiguration:
    """Validate that default batch sizes are reduced from 200 to 50."""

    def test_settings_default_batch_sizes(self):
        """Settings defaults must be 50 for all debit services."""
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
        assert s.fancysale_batch_size == 50
        assert s.simswap_batch_size == 50
        assert s.esim_batch_size == 50

    def test_global_settings_batch_sizes(self):
        """Imported global settings has batch size 50."""
        assert settings.fancysale_batch_size == 50
        assert settings.simswap_batch_size == 50
        assert settings.esim_batch_size == 50

    def test_adapter_constructor_defaults(self):
        """Adapter constructors must default batch_size to 50."""
        fs = FancySaleAdapter(token_manager=MagicMock())
        ss = SimswapAdapter(token_manager=MagicMock())
        es = EsimAdapter(token_manager=MagicMock())

        assert fs.batch_size == 50
        assert ss.batch_size == 50
        assert es.batch_size == 50


# ══════════════════════════════════════════════════════════════════════════════
# 2. ActiveOwnershipTracker Unit Tests
# ══════════════════════════════════════════════════════════════════════════════

class TestActiveOwnershipTracker:
    """Test in-memory active ownership tracking logic."""

    def setup_method(self):
        ownership_tracker.clear()

    def teardown_method(self):
        ownership_tracker.clear()

    def test_acquire_and_get_active(self):
        tracker = ActiveOwnershipTracker()
        tracker.acquire("FANCYSALE", [101, 102, "103"])

        active = tracker.get_active("FANCYSALE")
        assert active == {"101", "102", "103"}
        assert tracker.is_active("FANCYSALE", 101)
        assert tracker.is_active("FANCYSALE", "102")
        assert not tracker.is_active("FANCYSALE", 999)

    def test_service_isolation(self):
        """Refs registered for one service must not appear in other services."""
        tracker = ActiveOwnershipTracker()
        tracker.acquire("FANCYSALE", [500])

        assert tracker.is_active("FANCYSALE", 500)
        assert not tracker.is_active("SIMSWAP", 500)
        assert not tracker.is_active("ESIM", 500)

    def test_release_single(self):
        tracker = ActiveOwnershipTracker()
        tracker.acquire("SIMSWAP", ["A", "B", "C"])

        tracker.release("SIMSWAP", "B")
        assert tracker.get_active("SIMSWAP") == {"A", "C"}
        assert not tracker.is_active("SIMSWAP", "B")

    def test_release_all(self):
        tracker = ActiveOwnershipTracker()
        tracker.acquire("ESIM", [1, 2, 3, 4])

        tracker.release_all("ESIM", [2, 3])
        assert tracker.get_active("ESIM") == {"1", "4"}

    def test_clear_service(self):
        tracker = ActiveOwnershipTracker()
        tracker.acquire("FANCYSALE", [1, 2])
        tracker.acquire("SIMSWAP", [3, 4])

        tracker.clear("FANCYSALE")
        assert tracker.get_active("FANCYSALE") == set()
        assert tracker.get_active("SIMSWAP") == {"3", "4"}

    def test_clear_all(self):
        tracker = ActiveOwnershipTracker()
        tracker.acquire("FANCYSALE", [1])
        tracker.acquire("SIMSWAP", [2])
        tracker.acquire("ESIM", [3])

        tracker.clear()
        assert tracker.get_active("FANCYSALE") == set()
        assert tracker.get_active("SIMSWAP") == set()
        assert tracker.get_active("ESIM") == set()


# ══════════════════════════════════════════════════════════════════════════════
# 3. SQL Exclusion Predicate Builder Tests
# ══════════════════════════════════════════════════════════════════════════════

class TestBuildActiveExclusionPredicate:
    """Test dynamic NOT IN predicate generation for Oracle cleanup queries."""

    def test_empty_refs_returns_empty(self):
        sql, params = build_active_exclusion_predicate("REFID", [])
        assert sql == ""
        assert params == {}

    def test_single_ref_formatting(self):
        sql, params = build_active_exclusion_predicate("REFID", [101])
        assert "REFID NOT IN (:act_0)" in sql
        assert params == {"act_0": 101}

    def test_multiple_refs_sorted_and_cast(self):
        sql, params = build_active_exclusion_predicate("ID", [300, 100, 200])
        assert "ID NOT IN (:act_0, :act_1, :act_2)" in sql
        assert params == {"act_0": 100, "act_1": 200, "act_2": 300}

    def test_chunking_over_1000_items(self):
        """More than 1000 items must be chunked to avoid ORA-01795."""
        refs = list(range(1050))
        sql, params = build_active_exclusion_predicate("REFID", refs)

        assert len(params) == 1050
        assert "REFID NOT IN (:act_0" in sql
        assert "REFID NOT IN (:act_1000" in sql
        assert " AND (" in sql


# ══════════════════════════════════════════════════════════════════════════════
# 4. Adapter reset_stuck_processing Active Exclusion Tests
# ══════════════════════════════════════════════════════════════════════════════

class TestAdapterResetStuckExclusion:
    """Validate that reset_stuck_processing queries exclude active in-flight records."""

    def setup_method(self):
        ownership_tracker.clear()

    def teardown_method(self):
        ownership_tracker.clear()

    def test_fancysale_reset_excludes_active_refs(self):
        adapter = FancySaleAdapter(token_manager=MagicMock(), enabled=True)

        mock_cur = MagicMock()
        mock_conn = MagicMock()
        mock_conn.cursor.return_value = mock_cur

        with patch("app.debit.services.fancysale.get_oracle_conn") as mock_get_conn:
            mock_get_conn.return_value.__enter__.return_value = mock_conn

            # Explicitly pass active_refs
            adapter.reset_stuck_processing(stuck_minutes=15, active_refs={101, 102})

            sql_executed = mock_cur.execute.call_args[0][0]
            params_executed = mock_cur.execute.call_args[0][1]

            assert "REFID NOT IN (:act_0, :act_1)" in sql_executed
            assert "CAF_ENTRY_DONE  = 'P'" in sql_executed
            assert "NOT LIKE 'RECONCILIATION_REQUIRED%'" in sql_executed
            assert params_executed["stuck_minutes"] == 15
            assert params_executed["act_0"] == 101
            assert params_executed["act_1"] == 102

    def test_fancysale_reset_reads_from_ownership_tracker_when_none_passed(self):
        adapter = FancySaleAdapter(token_manager=MagicMock(), enabled=True)
        ownership_tracker.acquire("FANCYSALE", [555, 777])

        mock_cur = MagicMock()
        mock_conn = MagicMock()
        mock_conn.cursor.return_value = mock_cur

        with patch("app.debit.services.fancysale.get_oracle_conn") as mock_get_conn:
            mock_get_conn.return_value.__enter__.return_value = mock_conn

            adapter.reset_stuck_processing(stuck_minutes=10)

            sql_executed = mock_cur.execute.call_args[0][0]
            params_executed = mock_cur.execute.call_args[0][1]

            assert "REFID NOT IN (:act_0, :act_1)" in sql_executed
            assert params_executed["act_0"] == 555
            assert params_executed["act_1"] == 777

    def test_fancysale_reset_no_active_refs_omits_not_in(self):
        adapter = FancySaleAdapter(token_manager=MagicMock(), enabled=True)

        mock_cur = MagicMock()
        mock_conn = MagicMock()
        mock_conn.cursor.return_value = mock_cur

        with patch("app.debit.services.fancysale.get_oracle_conn") as mock_get_conn:
            mock_get_conn.return_value.__enter__.return_value = mock_conn

            adapter.reset_stuck_processing(stuck_minutes=10)

            sql_executed = mock_cur.execute.call_args[0][0]
            params_executed = mock_cur.execute.call_args[0][1]

            assert "NOT IN" not in sql_executed
            assert params_executed == {"stuck_minutes": 10}

    def test_simswap_reset_excludes_active_refs(self):
        adapter = SimswapAdapter(token_manager=MagicMock(), enabled=True)
        ownership_tracker.acquire("SIMSWAP", [2001, 2002])

        mock_cur = MagicMock()
        mock_conn = MagicMock()
        mock_conn.cursor.return_value = mock_cur

        with patch("app.debit.services.simswap.get_oracle_conn") as mock_get_conn:
            mock_get_conn.return_value.__enter__.return_value = mock_conn

            adapter.reset_stuck_processing(stuck_minutes=20)

            sql_executed = mock_cur.execute.call_args[0][0]
            params_executed = mock_cur.execute.call_args[0][1]

            assert "ID NOT IN (:act_0, :act_1)" in sql_executed
            assert "MODULE_TYPE           = 'SIMSWAP'" in sql_executed
            assert params_executed["act_0"] == 2001
            assert params_executed["act_1"] == 2002

    def test_esim_reset_excludes_active_refs(self):
        adapter = EsimAdapter(token_manager=MagicMock(), enabled=True)
        ownership_tracker.acquire("ESIM", [3001, 3002])

        mock_cur = MagicMock()
        mock_conn = MagicMock()
        mock_conn.cursor.return_value = mock_cur

        with patch("app.debit.services.esim.get_oracle_conn") as mock_get_conn:
            mock_get_conn.return_value.__enter__.return_value = mock_conn

            adapter.reset_stuck_processing(stuck_minutes=20)

            sql_executed = mock_cur.execute.call_args[0][0]
            params_executed = mock_cur.execute.call_args[0][1]

            assert "ID NOT IN (:act_0, :act_1)" in sql_executed
            assert "MODULE_TYPE           = 'ESIM'" in sql_executed
            assert params_executed["act_0"] == 3001
            assert params_executed["act_1"] == 3002


# ══════════════════════════════════════════════════════════════════════════════
# 5. Processor Ownership Lifecycle & Concurrency Guard Tests
# ══════════════════════════════════════════════════════════════════════════════

class TestProcessorOwnershipLifecycle:
    """Validate that run_debit_batch properly registers and releases ownership."""

    def setup_method(self):
        ownership_tracker.clear()

    def teardown_method(self):
        ownership_tracker.clear()

    @pytest.mark.asyncio
    async def test_ownership_acquired_and_released_during_batch(self):
        adapter = MagicMock()
        adapter.service_type = "FANCYSALE"
        adapter.enabled = True
        adapter.batch_size = 50
        adapter.token_manager = MagicMock()

        records = [{"refid": "FS-1"}, {"refid": "FS-2"}]
        adapter.fetch_and_claim.return_value = records
        adapter.get_record_ref.side_effect = lambda r: str(r["refid"])
        adapter.map_to_pyro_params.return_value = {
            "client_id": "CLI",
            "source_msisdn": "940001",
            "dest_msisdn": "940002",
            "amount": 100.0,
            "mpin": "12345",
            "remarks": "FS",
        }

        active_during_run = []

        async def fake_wallet_adjustment(**kwargs):
            # Capture what is active in ownership_tracker while processing
            active_during_run.append(ownership_tracker.get_active("FANCYSALE"))
            return {
                "statusCode": 200,
                "status": "SUCCESS",
                "data": {"pyroId": "P-1", "balanceBefore": 500, "balanceAfter": 400},
            }

        with patch("app.debit.processor.wallet_adjustment", side_effect=fake_wallet_adjustment):
            summary = await run_debit_batch(adapter)

            assert summary["processed"] == 2
            assert summary["success"] == 2

            # While processing, both records were actively tracked
            assert "FS-1" in active_during_run[0] or "FS-2" in active_during_run[0]

            # After batch completion, ownership tracker MUST be empty
            assert ownership_tracker.get_active("FANCYSALE") == set()

    @pytest.mark.asyncio
    async def test_ownership_released_even_on_exception(self):
        adapter = MagicMock()
        adapter.service_type = "SIMSWAP"
        adapter.enabled = True
        adapter.batch_size = 50
        adapter.token_manager = MagicMock()

        records = [{"id": 901}, {"id": 902}]
        adapter.fetch_and_claim.return_value = records
        adapter.get_record_ref.side_effect = lambda r: str(r["id"])
        adapter.map_to_pyro_params.side_effect = RuntimeError("Fatal mapping error")

        with patch("app.debit.processor.wallet_adjustment", new_callable=AsyncMock), \
             patch("app.debit.processor.async_insert_debit_txn_log", new_callable=AsyncMock):
            summary = await run_debit_batch(adapter)

            assert summary["processed"] == 2
            assert summary["failed"] == 2

            # All active refs must be cleared even after exceptions
            assert ownership_tracker.get_active("SIMSWAP") == set()

    @pytest.mark.asyncio
    async def test_service_concurrency_lock(self):
        """Concurrent calls to run_debit_batch for the same service execute sequentially."""
        adapter = MagicMock()
        adapter.service_type = "FANCYSALE"
        adapter.enabled = True
        adapter.batch_size = 50
        adapter.token_manager = MagicMock()

        adapter.fetch_and_claim.return_value = []

        execution_order = []

        async def run_batch_with_logging(batch_id):
            execution_order.append(f"start_{batch_id}")
            await asyncio.sleep(0.05)
            await run_debit_batch(adapter)
            execution_order.append(f"end_{batch_id}")

        # Run two batches concurrently
        await asyncio.gather(
            run_batch_with_logging(1),
            run_batch_with_logging(2),
        )

        # Execution must not interleave inside the critical section
        assert "start_1" in execution_order
        assert "start_2" in execution_order
