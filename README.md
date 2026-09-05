# Sanchar Mitra Pyro Services

This repository contains a Python FastAPI service that connects Sanchar Mitra data with the Pyro API.

It currently supports two business flows:

1. FRC recharge: finds newly activated subscribers, creates First Recharge requests, sends them to Pyro, receives final results, and updates Oracle/PostgreSQL.
2. Debit services: debits dealer/vendor wallets for service transactions. FancySale is implemented. SimSwap and ESIM adapters exist but are not implemented yet.

The service uses FastAPI, APScheduler, Oracle, PostgreSQL, httpx, and 3DES encryption.

## Beginner Summary

This service is an automated background worker. It does not provide a normal user interface. It runs scheduled jobs and exposes API endpoints for health checks, Pyro callbacks, and manual admin triggers.

For FRC recharge, the service:

1. Reads activated subscribers from Oracle.
2. Reads FRC plan, amount, dealer, and MPIN details from PostgreSQL.
3. Creates a tracking row in PostgreSQL.
4. Updates Oracle so the same subscriber is not picked again.
5. Sends the recharge request to Pyro.
6. Receives final success/failure through callback or status check.
7. Updates PostgreSQL and Oracle with the final result.

For FancySale debit, the service:

1. Reads eligible FancySale records from Oracle.
2. Claims them by marking them as processing.
3. Calls Pyro wallet-adjustment API.
4. Updates Oracle with success or failure.
5. Writes a PostgreSQL audit log.

## Important Terms

| Term | Meaning |
|---|---|
| FRC | First Recharge for a newly activated prepaid subscriber. |
| Pyro | External API used for recharge, status check, auth, and wallet debit. |
| BCD | Oracle activation table. Current code uses `CAF_ADMIN.BCD_LASKAR`. |
| CAF | Customer Application Form. `caf_serial_no` identifies a subscriber application. |
| GSM number | Subscriber mobile number. |
| CTOPUP | Dealer/vendor mobile number used for recharge/debit. |
| MPIN | PIN used for vendor wallet transaction. Must be protected. |
| Callback | HTTP POST from Pyro with final recharge result. |
| Status check | Fallback API call made when callback is missing. |
| Scheduler | Background job runner inside this service. |
| Adapter | Service-specific class used by the generic debit processor. |

## Architecture

```text
Oracle DB
  CAF_ADMIN.BCD_LASKAR
  CAF_ADMIN.VANITYSALE_FRANCH_DATA
        |
        v
FastAPI service
        |
        +--> PostgreSQL
        |      public.frc_pyro_request_data
        |      public.frc_txn_log
        |      public.debit_txn_log
        |
        +--> Pyro API
               /auth-api/authentication
               /auth-api/refresh-access-token
               /auth-api/generate-action-token
               /epin-vendor-api/recharge
               /epin-vendor-api/transaction-status
               /erp-stock-api/service-wallet-adjustment
```

External dependencies:

| Dependency | Used for |
|---|---|
| Oracle | Activation source data, BCD writeback, FancySale source/writeback. |
| PostgreSQL | FRC state table and audit logs. |
| Pyro API | Authentication, recharge, status check, and wallet debit. |

## Main Flows

### FRC Batch Population

Implemented in `app/batch/populator.py`.

Purpose: create pending FRC request rows.

Steps:

1. Read eligible records from Oracle `CAF_ADMIN.BCD_LASKAR`.
2. Eligibility requires `ACTIVATION_STATUS = 'C'`, `HLR_FINAL_ACT_DATE IS NOT NULL`, `FRC_FLOW_STATUS = 'NP'`, and `FRC_REQID IS NULL`.
3. Query PostgreSQL `cos_bcd` and `cos_bcd_dkyc` for matching GSM numbers.
4. Join `ctop_master` for dealer details.
5. Join `frc_plan_table` for recharge amount.
6. Encrypt MPIN using the Pyro secret key.
7. Insert rows into `public.frc_pyro_request_data` with `push_flag = 'N'`.
8. Update Oracle BCD row to `FRC_FLOW_STATUS = 'RQ'` and `FRC_REQID = reqid`.

