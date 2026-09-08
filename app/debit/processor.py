"""Debit Processor Concurrency & Orchestration Module (Phase 13).

Architecture & Concurrency Design:
- Each debit service (FANCYSALE, SIMSWAP, ESIM) has a dedicated execution lock to prevent
  overlapping batch executions between scheduled background jobs (APScheduler) and manual
  operator triggers (POST /admin/trigger-debit/{service_type}).
- Lock implementation: asyncio.Lock managed per canonical (uppercase) service type.

CRITICAL ARCHITECTURAL LIMITATION:
- An asyncio.Lock is STRICTLY PROCESS-LOCAL (tied to the memory space of a single Python event loop).
- Current Deployment Topology: Exactly one Uvicorn worker in one container instance.
  In this single-process deployment, asyncio.Lock completely prevents scheduler and manual trigger overlap.
- Future Scale-Out Constraint:
  If the application is scaled out to multiple Uvicorn worker processes (e.g. `uvicorn --workers N`)
  or across multiple container replicas / Kubernetes pods, asyncio.Lock WILL NOT provide distributed
  mutual exclusion across processes or nodes.
- Scale-Out Remediation:
  Before scaling beyond a single worker process/container, replace or augment this mechanism with
  a distributed locking system, such as PostgreSQL transaction/session advisory locks
  (`pg_try_advisory_lock(hashtext(service_type))`) or a Redis-based distributed lock (Redlock).
"""

import asyncio
import logging
from typing import Optional

from app.context import ExecutionContext
from app.db.debit_log import async_insert_debit_txn_log
from app.debit.ownership import ownership_tracker
from app.debit.pyro_client import DEBIT_SUCCESS_CODE, wallet_adjustment
from app.debit.services.base import DebitServiceAdapter

logger = logging.getLogger(__name__)

# ── Per-service concurrency lock to prevent overlapping batch executions in the same worker ──
_SERVICE_LOCKS: dict[str, asyncio.Lock] = {}
_SERVICE_LOCKS_GUARD = asyncio.Lock()


async def get_service_lock(service_type: str) -> asyncio.Lock:
    """Retrieve or create an asyncio.Lock for the specified service type.

    Normalizes service_type to uppercase to prevent case variations from evading lock mutual exclusion.
    Thread-safe and coroutine-safe initialization via _SERVICE_LOCKS_GUARD.

    NOTE: This lock is process-local. Refer to module docstring for multi-worker/multi-replica limitations.
    """
    canonical_type = service_type.strip().upper()
    async with _SERVICE_LOCKS_GUARD:
        if canonical_type not in _SERVICE_LOCKS:
            _SERVICE_LOCKS[canonical_type] = asyncio.Lock()
        return _SERVICE_LOCKS[canonical_type]


async def is_service_locked(service_type: str) -> bool:
    """Return True if the service lock is currently acquired by an active batch."""
    lock = await get_service_lock(service_type)
    return lock.locked()


def _reset_service_locks() -> None:
    """Internal test fixture helper to clear lock state between unit tests."""
    _SERVICE_LOCKS.clear()


# Alias for backwards compatibility
_get_service_lock = get_service_lock


