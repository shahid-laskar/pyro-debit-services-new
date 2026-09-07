import logging
from typing import Any, List, Optional, Set, Tuple

from app.auth.token_manager import PyroAuthService
from app.context import ExecutionContext
from app.db.oracle import get_oracle_conn
from app.debit.ownership import build_active_exclusion_predicate, ownership_tracker
from app.debit.services.base import WritebackError

logger = logging.getLogger(__name__)

# ── Oracle state constants ────────────────────────────────────────────────────
VS_STATUS_N  = "N"      # new / eligible
VS_STATUS_P  = "P"      # processing (set by us)
VS_STATUS_Y  = "Y"      # success    (set by us)
VS_STATUS_R  = "R"      # rejected   (set by us on Pyro failure)
VS_STATUS_QM = "QM"     # MPIN error — Sanchar Mitra sets; we re-pick after fix
VS_STATUS_QB = "QB"     # Balance error — Sanchar Mitra sets; we re-pick after top-up

FETCH_ELIGIBLE = (VS_STATUS_N, VS_STATUS_QM, VS_STATUS_QB)

# ── Q002 Claim Query with REFID, status guard, and CIRCLE_CODE guard ───────────
FANCYSALE_CLAIM_SQL = """
UPDATE CAF_ADMIN.VANITYSALE_FRANCH_DATA
SET CAF_ENTRY_DONE = 'P',
    CAF_ENTRY_DATE = SYSDATE,
    PYRO_REMARKS = 'Processing started'
WHERE REFID = :refid
  AND CAF_ENTRY_DONE IN ('N','QM','QB')
  AND CIRCLE_CODE = :circle_code
""".strip()


def build_fancysale_claim_query() -> str:
    """Return Q002 claim query SQL text with circle guard and PK."""
    return FANCYSALE_CLAIM_SQL


def build_fancysale_candidate_query(
    batch_size: int,
    context: Optional[ExecutionContext] = None,
) -> Tuple[str, dict]:
    """Construct candidate discovery query Q001 and parameter dictionary.

    If context is FILTERED, injects:
        AND CIRCLE_CODE IN (:c_0, :c_1, ...)
    If context is ALL (or None), omits circle filtering to preserve nationwide behavior.
    Preserves FIFO ordering (TRANS_DATE ASC).
    """
    bind_params: dict = {"batch_size": batch_size}
    circle_predicate = ""

    if context and context.mode == "FILTERED" and context.circle_codes:
        placeholders = []
        for idx, code in enumerate(context.circle_codes):
            param_name = f"c_{idx}"
            placeholders.append(f":{param_name}")
            bind_params[param_name] = code
        circle_predicate = f"  AND CIRCLE_CODE IN ({', '.join(placeholders)})\n"

    sql = f"""
SELECT *
FROM (
    SELECT
        REFID,
        CTOPUPNO,
        FANCY_NO,
        AMOUNT,
        CAF_ADMIN.F_DECRYPT(MPIN) AS plain_mpin,
        MPIN_LENGTH,
        SS_REQUEST_ID,
        CSCCODE,
        CIRCLE_CODE,
        TRANS_DATE,
        MODULE_TYPE,
        CAF_ENTRY_DONE
    FROM CAF_ADMIN.VANITYSALE_FRANCH_DATA
    WHERE CAF_ENTRY_DONE IN ('N', 'QM', 'QB')
{circle_predicate}    ORDER BY TRANS_DATE ASC
)
WHERE ROWNUM <= :batch_size
    """.strip()
    return sql, bind_params