`RQ` is the first idempotency guard. It prevents future batch runs from picking the same Oracle row again.

### FRC Recharge Dispatch

Implemented in `app/processor.py` and `app/pyro_client.py`.

Purpose: send pending FRC rows to Pyro.

Steps:

1. Fetch rows where `push_flag IN ('N', 'E')`.
2. Decrypt stored MPIN.
3. Generate a fresh Pyro action token.
4. Build recharge payload with dealer number, subscriber number, amount, client transaction ID, and MPIN.
5. Encrypt the payload using 3DES.
6. POST to `/epin-vendor-api/recharge`.
7. Write an audit row to `frc_txn_log`.
8. Update PostgreSQL and Oracle according to Pyro response.

Important outcomes:

| Pyro status | Meaning | PostgreSQL | Oracle |
|---|---|---|---|
| `2002` | Recharge registered | `push_flag = 'P'` | `FRC_FLOW_STATUS = 'W'` |
| `405` | Dealer balance insufficient | `push_flag = 'E'` | Not final yet |
| `506` | Invalid token | `push_flag = 'E'`, re-auth, abort current batch | Not final yet |
| Permanent error | Non-retryable failure | `push_flag = 'F'` | `ID` or `F` |
| Transient error | Retry until max retry limit | `E` or `F` | `F` after retries exhausted |

### FRC Callback

Implemented in `app/callback.py`.

Pyro posts final recharge result to:

```text
POST /callback/recharge
```

If deployed behind `/smpyro`, the public callback URL is typically:

```text
https://your-domain/smpyro/callback/recharge
```

Steps:

1. Parse callback JSON.
2. Read `data.transactionId`.
3. Find matching `frc_pyro_request_data` row by `pyro_trans_id`.
4. Ignore callback if the row is already terminal (`Y` or `F`).
5. Insert audit event into `frc_txn_log`.
6. On success, mark PostgreSQL `Y` and Oracle `P`.
7. On failure, mark PostgreSQL `F` and Oracle `ID` or `F`.

### FRC Status Check

Implemented in `app/status_checker.py`.

Purpose: recover rows where Pyro accepted the recharge but callback did not arrive.

It checks rows with `push_flag = 'P'` where `push_date` is between 2 and 60 minutes old.

Outcomes:

| Pyro status | Action |
|---|---|
| `2000` | Mark success in PostgreSQL and Oracle. |
| `902` | Mark final failure. |
| `901` | Keep waiting. |
| Other | Mark retry/failure based on current logic. |

### FRC Stuck Cleanup

Implemented in `app/cleanup/frc_cleanup.py`.

Rows older than the status-check window can remain stuck at `push_flag = 'P'`. The cleanup job marks very old `P` rows as failed. Default threshold is `FRC_STUCK_MINUTES = 75`.

### FancySale Debit

Implemented in `app/debit/processor.py`, `app/debit/pyro_client.py`, and `app/debit/services/fancysale.py`.

Steps:

1. Read eligible rows from Oracle `CAF_ADMIN.VANITYSALE_FRANCH_DATA`.
2. Eligible `CAF_ENTRY_DONE` values are `N`, `QM`, and `QB`.
3. Claim rows by setting `CAF_ENTRY_DONE = 'P'`.
4. Decrypt MPIN in Oracle using `CAF_ADMIN.F_DECRYPT(MPIN)`.
5. Validate MPIN length.
6. Build and encrypt wallet-adjustment payload.
7. POST to `/erp-stock-api/service-wallet-adjustment`.
8. If Pyro returns `statusCode = 200` and `status = SUCCESS`, mark Oracle row `Y`.
9. Otherwise mark Oracle row `R`.
10. Insert audit row into `public.debit_txn_log`.

### SimSwap and ESIM

Files exist, but adapters are stubs:

- `app/debit/services/simswap.py`
- `app/debit/services/esim.py`

Keep them disabled until implemented:

```env
SIMSWAP_ENABLED=false
ESIM_ENABLED=false
```

## Project Structure

