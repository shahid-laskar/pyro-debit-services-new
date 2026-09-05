# Sanchar Mitra — Debit Service

This is a standalone backend service that processes wallet adjustments (debits) for Sanchar Mitra services via the Pyro API. It supports **FancySale**,  **SimSwap** and **ESIM**.

## Architecture & Data Flow

The service operates primarily via a background scheduler that executes the following loop:
1. **Fetch**: 
   - **FANCYSALE**  Reads eligible records from the Oracle database `CAF_ADMIN.VANITYSALE_FRANCH_DATA`  where `CAF_ENTRY_DONE IN ('N', 'QM', 'QB')`.
   - **SIMSWAP**  Reads eligible records from the Oracle database  `CAF_ADMIN.SIMSWAP_AMOUNT_DEDUCT_REQUESTS` where `CAF_ENTRY_DONE IN ('N', 'QM', 'QB')`.
   - **ESIM**  Reads eligible records from the Oracle database  `CAF_ADMIN.SIMSWAP_AMOUNT_DEDUCT_REQUESTS` where `CAF_ENTRY_DONE IN ('N', 'QM', 'QB')`.
2. **Claim**: Marks the rows as `P` (Processing) atomically to prevent duplicate processing.
3. **Submit**: Calls the Pyro API (`/erp-stock-api/service-wallet-adjustment`) with the decrypted MPIN and transaction details.
4. **Writeback**: Updates the Oracle database with the result (`Y` for Success, `R` for Rejected).
5. **Audit Log**: Records the full transaction lifecycle to a PostgreSQL database (`debit_txn_log`) for monitoring and auditing.

## Prerequisites

- **Python 3.10+** (if running locally)
- **Docker & Docker Compose** (if running via containers)
- Access to the **Oracle Database** (for CAF tables)
- Access to the **PostgreSQL Database** (for audit logs)
- Valid credentials for the **Pyro API**

## Configuration

Configuration is managed via environment variables. Copy `.env.example` to `.env` and fill in the values:

```bash
cp .env.example .env
```

### Key Environment Variables:
- `PYRO_BASE_URL`: Base URL for the Pyro API.
- `ORACLE_*`: Credentials for the Oracle DB containing the CAF tables.
- `PG_*`: Credentials for the PostgreSQL DB for transaction logging.
- `FANCYSALE_*`: Service-specific credentials (API Key, Login ID, Password, Secret Key) and scheduler settings (`FANCYSALE_INTERVAL_MINUTES`, `FANCYSALE_BATCH_SIZE`).
- `ADMIN_API_KEY`: A secret string required to access the `/admin/*` endpoints.

## Running the Service

### Using Docker (Recommended for Production)

```bash
# Build and run in detached mode
docker-compose up -d --build

# For production (uses pre-built images):
docker-compose -f docker-compose-prod.yml up -d
```
*Note: The application exposes port `8010` by default.*

### Running Locally (Development)

1. Create a virtual environment and install dependencies:
   ```bash
   python -m venv .venv
   source .venv/bin/activate  # On Windows: .venv\Scripts\activate
   pip install -r requirements.txt
   ```
2. Start the server:
   ```bash
   uvicorn main:app --host 127.0.0.1 --port 8010
   ```

## API Endpoints

The service provides several REST endpoints for monitoring and administration.

### Health & Readiness
- `GET /health`: Basic health check. Returns `{"status": "ok"}`.
- `GET /ready`: Checks database connection pools (Oracle and Postgres) and scheduler status.

### Debit Status & Administration
*Note: All `/admin/*` endpoints require the `x-api-key` header to match your `ADMIN_API_KEY` environment variable.*

- `GET /debit/status`: Returns the current authentication token status for all debit services and their configuration settings.
- `POST /admin/trigger-debit/{service_type}`: Manually triggers a debit batch for a specific service (e.g., `FANCYSALE`).
- `POST /admin/reset-stuck-debit/{service_type}?stuck_minutes=10`: Emergency endpoint to reset records stuck in the `P` (Processing) state back to `N` so they can be retried.

## Scheduler Jobs

When `ENABLE_SCHEDULER=true`, the following background jobs run automatically:
- **Debit Batch Processing**: Runs every `FANCYSALE_INTERVAL_MINUTES` to process new transactions.
- **Stuck Record Cleanup**: Runs every 15 minutes to automatically reset any rows stuck in `P` state beyond the configured threshold (`FANCYSALE_STUCK_MINUTES`).
- **Daily Re-auth**: Runs at 00:10 daily to refresh Pyro authentication tokens for all enabled debit services.


Viewed pyro_client.py:1-49
Listed directory debit
Listed directory services
Viewed esim.py:1-75

When you are ready to implement the actual business logic for the ESIM and SimSwap services, you will primarily need to update the following two adapter files:

1. **`app/debit/services/esim.py`**
2. **`app/debit/services/simswap.py`**

Currently, both of these files contain stub classes (`EsimAdapter` and presumably `SimSwapAdapter`) with `implemented = False` and several methods raising `NotImplementedError`.

### What you'll need to do in those files:

You will need to replace the `NotImplementedError` raises with your actual logic for interacting with the database and formatting the requests. Specifically, you will implement these methods in each adapter:

*   **`fetch_and_claim(self, batch_size: int) -> List[dict]`**: Logic to SELECT unprocessed records from the respective Oracle table and UPDATE them to a "processing" state so other workers don't grab them.
*   **`map_to_pyro_params(self, record: dict) -> dict`**: Logic to map a single database record into the exact JSON payload expected by the Pyro API.
*   **`get_record_ref(self, record: dict) -> str`**: Returns a unique identifier for the record (like a transaction ID or MSISDN) used for logging.
*   **`mark_success(self, record: dict, pyro_txn_id: str, remarks: str)`**: Logic to UPDATE the Oracle table row to indicate the debit was successful.
*   **`mark_failed(self, record: dict, remarks: str)`**: Logic to UPDATE the Oracle table row to indicate the debit failed.
*   **`reset_stuck_processing(self, stuck_minutes: int) -> int`**: Logic to find records stuck in the "processing" state for too long and revert them back to "pending".

### Additional Steps:
*   **Change `implemented = False` to `implemented = True`** inside both class definitions once the logic is written.
*   **Update your `.env` file** to set `ESIM_ENABLED=true` and `SIMSWAP_ENABLED=true` so the `main.py` scheduler and router start using them.
*   (Optional) If you decide to keep your SQL queries in a separate file, you might also create or update files in the `app/db/` directory, but the methods calling those queries will still live in the two adapter files mentioned above.


### Deployment

On Windows dev PC:
bash# Build for linux/amd64 (server architecture)
docker buildx build --platform linux/amd64 -t pyro_debit_service:v2 .
# use :latest/v2/etc
# Build manually with buildx

# Save and compress
docker save pyro_debit_service:v2 | gzip > pyro_debit_service_v2.tar.gz

# Copy to server (use your server's user and IP)

scp pyro_debit_service_v2.tar.gz m01400120u1@10.201.222.67:/home/m01400120u1/debit_services/

inside server: nano docker-compose.yml paste docker-compose-prod.yml

scp .env m01400120u1@10.201.222.67:/home/m01400120u1/debit_services/


On the server:
cd /opt/debit_services

# Load the image

gzip -d pyro_debit_service_v1.tar.gz
docker load -i pyro_debit_service_v1.tar

# Make sure your .env and docker-compose.yml are here
ls -la

# Start the service
docker compose up -d

# Verify
docker compose ps
docker compose logs -f pyro-debit-service