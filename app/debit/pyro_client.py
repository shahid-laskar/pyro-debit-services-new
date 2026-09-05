import json
import logging
from datetime import datetime, timezone
from typing import Optional

import httpx

from app.auth.token_manager import PyroAuthService
from app.config import settings
from app.db.debit_log import _mask_debit_body, async_insert_debit_txn_log
from app.encryption import decrypt, encrypt

logger = logging.getLogger(__name__)

DEBIT_SUCCESS_CODE   = {200, 2000}
DEBIT_ENDPOINT_PATH  = "/erp-stock-api/service-wallet-adjustment"


def _parse_pyro_response(resp: httpx.Response, label: str,
                          secret_key: str) -> dict:
   
    raw = resp.text.strip()
    logger.debug("%s raw response: %s", label, raw[:300])

    try:
        return resp.json()
    except Exception as json_err:
        logger.debug("%s: plain JSON failed (%s) — trying decrypt", label, json_err)

    try:
        return json.loads(decrypt(raw, secret_key))
    except Exception as dec_err:
        logger.error("%s: both plain JSON and decrypt failed. Raw: %s", label, raw[:300])
        return {
            "statusCode": -1,
            "status":     "ERROR",
            "message":    f"Response parse failed: {dec_err}",
        }


async def wallet_adjustment(
    # Oracle source identifiers (for logging only)
    oracle_ref_id:  str,
    service_type:   str,

    # Pyro API fields
    client_id:      str,       # SS_REQUEST_ID
    source_msisdn:  str,       # CTOPUPNO as string
    dest_msisdn:    str,       # FANCY_NO as string
    amount:         float,
    mpin:           str,       # DECRYPTED plain text
    remarks:        str,       # MODULE_TYPE value

    # Auth — per-service instance, never the FRC token_manager
    token_manager:  PyroAuthService,

    attempt_no:     int = 1,
) -> dict:
    
    url        = f"{settings.pyro_base_url}{DEBIT_ENDPOINT_PATH}"
    started_at = datetime.now(timezone.utc)
    label      = f"DEBIT [{service_type}] ref={oracle_ref_id}"

    # ── 1. Ensure valid access token ──────────────────────────────────────────
    access_token = await token_manager.get_access_token()
    if not access_token:
        ended_at = datetime.now(timezone.utc)
        duration = int((ended_at - started_at).total_seconds() * 1000)
        data = {"statusCode": -1, "status": "ERROR",
                "message": "Failed to obtain access token"}
        await async_insert_debit_txn_log(
            service_type=service_type,
            oracle_ref_id=oracle_ref_id,
            client_id=client_id,
            source_msisdn=source_msisdn,
            dest_msisdn=dest_msisdn,
            amount=amount,
            api_stage="DEBIT",
            api_endpoint=url,
            attempt_no=attempt_no,
            request_body=None,
            response_http_code=None,
            response_body=json.dumps(data),
            pyro_status_code=-1,
            pyro_status_text="ERROR",
            pyro_txn_id=None,
            call_started_at=started_at,
            call_ended_at=ended_at,
            duration_ms=duration,
            is_success="N",
            is_perm_failure="N",
            error_class="TokenRefreshFailed",
            error_detail="get_access_token returned None",
        )
        logger.error("%s: access token unavailable — aborting", label)
        return data

    # ── 2. Build and encrypt payload ──────────────────────────────────────────
    payload = {
        "clientId":     client_id,
        "sourceMsisdn": source_msisdn,
        "destMsisdn":   dest_msisdn,
        "amount":       amount,
        "mpin":         mpin,
        "remarks":      remarks,
        "serviceType":  service_type,
    }
    # Use per-service secret_key for body encryption, NOT settings.pyro_secret_key
    encrypted_body = encrypt(json.dumps(payload), token_manager.secret_key)
    masked_body    = _mask_debit_body(payload)

    headers = {
        "apiKey":       token_manager.api_key,
        "accessToken":  access_token,
        "sessionToken": token_manager.session_token or "",
    }

    # ── 3. HTTP POST ──────────────────────────────────────────────────────────
    try:
        async with httpx.AsyncClient(verify=True, timeout=30.0) as client:
            resp = await client.post(url, headers=headers, content=encrypted_body)

        ended_at    = datetime.now(timezone.utc)
        duration    = int((ended_at - started_at).total_seconds() * 1000)
        data        = _parse_pyro_response(resp, label, token_manager.secret_key)
        status_code = data.get("statusCode", -1)
        status_text = data.get("status", "")
        is_success  = "Y" if (status_code in DEBIT_SUCCESS_CODE
                               and status_text == "SUCCESS") else "N"
        pyro_txn_id = str(data.get("data", {}).get("pyroId", "")) or None

        await async_insert_debit_txn_log(
            service_type=service_type,
            oracle_ref_id=oracle_ref_id,
            client_id=client_id,
            source_msisdn=source_msisdn,
            dest_msisdn=dest_msisdn,
            amount=amount,
            api_stage="DEBIT",
            api_endpoint=url,
            attempt_no=attempt_no,
            request_body=masked_body,
            response_http_code=resp.status_code,
            response_body=json.dumps(data),
            pyro_status_code=status_code,
            pyro_status_text=status_text,
            pyro_txn_id=pyro_txn_id,
            call_started_at=started_at,
            call_ended_at=ended_at,
            duration_ms=duration,
            is_success=is_success,
            is_perm_failure="N",
        )
        logger.info("%s statusCode=%s duration=%dms", label, status_code, duration)
        return data

    except httpx.TimeoutException as exc:
        ended_at = datetime.now(timezone.utc)
        duration = int((ended_at - started_at).total_seconds() * 1000)
        await async_insert_debit_txn_log(
            service_type=service_type,
            oracle_ref_id=oracle_ref_id,
            client_id=client_id,
            source_msisdn=source_msisdn,
            dest_msisdn=dest_msisdn,
            amount=amount,
            api_stage="DEBIT",
            api_endpoint=url,
            attempt_no=attempt_no,
            request_body=masked_body,
            response_http_code=None,
            response_body=None,
            pyro_status_code=-1,
            pyro_status_text="TIMEOUT",
            pyro_txn_id=None,
            call_started_at=started_at,
            call_ended_at=ended_at,
            duration_ms=duration,
            is_success="N",
            is_perm_failure="N",
            error_class="httpx.TimeoutException",
            error_detail=str(exc),
        )
        logger.error("%s timed out", label)
        return {"statusCode": -1, "status": "ERROR", "message": "Request timed out"}

    except Exception as exc:
        ended_at = datetime.now(timezone.utc)
        duration = int((ended_at - started_at).total_seconds() * 1000)
        await async_insert_debit_txn_log(
            service_type=service_type,
            oracle_ref_id=oracle_ref_id,
            client_id=client_id,
            source_msisdn=source_msisdn,
            dest_msisdn=dest_msisdn,
            amount=amount,
            api_stage="DEBIT",
            api_endpoint=url,
            attempt_no=attempt_no,
            request_body=masked_body,
            response_http_code=None,
            response_body=None,
            pyro_status_code=-1,
            pyro_status_text="ERROR",
            pyro_txn_id=None,
            call_started_at=started_at,
            call_ended_at=ended_at,
            duration_ms=duration,
            is_success="N",
            is_perm_failure="N",
            error_class=type(exc).__name__,
            error_detail=str(exc),
        )
        logger.error("%s error: %s", label, exc)
        return {"statusCode": -1, "status": "ERROR", "message": str(exc)}