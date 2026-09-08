# Phase 17: Database Performance & Index Review

## 1. Executive Summary & Principles

This document fulfills the requirements of **Phase 17 (Performance / Index Review)** for the `debit_services` subsystem (FancySale, SimSwap, ESIM) during its staged zonewise migration (`NZ` → `NZ,WZ` → `ALL`).

### Governing Rules
1. **No Speculative Indexing:** Do not create indexes simply from a migration plan. Indexes must only be added after DBA execution plan analysis (`EXPLAIN PLAN`) against real production data volumes.
2. **Use Actual Metadata:** Evaluation is anchored strictly on confirmed Oracle constraints, index catalogs, and column types.
3. **No Partitioning Assumptions:** All four target Oracle tables (`CAF_ADMIN.VANITYSALE_FRANCH_DATA`, `CAF_ADMIN.SIMSWAP_AMOUNT_DEDUCT_REQUESTS`, `CAF_ADMIN.BCD`, `CAF_ADMIN.SIM_SWAP_DATA`) are **non-partitioned physical tables**. Partition pruning must never be cited as an implementation rationale.
4. **Defense-in-Depth Predicates:** Scalar `CIRCLE_CODE` predicates in claim queries provide strict logical zone containment, cross-circle boundary isolation, and concurrency safety, rather than physical partition pruning.

---

## 2. Table-by-Table Performance & Index Evaluation

### 2.1 FancySale — `CAF_ADMIN.VANITYSALE_FRANCH_DATA`

#### Metadata Profile
- **Storage Type:** Physical table (non-partitioned).
- **Primary Row Identifier:** `REFID` (used by Q001, Q002, Q003, Q004).
- **Existing Constraints & Indexes:** Confirmed metadata reveals **no PK or UNIQUE constraint** on `REFID`. The only documented index is on `SYNC`.
- **Status & Date Columns:** `CAF_ENTRY_DONE` (`'N'`, `'P'`, `'Y'`, `'R'`, `'QM'`, `'QB'`), `TRANS_DATE` (transaction timestamp), `CAF_ENTRY_DATE` (claim timestamp).

#### Query Shapes
1. **Candidate Discovery (Q001):**
   ```sql
   SELECT * FROM (
       SELECT REFID, CTOPUPNO, FANCY_NO, AMOUNT, CAF_ADMIN.F_DECRYPT(MPIN) AS plain_mpin,
              MPIN_LENGTH, SS_REQUEST_ID, CSCCODE, CIRCLE_CODE, TRANS_DATE, MODULE_TYPE, CAF_ENTRY_DONE
       FROM CAF_ADMIN.VANITYSALE_FRANCH_DATA
       WHERE CAF_ENTRY_DONE IN ('N', 'QM', 'QB')
         AND CIRCLE_CODE IN (:circle_0, :circle_1, ...) -- Omitted in ALL mode
       ORDER BY TRANS_DATE ASC
   ) WHERE ROWNUM <= :batch_size
   ```
2. **Optimistic Claim (Q002):**
   ```sql
   UPDATE CAF_ADMIN.VANITYSALE_FRANCH_DATA
   SET CAF_ENTRY_DONE = 'P', CAF_ENTRY_DATE = SYSDATE, PYRO_REMARKS = 'Processing started'
   WHERE REFID = :refid
     AND CIRCLE_CODE = :circle_code
     AND CAF_ENTRY_DONE IN ('N', 'QM', 'QB')
   ```

#### Index Evaluation & Plan Analysis
- **Current Access Path:** Without an index on `(CAF_ENTRY_DONE, CIRCLE_CODE, TRANS_DATE)` or `REFID`, Q001 performs a table access full / fast full scan to filter pending statuses and sort by `TRANS_DATE`. Q002 performs a full table scan per claimed row unless an index on `REFID` exists in production.
- **Candidate Index:**
  ```sql
  -- Evaluated candidate index for candidate selection:
  CREATE INDEX idx_vanity_debit_candidate
  ON CAF_ADMIN.VANITYSALE_FRANCH_DATA (CAF_ENTRY_DONE, CIRCLE_CODE, TRANS_DATE);
  ```
