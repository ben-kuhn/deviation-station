"""Instantaneous-frequency and discriminator-based deviation estimators."""

from __future__ import annotations

import math
from dataclasses import dataclass

import numpy as np
from numpy.typing import ArrayLike, NDArray

FloatArray = NDArray[np.float64]
DEFAULT_MAX_SAMPLES = 100_000
MIN_SYMBOL_CAPTURE = 96
MAX_CLUSTER_COUNT = 16


class DiscriminatorError(ValueError):
    """Raised when an instantaneous-frequency measurement is unusable."""


@dataclass(frozen=True, slots=True)
class PeakDeviationEstimate:
    """Robust peak deviation derived from frequency percentiles."""

    deviation_hz: float
    center_hz: float
    low_hz: float
    high_hz: float
    sample_count: int
    raw_deviation_hz: float | None = None
    smoothing_window_samples: int = 0

    @property
    def definition(self) -> str:
        return "robust instantaneous-frequency peak"


@dataclass(frozen=True, slots=True)
class SymbolDeviationEstimate:
    """Symbol-level result for an equally spaced four-level FSK signal."""

    deviation_hz: float
    center_hz: float
    levels_hz: tuple[float, ...]
    populations: tuple[int, ...]
    level_spacing_hz: float
    rms_error_hz: float
    sample_count: int
    converged: bool
    timing_offset_samples: int | None = None
    timing_score: float | None = None
    capture_symbol_count: int | None = None

    @property
    def definition(self) -> str:
        return "4FSK outer-symbol deviation"


@dataclass(frozen=True, slots=True)
class KMeansResult:
    """Result and convergence evidence from one-dimensional clustering."""

    centers_hz: FloatArray
    labels: NDArray[np.intp]
    rms_error_hz: float
    iterations: int
    converged: bool


def quadrature_demod_frequency(
    iq: ArrayLike,
    sample_rate_hz: float,
    *,
    min_magnitude: float | None = None,
) -> FloatArray:
    """Demodulate complex I/Q to instantaneous frequency in hertz.

    ``min_magnitude`` optionally marks phase differences whose product
    magnitude falls below an absolute threshold as NaN. Rejected samples keep
    their original positions so symbol timing cannot be shifted accidentally;
    estimators reject captures containing those invalid intervals rather than
    compacting them into a false continuous waveform.

    The output always has one fewer sample than the input.
    """

    samples = np.asarray(iq, dtype=np.complex128)
    if samples.ndim != 1:
        raise DiscriminatorError("iq must be a one-dimensional array")
    if samples.size < 2:
        raise DiscriminatorError("at least two I/Q samples are required")
    if not np.all(np.isfinite(samples)):
        raise DiscriminatorError("iq contains non-finite values")

    rate = float(sample_rate_hz)
    if not math.isfinite(rate) or rate <= 0.0:
        raise DiscriminatorError("sample_rate_hz must be finite and greater than zero")

    products = samples[1:] * np.conj(samples[:-1])
    frequency = np.angle(products) * (rate / (2.0 * math.pi))
    if min_magnitude is not None:
        threshold = float(min_magnitude)
        if not math.isfinite(threshold) or threshold < 0.0:
            raise DiscriminatorError("min_magnitude must be finite and non-negative")
        frequency = frequency.astype(np.float64)
        frequency[np.abs(products) < threshold] = np.nan

    return frequency.astype(np.float64)


def _finite_frequency_samples(
    frequency_hz: ArrayLike,
    *,
    max_samples: int = DEFAULT_MAX_SAMPLES,
) -> FloatArray:
    samples = np.asarray(frequency_hz, dtype=np.float64)
    if samples.ndim != 1:
        raise DiscriminatorError("frequency_hz must be a one-dimensional array")
    if not np.all(np.isfinite(samples)):
        raise DiscriminatorError("frequency_hz contains non-finite samples")
    if isinstance(max_samples, bool) or not isinstance(max_samples, int):
        raise DiscriminatorError("max_samples must be an integer")
    if max_samples < 32:
        raise DiscriminatorError("max_samples must be at least 32")
    # Live callers should provide bounded windows. This final guard also makes
    # accidental multi-second/full-rate inputs safe and keeps the newest data.
    if samples.size > max_samples:
        samples = samples[-max_samples:]
    if samples.size < 32:
        raise DiscriminatorError("at least 32 finite frequency samples are required")
    return samples


