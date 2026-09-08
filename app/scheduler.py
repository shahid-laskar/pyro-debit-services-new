import asyncio
import logging
from datetime import datetime, timezone

from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger
from apscheduler.triggers.interval import IntervalTrigger

from app.config import settings

logger = logging.getLogger(__name__)
scheduler = AsyncIOScheduler()


# ── Debit jobs ─────────────────────────────────────────────────────────────────

async def _debit_job(service_type: str):
    
    try:
        from app.context import ExecutionContext, ExecutionSource
        from app.debit.services.registry import SERVICE_REGISTRY
        from app.debit.processor import run_debit_batch

        adapter = SERVICE_REGISTRY.get(service_type)
        if adapter is None:
            logger.error("Scheduler: unknown debit service_type %s", service_type)
            return

        if not adapter.enabled:
            logger.info("Scheduler: [%s] debit service disabled — skipping", service_type)
            return

        # Construct effective execution context using settings.enabled_zones (Phase 14)
        context = ExecutionContext.create(
            source=ExecutionSource.SCHEDULED,
            zones_str=settings.enabled_zones,
            service_type=service_type.lower(),
        )

        logger.info(
            "Scheduler: [%s] debit starting — execution_id=%s service_type=%s source=SCHEDULED zones=%s circle_count=%d mode=%s",
            service_type,
            context.execution_id,
            service_type,
            context.zones_display,
            context.circle_count,
            context.mode,
        )

        summary = await run_debit_batch(adapter, context=context)

        logger.info(
            "Scheduler: [%s] debit done — execution_id=%s service_type=%s source=SCHEDULED zones=%s circle_count=%d mode=%s summary=%s",
            service_type,
            context.execution_id,
            service_type,
            context.zones_display,
            context.circle_count,
            context.mode,
            summary,
        )
    except Exception as exc:
        logger.error("Scheduler: [%s] debit exception: %s", service_type, exc)


async def _stuck_cleanup_job():
   
    try:
        from app.context import ExecutionContext, ExecutionSource
        from app.debit.services.registry import get_enabled_services
        for adapter in get_enabled_services():
            # Construct effective execution context using settings.enabled_zones (Phase 14)
            context = ExecutionContext.create(
                source=ExecutionSource.SCHEDULED,
                zones_str=settings.enabled_zones,
                service_type=adapter.service_type.lower(),
            )

            logger.info(
                "Scheduler: [%s] cleanup starting — execution_id=%s service_type=%s source=SCHEDULED zones=%s circle_count=%d mode=%s stuck_minutes=%d",
                adapter.service_type,
                context.execution_id,
                adapter.service_type,
                context.zones_display,
                context.circle_count,
                context.mode,
                adapter.stuck_minutes,
            )

            count = await asyncio.to_thread(
                adapter.reset_stuck_processing,
                adapter.stuck_minutes,
                context=context,
            )

            logger.info(
                "Scheduler: [%s] cleanup done — execution_id=%s service_type=%s source=SCHEDULED zones=%s circle_count=%d mode=%s reset_count=%d",
                adapter.service_type,
                context.execution_id,
                adapter.service_type,
                context.zones_display,
                context.circle_count,
                context.mode,
                count,
            )
    except Exception as exc:
        logger.error("Scheduler: debit stuck cleanup exception: %s", exc)


async def _debit_daily_auth_job():
    """Re-authenticate all debit token managers. Fires at 00:10 daily."""
    from app.debit.token_managers import ALL_DEBIT_TOKEN_MANAGERS
    for tm in ALL_DEBIT_TOKEN_MANAGERS:
        try:
            ok = await tm.authenticate()
            level = logging.INFO if ok else logging.WARNING
            logger.log(level, "Scheduler: debit daily re-auth [%s] %s",
                       tm.label, "OK" if ok else "FAILED")
        except Exception as exc:
            logger.error("Scheduler: debit daily re-auth [%s] exception: %s",
                         tm.label, exc)


# ── Lifecycle ──────────────────────────────────────────────────────────────────

def start_scheduler():
    now = datetime.now(timezone.utc)

    # ── One debit job per service in registry ──────────────────────────────────
    from app.debit.services.registry import SERVICE_REGISTRY
    for svc_type, adapter in SERVICE_REGISTRY.items():
        scheduler.add_job(
            _debit_job,
            IntervalTrigger(minutes=adapter.interval_minutes),
            args=[svc_type],
            id=f"debit_{svc_type.lower()}",
            replace_existing=True,
            max_instances=1,
            coalesce=True,
            misfire_grace_time=60,
            next_run_time=now
            if adapter.enabled and settings.run_debit_on_startup
            else None,
        )
        logger.info(
            "Scheduler: registered debit_%s — every %dmin (startup=%s, enabled=%s)",
            svc_type.lower(), adapter.interval_minutes,
            settings.run_debit_on_startup, adapter.enabled,
        )

    # ── Stuck-record cleanup (all debit services, every 15 min) ───────────────
    scheduler.add_job(
        _stuck_cleanup_job,
        IntervalTrigger(minutes=15),
        id="stuck_cleanup",
        replace_existing=True,
        max_instances=1,
        coalesce=True,
        misfire_grace_time=60,
        next_run_time=now if settings.run_cleanup_on_startup else None,
    )

    # ── Daily re-auth (00:10) ──────────────────────────────────────────────────
    scheduler.add_job(
        _debit_daily_auth_job,
        CronTrigger(hour=0, minute=10),
        id="debit_daily_auth",
        replace_existing=True,
        max_instances=1,
        coalesce=True,
    )

    scheduler.start()
    logger.info(
        "Scheduler started — configured_zones: %s | stuck_cleanup: every 15min (startup=%s) | "
        "debit_daily_auth: 00:10",
        settings.enabled_zones,
        settings.run_cleanup_on_startup,
    )


def stop_scheduler():
    scheduler.shutdown()
    logger.info("Scheduler stopped")