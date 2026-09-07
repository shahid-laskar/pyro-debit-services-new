"""Unit tests for app/context.py (Phase 3 - Execution Context)."""

import uuid
import pytest
from dataclasses import FrozenInstanceError

from app.config import settings
from app.context import ExecutionContext, ExecutionSource
from app.zones import EZ, NZ, SZ, WZ, InvalidZoneError, resolve_zones


class TestExecutionContextCreation:
    """Validate construction of ExecutionContext instances."""

    def test_create_scheduled_default(self, monkeypatch):
        monkeypatch.setattr(settings, "enabled_zones", "ALL")
        ctx = ExecutionContext.create(source="SCHEDULED")
        assert ctx.source == "SCHEDULED"
        assert ctx.mode == "ALL"
        assert ctx.zone_codes == ("ALL",)
        assert ctx.circle_codes is None
        assert ctx.service_type is None
        # Verify valid UUID was generated
        parsed_uuid = uuid.UUID(ctx.execution_id)
        assert str(parsed_uuid) == ctx.execution_id

    def test_create_manual_api_explicit_zone(self):
        ctx = ExecutionContext.create(
            source="MANUAL_API",
            zones_str="NZ",
            service_type="fancysale",
            execution_id="exec-12345",
        )
        assert ctx.source == "MANUAL_API"
        assert ctx.execution_id == "exec-12345"
        assert ctx.service_type == "fancysale"
        assert ctx.mode == "FILTERED"
        assert ctx.zone_codes == ("NZ",)
        assert ctx.circle_codes == tuple(sorted(NZ))

    def test_create_with_enum_source(self):
        ctx1 = ExecutionContext.create(source=ExecutionSource.SCHEDULED)
        assert ctx1.source == "SCHEDULED"
        ctx2 = ExecutionContext.create(source=ExecutionSource.MANUAL_API)
        assert ctx2.source == "MANUAL_API"

    def test_create_multi_zone(self):
        ctx = ExecutionContext.create(source="SCHEDULED", zones_str="NZ,WZ")
        assert ctx.mode == "FILTERED"
        assert ctx.zone_codes == ("NZ", "WZ")
        expected_circles = tuple(sorted(set(NZ + WZ)))
        assert ctx.circle_codes == expected_circles

    def test_create_all_mode_explicit(self):
        ctx = ExecutionContext.create(source="MANUAL_API", zones_str="ALL")
        assert ctx.mode == "ALL"
        assert ctx.zone_codes == ("ALL",)
        assert ctx.circle_codes is None


class TestExecutionContextInheritance:
    """Validate Invariant E: Manual triggers and scheduled runs inherit configured zone scope."""

    def test_manual_api_inherits_configured_zone_when_omitted(self, monkeypatch):
        monkeypatch.setattr(settings, "enabled_zones", "WZ")
        ctx = ExecutionContext.create(source="MANUAL_API", zones_str=None)
        assert ctx.mode == "FILTERED"
        assert ctx.zone_codes == ("WZ",)
        assert ctx.circle_codes == tuple(sorted(WZ))

    def test_scheduled_inherits_configured_zone(self, monkeypatch):
        monkeypatch.setattr(settings, "enabled_zones", "EZ")
        ctx = ExecutionContext.create(source="SCHEDULED", zones_str=None)
        assert ctx.mode == "FILTERED"
        assert ctx.zone_codes == ("EZ",)
        assert ctx.circle_codes == tuple(sorted(EZ))

    def test_manual_explicit_overrides_configured_scope(self, monkeypatch):
        monkeypatch.setattr(settings, "enabled_zones", "WZ")
        ctx = ExecutionContext.create(source="MANUAL_API", zones_str="SZ")
        assert ctx.mode == "FILTERED"
        assert ctx.zone_codes == ("SZ",)
        assert ctx.circle_codes == tuple(sorted(SZ))