def _peak_from_samples(
    samples: FloatArray,
    low_percentile: float,
    high_percentile: float,
) -> tuple[float, float, float, float]:
    low, high = np.percentile(samples, [low_percentile, high_percentile])
    center = 0.5 * (low + high)
    deviation = max(high - center, center - low)
    return float(deviation), float(center), float(low), float(high)


def _smoothing_window_samples(sample_rate_hz: float, bandwidth_hz: float) -> int:
    rate = float(sample_rate_hz)
    bandwidth = float(bandwidth_hz)
    if not math.isfinite(rate) or rate <= 0.0:
        raise DiscriminatorError("sample_rate_hz must be finite and greater than zero")
    if not math.isfinite(bandwidth) or bandwidth <= 0.0:
        raise DiscriminatorError("smoothing_bandwidth_hz must be finite and greater than zero")
    if bandwidth >= rate / 2.0:
        raise DiscriminatorError("smoothing_bandwidth_hz must be below Nyquist")
    window = max(1, int(round(rate / (2.0 * bandwidth))))
    if window > 1 and window % 2 == 0:
        window += 1
    return window


def smooth_frequency_samples(
    frequency_hz: ArrayLike,
    sample_rate_hz: float,
    bandwidth_hz: float,
    *,
    max_samples: int = DEFAULT_MAX_SAMPLES,
) -> tuple[FloatArray, int]:
    """Apply a centered moving-average low-pass filter and discard its edges."""

    samples = _finite_frequency_samples(frequency_hz, max_samples=max_samples)
    window = _smoothing_window_samples(sample_rate_hz, bandwidth_hz)
    if samples.size < 4 * window:
        raise DiscriminatorError("not enough samples for the requested smoothing bandwidth")
    if window == 1:
        return samples, window

    kernel = np.full(window, 1.0 / window, dtype=np.float64)
    filtered = np.convolve(samples, kernel, mode="same")
    edge = window // 2
    return filtered[edge:-edge].astype(np.float64), window


def robust_peak_deviation(
    frequency_hz: ArrayLike,
    *,
    low_percentile: float = 0.5,
    high_percentile: float = 99.5,
    sample_rate_hz: float | None = None,
    smoothing_bandwidth_hz: float | None = None,
    max_samples: int = DEFAULT_MAX_SAMPLES,
) -> PeakDeviationEstimate:
    """Estimate peak deviation without trusting individual outlier samples.

    Supplying ``sample_rate_hz`` and ``smoothing_bandwidth_hz`` enables
    post-demodulator low-pass filtering. The unfiltered estimate is retained
    in the result so the profile can use a large raw/smoothed discrepancy as a
    noise-quality warning.
    """

    if not 0.0 <= low_percentile < high_percentile <= 100.0:
        raise DiscriminatorError("percentiles must satisfy 0 <= low < high <= 100")
    if (sample_rate_hz is None) != (smoothing_bandwidth_hz is None):
        raise DiscriminatorError(
            "sample_rate_hz and smoothing_bandwidth_hz must be provided together"
        )

    samples = _finite_frequency_samples(frequency_hz, max_samples=max_samples)
    raw_deviation, _, _, _ = _peak_from_samples(
        samples,
        low_percentile,
        high_percentile,
    )
    smoothing_window = 0
    if sample_rate_hz is not None and smoothing_bandwidth_hz is not None:
        samples, smoothing_window = smooth_frequency_samples(
            samples,
            sample_rate_hz,
            smoothing_bandwidth_hz,
            max_samples=max_samples,
        )

    deviation, center, low, high = _peak_from_samples(
        samples,
        low_percentile,
        high_percentile,
    )
    return PeakDeviationEstimate(
        deviation_hz=deviation,
        center_hz=center,
        low_hz=low,
        high_hz=high,
        sample_count=int(samples.size),
        raw_deviation_hz=raw_deviation,
        smoothing_window_samples=smoothing_window,
    )


