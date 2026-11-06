"""Detect the dominant modulation tone from demodulated FM audio."""

from __future__ import annotations

import math
from dataclasses import dataclass

import numpy as np


class ToneError(ValueError):
    """Raised when a tone cannot be estimated from the available samples."""


@dataclass(frozen=True, slots=True)
class ToneEstimate:
    frequency_hz: float
    confidence_db: float
    bin_hz: float
    sample_count: int

    @property
    def valid(self) -> bool:
        return self.confidence_db >= 6.0


def detect_tone_hz(
    samples,
    sample_rate_hz: float,
    *,
    min_hz: float = 300.0,
    max_hz: float = 5_000.0,
) -> ToneEstimate:
    """Estimate the strongest steady tone in demodulated frequency samples."""

    if not math.isfinite(sample_rate_hz) or sample_rate_hz <= 0.0:
        raise ToneError("sample rate must be finite and positive")
    if not math.isfinite(min_hz) or not math.isfinite(max_hz):
        raise ToneError("tone search bounds must be finite")
    if min_hz <= 0.0 or max_hz <= min_hz:
        raise ToneError("tone search bounds are invalid")
    values = np.asarray(samples, dtype=np.float64)
    if values.ndim != 1 or values.size < 256:
        raise ToneError("at least 256 demodulated samples are required")
    if not np.all(np.isfinite(values)):
        raise ToneError("demodulated samples contain non-finite values")

    values = values - float(np.mean(values))
    if not np.any(np.abs(values) > 1e-12):
        raise ToneError("demodulated audio is flat")

    window = np.hanning(values.size)
    spectrum = np.abs(np.fft.rfft(values * window))
    frequencies = np.fft.rfftfreq(values.size, d=1.0 / sample_rate_hz)
    upper = min(max_hz, sample_rate_hz / 2.0)
    search = (frequencies >= min_hz) & (frequencies <= upper)
    if not np.any(search):
        raise ToneError("tone search range is outside the sample bandwidth")
    band = spectrum[search]
    indices = np.flatnonzero(search)
    if band.size < 3 or not np.any(band > 0.0):
        raise ToneError("no usable tone peak found")
    peak_offset = int(np.argmax(band))
    peak_index = int(indices[peak_offset])
    peak = float(spectrum[peak_index])
    noise = float(np.median(band))
    confidence = (
        20.0 * math.log10(max(peak, 1e-12) / max(noise, 1e-12))
        if noise > 0.0
        else 120.0
    )

    # Parabolic interpolation improves the peak location beyond one FFT bin.
    offset = 0.0
    if 0 < peak_index < spectrum.size - 1:
        left = float(spectrum[peak_index - 1])
        center = float(spectrum[peak_index])
        right = float(spectrum[peak_index + 1])
        denominator = left - 2.0 * center + right
        if abs(denominator) > 1e-15:
            offset = 0.5 * (left - right) / denominator
            offset = max(-0.5, min(0.5, offset))
    bin_hz = sample_rate_hz / values.size
    frequency = (peak_index + offset) * bin_hz
    return ToneEstimate(
        frequency_hz=float(frequency),
        confidence_db=float(confidence),
        bin_hz=float(bin_hz),
        sample_count=int(values.size),
    )
