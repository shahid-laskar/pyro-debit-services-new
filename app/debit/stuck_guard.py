"""Stuck-cleanup guard: never reset a 'P' row whose debit may already have reached Pyro.

The stuck cleanup moves rows left in 'P' back to 'N' so they are picked up again.
The in-memory ownership tracker only protects rows that are in flight in *this*
process. After a crash or redeploy (or on another replica) a 'P' row can belong to
a debit that already succeeded, or whose outcome is unknown. Resetting such a row
causes a second debit.

Before each reset this guard:
  1. selects the rows the cleanup UPDATE is about to touch (same WHERE clause,
     derived from the adapter's own cleanup SQL so the two can never drift);
  2. classifies each one from debit_txn_log (app.db.debit_log);
  3. marks every row that is not provably un-debited as RECONCILIATION_REQUIRED
     and returns them so the caller excludes them from the reset.

Any failure reading debit_txn_log raises: the caller must then skip the reset
entirely (fail closed).
"""

import logging
import re
from typing import Any, Callable, Dict, Optional, Tuple

from app.db.debit_log import OUTCOME_NONE, fetch_debit_attempt_outcomes

logger = logging.getLogger(__name__)

_UPDATE_SET_RE = re.compile(
    r"^\s*UPDATE\s+(?P<table>\S+)\s+SET\s+.*?^WHERE\b", re.S | re.M | re.I
)

# The candidate SELECT uses a threshold one minute younger than the reset UPDATE,
# so every row the UPDATE can touch was already checked (the two statements run
# well under a minute apart).
_SELECT_MARGIN_MINUTES = 1


def derive_stuck_select(cleanup_sql: str, pk_column: str) -> str:
    """Turn 'UPDATE <table> SET ... WHERE <preds>' into 'SELECT <pk> FROM <table> WHERE <preds>'."""
    m = _UPDATE_SET_RE.match(cleanup_sql)
    if not m:
        raise ValueError("Unexpected cleanup SQL shape; cannot derive stuck-row SELECT")
    return f"SELECT {pk_column}\nFROM   {m.group('table')}\nWHERE" + cleanup_sql[m.end():]


def protect_stuck_rows(
    adapter: Any,
    cleanup_sql: str,
    cleanup_params: dict,
    pk_column: str,
    record_key: str,
    oracle_conn_factory: Callable,
) -> Dict[str, Tuple[Any, str, Optional[str]]]:
    """Return {ref: (raw_pk, outcome, pyro_txn_id)} for stuck rows that must NOT be reset.

    Raises if debit_txn_log cannot be read.
    """
    select_sql = derive_stuck_select(cleanup_sql, pk_column)
    select_params = dict(cleanup_params)
    select_params["stuck_minutes"] = max(
        float(select_params.get("stuck_minutes", 0)) - _SELECT_MARGIN_MINUTES, 0
    )

    with oracle_conn_factory() as conn:
        cur = conn.cursor()
        cur.execute(select_sql, select_params)
        rows = cur.fetchall() or []

    raw_by_ref = {str(r[0]): r[0] for r in rows if r and r[0] is not None}
    if not raw_by_ref:
        return {}

    outcomes = fetch_debit_attempt_outcomes(adapter.service_type, list(raw_by_ref))

    unsafe: Dict[str, Tuple[Any, str, Optional[str]]] = {}
    for ref, (outcome, pyro_txn_id) in outcomes.items():
        if outcome == OUTCOME_NONE or ref not in raw_by_ref:
            continue
        raw = raw_by_ref[ref]
        unsafe[ref] = (raw, outcome, pyro_txn_id)
        detail = f"stuck cleanup: earlier debit attempt outcome {outcome} - not reset"
        try:
            adapter.mark_reconciliation_required({record_key: raw}, pyro_txn_id, detail)
        except Exception as exc:
            # Still excluded from this reset, and still classified as unsafe next run.
            logger.critical(
                "[%s] stuck cleanup: could not mark ref=%s RECONCILIATION_REQUIRED: %s",
                adapter.service_type, ref, exc,
            )
        logger.critical(
            "[%s] stuck cleanup: ref=%s NOT reset — earlier debit attempt outcome %s (pyroId=%s); "
            "manual reconciliation required",
            adapter.service_type, ref, outcome, pyro_txn_id,
        )
    return unsafe
