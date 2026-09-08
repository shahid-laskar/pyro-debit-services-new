"""Unit tests for Phase 14 — Scheduler Effective Context & Execution Logging.

Verifies:
1. Scheduled debit job (_debit_job) constructs effective execution context using settings.enabled_zones.
2. Scheduled cleanup job (_stuck_cleanup_job) constructs effective execution context using the same settings.enabled_zones.
3. Both jobs log all 6 mandatory telemetry/audit fields:
   - execution_id
   - service_type
   - source=SCHEDULED
   - zones
   - circle_count
   - mode
4. Context scoping matches configured single-zone (NZ), multi-zone (NZ,WZ), and nationwide (ALL) modes.
"""

from unittest.mock import AsyncMock, MagicMock, patch
import pytest

from app.config import settings
from app.context import ExecutionContext
from app.scheduler import _debit_job, _stuck_cleanup_job, start_scheduler, stop_scheduler


# ══════════════════════════════════════════════════════════════════════════════
# 1. Debit Job Execution Context & Logging Tests
# ══════════════════════════════════════════════════════════════════════════════

class TestSchedulerDebitJobZoneContext:
    """Validate _debit_job zone context construction and telemetry logging."""

    @pytest.mark.asyncio
    async def test_debit_job_constructs_context_from_settings_enabled_zones_filtered(self):
        """_debit_job uses settings.enabled_zones='NZ' to create a FILTERED context with 9 circles."""
        mock_adapter = MagicMock()
        mock_adapter.enabled = True
        mock_adapter.service_type = "FANCYSALE"

        with patch("app.debit.services.registry.SERVICE_REGISTRY", {"FANCYSALE": mock_adapter}), \
             patch("app.debit.processor.run_debit_batch", new_callable=AsyncMock) as mock_run_batch, \
             patch.object(settings, "enabled_zones", "NZ"), \
             patch("app.scheduler.logger.info") as mock_info:

            mock_run_batch.return_value = {"processed": 0, "success": 0, "failed": 0}
            await _debit_job("FANCYSALE")

            assert mock_run_batch.called
            ctx: ExecutionContext = mock_run_batch.call_args[1]["context"]

            assert ctx.source == "SCHEDULED"
            assert ctx.zone_codes == ("NZ",)
            assert ctx.mode == "FILTERED"
            assert ctx.circle_count == 9
            assert ctx.service_type == "fancysale"

            # Check all 6 fields logged at start and completion
            info_messages = [call[0][0] % call[0][1:] for call in mock_info.call_args_list]
            starting_log = [msg for msg in info_messages if "debit starting" in msg]
            done_log = [msg for msg in info_messages if "debit done" in msg]

            assert len(starting_log) == 1
            assert f"execution_id={ctx.execution_id}" in starting_log[0]
            assert "service_type=FANCYSALE" in starting_log[0]
            assert "source=SCHEDULED" in starting_log[0]
            assert "zones=NZ" in starting_log[0]
            assert "circle_count=9" in starting_log[0]
            assert "mode=FILTERED" in starting_log[0]

            assert len(done_log) == 1
            assert f"execution_id={ctx.execution_id}" in done_log[0]
            assert "zones=NZ" in done_log[0]
            assert "mode=FILTERED" in done_log[0]

    @pytest.mark.asyncio
    async def test_debit_job_constructs_context_all_mode(self):
        """_debit_job with settings.enabled_zones='ALL' creates an ALL mode context with all circles."""
        mock_adapter = MagicMock()
        mock_adapter.enabled = True
        mock_adapter.service_type = "SIMSWAP"

        with patch("app.debit.services.registry.SERVICE_REGISTRY", {"SIMSWAP": mock_adapter}), \
             patch("app.debit.processor.run_debit_batch", new_callable=AsyncMock) as mock_run_batch, \
             patch.object(settings, "enabled_zones", "ALL"), \
             patch("app.scheduler.logger.info") as mock_info:

            mock_run_batch.return_value = {"processed": 0, "success": 0, "failed": 0}
            await _debit_job("SIMSWAP")

            ctx: ExecutionContext = mock_run_batch.call_args[1]["context"]
            assert ctx.source == "SCHEDULED"
            assert ctx.zone_codes == ("ALL",)
            assert ctx.mode == "ALL"
            assert ctx.circle_codes is None
            assert ctx.circle_count == 31

            info_messages = [call[0][0] % call[0][1:] for call in mock_info.call_args_list]
            starting_log = [msg for msg in info_messages if "debit starting" in msg]
            assert "zones=ALL" in starting_log[0]
            assert "mode=ALL" in starting_log[0]
            assert "circle_count=31" in starting_log[0]

    @pytest.mark.asyncio
    async def test_debit_job_constructs_context_multi_zone(self):
        """_debit_job with settings.enabled_zones='NZ,WZ' creates multi-zone context."""
        mock_adapter = MagicMock()
        mock_adapter.enabled = True
        mock_adapter.service_type = "ESIM"

        with patch("app.debit.services.registry.SERVICE_REGISTRY", {"ESIM": mock_adapter}), \
             patch("app.debit.processor.run_debit_batch", new_callable=AsyncMock) as mock_run_batch, \
             patch.object(settings, "enabled_zones", "NZ,WZ"), \
             patch("app.scheduler.logger.info") as mock_info:

            mock_run_batch.return_value = {"processed": 0}
            await _debit_job("ESIM")

            ctx: ExecutionContext = mock_run_batch.call_args[1]["context"]
            assert ctx.zone_codes == ("NZ", "WZ")
            assert ctx.mode == "FILTERED"
            assert ctx.circle_count == 14

            info_messages = [call[0][0] % call[0][1:] for call in mock_info.call_args_list]
            starting_log = [msg for msg in info_messages if "debit starting" in msg]
            assert "zones=NZ,WZ" in starting_log[0]
            assert "circle_count=14" in starting_log[0]

    @pytest.mark.asyncio
    async def test_debit_job_skips_when_disabled(self):
        """_debit_job logs and returns early when adapter.enabled is False."""
        mock_adapter = MagicMock()
        mock_adapter.enabled = False
        mock_adapter.service_type = "FANCYSALE"

        with patch("app.debit.services.registry.SERVICE_REGISTRY", {"FANCYSALE": mock_adapter}), \
             patch("app.debit.processor.run_debit_batch", new_callable=AsyncMock) as mock_run_batch:

            await _debit_job("FANCYSALE")
            assert not mock_run_batch.called


