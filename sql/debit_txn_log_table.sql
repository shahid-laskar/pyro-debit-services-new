-- ═══════════════════════════════════════════════════════════════════════
-- Migration: add_debit_txn_log.sql
-- Run once on Postgres BEFORE deploying the FancySale service.
-- Idempotent: uses IF NOT EXISTS / DO NOTHING guards throughout.
-- ═══════════════════════════════════════════════════════════════════════

-- TABLE -----------------------------------------------------------------
CREATE TABLE IF NOT EXISTS public.debit_txn_log (
    id                  BIGSERIAL       PRIMARY KEY,
    service_type        VARCHAR(20)     NOT NULL,   -- 'FANCYSALE' | 'SIMSWAP' | 'ESIM'
    oracle_ref_id       VARCHAR(50),                -- REFID (VANITYSALE) or equiv per service
    client_id           VARCHAR(50),                -- clientId sent to Pyro (SS_REQUEST_ID)
    source_msisdn       VARCHAR(15),                -- sourceMsisdn (CTOPUPNO)
    dest_msisdn         VARCHAR(15),                -- destMsisdn   (FANCY_NO)
    amount              NUMERIC(12,2),
    api_stage           VARCHAR(30),                -- 'DEBIT' | 'AUTH' | 'CLEANUP'
    api_endpoint        VARCHAR(200),
    attempt_no          SMALLINT        DEFAULT 1,
    request_body        TEXT,                       -- MPIN masked to ***
    response_http_code  SMALLINT,
    response_body       TEXT,
    pyro_status_code    INT,
    pyro_status_text    VARCHAR(50),
    pyro_txn_id         VARCHAR(50),                -- pyroId from Pyro response
    call_started_at     TIMESTAMPTZ,
    call_ended_at       TIMESTAMPTZ,
    duration_ms         INT,
    is_success          CHAR(1)         DEFAULT 'N' CHECK (is_success IN ('Y','N')),
    is_perm_failure     CHAR(1)         DEFAULT 'N' CHECK (is_perm_failure IN ('Y','N')),
    error_class         VARCHAR(100),
    error_detail        TEXT,
    created_at          TIMESTAMPTZ     DEFAULT NOW()
);

-- INDEXES ---------------------------------------------------------------
CREATE INDEX IF NOT EXISTS debit_txn_log_svc_ref
    ON public.debit_txn_log (service_type, oracle_ref_id);

CREATE INDEX IF NOT EXISTS debit_txn_log_created
    ON public.debit_txn_log (service_type, created_at DESC);

CREATE INDEX IF NOT EXISTS debit_txn_log_pyrotxn
    ON public.debit_txn_log (pyro_txn_id)
    WHERE pyro_txn_id IS NOT NULL;

-- COMMENTS --------------------------------------------------------------
COMMENT ON TABLE  public.debit_txn_log IS
    'Audit log for all Pyro wallet-debit API calls (FancySale, SimSwap, ESIM)';
COMMENT ON COLUMN public.debit_txn_log.service_type   IS 'FANCYSALE | SIMSWAP | ESIM';
COMMENT ON COLUMN public.debit_txn_log.oracle_ref_id  IS
    'Source Oracle table PK — REFID for VANITYSALE_FRANCH_DATA';
COMMENT ON COLUMN public.debit_txn_log.client_id      IS
    'SS_REQUEST_ID sent as clientId to Pyro';
COMMENT ON COLUMN public.debit_txn_log.request_body   IS
    'JSON-serialised request payload; mpin field masked to ***';

-- ═══════════════════════════════════════════════════════════════════════
-- ONE-TIME ORACLE MANUAL STEP (run before first deployment):
-- Reset the 13 pre-existing stuck-P records that have CAF_ENTRY_DATE=NULL.
-- The automated cleanup job skips NULL dates intentionally.
--
-- Connect to Oracle and run:
--
--   UPDATE CAF_ADMIN.VANITYSALE_FRANCH_DATA
--   SET    CAF_ENTRY_DONE = 'N',
--          PYRO_REMARKS   = 'Manual reset: pre-deployment cleanup'
--   WHERE  CAF_ENTRY_DONE = 'P'
--     AND  CAF_ENTRY_DATE IS NULL;
--   COMMIT;
--
-- ═══════════════════════════════════════════════════════════════════════