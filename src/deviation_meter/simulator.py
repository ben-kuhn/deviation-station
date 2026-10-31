"""Complex-baseband signal simulators for development and regression tests."""

from __future__ import annotations

import math
from collections.abc import Sequence

import numpy as np
from numpy.typing import NDArray

ComplexArray = NDArray[np.complex128]
FloatArray = NDArray[np.float64]
MAX_SIMULATED_SAMPLES = 1_000_000
MAX_FILTER_TAPS = 65_537


class SimulationError(ValueError):
    """Raised when a simulated signal cannot be generated as requested."""


def _positive_finite(value: float, name: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise SimulationError(f"{name} must be a real number")
    result = float(value)
    if not math.isfinite(result) or result <= 0.0:
        raise SimulationError(f"{name} must be finite and greater than zero")
    return result


def _nonnegative_finite(value: float, name: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise SimulationError(f"{name} must be a real number")
    result = float(value)
    if not math.isfinite(result) or result < 0.0:
        raise SimulationError(f"{name} must be finite and non-negative")
    return result


def _rng(seed: int | None) -> np.random.Generator:
    return np.random.default_rng(seed)


def _validate_sample_count(sample_count: int) -> None:
    if sample_count > MAX_SIMULATED_SAMPLES:
        raise SimulationError(
            f"requested {sample_count} samples; limit is {MAX_SIMULATED_SAMPLES}"
        )


def _validate_finite_complex(value: complex, name: str) -> complex:
    result = complex(value)
    if not math.isfinite(result.real) or not math.isfinite(result.imag):
        raise SimulationError(f"{name} must have finite real and imaginary parts")
    return result


def _add_complex_noise(
    signal: ComplexArray,
    snr_db: float | None,
    rng: np.random.Generator,
) -> ComplexArray:
    if snr_db is None:
        return signal
    if not math.isfinite(snr_db):
        raise SimulationError("snr_db must be finite when provided")

    signal_power = float(np.mean(np.abs(signal) ** 2))
    if signal_power <= 0.0:
        raise SimulationError("cannot add noise to a zero-power signal")

    noise_power = signal_power / (10.0 ** (snr_db / 10.0))
    sigma = math.sqrt(noise_power / 2.0)
    noise = sigma * (rng.standard_normal(signal.size) + 1j * rng.standard_normal(signal.size))
    return signal + noise.astype(np.complex128)


def time_axis(duration_s: float, sample_rate_hz: float) -> FloatArray:
    """Return a uniformly sampled time axis."""

    duration = _positive_finite(duration_s, "duration_s")
    sample_rate = _positive_finite(sample_rate_hz, "sample_rate_hz")
    sample_count = max(1, int(round(duration * sample_rate)))
    _validate_sample_count(sample_count)
    return np.arange(sample_count, dtype=np.float64) / sample_rate


def sinusoidal_fm_iq(
    tone_frequency_hz: float,
    deviation_hz: float,
    sample_rate_hz: float,
    duration_s: float,
    *,
    amplitude: float = 1.0,
    frequency_offset_hz: float = 0.0,
    phase_offset_rad: float = 0.0,
    snr_db: float | None = None,
    dc_offset: complex = 0.0,
    seed: int | None = None,
) -> ComplexArray:
    """Generate complex baseband for sinusoidal FM.

    The resulting RF signal is conceptually::

        A cos(2*pi*fc*t + beta*sin(2*pi*fm*t))

    After removing the carrier, the returned samples contain the phase
    trajectory and any requested frequency offset.
    """

    tone_hz = _positive_finite(tone_frequency_hz, "tone_frequency_hz")
    deviation = _nonnegative_finite(deviation_hz, "deviation_hz")
    sample_rate = _positive_finite(sample_rate_hz, "sample_rate_hz")
    amplitude_value = _nonnegative_finite(amplitude, "amplitude")
    if tone_hz >= sample_rate / 2.0:
        raise SimulationError("tone_frequency_hz must be below Nyquist")
    offset_hz = float(frequency_offset_hz)
    if not math.isfinite(offset_hz):
        raise SimulationError("frequency_offset_hz must be finite")
    if abs(offset_hz) + deviation >= sample_rate / 2.0:
        raise SimulationError("frequency excursion reaches or exceeds Nyquist")

    phase_offset = float(phase_offset_rad)
    if not math.isfinite(phase_offset):
        raise SimulationError("phase_offset_rad must be finite")

    t = time_axis(duration_s, sample_rate)
    beta = deviation / tone_hz
    phase = (
        2.0 * math.pi * offset_hz * t
        + beta * np.sin(2.0 * math.pi * tone_hz * t)
        + phase_offset
    )
    signal = amplitude_value * np.exp(1j * phase)
    signal = signal.astype(np.complex128)
    signal = _add_complex_noise(signal, snr_db, _rng(seed))
    return signal + _validate_finite_complex(dc_offset, "dc_offset")


def unmodulated_carrier_iq(
    sample_rate_hz: float,
    duration_s: float,
    *,
    frequency_offset_hz: float = 0.0,
    amplitude: float = 1.0,
    phase_offset_rad: float = 0.0,
    snr_db: float | None = None,
    dc_offset: complex = 0.0,
    seed: int | None = None,
) -> ComplexArray:
    """Generate an unmodulated complex carrier."""

    sample_rate = _positive_finite(sample_rate_hz, "sample_rate_hz")
    offset_hz = float(frequency_offset_hz)
    phase_offset = float(phase_offset_rad)
    if not math.isfinite(offset_hz):
        raise SimulationError("frequency_offset_hz must be finite")
    if abs(offset_hz) >= sample_rate / 2.0:
        raise SimulationError("frequency_offset_hz must be below Nyquist")
    if not math.isfinite(phase_offset):
        raise SimulationError("phase_offset_rad must be finite")

    t = time_axis(duration_s, sample_rate)
    phase = 2.0 * math.pi * offset_hz * t + phase_offset
    signal = _nonnegative_finite(amplitude, "amplitude") * np.exp(1j * phase)
    signal = _add_complex_noise(signal.astype(np.complex128), snr_db, _rng(seed))
    return signal + _validate_finite_complex(dc_offset, "dc_offset")


def _raised_cosine_taps(
    samples_per_symbol: int,
    rolloff: float,
    span_symbols: int,
) -> FloatArray:
    if not 0.0 <= rolloff <= 1.0:
        raise SimulationError("rolloff must be between 0 and 1")
    if isinstance(span_symbols, bool) or not isinstance(span_symbols, int):
        raise SimulationError("span_symbols must be an integer")
    if span_symbols < 2:
        raise SimulationError("span_symbols must be at least 2")

    tap_count = 2 * span_symbols * samples_per_symbol + 1
    if tap_count > MAX_FILTER_TAPS:
        raise SimulationError(f"raised-cosine filter exceeds {MAX_FILTER_TAPS} taps")
    half_span = span_symbols * samples_per_symbol
    sample_offsets = np.arange(-half_span, half_span + 1, dtype=np.float64)
    t = sample_offsets / samples_per_symbol
    taps = np.empty_like(t)

    for index, time_symbols in enumerate(t):
        if abs(time_symbols) < 1e-12:
            taps[index] = 1.0
        else:
            denominator = 1.0 - (2.0 * rolloff * time_symbols) ** 2
            numerator = np.sinc(time_symbols) * np.cos(math.pi * rolloff * time_symbols)
            if abs(denominator) < 1e-12:
                taps[index] = (math.pi / 4.0) * np.sinc(1.0 / (2.0 * rolloff))
            else:
                taps[index] = numerator / denominator
    return taps.astype(np.float64)


def fsk4_symbol_indices(
    symbol_count: int,
    *,
    seed: int | None = None,
    rng: np.random.Generator | None = None,
) -> NDArray[np.int8]:
    """Generate uniformly distributed random 4FSK symbol indices (0..3)."""

    if isinstance(symbol_count, bool) or not isinstance(symbol_count, int):
        raise SimulationError("symbol_count must be an integer")
    if symbol_count < 1:
        raise SimulationError("symbol_count must be positive")
    generator = rng if rng is not None else _rng(seed)
    return generator.integers(0, 4, size=symbol_count, dtype=np.int8)


def _fft_convolve_full(first: FloatArray, second: FloatArray) -> FloatArray:
    """Bounded full linear convolution using an FFT rather than O(N*M) work."""

    full_size = first.size + second.size - 1
    _validate_sample_count(full_size)
    fft_size = 1 << (full_size - 1).bit_length()
    spectrum = np.fft.rfft(first, fft_size) * np.fft.rfft(second, fft_size)
    return np.fft.irfft(spectrum, fft_size)[:full_size].astype(np.float64)


def fsk4_frequency_trajectory(
    symbol_indices: Sequence[int] | NDArray[np.integer],
    symbol_rate_hz: float,
    samples_per_symbol: int,
    outer_deviation_hz: float,
    *,
    pulse_shape: str = "raised_cosine",
    rolloff: float = 0.5,
    span_symbols: int = 8,
) -> FloatArray:
    """Generate a 4FSK frequency trajectory with levels +/-3 and +/-1.

    ``outer_deviation_hz`` is the magnitude of the outer symbols. The inner
    symbols are therefore at one third of that value.
    """

    symbol_rate = _positive_finite(symbol_rate_hz, "symbol_rate_hz")
    outer_deviation = _positive_finite(outer_deviation_hz, "outer_deviation_hz")
    if isinstance(samples_per_symbol, bool) or not isinstance(samples_per_symbol, int):
        raise SimulationError("samples_per_symbol must be an integer")
    if samples_per_symbol < 4:
        raise SimulationError("samples_per_symbol must be at least 4")

    symbols = np.asarray(symbol_indices)
    if symbols.ndim != 1 or symbols.size < 1:
        raise SimulationError("symbol_indices must be a non-empty one-dimensional array")
    if not np.issubdtype(symbols.dtype, np.integer):
        raise SimulationError("symbol_indices must contain integers")
    if np.any((symbols < 0) | (symbols > 3)):
        raise SimulationError("symbol indices must be in the range 0 through 3")
    _validate_sample_count(symbols.size * samples_per_symbol)

    normalized_levels = np.array([-3.0, -1.0, 1.0, 3.0], dtype=np.float64)
    symbol_frequencies = normalized_levels[symbols.astype(np.intp)] * (outer_deviation / 3.0)

    if pulse_shape == "rectangular":
        return np.repeat(symbol_frequencies, samples_per_symbol).astype(np.float64)

    if pulse_shape != "raised_cosine":
        raise SimulationError("pulse_shape must be 'rectangular' or 'raised_cosine'")

    taps = _raised_cosine_taps(samples_per_symbol, rolloff, span_symbols)
    impulses = np.zeros(symbols.size * samples_per_symbol, dtype=np.float64)
    impulses[::samples_per_symbol] = symbol_frequencies
    full = _fft_convolve_full(impulses, taps)
    center = taps.size // 2
    trajectory = full[center : center + impulses.size]
    return trajectory.astype(np.float64)


def fsk4_iq(
    symbol_rate_hz: float,
    outer_deviation_hz: float,
    sample_rate_hz: float,
    symbol_count: int,
    *,
    pulse_shape: str = "raised_cosine",
    rolloff: float = 0.5,
    span_symbols: int = 8,
    amplitude: float = 1.0,
    frequency_offset_hz: float = 0.0,
    snr_db: float | None = None,
    symbol_indices: Sequence[int] | NDArray[np.integer] | None = None,
    seed: int | None = None,
) -> ComplexArray:
    """Generate complex-baseband 4FSK with standard +/-3 and +/-1 levels."""

    sample_rate = _positive_finite(sample_rate_hz, "sample_rate_hz")
    symbol_rate = _positive_finite(symbol_rate_hz, "symbol_rate_hz")
    symbols_per_second_ratio = sample_rate / symbol_rate
    samples_per_symbol = int(round(symbols_per_second_ratio))
    if samples_per_symbol < 4 or not math.isclose(
        samples_per_symbol,
        symbols_per_second_ratio,
        rel_tol=0.0,
        abs_tol=1e-9,
    ):
        raise SimulationError("sample_rate_hz must be an integer multiple of symbol_rate_hz")

    if isinstance(symbol_count, bool) or not isinstance(symbol_count, int):
        raise SimulationError("symbol_count must be an integer")
    if symbol_count < 1:
        raise SimulationError("symbol_count must be positive")
    _validate_sample_count(symbol_count * samples_per_symbol)

    if symbol_indices is None:
        indices = fsk4_symbol_indices(symbol_count, seed=seed)
    else:
        indices = np.asarray(symbol_indices)
        if indices.ndim != 1 or indices.size < 1:
            raise SimulationError("symbol_indices must be a non-empty one-dimensional array")
        if symbol_count != indices.size:
            raise SimulationError("symbol_count must match the supplied symbol_indices length")

    frequency = fsk4_frequency_trajectory(
        indices,
        symbol_rate,
        samples_per_symbol,
        outer_deviation_hz,
        pulse_shape=pulse_shape,
        rolloff=rolloff,
        span_symbols=span_symbols,
    )

    offset_hz = float(frequency_offset_hz)
    if not math.isfinite(offset_hz):
        raise SimulationError("frequency_offset_hz must be finite")
    total_frequency = frequency + offset_hz
    if np.max(np.abs(total_frequency)) >= sample_rate / 2.0:
        raise SimulationError("frequency excursion reaches or exceeds Nyquist")

    phase = 2.0 * math.pi * np.cumsum(total_frequency, dtype=np.float64) / sample_rate
    signal = _nonnegative_finite(amplitude, "amplitude") * np.exp(1j * phase)
    signal = _add_complex_noise(signal.astype(np.complex128), snr_db, _rng(seed))
    return signal
