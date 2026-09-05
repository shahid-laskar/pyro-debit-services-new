"""
Debit Service
Run: uvicorn main:app --host 127.0.0.1 --port 8000 --proxy-headers --forwarded-allow-ips '*'
"""

import logging
from contextlib import asynccontextmanager

from fastapi import Depends, FastAPI

from app.config import settings
from app.db.oracle import close_oracle_pool, init_oracle_pool, is_oracle_pool_ready
from app.db.postgres import close_pg_pool, init_pg_pool, is_pg_pool_ready
from app.scheduler import start_scheduler, stop_scheduler
from app.security import require_admin_api_key

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)-8s | %(name)s | %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
logger = logging.getLogger(__name__)


@asynccontextmanager
async def lifespan(app: FastAPI):
    scheduler_started = False
    logger.info("Debit Service -- starting up")
    if not settings.admin_api_key:
        logger.critical("ADMIN_API_KEY is not configured; admin endpoints will reject requests")

    init_pg_pool()
    init_oracle_pool()

    # ── Debit service validation ───────────────────────────────────────────────
    from app.debit.services.registry import SERVICE_REGISTRY
    from app.debit.token_managers import ALL_DEBIT_TOKEN_MANAGERS

    enabled_unimplemented = [
        svc_type for svc_type, adapter in SERVICE_REGISTRY.items()
        if adapter.enabled and not getattr(adapter, "implemented", False)
    ]
    if enabled_unimplemented:
        raise RuntimeError(
            "Enabled debit service adapter(s) are not implemented: "
            + ", ".join(enabled_unimplemented)
        )

    # ── Debit service auth ─────────────────────────────────────────────────────
    enabled_labels = {
        adapter.token_manager.label
        for adapter in SERVICE_REGISTRY.values()
        if adapter.enabled
    }
    debit_token_managers = [
        tm for tm in ALL_DEBIT_TOKEN_MANAGERS
        if settings.validate_disabled_debit_credentials or tm.label in enabled_labels
    ]
    for tm in debit_token_managers:
        try:
            ok = await tm.authenticate()
            level = logging.INFO if ok else logging.WARNING
            logger.log(level, "  Debit auth [%s]: %s", tm.label, "OK" if ok else "FAILED")
        except Exception as exc:
            logger.warning("  Debit auth [%s] exception (non-fatal): %s: %s",
                           tm.label, type(exc).__name__, exc)

    if settings.enable_scheduler:
        start_scheduler()
        scheduler_started = True
    else:
        logger.info("Scheduler disabled by ENABLE_SCHEDULER=false")

    logger.info("Debit Service -- ready")
    yield

    logger.info("Debit Service -- shutting down")
    if scheduler_started:
        stop_scheduler()
    close_oracle_pool()
    close_pg_pool()
    logger.info("Shutdown complete")


app = FastAPI(
    title="Debit Service",
    version="1.0.0",
    root_path=settings.root_path,
    lifespan=lifespan,
    docs_url="/docs",
    openapi_url="/openapi.json",
)

# ── Debit router (FancySale / SimSwap / ESIM) ─────────────────────────────────
from app.debit.router import router as debit_router
app.include_router(debit_router)


# ── Ops endpoints ──────────────────────────────────────────────────────────────

@app.get("/health", tags=["Ops"])
async def health():
    return {"status": "ok"}


@app.get("/ready", tags=["Ops"])
async def ready():
    pg_ready     = is_pg_pool_ready()
    oracle_ready = is_oracle_pool_ready()
    return {
        "status":           "ready" if pg_ready and oracle_ready else "not_ready",
        "postgres_pool":    pg_ready,
        "oracle_pool":      oracle_ready,
        "scheduler_enabled": settings.enable_scheduler,
    }