```text
.
|-- main.py                         FastAPI entrypoint
|-- requirements.txt                Python dependencies
|-- Dockerfile                      Container build file
|-- docker-compose.yml              Container runtime config
|-- frc.conf                        Reverse proxy config sample
|-- .env.example                    Environment template
|-- app/
|   |-- config.py                   Reads settings
|   |-- encryption.py               3DES helpers
|   |-- scheduler.py                Scheduled jobs
|   |-- processor.py                FRC recharge processor
|   |-- pyro_client.py              FRC Pyro client
|   |-- status_checker.py           FRC status check fallback
|   |-- callback.py                 Pyro callback endpoint
|   |-- auth/token_manager.py       Pyro token manager
|   |-- batch/populator.py          FRC population job
|   |-- cleanup/frc_cleanup.py      FRC stuck cleanup
|   |-- db/oracle.py                Oracle pool and BCD operations
|   |-- db/postgres.py              PostgreSQL pool and FRC operations
|   |-- db/debit_log.py             Debit audit log writes
|   |-- debit/router.py             Debit/admin endpoints
|   |-- debit/processor.py          Generic debit processor
|   |-- debit/pyro_client.py        Debit Pyro client
|   |-- debit/token_managers.py     Debit token managers
|   |-- debit/services/             Debit adapters
|-- sql/create_tables.sql           FRC tables
|-- sql/debit_txn_log_table.sql     Debit audit table
|-- test_fetch.py                   Manual/test script
|-- test_single_recharge.py         Manual/test script
|-- test_token.py                   Manual/test script
```

Top-level `batch/` and `recharge/` packages appear to be legacy or duplicate modules. The active app imports from `app/`.

## File Guide

| File | Purpose |
|---|---|
| `main.py` | Creates FastAPI app, opens DB pools, authenticates Pyro, starts scheduler, registers routers. |
| `app/config.py` | Defines all environment-driven settings. |
| `app/encryption.py` | Encrypts/decrypts 3DES payloads compatible with Pyro. |
| `app/auth/token_manager.py` | Manages session, access, and action tokens. |
| `app/scheduler.py` | Defines all APScheduler jobs. |
| `app/db/oracle.py` | Oracle connection pool, BCD fetch, and BCD status updates. |
| `app/db/postgres.py` | PostgreSQL connection pool, FRC state table operations, FRC audit logging. |
| `app/batch/populator.py` | Creates FRC request rows from Oracle and PostgreSQL source data. |
| `app/processor.py` | Sends FRC recharge requests and handles retry/failure logic. |
| `app/pyro_client.py` | Calls Pyro FRC recharge and status APIs. |
| `app/callback.py` | Receives Pyro recharge callbacks. |
| `app/status_checker.py` | Polls Pyro for transactions missing callbacks. |
| `app/cleanup/frc_cleanup.py` | Marks old stuck FRC rows as failed. |
| `app/debit/router.py` | Exposes debit status and manual trigger endpoints. |
| `app/debit/processor.py` | Generic debit processing loop. |
| `app/debit/pyro_client.py` | Calls Pyro wallet-adjustment API. |
| `app/debit/services/fancysale.py` | FancySale Oracle mapping and writeback logic. |
| `app/debit/services/registry.py` | Maps service names to adapters. |

## Database Tables

### Oracle

| Table/function | Purpose |
|---|---|
| `CAF_ADMIN.BCD_LASKAR` | FRC activation source and status writeback. |
| `CAF_ADMIN.VANITYSALE_FRANCH_DATA` | FancySale debit source and status writeback. |
| `CAF_ADMIN.F_DECRYPT` | Decrypts FancySale MPIN in Oracle query. |

### PostgreSQL Source Tables Read by FRC

| Table | Purpose |
|---|---|
| `public.cos_bcd` | EKYC FRC source data. |
| `public.cos_bcd_dkyc` | DKYC FRC source data. |
| `public.ctop_master` | Dealer/vendor information. |
| `public.frc_plan_table` | FRC plan and amount mapping. |

### PostgreSQL Tables Created by This Repository

