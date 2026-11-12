"""Shared live-measurement snapshot types."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Mapping

from .measurement import DeviationReading


@dataclass(frozen=True, slots=True)
class LiveSnapshot:
    """Latest measurement and spectrum produced by the live flowgraph."""

    timestamp_s: float
    profile_id: str
    reading: DeviationReading | None
    reporting_deviation_hz: float | None
    segment_count: int
    valid_segment_count: int
    summary: Mapping[str, Any] | None
    spectrum_db: tuple[float, ...]
    window_duration_s: float
    snr_db: float | None = None
