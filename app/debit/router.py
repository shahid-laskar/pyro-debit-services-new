"""

Endpoints
─────────
GET  /debit/status                          — token status for all 3 debit TMs
POST /admin/trigger-debit/{service_type}    — manual batch run for one service
POST /admin/reset-stuck-debit/{service_type}— emergency: reset ALL P rows for service
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


@router.get("/debit/status", tags=["Debit"])
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


@router.post(
    "/admin/trigger-debit/{service_type}",
    tags=["Admin"],
    dependencies=[Depends(require_admin_api_key)],
)
async def trigger_debit(service_type: str):
   
    from app.debit.processor import run_debit_batch
    from app.debit.services.registry import get_service

    try:
        adapter = get_service(service_type)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail=str(exc))

    summary = await run_debit_batch(adapter)
    return {"triggered": True, "summary": summary}


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
):
    """
    Emergency reset: move stuck CAF_ENTRY_DONE='P' records back to 'N' for a service.
    Omitting stuck_minutes uses the service's own configured threshold (safe default).
    Passing stuck_minutes=0 resets ALL P records regardless of age.
    """
    from app.debit.services.registry import get_service

    try:
        adapter = get_service(service_type)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail=str(exc))

    effective_minutes = stuck_minutes if stuck_minutes is not None else adapter.stuck_minutes
    count = await asyncio.to_thread(adapter.reset_stuck_processing, effective_minutes)
    return {
        "service_type":       service_type,
        "stuck_minutes_used": effective_minutes,
        "rows_reset":         count,
    }