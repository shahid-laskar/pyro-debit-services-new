import asyncio
import json
import logging
from datetime import datetime, timezone
from typing import Dict, List, Optional, Sequence, Tuple

from app.db.postgres import get_pg_conn, _pg_retry     # reuse the existing pool

logger = logging.getLogger(__name__)


# ── Helpers ────────────────────────────────────────────────────────────────────

SENSITIVE_KEYS = {
    "mpin", "password", "api_key", "apikey", "jwt",
    "token", "access_token", "session_token", "secret", "secret_key",
}


def _mask_debit_body(body: dict) -> str:
    """Mask sensitive fields before persistence in transaction audit log."""
    masked = body.copy()
    for k in list(masked.keys()):
        if k.lower() in SENSITIVE_KEYS:
            masked[k] = "***"
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

# ── Debit idempotency (P0) ─────────────────────────────────────────────────────
#
# debit_txn_log is the only durable record of whether a wallet debit may already
# have reached Pyro. Before any Pyro call, and before any stuck-row reset, we
# classify the attempt history of an Oracle reference:
#
#   OUTCOME_NONE     no Pyro call was ever (possibly) sent, or every attempt
#                    ended with a definitive non-success. Safe to (re)debit.
#   OUTCOME_SUCCESS  a debit already succeeded. Never debit again.
#   OUTCOME_UNKNOWN  a call may have reached Pyro but its outcome was never
#                    recorded (intent without result, timeout, parse failure,
#                    HTTP 5xx). Never debit again until manually reconciled.
#
# Ops can close an UNKNOWN by inserting an api_stage='MANUAL_RECONCILED' row for
# the reference: is_success='Y' means "debit did happen", 'N' means "it did not,
# safe to retry".

OUTCOME_NONE    = "NONE"
OUTCOME_SUCCESS = "SUCCESS"
OUTCOME_UNKNOWN = "UNKNOWN"

STAGE_INTENT            = "DEBIT_INTENT"
STAGE_DEBIT             = "DEBIT"
STAGE_RECONCILIATION    = "RECONCILIATION_REQUIRED"
STAGE_MANUAL_RECONCILED = "MANUAL_RECONCILED"
STAGE_IDEMPOTENT_SKIP   = "IDEMPOTENT_SKIP"

# DEBIT rows with these error classes are written before any HTTP request is sent.
_NOT_SENT_ERROR_CLASSES = {"TokenRefreshFailed", "RequestNotSent"}


def classify_debit_attempts(rows: List[dict]) -> Tuple[str, Optional[str]]:
    """Classify one reference's debit_txn_log rows (ordered by id ascending).

    Returns (outcome, pyro_txn_id_of_success_or_None).
    """
    state = OUTCOME_NONE
    success_pyro_id: Optional[str] = None

    for row in rows:
        stage      = row.get("api_stage")
        is_success = row.get("is_success")
        status     = row.get("pyro_status_code")

        if stage == STAGE_MANUAL_RECONCILED:
            if is_success == "Y":
                state = OUTCOME_SUCCESS
                success_pyro_id = row.get("pyro_txn_id") or success_pyro_id
            else:
                state = OUTCOME_NONE
            continue

        if stage == STAGE_IDEMPOTENT_SKIP:
            continue   # audit-only row, carries no new information

        if is_success == "Y":
            # DEBIT success, or RECONCILIATION_REQUIRED after a successful debit
            state = OUTCOME_SUCCESS
            success_pyro_id = row.get("pyro_txn_id") or success_pyro_id
            continue

        if state == OUTCOME_SUCCESS:
            continue   # a success is never undone by a later non-success row

        if stage == STAGE_INTENT:
            state = OUTCOME_UNKNOWN          # open until a definitive result arrives
        elif stage == STAGE_RECONCILIATION:
            state = OUTCOME_UNKNOWN
        elif stage == STAGE_DEBIT:
            if status is None or row.get("error_class") in _NOT_SENT_ERROR_CLASSES:
                state = OUTCOME_NONE         # nothing was sent to Pyro
            elif status == -1 or (row.get("response_http_code") or 0) >= 500:
                state = OUTCOME_UNKNOWN      # sent, outcome not known
            else:
                state = OUTCOME_NONE         # definitive Pyro rejection
        # any other stage is informational

    return state, success_pyro_id


@_pg_retry
def fetch_debit_attempt_outcomes(
    service_type: str, oracle_ref_ids: Sequence[str]
) -> Dict[str, Tuple[str, Optional[str]]]:
    """Classify the debit history for each Oracle reference.

    Raises on any DB error: callers must fail closed (no Pyro call, no reset).
    References with no log rows are returned as OUTCOME_NONE.
    """
    refs = sorted({str(r) for r in oracle_ref_ids if r is not None})
    if not refs:
        return {}
    sql = """
        SELECT oracle_ref_id, api_stage, is_success, pyro_status_code,
               response_http_code, pyro_txn_id, error_class
        FROM public.debit_txn_log
        WHERE service_type = %s
          AND oracle_ref_id = ANY(%s)
        ORDER BY oracle_ref_id, id
    """
    grouped: Dict[str, List[dict]] = {r: [] for r in refs}
    with get_pg_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(sql, (service_type, refs))
            for (ref, stage, is_success, status, http_code, pyro_id, err_cls) in cur.fetchall():
                grouped.setdefault(str(ref), []).append({
                    "api_stage": stage,
                    "is_success": is_success,
                    "pyro_status_code": status,
                    "response_http_code": http_code,
                    "pyro_txn_id": pyro_id,
                    "error_class": err_cls,
                })
    return {ref: classify_debit_attempts(rows) for ref, rows in grouped.items()}


def insert_debit_intent(
    service_type: str,
    oracle_ref_id: str,
    client_id: Optional[str],
    source_msisdn: Optional[str],
    dest_msisdn: Optional[str],
    amount: Optional[float],
) -> None:
    """Durably record that a Pyro debit call is about to be sent.

    Unlike insert_debit_txn_log this RAISES on failure: if the intent cannot be
    recorded, the caller must not call Pyro.
    """
    _do_insert_debit_txn_log((
        service_type, oracle_ref_id, client_id,
        source_msisdn, dest_msisdn, amount,
        STAGE_INTENT, None, 1,
        None, None, None,
        None, None, None,
        datetime.now(timezone.utc), None, None,
        "N", "N", None, None,
    ))


async def async_fetch_debit_attempt_outcomes(service_type, oracle_ref_ids):
    return await asyncio.to_thread(fetch_debit_attempt_outcomes, service_type, oracle_ref_ids)


async def async_insert_debit_intent(**kwargs) -> None:
    await asyncio.to_thread(insert_debit_intent, **kwargs)
