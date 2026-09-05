import logging
from typing import List

from app.auth.token_manager import PyroAuthService
from app.db.oracle import get_oracle_conn

logger = logging.getLogger(__name__)

# ── Oracle state constants ─────────────────────────────────────────────────────
_STATUS_N  = "N"       # new / eligible
_STATUS_P  = "P"       # processing (set by us)
_STATUS_Y  = "Y"       # success    (set by us)
_STATUS_R  = "R"       # rejected   (set by us on Pyro failure)
_STATUS_QM = "QM"      # MPIN error — Sanchar Mitra sets; we re-pick after fix
_STATUS_QB = "QB"      # balance error — Sanchar Mitra sets; we re-pick after top-up

_MODULE_TYPE = "ESIM"


class EsimAdapter:    

    service_type = "ESIM"
    implemented  = True

    def __init__(
        self,
        token_manager:    PyroAuthService,
        enabled:          bool = False,
        batch_size:       int  = 200,
        interval_minutes: int  = 30,
        stuck_minutes:    int  = 10,
    ):
        self.token_manager    = token_manager
        self.enabled          = enabled
        self.batch_size       = batch_size
        self.interval_minutes = interval_minutes
        self.stuck_minutes    = stuck_minutes

    # ── Interface implementation ───────────────────────────────────────────────

    def fetch_and_claim(self, batch_size: int) -> List[dict]:
        
        if not self.enabled:
            return []

        select_sql = """
            SELECT *
            FROM (
                SELECT
                    ID,
                    REFID,
                    CTOPUPNO,
                    GSMNUMBER,
                    SIMNUMBER,
                    AMOUNT,
                    CAF_ADMIN.F_DECRYPT(MPIN) AS plain_mpin,
                    MPIN_LENGTH,
                    SS_REQUEST_ID,
                    MODULE_TYPE,
                    REQUEST_DATE,
                    AMOUNT_DEDUCT_FLAG,
                    CIRCLE_CODE,
                    DEALERCODE,
                    SWAP_TYPE,
                    SOURCE
                FROM CAF_ADMIN.SIMSWAP_AMOUNT_DEDUCT_REQUESTS
                WHERE AMOUNT_DEDUCT_FLAG IN ('N', 'QM', 'QB')
                  AND MODULE_TYPE         = 'ESIM'                 
                ORDER BY REQUEST_DATE ASC
            )
            WHERE ROWNUM <= :batch_size
        """

        claim_sql = """
            UPDATE CAF_ADMIN.SIMSWAP_AMOUNT_DEDUCT_REQUESTS
            SET    AMOUNT_DEDUCT_FLAG    = 'P',
                   AMOUNT_DEDUCT_DATE    = SYSDATE,
                   AMOUNT_DEDUCT_REMARKS = 'Processing started'
            WHERE  ID                    = :id
              AND  AMOUNT_DEDUCT_FLAG    IN ('N', 'QM', 'QB')
              AND  MODULE_TYPE           = 'ESIM'
        """

        with get_oracle_conn() as conn:
            cur = conn.cursor()

            cur.execute(select_sql, batch_size=batch_size)
            cols       = [c[0].lower() for c in cur.description]
            candidates = [dict(zip(cols, row)) for row in cur.fetchall()]

            if not candidates:
                return []

            claimed = []
            for row in candidates:
                cur.execute(claim_sql, {"id": row["id"]})
                if cur.rowcount == 1:
                    claimed.append(row)

            conn.commit()

        lost = len(candidates) - len(claimed)
        if lost:
            logger.warning(
                "[ESIM] fetch_and_claim: %d candidate(s) found, %d claimed "
                "(%d lost to concurrent worker)",
                len(candidates), len(claimed), lost,
            )
        else:
            logger.info("[ESIM] fetch_and_claim: %d row(s) claimed", len(claimed))

        return claimed

    def map_to_pyro_params(self, record: dict) -> dict:
       
        record_id = record.get("id", "UNKNOWN")

        mpin         = str(record.get("plain_mpin") or "").strip()
        expected_len = int(record.get("mpin_length") or 0)

        if not mpin:
            raise ValueError(
                f"ID={record_id}: plain_mpin is empty after Oracle F_DECRYPT"
            )
        if expected_len and len(mpin) != expected_len:
            raise ValueError(
                f"ID={record_id}: MPIN length mismatch — "
                f"got {len(mpin)}, expected {expected_len}"
            )

        raw_ctopupno = record.get("ctopupno")
        gsmnumber    = record.get("gsmnumber")
        if raw_ctopupno is None:
            raise ValueError(f"ID={record_id}: CTOPUPNO is NULL in Oracle")
        if not gsmnumber:
            raise ValueError(f"ID={record_id}: GSMNUMBER is NULL/empty in Oracle")

        source_msisdn = str(int(raw_ctopupno))
        dest_msisdn   = str(gsmnumber).strip()
        amount        = float(record["amount"])
        client_id     = str(record["ss_request_id"])
        remarks       = str(record.get("module_type") or _MODULE_TYPE)

        return dict(
            client_id=client_id,
            source_msisdn=source_msisdn,
            dest_msisdn=dest_msisdn,
            amount=amount,
            mpin=mpin,
            remarks=remarks,
        )

    def get_record_ref(self, record: dict) -> str:
        """Use the identity PK (ID) as the canonical log reference."""
        return str(record.get("id", "UNKNOWN"))

    def mark_success(self, record: dict, pyro_txn_id: str, remarks: str) -> None:
        
        record_id = record["id"]
        gsmnumber = str(record.get("gsmnumber") or "").strip()

        # ── Phase 1: primary writeback ────────────────────────────────────────
        primary_sql = """
            UPDATE CAF_ADMIN.SIMSWAP_AMOUNT_DEDUCT_REQUESTS
            SET    AMOUNT_DEDUCT_FLAG    = 'Y',
                   TRANSACTION_ID        = :pyro_txn_id,
                   AMOUNT_DEDUCT_DATE    = SYSDATE,
                   AMOUNT_DEDUCT_REMARKS = :remarks
            WHERE  ID                    = :id
              AND  AMOUNT_DEDUCT_FLAG    = 'P'
        """
        try:
            with get_oracle_conn() as conn:
                cur = conn.cursor()
                cur.execute(primary_sql, {
                    "pyro_txn_id": (pyro_txn_id or "")[:50],
                    "remarks":     remarks[:200],
                    "id":          record_id,
                })
                if cur.rowcount == 0:
                    logger.warning(
                        "[ESIM] mark_success: ID=%s rowcount=0 "
                        "(already moved out of P?)", record_id
                    )
                conn.commit()
        except Exception as exc:
            logger.error(
                "[ESIM] mark_success primary DB failure (non-fatal) ID=%s: %s",
                record_id, exc,
            )
            return  # Secondary update only makes sense if primary succeeded

        
        secondary_sql = """
            UPDATE CAF_ADMIN.SIM_SWAP_DATA
            SET    ACTIVATION_STATUS = 'AI'
            WHERE  GSMNUMBER         = :gsmnumber
              AND  ACTIVATION_STATUS = 'IF'
        """
        try:
            with get_oracle_conn() as conn:
                cur = conn.cursor()
                cur.execute(secondary_sql, {"gsmnumber": gsmnumber})
                if cur.rowcount == 0:
                    logger.warning(
                        "[ESIM] mark_success secondary (SIM_SWAP_DATA): no row updated for "
                        "GSMNUMBER=%s — row may be absent or ACTIVATION_STATUS != 'IF'. "
                        "Primary record ID=%s already marked Y. Check SIM_SWAP_DATA manually if needed.",
                        gsmnumber, record_id,
                    )
                else:
                    logger.info(
                        "[ESIM] mark_success secondary (SIM_SWAP_DATA): "
                        "ACTIVATION_STATUS set to AI for GSMNUMBER=%s (ID=%s)",
                        gsmnumber, record_id,
                    )
                conn.commit()
        except Exception as exc:
            logger.error(
                "[ESIM] mark_success secondary (SIM_SWAP_DATA) DB failure — "
                "MANUAL FIX REQUIRED: set CAF_ADMIN.SIM_SWAP_DATA.ACTIVATION_STATUS = 'AI' "
                "WHERE GSMNUMBER = '%s'. Primary record ID=%s is already Y. Error: %s",
                gsmnumber, record_id, exc,
            )

    def mark_failed(self, record: dict, remarks: str) -> None:
        """Flip AMOUNT_DEDUCT_FLAG → R and record AMOUNT_DEDUCT_REMARKS."""
        sql = """
            UPDATE CAF_ADMIN.SIMSWAP_AMOUNT_DEDUCT_REQUESTS
            SET    AMOUNT_DEDUCT_FLAG    = 'R',
                   AMOUNT_DEDUCT_DATE    = SYSDATE,
                   AMOUNT_DEDUCT_REMARKS = :remarks
            WHERE  ID                    = :id
              AND  AMOUNT_DEDUCT_FLAG    = 'P'
        """
        try:
            with get_oracle_conn() as conn:
                cur = conn.cursor()
                cur.execute(sql, {
                    "remarks": remarks[:200],
                    "id":      record["id"],
                })
                if cur.rowcount == 0:
                    logger.warning(
                        "[ESIM] mark_failed: ID=%s rowcount=0 "
                        "(already moved out of P?)", record["id"]
                    )
                conn.commit()
        except Exception as exc:
            logger.error(
                "[ESIM] mark_failed DB failure (non-fatal) ID=%s: %s",
                record["id"], exc,
            )

    def reset_stuck_processing(self, stuck_minutes: int) -> int:
        
        if not self.enabled:
            return 0

        sql = """
            UPDATE CAF_ADMIN.SIMSWAP_AMOUNT_DEDUCT_REQUESTS
            SET    AMOUNT_DEDUCT_FLAG    = 'N',
                   AMOUNT_DEDUCT_REMARKS = 'Reset: stuck in processing state'
            WHERE  AMOUNT_DEDUCT_FLAG    = 'P'
              AND  MODULE_TYPE           = 'ESIM'
              AND  AMOUNT_DEDUCT_DATE   IS NOT NULL
              AND  AMOUNT_DEDUCT_DATE    < SYSDATE - (:stuck_minutes / 1440)
        """
        try:
            with get_oracle_conn() as conn:
                cur = conn.cursor()
                cur.execute(sql, {"stuck_minutes": stuck_minutes})
                count = cur.rowcount
                conn.commit()
            if count:
                logger.warning(
                    "[ESIM] reset_stuck_processing: reset %d stuck-P row(s) "
                    "older than %d min back to N", count, stuck_minutes,
                )
            return count
        except Exception as exc:
            logger.error("[ESIM] reset_stuck_processing error: %s", exc)
            return 0