class TestExecutionContextRejections:
    """Validate fail-closed error handling during context construction."""

    def test_reject_invalid_source(self):
        with pytest.raises(ValueError, match="Invalid execution source"):
            ExecutionContext.create(source="CRON_TRIGGER")  # type: ignore

    def test_reject_invalid_zones_str(self):
        with pytest.raises(InvalidZoneError):
            ExecutionContext.create(source="MANUAL_API", zones_str="INVALID_ZONE")

    def test_reject_empty_string_zones(self):
        with pytest.raises(InvalidZoneError):
            ExecutionContext.create(source="MANUAL_API", zones_str="")

    def test_reject_contradictory_all_and_specific_zone(self):
        with pytest.raises(InvalidZoneError, match="Contradictory zone selection"):
            ExecutionContext.create(source="MANUAL_API", zones_str="ALL,NZ")

    def test_reject_malformed_commas(self):
        with pytest.raises(InvalidZoneError, match="Malformed zone selection"):
            ExecutionContext.create(source="MANUAL_API", zones_str="NZ,,WZ")


class TestExecutionContextBehavior:
    """Validate helper methods, properties, serialization, and immutability."""

    def test_is_circle_allowed_filtered(self):
        ctx = ExecutionContext.create(source="SCHEDULED", zones_str="NZ")
        assert ctx.is_circle_allowed(2) is True
        assert ctx.is_circle_allowed("2") is True
        assert ctx.is_circle_allowed(55) is True
        # WZ circle
        assert ctx.is_circle_allowed(1) is False
        assert ctx.is_circle_allowed("1") is False
        # Non-numeric
        assert ctx.is_circle_allowed("invalid") is False

    def test_is_circle_allowed_all(self):
        ctx = ExecutionContext.create(source="SCHEDULED", zones_str="ALL")
        assert ctx.is_circle_allowed(2) is True
        assert ctx.is_circle_allowed(1) is True
        assert ctx.is_circle_allowed(70) is True
        assert ctx.is_circle_allowed(9999) is True

    def test_circle_list_property(self):
        ctx_filtered = ExecutionContext.create(source="MANUAL_API", zones_str="WZ")
        assert isinstance(ctx_filtered.circle_list, list)
        assert ctx_filtered.circle_list == sorted(WZ)

        ctx_all = ExecutionContext.create(source="SCHEDULED", zones_str="ALL")
        assert ctx_all.circle_list is None

    def test_zones_display_property(self):
        ctx_multi = ExecutionContext.create(source="SCHEDULED", zones_str="NZ,SZ")
        assert ctx_multi.zones_display == "NZ,SZ"

        ctx_all = ExecutionContext.create(source="SCHEDULED", zones_str="ALL")
        assert ctx_all.zones_display == "ALL"

    def test_to_dict_serialization(self):
        ctx = ExecutionContext.create(
            source="MANUAL_API",
            zones_str="WZ",
            service_type="simswap",
            execution_id="fixed-id-1",
        )
        d = ctx.to_dict()
        assert d == {
            "execution_id": "fixed-id-1",
            "source": "MANUAL_API",
            "service_type": "simswap",
            "mode": "FILTERED",
            "zone_codes": ["WZ"],
            "circle_codes": [1, 3, 4, 10, 12],
            "circle_count": 5,
        }

    def test_immutability(self):
        ctx = ExecutionContext.create(source="SCHEDULED", zones_str="NZ")
        with pytest.raises(FrozenInstanceError):
            ctx.mode = "ALL"  # type: ignore
        with pytest.raises(FrozenInstanceError):
            ctx.execution_id = "tampered"  # type: ignore
        with pytest.raises(FrozenInstanceError):
            ctx.source = "MANUAL_API"  # type: ignore

    def test_from_selection_factory(self):
        selection = resolve_zones("SZ")
        ctx = ExecutionContext.from_selection(
            selection=selection,
            source="SCHEDULED",
            service_type="esim",
            execution_id="from-sel-1",
        )
        assert ctx.execution_id == "from-sel-1"
        assert ctx.source == "SCHEDULED"
        assert ctx.service_type == "esim"
        assert ctx.mode == "FILTERED"
        assert ctx.zone_codes == ("SZ",)
        assert ctx.circle_codes == tuple(sorted(SZ))

    def test_from_selection_invalid_source_rejected(self):
        selection = resolve_zones("SZ")
        with pytest.raises(ValueError, match="Invalid execution source"):
            ExecutionContext.from_selection(selection=selection, source="INVALID")  # type: ignore