def one_dimensional_kmeans(
    values: ArrayLike,
    cluster_count: int,
    *,
    max_iterations: int = 100,
    tolerance_hz: float = 1e-3,
    trim_fraction: float = 0.005,
    max_samples: int = DEFAULT_MAX_SAMPLES,
) -> KMeansResult:
    """Cluster one-dimensional samples with bounded work and convergence data."""

    data = _finite_frequency_samples(values, max_samples=max_samples)
    if isinstance(cluster_count, bool) or not isinstance(cluster_count, int):
        raise DiscriminatorError("cluster_count must be an integer")
    if cluster_count < 2:
        raise DiscriminatorError("cluster_count must be at least two")
    if cluster_count > MAX_CLUSTER_COUNT:
        raise DiscriminatorError(f"cluster_count cannot exceed {MAX_CLUSTER_COUNT}")
    if data.size < cluster_count * 16:
        raise DiscriminatorError("not enough samples for the requested cluster count")
    if isinstance(max_iterations, bool) or not isinstance(max_iterations, int):
        raise DiscriminatorError("max_iterations must be an integer")
    if max_iterations < 1:
        raise DiscriminatorError("max_iterations must be positive")
    if not 0.0 <= trim_fraction < 0.5:
        raise DiscriminatorError("trim_fraction must be in [0, 0.5)")
    if not math.isfinite(tolerance_hz) or tolerance_hz <= 0.0:
        raise DiscriminatorError("tolerance_hz must be finite and positive")

    if trim_fraction > 0.0:
        lower, upper = np.quantile(data, [trim_fraction, 1.0 - trim_fraction])
        data = data[(data >= lower) & (data <= upper)]
        if data.size < cluster_count * 16:
            raise DiscriminatorError("not enough samples remain after trimming")

    if np.unique(data).size < cluster_count:
        raise DiscriminatorError("fewer distinct frequency values than cluster centers")

    quantiles = (np.arange(cluster_count, dtype=np.float64) + 0.5) / cluster_count
    centers = np.quantile(data, quantiles)
    converged = False
    iterations = 0

    for iterations in range(1, max_iterations + 1):
        distances = np.abs(data[:, None] - centers[None, :])
        labels = np.argmin(distances, axis=1)
        counts = np.bincount(labels, minlength=cluster_count)
        if np.any(counts == 0):
            raise DiscriminatorError("cluster analysis produced an empty level")
        next_centers = np.array(
            [np.mean(data[labels == index]) for index in range(cluster_count)],
            dtype=np.float64,
        )
        shift = float(np.max(np.abs(next_centers - centers)))
        centers = next_centers
        if shift <= tolerance_hz:
            converged = True
            break

    # Recompute labels from the final centers, including on non-convergence.
    order = np.argsort(centers)
    centers = centers[order]
    distances = np.abs(data[:, None] - centers[None, :])
    labels = np.argmin(distances, axis=1)
    residuals = data - centers[labels]
    rms = float(np.sqrt(np.mean(residuals**2)))
    return KMeansResult(
        centers_hz=centers,
        labels=labels,
        rms_error_hz=rms,
        iterations=iterations,
        converged=converged,
    )


def _recover_symbol_center_samples(
    samples: FloatArray,
    sample_rate_hz: float,
    symbol_rate_hz: float,
) -> tuple[FloatArray, int, float]:
    rate = float(sample_rate_hz)
    symbol_rate = float(symbol_rate_hz)
    if not math.isfinite(rate) or rate <= 0.0:
        raise DiscriminatorError("sample_rate_hz must be finite and greater than zero")
    if not math.isfinite(symbol_rate) or symbol_rate <= 0.0:
        raise DiscriminatorError("symbol_rate_hz must be finite and greater than zero")

    ratio = rate / symbol_rate
    samples_per_symbol = int(round(ratio))
    if samples_per_symbol < 4 or not math.isclose(
        ratio,
        samples_per_symbol,
        rel_tol=0.0,
        abs_tol=1e-9,
    ):
        raise DiscriminatorError(
            "symbol timing recovery currently requires an integer samples/symbol ratio"
        )

    candidates: list[tuple[int, FloatArray, float]] = []
    for offset in range(samples_per_symbol):
        symbol_samples = samples[offset::samples_per_symbol]
        if symbol_samples.size < MIN_SYMBOL_CAPTURE:
            continue
        # Nyquist-pulse-shaped 4FSK has its greatest level separation at the
        # symbol centers. Trim only extreme noise clicks for clock selection.
        lower, upper = np.quantile(symbol_samples, [0.01, 0.99])
        clipped = symbol_samples[(symbol_samples >= lower) & (symbol_samples <= upper)]
        score = float(np.var(clipped)) if clipped.size >= 32 else 0.0
        candidates.append((offset, symbol_samples, score))

    if not candidates:
        raise DiscriminatorError(
            f"at least {MIN_SYMBOL_CAPTURE} symbols are required for timing recovery"
        )

    scores = np.array([candidate[2] for candidate in candidates], dtype=np.float64)
    best_offset, best_samples, best_score = max(candidates, key=lambda item: item[2])
    median_score = float(np.median(scores))
    timing_score = best_score / median_score if median_score > 0.0 else math.inf
    return best_samples, best_offset, timing_score


