import asyncio
import json
import logging
from datetime import datetime
from typing import Optional

from app.db.postgres import get_pg_conn, _pg_retry     # reuse the existing pool

logger = logging.getLogger(__name__)


# ── Helpers ────────────────────────────────────────────────────────────────────

def _mask_debit_body(body: dict) -> str:
    masked = body.copy()
    if "mpin" in masked:
        masked["mpin"] = "***"
    return json.dumps(masked)


# ── Sync insert ────────────────────────────────────────────────────────────────
@_pg_retry                          # retries once on OperationalError
def _do_insert_debit_txn_log(values: tuple) -> None:
    """Inner insert — allowed to raise so _pg_retry can catch and retry."""
    sql = """
        INSERT INTO public.debit_txn_log (
            service_type, oracle_ref_id, client_id,
            source_msisdn, dest_msisdn, amount,
            api_stage, api_endpoint, attempt_no,
            request_body, response_http_code, response_body,
            pyro_status_code, pyro_status_text, pyro_txn_id,
            call_started_at, call_ended_at, duration_ms,
            is_success, is_perm_failure, error_class, error_detail
        ) VALUES (
            %s, %s, %s,
            %s, %s, %s,
            %s, %s, %s,
            %s, %s, %s,
            %s, %s, %s,
            %s, %s, %s,
            %s, %s, %s, %s
        )
    """
    with get_pg_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(sql, values)
            
def insert_debit_txn_log(
    service_type:       str,
    oracle_ref_id:      Optional[str],
    client_id:          Optional[str],
    source_msisdn:      Optional[str],
    dest_msisdn:        Optional[str],
    amount:             Optional[float],
    api_stage:          str,
    api_endpoint:       Optional[str],
    attempt_no:         int,
    request_body:       Optional[str],          # already masked (call _mask_debit_body)
    response_http_code: Optional[int],
    response_body:      Optional[str],
    pyro_status_code:   Optional[int],
    pyro_status_text:   Optional[str],
    pyro_txn_id:        Optional[str],
    call_started_at:    Optional[datetime],
    call_ended_at:      Optional[datetime],
    duration_ms:        Optional[int],
    is_success:         str,                    # 'Y' | 'N'
    is_perm_failure:    str = "N",              # 'Y' | 'N'
    error_class:        Optional[str] = None,
    error_detail:       Optional[str] = None,
) -> None:
    values = (service_type, oracle_ref_id, client_id,
                    source_msisdn, dest_msisdn, amount,
                    api_stage, api_endpoint, attempt_no,
                    request_body, response_http_code, response_body,
                    pyro_status_code, pyro_status_text, pyro_txn_id,
                    call_started_at, call_ended_at, duration_ms,
                    is_success, is_perm_failure, error_class, error_detail)

    try:
        _do_insert_debit_txn_log(values)        
    except Exception as exc:
        logger.error(
            "debit_txn_log insert failed service=%s ref=%s stage=%s: %s",
            service_type, oracle_ref_id, api_stage, exc
        )


async def async_insert_debit_txn_log(**kwargs) -> None:
    """Fire-and-forget async wrapper — log failures are non-fatal."""
    await asyncio.to_thread(insert_debit_txn_log, **kwargs)