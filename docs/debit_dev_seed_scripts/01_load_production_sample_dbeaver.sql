-- =====================================================================
-- 01_load_production_sample_dbeaver.sql
-- LOCAL DEVELOPMENT ONLY
--
-- DBeaver/JDBC-safe version.
--
-- EXPECTATION:
--   You have already imported the production-derived sample rows into
--   these LOCAL Oracle tables:
--
--     CAF_ADMIN.VANITYSALE_FRANCH_DATA
--     CAF_ADMIN.SIMSWAP_AMOUNT_DEDUCT_REQUESTS
--     CAF_ADMIN.BCD
--     CAF_ADMIN.SIM_SWAP_DATA
--
-- This script creates local DEV snapshot tables for reset purposes.
--
-- IMPORTANT:
--   Run ONLY on the local Oracle development database.
-- =====================================================================

-- ---------------------------------------------------------------------
-- 1. Show currently loaded local row counts
-- ---------------------------------------------------------------------

SELECT 'FANCYSALE' AS TABLE_NAME, COUNT(*) AS ROW_COUNT
FROM CAF_ADMIN.VANITYSALE_FRANCH_DATA
UNION ALL
SELECT 'SIMSWAP_AMOUNT_DEDUCT_REQUESTS', COUNT(*)
FROM CAF_ADMIN.SIMSWAP_AMOUNT_DEDUCT_REQUESTS
UNION ALL
SELECT 'BCD', COUNT(*)
FROM CAF_ADMIN.BCD
UNION ALL
SELECT 'SIM_SWAP_DATA', COUNT(*)
FROM CAF_ADMIN.SIM_SWAP_DATA;

-- ---------------------------------------------------------------------
-- 2. Remove previous DEV snapshots if they exist.
--
-- DBeaver executes each anonymous PL/SQL block as a normal Oracle
-- statement. No SQL*Plus SET/SHOW commands are used.
-- ---------------------------------------------------------------------

BEGIN
    EXECUTE IMMEDIATE 'DROP TABLE CAF_ADMIN.DEV_FANCYSALE_SEED PURGE';
EXCEPTION
    WHEN OTHERS THEN
        IF SQLCODE != -942 THEN
            RAISE;
        END IF;
END;
/

BEGIN
    EXECUTE IMMEDIATE 'DROP TABLE CAF_ADMIN.DEV_SIMSWAP_SEED PURGE';
EXCEPTION
    WHEN OTHERS THEN
        IF SQLCODE != -942 THEN
            RAISE;
        END IF;
END;
/

BEGIN
    EXECUTE IMMEDIATE 'DROP TABLE CAF_ADMIN.DEV_BCD_SEED PURGE';
EXCEPTION
    WHEN OTHERS THEN
        IF SQLCODE != -942 THEN
            RAISE;
        END IF;
END;
/

BEGIN
    EXECUTE IMMEDIATE 'DROP TABLE CAF_ADMIN.DEV_SIM_SWAP_DATA_SEED PURGE';
EXCEPTION
    WHEN OTHERS THEN
        IF SQLCODE != -942 THEN
            RAISE;
        END IF;
END;
/

-- ---------------------------------------------------------------------
-- 3. Snapshot the local imported data.
-- ---------------------------------------------------------------------

CREATE TABLE CAF_ADMIN.DEV_FANCYSALE_SEED AS
SELECT *
FROM CAF_ADMIN.VANITYSALE_FRANCH_DATA;

CREATE TABLE CAF_ADMIN.DEV_SIMSWAP_SEED AS
SELECT *
FROM CAF_ADMIN.SIMSWAP_AMOUNT_DEDUCT_REQUESTS;

CREATE TABLE CAF_ADMIN.DEV_BCD_SEED AS
SELECT *
FROM CAF_ADMIN.BCD;

CREATE TABLE CAF_ADMIN.DEV_SIM_SWAP_DATA_SEED AS
SELECT *
FROM CAF_ADMIN.SIM_SWAP_DATA;

COMMIT;

-- ---------------------------------------------------------------------
-- 4. Verify snapshot counts
-- ---------------------------------------------------------------------

SELECT 'DEV_FANCYSALE_SEED' AS SNAPSHOT_TABLE, COUNT(*) AS ROW_COUNT
FROM CAF_ADMIN.DEV_FANCYSALE_SEED
UNION ALL
SELECT 'DEV_SIMSWAP_SEED', COUNT(*)
FROM CAF_ADMIN.DEV_SIMSWAP_SEED
UNION ALL
SELECT 'DEV_BCD_SEED', COUNT(*)
FROM CAF_ADMIN.DEV_BCD_SEED
UNION ALL
SELECT 'DEV_SIM_SWAP_DATA_SEED', COUNT(*)
FROM CAF_ADMIN.DEV_SIM_SWAP_DATA_SEED;
