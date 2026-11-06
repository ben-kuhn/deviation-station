"""Offline analysis of recorded CF32 captures."""

from __future__ import annotations

import json
import math
from dataclasses import dataclass
from fractions import Fraction
from pathlib import Path
from typing import Any, Mapping

import numpy as np
from numpy.typing import NDArray
from scipy.signal import filtfilt, firwin, resample_poly

from .discriminator import DiscriminatorError, quadrature_demod_frequency
from .measurement import DeviationReading
from .profiles import (
    Fsk4Profile,
    GENERIC_FM_PROFILE,
    GenericFmProfile,
    MODULE17_M17_PROFILE,
    MMDVM_DMR_PROFILE,
)

ComplexArray = NDArray[np.complex128]
MAX_ANALYSIS_SAMPLES = 50_000_000


class AnalysisError(ValueError):
    """Raised when a capture cannot be analyzed."""


@dataclass(frozen=True, slots=True)
class RecordedCapture:
    """Loaded capture and the metadata needed to channelize it."""

    path: Path
    metadata_path: Path
    samples: ComplexArray
    sample_rate_hz: float
    center_frequency_hz: float
    signal_frequency_hz: float
    metadata: Mapping[str, Any]
    warnings: tuple[str, ...]

    @property
    def offset_hz(self) -> float:
        return self.signal_frequency_hz - self.center_frequency_hz


@dataclass(frozen=True, slots=True)
class ChannelizedSignal:
    samples: ComplexArray
    sample_rate_hz: float
    shift_hz: float
    channel_bandwidth_hz: float
    lowpass_taps: int


@dataclass(frozen=True, slots=True)
class ActivitySegment:
    start_sample: int
    end_sample: int
    sample_rate_hz: float
    rms: float
    noise_rms: float

    @property
    def start_s(self) -> float:
        return self.start_sample / self.sample_rate_hz

    @property
    def end_s(self) -> float:
        return self.end_sample / self.sample_rate_hz

    @property
    def duration_s(self) -> float:
        return (self.end_sample - self.start_sample) / self.sample_rate_hz

    @property
    def snr_db(self) -> float:
        if self.noise_rms <= 0.0 or self.rms <= 0.0:
            return float("nan")
        return 20.0 * math.log10(self.rms / self.noise_rms)


@dataclass(frozen=True, slots=True)
class SegmentReading:
    segment: ActivitySegment
    reading: DeviationReading | None
    error: str | None = None


@dataclass(frozen=True, slots=True)
class WindowAnalysis:
    """Measurement result for one contiguous block of samples."""

    profile: Fsk4Profile | GenericFmProfile
    channel: ChannelizedSignal
    segments: tuple[SegmentReading, ...]
    selected_index: int | None

    @property
    def selected(self) -> DeviationReading | None:
        if self.selected_index is None:
            return None
        return self.segments[self.selected_index].reading

    def summary(self) -> dict[str, Any] | None:
        return summarize_segments(self.segments)


def summarize_segments(segments: tuple[SegmentReading, ...]) -> dict[str, Any] | None:
    values = [
        item.reading.deviation_hz
        for item in segments
        if item.reading is not None and item.reading.deviation_hz is not None
    ]
    if not values:
        return None
    deviations = np.asarray(values, dtype=np.float64)
    return {
        "valid_segments": int(deviations.size),
        "total_segments": len(segments),
        "median_deviation_hz": float(np.median(deviations)),
        "minimum_deviation_hz": float(np.min(deviations)),
        "maximum_deviation_hz": float(np.max(deviations)),
        "standard_deviation_hz": float(np.std(deviations)),
    }


def select_representative(segments: tuple[SegmentReading, ...]) -> int | None:
    valid_indices = [
        index
        for index, item in enumerate(segments)
        if item.reading is not None
        and item.reading.valid
        and item.reading.deviation_hz is not None
    ]
    if not valid_indices:
        return None
    deviations = np.asarray(
        [segments[index].reading.deviation_hz for index in valid_indices],
        dtype=np.float64,
    )
    median = float(np.median(deviations))
    return min(
        valid_indices,
        key=lambda index: (
            abs(segments[index].reading.deviation_hz - median),
            -segments[index].segment.duration_s,
        ),
    )