| Table | Script | Purpose |
|---|---|---|
| `public.frc_pyro_request_data` | `sql/create_tables.sql` | Main FRC control/state table. |
| `public.frc_txn_log` | `sql/create_tables.sql` | FRC API audit log. |
| `public.debit_txn_log` | `sql/debit_txn_log_table.sql` | Debit API audit log. |

## Status Flags

### FRC `push_flag`

| Flag | Meaning |
|---|---|
| `N` | New, ready to send to Pyro. |
| `P` | Pushed to Pyro, waiting for callback/status result. |
| `Y` | Success. |
| `F` | Final failure. |
| `E` | Retryable error. |

### Oracle BCD `FRC_FLOW_STATUS`

| Value | Meaning |
|---|---|
| `NP` | Not processed; eligible for pickup. |
| `RQ` | Request queued. |
| `W` | Waiting for callback. |
| `NR` | No response; status check started. |
| `P` | Processed successfully. |
| `ID` | Invalid data failure. |
| `F` | General failure. |

### FancySale `CAF_ENTRY_DONE`

| Value | Meaning |
|---|---|
| `N` | New/eligible. |
| `QM` | MPIN issue corrected; eligible again. |
| `QB` | Balance issue corrected; eligible again. |
| `P` | Processing. |
| `Y` | Success. |
| `R` | Rejected/failed. |

## Configuration

Configuration is loaded from environment variables and `.env` through `pydantic-settings`.

Create a local file:

```powershell
Copy-Item .env.example .env
```

Required FRC Pyro settings:

```env
PYRO_BASE_URL=https://host:port
PYRO_API_KEY=...
PYRO_LOGIN_ID=...
PYRO_PASSWORD=...
PYRO_SECRET_KEY=...
```

Required Oracle settings:

```env
ORACLE_USER=CAF_ADMIN
ORACLE_PASSWORD=...
ORACLE_DSN=host:1521/service_name
```

Required PostgreSQL settings:

```env
PG_HOST=localhost
PG_PORT=5432
PG_DATABASE=...
PG_USER=...
PG_PASSWORD=...
PG_MIN_CONN=2
PG_MAX_CONN=10
```

Scheduler and processing settings:

```env
CALLBACK_BASE_URL=https://your-domain/smpyro
ROOT_PATH=/smpyro
ADMIN_API_KEY=change_this_long_random_admin_key
CALLBACK_SECRET=
ENABLE_SCHEDULER=true
BATCH_POPULATION_INTERVAL_MINUTES=60
RUN_BATCH_ON_STARTUP=false
RUN_RECHARGE_ON_STARTUP=false
RUN_DEBIT_ON_STARTUP=false
RUN_CLEANUP_ON_STARTUP=true
ORACLE_BATCH_FETCH_SIZE=500
RECHARGE_BATCH_SIZE=500
STATUS_CHECK_MAX_ATTEMPTS=5
FRC_STUCK_MINUTES=75
FRC_CLAIM_TIMEOUT_MINUTES=15
```

FancySale settings:

```env
FANCYSALE_API_KEY=...
FANCYSALE_LOGIN_ID=...
FANCYSALE_PASSWORD=...
FANCYSALE_SECRET_KEY=...
FANCYSALE_ENABLED=true
FANCYSALE_BATCH_SIZE=200
FANCYSALE_INTERVAL_MINUTES=30
FANCYSALE_STUCK_MINUTES=10
```

Keep unimplemented services disabled:

```env
SIMSWAP_ENABLED=false
ESIM_ENABLED=false
```

Note: `.env.example` is currently incomplete for debit settings. See `issues.md`.

## Local Setup

Prerequisites:

- Python 3.12 recommended.
- Oracle DB access.
- PostgreSQL DB access.
- Pyro API access.
- Oracle client/network support for the `oracledb` package.

Create virtual environment:

```powershell
python -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install --upgrade pip
pip install -r requirements.txt
```

Prepare PostgreSQL tables:

```bash
psql -h <host> -U <user> -d <database> -f sql/create_tables.sql
psql -h <host> -U <user> -d <database> -f sql/debit_txn_log_table.sql
```

Run locally:

```powershell
uvicorn main:app --host 127.0.0.1 --port 8000 --proxy-headers --forwarded-allow-ips "*"
```

