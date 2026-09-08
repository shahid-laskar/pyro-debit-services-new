# Phase 20: Nationwide 'ALL' Mode Regression Review

## 1. Executive Summary

This document fulfills the requirements of **Phase 20 (ALL Regression)** of [`debit_services_final_implementation_plan.md`](file:///D:/pyro/docs/debit_services_final_implementation_plan.md) on branch `feature/zonewise-staged-migration`.

### Purpose
To rigorously verify and prove that configuring:
```bash
ENABLED_ZONES=ALL
```
restores full **nationwide business behavior** equivalent to the legacy service, while strictly preserving all modern reliability, concurrency, writeback, and audit safety controls:

$$\text{ALL Mode} \approx \text{Legacy Nationwide Business Behavior} + \text{Modern Safety Controls}$$

---

## 2. Legacy vs. Modern ALL Mode Comparative Matrix

| Dimension | Legacy Nationwide Service | Modern Staged Service (`ENABLED_ZONES=ALL`) | Architectural Rationale |
| :--- | :--- | :--- | :--- |
| **Candidate Scope** | Nationwide (no circle filters) | Nationwide (no circle filters) | Preserves complete nationwide candidate discovery across all 31 circles. |
| **Ordering** | FIFO (`TRANS_DATE ASC` / `REQUEST_DATE ASC`) | FIFO (`TRANS_DATE ASC` / `REQUEST_DATE ASC`) | Preserves strict chronological processing nationwide. |
| **Multi-Zone Processing** | Arbitrary mix of circles processed in one batch | NZ, WZ, EZ, SZ processed in single batch without boundaries | Validated across all 4 zones. `is_circle_allowed` returns `True` for all circles. |
| **Candidate SQL Bind Parameters** | 1 bind variable (`:batch_size`) | 1 bind variable (`:batch_size`) | Circle filter bind parameters (`:c_0..:c_N`) are completely omitted. |
| **Claim Verification** | Unchecked or loose row update | Strict `cur.rowcount == 1` optimistic check | Worker losing claim discards row; zero unmocked Pyro calls executed on collisions. |
| **Stuck Cleanup Scope** | Nationwide (resets any row with status `'P'`) | Nationwide (no circle filters) | Ensures stuck rows across all circles are recovered automatically. |
| **Cleanup Race Protection** | ❌ **Vulnerable:** resets rows actively being processed | ✅ **Protected:** `ActiveOwnershipTracker` appends `AND REFID NOT IN (:act_0..)` | Eliminates stuck-cleanup timing race on tail records. |
| **Writeback Hardening** | ❌ **Vulnerable:** swallowed writeback error leaves row in `'P'`; cleanup resets to `'N'` $\rightarrow$ **Double Debit** | ✅ **Hardened:** marks row `RECONCILIATION_REQUIRED`; cleanup excludes `RECONCILIATION_REQUIRED%` | Eliminates duplicate wallet debits on Oracle writeback failures. |
| **Batch Concurrency** | ❌ Overlapping scheduler and manual triggers | ✅ Per-service `asyncio.Lock` serialization | Prevents concurrent batch executions within the worker process. |
| **Audit Trail** | Fragmented application logs | Durable PostgreSQL `public.debit_txn_log` (Q018) | Complete transaction lineage with masked credentials across all zones. |

---

## 3. SQL Query Differential Analysis (Filtered vs. ALL Mode)

### 3.1 Candidate Discovery Queries (Q001, Q006, Q012)
In filtered mode (`NZ`), candidate discovery queries inject:
```sql
AND CIRCLE_CODE IN (:c_0, :c_1, :c_2, :c_3, :c_4, :c_5, :c_6, :c_7, :c_8)
```
In `ALL` mode:
- **FancySale (Q001):** The `CIRCLE_CODE IN` clause is omitted entirely:
  ```sql
  SELECT * FROM (
      SELECT REFID, CTOPUPNO, FANCY_NO, AMOUNT, CAF_ADMIN.F_DECRYPT(MPIN) AS plain_mpin,
             MPIN_LENGTH, SS_REQUEST_ID, CSCCODE, CIRCLE_CODE, TRANS_DATE, MODULE_TYPE, CAF_ENTRY_DONE
      FROM CAF_ADMIN.VANITYSALE_FRANCH_DATA
      WHERE CAF_ENTRY_DONE IN ('N', 'QM', 'QB')
      ORDER BY TRANS_DATE ASC
  ) WHERE ROWNUM <= :batch_size
  ```
- **SimSwap (Q006):** Preserves `MODULE_TYPE = 'SIMSWAP'` and omits circle filter:
  ```sql
  SELECT * FROM (
      SELECT ID, REFID, CTOPUPNO, GSMNUMBER, SIMNUMBER, AMOUNT, CAF_ADMIN.F_DECRYPT(MPIN) AS plain_mpin,
             MPIN_LENGTH, SS_REQUEST_ID, MODULE_TYPE, REQUEST_DATE, AMOUNT_DEDUCT_FLAG,
             CIRCLE_CODE, DEALERCODE, SWAP_TYPE, SOURCE
      FROM CAF_ADMIN.SIMSWAP_AMOUNT_DEDUCT_REQUESTS
      WHERE AMOUNT_DEDUCT_FLAG IN ('N', 'QM', 'QB')
        AND MODULE_TYPE = 'SIMSWAP'
      ORDER BY REQUEST_DATE ASC
  ) WHERE ROWNUM <= :batch_size
  ```
- **ESIM (Q012):** Preserves `MODULE_TYPE = 'ESIM'` and omits circle filter:
  ```sql
  SELECT * FROM (
      SELECT ID, REFID, CTOPUPNO, GSMNUMBER, SIMNUMBER, AMOUNT, CAF_ADMIN.F_DECRYPT(MPIN) AS plain_mpin,
             MPIN_LENGTH, SS_REQUEST_ID, MODULE_TYPE, REQUEST_DATE, AMOUNT_DEDUCT_FLAG,
             CIRCLE_CODE, DEALERCODE, SWAP_TYPE, SOURCE
      FROM CAF_ADMIN.SIMSWAP_AMOUNT_DEDUCT_REQUESTS
      WHERE AMOUNT_DEDUCT_FLAG IN ('N', 'QM', 'QB')
        AND MODULE_TYPE = 'ESIM'
      ORDER BY REQUEST_DATE ASC
  ) WHERE ROWNUM <= :batch_size
  ```

### 3.2 Stuck Cleanup Queries (Q005, Q011, Q017)
In `ALL` mode, cleanup recovers stuck rows nationwide while retaining active exclusion and reconciliation protection:
```sql
UPDATE CAF_ADMIN.VANITYSALE_FRANCH_DATA
SET    CAF_ENTRY_DONE = 'N',
       PYRO_REMARKS   = 'Reset: stuck in processing state'
WHERE  CAF_ENTRY_DONE  = 'P'
  AND  CAF_ENTRY_DATE IS NOT NULL
  AND  CAF_ENTRY_DATE  < SYSDATE - (:stuck_minutes / 1440)
  AND  (PYRO_REMARKS IS NULL OR PYRO_REMARKS NOT LIKE 'RECONCILIATION_REQUIRED%')
  AND (REFID NOT IN (:act_0, :act_1, ...))
```

---

## 4. Empirical Verification Against Live Non-Production Databases

Empirical testing executed against non-production databases confirms live operation:

### 4.1 Oracle 21c XE Database (`host.docker.internal:1521/xepdb1`)
1. **FancySale Candidate Discovery (Q001 in ALL mode):**
   - Bind parameter dictionary: `{'batch_size': 50}` (zero circle binds).
   - Rows returned: **22 rows**.
   - Distinct circles discovered: `{'10', '54', '55', '59', '62', '71'}`.
   - Spans both **North Zone** (`10, 54, 55, 59, 62`) and **East Zone** (`71`).
2. **SimSwap Candidate Discovery (Q006 in ALL mode):**
   - Bind parameter dictionary: `{'batch_size': 50}`.
   - Rows returned: **19 rows**.
   - Distinct circles discovered: `{'10', '54', '55', '59', '71'}`.
   - Preserves strict `MODULE_TYPE = 'SIMSWAP'` isolation.
3. **ESIM Candidate Discovery (Q012 in ALL mode):**
   - Bind parameter dictionary: `{'batch_size': 50}`.
   - Rows returned: **19 rows**.
   - Distinct circles discovered: `{'10', '54', '55', '59', '71'}`.
   - Preserves strict `MODULE_TYPE = 'ESIM'` isolation.
4. **Cleanup Query Syntax (Q005, Q011, Q017):**
   - Executed in a rolled-back transaction with active reference exclusions.
   - SQL compilation and bind mapping confirmed 100% valid with zero syntax or type conversion errors.

### 4.2 PostgreSQL 17.2 Database (`localhost:5432/postgres`)
- `public.debit_txn_log` accessible and ready to record transactions from any circle across India.

---

## 5. Automated Test Suite

Dedicated integration regression test suite: [`tests/integration/test_all_regression.py`](file:///D:/pyro/debit_services/tests/integration/test_all_regression.py).

### Test Suite Structure (28 Test Cases)
1. **`TestAllModeContextAndConfiguration`** (4 tests):
   - Explicit `zones_str="ALL"` context initialization.
   - Context scope inheritance from `settings.enabled_zones = "ALL"`.
   - `is_circle_allowed` permission across NZ, WZ, EZ, and SZ circles.
   - Telemetry serialization dictionary format.
2. **`TestAllModeCandidateSQLEquivalence`** (4 tests):
   - FancySale Q001 candidate query generation without circle filters.
   - SimSwap Q006 candidate query generation without circle filters.
   - ESIM Q012 candidate query generation without circle filters.
   - Differential comparison between `NZ` (filtered) and `ALL` (nationwide).
3. **`TestAllModeCleanupEquivalenceAndSafety`** (4 tests):
   - FancySale Q005 stuck cleanup nationwide SQL.
   - SimSwap Q011 stuck cleanup nationwide SQL.
   - ESIM Q017 stuck cleanup nationwide SQL.
   - Immunity of `RECONCILIATION_REQUIRED` records from cleanup reset.
4. **`TestAllModeMultiZoneCandidateProcessing`** (4 tests):
   - FancySale claims records across NZ, WZ, EZ, and SZ in a single batch.
   - SimSwap claims records across NZ, WZ, EZ, and SZ in a single batch.
   - ESIM claims records across NZ, WZ, EZ, and SZ in a single batch.
   - Contrast proving out-of-zone candidates are dropped in filtered mode but retained in ALL mode.
5. **`TestAllModeSafetyInvariantRetention`** (3 tests):
   - Optimistic claim collision defense (`cur.rowcount == 0` drops row, zero Pyro calls).
   - Writeback hardening with `mark_reconciliation_required` upon Oracle writeback failure.
   - Per-service concurrency mutex serialization via `asyncio.Lock`.
6. **`TestAllModeEndToEndProcessorBatch`** (4 tests):
   - Full end-to-end `run_debit_batch` for FancySale across 4 zones.
   - Full end-to-end `run_debit_batch` for SimSwap with secondary `CAF_ADMIN.BCD` sync.
   - Full end-to-end `run_debit_batch` for ESIM with secondary `CAF_ADMIN.SIM_SWAP_DATA` sync.
   - Active in-flight ownership protection during nationwide cleanup.
7. **`TestNonProdOracleLiveIntegration`** (4 tests):
   - Live Q001 execution against Oracle XE returning multi-zone candidates.
   - Live Q006 execution against Oracle XE returning multi-zone candidates.
   - Live Q012 execution against Oracle XE returning multi-zone candidates.
   - Live cleanup query execution in rolled-back transaction.
8. **`TestNonProdPostgresLiveIntegration`** (1 test):
   - Live schema probe of `public.debit_txn_log`.

**Result:** 28 passed, 0 failed (100% pass rate). Total project suite: 354 passed.
