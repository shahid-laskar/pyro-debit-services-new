"""Unit tests for app/config.py (Phase 2 - Configuration and Zone Rollout)."""

import pytest
from pydantic import ValidationError

from app.config import Settings, settings


# Minimal configuration dictionary to instantiate Settings in isolation
DUMMY_KWARGS = {
    "pyro_base_url": "http://127.0.0.1:9999",
    "oracle_user": "test_user",
    "oracle_password": "test_password",
    "oracle_dsn": "localhost:1521/xe",
    "pg_host": "localhost",
    "pg_database": "test_db",
    "pg_user": "test_user",
    "pg_password": "test_password",
}


class TestSettingsEnabledZones:
    """Validate enabled_zones configuration, normalization, and fail-closed validation."""

    def test_default_enabled_zones(self):
        """Assert that default enabled_zones is 'ALL'."""
        s = Settings(**DUMMY_KWARGS, _env_file=None)
        assert s.enabled_zones == "ALL"

    def test_global_settings_has_enabled_zones(self):
        """Assert that imported global settings has valid enabled_zones."""
        assert hasattr(settings, "enabled_zones")
        assert settings.enabled_zones in {"ALL", "NZ", "WZ", "EZ", "SZ"} or "," in settings.enabled_zones

    def test_prohibited_enabled_circles_attribute_not_present(self):
        """Invariant: do NOT add enabled_circles attribute to Settings."""
        assert not hasattr(settings, "enabled_circles")
        assert "enabled_circles" not in Settings.model_fields
        s = Settings(**DUMMY_KWARGS, _env_file=None)
        assert not hasattr(s, "enabled_circles")

    @pytest.mark.parametrize("zone_code", ["NZ", "WZ", "EZ", "SZ"])
    def test_valid_single_zones(self, zone_code):
        s = Settings(**DUMMY_KWARGS, enabled_zones=zone_code, _env_file=None)
        assert s.enabled_zones == zone_code

    @pytest.mark.parametrize(
        "input_zones, expected_normalized",
        [
            ("nz", "NZ"),
            ("wz", "WZ"),
            ("ez", "EZ"),
            ("sz", "SZ"),
            ("all", "ALL"),
            ("ALL", "ALL"),
            (" NZ ", "NZ"),
            ("nz, wz", "NZ,WZ"),
            (" NZ , WZ ", "NZ,WZ"),
            ("WZ,NZ", "WZ,NZ"),
            ("NZ,NZ", "NZ"),
            ("NZ,WZ,EZ,SZ", "NZ,WZ,EZ,SZ"),
            ("sz, ez, wz, nz", "SZ,EZ,WZ,NZ"),
        ],
    )
    def test_normalization_and_formatting(self, input_zones, expected_normalized):
        s = Settings(**DUMMY_KWARGS, enabled_zones=input_zones, _env_file=None)
        assert s.enabled_zones == expected_normalized

    @pytest.mark.parametrize(
        "invalid_zone_input",
        [
            "",
            "   ",
            "UNKNOWN",
            "NZZ",
            "ALL,NZ",
            "NZ,ALL",
            "NZ,,WZ",
            ",NZ",
            "NZ,",
            ",,",
            "ALL,WZ,EZ",
            "NZ,UNKNOWN",
        ],
    )
    def test_reject_invalid_enabled_zones(self, invalid_zone_input):
        """Assert that invalid zone configuration strictly raises ValidationError (fail-closed)."""
        with pytest.raises(ValidationError) as exc_info:
            Settings(**DUMMY_KWARGS, enabled_zones=invalid_zone_input, _env_file=None)
        assert "Invalid ENABLED_ZONES configuration" in str(exc_info.value)

    def test_reject_non_string_type(self):
        """Assert that non-string values are rejected."""
        with pytest.raises(ValidationError):
            Settings(**DUMMY_KWARGS, enabled_zones=123, _env_file=None)  # type: ignore

    def test_env_var_override_valid(self, monkeypatch):
        """Assert that ENABLED_ZONES environment variable correctly overrides default."""
        monkeypatch.setenv("ENABLED_ZONES", "NZ,WZ")
        s = Settings(**DUMMY_KWARGS, _env_file=None)
        assert s.enabled_zones == "NZ,WZ"

    def test_env_var_override_invalid_fails_closed(self, monkeypatch):
        """Assert that invalid ENABLED_ZONES in env var fails closed with ValidationError."""
        monkeypatch.setenv("ENABLED_ZONES", "INVALID_ZONE")
        with pytest.raises(ValidationError) as exc_info:
            Settings(**DUMMY_KWARGS, _env_file=None)
        assert "Invalid ENABLED_ZONES configuration" in str(exc_info.value)
