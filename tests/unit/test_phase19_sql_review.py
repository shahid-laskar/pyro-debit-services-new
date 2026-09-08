"""Unit tests for Phase 19: Read-Only Final SQL Review.

Statically audits and validates all modified SQL statements against the 7 mandatory
checkpoints required by Section 24 of debit_services_final_implementation_plan.md:
1. Parameter binding
2. Status guards
3. Circle guards
4. Module guards
5. Identity predicates
6. Affected-row validation
7. Transaction boundaries
"""

import re
import pytest

from app.context import ExecutionContext, ExecutionSource
from app.debit.services.esim import (
    ESIM_CLAIM_SQL,
    ESIM_PRIMARY_SUCCESS_SQL,
    ESIM_FAILURE_SQL,
    ESIM_RECONCILIATION_SQL,
    build_esim_candidate_query,
    build_esim_claim_query,
    build_esim_cleanup_query,
    build_esim_sim_swap_data_update_query,
)
from app.debit.services.fancysale import (
    FANCYSALE_CLAIM_SQL,
    build_fancysale_candidate_query,
    build_fancysale_claim_query,
    build_fancysale_cleanup_query,
)
from app.debit.services.simswap import (
    SIMSWAP_CLAIM_SQL,
    SIMSWAP_PRIMARY_SUCCESS_SQL,
    SIMSWAP_FAILURE_SQL,
    SIMSWAP_RECONCILIATION_SQL,
    build_simswap_candidate_query,
    build_simswap_claim_query,
    build_simswap_cleanup_query,
    build_simswap_bcd_update_query,
)


class TestCheckpoint1ParameterBinding:
    """Checkpoint 1: Validate parameter binding across all SQL statements."""

    def test_all_queries_use_oracle_named_binds(self):
        ctx = ExecutionContext.create(source="SCHEDULED", zones_str="NZ")

        # Candidate queries
        _, fs_params = build_fancysale_candidate_query(50, ctx)
        assert "batch_size" in fs_params
        assert all(k.startswith("c_") for k in fs_params if k != "batch_size")

        _, ss_params = build_simswap_candidate_query(50, ctx)
        assert "batch_size" in ss_params
        assert all(k.startswith("c_") for k in ss_params if k != "batch_size")

        _, es_params = build_esim_candidate_query(50, ctx)
        assert "batch_size" in es_params
        assert all(k.startswith("c_") for k in es_params if k != "batch_size")

        # Cleanup queries
        _, fs_clean_params = build_fancysale_cleanup_query(10, ctx, active_refs={"REF1"})
        assert "stuck_minutes" in fs_clean_params
        assert any(k.startswith("c_") for k in fs_clean_params)
        assert any(k.startswith("act_") for k in fs_clean_params)

        # Secondary update queries
        _, bcd_params = build_simswap_bcd_update_query({
            "gsmnumber": "9412345678",
            "caf_serial_no": "CAF01",
            "circle_code": 2,
        })
        assert bcd_params == {
            "gsmnumber": "9412345678",
            "caf_serial_no": "CAF01",
            "circle_code": 2,
        }


class TestCheckpoint2StatusGuards:
    """Checkpoint 2: Validate state transitions preconditions in SQL WHERE clauses."""

    def test_candidate_discovery_status_guards(self):
        fs_sql, _ = build_fancysale_candidate_query(50)
        ss_sql, _ = build_simswap_candidate_query(50)
        es_sql, _ = build_esim_candidate_query(50)

        assert "CAF_ENTRY_DONE IN ('N', 'QM', 'QB')" in fs_sql
        assert "AMOUNT_DEDUCT_FLAG IN ('N', 'QM', 'QB')" in ss_sql
        assert "AMOUNT_DEDUCT_FLAG IN ('N', 'QM', 'QB')" in es_sql

    def test_claim_status_guards(self):
        assert "CAF_ENTRY_DONE IN ('N','QM','QB')" in FANCYSALE_CLAIM_SQL
        assert "AMOUNT_DEDUCT_FLAG IN ('N', 'QM', 'QB')" in SIMSWAP_CLAIM_SQL
        assert "AMOUNT_DEDUCT_FLAG IN ('N', 'QM', 'QB')" in ESIM_CLAIM_SQL

    def test_writeback_status_guards(self):
        assert "AMOUNT_DEDUCT_FLAG    = 'P'" in SIMSWAP_PRIMARY_SUCCESS_SQL
        assert "AMOUNT_DEDUCT_FLAG    = 'P'" in ESIM_PRIMARY_SUCCESS_SQL
        assert "AMOUNT_DEDUCT_FLAG    = 'P'" in SIMSWAP_FAILURE_SQL
        assert "AMOUNT_DEDUCT_FLAG    = 'P'" in ESIM_FAILURE_SQL

    def test_cleanup_status_guards_and_reconciliation_protection(self):
        fs_sql, _ = build_fancysale_cleanup_query(10)
        ss_sql, _ = build_simswap_cleanup_query(10)
        es_sql, _ = build_esim_cleanup_query(10)

        assert re.search(r"CAF_ENTRY_DONE\s*=\s*'P'", fs_sql)
        assert "PYRO_REMARKS NOT LIKE 'RECONCILIATION_REQUIRED%'" in fs_sql

        assert re.search(r"AMOUNT_DEDUCT_FLAG\s*=\s*'P'", ss_sql)
        assert "AMOUNT_DEDUCT_REMARKS NOT LIKE 'RECONCILIATION_REQUIRED%'" in ss_sql

        assert re.search(r"AMOUNT_DEDUCT_FLAG\s*=\s*'P'", es_sql)
        assert "AMOUNT_DEDUCT_REMARKS NOT LIKE 'RECONCILIATION_REQUIRED%'" in es_sql