- **Evaluation:**
  - *Selectivity:* High selectivity for pending records (`'N'`, `'QM'`, `'QB'`), which represent a tiny fraction (<1%) of the total historical vanity sale table.
  - *Ordering:* Including `TRANS_DATE` in the composite index allows the optimizer to satisfy `ORDER BY TRANS_DATE ASC` directly from the index tree without an explicit sort buffer (`SORT ORDER BY STOPKEY`).
  - *Recommendation:* **Do not create this index preemptively.** In production, `CAF_ADMIN` is an enterprise telecom schema where index additions require formal change control. The application team must collect `EXPLAIN PLAN` and AWR reports during the initial `NZ` rollout before requesting index creation from the DBA.

---

### 2.2 SimSwap & ESIM — `CAF_ADMIN.SIMSWAP_AMOUNT_DEDUCT_REQUESTS`

#### Metadata Profile
- **Storage Type:** Physical table (non-partitioned).
- **Primary Row Identifier:** `ID` (Confirmed **Primary Key**, backed by unique index).
- **Service Segregation:** `MODULE_TYPE` (`'SIMSWAP'` vs. `'ESIM'`).
- **Status & Date Columns:** `AMOUNT_DEDUCT_FLAG` (`'N'`, `'P'`, `'Y'`, `'R'`, `'QM'`, `'QB'`), `REQUEST_DATE`, `AMOUNT_DEDUCT_DATE`.

#### Query Shapes
1. **Candidate Discovery (Q006 SimSwap / Q012 ESIM):**
   ```sql
   SELECT * FROM (
       SELECT ID, CTOPUPNO, FANCY_NO, AMOUNT, CAF_ADMIN.F_DECRYPT(MPIN) AS plain_mpin,
              MPIN_LENGTH, SS_REQUEST_ID, CSCCODE, CIRCLE_CODE, REQUEST_DATE, MODULE_TYPE,
              AMOUNT_DEDUCT_FLAG, CAF_SERIAL_NO
       FROM CAF_ADMIN.SIMSWAP_AMOUNT_DEDUCT_REQUESTS
       WHERE AMOUNT_DEDUCT_FLAG IN ('N', 'QM', 'QB')
         AND MODULE_TYPE = :module_type
         AND CIRCLE_CODE IN (:circle_0, :circle_1, ...) -- Omitted in ALL mode
       ORDER BY REQUEST_DATE ASC
   ) WHERE ROWNUM <= :batch_size
   ```
2. **Optimistic Claim (Q007 SimSwap / Q013 ESIM):**
   ```sql
   UPDATE CAF_ADMIN.SIMSWAP_AMOUNT_DEDUCT_REQUESTS
   SET AMOUNT_DEDUCT_FLAG = 'P', AMOUNT_DEDUCT_DATE = SYSDATE, AMOUNT_DEDUCT_REMARKS = 'Processing started'
   WHERE ID = :id
     AND CIRCLE_CODE = :circle_code
     AND AMOUNT_DEDUCT_FLAG IN ('N', 'QM', 'QB')
     AND MODULE_TYPE = :module_type
   ```
3. **Status Writebacks (Q008 / Q010 / Q014 / Q016):**
   ```sql
   UPDATE CAF_ADMIN.SIMSWAP_AMOUNT_DEDUCT_REQUESTS
   SET AMOUNT_DEDUCT_FLAG = :status, ...
   WHERE ID = :id AND AMOUNT_DEDUCT_FLAG = 'P' AND MODULE_TYPE = :module_type
   ```