# ══════════════════════════════════════════════════════════════════════════════
# 2. Stuck Cleanup Job Execution Context & Logging Tests
# ══════════════════════════════════════════════════════════════════════════════

class TestSchedulerCleanupJobZoneContext:
    """Validate _stuck_cleanup_job zone context construction and telemetry logging."""

    @pytest.mark.asyncio
    async def test_cleanup_job_constructs_context_from_settings_enabled_zones(self):
        """_stuck_cleanup_job passes context using settings.enabled_zones and logs all 6 fields."""
        mock_adapter = MagicMock()
        mock_adapter.service_type = "FANCYSALE"
        mock_adapter.stuck_minutes = 10
        mock_adapter.reset_stuck_processing.return_value = 5

        with patch("app.debit.services.registry.get_enabled_services", return_value=[mock_adapter]), \
             patch.object(settings, "enabled_zones", "NZ"), \
             patch("app.scheduler.logger.info") as mock_info:

            await _stuck_cleanup_job()

            assert mock_adapter.reset_stuck_processing.called
            call_kwargs = mock_adapter.reset_stuck_processing.call_args[1]
            ctx: ExecutionContext = call_kwargs["context"]

            assert ctx.source == "SCHEDULED"
            assert ctx.zone_codes == ("NZ",)
            assert ctx.mode == "FILTERED"
            assert ctx.circle_count == 9
            assert ctx.service_type == "fancysale"

            # Check logging
            info_messages = [call[0][0] % call[0][1:] for call in mock_info.call_args_list]
            starting_log = [msg for msg in info_messages if "cleanup starting" in msg]
            done_log = [msg for msg in info_messages if "cleanup done" in msg]

            assert len(starting_log) == 1
            assert f"execution_id={ctx.execution_id}" in starting_log[0]
            assert "service_type=FANCYSALE" in starting_log[0]
            assert "source=SCHEDULED" in starting_log[0]
            assert "zones=NZ" in starting_log[0]
            assert "circle_count=9" in starting_log[0]
            assert "mode=FILTERED" in starting_log[0]
            assert "stuck_minutes=10" in starting_log[0]

            assert len(done_log) == 1
            assert f"execution_id={ctx.execution_id}" in done_log[0]
            assert "reset_count=5" in done_log[0]

    @pytest.mark.asyncio
    async def test_cleanup_job_inherits_same_scope_across_multiple_adapters(self):
        """_stuck_cleanup_job processes each enabled adapter with the same configured zone scope."""
        adapter1 = MagicMock()
        adapter1.service_type = "FANCYSALE"
        adapter1.stuck_minutes = 10
        adapter1.reset_stuck_processing.return_value = 0

        adapter2 = MagicMock()
        adapter2.service_type = "SIMSWAP"
        adapter2.stuck_minutes = 15
        adapter2.reset_stuck_processing.return_value = 0

        with patch("app.debit.services.registry.get_enabled_services", return_value=[adapter1, adapter2]), \
             patch.object(settings, "enabled_zones", "WZ"):

            await _stuck_cleanup_job()

            ctx1: ExecutionContext = adapter1.reset_stuck_processing.call_args[1]["context"]
            ctx2: ExecutionContext = adapter2.reset_stuck_processing.call_args[1]["context"]

            assert ctx1.zone_codes == ("WZ",)
            assert ctx1.circle_count == 5
            assert ctx1.service_type == "fancysale"

            assert ctx2.zone_codes == ("WZ",)
            assert ctx2.circle_count == 5
            assert ctx2.service_type == "simswap"


# ══════════════════════════════════════════════════════════════════════════════
# 3. Scheduler Lifecycle Tests
# ══════════════════════════════════════════════════════════════════════════════

class TestSchedulerLifecycle:
    """Validate scheduler startup logging and job registration."""

    def test_start_scheduler_logs_configured_zones(self):
        with patch.object(settings, "enabled_zones", "NZ"), \
             patch("app.scheduler.scheduler.start") as mock_start, \
             patch("app.scheduler.logger.info") as mock_info:

            start_scheduler()
            mock_start.assert_called_once()

            info_messages = [call[0][0] % call[0][1:] for call in mock_info.call_args_list]
            startup_log = [msg for msg in info_messages if "configured_zones: NZ" in msg]
            assert len(startup_log) == 1
