# Complete Project Overview: Debit Services

This document provides a comprehensive, file-by-file breakdown of your `debit-services` project. It will help you (and any new developers) understand the architecture, data flow, and exact responsibilities of every file.

---

## 1. Root Directory

Files in the root directory are primarily used for entry points, configuration, and deployment.

- **[main.py](file:///d:/PROJECT/sanchar-mitra-new/debit-services/main.py)**
  The entry point for your FastAPI application. It creates the `FastAPI()` instance, defines the `lifespan` context manager (which handles database pool initialization, service validation, and initial Pyro API authentication on startup), configures routing by including `debit_router`, and defines basic operational endpoints like `/health` and `/ready`.

- **[.env](file:///d:/PROJECT/sanchar-mitra-new/debit-services/.env) & [.env.example](file:///d:/PROJECT/sanchar-mitra-new/debit-services/.env.example)**
  Environment variables that control the application's configuration (like database credentials and API keys) without hardcoding sensitive data into the source code. `.env.example` serves as a template for developers to create their local `.env` file.

- **[Dockerfile](file:///d:/PROJECT/sanchar-mitra-new/debit-services/Dockerfile)**
  Contains instructions for building the Docker image for your application. It specifies the base Python image, copies necessary files, installs dependencies, and defines the startup command.

- **[docker-compose.yml](file:///d:/PROJECT/sanchar-mitra-new/debit-services/docker-compose.yml) & [docker-compose-prod.yml](file:///d:/PROJECT/sanchar-mitra-new/debit-services/docker-compose-prod.yml)**
  Defines the multi-container setup (if any) and runtime configurations (like ports, volumes, and environment variables) to easily run the application via Docker Compose for development and production environments respectively.

- **[requirements.txt](file:///d:/PROJECT/sanchar-mitra-new/debit-services/requirements.txt)**
  A list of Python dependencies (like `fastapi`, `uvicorn`, `oracledb`, `psycopg2`, etc.) required to run the project.

- **[encrypt_mpin.py](file:///d:/PROJECT/sanchar-mitra-new/debit-services/encrypt_mpin.py)**
  A standalone utility script, likely used as a helper for developers or administrators to manually encrypt or test MPIN encryption outside the main application flow.

- **[debit_readme.md](file:///d:/PROJECT/sanchar-mitra-new/debit-services/debit_readme.md)**
  Your existing high-level documentation explaining the architecture, data flow, setup instructions, and deployment steps for the service.

---

## 2. Core Application Logic (`app/`)

The `app` directory contains the main business logic, configuration, and shared utilities.

- **[app/config.py](file:///d:/PROJECT/sanchar-mitra-new/debit-services/app/config.py)**
  Uses Pydantic's `BaseSettings` to load, type-check, and validate all environment variables defined in `.env`. It makes these settings easily accessible throughout the code via the `settings` object.

- **[app/security.py](file:///d:/PROJECT/sanchar-mitra-new/debit-services/app/security.py)**
  Contains security dependencies, primarily the `require_admin_api_key` function. This function checks the `X-Admin-API-Key` HTTP header against the `ADMIN_API_KEY` defined in the settings, securing your admin endpoints.

- **[app/scheduler.py](file:///d:/PROJECT/sanchar-mitra-new/debit-services/app/scheduler.py)**
  Uses `APScheduler` to run background tasks asynchronously. It handles registering jobs for processing debit batches (based on configured intervals), cleaning up stuck records, and re-authenticating tokens daily.

- **[app/encryption.py](file:///d:/PROJECT/sanchar-mitra-new/debit-services/app/encryption.py)**
  Contains the core cryptographic functions (`encrypt` and `decrypt`) using `DES3` in ECB mode with PKCS5 padding. This is used for encrypting requests (like MPINs and payloads) sent to the Pyro API.

---

## 3. Authentication (`app/auth/`)

- **[app/auth/token_manager.py](file:///d:/PROJECT/sanchar-mitra-new/debit-services/app/auth/token_manager.py)**
  Defines the `PyroAuthService` class. This is a robust manager that handles the complex authentication flow with the Pyro API. It securely stores credentials, manages session and access tokens, automatically refreshes expired access tokens, and uses `asyncio.Lock` to prevent race conditions during token renewal.

---

## 4. Database Access (`app/db/`)

Handles all connections and queries to Oracle (source of truth for transactions) and PostgreSQL (audit logging).

- **[app/db/oracle.py](file:///d:/PROJECT/sanchar-mitra-new/debit-services/app/db/oracle.py)**
  Manages the connection pool for the Oracle database using `oracledb`. It exposes a `get_oracle_conn` context manager to safely acquire and release connections.

- **[app/db/postgres.py](file:///d:/PROJECT/sanchar-mitra-new/debit-services/app/db/postgres.py)**
  Manages the connection pool for the PostgreSQL database using `psycopg2`. Includes a retry decorator (`_pg_retry`) to handle transient connection drops gracefully.

- **[app/db/debit_log.py](file:///d:/PROJECT/sanchar-mitra-new/debit-services/app/db/debit_log.py)**
  Provides the `insert_debit_txn_log` and `async_insert_debit_txn_log` functions. These are responsible for writing comprehensive audit logs of every attempt to contact the Pyro API, ensuring sensitive data (like the `mpin`) is masked before logging.

---

## 5. Debit Processing Logic (`app/debit/`)

This directory is the core of the service, orchestrating the extraction of data from Oracle and submission to the Pyro API.

- **[app/debit/router.py](file:///d:/PROJECT/sanchar-mitra-new/debit-services/app/debit/router.py)**
  Defines the FastAPI HTTP endpoints for the debit system, such as `/debit/status` (to check token health), and the admin endpoints `/admin/trigger-debit/{service_type}` and `/admin/reset-stuck-debit/{service_type}`.

- **[app/debit/processor.py](file:///d:/PROJECT/sanchar-mitra-new/debit-services/app/debit/processor.py)**
  Contains the `run_debit_batch` orchestrator function. This function uses a provided adapter (like FancySale) to claim eligible records, map them to the correct format, call the Pyro API, handle the success or failure response, and mark the records accordingly in the database.

- **[app/debit/pyro_client.py](file:///d:/PROJECT/sanchar-mitra-new/debit-services/app/debit/pyro_client.py)**
  Contains the actual HTTP client logic for talking to the Pyro API (`wallet_adjustment`). It handles acquiring the valid access token, encrypting the payload, making the HTTP POST request, parsing the encrypted/JSON response, and triggering the audit logging.

- **[app/debit/token_managers.py](file:///d:/PROJECT/sanchar-mitra-new/debit-services/app/debit/token_managers.py)**
  Instantiates the specific `PyroAuthService` instances for each supported service (`fancysale_tm`, `simswap_tm`, `esim_tm`) based on the credentials loaded in `config.py`.

### 5.1 Service Adapters (`app/debit/services/`)
This submodule uses the **Adapter Pattern** to allow different services (FancySale, ESIM, etc.) to plug into the main debit processor seamlessly.

- **[app/debit/services/base.py](file:///d:/PROJECT/sanchar-mitra-new/debit-services/app/debit/services/base.py)**
  Defines the `DebitServiceAdapter` protocol. This acts as an interface that all specific services must implement, requiring methods like `fetch_and_claim`, `map_to_pyro_params`, `mark_success`, and `mark_failed`.

- **[app/debit/services/registry.py](file:///d:/PROJECT/sanchar-mitra-new/debit-services/app/debit/services/registry.py)**
  Maintains the `SERVICE_REGISTRY` dictionary. It instantiates the specific adapters (FancySale, SimSwap, ESIM) and provides helper functions to retrieve them by name.

- **[app/debit/services/fancysale.py](file:///d:/PROJECT/sanchar-mitra-new/debit-services/app/debit/services/fancysale.py)**
  The concrete implementation of the adapter for **FancySale**. It contains the specific Oracle SQL queries needed to fetch FancySale data, claim it, map it, and write back the success/failure states.

- **[app/debit/services/simswap.py](file:///d:/PROJECT/sanchar-mitra-new/debit-services/app/debit/services/simswap.py) & [app/debit/services/esim.py](file:///d:/PROJECT/sanchar-mitra-new/debit-services/app/debit/services/esim.py)**
  Currently stubbed implementations for future services. As noted in your `debit_readme.md`, you will need to implement the SQL queries and mapping logic in these files to activate these services.