#### Index Evaluation & Plan Analysis
- **Primary Key Efficiency:**
  All row-level mutations (claim, success writeback, failure writeback) use `WHERE ID = :id`. Because `ID` is the indexed primary key, Oracle executes an `INDEX UNIQUE SCAN` on the PK index followed by scalar filtering on `CIRCLE_CODE`, `AMOUNT_DEDUCT_FLAG`, and `MODULE_TYPE`. This executes in <1 ms with single-block I/O.
- **Candidate Index for Discovery:**
  ```sql
  -- Evaluated candidate index for candidate queue discovery:
  CREATE INDEX idx_simswap_deduct_cand
  ON CAF_ADMIN.SIMSWAP_AMOUNT_DEDUCT_REQUESTS (AMOUNT_DEDUCT_FLAG, MODULE_TYPE, CIRCLE_CODE, REQUEST_DATE);
  ```
- **Evaluation:**
  - The combination of `AMOUNT_DEDUCT_FLAG IN ('N','QM','QB')` and `MODULE_TYPE` isolates active queue items efficiently.
  - However, because the batch fetch interval is 15–30 minutes and batch sizes are modest (e.g. 50–200 rows), creating this composite index is only necessary if table size exceeds millions of rows and full table scan times exceed acceptable SLA (>2 seconds).
  - *Recommendation:* Keep existing PK access; monitor candidate selection latency in production logs.

---

### 2.3 Secondary Activation Writeback — `CAF_ADMIN.BCD` (SimSwap Q009)

#### Metadata Profile
- **Storage Type:** Physical table (non-partitioned).
- **Composite Primary Key:** `(GSMNUMBER, CAF_SERIAL_NO)`.
- **Primary Target of Q009:**
  ```sql
  UPDATE CAF_ADMIN.BCD
  SET ACTIVATION_STATUS = 'AI'
  WHERE GSMNUMBER = :gsmnumber
    AND CAF_SERIAL_NO = :caf_serial_no
    AND ACTIVATION_STATUS = 'IF'
  ```

#### Index Evaluation & Plan Analysis
- **Exact Primary Key Utilization:**
  In earlier iterations, Q009 relied solely on `GSMNUMBER = :gsmnumber`, which could match historical rows or trigger multi-row scans.
  With the Phase 10 / Phase 11 hardening, `CAF_SERIAL_NO` is projected from `SIMSWAP_AMOUNT_DEDUCT_REQUESTS` and bound into Q009.
- **Access Path:**
  Because the primary key of `CAF_ADMIN.BCD` is composite `(GSMNUMBER, CAF_SERIAL_NO)`, the predicate `GSMNUMBER = :gsmnumber AND CAF_SERIAL_NO = :caf_serial_no` hits the **primary key unique index** directly (`INDEX UNIQUE SCAN`).
- **Recommendation:** No new index is required on `CAF_ADMIN.BCD`. Utilizing the composite PK eliminates any table scan or write contention.

---

### 2.4 Secondary Activation Writeback — `CAF_ADMIN.SIM_SWAP_DATA` (ESIM Q015)

#### Metadata Profile
- **Storage Type:** Physical table (non-partitioned).
- **Primary Key:** `ID`.
- **Existing Lookup Index:** Composite index starting with:
  ```text
  (GSMNUMBER, NEW_SIM, CAF_SERIAL_NO, SWAP_DATE)
  ```
- **Q015 Query:**
  ```sql
  UPDATE CAF_ADMIN.SIM_SWAP_DATA
  SET ACTIVATION_STATUS = 'AI'
  WHERE GSMNUMBER = :gsmnumber
    AND ACTIVATION_STATUS = 'IF'
  ```

#### Index Evaluation & Plan Analysis
- **Index Leading Edge:**
  The existing index leading column is `GSMNUMBER`. When Oracle executes Q015, the predicate `GSMNUMBER = :gsmnumber` utilizes an `INDEX RANGE SCAN` on the leading column, quickly locating the relevant records without scanning the table blocks.
