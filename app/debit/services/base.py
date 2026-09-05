from typing import List, Protocol, runtime_checkable

from app.auth.token_manager import PyroAuthService


@runtime_checkable
class DebitServiceAdapter(Protocol):
    
    service_type:  str              # "FANCYSALE" | "SIMSWAP" | "ESIM"
    enabled:       bool
    batch_size:    int
    token_manager: PyroAuthService

    # ── Data access ────────────────────────────────────────────────────────────

    def fetch_and_claim(self, batch_size: int) -> List[dict]:
       
        ...

    def map_to_pyro_params(self, record: dict) -> dict:
        
        ...

    def get_record_ref(self, record: dict) -> str:
        
        ...

    # ── State transitions ──────────────────────────────────────────────────────

    def mark_success(self, record: dict, pyro_txn_id: str, remarks: str) -> None:
       
        ...

    def mark_failed(self, record: dict, remarks: str) -> None:
       
        ...

    def reset_stuck_processing(self, stuck_minutes: int) -> int:
        
        ...