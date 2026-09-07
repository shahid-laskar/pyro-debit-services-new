from typing import Any, List, Optional, Protocol, Set, runtime_checkable

from app.auth.token_manager import PyroAuthService
from app.context import ExecutionContext


class WritebackError(RuntimeError):
    """Raised when primary status writeback to Oracle fails after a successful financial debit."""


@runtime_checkable
class DebitServiceAdapter(Protocol):
    
    service_type:  str              # "FANCYSALE" | "SIMSWAP" | "ESIM"
    enabled:       bool
    batch_size:    int
    token_manager: PyroAuthService

    # ── Data access ────────────────────────────────────────────────────────────

    def fetch_and_claim(
        self, batch_size: int, context: Optional[ExecutionContext] = None
    ) -> List[dict]:
       
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

    def mark_reconciliation_required(
        self, record: dict, pyro_txn_id: str, error_detail: str
    ) -> None:
        
        ...

    def reset_stuck_processing(
        self,
        stuck_minutes: int,
        context: Optional[ExecutionContext] = None,
        active_refs: Optional[Set[Any]] = None,
    ) -> int:
        
        ...