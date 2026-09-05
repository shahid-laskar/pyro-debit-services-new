
CREATE TABLE IF NOT EXISTS public.debit_txn_log (
    id                  BIGSERIAL       PRIMARY KEY,
    service_type        VARCHAR(20)     NOT NULL,  
    oracle_ref_id       VARCHAR(50),                
    client_id           VARCHAR(50),                
    source_msisdn       VARCHAR(15),                
    dest_msisdn         VARCHAR(15),                
    amount              NUMERIC(12,2),
    api_stage           VARCHAR(30),                
    api_endpoint        VARCHAR(200),
    attempt_no          SMALLINT        DEFAULT 1,
    request_body        TEXT,                       
    response_http_code  SMALLINT,
    response_body       TEXT,
    pyro_status_code    INT,
    pyro_status_text    VARCHAR(50),
    pyro_txn_id         VARCHAR(50),                
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