def four_fsk_symbol_deviation(
    frequency_hz: ArrayLike,
    *,
    sample_rate_hz: float | None = None,
    symbol_rate_hz: float | None = None,
    trim_fraction: float = 0.005,
    max_relative_spacing_error: float = 0.25,
    min_population_fraction: float = 0.05,
    max_samples: int = DEFAULT_MAX_SAMPLES,
) -> SymbolDeviationEstimate:
    """Estimate outer-symbol deviation from a four-level FSK signal.

    When sample and symbol rates are supplied, timing recovery selects the
    sampling phase with maximum level variance and clusters only symbol-center
    samples. This avoids the transition-sample bias of clustering an entire
    pulse-shaped waveform. Without rates, the function retains its generic
    all-sample clustering behavior for synthetic level measurements.
    """

    if (sample_rate_hz is None) != (symbol_rate_hz is None):
        raise DiscriminatorError(
            "sample_rate_hz and symbol_rate_hz must be provided together"
        )

    samples = _finite_frequency_samples(frequency_hz, max_samples=max_samples)
    timing_offset: int | None = None
    timing_score: float | None = None
    capture_symbol_count: int | None = None
    if sample_rate_hz is not None and symbol_rate_hz is not None:
        samples, timing_offset, timing_score = _recover_symbol_center_samples(
            samples,
            sample_rate_hz,
            symbol_rate_hz,
        )
        capture_symbol_count = int(samples.size)

    result = one_dimensional_kmeans(
        samples,
        cluster_count=4,
        trim_fraction=trim_fraction,
        max_samples=max_samples,
    )
    centers = result.centers_hz
    labels = result.labels
    populations = np.bincount(labels, minlength=4)
    counts = tuple(int(value) for value in populations)
    if any(count < labels.size * min_population_fraction for count in counts):
        raise DiscriminatorError("one or more 4FSK symbol levels have too few samples")

    spacing = np.diff(centers)
    mean_spacing = float(np.mean(spacing))
    if mean_spacing <= 0.0:
        raise DiscriminatorError("4FSK levels are not separated")
    relative_spacing_error = float(np.max(np.abs(spacing - mean_spacing)) / mean_spacing)
    if relative_spacing_error > max_relative_spacing_error:
        raise DiscriminatorError(
            "frequency levels are not sufficiently equally spaced for 4FSK"
        )

    center = 0.5 * (centers[0] + centers[-1])
    deviation = 0.5 * (centers[-1] - centers[0])
    inner_deviation = min(abs(centers[1] - center), abs(centers[-2] - center))
    if inner_deviation <= 0.0 or deviation <= 0.0:
        raise DiscriminatorError("invalid 4FSK symbol-level ordering")

    return SymbolDeviationEstimate(
        deviation_hz=float(deviation),
        center_hz=float(center),
        levels_hz=tuple(float(value) for value in centers),
        populations=counts,
        level_spacing_hz=mean_spacing,
        rms_error_hz=result.rms_error_hz,
        sample_count=int(labels.size),
        converged=result.converged,
        timing_offset_samples=timing_offset,
        timing_score=timing_score,
        capture_symbol_count=capture_symbol_count,
    )
