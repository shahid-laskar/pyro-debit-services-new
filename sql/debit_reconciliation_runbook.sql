-- ═══════════════════════════════════════════════════════════════════════
-- Debit reconciliation runbook (P0 idempotency guards)
-- No DDL. debit_txn_log.api_stage is free text.
--
-- A reference is blocked from any further Pyro debit when its debit_txn_log
-- history shows either:
--   SUCCESS  - a debit already succeeded, or
--   UNKNOWN  - a call may have reached Pyro but no definitive result was recorded
--              (DEBIT_INTENT without a result, timeout / parse failure / HTTP 5xx,
--               or an api_stage='RECONCILIATION_REQUIRED' row with is_success='N').
-- The Oracle row stays in 'P' with a remark starting 'RECONCILIATION_REQUIRED'.
-- ═══════════════════════════════════════════════════════════════════════

-- 1. References currently waiting for reconciliation (Postgres) ----------
SELECT service_type, oracle_ref_id, client_id, amount,
       pyro_status_code, response_http_code, error_class, error_detail, created_at
FROM   public.debit_txn_log
WHERE  api_stage = 'RECONCILIATION_REQUIRED'
  AND  created_at >= NOW() - INTERVAL '7 days'
ORDER  BY created_at DESC;

-- Full history of one reference
-- SELECT id, api_stage, is_success, pyro_status_code, response_http_code,
--        pyro_txn_id, error_class, error_detail, created_at
-- FROM   public.debit_txn_log
-- WHERE  service_type = 'FANCYSALE' AND oracle_ref_id = '<REFID>'
-- ORDER  BY id;

-- 2. Oracle rows parked for reconciliation -------------------------------
-- SELECT REFID, SS_REQUEST_ID, AMOUNT, CAF_ENTRY_DATE, PYRO_REMARKS
-- FROM   CAF_ADMIN.VANITYSALE_FRANCH_DATA
-- WHERE  CAF_ENTRY_DONE = 'P' AND PYRO_REMARKS LIKE 'RECONCILIATION_REQUIRED%';
--
-- SELECT ID, MODULE_TYPE, SS_REQUEST_ID, AMOUNT, AMOUNT_DEDUCT_DATE, AMOUNT_DEDUCT_REMARKS
-- FROM   CAF_ADMIN.SIMSWAP_AMOUNT_DEDUCT_REQUESTS
-- WHERE  AMOUNT_DEDUCT_FLAG = 'P' AND AMOUNT_DEDUCT_REMARKS LIKE 'RECONCILIATION_REQUIRED%';

-- 3. Closing a reference after checking with Pyro ------------------------
-- 3a. Pyro confirms the debit HAPPENED (pyroId known):
--     Postgres: record the verdict so the service never debits it again.
-- INSERT INTO public.debit_txn_log
--        (service_type, oracle_ref_id, api_stage, is_success, pyro_txn_id, error_detail)
-- VALUES ('FANCYSALE', '<REFID>', 'MANUAL_RECONCILED', 'Y', '<pyroId>', '<ticket / who>');
--     Oracle: set the row to success, e.g.
-- UPDATE CAF_ADMIN.VANITYSALE_FRANCH_DATA
-- SET    CAF_ENTRY_DONE = 'Y', TRANSACTION_ID = '<pyroId>', PROCESSED_SM = 'Y',
--        PYRO_REMARKS = 'Manual reconciliation: debited pyroId=<pyroId>'
-- WHERE  REFID = <REFID> AND CAF_ENTRY_DONE = 'P';
--
-- 3b. Pyro confirms the debit did NOT happen (safe to retry):
-- INSERT INTO public.debit_txn_log
--        (service_type, oracle_ref_id, api_stage, is_success, error_detail)
-- VALUES ('FANCYSALE', '<REFID>', 'MANUAL_RECONCILED', 'N', '<ticket / who>');
--     Oracle: put the row back in the queue, e.g.
-- UPDATE CAF_ADMIN.VANITYSALE_FRANCH_DATA
-- SET    CAF_ENTRY_DONE = 'N', PYRO_REMARKS = 'Manual reconciliation: not debited, re-queued'
-- WHERE  REFID = <REFID> AND CAF_ENTRY_DONE = 'P';
--
-- Order matters: always insert the MANUAL_RECONCILED row BEFORE changing Oracle.
