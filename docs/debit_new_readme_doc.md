# Sanchar Mitra — Debit Service

A standalone backend service that processes wallet adjustments (debits) for Sanchar Mitra services via the Pyro API. Supports **FancySale**, **SimSwap**, and **ESIM**.

---

## Table of Contents

1. [Architecture Overview](#architecture-overview)
2. [Complete Data Flow — Step by Step](#complete-data-flow--step-by-step)
   - [FancySale](#fancysale-data-flow)
   - [SimSwap](#simswap-data-flow)
   - [ESIM](#esim-data-flow)
3. [Oracle State Machine](#oracle-state-machine)
4. [Encryption](#encryption)
5. [Prerequisites](#prerequisites)
6. [Configuration](#configuration)
7. [Running the Service](#running-the-service)
8. [API Endpoints](#api-endpoints)
9. [Scheduler Jobs](#scheduler-jobs)
10. [Adding a New Service](#adding-a-new-service)
11. [Database Schema](#database-schema)

---

## Architecture Overview

The service is built on **FastAPI** with an **APScheduler** background scheduler. Each supported service (FancySale, SimSwap, ESIM) has its own **adapter** class that encapsulates the Oracle table access and field mapping for that service. A single generic **processor** (`processor.py`) drives all adapters through an identical pipeline, so adding a new service requires only a new adapter file — nothing else changes.

```
┌─────────────────────────────────────────────────┐
│                   APScheduler                   │
│  debit_fancysale  debit_simswap  debit_esim     │
│  (every N min)    (every N min)  (every N min)  │
│  stuck_cleanup (every 15 min)                   │
│  debit_daily_auth (00:10 daily)                 │
└───────────────────┬─────────────────────────────┘
                    │ run_debit_batch(adapter)
                    ▼
┌─────────────────────────────────────────────────┐
│           Generic Processor (processor.py)       │
│  1. fetch_and_claim()                           │
│  2. map_to_pyro_params()                        │
│  3. wallet_adjustment() → Pyro HTTP POST        │
│  4. mark_success() / mark_failed()              │
│  5. async_insert_debit_txn_log() → Postgres     │
└────────────┬───────────────────┬────────────────┘
             │                   │
             ▼                   ▼
    ┌─────────────────┐  ┌──────────────────────┐
    │  Oracle DB      │  │  Postgres DB          │
    │  CAF tables     │  │  debit_txn_log        │
    └─────────────────┘  └──────────────────────┘
```

---

## Complete Data Flow — Step by Step

### FancySale Data Flow

#### Phase 1 — Batch Claim (runs every `FANCYSALE_INTERVAL_MINUTES`, default 30 min)

```
STEP 1: Query Oracle — CAF_ADMIN.VANITYSALE_FRANCH_DATA
        Filter:  CAF_ENTRY_DONE IN ('N', 'QM', 'QB')
                 ┌─ 'N'  → new, not yet processed
                 ├─ 'QM' → MPIN corrected by Sanchar Mitra, ready to retry
                 └─ 'QB' → balance topped up by Sanchar Mitra, ready to retry
        Order:   TRANS_DATE ASC  (oldest first)
        Limit:   FANCYSALE_BATCH_SIZE rows (default 200)

        Columns fetched:
          REFID, CTOPUPNO, FANCY_NO, AMOUNT,
          CAF_ADMIN.F_DECRYPT(MPIN) AS plain_mpin,   ← decrypted inside Oracle
          MPIN_LENGTH, SS_REQUEST_ID, CSCCODE,
          CIRCLE_CODE, TRANS_DATE, MODULE_TYPE, CAF_ENTRY_DONE

STEP 2: Claim each candidate row (optimistic lock pattern)
        For each candidate:
          UPDATE VANITYSALE_FRANCH_DATA
          SET    CAF_ENTRY_DONE = 'P',
                 CAF_ENTRY_DATE = SYSDATE,
                 PYRO_REMARKS   = 'Processing started'
          WHERE  REFID          = :refid
            AND  CAF_ENTRY_DONE IN ('N', 'QM', 'QB')  ← re-check prevents double-claim

        If rowcount == 0 → another worker claimed it first; skip silently
        If rowcount == 1 → row is ours; add to claimed list

        Note: FOR UPDATE SKIP LOCKED is unavailable because
              VANITYSALE_FRANCH_DATA is a view containing
              the F_DECRYPT() function call (ORA-02014).
              The per-row optimistic update replaces the lock guarantee.
```

#### Phase 2 — Pyro Wallet Adjustment

```
STEP 3: Validate and map Oracle row → Pyro request
        Checks performed:
          • plain_mpin must be non-empty
          • len(plain_mpin) must match MPIN_LENGTH column
          • CTOPUPNO and FANCY_NO must be non-NULL

        Pyro request fields:
          clientId     ← SS_REQUEST_ID
          sourceMsisdn ← CTOPUPNO (int → string)
          destMsisdn   ← FANCY_NO  (int → string)
          amount       ← AMOUNT    (float)
          mpin         ← plain_mpin (already decrypted by Oracle)
          remarks      ← MODULE_TYPE (e.g. 'FANCYSALE')
          serviceType  ← 'FANCYSALE'

        If validation fails → mark_failed() with '[MPIN_ERR]' prefix
                            → skip Pyro call
                            → log to debit_txn_log as permanent failure

STEP 4: Authenticate — get a valid Pyro access token
        Token manager checks JWT exp claim (with 60s buffer).
        If expired → POST /auth-api/refresh-access-token
        If refresh fails → POST /auth-api/authentication (full re-login)
        If both fail → abort record with token error, log to debit_txn_log

STEP 5: Encrypt request body and POST to Pyro
        Payload is serialised to JSON then encrypted with 3DES-ECB
        using FANCYSALE_SECRET_KEY (SHA-1 key derivation, PKCS5 padding,
        Base64 output without trailing '=').

        Headers:
          apiKey:       FANCYSALE_API_KEY
          accessToken:  (JWT from Step 4)
          sessionToken: (from initial authentication)

        POST {PYRO_BASE_URL}/erp-stock-api/service-wallet-adjustment
        Timeout: 30 seconds

STEP 6: Parse Pyro response
        Pyro returns plain JSON in the current environment.
        Fallback: attempt 3DES decrypt if plain JSON parse fails.

        Success criteria: statusCode IN (200, 2000) AND status == 'SUCCESS'

        On success:
          pyroId       → TRANSACTION_ID in Oracle
          balanceBefore, balanceAfter → included in PYRO_REMARKS
```

#### Phase 3 — Writeback

```
STEP 7: Write result back to Oracle
        On SUCCESS:
          UPDATE VANITYSALE_FRANCH_DATA
          SET    CAF_ENTRY_DONE = 'Y',
                 TRANSACTION_ID = :pyro_txn_id,
                 PYRO_REMARKS   = '[200] SUCCESS pyroId=... balBefore=... balAfter=...',
                 PROCESSED_SM   = 'Y'
          WHERE  REFID          = :refid
            AND  CAF_ENTRY_DONE = 'P'

        On FAILURE:
          UPDATE VANITYSALE_FRANCH_DATA
          SET    CAF_ENTRY_DONE = 'R',
                 PYRO_REMARKS   = '[<statusCode>] <message>'
          WHERE  REFID          = :refid
            AND  CAF_ENTRY_DONE = 'P'

        Note: For FancySale, the BCD table is updated by Praveen Sir's
              existing scheduled process — NOT by this service.

STEP 8: Write audit log to Postgres (debit_txn_log)
        service_type = 'FANCYSALE'
        Captures: oracle_ref_id, client_id, source/dest MSISDN, amount,
                  HTTP request/response, timing, pyro_txn_id, success flag.
        MPIN is masked to '***' before persistence.
        Non-fatal: log errors are caught and never re-raised.
```

---

### SimSwap Data Flow

#### Phase 1 — Batch Claim (runs every `SIMSWAP_INTERVAL_MINUTES`, default 30 min)

```
STEP 1: Query Oracle — CAF_ADMIN.SIMSWAP_AMOUNT_DEDUCT_REQUESTS
        Filter:  CAF_ENTRY_DONE IN ('N', 'QM', 'QB')
        Order:   TRANS_DATE ASC  (oldest first)
        Limit:   SIMSWAP_BATCH_SIZE rows (default 200)

STEP 2: Claim each candidate row (same optimistic lock pattern as FancySale)
        UPDATE SIMSWAP_AMOUNT_DEDUCT_REQUESTS
        SET    CAF_ENTRY_DONE = 'P',
               CAF_ENTRY_DATE = SYSDATE,
               PYRO_REMARKS   = 'Processing started'
        WHERE  <primary_key> = :pk
          AND  CAF_ENTRY_DONE IN ('N', 'QM', 'QB')
```

#### Phase 2 — Pyro Wallet Adjustment

```
STEP 3: Validate and map Oracle row → Pyro request
        serviceType = 'SIMSWAP'
        (Field mapping mirrors FancySale, adapted for SIMSWAP table columns)

STEP 4–6: Same token auth + 3DES encrypt + POST flow as FancySale
           using SIMSWAP_API_KEY / SIMSWAP_SECRET_KEY credentials.
```

#### Phase 3 — Writeback

```
STEP 7: Write result back to Oracle
        On SUCCESS:
          UPDATE SIMSWAP_AMOUNT_DEDUCT_REQUESTS
          SET    CAF_ENTRY_DONE = 'Y',
                 TRANSACTION_ID = :pyro_txn_id,
                 PYRO_REMARKS   = '...'
          WHERE  <primary_key> = :pk
            AND  CAF_ENTRY_DONE = 'P'

          ALSO:
          UPDATE CAF_ADMIN.BCD
          SET    ACTIVATION_STATUS = 'AI'
          WHERE  GSMNUMBER         = :gsmnumber
            AND  ACTIVATION_STATUS = 'IF'

        On FAILURE:
          UPDATE SIMSWAP_AMOUNT_DEDUCT_REQUESTS
          SET    CAF_ENTRY_DONE = 'R',
                 PYRO_REMARKS   = '...'
          WHERE  <primary_key> = :pk
            AND  CAF_ENTRY_DONE = 'P'

STEP 8: Write audit log to Postgres
        service_type = 'SIMSWAP'
```

---

### ESIM Data Flow

#### Phase 1 — Batch Claim (runs every `ESIM_INTERVAL_MINUTES`, default 30 min)

```
STEP 1: Query Oracle — CAF_ADMIN.SIMSWAP_AMOUNT_DEDUCT_REQUESTS
        (ESIM and SimSwap share the same source table)
        Filter:  CAF_ENTRY_DONE IN ('N', 'QM', 'QB')
        Order:   TRANS_DATE ASC
        Limit:   ESIM_BATCH_SIZE rows (default 200)

STEP 2: Claim each candidate row (same optimistic lock pattern)
```

#### Phase 2 — Pyro Wallet Adjustment

```
STEP 3: Validate and map Oracle row → Pyro request
        serviceType = 'ESIM'
        using ESIM_API_KEY / ESIM_SECRET_KEY credentials.

STEP 4–6: Same token auth + 3DES encrypt + POST flow as FancySale.
```

#### Phase 3 — Writeback

```
STEP 7: Write result back to Oracle
        On SUCCESS:
          UPDATE SIMSWAP_AMOUNT_DEDUCT_REQUESTS
          SET    CAF_ENTRY_DONE = 'Y',
                 TRANSACTION_ID = :pyro_txn_id,
                 PYRO_REMARKS   = '...'
          WHERE  <primary_key> = :pk
            AND  CAF_ENTRY_DONE = 'P'

          ALSO:
          UPDATE CAF_ADMIN.SIM_SWAP_DATA
          SET    ACTIVATION_STATUS = 'AI'
          WHERE  GSMNUMBER         = :gsmnumber
            AND  ACTIVATION_STATUS = 'IF'

        On FAILURE:
          UPDATE SIMSWAP_AMOUNT_DEDUCT_REQUESTS
          SET    CAF_ENTRY_DONE = 'R',
                 PYRO_REMARKS   = '...'
          WHERE  <primary_key> = :pk
            AND  CAF_ENTRY_DONE = 'P'

STEP 8: Write audit log to Postgres
        service_type = 'ESIM'
```

---

## Oracle State Machine

All three services share the same `CAF_ENTRY_DONE` state machine:

```
                    ┌─────────────────────────┐
                    │  N  (New / Eligible)     │◄──────────────────┐
                    └────────────┬────────────┘                   │
                                 │ fetch_and_claim()               │ reset_stuck_
                                 ▼                                 │ processing()
                    ┌─────────────────────────┐                   │
                    │  P  (Processing)         │───── stuck? ──────┘
                    └──────┬──────────┬────────┘
                           │          │
               mark_success()    mark_failed()
                           │          │
                           ▼          ▼
              ┌──────────────┐  ┌───────────────┐
              │ Y (Success)  │  │  R (Rejected) │
              └──────────────┘  └───────────────┘

  QM → P : MPIN was corrected by Sanchar Mitra; re-picked on next batch
  QB → P : Balance was topped up by Sanchar Mitra; re-picked on next batch
```

| Status | Meaning | Set By |
|--------|---------|--------|
| `N` | New, eligible for processing | Source system / stuck cleanup |
| `QM` | Queued after MPIN fix | Sanchar Mitra operator |
| `QB` | Queued after balance top-up | Sanchar Mitra operator |
| `P` | Processing (claimed by this service) | `fetch_and_claim()` |
| `Y` | Successfully debited | `mark_success()` |
| `R` | Rejected / failed | `mark_failed()` |

---

## Encryption

Pyro uses **3DES-ECB** (DESede) for request body encryption, matching its Java `EncryptionUtilsClientShared` implementation.

```
Key derivation:  SHA-1(secret_key_string) → 20 bytes + 4 zero bytes = 24-byte key
Mode:            ECB (no IV)
Padding:         PKCS5 (8-byte DES block size)
Output:          Base64 without trailing '='
```

**What is encrypted vs. plain:**

| Data | Encrypted? | Notes |
|------|-----------|-------|
| Pyro auth request body | Yes | 3DES with service secret key |
| Pyro debit request body | Yes | 3DES with service secret key |
| Pyro responses | No | Plain JSON in current environment |
| MPIN at rest in Oracle | Yes | Oracle's own `CAF_ADMIN.F_DECRYPT()` — decrypted at SELECT time |
| MPIN in Postgres log | Masked | Replaced with `***` before insert |

---

## Prerequisites

- **Python 3.10+** (if running locally)
- **Docker & Docker Compose** (if running via containers)
- Access to the **Oracle Database** (`CAF_ADMIN` schema with CAF tables)
- Access to the **PostgreSQL Database** (for `debit_txn_log` audit table)
- Valid credentials for the **Pyro API** (one set per service)

### One-Time DB Setup

**PostgreSQL** — run before first deployment:
```bash
psql -U <user> -d <database> -f debit_txn_log_table.sql
```

**Oracle** — reset any pre-existing stuck-P records with a NULL `CAF_ENTRY_DATE`
(these are excluded from the automated cleanup job intentionally):
```sql
UPDATE CAF_ADMIN.VANITYSALE_FRANCH_DATA
SET    CAF_ENTRY_DONE = 'N',
       PYRO_REMARKS   = 'Manual reset: pre-deployment cleanup'
WHERE  CAF_ENTRY_DONE = 'P'
  AND  CAF_ENTRY_DATE IS NULL;
COMMIT;
```

---

## Configuration

All configuration is via environment variables. Copy `_env` to `.env` and fill in the values.

### Core Variables

| Variable | Description | Default |
|----------|-------------|---------|
| `PYRO_BASE_URL` | Base URL for the Pyro API | _(required)_ |
| `PYRO_REQUEST_TIMEOUT_SECONDS` | HTTP timeout for all Pyro calls | `30.0` |
| `ADMIN_API_KEY` | Secret for `/admin/*` endpoint authentication | _(required)_ |
| `ROOT_PATH` | FastAPI root path (for reverse proxy) | `/smpyro` |

### Oracle DB

| Variable | Description |
|----------|-------------|
| `ORACLE_USER` | Oracle username |
| `ORACLE_PASSWORD` | Oracle password |
| `ORACLE_DSN` | `host:port/service_name` |

### PostgreSQL DB

| Variable | Description | Default |
|----------|-------------|---------|
| `PG_HOST` | Postgres host | _(required)_ |
| `PG_PORT` | Postgres port | `5432` |
| `PG_DATABASE` | Database name | _(required)_ |
| `PG_USER` | Postgres username | _(required)_ |
| `PG_PASSWORD` | Postgres password | _(required)_ |
| `PG_MIN_CONN` | Connection pool minimum | `2` |
| `PG_MAX_CONN` | Connection pool maximum | `10` |

### Scheduler

| Variable | Description | Default |
|----------|-------------|---------|
| `ENABLE_SCHEDULER` | Enable background jobs | `true` |
| `RUN_DEBIT_ON_STARTUP` | Run debit batch immediately on start | `false` |
| `RUN_CLEANUP_ON_STARTUP` | Run stuck-record cleanup on start | `true` |

### Per-Service Variables

Each service (`FANCYSALE`, `SIMSWAP`, `ESIM`) has an identical set of variables:

| Variable | Description | Default |
|----------|-------------|---------|
| `<SVC>_API_KEY` | Pyro API key for this service | `""` |
| `<SVC>_LOGIN_ID` | Pyro login ID | `""` |
| `<SVC>_PASSWORD` | Pyro password | `""` |
| `<SVC>_SECRET_KEY` | 3DES encryption key | `""` |
| `<SVC>_ENABLED` | Enable this service | `false` (`true` for FANCYSALE) |
| `<SVC>_BATCH_SIZE` | Records per scheduler run | `200` |
| `<SVC>_INTERVAL_MINUTES` | Scheduler interval | `30` |
| `<SVC>_STUCK_MINUTES` | Minutes before a P record is considered stuck | `10` |

---

## Running the Service

### Using Docker (Recommended for Production)

```bash
# Build and start
docker-compose up -d --build

# Using a pre-built image (production)
docker-compose -f docker-compose-prod.yml up -d

# View logs
docker logs -f pyro_debit_service
```

The service exposes port **8010**.

### Running Locally (Development)

```bash
python -m venv .venv
source .venv/bin/activate        # Windows: .venv\Scripts\activate
pip install -r requirements.txt

# Copy and edit environment
cp _env .env

uvicorn main:app --host 127.0.0.1 --port 8010
```

---

## API Endpoints

### Health & Readiness

| Method | Path | Description |
|--------|------|-------------|
| `GET` | `/health` | Basic liveness check. Returns `{"status": "ok"}` |
| `GET` | `/ready` | Checks Oracle pool, Postgres pool, and scheduler status |

### Debit Status

| Method | Path | Auth | Description |
|--------|------|------|-------------|
| `GET` | `/debit/status` | None | Token status and config for all three debit services |

### Admin Endpoints

All `/admin/*` endpoints require the `X-Admin-API-Key` header matching `ADMIN_API_KEY`.

| Method | Path | Description |
|--------|------|-------------|
| `POST` | `/admin/trigger-debit/{service_type}` | Manually trigger a debit batch (e.g. `FANCYSALE`) |
| `POST` | `/admin/reset-stuck-debit/{service_type}?stuck_minutes=10` | Reset stuck-P records back to N |

**reset-stuck-debit parameters:**

- Omit `stuck_minutes` → uses the service's configured threshold (safe default)
- Pass `stuck_minutes=0` → resets ALL P records regardless of age (use with caution)

---

## Scheduler Jobs

When `ENABLE_SCHEDULER=true`, four background jobs run:

| Job ID | Trigger | What it does |
|--------|---------|--------------|
| `debit_fancysale` | Every `FANCYSALE_INTERVAL_MINUTES` | Debit batch for FancySale |
| `debit_simswap` | Every `SIMSWAP_INTERVAL_MINUTES` | Debit batch for SimSwap |
| `debit_esim` | Every `ESIM_INTERVAL_MINUTES` | Debit batch for ESIM |
| `stuck_cleanup` | Every 15 minutes | Resets P records stuck beyond each service's `stuck_minutes` threshold |
| `debit_daily_auth` | Daily at 00:10 | Re-authenticates all three Pyro token managers |

Disabled services (`ENABLED=false`) are registered in the scheduler but skipped at runtime — the jobs are harmless no-ops.

---

## Adding a New Service

The architecture is designed so that adding a new debit service (e.g. `PORTING`) requires changes in only three places:

1. **Create `app/debit/services/porting.py`** — implement all six methods of the `DebitServiceAdapter` protocol and set `implemented = True`.
2. **Add credentials to `config.py`** — add `porting_api_key`, `porting_enabled`, etc.
3. **Register in `app/debit/services/registry.py`** — add `"PORTING": PortingAdapter(...)` to `SERVICE_REGISTRY`.

No changes are needed in `processor.py`, `scheduler.py`, `router.py`, or `pyro_client.py`.

### Methods to implement

| Method | Responsibility |
|--------|---------------|
| `fetch_and_claim(batch_size)` | SELECT eligible rows + optimistic UPDATE to P |
| `map_to_pyro_params(record)` | Map Oracle row dict → `wallet_adjustment()` kwargs |
| `get_record_ref(record)` | Return a string primary key for logging |
| `mark_success(record, pyro_txn_id, remarks)` | UPDATE row to Y in Oracle |
| `mark_failed(record, remarks)` | UPDATE row to R in Oracle |
| `reset_stuck_processing(stuck_minutes)` | Reset old P rows back to N; return count |

---

## Database Schema

### Postgres — `public.debit_txn_log`

Full audit trail for every Pyro API call across all services.

| Column | Type | Description |
|--------|------|-------------|
| `id` | BIGSERIAL | Auto-increment primary key |
| `service_type` | VARCHAR(20) | `FANCYSALE` / `SIMSWAP` / `ESIM` |
| `oracle_ref_id` | VARCHAR(50) | Source Oracle PK (e.g. REFID for FancySale) |
| `client_id` | VARCHAR(50) | `clientId` sent to Pyro (SS_REQUEST_ID) |
| `source_msisdn` | VARCHAR(15) | Source MSISDN (CTOPUPNO) |
| `dest_msisdn` | VARCHAR(15) | Destination MSISDN (FANCY_NO / GSMNUMBER) |
| `amount` | NUMERIC(12,2) | Debit amount |
| `api_stage` | VARCHAR(30) | `DEBIT` / `AUTH` / `CLEANUP` |
| `api_endpoint` | VARCHAR(200) | Full URL called |
| `attempt_no` | SMALLINT | Attempt number (currently always 1) |
| `request_body` | TEXT | JSON payload with MPIN masked to `***` |
| `response_http_code` | SMALLINT | HTTP status code from Pyro |
| `response_body` | TEXT | Raw Pyro response body |
| `pyro_status_code` | INT | `statusCode` field from Pyro JSON |
| `pyro_status_text` | VARCHAR(50) | `status` field from Pyro JSON |
| `pyro_txn_id` | VARCHAR(50) | `pyroId` from Pyro success response |
| `call_started_at` | TIMESTAMPTZ | UTC timestamp before HTTP call |
| `call_ended_at` | TIMESTAMPTZ | UTC timestamp after HTTP call |
| `duration_ms` | INT | Round-trip duration in milliseconds |
| `is_success` | CHAR(1) | `Y` or `N` |
| `is_perm_failure` | CHAR(1) | `Y` if failure is non-retryable (e.g. bad MPIN data) |
| `error_class` | VARCHAR(100) | Python exception class name |
| `error_detail` | TEXT | Exception message |
| `created_at` | TIMESTAMPTZ | Row creation timestamp |

**Indexes:**
- `(service_type, oracle_ref_id)` — lookup by service + Oracle PK
- `(service_type, created_at DESC)` — recent records per service
- `(pyro_txn_id)` WHERE NOT NULL — lookup by Pyro transaction ID

### Oracle — FancySale (`CAF_ADMIN.VANITYSALE_FRANCH_DATA`)

Key columns written by this service:

| Column | Written Value | When |
|--------|--------------|------|
| `CAF_ENTRY_DONE` | `P` → `Y` or `R` | Claim / writeback |
| `CAF_ENTRY_DATE` | `SYSDATE` | On claim |
| `TRANSACTION_ID` | Pyro `pyroId` | On success |
| `PYRO_REMARKS` | Status message | On claim, success, failure |
| `PROCESSED_SM` | `Y` | On success |

### Oracle — SimSwap / ESIM (`CAF_ADMIN.SIMSWAP_AMOUNT_DEDUCT_REQUESTS`)

Key columns written by this service:

| Column | Written Value | When |
|--------|--------------|------|
| `CAF_ENTRY_DONE` | `P` → `Y` or `R` | Claim / writeback |
| `CAF_ENTRY_DATE` | `SYSDATE` | On claim |
| `TRANSACTION_ID` | Pyro `pyroId` | On success |
| `PYRO_REMARKS` | Status message | On claim, success, failure |

Additionally, on SimSwap success: `CAF_ADMIN.BCD.ACTIVATION_STATUS` set to `AI` where `GSMNUMBER` matches and current status is `IF`.

Additionally, on ESIM success: `CAF_ADMIN.SIM_SWAP_DATA.ACTIVATION_STATUS` set to `AI` where `GSMNUMBER` matches and current status is `IF`.