class TestCheckpoint3CircleGuards:
    """Checkpoint 3: Validate circle guards in filtered mode and omission in ALL mode."""

    def test_filtered_mode_injects_circle_guards(self):
        ctx = ExecutionContext.create(source="SCHEDULED", zones_str="NZ")
        fs_sql, _ = build_fancysale_candidate_query(50, ctx)
        ss_sql, _ = build_simswap_candidate_query(50, ctx)
        es_sql, _ = build_esim_candidate_query(50, ctx)

        assert "CIRCLE_CODE IN (" in fs_sql
        assert "CIRCLE_CODE IN (" in ss_sql
        assert "CIRCLE_CODE IN (" in es_sql

    def test_all_mode_omits_circle_guards(self):
        ctx = ExecutionContext.create(source="SCHEDULED", zones_str="ALL")
        fs_sql, _ = build_fancysale_candidate_query(50, ctx)
        ss_sql, _ = build_simswap_candidate_query(50, ctx)
        es_sql, _ = build_esim_candidate_query(50, ctx)

        assert "CIRCLE_CODE IN" not in fs_sql
        assert "CIRCLE_CODE IN" not in ss_sql
        assert "CIRCLE_CODE IN" not in es_sql

    def test_claim_and_secondary_writeback_circle_guards(self):
        assert "CIRCLE_CODE = :circle_code" in FANCYSALE_CLAIM_SQL
        assert "CIRCLE_CODE = :circle_code" in SIMSWAP_CLAIM_SQL
        assert "CIRCLE_CODE = :circle_code" in ESIM_CLAIM_SQL

        bcd_sql, _ = build_simswap_bcd_update_query({"circle_code": 2, "gsmnumber": "9412345678"})
        assert "CIRCLE_CODE       = :circle_code" in bcd_sql

        sim_swap_sql, _ = build_esim_sim_swap_data_update_query({"circle_code": 60, "gsmnumber": "9412345678"})
        assert "CIRCLE_CODE       = :circle_code" in sim_swap_sql


class TestCheckpoint4ModuleGuards:
    """Checkpoint 4: Validate MODULE_TYPE isolation for shared table."""

    def test_simswap_module_guards_are_strict(self):
        cand_sql, _ = build_simswap_candidate_query(50)
        claim_sql = build_simswap_claim_query()
        clean_sql, _ = build_simswap_cleanup_query(10)

        for sql in (cand_sql, claim_sql, clean_sql, SIMSWAP_PRIMARY_SUCCESS_SQL, SIMSWAP_FAILURE_SQL):
            assert re.search(r"MODULE_TYPE\s*=\s*'SIMSWAP'", sql)
            assert not re.search(r"MODULE_TYPE\s*=\s*'ESIM'", sql)

    def test_esim_module_guards_are_strict(self):
        cand_sql, _ = build_esim_candidate_query(50)
        claim_sql = build_esim_claim_query()
        clean_sql, _ = build_esim_cleanup_query(10)

        for sql in (cand_sql, claim_sql, clean_sql, ESIM_PRIMARY_SUCCESS_SQL, ESIM_FAILURE_SQL):
            assert re.search(r"MODULE_TYPE\s*=\s*'ESIM'", sql)
            assert not re.search(r"MODULE_TYPE\s*=\s*'SIMSWAP'", sql)


class TestCheckpoint5IdentityPredicates:
    """Checkpoint 5: Validate targeting via confirmed stable identifiers."""

    def test_fancysale_uses_refid_identifier(self):
        assert "WHERE REFID = :refid" in FANCYSALE_CLAIM_SQL
        clean_sql, _ = build_fancysale_cleanup_query(10, active_refs={"REF_101"})
        assert "REFID NOT IN (" in clean_sql

    def test_simswap_and_esim_use_pk_id(self):
        assert "WHERE ID = :id" in SIMSWAP_CLAIM_SQL
        assert "WHERE  ID                    = :id" in SIMSWAP_PRIMARY_SUCCESS_SQL
        assert "WHERE ID = :id" in ESIM_CLAIM_SQL
        assert "WHERE  ID                    = :id" in ESIM_PRIMARY_SUCCESS_SQL

    def test_bcd_uses_composite_pk(self):
        bcd_sql, _ = build_simswap_bcd_update_query({
            "gsmnumber": "9412345678",
            "caf_serial_no": "CAF999",
            "circle_code": 2,
        })
        assert re.search(r"GSMNUMBER\s*=\s*:gsmnumber", bcd_sql)
        assert re.search(r"CAF_SERIAL_NO\s*=\s*:caf_serial_no", bcd_sql)


class TestCheckpoint6And7AffectedRowAndTransactionIntegrity:
    """Checkpoint 6 & 7: Validate affected row checks and transaction commit boundaries in code."""

    def test_code_checks_rowcount_and_commits(self):
        import inspect
        from app.debit.services.fancysale import FancySaleAdapter
        from app.debit.services.simswap import SimswapAdapter
        from app.debit.services.esim import EsimAdapter

        for adapter_cls in (FancySaleAdapter, SimswapAdapter, EsimAdapter):
            # Check fetch_and_claim inspects rowcount == 1
            src_claim = inspect.getsource(adapter_cls.fetch_and_claim)
            assert "cur.rowcount == 1" in src_claim
            assert "cur.rowcount > 1" in src_claim

            # Check mark_success raises WritebackError if rowcount == 0 and commits
            src_success = inspect.getsource(adapter_cls.mark_success)
            assert "cur.rowcount == 0" in src_success
            assert "raise WritebackError" in src_success
            assert "conn.commit()" in src_success

            # Check reset_stuck_processing commits
            src_clean = inspect.getsource(adapter_cls.reset_stuck_processing)
            assert "conn.commit()" in src_clean
