import math

import numpy as np
import pytest

from deviation_meter.discriminator import (
    DiscriminatorError,
    four_fsk_symbol_deviation,
    one_dimensional_kmeans,
    quadrature_demod_frequency,
    robust_peak_deviation,
)


def test_quadrature_demod_measures_constant_frequency() -> None:
    sample_rate = 48_000.0
    offset = 1250.0
    t = np.arange(20_000) / sample_rate
    iq = np.exp(2j * math.pi * offset * t)
    frequency = quadrature_demod_frequency(iq, sample_rate)
    assert np.mean(frequency) == pytest.approx(offset, abs=1e-6)
    assert frequency.size == iq.size - 1


def test_robust_peak_deviation_on_synthetic_tone() -> None:
    frequency = 2400.0 * np.cos(np.linspace(0.0, 2.0 * math.pi * 20.0, 20_000))
    result = robust_peak_deviation(frequency)
    assert result.deviation_hz == pytest.approx(2400.0, rel=0.005)
    assert result.center_hz == pytest.approx(0.0, abs=1.0)


def test_kmeans_finds_known_levels() -> None:
    levels = np.array([-2400.0, -800.0, 800.0, 2400.0])
    rng = np.random.default_rng(8)
    values = np.repeat(levels, 500) + rng.normal(0.0, 5.0, 2000)
    result = one_dimensional_kmeans(values, 4)
    assert result.centers_hz == pytest.approx(levels, abs=1.0)
    assert set(result.labels.tolist()) == {0, 1, 2, 3}
    assert result.rms_error_hz < 10.0
    assert result.converged


def test_four_fsk_levels_report_outer_deviation() -> None:
    levels = np.array([-1944.0, -648.0, 648.0, 1944.0])
    rng = np.random.default_rng(4)
    values = np.repeat(levels, 400) + rng.normal(0.0, 2.0, 1600)
    result = four_fsk_symbol_deviation(values)
    assert result.deviation_hz == pytest.approx(1944.0, abs=2.0)
    assert result.center_hz == pytest.approx(0.0, abs=2.0)
    assert result.level_spacing_hz == pytest.approx(1296.0, abs=3.0)
    assert result.definition == "4FSK outer-symbol deviation"


def test_four_fsk_rejects_non_equally_spaced_levels() -> None:
    levels = np.array([-2400.0, -2000.0, 100.0, 2400.0])
    values = np.repeat(levels, 400)
    with pytest.raises(DiscriminatorError):
        four_fsk_symbol_deviation(values)


def test_four_fsk_rejects_insufficient_samples() -> None:
    with pytest.raises(DiscriminatorError):
        four_fsk_symbol_deviation(np.linspace(-1000.0, 1000.0, 10))


def test_kmeans_reports_non_convergence() -> None:
    rng = np.random.default_rng(99)
    values = rng.normal(0.0, 1000.0, 1000)
    result = one_dimensional_kmeans(
        values,
        4,
        max_iterations=1,
        tolerance_hz=1e-12,
    )
    assert result.iterations == 1
    assert not result.converged


def test_kmeans_bounds_input_samples() -> None:
    values = np.tile(np.array([-2400.0, -800.0, 800.0, 2400.0]), 1000)
    result = one_dimensional_kmeans(
        values,
        4,
        trim_fraction=0.0,
        max_samples=128,
    )
    assert result.labels.size == 128


def test_non_finite_frequency_samples_are_rejected() -> None:
    values = np.linspace(-1000.0, 1000.0, 100)
    values[10] = np.nan
    with pytest.raises(DiscriminatorError):
        four_fsk_symbol_deviation(values)


def test_kmeans_rejects_excessive_cluster_count() -> None:
    with pytest.raises(DiscriminatorError, match="cannot exceed"):
        one_dimensional_kmeans(np.linspace(-1000.0, 1000.0, 1000), 100)


def test_quadrature_demod_gate_preserves_sample_positions() -> None:
    iq = np.ones(100, dtype=np.complex128)
    iq[:20] = 0.0
    frequency = quadrature_demod_frequency(iq, 48_000.0, min_magnitude=0.5)
    assert frequency.size == 99
    assert np.isnan(frequency[:20]).all()
    assert frequency[20:] == pytest.approx(np.zeros(79))
    with pytest.raises(DiscriminatorError, match="non-finite"):
        four_fsk_symbol_deviation(frequency)