async def run_debit_batch(
    adapter: DebitServiceAdapter,
    context: Optional[ExecutionContext] = None,
) -> dict:
    
    svc = adapter.service_type

    if not adapter.enabled:
        logger.info("[%s] Debit service disabled — skipping batch", svc)
        return {"service_type": svc, "processed": 0, "success": 0, "failed": 0, "reconciliation_required": 0}

    lock = await get_service_lock(svc)
    was_locked = lock.locked()
    if was_locked:
        logger.warning(
            "[%s] Debit batch already in progress (locked) — waiting for active batch to release lock",
            svc,
        )

    async with lock:
        if was_locked:
            logger.info("[%s] Lock acquired after waiting — proceeding with batch", svc)
        # ── 1. Claim eligible records ─────────────────────────────────────────
        records = await asyncio.to_thread(adapter.fetch_and_claim, adapter.batch_size, context)

        if not records:
            logger.info("[%s] Debit processor: no eligible records", svc)
            return {"service_type": svc, "processed": 0, "success": 0, "failed": 0, "reconciliation_required": 0}

        refs = [adapter.get_record_ref(record) for record in records]
        ownership_tracker.acquire(svc, refs)

        try:
            logger.info("[%s] Debit processor: processing %d record(s)", svc, len(records))
            success = failed = reconciliation_count = 0

            for record in records:
                ref = adapter.get_record_ref(record)
                try:
                    # ── 2. Map record → Pyro params ───────────────────────────────────
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

                    # ── 3. Call Pyro ──────────────────────────────────────────────────
                    response = await wallet_adjustment(
                        oracle_ref_id=ref,
                        service_type=svc,
                        token_manager=adapter.token_manager,
                        attempt_no=1,
                        **params,
                    )

                    sc   = response.get("statusCode")
                    data = response.get("data", {})

                    # ── 4. Handle outcome ─────────────────────────────────────────────
                    if sc in DEBIT_SUCCESS_CODE and response.get("status") == "SUCCESS":
                        pyro_txn_id = str(data.get("pyroId", ""))
                        bal_before  = data.get("balanceBefore", 0.0)
                        bal_after   = data.get("balanceAfter",  0.0)
                        remarks = (
                            f"[200] SUCCESS {response.get('message', '')} "
                            f"pyroId={pyro_txn_id} "
                            f"balBefore={bal_before} balAfter={bal_after}"
                        )
                        try:
                            await asyncio.to_thread(adapter.mark_success, record, pyro_txn_id, remarks)
                            logger.info("[%s] ref=%s SUCCESS pyroId=%s", svc, ref, pyro_txn_id)
                            success += 1
                        except Exception as writeback_exc:
                            logger.critical(
                                "[%s] CRITICAL: Financial debit succeeded (pyroId=%s) but writeback FAILED for ref=%s: %s. "
                                "Manual reconciliation required to prevent double debits!",
                                svc, pyro_txn_id, ref, writeback_exc,
                            )
                            # Durably record writeback failure in Postgres debit_txn_log
                            await async_insert_debit_txn_log(
                                service_type=svc,
                                oracle_ref_id=ref,
                                client_id=params.get("client_id"),
                                source_msisdn=params.get("source_msisdn"),
                                dest_msisdn=params.get("dest_msisdn"),
                                amount=params.get("amount"),
                                api_stage="RECONCILIATION_REQUIRED",
                                api_endpoint=None,
                                attempt_no=1,
                                request_body=None,
                                response_http_code=response.get("http_status"),
                                response_body=None,
                                pyro_status_code=sc,
                                pyro_status_text=response.get("status"),
                                pyro_txn_id=pyro_txn_id,
                                call_started_at=None,
                                call_ended_at=None,
                                duration_ms=None,
                                is_success="Y",
                                is_perm_failure="Y",
                                error_class=type(writeback_exc).__name__,
                                error_detail=str(writeback_exc),
                            )
                            # Mark request in Oracle as requiring reconciliation to prevent cleanup reset
                            try:
                                await asyncio.to_thread(
                                    adapter.mark_reconciliation_required, record, pyro_txn_id, str(writeback_exc)
                                )
                            except Exception as recon_exc:
                                logger.critical(
                                    "[%s] CRITICAL: Failed to mark_reconciliation_required in Oracle for ref=%s: %s",
                                    svc, ref, recon_exc,
                                )
                            failed += 1
                            reconciliation_count += 1
                            continue
                    else:
                        remarks = f"[{sc}] {response.get('message', 'Unknown failure')}"
                        await asyncio.to_thread(adapter.mark_failed, record, remarks[:2000])
                        logger.warning("[%s] ref=%s FAILED: %s", svc, ref, remarks)
                        failed += 1
                finally:
                    ownership_tracker.release(svc, ref)

            summary = {
                "service_type":            svc,
                "processed":               len(records),
                "success":                 success,
                "failed":                  failed,
                "reconciliation_required": reconciliation_count,
                "execution_id":            context.execution_id if context else None,
            }
            logger.info("[%s] Debit batch complete — %s", svc, summary)
            return summary
        finally:
            ownership_tracker.release_all(svc, refs)