For local testing without background jobs:

```env
ENABLE_SCHEDULER=false
```

## Docker

Build and start:

```bash
docker compose build
docker compose up -d
```

The current compose file maps service port `8000` to host port `8010`:

```text
http://localhost:8010/health
```

View logs:

```bash
docker compose logs -f frc-recharge
```

Stop:

```bash
docker compose down
```

The Dockerfile uses one Uvicorn worker. This is intentional because the scheduler runs inside the app process. Multiple workers would start multiple schedulers.

## API Endpoints

| Method | Path | Purpose |
|---|---|---|
| GET | `/health` | Health check. |
| GET | `/token-status` | FRC token status. |
| GET | `/debit/status` | Debit token and service status. |
| POST | `/callback/recharge` | Pyro callback endpoint. |
| POST | `/admin/trigger-batch-population` | Run FRC population now. |
| POST | `/admin/trigger-recharge` | Run FRC recharge dispatch now. |
| POST | `/admin/trigger-status-check` | Run FRC status checker now. |
| POST | `/admin/trigger-frc-stuck-cleanup` | Run FRC stuck cleanup now. |
| POST | `/admin/trigger-debit/{service_type}` | Run one debit service now, e.g. `FANCYSALE`. |
| POST | `/admin/reset-stuck-debit/{service_type}` | Reset stuck debit rows. |

If deployed under `/smpyro`, public paths usually become `/smpyro/health`, `/smpyro/callback/recharge`, and `/smpyro/docs`.

## Scheduler Jobs

Actual schedule in `app/scheduler.py`:

| Job | Schedule |
|---|---|
| FRC daily auth | Every day at 00:05. |
| FRC batch population | Every `BATCH_POPULATION_INTERVAL_MINUTES`; startup run controlled by `RUN_BATCH_ON_STARTUP`. |
| FRC recharge dispatch | Every 30 minutes; startup run controlled by `RUN_RECHARGE_ON_STARTUP`. |
| FRC status check | Every 5 minutes. |
| Debit batch jobs | Every `FANCYSALE_INTERVAL_MINUTES`; startup run controlled by `RUN_DEBIT_ON_STARTUP`. |
| Stuck cleanup | Every 15 minutes; startup run controlled by `RUN_CLEANUP_ON_STARTUP`. |
| Debit daily auth | Every day at 00:10. |

Important: production defaults should keep money-moving startup runs disabled.

## Operations

Health check:

```bash
curl http://localhost:8010/health
```

Check FRC token status:

```bash
curl http://localhost:8010/token-status
```

Check debit status:

```bash
curl http://localhost:8010/debit/status
```

Run FRC population manually:

```bash
curl -X POST http://localhost:8010/admin/trigger-batch-population -H "X-Admin-API-Key: $ADMIN_API_KEY"
```

Run FRC recharge manually:

```bash
curl -X POST http://localhost:8010/admin/trigger-recharge -H "X-Admin-API-Key: $ADMIN_API_KEY"
```

Run FRC status check manually:

```bash
curl -X POST http://localhost:8010/admin/trigger-status-check -H "X-Admin-API-Key: $ADMIN_API_KEY"
```

Run FancySale debit manually:

```bash
curl -X POST http://localhost:8010/admin/trigger-debit/FANCYSALE -H "X-Admin-API-Key: $ADMIN_API_KEY"
```

Check today's FRC rows:

```sql
SELECT push_flag, COUNT(*)
FROM public.frc_pyro_request_data
WHERE batch_date = CURRENT_DATE
GROUP BY push_flag
ORDER BY push_flag;
```

Find failed FRC rows:

```sql
SELECT reqid, caf_serial_no, gsmno, push_remarks, last_error_msg, pyro_final_statuscode, updated_ts
FROM public.frc_pyro_request_data
WHERE push_flag = 'F'
ORDER BY updated_ts DESC
LIMIT 50;
```

View FRC API history for one request:

```sql
SELECT api_stage, pyro_status_code, pyro_status_text, is_success, duration_ms, logged_at
FROM public.frc_txn_log
WHERE frc_reqid = <reqid>
ORDER BY logged_at;
```

