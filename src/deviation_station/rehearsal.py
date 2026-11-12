"""Deterministic plants for hardware-free Auto-Calibrate rehearsals."""

from __future__ import annotations

import math
from dataclasses import dataclass, field

import numpy as np

from .measurement import (
    DeviationReading,
    MeasurementEngine,
    MeasurementQuality,
    ReadingStatus,
)

FFT_SIZE = 1_024


@dataclass(slots=True)
class RehearsalIO:
    """Controller effects applied to a simulated plant instead of hardware."""

    plant: "BesselRehearsalPlant | TargetRehearsalPlant"
    key_count: int = 0
    unkey_count: int = 0

    def key_ptt(self) -> bool:
        self.key_count += 1
        self.plant.keyed = True
        return True

    def unkey_ptt(self) -> bool:
        self.unkey_count += 1
        self.plant.keyed = False
        return True

    def start_tone(self, level: float) -> None:
        self.plant.tone_on = True
        self.plant.set_level(level)

    def stop_tone(self) -> None:
        self.plant.tone_on = False

    def set_level(self, level: float) -> None:
        self.plant.set_level(level)


@dataclass(slots=True)
class BesselRehearsalPlant:
    """A synthetic single carrier null at a known audio level."""

    tone_frequency_hz: float = 1_000.0
    null_index: int = 1
    null_level: float = 0.40
    null_width: float = 0.035
    null_depth_db: float = 45.0
    sample_rate_hz: float = 48_000.0
    noise_db: float = -95.0
    baseline_db: float = -30.0
    carrier_offset_hz: float = -140.625
    keyed: bool = False
    tone_on: bool = False
    level: float = 0.0

    def __post_init__(self) -> None:
        if self.null_index not in (1, 2):
            raise ValueError("rehearsal null_index must be 1 or 2")
        if self.null_index == 2 and self.null_level == 0.40:
            self.null_level = 0.80

    def set_level(self, level: float) -> None:
        self.level = max(0.0, min(1.0, float(level)))

    def spectrum(self) -> tuple[float, ...]:
        values = np.full(FFT_SIZE, self.noise_db, dtype=np.float64)
        if not self.keyed:
            return tuple(float(value) for value in values)
        center = FFT_SIZE // 2
        bin_hz = self.sample_rate_hz / FFT_SIZE
        offset = int(round(self.carrier_offset_hz / bin_hz))
        index = center + offset
        carrier_db = self.baseline_db
        if self.tone_on:
            distance = (self.level - self.null_level) / self.null_width
            carrier_db -= self.null_depth_db * math.exp(-(distance * distance))
        values[index] = carrier_db
        return tuple(float(value) for value in values)


@dataclass(slots=True)
class TargetRehearsalPlant:
    """A synthetic discriminator reading with a monotonic level response."""

    slope_hz_per_level: float = 4_200.0
    offset_hz: float = 35.0
    keyed: bool = False
    tone_on: bool = False
    level: float = 0.0
    updates: int = field(default=0, init=False)

    def set_level(self, level: float) -> None:
        self.level = max(0.0, min(1.0, float(level)))

    def reading(self) -> DeviationReading:
        self.updates += 1
        value = max(
            0.0,
            self.offset_hz
            + self.slope_hz_per_level * self.level
            + 4.0 * math.sin(self.updates * 0.7),
        )
        return DeviationReading(
            deviation_hz=value,
            engine=MeasurementEngine.DISCRIMINATOR,
            profile_id="generic-fm",
            definition="robust instantaneous-frequency peak",
            quality=MeasurementQuality(
                status=ReadingStatus.GOOD,
                sample_count=4_096,
            ),
            metadata={"simulated": True, "level": self.level},
        )
