"""Execution Context for Zonewise Staged Migration (Phase 3).

Prevents individual modules from independently parsing or resolving raw zone strings.
Encapsulates runtime execution identity, trigger source, and resolved zone/circle scope
in an immutable context passed to all downstream processing components.
"""

from dataclasses import dataclass
from enum import Enum
from typing import List, Literal, Optional, Tuple
import uuid

from app.config import settings
from app.zones import (
    CIRCLE_METADATA,
    InvalidZoneError,
    ZoneSelection,
    resolve_zones,
)


class ExecutionSource(str, Enum):
    """Source of execution triggering."""
    SCHEDULED = "SCHEDULED"
    MANUAL_API = "MANUAL_API"


@dataclass(frozen=True)
class ExecutionContext:
    """Immutable execution context for debit service processing and cleanup.

    Attributes:
        execution_id: Unique string identifying this execution run.
        source: Trigger origin ('SCHEDULED' or 'MANUAL_API').
        zone_codes: Tuple of normalized zone codes (e.g. ('NZ',), ('NZ', 'WZ'), ('ALL',)).
        circle_codes: Tuple of integer circle codes, or None when mode == 'ALL'.
        mode: Filtering mode ('ALL' for nationwide or 'FILTERED' for zone-restricted).
        service_type: Optional debit service name ('fancysale', 'simswap', 'esim') for tracing.
    """

    execution_id: str
    source: Literal["SCHEDULED", "MANUAL_API"]
    zone_codes: Tuple[str, ...]
    circle_codes: Optional[Tuple[int, ...]]
    mode: Literal["ALL", "FILTERED"]
    service_type: Optional[str] = None

    @classmethod
    def create(
        cls,
        source: Literal["SCHEDULED", "MANUAL_API"] | ExecutionSource,
        zones_str: Optional[str] = None,
        service_type: Optional[str] = None,
        execution_id: Optional[str] = None,
    ) -> "ExecutionContext":
        """Construct an ExecutionContext, strictly resolving zone scope.

        Rules:
            - If zones_str is None:
              Inherits effective scope from settings.enabled_zones (Invariant E).
            - If zones_str is provided:
              Strictly resolves zones_str via resolve_zones().
            - If execution_id is not provided:
              Generates a new unique UUID4 string.

        Args:
            source: 'SCHEDULED' or 'MANUAL_API' (or ExecutionSource enum).
            zones_str: Optional raw zone string (e.g. 'NZ', 'NZ,WZ', 'ALL').
            service_type: Optional service type label (e.g. 'fancysale').
            execution_id: Optional explicit execution identifier.

        Returns:
            An immutable ExecutionContext instance.

        Raises:
            InvalidZoneError: If zones_str or configured fallback is invalid.
            ValueError: If source is not valid.
        """
        raw_source = source.value if isinstance(source, ExecutionSource) else str(source)
        if raw_source not in ("SCHEDULED", "MANUAL_API"):
            raise ValueError(f"Invalid execution source '{source}'. Must be 'SCHEDULED' or 'MANUAL_API'.")

        effective_source: Literal["SCHEDULED", "MANUAL_API"] = raw_source  # type: ignore

        # If zones_str is omitted, inherit configured zone scope (settings.enabled_zones)
        target_zones = zones_str if zones_str is not None else settings.enabled_zones
        selection = resolve_zones(target_zones)

        return cls(
            execution_id=execution_id or str(uuid.uuid4()),
            source=effective_source,
            zone_codes=selection.zone_codes,
            circle_codes=selection.circle_codes,
            mode=selection.mode,
            service_type=service_type,
        )

    @classmethod
    def from_selection(
        cls,
        selection: ZoneSelection,
        source: Literal["SCHEDULED", "MANUAL_API"] | ExecutionSource,
        service_type: Optional[str] = None,
        execution_id: Optional[str] = None,
    ) -> "ExecutionContext":
        """Construct an ExecutionContext directly from an existing ZoneSelection."""
        raw_source = source.value if isinstance(source, ExecutionSource) else str(source)
        if raw_source not in ("SCHEDULED", "MANUAL_API"):
            raise ValueError(f"Invalid execution source '{source}'. Must be 'SCHEDULED' or 'MANUAL_API'.")

        effective_source: Literal["SCHEDULED", "MANUAL_API"] = raw_source  # type: ignore

        return cls(
            execution_id=execution_id or str(uuid.uuid4()),
            source=effective_source,
            zone_codes=selection.zone_codes,
            circle_codes=selection.circle_codes,
            mode=selection.mode,
            service_type=service_type,
        )

    def is_circle_allowed(self, circle_code: int | str) -> bool:
        """Check if a circle code is permitted under this execution context."""
        if self.mode == "ALL" or self.circle_codes is None:
            return True
        try:
            return int(circle_code) in self.circle_codes
        except (ValueError, TypeError):
            return False

    @property
    def circle_list(self) -> Optional[List[int]]:
        """Return circle codes as a list for SQL parameter binding, or None if ALL mode."""
        return list(self.circle_codes) if self.circle_codes is not None else None

    @property
    def zones_display(self) -> str:
        """Comma-delimited string of active zone codes."""
        return ",".join(self.zone_codes)

    @property
    def circle_count(self) -> int:
        """Return count of active circles in this context (nationwide total if ALL mode)."""
        return len(self.circle_codes) if self.circle_codes is not None else len(CIRCLE_METADATA)

    def to_dict(self) -> dict:
        """Serialize context for logging, telemetry, and API responses."""
        return {
            "execution_id": self.execution_id,
            "source": self.source,
            "service_type": self.service_type,
            "mode": self.mode,
            "zone_codes": list(self.zone_codes),
            "circle_codes": list(self.circle_codes) if self.circle_codes is not None else None,
            "circle_count": len(self.circle_codes) if self.circle_codes is not None else len(CIRCLE_METADATA),
        }
