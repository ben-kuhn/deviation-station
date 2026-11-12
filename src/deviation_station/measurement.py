"""Common result and quality types shared by both measurement engines."""

from __future__ import annotations

import math
import time
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any, Mapping


class MeasurementEngine(StrEnum):
    """Available deviation-estimation strategies."""

    BESSEL = "bessel"
    DISCRIMINATOR = "discriminator"


class ReadingStatus(StrEnum):
    """Overall trustworthiness of a measurement result."""

    GOOD = "good"
    MARGINAL = "marginal"
    UNKNOWN = "unknown"
    INVALID = "invalid"


@dataclass(frozen=True, slots=True)
class MeasurementQuality:
    """Quality information attached to every reading."""

    status: ReadingStatus = ReadingStatus.UNKNOWN
    snr_db: float | None = None
    carrier_locked: bool | None = None
    overload: bool | None = None
    clipped_fraction: float | None = None
    sample_count: int = 0
    issues: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        if self.snr_db is not None and not math.isfinite(self.snr_db):
            raise ValueError("snr_db must be finite when provided")
        if self.clipped_fraction is not None and not 0.0 <= self.clipped_fraction <= 1.0:
            raise ValueError("clipped_fraction must be between 0 and 1 when provided")
        if self.sample_count < 0:
            raise ValueError("sample_count cannot be negative")
        if self.overload and self.status is ReadingStatus.GOOD:
            raise ValueError("an overloaded measurement cannot have good status")


@dataclass(frozen=True, slots=True)
class DeviationReading:
    """A single result from either measurement engine.

    The common representation prevents the UI and calibration controller from
    accidentally mixing engine-specific semantics without knowing which
    definition produced a value.
    """

    deviation_hz: float | None
    engine: MeasurementEngine
    profile_id: str
    definition: str
    quality: MeasurementQuality = field(default_factory=MeasurementQuality)
    timestamp_s: float = field(default_factory=time.time)
    metadata: Mapping[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if self.deviation_hz is not None:
            if not math.isfinite(self.deviation_hz):
                raise ValueError("deviation_hz must be finite when provided")
            if self.deviation_hz < 0.0:
                raise ValueError("deviation_hz cannot be negative")
        if not self.profile_id:
            raise ValueError("profile_id cannot be empty")
        if not self.definition:
            raise ValueError("definition cannot be empty")
        if not math.isfinite(self.timestamp_s):
            raise ValueError("timestamp_s must be finite")

    @property
    def valid(self) -> bool:
        """Whether this reading may be presented as a measurement."""

        return self.deviation_hz is not None and self.quality.status in {
            ReadingStatus.GOOD,
            ReadingStatus.MARGINAL,
        }

    def as_dict(self) -> dict[str, Any]:
        """Return a JSON-serializable representation for reports and tests."""

        return {
            "deviation_hz": self.deviation_hz,
            "engine": self.engine.value,
            "profile_id": self.profile_id,
            "definition": self.definition,
            "valid": self.valid,
            "timestamp_s": self.timestamp_s,
            "quality": {
                "status": self.quality.status.value,
                "snr_db": self.quality.snr_db,
                "carrier_locked": self.quality.carrier_locked,
                "overload": self.quality.overload,
                "clipped_fraction": self.quality.clipped_fraction,
                "sample_count": self.quality.sample_count,
                "issues": list(self.quality.issues),
            },
            "metadata": dict(self.metadata),
        }