@dataclass(frozen=True, slots=True)
class CaptureAnalysis:
    capture: RecordedCapture
    profile: Fsk4Profile | GenericFmProfile
    channel: ChannelizedSignal
    segments: tuple[SegmentReading, ...]
    selected_index: int | None

    @property
    def selected(self) -> DeviationReading | None:
        if self.selected_index is None:
            return None
        return self.segments[self.selected_index].reading

    def summary(self) -> dict[str, Any] | None:
        return summarize_segments(self.segments)

    def as_dict(self) -> dict[str, Any]:
        capture_meta = self.capture.metadata
        return {
            "schema_version": 1,
            "capture": {
                "path": str(self.capture.path),
                "metadata_path": str(self.capture.metadata_path),
                "sample_rate_hz": self.capture.sample_rate_hz,
                "center_frequency_hz": self.capture.center_frequency_hz,
                "signal_frequency_hz": self.capture.signal_frequency_hz,
                "offset_hz": self.capture.offset_hz,
                "warnings": list(self.capture.warnings),
                "recorded_format": capture_meta.get("format"),
            },
            "analysis": {
                "profile_id": self.profile.profile_id,
                "profile_label": self.profile.label,
                "target_sample_rate_hz": self.channel.sample_rate_hz,
                "channel_bandwidth_hz": self.channel.channel_bandwidth_hz,
                "shift_hz": self.channel.shift_hz,
                "lowpass_taps": self.channel.lowpass_taps,
            },
            "activity": [
                {
                    "start_s": segment.segment.start_s,
                    "end_s": segment.segment.end_s,
                    "duration_s": segment.segment.duration_s,
                    "rms": segment.segment.rms,
                    "noise_rms": segment.segment.noise_rms,
                    "snr_db": segment.segment.snr_db,
                    "reading": (
                        segment.reading.as_dict() if segment.reading is not None else None
                    ),
                    "error": segment.error,
                }
                for segment in self.segments
            ],
            "summary": self.summary(),
            "selected_segment": self.selected_index,
            "selected_reading": (
                self.selected.as_dict() if self.selected is not None else None
            ),
        }


_PROFILES: dict[str, Fsk4Profile | GenericFmProfile] = {
    "m17": MODULE17_M17_PROFILE,
    "dmr": MMDVM_DMR_PROFILE,
    "generic-fm": GENERIC_FM_PROFILE,
}
PROFILE_NAMES = tuple(_PROFILES)


def profile_for_name(name: str) -> Fsk4Profile | GenericFmProfile:
    try:
        return _PROFILES[name]
    except KeyError as exc:
        raise AnalysisError(
            f"unknown profile {name!r}; choose from {', '.join(PROFILE_NAMES)}"
        ) from exc


