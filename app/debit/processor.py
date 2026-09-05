import asyncio
import logging

from app.db.debit_log import async_insert_debit_txn_log
from app.debit.pyro_client import DEBIT_SUCCESS_CODE, wallet_adjustment
from app.debit.services.base import DebitServiceAdapter

logger = logging.getLogger(__name__)


async def run_debit_batch(adapter: DebitServiceAdapter) -> dict:
    
    svc = adapter.service_type

    if not adapter.enabled:
        logger.info("[%s] Debit service disabled — skipping batch", svc)
        return {"service_type": svc, "processed": 0, "success": 0, "failed": 0}

    # ── 1. Claim eligible records ─────────────────────────────────────────────
    records = await asyncio.to_thread(adapter.fetch_and_claim, adapter.batch_size)

    if not records:
        logger.info("[%s] Debit processor: no eligible records", svc)
        return {"service_type": svc, "processed": 0, "success": 0, "failed": 0}

    logger.info("[%s] Debit processor: processing %d record(s)", svc, len(records))
    success = failed = 0

    for record in records:
        ref = adapter.get_record_ref(record)

        # ── 2. Map record → Pyro params ───────────────────────────────────────
        try:
            params = adapter.map_to_pyro_params(record)
        except (ValueError, Exception) as exc:
            err_msg = f"[MPIN_ERR] {exc}"
            logger.error("[%s] ref=%s map_to_pyro_params failed: %s", svc, ref, exc)
            await asyncio.to_thread(adapter.mark_failed, record, err_msg[:2000])
            # Log to audit table as permanent failure (data problem, no Pyro call made)
            await async_insert_debit_txn_log(
                service_type=svc,
                oracle_ref_id=ref,
                client_id=None,
                source_msisdn=None,
                dest_msisdn=None,
                amount=None,
                api_stage="DEBIT",
                api_endpoint=None,
                attempt_no=1,
                request_body=None,
                response_http_code=None,
                response_body=None,
                pyro_status_code=None,
                pyro_status_text=None,
                pyro_txn_id=None,
                call_started_at=None,
                call_ended_at=None,
                duration_ms=None,
                is_success="N",
                is_perm_failure="Y",
                error_class=type(exc).__name__,
                error_detail=str(exc),
            )
            failed += 1
            continue

        # ── 3. Call Pyro ──────────────────────────────────────────────────────
        response = await wallet_adjustment(
            oracle_ref_id=ref,
            service_type=svc,
            token_manager=adapter.token_manager,
            attempt_no=1,
            **params,
        )

        sc   = response.get("statusCode")
        data = response.get("data", {})

        # ── 4. Handle outcome ─────────────────────────────────────────────────
        if sc in DEBIT_SUCCESS_CODE and response.get("status") == "SUCCESS":
            pyro_txn_id = str(data.get("pyroId", ""))
            bal_before  = data.get("balanceBefore", 0.0)
            bal_after   = data.get("balanceAfter",  0.0)
            remarks = (
                f"[200] SUCCESS {response.get('message', '')} "
                f"pyroId={pyro_txn_id} "
                f"balBefore={bal_before} balAfter={bal_after}"
            )
            await asyncio.to_thread(adapter.mark_success, record, pyro_txn_id, remarks)
            logger.info("[%s] ref=%s SUCCESS pyroId=%s", svc, ref, pyro_txn_id)
            success += 1
        else:
            remarks = f"[{sc}] {response.get('message', 'Unknown failure')}"
            await asyncio.to_thread(adapter.mark_failed, record, remarks[:2000])
            logger.warning("[%s] ref=%s FAILED: %s", svc, ref, remarks)
            failed += 1

    summary = {
        "service_type": svc,
        "processed":    len(records),
        "success":      success,
        "failed":       failed,
    }
    logger.info("[%s] Debit batch complete — %s", svc, summary)
    return summary