-- =====================================================================
-- 02_prepare_debit_test_data.sql
-- LOCAL DEVELOPMENT ONLY
--
-- PURPOSE
--   Transform the imported production-shaped sample into a controlled
--   debit test population.
--
-- TARGET:
--   - 20 FancySale rows
--   - 20 SimSwap rows
--   - 20 ESIM rows
--
-- IMPORTANT:
--   CAF_SERIAL_NO is NOT mandatory in the debit request table.
--   Existing NULL values are preserved.
--
--   Local MPIN_LENGTH is normalized to 6 because the development
--   F_DECRYPT() returns the fixed plaintext PIN '123456'.
--
-- RUN ONLY AFTER:
--   00_create_dev_decrypt_function.sql
--   01_load_production_sample.sql
--
-- LOCAL DATABASE ONLY.
-- =====================================================================


DECLARE
    v_count NUMBER;
BEGIN
    SELECT COUNT(*) INTO v_count FROM CAF_ADMIN.DEV_FANCYSALE_SEED;
    IF v_count = 0 THEN
        RAISE_APPLICATION_ERROR(-20001,
            'DEV_FANCYSALE_SEED is empty. Run 01_load_production_sample.sql first.');
    END IF;

    SELECT COUNT(*) INTO v_count FROM CAF_ADMIN.DEV_SIMSWAP_SEED;
    IF v_count = 0 THEN
        RAISE_APPLICATION_ERROR(-20002,
            'DEV_SIMSWAP_SEED is empty. Run 01_load_production_sample.sql first.');
    END IF;
END;


-- =====================================================================
-- 1. FancySale
--
-- First 20 local rows:
--   1-5   -> NZ 55
--   6-10  -> NZ 59
--   11-14 -> WZ 10
--   15-17 -> EZ 71
--   18-20 -> SZ 54
--
-- Preserve the imported MPIN ciphertext.
-- Set MPIN_LENGTH=6 for the local F_DECRYPT() implementation.
-- =====================================================================

MERGE INTO CAF_ADMIN.VANITYSALE_FRANCH_DATA t
USING (
    SELECT
        rid,
        CASE
            WHEN rn BETWEEN 1 AND 5  THEN 55
            WHEN rn BETWEEN 6 AND 10 THEN 59
            WHEN rn BETWEEN 11 AND 14 THEN 10
            WHEN rn BETWEEN 15 AND 17 THEN 71
            ELSE 54
        END AS new_circle
    FROM (
        SELECT
            ROWID AS rid,
            ROW_NUMBER() OVER (
                ORDER BY TRANS_DATE, ROWID
            ) AS rn
        FROM CAF_ADMIN.VANITYSALE_FRANCH_DATA
    )
    WHERE rn <= 20
) s
ON (t.ROWID = s.rid)
WHEN MATCHED THEN
    UPDATE SET
        t.CIRCLE_CODE = s.new_circle,
        t.AMOUNT = 1,
        t.MPIN_LENGTH = 6,
        t.CAF_ENTRY_DONE = 'N';

-- =====================================================================
-- 2. SIMSWAP / ESIM
--
-- First 20 rows  -> SIMSWAP
-- Next 20 rows   -> ESIM
--
-- Circle distribution:
--   55 (NZ), 59 (NZ), 10 (WZ), 71 (EZ), 54 (SZ)
--
-- Preserve existing CAF_SERIAL_NO, including NULL values.
-- Preserve imported MPIN ciphertext.
-- Normalize MPIN_LENGTH to 6.
-- =====================================================================

MERGE INTO CAF_ADMIN.SIMSWAP_AMOUNT_DEDUCT_REQUESTS t
USING (
    SELECT
        rid,
        rn,
        CASE
            WHEN rn <= 20 THEN 'SIMSWAP'
            ELSE 'ESIM'
        END AS new_module,
        CASE MOD(rn - 1, 5)
            WHEN 0 THEN 55
            WHEN 1 THEN 59
            WHEN 2 THEN 10
            WHEN 3 THEN 71
            ELSE 54
        END AS new_circle
    FROM (
        SELECT
            ROWID AS rid,
            ROW_NUMBER() OVER (
                ORDER BY REQUEST_DATE, ID
            ) AS rn
        FROM CAF_ADMIN.SIMSWAP_AMOUNT_DEDUCT_REQUESTS
    )
    WHERE rn <= 40
) s
ON (t.ROWID = s.rid)
WHEN MATCHED THEN
    UPDATE SET
        t.MODULE_TYPE = s.new_module,
        t.CIRCLE_CODE = s.new_circle,
        t.AMOUNT = 1,
        t.MPIN_LENGTH = 6,
        t.AMOUNT_DEDUCT_FLAG = 'N';

-- =====================================================================
-- 3. Prepare BCD secondary rows for SIMSWAP
--
-- CAF_SERIAL_NO may be NULL in the request.
--
-- Prefer exact GSM + CAF when request CAF is non-NULL.
-- For NULL-CAF requests, fall back to GSM-only for the local test seed.
--
-- This reflects the current application relationship while still allowing
-- us to test NULL CAF request records.
-- =====================================================================