class FancySaleAdapter:
    
    service_type  = "FANCYSALE"
    implemented   = True

    def __init__(
        self,
        token_manager:    PyroAuthService,
        enabled:          bool = True,
        batch_size:       int  = 50,
        interval_minutes: int  = 30,
        stuck_minutes:    int  = 10,
    ):
        self.token_manager    = token_manager
        self.enabled          = enabled
        self.batch_size       = batch_size
        self.interval_minutes = interval_minutes
        self.stuck_minutes    = stuck_minutes

    # ── Interface implementation ───────────────────────────────────────────────

    def fetch_and_claim(
        self,
        batch_size: int,
        context: Optional[ExecutionContext] = None,
    ) -> List[dict]:
        
        if not self.enabled:
            return []

        ctx = context if context is not None else ExecutionContext.create(
            source="SCHEDULED",
            service_type=self.service_type,
        )

        select_sql, bind_params = build_fancysale_candidate_query(batch_size, ctx)
        claim_sql = build_fancysale_claim_query()

        with get_oracle_conn() as conn:
            cur = conn.cursor()

            cur.execute(select_sql, bind_params)
            cols = [c[0].lower() for c in cur.description]
            candidates = [dict(zip(cols, row)) for row in cur.fetchall()]

            if not candidates:
                return []

            claimed = []
            for row in candidates:
                circle_code = row.get("circle_code")
                # Defense-in-depth: skip row if circle not permitted in this context
                if not ctx.is_circle_allowed(circle_code):
                    logger.warning(
                        "[FANCYSALE] Candidate REFID=%s has circle_code=%s disallowed in %s mode",
                        row.get("refid"), circle_code, ctx.mode,
                    )
                    continue

                cur.execute(claim_sql, {
                    "refid": row["refid"],
                    "circle_code": circle_code,
                })
                if cur.rowcount == 1:
                    claimed.append(row)
                elif cur.rowcount > 1:
                    logger.critical(
                        "[FANCYSALE] Critical identity anomaly: REFID=%s modified %d rows (expected 1). Discarding claim.",
                        row["refid"], cur.rowcount,
                    )

            conn.commit()

        if len(claimed) < len(candidates):
            logger.warning(
                "[FANCYSALE] fetch_and_claim [%s]: %d candidate(s) found, %d claimed "
                "(%d lost to concurrent worker or skipped)",
                ctx.execution_id[:8], len(candidates), len(claimed), len(candidates) - len(claimed),
            )
        else:
            logger.info(
                "[FANCYSALE] fetch_and_claim [%s]: %d row(s) claimed (mode=%s, zones=%s)",
                ctx.execution_id[:8], len(claimed), ctx.mode, ctx.zones_display,
            )
        return claimed

    def map_to_pyro_params(self, record: dict) -> dict:
        
        mpin = record.get("plain_mpin") or ""
        mpin = str(mpin).strip()
        expected_len = int(record.get("mpin_length") or 0)

        if not mpin:
            raise ValueError(
                f"REFID={record['refid']}: plain_mpin is empty after Oracle F_DECRYPT"
            )
        if expected_len and len(mpin) != expected_len:
            raise ValueError(
                f"REFID={record['refid']}: MPIN length mismatch — "
                f"got {len(mpin)}, expected {expected_len}"
            )

        raw_ctopupno = record.get("ctopupno")
        raw_fancy_no = record.get("fancy_no")
        if raw_ctopupno is None:
            raise ValueError(f"REFID={record['refid']}: CTOPUPNO is NULL in Oracle")
        if raw_fancy_no is None:
            raise ValueError(f"REFID={record['refid']}: FANCY_NO is NULL in Oracle")
        source_msisdn = str(int(raw_ctopupno))
        dest_msisdn   = str(int(raw_fancy_no))
        amount        = float(record["amount"])
        client_id     = str(record["ss_request_id"])
        remarks       = str(record.get("module_type") or "FANCYSALE")

        return dict(
            client_id=client_id,
            source_msisdn=source_msisdn,
            dest_msisdn=dest_msisdn,
            amount=amount,
            mpin=mpin,
            remarks=remarks,
        )

    def get_record_ref(self, record: dict) -> str:
        return str(record.get("refid", "UNKNOWN"))

    def mark_success(self, record: dict, pyro_txn_id: str, remarks: str) -> None:
        """Write Y + TRANSACTION_ID + PYRO_REMARKS + PROCESSED_SM to Oracle."""
        sql = """
            UPDATE CAF_ADMIN.VANITYSALE_FRANCH_DATA
            SET    CAF_ENTRY_DONE = 'Y',
                   TRANSACTION_ID = :pyro_txn_id,
                   PYRO_REMARKS   = :remarks,
                   PROCESSED_SM   = 'Y'
            WHERE  REFID          = :refid
              AND  CAF_ENTRY_DONE = 'P'
        """
        try:
            with get_oracle_conn() as conn:
                cur = conn.cursor()
                cur.execute(sql, {
                    "pyro_txn_id": pyro_txn_id[:100] if pyro_txn_id else None,
                    "remarks":     remarks[:2000],
                    "refid":       record["refid"],
                })
                if cur.rowcount == 0:
                    raise WritebackError(
                        f"[FANCYSALE] mark_success REFID={record['refid']} rowcount=0 (row not in status 'P')"
                    )
                conn.commit()
        except WritebackError:
            raise
        except Exception as exc:
            logger.critical(
                "[FANCYSALE] mark_success Oracle DB exception for REFID=%s: %s",
                record["refid"], exc,
            )
            raise WritebackError(
                f"[FANCYSALE] mark_success DB failure for REFID={record['refid']}: {exc}"
            ) from exc

    def mark_reconciliation_required(
        self, record: dict, pyro_txn_id: str, error_detail: str
    ) -> None:
        """Emergency update to mark record in Oracle as requiring reconciliation, preventing cleanup reset."""
        sql = """
            UPDATE CAF_ADMIN.VANITYSALE_FRANCH_DATA
            SET    PYRO_REMARKS   = :remarks
            WHERE  REFID          = :refid
              AND  CAF_ENTRY_DONE = 'P'
        """
        remarks = f"RECONCILIATION_REQUIRED pyroId={pyro_txn_id}: {error_detail}"[:2000]
        try:
            with get_oracle_conn() as conn:
                cur = conn.cursor()
                cur.execute(sql, {"remarks": remarks, "refid": record["refid"]})
                conn.commit()
        except Exception as exc:
            logger.critical(
                "[FANCYSALE] mark_reconciliation_required DB failure for REFID=%s: %s",
                record["refid"], exc,
            )
            raise

    def mark_failed(self, record: dict, remarks: str) -> None:
        """Write R + PYRO_REMARKS to Oracle."""
        sql = """
            UPDATE CAF_ADMIN.VANITYSALE_FRANCH_DATA
            SET    CAF_ENTRY_DONE = 'R',
                   PYRO_REMARKS   = :remarks
            WHERE  REFID          = :refid
              AND  CAF_ENTRY_DONE = 'P'
        """
        try:
            with get_oracle_conn() as conn:
                cur = conn.cursor()
                cur.execute(sql, {
                    "remarks": remarks[:2000],
                    "refid":   record["refid"],
                })
                if cur.rowcount == 0:
                    logger.warning(
                        "[FANCYSALE] mark_failed: REFID=%s rowcount=0 "
                        "(already moved out of P?)", record["refid"]
                    )
                conn.commit()
        except Exception as exc:
            logger.error(
                "[FANCYSALE] mark_failed DB failure (non-fatal) REFID=%s: %s",
                record["refid"], exc
            )

    def reset_stuck_processing(
        self, stuck_minutes: int, active_refs: Optional[Set[Any]] = None
    ) -> int:
        
        if not self.enabled:
            return 0

        if active_refs is None:
            active_refs = ownership_tracker.get_active(self.service_type)

        exclusion_sql, exclusion_params = build_active_exclusion_predicate(
            "REFID", active_refs
        )

        sql = f"""
            UPDATE CAF_ADMIN.VANITYSALE_FRANCH_DATA
            SET    CAF_ENTRY_DONE = 'N',
                   PYRO_REMARKS   = 'Reset: stuck in processing state'
            WHERE  CAF_ENTRY_DONE  = 'P'
              AND  CAF_ENTRY_DATE IS NOT NULL
              AND  CAF_ENTRY_DATE  < SYSDATE - (:stuck_minutes / 1440)
              AND  (PYRO_REMARKS IS NULL OR PYRO_REMARKS NOT LIKE 'RECONCILIATION_REQUIRED%')
{exclusion_sql}        """.strip()
        params = {"stuck_minutes": stuck_minutes, **exclusion_params}
        try:
            with get_oracle_conn() as conn:
                cur = conn.cursor()
                cur.execute(sql, params)
                count = cur.rowcount
                conn.commit()
            if count:
                logger.warning(
                    "[FANCYSALE] reset_stuck_processing: reset %d stuck-P row(s) "
                    "older than %d min back to N (excluded %d active in-flight)",
                    count, stuck_minutes, len(active_refs),
                )
            return count
        except Exception as exc:
            logger.error("[FANCYSALE] reset_stuck_processing error: %s", exc)
            return 0