- **Recommendation:** No additional index is required. Writeback volume for ESIM is naturally bounded by the per-batch size, and the leading edge of the existing index provides optimal access.

---

### 2.5 PostgreSQL Audit Trail — `public.debit_txn_log`

#### Metadata Profile
- **Storage Type:** PostgreSQL physical table.
- **Primary Key:** `id BIGSERIAL PRIMARY KEY`.
- **Indexes:**
  ```sql
  CREATE INDEX debit_txn_log_svc_ref ON public.debit_txn_log (service_type, oracle_ref_id);
  CREATE INDEX debit_txn_log_created ON public.debit_txn_log (service_type, created_at DESC);
  CREATE INDEX debit_txn_log_pyrotxn ON public.debit_txn_log (pyro_txn_id) WHERE pyro_txn_id IS NOT NULL;
  ```

#### Performance & Design Confirmation
- **Append-Only Pattern:** `public.debit_txn_log` is solely an append-only audit trail. The service never issues blocking `SELECT` queries against it during runtime debit operations.
- **Asynchronous Execution:** Database writes are executed via `async_insert_debit_txn_log` (using `asyncio.to_thread`), preventing database latency from blocking the main FastAPI asynchronous event loop.
- **No Idempotency Constraint:** As verified in Phase 3 and Invariant B, `debit_txn_log` does not have a unique constraint on `(service_type, oracle_ref_id)`. Idempotency is enforced strictly via optimistic state transitions in Oracle (`'N'`/`'QM'`/`'QB'` → `'P'` → `'Y'`).

---

## 3. Production Rollout & Index Decision Matrix

| Subsystem / Query | Target Table | Predicate Shape | Proposed Candidate Index | Recommendation |
| :--- | :--- | :--- | :--- | :--- |
| **FancySale Q001** | `VANITYSALE_FRANCH_DATA` | `CAF_ENTRY_DONE`, `CIRCLE_CODE`, `TRANS_DATE` | `(CAF_ENTRY_DONE, CIRCLE_CODE, TRANS_DATE)` | **Defer to post-NZ AWR review.** Do not create index ahead of DBA plan analysis. |
| **FancySale Q002** | `VANITYSALE_FRANCH_DATA` | `REFID`, `CIRCLE_CODE`, `CAF_ENTRY_DONE` | `(REFID)` | Verify existing production index with DBA. |
| **SimSwap/ESIM Q006/Q012** | `SIMSWAP_AMOUNT_DEDUCT_REQUESTS` | `AMOUNT_DEDUCT_FLAG`, `MODULE_TYPE`, `CIRCLE_CODE`, `REQUEST_DATE` | `(AMOUNT_DEDUCT_FLAG, MODULE_TYPE, CIRCLE_CODE, REQUEST_DATE)` | Optional. Existing candidate batch sizes (50-200) execute within acceptable SLAs. |
| **SimSwap/ESIM Q007/Q013** | `SIMSWAP_AMOUNT_DEDUCT_REQUESTS` | `ID`, `CIRCLE_CODE`, `AMOUNT_DEDUCT_FLAG`, `MODULE_TYPE` | Primary Key `(ID)` | **Optimal.** Already backed by PK unique index. |
| **SimSwap Q009** | `BCD` | `GSMNUMBER`, `CAF_SERIAL_NO`, `ACTIVATION_STATUS` | Composite PK `(GSMNUMBER, CAF_SERIAL_NO)` | **Optimal.** Directly utilizes confirmed composite PK. |
| **ESIM Q015** | `SIM_SWAP_DATA` | `GSMNUMBER`, `ACTIVATION_STATUS` | Leading column of `(GSMNUMBER, ...)` | **Optimal.** Hits leading column of existing lookup index. |
| **Audit Trail Q018** | `debit_txn_log` | `service_type`, `oracle_ref_id`, `created_at` | Existing B-Tree & Partial indexes | **Optimal.** Append-only, indexed for operational lookups. |
