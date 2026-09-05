from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    # ── Pyro API (shared base URL — used by all debit token managers) ─────────
    pyro_base_url: str
    pyro_request_timeout_seconds: float = 30.0

    # ── Oracle DB (FancySale / SimSwap / ESIM CAF tables) ─────────────────────
    oracle_user: str
    oracle_password: str
    oracle_dsn: str              # host:port/service_name

    # ── Postgres DB (debit_txn_log, oracle_writeback_outbox) ──────────────────
    pg_host: str
    pg_port: int = 5432
    pg_database: str
    pg_user: str
    pg_password: str
    pg_min_conn: int = 2
    pg_max_conn: int = 10

    # ── Deployment ────────────────────────────────────────────────────────────
    root_path: str = "/smpyro"
    admin_api_key: str = ""

    # ── Scheduler ─────────────────────────────────────────────────────────────
    enable_scheduler: bool = True
    run_debit_on_startup: bool = False
    run_cleanup_on_startup: bool = True

    # ── FancySale debit credentials ───────────────────────────────────────────
    fancysale_api_key: str = ""
    fancysale_login_id: str = ""
    fancysale_password: str = ""
    fancysale_secret_key: str = ""
    fancysale_enabled: bool = True
    fancysale_batch_size: int = 200
    fancysale_interval_minutes: int = 30
    fancysale_stuck_minutes: int = 10
    # ── SimSwap debit credentials (feature off by default) ────────────────────
    simswap_api_key: str = ""
    simswap_login_id: str = ""
    simswap_password: str = ""
    simswap_secret_key: str = ""
    simswap_enabled: bool = False
    simswap_batch_size: int = 200
    simswap_interval_minutes: int = 30
    simswap_stuck_minutes: int = 10

    # ── ESIM debit credentials (feature off by default) ───────────────────────
    esim_api_key: str = ""
    esim_login_id: str = ""
    esim_password: str = ""
    esim_secret_key: str = ""
    esim_enabled: bool = False
    esim_batch_size: int = 200
    esim_interval_minutes: int = 30
    esim_stuck_minutes: int = 10
    
    validate_disabled_debit_credentials: bool = False


settings = Settings()