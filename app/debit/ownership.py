"""In-memory active ownership tracking for in-flight debit records.

Prevents cleanup jobs and concurrent workers from resetting or interfering with
records currently being processed by an active worker loop.
"""

from typing import Any, Iterable, Optional, Set, Tuple
import threading


class ActiveOwnershipTracker:
    """Thread-safe tracker for in-flight claimed record references."""

    def __init__(self):
        self._lock = threading.Lock()
        self._active: dict[str, Set[str]] = {
            "FANCYSALE": set(),
            "SIMSWAP": set(),
            "ESIM": set(),
        }

    def acquire(self, service_type: str, refs: Iterable[Any]) -> None:
        """Register record references as actively in-flight."""
        svc = service_type.upper()
        str_refs = {str(r) for r in refs if r is not None}
        with self._lock:
            self._active.setdefault(svc, set()).update(str_refs)

    def release(self, service_type: str, ref: Any) -> None:
        """Release a single record reference upon processing completion."""
        svc = service_type.upper()
        with self._lock:
            if svc in self._active:
                self._active[svc].discard(str(ref))

    def release_all(self, service_type: str, refs: Iterable[Any]) -> None:
        """Release a collection of record references."""
        svc = service_type.upper()
        str_refs = {str(r) for r in refs if r is not None}
        with self._lock:
            if svc in self._active:
                self._active[svc].difference_update(str_refs)

    def get_active(self, service_type: str) -> Set[str]:
        """Return a copy of all actively owned record references for a service."""
        svc = service_type.upper()
        with self._lock:
            return set(self._active.get(svc, set()))

    def is_active(self, service_type: str, ref: Any) -> bool:
        """Check if a specific reference is actively owned."""
        svc = service_type.upper()
        with self._lock:
            return str(ref) in self._active.get(svc, set())

    def clear(self, service_type: Optional[str] = None) -> None:
        """Clear active references (for testing or shutdown)."""
        with self._lock:
            if service_type:
                self._active[service_type.upper()] = set()
            else:
                for svc in self._active:
                    self._active[svc] = set()


def build_active_exclusion_predicate(
    column_name: str, active_refs: Iterable[Any], prefix: str = "act_"
) -> Tuple[str, dict]:
    """Build a SQL predicate and bind parameter dictionary to exclude active references.

    Example:
        build_active_exclusion_predicate("REFID", [101, 102])
        -> ("  AND REFID NOT IN (:act_0, :act_1)\n", {"act_0": 101, "act_1": 102})

    Handles chunking in groups of up to 1000 to strictly avoid Oracle ORA-01795 error.
    """
    if not active_refs:
        return "", {}

    # Sort items for deterministic query ordering
    sorted_items = sorted(str(r) for r in active_refs if r is not None)
    if not sorted_items:
        return "", {}

    # Try numeric conversion where possible for optimal Oracle index usage
    values = []
    for s in sorted_items:
        try:
            values.append(int(s))
        except (ValueError, TypeError):
            values.append(s)

    chunks = [values[i : i + 1000] for i in range(0, len(values), 1000)]
    clauses = []
    bind_params: dict = {}
    param_idx = 0

    for chunk in chunks:
        placeholders = []
        for val in chunk:
            p_name = f"{prefix}{param_idx}"
            placeholders.append(f":{p_name}")
            bind_params[p_name] = val
            param_idx += 1
        clauses.append(f"{column_name} NOT IN ({', '.join(placeholders)})")

    predicate = "  AND (" + " AND ".join(clauses) + ")\n"
    return predicate, bind_params


# Global singleton tracker instance
ownership_tracker = ActiveOwnershipTracker()
