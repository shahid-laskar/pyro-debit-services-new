"""Shared test defaults for the debit idempotency guards.

Unit tests run without a Postgres pool. By default every reference has no prior
debit attempt (OUTCOME_NONE) and recording a debit intent succeeds. Tests that
exercise the guards themselves override these patches.
"""

from unittest.mock import AsyncMock, patch

import pytest


@pytest.fixture(autouse=True)
def _default_debit_history():
    with patch(
        "app.debit.processor.async_fetch_debit_attempt_outcomes",
        new_callable=AsyncMock,
        return_value={},
    ) as fetch_outcomes, patch(
        "app.debit.processor.async_insert_debit_intent",
        new_callable=AsyncMock,
    ) as insert_intent, patch(
        "app.debit.stuck_guard.fetch_debit_attempt_outcomes",
        return_value={},
    ) as guard_outcomes:
        yield {
            "fetch_outcomes": fetch_outcomes,
            "insert_intent": insert_intent,
            "guard_outcomes": guard_outcomes,
        }
