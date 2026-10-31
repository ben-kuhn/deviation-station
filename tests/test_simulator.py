import math

import numpy as np
import pytest

from deviation_meter.discriminator import quadrature_demod_frequency
from deviation_meter.simulator import (
    SimulationError,
    fsk4_frequency_trajectory,
    fsk4_iq,
    fsk4_symbol_indices,
    sinusoidal_fm_iq,
    unmodulated_carrier_iq,
)


def test_unmodulated_carrier_frequency() -> None:
    sample_rate = 192_000.0
    iq = unmodulated_carrier_iq(
        sample_rate,
        0.1,
        frequency_offset_hz=700.0,
    )
    frequency = quadrature_demod_frequency(iq, sample_rate)
    assert np.mean(frequency) == pytest.approx(700.0, abs=1e-6)


def test_sinusoidal_fm_has_requested_peak_deviation() -> None:
    sample_rate = 192_000.0
    iq = sinusoidal_fm_iq(
        tone_frequency_hz=1000.0,
        deviation_hz=2400.0,
        sample_rate_hz=sample_rate,
        duration_s=0.25,
    )
    frequency = quadrature_demod_frequency(iq, sample_rate)
    assert np.max(frequency) == pytest.approx(2400.0, abs=1.0)
    assert np.min(frequency) == pytest.approx(-2400.0, abs=1.0)


def test_sinusoidal_fm_frequency_offset_is_independent_of_deviation() -> None:
    sample_rate = 192_000.0
    iq = sinusoidal_fm_iq(
        tone_frequency_hz=1000.0,
        deviation_hz=1500.0,
        sample_rate_hz=sample_rate,
        duration_s=0.2,
        frequency_offset_hz=3000.0,
    )
    frequency = quadrature_demod_frequency(iq, sample_rate)
    assert np.mean(frequency) == pytest.approx(3000.0, abs=2.0)
    assert np.max(frequency) == pytest.approx(4500.0, abs=2.0)


def test_four_fsk_symbol_indices_are_in_range() -> None:
    indices = fsk4_symbol_indices(10_000, seed=3)
    assert indices.shape == (10_000,)
    assert set(indices.tolist()) == {0, 1, 2, 3}


def test_rectangular_four_fsk_instantaneous_frequency_levels() -> None:
    symbols = np.tile(np.arange(4, dtype=np.int8), 200)
    trajectory = fsk4_frequency_trajectory(
        symbols,
        symbol_rate_hz=4800.0,
        samples_per_symbol=20,
        outer_deviation_hz=2400.0,
        pulse_shape="rectangular",
    )
    assert set(np.unique(trajectory).tolist()) == {-2400.0, -800.0, 800.0, 2400.0}


def test_raised_cosine_trajectory_hits_symbol_levels_at_symbol_centres() -> None:
    symbols = np.tile(np.arange(4, dtype=np.int8), 100)
    samples_per_symbol = 16
    span_symbols = 8
    trajectory = fsk4_frequency_trajectory(
        symbols,
        symbol_rate_hz=4800.0,
        samples_per_symbol=samples_per_symbol,
        outer_deviation_hz=2400.0,
        pulse_shape="raised_cosine",
        rolloff=0.5,
        span_symbols=span_symbols,
    )
    expected = np.array([-2400.0, -800.0, 800.0, 2400.0])
    centre_indices = (
        np.arange(span_symbols, symbols.size - span_symbols) * samples_per_symbol
    )
    actual = trajectory[centre_indices]
    expected_tiled = expected[
        symbols[span_symbols:-span_symbols].astype(np.intp)
    ]
    assert actual == pytest.approx(expected_tiled, abs=1.0)


def test_four_fsk_iq_has_expected_frequency_levels_without_noise() -> None:
    sample_rate = 192_000.0
    iq = fsk4_iq(
        symbol_rate_hz=4800.0,
        outer_deviation_hz=2400.0,
        sample_rate_hz=sample_rate,
        symbol_count=500,
        pulse_shape="rectangular",
        seed=2,
    )
    frequency = quadrature_demod_frequency(iq, sample_rate)
    inner = frequency[1000:-1000]
    levels = np.unique(np.round(inner / 100.0) * 100.0)
    assert set(levels.tolist()) == {-2400.0, -800.0, 800.0, 2400.0}


def test_fsk_iq_requires_integer_samples_per_symbol() -> None:
    with pytest.raises(SimulationError):
        fsk4_iq(
            symbol_rate_hz=4800.0,
            outer_deviation_hz=2400.0,
            sample_rate_hz=100_000.0,
            symbol_count=100,
        )


@pytest.mark.parametrize("bad_offset", [math.inf, math.nan])
def test_fm_offset_must_be_finite(bad_offset: float) -> None:
    with pytest.raises(SimulationError):
        sinusoidal_fm_iq(1000.0, 2400.0, 48_000.0, 0.1, frequency_offset_hz=bad_offset)


def test_unmodulated_carrier_rejects_aliasing() -> None:
    with pytest.raises(SimulationError, match="Nyquist"):
        unmodulated_carrier_iq(48_000.0, 0.1, frequency_offset_hz=25_000.0)


def test_short_raised_cosine_trajectory_keeps_symbol_center_alignment() -> None:
    trajectory = fsk4_frequency_trajectory(
        [3],
        symbol_rate_hz=4800.0,
        samples_per_symbol=16,
        outer_deviation_hz=2400.0,
        pulse_shape="raised_cosine",
    )
    assert trajectory.size == 16
    assert trajectory[0] == pytest.approx(2400.0)


def test_simulator_rejects_excessive_sample_allocation() -> None:
    with pytest.raises(SimulationError, match="limit"):
        sinusoidal_fm_iq(1000.0, 2400.0, 192_000.0, 1000.0)


def test_simulator_rejects_non_finite_dc_offset() -> None:
    with pytest.raises(SimulationError):
        unmodulated_carrier_iq(48_000.0, 0.1, dc_offset=complex(math.nan, 0.0))


def test_sinusoidal_fm_rejects_aliased_modulation_tone() -> None:
    with pytest.raises(SimulationError, match="tone_frequency_hz"):
        sinusoidal_fm_iq(48_000.0, 2400.0, 48_000.0, 0.1)


def test_four_fsk_rejects_non_integer_symbol_indices() -> None:
    with pytest.raises(SimulationError, match="integers"):
        fsk4_frequency_trajectory([0.9], 4800.0, 16, 2400.0)


def test_raised_cosine_rejects_excessive_filter_length() -> None:
    with pytest.raises(SimulationError, match="filter"):
        fsk4_frequency_trajectory(
            [0],
            4800.0,
            16,
            2400.0,
            span_symbols=100_000,
        )