View FancySale debit logs:

```sql
SELECT service_type, oracle_ref_id, pyro_status_code, pyro_status_text, is_success, created_at
FROM public.debit_txn_log
WHERE service_type = 'FANCYSALE'
ORDER BY created_at DESC
LIMIT 50;
```

## Troubleshooting

### Service Does Not Start

Check logs:

```bash
docker compose logs frc-recharge
```

Common causes:

| Symptom | Likely cause |
|---|---|
| Settings validation error | Missing required `.env` variable. |
| Oracle pool error | Bad `ORACLE_DSN`, credentials, network, or Oracle client issue. |
| PostgreSQL pool error | Bad Postgres host/database/user/password. |
| Pyro auth failed | Bad Pyro URL/API key/login/password/secret. |

### No FRC Rows Are Inserted

Check Oracle eligibility:

```sql
SELECT COUNT(*)
FROM CAF_ADMIN.BCD_LASKAR
WHERE ACTIVATION_STATUS = 'C'
  AND HLR_FINAL_ACT_DATE IS NOT NULL
  AND FRC_FLOW_STATUS = 'NP'
  AND FRC_REQID IS NULL;
```

Check PostgreSQL source data:

```sql
SELECT COUNT(*)
FROM public.cos_bcd
WHERE frc_plan_name IS NOT NULL
  AND frc_plan_code IS NOT NULL
  AND frc_category_code IS NOT NULL
  AND frc_ctopup_number IS NOT NULL
  AND frc_ctopup_number_mpin IS NOT NULL;
```

If Oracle has rows but Postgres insert count is zero, check `ctop_master` and `frc_plan_table` joins.

### Recharge Rows Stay in `P`

Possible causes:

- Pyro did not call the callback URL.
- Callback URL is not publicly reachable.
- `pyro_trans_id` does not match callback transaction ID.
- Status checker is failing.
- Row is older than status-check window and needs stuck cleanup.

Query:

```sql
SELECT reqid, caf_serial_no, gsmno, pyro_trans_id, push_date, status_check_count, last_status_check_at
FROM public.frc_pyro_request_data
WHERE push_flag = 'P'
ORDER BY push_date;
```

### FancySale Rows Stay in `P`

Use the stuck reset endpoint carefully:

```bash
curl -X POST "http://localhost:8010/admin/reset-stuck-debit/FANCYSALE?stuck_minutes=10"
```

`stuck_minutes=0` resets all processing rows for that service. Use only during controlled recovery.

### SimSwap or ESIM Fails

They are not implemented. Disable them:

```env
SIMSWAP_ENABLED=false
ESIM_ENABLED=false
```

## Development Notes

### Adding a Debit Service

1. Implement an adapter in `app/debit/services/`.
2. Satisfy the protocol in `app/debit/services/base.py`.
3. Register it in `app/debit/services/registry.py`.
4. Add settings in `app/config.py` and `.env.example`.
5. Add schema and audit requirements.
6. Add tests for claim, mapping, success writeback, failure writeback, and stuck reset.
7. Keep it disabled until tested end to end.

### Why DB Calls Use `asyncio.to_thread()`

The code uses synchronous database libraries. To avoid blocking the FastAPI event loop, blocking DB operations are wrapped in `asyncio.to_thread()`.

### Why Uvicorn Uses One Worker

The scheduler is in-process. More than one worker would create more than one scheduler, causing duplicate scheduled processing.

## Production Checklist

Before production:

- Fill all required `.env` values.
- Keep `.env` out of Git.
- Run PostgreSQL schema scripts.
- Confirm Oracle table names and grants.
- Confirm `CAF_ADMIN.F_DECRYPT` is available for FancySale.
- Confirm Pyro callback URL is registered and reachable.
- Put the service behind TLS and a reverse proxy.
- Protect admin endpoints with authentication or network controls.
- Keep Uvicorn worker count at `1` unless scheduler is moved to a separate process.
- Keep `SIMSWAP_ENABLED=false` and `ESIM_ENABLED=false` until implemented.
- Review `issues.md` for known production-readiness gaps.

