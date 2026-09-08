-- =====================================================================
-- 00_create_dev_decrypt_function.sql
-- LOCAL DEVELOPMENT ONLY
--
-- The production debit code calls CAF_ADMIN.F_DECRYPT(MPIN).
-- Production MPIN values are ciphertext/encoded values such as:
--     LJcOTcjiM3E=
--
-- The local test environment must return a 6-digit plaintext MPIN.
--
-- This function intentionally does NOT reproduce production encryption
-- and does not contain production secrets.
--
-- DO NOT deploy this function to production.
-- =====================================================================

CREATE OR REPLACE FUNCTION CAF_ADMIN.F_DECRYPT (
    p_value IN VARCHAR2
)
RETURN VARCHAR2
IS
BEGIN
    IF p_value IS NULL THEN
        RETURN NULL;
    END IF;

    -- Local deterministic test PIN.
    RETURN '123456';
END;
/

SHOW ERRORS FUNCTION CAF_ADMIN.F_DECRYPT;

-- Verification
SELECT
    CAF_ADMIN.F_DECRYPT('LJcOTcjiM3E=') AS plain_mpin,
    LENGTH(CAF_ADMIN.F_DECRYPT('LJcOTcjiM3E=')) AS mpin_length
FROM dual;
