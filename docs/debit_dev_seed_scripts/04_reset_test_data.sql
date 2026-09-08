-- =====================================================================
-- 04_reset_test_data.sql
-- LOCAL DEVELOPMENT ONLY
--
-- PURPOSE
--   Restore the four debit tables to the exact local snapshot taken by
--   01_load_production_sample.sql.
--
-- WARNING
--   This script DELETES/INSERTS data.
--
--   It is SAFE ONLY against the disposable LOCAL development database.
--   NEVER execute against production.
--
--   We deliberately restore from DEV_* snapshots rather than using
--   production connections.
-- =====================================================================

SET SERVEROUTPUT ON;

-- ---------------------------------------------------------------------
-- Safety check: make sure the DEV snapshots exist before proceeding.
-- ---------------------------------------------------------------------

DECLARE
    v_count NUMBER;
BEGIN
    SELECT COUNT(*) INTO v_count
    FROM CAF_ADMIN.DEV_FANCYSALE_SEED;

    IF v_count = 0 THEN
        RAISE_APPLICATION_ERROR(-20010,
            'DEV_FANCYSALE_SEED missing. Refusing reset.');
    END IF;

    SELECT COUNT(*) INTO v_count
    FROM CAF_ADMIN.DEV_SIMSWAP_SEED;

    IF v_count = 0 THEN
        RAISE_APPLICATION_ERROR(-20011,
            'DEV_SIMSWAP_SEED missing. Refusing reset.');
    END IF;

    SELECT COUNT(*) INTO v_count
    FROM CAF_ADMIN.DEV_BCD_SEED;

    IF v_count = 0 THEN
        RAISE_APPLICATION_ERROR(-20012,
            'DEV_BCD_SEED missing. Refusing reset.');
    END IF;

    SELECT COUNT(*) INTO v_count
    FROM CAF_ADMIN.DEV_SIM_SWAP_DATA_SEED;

    IF v_count = 0 THEN
        RAISE_APPLICATION_ERROR(-20013,
            'DEV_SIM_SWAP_DATA_SEED missing. Refusing reset.');
    END IF;
END;
/

-- =====================================================================
-- Restore FancySale
-- =====================================================================

TRUNCATE TABLE CAF_ADMIN.VANITYSALE_FRANCH_DATA;

INSERT INTO CAF_ADMIN.VANITYSALE_FRANCH_DATA
SELECT *
FROM CAF_ADMIN.DEV_FANCYSALE_SEED;

-- =====================================================================
-- Restore SimSwap / ESIM shared table
-- =====================================================================

TRUNCATE TABLE CAF_ADMIN.SIMSWAP_AMOUNT_DEDUCT_REQUESTS;

INSERT INTO CAF_ADMIN.SIMSWAP_AMOUNT_DEDUCT_REQUESTS
SELECT *
FROM CAF_ADMIN.DEV_SIMSWAP_SEED;

-- =====================================================================
-- Restore BCD
-- =====================================================================

TRUNCATE TABLE CAF_ADMIN.BCD;

INSERT INTO CAF_ADMIN.BCD
SELECT *
FROM CAF_ADMIN.DEV_BCD_SEED;

-- =====================================================================
-- Restore SIM_SWAP_DATA
-- =====================================================================

TRUNCATE TABLE CAF_ADMIN.SIM_SWAP_DATA;

INSERT INTO CAF_ADMIN.SIM_SWAP_DATA
SELECT *
FROM CAF_ADMIN.DEV_SIM_SWAP_DATA_SEED;

COMMIT;

-- =====================================================================
-- Verification
-- =====================================================================

SELECT 'VANITYSALE_FRANCH_DATA' AS TABLE_NAME,
       COUNT(*) AS ROW_COUNT
FROM CAF_ADMIN.VANITYSALE_FRANCH_DATA
UNION ALL
SELECT 'SIMSWAP_AMOUNT_DEDUCT_REQUESTS',
       COUNT(*)
FROM CAF_ADMIN.SIMSWAP_AMOUNT_DEDUCT_REQUESTS
UNION ALL
SELECT 'BCD',
       COUNT(*)
FROM CAF_ADMIN.BCD
UNION ALL
SELECT 'SIM_SWAP_DATA',
       COUNT(*)
FROM CAF_ADMIN.SIM_SWAP_DATA
ORDER BY TABLE_NAME;
