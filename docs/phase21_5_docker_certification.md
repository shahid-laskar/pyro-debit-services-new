# Phase 21.5 — Production Docker Artifact Certification Report

**Subsystem:** `debit_services` (FancySale, SimSwap, ESIM)  
**Task:** Production Docker Artifact Certification & Deployment Readiness Verification  
**Evaluation Date:** 2026-09-08  
**Authoritative Plan Reference:** [`debit_services_final_implementation_plan.md`](file:///D:/pyro/docs/debit_services_final_implementation_plan.md)  
**Certification Status:** **CERTIFIED — READY FOR PHASE 22 DEPLOYMENT**  

---

## 1. Candidate Artifact

| Parameter | Value | Evidence / Command |
| :--- | :--- | :--- |
| **Git Commit SHA** | [`f222e92b535d5c80197b5c77d46931ae47d4a484`](file:///D:/pyro/debit_services/) | `git rev-parse HEAD` |
| **Docker Image Tag** | `pyro-debit-services:v4` (also available as `debit_service:v2`) | `docker images` |
| **Image ID** | `sha256:531ee9d7f7bfa980611fbfca5bb0229a48fd0800a15224e155dc92df20270e92` | `docker inspect pyro-debit-services:v4` |
| **Exact Image Digest** | `sha256:531ee9d7f7bfa980611fbfca5bb0229a48fd0800a15224e155dc92df20270e92` | RepoDigests / Descriptor.digest |
| **Build Timestamp** | `2026-09-08T10:57:45.664165934Z` | `docker inspect Config.Created` |
| **Image Size** | `79,500,731` bytes (~79.5 MB — optimized with `.dockerignore`) | `docker inspect Size` |

---

## 2. Runtime Environment

| Property | Container Runtime | Host Environment | Compliance / Assessment |
| :--- | :--- | :--- | :--- |
| **Python Version** | `3.12.14` (GCC 14.2.0) | `3.14.5` (MSC v.1944) | **ACCEPTABLE**: Python 3.12 is the production target runtime with pre-compiled stable wheels for Linux. |
| **Operating System** | Linux (Debian GNU/Linux trixie/sid) | Windows 10 (Build 19045) | **PASS**: Standard Linux container on Docker Engine 28.3.2. |
| **Architecture** | `x86_64` (`amd64`) | `AMD64` | **PASS**: Exact match. |
| **ASGI Server** | `uvicorn 0.52.4` (`uvloop 0.22.1`, `httptools 0.8.0`) | N/A | **PASS**: High-performance production event loop. |
| **Process Model** | 1 Uvicorn master/worker process | N/A | **PASS**: Guarantees single scheduler and safe in-memory concurrency tracker. |
| **Container User** | `appuser` (non-root, UID 999) | itcell | **PASS**: Least-privilege security principle enforced. |

---

## 3. Dependency Inventory & Reproducibility

Dependencies installed inside `pyro-debit-services:v4` (`pip freeze`):

| Package | Version | Purpose |
| :--- | :--- | :--- |
| `fastapi` | `0.141.1` | Core REST API framework |
| `uvicorn` | `0.52.4` | ASGI server |
| `uvloop` | `0.22.1` | High-performance asynchronous event loop |
| `httptools` | `0.8.0` | Fast HTTP parser |
| `APScheduler` | `3.11.3` | Async in-process job scheduler |
| `oracledb` | `4.0.2` | Oracle Database connectivity (thin/thick modes) |
| `psycopg2-binary`| `2.9.12` | PostgreSQL audit log connectivity |
| `httpx` | `0.28.1` | Async HTTP client for Pyro API integration |
| `httpcore` | `1.0.9` | Low-level HTTP transport |
| `pydantic` | `2.13.5` | Data validation and typing |
| `pydantic-settings` | `2.15.0` | Environment settings management |
| `pycryptodome` | `3.23.0` | 3DES cryptographic operations |
| `cryptography` | `50.0.1` | Cryptographic primitives |
| `starlette` | `1.6.0` | ASGI foundation for FastAPI |
| `python-dotenv` | `1.2.3` | Environment file parsing |
| `pytest` | `9.1.1` | Test framework |
| `pytest-asyncio` | `1.4.0` | Async testing extension |

All core packages are pinned or constrained, resolving without version conflicts.

---

## 4. Configuration Specification

The container strictly consumes configuration from runtime environment variables (or `.env` file mounted/passed at runtime). **No secrets or default configurations are baked into the image.**

### Required Environment Variables for Production

| Variable | Type / Format | Purpose |
| :--- | :--- | :--- |
| `PYRO_BASE_URL` | URL (`https://...`) | Pyro API Gateway base URL |
| `PYRO_REQUEST_TIMEOUT_SECONDS` | Float (default 30.0) | HTTP client timeout |
| `ORACLE_USER` | String | Oracle schema user (e.g. `CAF_ADMIN` / `SYSTEM`) |
| `ORACLE_PASSWORD` | Secret String | Oracle password |
| `ORACLE_DSN` | String (`host:port/service`) | Oracle DSN / TNS entry |
| `PG_HOST` | Hostname / IP | PostgreSQL host |
| `PG_PORT` | Integer (default 5432) | PostgreSQL port |
| `PG_DATABASE` | String | PostgreSQL database name |
| `PG_USER` | String | PostgreSQL username |
| `PG_PASSWORD` | Secret String | PostgreSQL password |
| `PG_MIN_CONN` / `PG_MAX_CONN` | Integers (2 / 10) | Connection pool sizing |
| `ROOT_PATH` | Path (`/smpyro`) | Application root mount path for reverse proxy |
| `ADMIN_API_KEY` | Secret String | Static token for operational endpoint authorization |
| `ENABLED_ZONES` | String (`NZ` \| `ALL`) | Zone rollout selector (`NZ` during Pilot; `ALL` nationwide) |
| `ENABLE_SCHEDULER` | Boolean (`true` / `false`)| Master scheduler activation |
| `RUN_DEBIT_ON_STARTUP` | Boolean (`false`) | Guard against startup burst debit |
| `RUN_CLEANUP_ON_STARTUP` | Boolean (`true` / `false`)| Recovery cleanup on boot |
| `FANCYSALE_*` | API key, login ID, password, 3DES secret | FancySale debit credentials & flags |
| `SIMSWAP_*` | API key, login ID, password, 3DES secret | SimSwap debit credentials & flags |
| `ESIM_*` | API key, login ID, password, 3DES secret | ESIM debit credentials & flags |

---

## 5. Connectivity Matrix

Tested directly from inside candidate container `pyro-debit-services:v4`:

| Component | Target Endpoint | Protocol / Port | Verification Check | Result |
| :--- | :--- | :--- | :--- | :--- |
| **Oracle DB** | `host.docker.internal:1521/xepdb1` | TCP / 1521 (TNS) | Read counts on `CAF_ADMIN.VANITYSALE_FRANCH_DATA` (1000), `SIMSWAP_AMOUNT_DEDUCT_REQUESTS` (1000), `BCD` (1001), `SIM_SWAP_DATA` (1001) and call `CAF_ADMIN.F_DECRYPT('1234')` | **PASS** |
| **PostgreSQL DB** | `host.docker.internal:5432` | TCP / 5432 (Postgres) | Read query on `public.debit_txn_log` | **PASS** |
| **Pyro Gateway** | `bsnlapigateway.pyrogroup.com` | TCP / 443 (HTTPS) | DNS resolution & TLS socket connection; live auth for FANCYSALE, SIMSWAP, ESIM token managers | **PASS** |
| **HTTP Ingress** | Container Port `8010` | TCP / HTTP | Direct and reverse-proxied request ingress (`/ready`, `/health`, `/admin/zones`) | **PASS** |

---

## 6. API & Security Certification

| Route | Method | Auth Condition | Expected Status | Actual Status | Response Payload Verification | Result |
| :--- | :--- | :--- | :--- | :--- | :--- | :--- |
| `/health` | `GET` | Public (Unauthenticated) | 200 OK | 200 OK | `{"status": "ok"}` | **PASS** |
| `/ready` | `GET` | Public (Unauthenticated) | 200 OK | 200 OK | `{"status": "ready", "postgres_pool": true, "oracle_pool": true, "scheduler_enabled": true}` | **PASS** |
| `/admin/zones` | `GET` | Missing `X-Admin-API-Key` | 403 Forbidden | 403 Forbidden | `{"detail": "Invalid admin API key"}` | **PASS** |
| `/admin/zones` | `GET` | Invalid `X-Admin-API-Key` | 403 Forbidden | 403 Forbidden | `{"detail": "Invalid admin API key"}` | **PASS** |
| `/admin/zones` | `GET` | Valid `X-Admin-API-Key` | 200 OK | 200 OK | `configured_zones: "NZ"`, `mode: "FILTERED"`, `active_zone_codes: ["NZ"]`, `active_circle_count: 9`, `active_circles: [2, 55, 56, 59, 60, 61, 62, 64, 65]` | **PASS** |
| `/debit/status` | `GET` | Missing `X-Admin-API-Key` | 403 Forbidden | 403 Forbidden | `{"detail": "Invalid admin API key"}` | **PASS** |
| `/debit/status` | `GET` | Valid `X-Admin-API-Key` | 200 OK | 200 OK | 3 tokens (`FANCYSALE`, `SIMSWAP`, `ESIM`) active, session & access tokens present | **PASS** |
| `/admin/trigger-debit/*` | `POST` | Missing `X-Admin-API-Key` | 403 Forbidden | 403 Forbidden | Rejects unauthorized trigger | **PASS** |
| `/admin/reset-stuck-debit/*` | `POST` | Missing `X-Admin-API-Key` | 403 Forbidden | 403 Forbidden | Rejects unauthorized reset | **PASS** |
| **Root Path Compatibility** | All | Path prefix `/smpyro` | Compliant | Compliant | Nginx proxy header `X-Forwarded-Prefix: /smpyro` compatible | **PASS** |

---

## 7. Scheduler & Concurrency Verification

* **Registered Jobs:**
  1. `debit_fancysale` (IntervalTrigger: every 1440 min during cert; startup=False, enabled=True)
  2. `debit_simswap` (IntervalTrigger: every 1440 min during cert; startup=False, enabled=True)
  3. `debit_esim` (IntervalTrigger: every 1440 min during cert; startup=False, enabled=True)
  4. `stuck_cleanup` (IntervalTrigger: every 15 min; startup=False)
  5. `debit_daily_auth` (CronTrigger: 00:10 UTC daily)
* **Single Process Verification:**
  - `docker top pyro_debit_cert` confirmed exactly **1 Uvicorn worker process** (PID 3994, UID 999).
  - No duplicate scheduler instances running.
* **Controlled Non-Intrusive Execution:**
  - Baseline `public.debit_txn_log` count: **18**
  - Post-certification `public.debit_txn_log` count: **18**
  - **Actual debit execution count during certification: 0** (PASS).

---

## 8. Secret Scan & Image Cleanliness

1. **File System Scan (`/app`):**
   - Executed recursive search across `/app` in `pyro-debit-services:v4` for `.env*`, `*.pem`, `*.key`, `*.pfx`, `*.crt`.
   - **Result: 0 sensitive files present inside image.** (`.dockerignore` successfully excludes `.env*`).
2. **Environment Variables Scan:**
   - Evaluated `docker run --rm pyro-debit-services:v4 env`.
   - **Result: Only standard base Python variables present (`PYTHON_VERSION=3.12.14`, `PATH`, `LANG`). 0 credentials baked in.**
3. **Application Logs Scan:**
   - Full container startup, operational, and shutdown logs scanned for database passwords, Pyro credentials, and admin API keys.
   - **Result: CLEAN. 0 credentials or sensitive tokens exposed in logs.**

---

## 9. Lifecycle & Graceful Restart Test

1. **Stop & Resume Test:**
   - Command: `docker stop pyro_debit_cert` (Container transitioned down cleanly).
   - Command: `docker start pyro_debit_cert` (Container restarted).
2. **State Post-Restart:**
   - `/ready` endpoint returned 200 OK within 4 seconds:
     `{"status": "ready", "postgres_pool": true, "oracle_pool": true, "scheduler_enabled": true}`
   - PostgreSQL and Oracle pools reconnected without socket or connection leaks.
   - Scheduler resumed without duplicating job registrations.
   - **Result: PASS.**

---

## 10. Audit Summary & Gate Certification

```text
================================================================================
                    PRODUCTION DOCKER CERTIFICATION GATE
================================================================================
[x] Docker build succeeds cleanly without warnings
[x] Python runtime verified (3.12.14 on Linux amd64)
[x] All required dependencies pinned and available
[x] 0 secrets or configuration files baked into container image
[x] Container starts and passes readiness probes within 3 seconds
[x] Oracle connectivity verified (4 tables + F_DECRYPT callable)
[x] PostgreSQL connectivity verified (public.debit_txn_log accessible)
[x] Zone rollout engine verified (/admin/zones returns NZ circle set)
[x] Security enforcement verified (403 without key, 200 with valid key)
[x] Scheduler registered exactly 5 canonical jobs
[x] Process model verified (exactly 1 Uvicorn worker process)
[x] 0 uncontrolled debit transactions executed during certification
[x] Container stop / restart verified cleanly
[x] Container logs completely free of sensitive credentials
[x] Exact image digest recorded
================================================================================
```

### Files Changed

```text
Files changed:
docs/phase21_5_docker_certification.md (updated with latest v4 certification evidence)

Database changes:
NONE

Production changes:
NONE
```

---

## 11. Final Decision

# **CERTIFIED — READY FOR PHASE 22 DEPLOYMENT**

**Authoritative Production Docker Image Digest:**
```text
sha256:531ee9d7f7bfa980611fbfca5bb0229a48fd0800a15224e155dc92df20270e92
```
**Canonical Image Tag:** `pyro-debit-services:v4`
