"""Equipment-specific measurement profiles."""

from __future__ import annotations

import math
from dataclasses import dataclass

from numpy.typing import ArrayLike

from .discriminator import four_fsk_symbol_deviation, robust_peak_deviation
from .measurement import (
    DeviationReading,
    MeasurementEngine,
    MeasurementQuality,
    ReadingStatus,
)


@dataclass(frozen=True, slots=True)
class Fsk4Profile:
    """A four-level FSK profile with equally spaced symbol frequencies."""

    profile_id: str
    label: str
    symbol_rate_hz: float
    expected_outer_deviation_hz: float
    expected_inner_deviation_hz: float
    maximum_deviation_hz: float = 15_000.0
    minimum_timing_score: float = 1.08

    def measure(
        self,
        frequency_hz: ArrayLike,
        sample_rate_hz: float,
    ) -> DeviationReading:
        estimate = four_fsk_symbol_deviation(
            frequency_hz,
            sample_rate_hz=sample_rate_hz,
            symbol_rate_hz=self.symbol_rate_hz,
        )
        relative_error = estimate.rms_error_hz / estimate.deviation_hz
        issues: list[str] = []
        invalid = False
        if not estimate.converged:
            invalid = True
            issues.append("symbol-level clustering did not converge")
        if estimate.timing_score is None or estimate.timing_score < self.minimum_timing_score:
            invalid = True
            issues.append("symbol timing confidence is too low")
        if estimate.deviation_hz > self.maximum_deviation_hz:
            invalid = True
            issues.append("estimated deviation exceeds the profile's plausible range")
        if relative_error > 0.16:
            invalid = True
            issues.append("symbol frequency clusters are too broad for a reliable reading")

        if invalid:
            status = ReadingStatus.INVALID
        elif relative_error <= 0.10:
            status = ReadingStatus.GOOD
        else:
            status = ReadingStatus.MARGINAL

        quality = MeasurementQuality(
            status=status,
            sample_count=estimate.sample_count,
            issues=tuple(issues),
        )
        return DeviationReading(
            deviation_hz=estimate.deviation_hz if status is not ReadingStatus.INVALID else None,
            engine=MeasurementEngine.DISCRIMINATOR,
            profile_id=self.profile_id,
            definition=estimate.definition,
            quality=quality,
            metadata={
                "profile_label": self.label,
                "symbol_rate_hz": self.symbol_rate_hz,
                "symbol_levels_hz": estimate.levels_hz,
                "symbol_populations": estimate.populations,
                "level_spacing_hz": estimate.level_spacing_hz,
                "rms_error_hz": estimate.rms_error_hz,
                "capture_symbol_count": estimate.capture_symbol_count,
                "timing_offset_samples": estimate.timing_offset_samples,
                "timing_score": estimate.timing_score,
                "clustering_converged": estimate.converged,
                "expected_outer_deviation_hz": self.expected_outer_deviation_hz,
                "expected_inner_deviation_hz": self.expected_inner_deviation_hz,
            },
        )


@dataclass(frozen=True, slots=True)
class GenericFmProfile:
    """A generic robust-peak profile for analog FM or unknown waveforms."""

    profile_id: str = "generic-fm"
    label: str = "Generic FM"
    low_percentile: float = 0.5
    high_percentile: float = 99.5
    smoothing_bandwidth_hz: float | None = 6000.0
    maximum_carrier_flatness: float = 0.30

    def measure(
        self,
        frequency_hz: ArrayLike,
        sample_rate_hz: float | None = None,
        *,
        carrier_flatness: float | None = None,
    ) -> DeviationReading:
        smoothing_enabled = (
            sample_rate_hz is not None and self.smoothing_bandwidth_hz is not None
        )
        estimate = robust_peak_deviation(
            frequency_hz,
            low_percentile=self.low_percentile,
            high_percentile=self.high_percentile,
            sample_rate_hz=sample_rate_hz if smoothing_enabled else None,
            smoothing_bandwidth_hz=(
                self.smoothing_bandwidth_hz if smoothing_enabled else None
            ),
        )
        issues: list[str] = []
        if sample_rate_hz is None:
            status = ReadingStatus.UNKNOWN
            issues.append("sample rate is required for filtering and quality assessment")
        else:
            raw = estimate.raw_deviation_hz or 0.0
            smoothed = estimate.deviation_hz
            largest = max(raw, smoothed)
            smallest = min(raw, smoothed)
            if largest <= 1e-9:
                disagreement = 1.0
            elif smallest <= 1e-9:
                disagreement = float("inf")
            else:
                disagreement = largest / smallest
            if disagreement > 1.75:
                status = ReadingStatus.INVALID
                issues.append("raw and smoothed peak estimates disagree excessively")
            elif disagreement > 1.10 or estimate.smoothing_window_samples == 0:
                status = ReadingStatus.MARGINAL
                issues.append("noise or filtering uncertainty remains in the peak estimate")
            else:
                status = ReadingStatus.GOOD
            if (
                carrier_flatness is not None
                and math.isfinite(carrier_flatness)
                and carrier_flatness > self.maximum_carrier_flatness
            ):
                status = ReadingStatus.INVALID
                issues.append(
                    "receiver noise floor dominates; no FM carrier detected"
                )

        return DeviationReading(
            deviation_hz=estimate.deviation_hz if status is not ReadingStatus.INVALID else None,
            engine=MeasurementEngine.DISCRIMINATOR,
            profile_id=self.profile_id,
            definition=estimate.definition,
            quality=MeasurementQuality(
                status=status,
                sample_count=estimate.sample_count,
                issues=tuple(issues),
            ),
            metadata={
                "profile_label": self.label,
                "center_hz": estimate.center_hz,
                "low_hz": estimate.low_hz,
                "high_hz": estimate.high_hz,
                "low_percentile": self.low_percentile,
                "high_percentile": self.high_percentile,
                "raw_deviation_hz": estimate.raw_deviation_hz,
                "smoothing_window_samples": estimate.smoothing_window_samples,
                "smoothing_bandwidth_hz": (
                    self.smoothing_bandwidth_hz if sample_rate_hz is not None else None
                ),
                "carrier_flatness": carrier_flatness,
            },
        )


# ETSI DMR uses 4FSK at 4800 symbols/s with nominal symbol frequencies at
# +/-1944 Hz and +/-648 Hz. Peak (outer-symbol) deviation is 1944 Hz.
MMDVM_DMR_PROFILE = Fsk4Profile(
    profile_id="mmdvm-dmr",
    label="MMDVM / DMR 4FSK",
    symbol_rate_hz=4800.0,
    expected_outer_deviation_hz=1944.0,
    expected_inner_deviation_hz=648.0,
)

# M17 uses 4FSK at 4800 symbols/s with nominal symbol frequencies at
# +/-2400 Hz and +/-800 Hz. Peak (outer-symbol) deviation is 2400 Hz.
MODULE17_M17_PROFILE = Fsk4Profile(
    profile_id="module17-m17",
    label="Module17 / M17 4FSK",
    symbol_rate_hz=4800.0,
    expected_outer_deviation_hz=2400.0,
    expected_inner_deviation_hz=800.0,
)

GENERIC_FM_PROFILE = GenericFmProfile()

DIGITAL_PROFILES: tuple[Fsk4Profile, ...] = (
    MMDVM_DMR_PROFILE,
    MODULE17_M17_PROFILE,
)