def load_capture(
    path: Path,
    *,
    signal_frequency_hz: float | None = None,
) -> RecordedCapture:
    """Load a raw CF32 capture and validate its sidecar metadata."""

    capture_path = Path(path)
    metadata_path = capture_path.with_name(capture_path.name + ".json")
    if not capture_path.exists():
        raise AnalysisError(f"capture does not exist: {capture_path}")
    if not metadata_path.exists():
        raise AnalysisError(f"capture metadata does not exist: {metadata_path}")

    try:
        metadata = json.loads(metadata_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise AnalysisError(f"cannot read capture metadata: {exc}") from exc

    fmt = metadata.get("format", {})
    if fmt.get("name") != "cf32" or fmt.get("dtype") != "complex64":
        raise AnalysisError("only raw little-endian complex64 CF32 captures are supported")
    if fmt.get("byte_order") != "little" or int(fmt.get("header_bytes", -1)) != 0:
        raise AnalysisError("unsupported CF32 byte order or header")

    result = metadata.get("result", {})
    recorded_samples = int(result.get("captured_samples", 0))
    if recorded_samples < 1:
        raise AnalysisError("metadata does not report any captured samples")
    if recorded_samples > MAX_ANALYSIS_SAMPLES:
        raise AnalysisError(
            f"capture exceeds the offline analysis limit of {MAX_ANALYSIS_SAMPLES} samples"
        )

    expected_bytes = recorded_samples * np.dtype("<c8").itemsize
    actual_bytes = capture_path.stat().st_size
    if actual_bytes != expected_bytes:
        raise AnalysisError(
            f"capture size mismatch: metadata says {recorded_samples} samples, "
            f"file contains {actual_bytes / np.dtype('<c8').itemsize:g}"
        )

    sample_rate = float(result.get("sample_rate_hz", 0.0))
    if not math.isfinite(sample_rate) or sample_rate <= 0.0:
        raise AnalysisError("metadata has an invalid capture sample rate")

    requested = metadata.get("requested", {})
    device = metadata.get("device", {})
    center = device.get("actual_center_frequency_hz")
    if center is None:
        center = requested.get("center_frequency_hz")
    if center is None:
        raise AnalysisError("metadata does not identify the SDR center frequency")
    center = float(center)
    if not math.isfinite(center) or center <= 0.0:
        raise AnalysisError("metadata has an invalid center frequency")

    if signal_frequency_hz is not None:
        signal = float(signal_frequency_hz)
    else:
        recorded_signal = requested.get("signal_frequency_hz")
        if recorded_signal is None:
            raise AnalysisError(
                "capture metadata has no signal frequency; provide --signal-frequency"
            )
        signal = float(recorded_signal)
    if not math.isfinite(signal) or signal <= 0.0:
        raise AnalysisError("signal frequency must be finite and greater than zero")

    samples = np.fromfile(capture_path, dtype="<c8")
    if samples.size != recorded_samples:
        raise AnalysisError("capture read did not match the recorded sample count")
    samples = samples.astype(np.complex128, copy=False)

    warnings: list[str] = []
    if not bool(result.get("completed", False)):
        warnings.append("source capture is marked incomplete")
    clipped = float(result.get("clipped_fraction", 0.0) or 0.0)
    if clipped > 0.001:
        warnings.append(f"source capture has {100.0 * clipped:.3f}% clipped samples")
    overflows = int(result.get("overflow_count", 0) or 0)
    if overflows:
        warnings.append(f"source capture reports {overflows} stream overflows")

    return RecordedCapture(
        path=capture_path,
        metadata_path=metadata_path,
        samples=samples,
        sample_rate_hz=sample_rate,
        center_frequency_hz=center,
        signal_frequency_hz=signal,
        metadata=metadata,
        warnings=tuple(warnings),
    )


def _rate_fraction(target_rate_hz: float, source_rate_hz: float) -> Fraction:
    target = int(round(target_rate_hz))
    source = int(round(source_rate_hz))
    if target <= 0 or source <= 0:
        raise AnalysisError("sample rates must be positive")
    if not math.isclose(target_rate_hz, target, abs_tol=1e-6) or not math.isclose(
        source_rate_hz,
        source,
        abs_tol=1e-6,
    ):
        return Fraction(target_rate_hz / source_rate_hz).limit_denominator(1_000_000)
    return Fraction(target, source)


def channelize_samples(
    samples: NDArray[np.complexfloating],
    *,
    source_sample_rate_hz: float,
    target_sample_rate_hz: float = 48_000.0,
    channel_bandwidth_hz: float = 20_000.0,
    shift_hz: float = 0.0,
) -> ChannelizedSignal:
    """Frequency-shift, resample, and low-pass filter complex samples."""

    source_rate = float(source_sample_rate_hz)
    target_rate = float(target_sample_rate_hz)
    shift = float(shift_hz)
    bandwidth = float(channel_bandwidth_hz)
    if not math.isfinite(source_rate) or source_rate <= 0.0:
        raise AnalysisError("source sample rate must be finite and greater than zero")
    if abs(shift) >= source_rate / 2.0:
        raise AnalysisError("signal frequency lies outside the captured Nyquist band")
    if not math.isfinite(target_rate) or target_rate <= 0.0:
        raise AnalysisError("target sample rate must be finite and greater than zero")
    if not math.isfinite(bandwidth) or bandwidth <= 0.0:
        raise AnalysisError("channel bandwidth must be finite and greater than zero")
    if bandwidth >= target_rate:
        raise AnalysisError("channel bandwidth must be below the target sample rate")

    shifted = np.asarray(samples, dtype=np.complex128)
    if shifted.ndim != 1 or shifted.size < 1:
        raise AnalysisError("samples must be a non-empty one-dimensional array")
    if shift != 0.0:
        indices = np.arange(shifted.size, dtype=np.float64)
        shifted = shifted * np.exp(-2j * math.pi * shift * indices / source_rate)

    ratio = _rate_fraction(target_rate, source_rate)
    if ratio != 1:
        shifted = resample_poly(shifted, ratio.numerator, ratio.denominator)

    cutoff = min(bandwidth / 2.0, 0.45 * target_rate)
    taps = 101
    coefficients = firwin(
        taps,
        cutoff,
        fs=target_rate,
        window="blackmanharris",
    )
    filtered = filtfilt(coefficients, [1.0], shifted)

    return ChannelizedSignal(
        samples=filtered.astype(np.complex128, copy=False),
        sample_rate_hz=target_rate,
        shift_hz=shift,
        channel_bandwidth_hz=bandwidth,
        lowpass_taps=taps,
    )


def channelize(
    capture: RecordedCapture,
    *,
    target_sample_rate_hz: float = 48_000.0,
    channel_bandwidth_hz: float = 20_000.0,
    signal_frequency_hz: float | None = None,
) -> ChannelizedSignal:
    """Frequency-shift, resample, and low-pass filter a raw capture."""

    if signal_frequency_hz is None:
        signal_frequency_hz = capture.signal_frequency_hz
    shift_hz = float(signal_frequency_hz) - capture.center_frequency_hz
    return channelize_samples(
        capture.samples,
        source_sample_rate_hz=capture.sample_rate_hz,
        target_sample_rate_hz=target_sample_rate_hz,
        channel_bandwidth_hz=channel_bandwidth_hz,
        shift_hz=shift_hz,
    )


def detect_activity(
    signal: ChannelizedSignal,
    *,
    threshold_db: float = 10.0,
    min_duration_s: float = 0.02,
    merge_gap_s: float = 0.004,
    assume_continuous_if_flat: bool = False,
) -> tuple[tuple[ActivitySegment, ...], float]:
    """Return active signal segments and the estimated noise RMS."""

    if not math.isfinite(threshold_db):
        raise AnalysisError("activity threshold must be finite")
    if min_duration_s <= 0.0 or merge_gap_s < 0.0:
        raise AnalysisError("invalid activity duration settings")

    rate = signal.sample_rate_hz
    block_size = max(8, int(round(rate * 0.002)))
    block_count = signal.samples.size // block_size
    if block_count < 1:
        raise AnalysisError("capture is too short for activity detection")

    power = np.mean(
        np.abs(signal.samples[: block_count * block_size]).reshape(block_count, block_size)
        ** 2,
        axis=1,
    )
    block_rms = np.sqrt(power)
    noise_rms = float(np.percentile(block_rms, 10.0))
    if not math.isfinite(noise_rms) or noise_rms <= 0.0:
        noise_rms = float(np.median(block_rms))
    threshold = max(
        noise_rms * 10.0 ** (threshold_db / 20.0),
        float(np.max(block_rms)) * 0.02,
    )
    peak_rms = float(np.max(block_rms))
    dynamic_range_db = (
        20.0 * math.log10(peak_rms / noise_rms)
        if noise_rms > 0.0 and peak_rms > 0.0
        else 0.0
    )
    noise_estimated = True
    if assume_continuous_if_flat and dynamic_range_db < threshold_db:
        # A live window can sit entirely inside a continuous M17 transmission.
        # There is no noise-only region from which to derive a threshold. Pass
        # the whole window to the profile; digital quality checks still reject
        # unstructured noise.
        active = np.ones(block_count, dtype=bool)
        noise_estimated = False
    else:
        active = block_rms >= threshold

    groups: list[list[int]] = []
    start: int | None = None
    for index, is_active in enumerate(active):
        if is_active and start is None:
            start = index
        elif not is_active and start is not None:
            groups.append([start, index])
            start = None
    if start is not None:
        groups.append([start, block_count])

    max_gap_blocks = int(round(merge_gap_s / (block_size / rate)))
    if max_gap_blocks > 0 and len(groups) > 1:
        merged: list[list[int]] = [groups[0]]
        for group in groups[1:]:
            if group[0] - merged[-1][1] <= max_gap_blocks:
                merged[-1][1] = group[1]
            else:
                merged.append(group)
        groups = merged

    minimum_blocks = max(1, int(round(min_duration_s / (block_size / rate))))
    segments: list[ActivitySegment] = []
    for start_block, end_block in groups:
        if end_block - start_block < minimum_blocks:
            continue
        start_sample = start_block * block_size
        end_sample = min(end_block * block_size, signal.samples.size)
        segment_rms = float(np.sqrt(np.mean(np.abs(signal.samples[start_sample:end_sample]) ** 2)))
        segments.append(
            ActivitySegment(
                start_sample=start_sample,
                end_sample=end_sample,
                sample_rate_hz=rate,
                rms=segment_rms,
                noise_rms=noise_rms if noise_estimated else float("nan"),
            )
        )
    return tuple(segments), noise_rms


def channel_spectral_flatness(samples: NDArray[np.complexfloating]) -> float | None:
    """Return spectral flatness of a channelized complex block.

    White noise is close to flat (near 1.0), while an FM carrier and its
    sidebands concentrate energy into a small part of the channel spectrum
    (near 0.0).  This gives the Generic FM profile a way to reject a live
    window that contains only receiver noise.
    """

    values = np.asarray(samples, dtype=np.complex128)
    if values.ndim != 1 or values.size < 64:
        return None
    if not np.all(np.isfinite(values)):
        return None
    values = values - np.mean(values)
    if not np.any(np.abs(values) > 1e-12):
        return None
    window = np.hanning(values.size)
    power = np.abs(np.fft.fft(values * window)) ** 2
    power = power[1:]  # discard the DC term
    if power.size == 0 or not np.any(power > 0.0):
        return None
    floor = 1e-30
    geometric_mean = math.exp(float(np.mean(np.log(power + floor))))
    arithmetic_mean = float(np.mean(power))
    if arithmetic_mean <= 0.0:
        return None
    return geometric_mean / arithmetic_mean


def _measure_segments(
    profile: Fsk4Profile | GenericFmProfile,
    signal: ChannelizedSignal,
    segments: tuple[ActivitySegment, ...],
    *,
    frequency_hz: NDArray[np.floating] | None = None,
) -> tuple[SegmentReading, ...]:
    results: list[SegmentReading] = []
    edge_trim_s = 0.02 if profile.profile_id == MODULE17_M17_PROFILE.profile_id else 0.002
    edge_trim = int(round(edge_trim_s * signal.sample_rate_hz))
    if frequency_hz is not None and frequency_hz.size < signal.samples.size:
        raise AnalysisError(
            "demodulated frequency and complex channel samples must have matching length"
        )
    for segment in segments:
        start = segment.start_sample + edge_trim
        end = segment.end_sample - edge_trim
        if end - start < 64:
            results.append(
                SegmentReading(
                    segment=segment,
                    reading=None,
                    error="active segment is too short after edge trimming",
                )
            )
            continue
        if frequency_hz is None:
            segment_frequency = quadrature_demod_frequency(
                signal.samples[start:end],
                signal.sample_rate_hz,
            )
        else:
            segment_frequency = frequency_hz[start:end]
        try:
            if isinstance(profile, GenericFmProfile):
                reading = profile.measure(
                    segment_frequency,
                    signal.sample_rate_hz,
                    carrier_flatness=channel_spectral_flatness(
                        signal.samples[start:end]
                    ),
                )
            else:
                reading = profile.measure(
                    segment_frequency,
                    signal.sample_rate_hz,
                )
        except (DiscriminatorError, ValueError) as exc:
            results.append(
                SegmentReading(
                    segment=segment,
                    reading=None,
                    error=str(exc),
                )
            )
            continue
        results.append(SegmentReading(segment=segment, reading=reading))
    return tuple(results)


def measure_channelized_frequency(
    profile: Fsk4Profile | GenericFmProfile,
    samples: NDArray[np.complexfloating],
    frequency_hz: NDArray[np.floating],
    sample_rate_hz: float,
    *,
    activity_threshold_db: float = 10.0,
    assume_continuous_if_flat: bool = False,
) -> WindowAnalysis:
    """Measure already channelized I/Q plus externally demodulated frequency."""

    complex_samples = np.asarray(samples, dtype=np.complex128)
    frequency = np.asarray(frequency_hz, dtype=np.float64)
    if complex_samples.ndim != 1 or frequency.ndim != 1:
        raise AnalysisError("channel samples and frequency must be one-dimensional")
    if complex_samples.size != frequency.size:
        raise AnalysisError("channel samples and frequency must have equal length")
    signal = ChannelizedSignal(
        samples=complex_samples,
        sample_rate_hz=float(sample_rate_hz),
        shift_hz=0.0,
        channel_bandwidth_hz=float(sample_rate_hz),
        lowpass_taps=0,
    )
    segments, _ = detect_activity(
        signal,
        threshold_db=activity_threshold_db,
        assume_continuous_if_flat=assume_continuous_if_flat,
    )
    readings = _measure_segments(
        profile,
        signal,
        segments,
        frequency_hz=frequency,
    )
    return WindowAnalysis(
        profile=profile,
        channel=signal,
        segments=readings,
        selected_index=select_representative(readings),
    )


def analyze_samples(
    samples: NDArray[np.complexfloating],
    *,
    source_sample_rate_hz: float,
    profile: Fsk4Profile | GenericFmProfile,
    target_sample_rate_hz: float = 48_000.0,
    channel_bandwidth_hz: float = 20_000.0,
    shift_hz: float = 0.0,
    activity_threshold_db: float = 10.0,
    assume_continuous_if_flat: bool = False,
) -> WindowAnalysis:
    """Channelize and measure one block of complex IQ samples."""

    signal = channelize_samples(
        samples,
        source_sample_rate_hz=source_sample_rate_hz,
        target_sample_rate_hz=target_sample_rate_hz,
        channel_bandwidth_hz=channel_bandwidth_hz,
        shift_hz=shift_hz,
    )
    segments, _ = detect_activity(
        signal,
        threshold_db=activity_threshold_db,
        assume_continuous_if_flat=assume_continuous_if_flat,
    )
    readings = _measure_segments(profile, signal, segments)
    return WindowAnalysis(
        profile=profile,
        channel=signal,
        segments=readings,
        selected_index=select_representative(readings),
    )


def write_analysis_report(result: CaptureAnalysis, path: Path) -> Path:
    """Write an analysis report atomically."""

    report_path = Path(path)
    report_path.parent.mkdir(parents=True, exist_ok=True)
    temporary = report_path.with_name(report_path.name + ".tmp")
    temporary.write_text(
        json.dumps(result.as_dict(), indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    temporary.replace(report_path)
    return report_path


def analyze_capture(
    path: Path,
    *,
    profile_name: str,
    target_sample_rate_hz: float = 48_000.0,
    channel_bandwidth_hz: float = 20_000.0,
    signal_frequency_hz: float | None = None,
    activity_threshold_db: float = 10.0,
) -> CaptureAnalysis:
    """Load and analyze one capture using a digital-mode profile."""

    profile = profile_for_name(profile_name)
    if isinstance(profile, Fsk4Profile):
        samples_per_symbol = target_sample_rate_hz / profile.symbol_rate_hz
        rounded_sps = int(round(samples_per_symbol))
        if rounded_sps < 4 or not math.isclose(
            samples_per_symbol,
            rounded_sps,
            rel_tol=0.0,
            abs_tol=1e-9,
        ):
            raise AnalysisError(
                "target sample rate must be an integer multiple of the profile symbol rate"
            )

    capture = load_capture(path, signal_frequency_hz=signal_frequency_hz)
    signal = channelize(
        capture,
        target_sample_rate_hz=target_sample_rate_hz,
        channel_bandwidth_hz=channel_bandwidth_hz,
        signal_frequency_hz=signal_frequency_hz,
    )
    segments, _ = detect_activity(
        signal,
        threshold_db=activity_threshold_db,
    )
    readings = _measure_segments(profile, signal, segments)
    return CaptureAnalysis(
        capture=capture,
        profile=profile,
        channel=signal,
        segments=readings,
        selected_index=select_representative(readings),
    )
