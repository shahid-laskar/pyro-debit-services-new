-- =====================================================================
-- 03_prepare_failure_cases.sql
-- LOCAL DEVELOPMENT ONLY
--
-- PURPOSE
--   Create deterministic failure/stuck cases while preserving the fact
--   that CAF_SERIAL_NO may legitimately be NULL in the debit request table.
--
-- RUN AFTER:
--   01_load_production_sample.sql
--   02_prepare_debit_test_data.sql
--
-- LOCAL DATABASE ONLY.
-- =====================================================================


-- =====================================================================
-- 1. FancySale stuck record
-- =====================================================================

UPDATE CAF_ADMIN.VANITYSALE_FRANCH_DATA t
SET
    t.CAF_ENTRY_DONE = 'P',
    t.CAF_ENTRY_DATE = SYSDATE - (45 / 1440),
    t.PYRO_REMARKS = 'DEV-FAILURE-CASE: stale FancySale processing'
WHERE t.ROWID = (
    SELECT rid
    FROM (
        SELECT
            ROWID AS rid,
            ROW_NUMBER() OVER (
                ORDER BY TRANS_DATE, ROWID
            ) AS rn
        FROM CAF_ADMIN.VANITYSALE_FRANCH_DATA
        WHERE CIRCLE_CODE IN (55,59)
          AND CAF_ENTRY_DONE = 'N'
    )
    WHERE rn = 1
);

-- =====================================================================
-- 2. SimSwap stuck record
-- =====================================================================

UPDATE CAF_ADMIN.SIMSWAP_AMOUNT_DEDUCT_REQUESTS t
SET
    t.AMOUNT_DEDUCT_FLAG = 'P',
    t.AMOUNT_DEDUCT_DATE = SYSDATE - (45 / 1440),
    t.AMOUNT_DEDUCT_REMARKS = 'DEV-FAILURE-CASE: stale SIMSWAP'
WHERE t.ID = (
    SELECT id
    FROM (
        SELECT
            ID,
            ROW_NUMBER() OVER (
                ORDER BY REQUEST_DATE, ID
            ) AS rn
        FROM CAF_ADMIN.SIMSWAP_AMOUNT_DEDUCT_REQUESTS
        WHERE MODULE_TYPE = 'SIMSWAP'
          AND AMOUNT_DEDUCT_FLAG = 'N'
    )
    WHERE rn = 1
);

-- =====================================================================
-- 3. ESIM stuck record
-- =====================================================================

UPDATE CAF_ADMIN.SIMSWAP_AMOUNT_DEDUCT_REQUESTS t
SET
    t.AMOUNT_DEDUCT_FLAG = 'P',
    t.AMOUNT_DEDUCT_DATE = SYSDATE - (45 / 1440),
    t.AMOUNT_DEDUCT_REMARKS = 'DEV-FAILURE-CASE: stale ESIM'
WHERE t.ID = (
    SELECT id
    FROM (
        SELECT
            ID,
            ROW_NUMBER() OVER (
                ORDER BY REQUEST_DATE, ID
            ) AS rn
        FROM CAF_ADMIN.SIMSWAP_AMOUNT_DEDUCT_REQUESTS
        WHERE MODULE_TYPE = 'ESIM'
          AND AMOUNT_DEDUCT_FLAG = 'N'
    )
    WHERE rn = 1
);

-- =====================================================================
-- 4. SimSwap secondary-writeback failure
--
-- Keep the request itself valid, but make one locally linked BCD record
-- non-updatable by changing only ACTIVATION_STATUS.
-- =====================================================================

UPDATE CAF_ADMIN.BCD b
SET
    b.ACTIVATION_STATUS = 'AI'
WHERE b.ROWID = (
    SELECT rid
    FROM (
        SELECT
            b2.ROWID AS rid,
            ROW_NUMBER() OVER (ORDER BY s.ID, b2.ROWID) AS rn
        FROM CAF_ADMIN.BCD b2
        JOIN CAF_ADMIN.SIMSWAP_AMOUNT_DEDUCT_REQUESTS s
          ON s.GSMNUMBER = b2.GSMNUMBER
        WHERE s.MODULE_TYPE = 'SIMSWAP'
          AND s.AMOUNT_DEDUCT_FLAG = 'N'
    )
    WHERE rn = 1
);

-- =====================================================================
-- 5. ESIM secondary-writeback failure
-- =====================================================================

UPDATE CAF_ADMIN.SIM_SWAP_DATA d
SET
    d.ACTIVATION_STATUS = 'AI'
WHERE d.ROWID = (
    SELECT rid
    FROM (
        SELECT
            d2.ROWID AS rid,
            ROW_NUMBER() OVER (ORDER BY s.ID, d2.ROWID) AS rn
        FROM CAF_ADMIN.SIM_SWAP_DATA d2
        JOIN CAF_ADMIN.SIMSWAP_AMOUNT_DEDUCT_REQUESTS s
          ON s.GSMNUMBER = d2.GSMNUMBER
        WHERE s.MODULE_TYPE = 'ESIM'
          AND s.AMOUNT_DEDUCT_FLAG = 'N'
    )
    WHERE rn = 1
);

COMMIT;

-- =====================================================================
-- Verification
-- =====================================================================

SELECT
    ID,
    GSMNUMBER,
    CAF_SERIAL_NO,
    CIRCLE_CODE,
    MODULE_TYPE,
    AMOUNT,
    AMOUNT_DEDUCT_FLAG,
    AMOUNT_DEDUCT_DATE,
    AMOUNT_DEDUCT_REMARKS
FROM CAF_ADMIN.SIMSWAP_AMOUNT_DEDUCT_REQUESTS
WHERE MODULE_TYPE IN ('SIMSWAP','ESIM')
ORDER BY MODULE_TYPE, ID;

SELECT
    REFID,
    CIRCLE_CODE,
    CAF_ENTRY_DONE,
    CAF_ENTRY_DATE,
    PYRO_REMARKS
FROM CAF_ADMIN.VANITYSALE_FRANCH_DATA
WHERE PYRO_REMARKS LIKE 'DEV-FAILURE-CASE:%'
ORDER BY REFID;