MERGE INTO CAF_ADMIN.BCD b
USING (
    SELECT
        s.ID AS request_id,
        s.GSMNUMBER,
        s.CAF_SERIAL_NO AS request_caf,
        s.CIRCLE_CODE AS request_circle,
        b.ROWID AS bcd_rowid
    FROM CAF_ADMIN.SIMSWAP_AMOUNT_DEDUCT_REQUESTS s
    JOIN CAF_ADMIN.BCD b
      ON b.GSMNUMBER = s.GSMNUMBER
     AND (
            (s.CAF_SERIAL_NO IS NOT NULL
             AND b.CAF_SERIAL_NO = s.CAF_SERIAL_NO)
         OR (s.CAF_SERIAL_NO IS NULL)
     )
    WHERE s.MODULE_TYPE = 'SIMSWAP'
      AND s.AMOUNT_DEDUCT_FLAG = 'N'
      AND NOT EXISTS (
          SELECT 1
          FROM CAF_ADMIN.BCD b2
          JOIN CAF_ADMIN.SIMSWAP_AMOUNT_DEDUCT_REQUESTS s2
            ON s2.GSMNUMBER = b2.GSMNUMBER
           AND s2.CAF_SERIAL_NO IS NOT NULL
           AND b2.CAF_SERIAL_NO = s2.CAF_SERIAL_NO
          WHERE s2.ID = s.ID
            AND b2.ROWID != b.ROWID
      )
) x
ON (b.ROWID = x.bcd_rowid)
WHEN MATCHED THEN
    UPDATE SET
        b.CIRCLE_CODE = x.request_circle,
        b.ACTIVATION_STATUS = 'IF';

-- =====================================================================
-- 4. Prepare SIM_SWAP_DATA secondary rows for ESIM
--
-- Same NULL-CAF handling as BCD.
-- =====================================================================

MERGE INTO CAF_ADMIN.SIM_SWAP_DATA d
USING (
    SELECT
        s.ID AS request_id,
        s.GSMNUMBER,
        s.CAF_SERIAL_NO AS request_caf,
        s.CIRCLE_CODE AS request_circle,
        d.ROWID AS data_rowid
    FROM CAF_ADMIN.SIMSWAP_AMOUNT_DEDUCT_REQUESTS s
    JOIN CAF_ADMIN.SIM_SWAP_DATA d
      ON d.GSMNUMBER = s.GSMNUMBER
     AND (
            (s.CAF_SERIAL_NO IS NOT NULL
             AND d.CAF_SERIAL_NO = s.CAF_SERIAL_NO)
         OR (s.CAF_SERIAL_NO IS NULL)
     )
    WHERE s.MODULE_TYPE = 'ESIM'
      AND s.AMOUNT_DEDUCT_FLAG = 'N'
      AND NOT EXISTS (
          SELECT 1
          FROM CAF_ADMIN.SIM_SWAP_DATA d2
          JOIN CAF_ADMIN.SIMSWAP_AMOUNT_DEDUCT_REQUESTS s2
            ON s2.GSMNUMBER = d2.GSMNUMBER
           AND s2.CAF_SERIAL_NO IS NOT NULL
           AND d2.CAF_SERIAL_NO = s2.CAF_SERIAL_NO
          WHERE s2.ID = s.ID
            AND d2.ROWID != d.ROWID
      )
) x
ON (d.ROWID = x.data_rowid)
WHEN MATCHED THEN
    UPDATE SET
        d.CIRCLE_CODE = x.request_circle,
        d.ACTIVATION_STATUS = 'IF';

COMMIT;

-- =====================================================================
-- 5. Verification
-- =====================================================================

SELECT
    MODULE_TYPE,
    COUNT(*) AS TOTAL_ROWS,
    SUM(CASE WHEN CAF_SERIAL_NO IS NULL THEN 1 ELSE 0 END) AS NULL_CAF_ROWS,
    SUM(CASE WHEN CAF_SERIAL_NO IS NOT NULL THEN 1 ELSE 0 END) AS NON_NULL_CAF_ROWS,
    SUM(CASE WHEN AMOUNT = 1 THEN 1 ELSE 0 END) AS AMOUNT_ONE_ROWS,
    SUM(CASE WHEN MPIN_LENGTH = 6 THEN 1 ELSE 0 END) AS MPIN_LENGTH_SIX_ROWS
FROM CAF_ADMIN.SIMSWAP_AMOUNT_DEDUCT_REQUESTS
WHERE MODULE_TYPE IN ('SIMSWAP', 'ESIM')
GROUP BY MODULE_TYPE
ORDER BY MODULE_TYPE;

SELECT
    CIRCLE_CODE,
    MODULE_TYPE,
    AMOUNT_DEDUCT_FLAG,
    COUNT(*) AS CNT
FROM CAF_ADMIN.SIMSWAP_AMOUNT_DEDUCT_REQUESTS
WHERE MODULE_TYPE IN ('SIMSWAP','ESIM')
GROUP BY CIRCLE_CODE, MODULE_TYPE, AMOUNT_DEDUCT_FLAG
ORDER BY CIRCLE_CODE, MODULE_TYPE, AMOUNT_DEDUCT_FLAG;

SELECT
    CIRCLE_CODE,
    CAF_ENTRY_DONE,
    COUNT(*) AS CNT
FROM CAF_ADMIN.VANITYSALE_FRANCH_DATA
GROUP BY CIRCLE_CODE, CAF_ENTRY_DONE
ORDER BY CIRCLE_CODE, CAF_ENTRY_DONE;

-- Check local decrypt behavior
SELECT
    CAF_ADMIN.F_DECRYPT(MPIN) AS LOCAL_PLAIN_MPIN,
    COUNT(*) AS CNT
FROM (
    SELECT MPIN
    FROM CAF_ADMIN.VANITYSALE_FRANCH_DATA
    WHERE CAF_ENTRY_DONE = 'N'
      AND MPIN IS NOT NULL
      AND ROWNUM <= 10
)
GROUP BY CAF_ADMIN.F_DECRYPT(MPIN);
