"""Bessel-null calculations for sinusoidal FM deviation measurement."""

from __future__ import annotations

import math
from collections.abc import Sequence
from dataclasses import dataclass

import numpy as np

from .measurement import (
    DeviationReading,
    MeasurementEngine,
    MeasurementQuality,
    ReadingStatus,
)

# Positive zeros of J0(x), in ascending order. These are standard
# mathematical constants; keeping them here avoids a SciPy runtime dependency.
BESSEL_J0_ZEROS: tuple[float, ...] = (
    2.404825557695773,
    5.520078110286311,
    8.653727912911013,
    11.791534439014281,
    14.930917708487787,
    18.071063967910924,
    21.21163662987926,
    24.352471530749302,
    27.493479132040254,
    30.634606468431977,
)


class BesselNullError(ValueError):
    """Raised when a Bessel-null calculation has invalid input."""


def _positive_finite(value: float, name: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise BesselNullError(f"{name} must be a real number")
    result = float(value)
    if not math.isfinite(result) or result <= 0.0:
        raise BesselNullError(f"{name} must be finite and greater than zero")
    return result


def carrier_null_index(null_index: int) -> float:
    """Return the modulation index for the one-based ``J0`` carrier null."""

    if isinstance(null_index, bool) or not isinstance(null_index, int):
        raise BesselNullError("null_index must be an integer")
    if null_index < 1:
        raise BesselNullError("null_index must be one or greater")

    try:
        return BESSEL_J0_ZEROS[null_index - 1]
    except IndexError as exc:
        raise BesselNullError(
            f"null_index {null_index} is outside the supported table "
            f"(1 through {len(BESSEL_J0_ZEROS)})"
        ) from exc


def deviation_from_carrier_null(
    tone_frequency_hz: float,
    null_index: int = 1,
) -> float:
    """Calculate peak deviation from a known carrier-null measurement.

    At a J0 null, ``beta = deviation / tone_frequency``. Therefore the peak
    deviation is the tabulated null index multiplied by the modulation tone
    frequency.
    """

    tone_hz = _positive_finite(tone_frequency_hz, "tone_frequency_hz")
    return carrier_null_index(null_index) * tone_hz


def carrier_null_tone_frequency(
    target_deviation_hz: float,
    null_index: int = 1,
) -> float:
    """Return the tone frequency that places a carrier null at a target."""

    target_hz = _positive_finite(target_deviation_hz, "target_deviation_hz")
    return target_hz / carrier_null_index(null_index)


@dataclass(frozen=True, slots=True)
class BesselProgress:
    """Live state of the Bessel carrier-null detector."""

    state: str
    carrier_locked: bool
    tone_frequency_hz: float
    null_index: int
    carrier_offset_hz: float | None = None
    carrier_db: float | None = None
    baseline_db: float | None = None
    null_depth_db: float | None = None
    nulls_seen: int = 0
    captured: DeviationReading | None = None


class BesselNullDetector:
    """Track carrier power and capture selected J0 carrier nulls."""

    def __init__(
        self,
        *,
        tone_frequency_hz: float = 1000.0,
        null_index: int = 1,
        sample_rate_hz: float = 48_000.0,
        carrier_search_hz: float = 800.0,
        min_null_depth_db: float = 15.0,
        recovery_db: float = 5.0,
        rearm_db: float = 10.0,
        smoothing_alpha: float = 0.35,
    ) -> None:
        self.tone_hz = _positive_finite(tone_frequency_hz, "tone_frequency_hz")
        self.null_index = null_index
        self.beta = carrier_null_index(null_index)
        self.sample_rate_hz = _positive_finite(sample_rate_hz, "sample_rate_hz")
        self.carrier_search_hz = _positive_finite(
            carrier_search_hz,
            "carrier_search_hz",
        )
        if not math.isfinite(min_null_depth_db) or min_null_depth_db <= 0.0:
            raise BesselNullError("min_null_depth_db must be positive")
        if not math.isfinite(recovery_db) or recovery_db <= 0.0:
            raise BesselNullError("recovery_db must be positive")
        if (
            not math.isfinite(rearm_db)
            or rearm_db <= 0.0
            or rearm_db >= min_null_depth_db
        ):
            raise BesselNullError(
                "rearm_db must be positive and below min_null_depth_db"
            )
        if not 0.0 < smoothing_alpha <= 1.0:
            raise BesselNullError("smoothing_alpha must be in (0, 1]")
        self.min_null_depth_db = float(min_null_depth_db)
        self.recovery_db = float(recovery_db)
        self.rearm_db = float(rearm_db)
        self.smoothing_alpha = float(smoothing_alpha)

        self._locked = False
        self._carrier_index: int | None = None
        self._carrier_offset_hz: float | None = None
        self._carrier_db: float | None = None
        self._baseline_db: float | None = None
        self._smoothed_db: float | None = None
        self._in_null = False
        self._null_min_db = math.inf
        self._null_dwell = 0
        self._rearm_pending = False
        self._nulls_seen = 0
        self._captured: DeviationReading | None = None
        self._updates = 0
        self._last_spectrum: np.ndarray | None = None

    @property
    def expected_deviation_hz(self) -> float:
        return self.beta * self.tone_hz

    def _progress(self, state: str) -> BesselProgress:
        depth = (
            self._baseline_db - self._smoothed_db
            if self._baseline_db is not None and self._smoothed_db is not None
            else None
        )
        return BesselProgress(
            state=state,
            carrier_locked=self._locked,
            tone_frequency_hz=self.tone_hz,
            null_index=self.null_index,
            carrier_offset_hz=self._carrier_offset_hz,
            carrier_db=self._smoothed_db,
            baseline_db=self._baseline_db,
            null_depth_db=depth,
            nulls_seen=self._nulls_seen,
            captured=self._captured,
        )

    def _validate_spectrum(self, spectrum_db: Sequence[float]) -> np.ndarray:
        spectrum = np.asarray(spectrum_db, dtype=np.float64)
        if spectrum.ndim != 1 or spectrum.size < 64:
            raise BesselNullError("spectrum must be a one-dimensional array of at least 64 bins")
        if not np.all(np.isfinite(spectrum)):
            raise BesselNullError("spectrum contains non-finite values")
        return spectrum

    def _carrier_power(self, spectrum: np.ndarray) -> float:
        assert self._carrier_index is not None
        lower = max(0, self._carrier_index - 1)
        upper = min(spectrum.size, self._carrier_index + 2)
        return float(np.max(spectrum[lower:upper]))

    def lock_carrier(self, spectrum_db: Sequence[float]) -> BesselProgress:
        """Lock to the strongest bin near zero after offset translation."""

        spectrum = self._validate_spectrum(spectrum_db)
        self._last_spectrum = spectrum
        bin_hz = self.sample_rate_hz / spectrum.size
        center = spectrum.size // 2
        half_bins = max(1, int(round(self.carrier_search_hz / bin_hz)))
        lower = max(0, center - half_bins)
        upper = min(spectrum.size, center + half_bins + 1)
        region = spectrum[lower:upper]
        peak = int(np.argmax(region)) + lower
        peak_power = float(spectrum[peak])
        noise_floor = float(np.median(spectrum))
        if peak_power < noise_floor + 6.0:
            self._locked = False
            return self._progress("WAITING FOR CARRIER")

        self._locked = True
        self._carrier_index = peak
        self._carrier_offset_hz = (peak - center) * bin_hz
        self._carrier_db = peak_power
        self._smoothed_db = peak_power
        self._baseline_db = peak_power
        self._in_null = False
        self._null_min_db = math.inf
        self._null_dwell = 0
        self._rearm_pending = False
        self._nulls_seen = 0
        self._captured = None
        self._updates = 0
        return self._progress("ARMED")

    def _make_reading(self, depth_db: float) -> DeviationReading:
        status = (
            ReadingStatus.GOOD
            if depth_db >= 25.0
            else ReadingStatus.MARGINAL
        )
        issues = () if status is ReadingStatus.GOOD else (
            "carrier null is shallow; verify modulation quality and SNR",
        )
        return DeviationReading(
            deviation_hz=self.expected_deviation_hz,
            engine=MeasurementEngine.BESSEL,
            profile_id=f"bessel-j0-{self.null_index}",
            definition=f"J0 carrier null {self.null_index}",
            quality=MeasurementQuality(
                status=status,
                carrier_locked=True,
                sample_count=self._updates,
            ),
            metadata={
                "tone_frequency_hz": self.tone_hz,
                "null_index": self.null_index,
                "null_depth_db": depth_db,
                "carrier_offset_hz": self._carrier_offset_hz,
                "baseline_db": self._baseline_db,
                "capture_count": self._nulls_seen,
            },
        )

    def update(self, spectrum_db: Sequence[float]) -> BesselProgress:
        """Process one spectrum update and return detector state."""

        spectrum = self._validate_spectrum(spectrum_db)
        self._last_spectrum = spectrum
        if not self._locked:
            return self.lock_carrier(spectrum)
        self._updates += 1
        power = self._carrier_power(spectrum)
        if self._smoothed_db is None:
            self._smoothed_db = power
        else:
            alpha = self.smoothing_alpha
            self._smoothed_db = alpha * power + (1.0 - alpha) * self._smoothed_db
        assert self._baseline_db is not None
        assert self._smoothed_db is not None

        if self._captured is not None:
            return self._progress("CAPTURED")

        if not self._in_null:
            self._baseline_db = max(self._baseline_db, self._smoothed_db)
            if self._rearm_pending:
                if self._baseline_db - power <= self.rearm_db:
                    self._rearm_pending = False
                    return self._progress("ARMED")
                return self._progress("RECOVERING")
            if self._smoothed_db < self._baseline_db - 4.0:
                self._in_null = True
                self._null_min_db = self._smoothed_db
                self._null_dwell = 1
                return self._progress("SEEKING NULL")
            return self._progress("ARMED")

        self._null_dwell += 1
        self._null_min_db = min(self._null_min_db, self._smoothed_db)
        if (
            self._smoothed_db >= self._null_min_db + self.recovery_db
            and self._null_dwell >= 2
        ):
            depth = self._baseline_db - self._null_min_db
            if depth >= self.min_null_depth_db:
                self._nulls_seen += 1
                if self._nulls_seen >= self.null_index:
                    self._captured = self._make_reading(depth)
                    self._in_null = False
                    return self._progress("CAPTURED")
            self._in_null = False
            self._null_dwell = 0
            self._null_min_db = math.inf
            self._rearm_pending = True
            if self._nulls_seen:
                return self._progress(f"PASSED NULL {self._nulls_seen}")
            return self._progress("ARMED")
        return self._progress("SEEKING NULL")

    def set_min_null_depth_db(self, min_null_depth_db: float) -> None:
        """Change the depth required before a null can be captured."""

        if not math.isfinite(min_null_depth_db) or min_null_depth_db <= self.rearm_db:
            raise BesselNullError(
                "min_null_depth_db must be finite and above rearm_db"
            )
        self.min_null_depth_db = float(min_null_depth_db)

    def capture_manual(self) -> BesselProgress:
        """Capture the current null even without observing recovery."""

        if not self._locked or self._baseline_db is None or self._smoothed_db is None:
            return self._progress("WAITING FOR CARRIER")
        depth = self._baseline_db - self._smoothed_db
        if depth < self.min_null_depth_db:
            return self._progress("NULL NOT DEEP ENOUGH")
        self._nulls_seen += 1
        self._captured = self._make_reading(depth)
        self._in_null = False
        return self._progress("CAPTURED")
