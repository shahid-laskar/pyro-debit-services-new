"""

Endpoints
─────────
GET  /debit/status                           — token status for all 3 debit TMs (Admin protected)
GET  /admin/zones                            — active zone configuration & resolved circles
POST /admin/trigger-debit/{service_type}     — manual batch run for one service
POST /admin/reset-stuck-debit/{service_type} — emergency: reset ALL P rows for service
"""

import asyncio
import logging
from datetime import datetime, timezone
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Query

from app.config import settings
from app.debit.token_managers import ALL_DEBIT_TOKEN_MANAGERS
from app.security import require_admin_api_key

logger = logging.getLogger(__name__)
router = APIRouter()


@router.get(
    "/debit/status",
    tags=["Debit"],
    dependencies=[Depends(require_admin_api_key)],
)
async def debit_status():
    
    from app.debit.services.registry import SERVICE_REGISTRY

    now = datetime.now(timezone.utc).timestamp()
    token_statuses = []
    for tm in ALL_DEBIT_TOKEN_MANAGERS:
        exp = tm._access_token_exp
        token_statuses.append({
            "label":                  tm.label,
            "session_token_present":  tm.session_token is not None,
            "access_token_present":   tm.access_token  is not None,
            "access_expires_in_s":    max(0, round(exp - now)) if exp else None,
        })

    service_statuses = [
        {"service_type": svc_type, "enabled": adapter.enabled,
         "batch_size": adapter.batch_size}
        for svc_type, adapter in SERVICE_REGISTRY.items()
    ]

    return {
        "tokens":   token_statuses,
        "services": service_statuses,
    }


@router.get(
    "/admin/zones",
    tags=["Admin"],
    dependencies=[Depends(require_admin_api_key)],
)
async def get_zones_metadata():
    """Inspect active zone configuration, mode, and resolved circles."""
    from app.zones import CIRCLE_METADATA, InvalidZoneError, resolve_zones

    try:
        selection = resolve_zones(settings.enabled_zones)
    except InvalidZoneError as exc:
        raise HTTPException(
            status_code=500,
            detail=f"Configured enabled_zones '{settings.enabled_zones}' is invalid: {exc}",
        )

    active_circles = (
        list(selection.circle_codes)
        if selection.circle_codes is not None
        else sorted(CIRCLE_METADATA.keys())
    )

    return {
        "configured_zones": settings.enabled_zones,
        "active_zone_codes": list(selection.zone_codes),
        "mode": selection.mode,
        "active_circle_count": len(active_circles),
        "active_circles": active_circles,
    }


@router.post(
    "/admin/trigger-debit/{service_type}",
    tags=["Admin"],
    dependencies=[Depends(require_admin_api_key)],
)
async def trigger_debit(
    service_type: str,
    zones: Optional[str] = Query(
        default=None,
        description=(
            "Optional comma-separated zone codes to process (e.g. 'NZ', 'NZ,WZ', 'ALL'). "
            "If omitted, strictly defaults to settings.enabled_zones. Never silently expands to ALL."
        ),
    ),
):
    from app.context import ExecutionContext, ExecutionSource
    from app.debit.processor import run_debit_batch
    from app.debit.services.registry import get_service
    from app.zones import InvalidZoneError

    try:
        adapter = get_service(service_type)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail=str(exc))

    try:
        context = ExecutionContext.create(
            source=ExecutionSource.MANUAL_API,
            zones_str=zones,
            service_type=service_type.lower(),
        )
    except InvalidZoneError as exc:
        raise HTTPException(status_code=400, detail=str(exc))

    logger.info(
        "[ADMIN] trigger_debit invoked for %s: execution_id=%s, requested_zones=%s, effective_zones=%s, configured_zones=%s, mode=%s, circle_count=%d",
        service_type,
        context.execution_id,
        zones,
        list(context.zone_codes),
        settings.enabled_zones,
        context.mode,
        context.circle_count,
    )

    summary = await run_debit_batch(adapter, context=context)
    return {
        "triggered": True,
        "execution_id": context.execution_id,
        "requested_zones": zones,
        "effective_zones": list(context.zone_codes),
        "configured_zones": settings.enabled_zones,
        "mode": context.mode,
        "summary": summary,
    }


@router.post(
    "/admin/reset-stuck-debit/{service_type}",
    tags=["Admin"],
    dependencies=[Depends(require_admin_api_key)],
)
async def reset_stuck_debit(
    service_type: str,
    stuck_minutes: Optional[int] = Query(
        default=None,
        description=(
            "Age threshold in minutes. Records stuck in 'P' state longer than this "
            "are reset to 'N'. Defaults to the service's configured stuck_minutes. "
            "Pass 0 explicitly to reset ALL 'P' records regardless of age — use with caution."
        ),
    ),
    zones: Optional[str] = Query(
        default=None,
        description=(
            "Optional comma-separated zone codes to reset (e.g. 'NZ', 'NZ,WZ', 'ALL'). "
            "If omitted, strictly defaults to settings.enabled_zones. Never silently expands to ALL."
        ),
    ),
):
    """
    Emergency reset: move stuck CAF_ENTRY_DONE='P' records back to 'N' for a service.
    Omitting stuck_minutes uses the service's own configured threshold (safe default).
    Passing stuck_minutes=0 resets ALL P records regardless of age.
    Omitting zones uses configured settings.enabled_zones (never silently expands to ALL).
    """
    from app.context import ExecutionContext, ExecutionSource
    from app.debit.services.registry import get_service
    from app.zones import InvalidZoneError

    try:
        adapter = get_service(service_type)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail=str(exc))

    try:
        context = ExecutionContext.create(
            source=ExecutionSource.MANUAL_API,
            zones_str=zones,
            service_type=service_type.lower(),
        )
    except InvalidZoneError as exc:
        raise HTTPException(status_code=400, detail=str(exc))

    effective_minutes = stuck_minutes if stuck_minutes is not None else adapter.stuck_minutes

    logger.warning(
        "[ADMIN_AUDIT] reset_stuck_debit invoked for %s: execution_id=%s, stuck_minutes=%d, requested_zones=%s, effective_zones=%s, configured_zones=%s, mode=%s, circle_count=%d",
        service_type,
        context.execution_id,
        effective_minutes,
        zones,
        list(context.zone_codes),
        settings.enabled_zones,
        context.mode,
        context.circle_count,
    )

    count = await asyncio.to_thread(
        adapter.reset_stuck_processing,
        effective_minutes,
        context=context,
    )
    return {
        "service_type":       service_type,
        "execution_id":       context.execution_id,
        "stuck_minutes_used": effective_minutes,
        "requested_zones":    zones,
        "effective_zones":    list(context.zone_codes),
        "configured_zones":   settings.enabled_zones,
        "mode":               context.mode,
        "rows_reset":         count,
    }