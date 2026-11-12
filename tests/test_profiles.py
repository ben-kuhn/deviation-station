import numpy as np
import pytest

from deviation_station.discriminator import DiscriminatorError, quadrature_demod_frequency
from deviation_station.measurement import MeasurementEngine, ReadingStatus
from deviation_station.profiles import (
    GENERIC_FM_PROFILE,
    GenericFmProfile,
    MODULE17_M17_PROFILE,
    MMDVM_DMR_PROFILE,
)
from deviation_station.simulator import fsk4_iq, sinusoidal_fm_iq


@pytest.mark.parametrize(
    ("profile", "expected_hz"),
    [
        (MODULE17_M17_PROFILE, 2400.0),
        (MMDVM_DMR_PROFILE, 1944.0),
    ],
)
def test_digital_profile_measures_pulse_shaped_4fsk(profile, expected_hz: float) -> None:
    sample_rate = 192_000.0
    iq = fsk4_iq(
        symbol_rate_hz=profile.symbol_rate_hz,
        outer_deviation_hz=expected_hz,
        sample_rate_hz=sample_rate,
        symbol_count=2000,
        pulse_shape="raised_cosine",
        seed=11,
    )
    frequency = quadrature_demod_frequency(iq, sample_rate)
    reading = profile.measure(frequency[1000:-1000], sample_rate)

    assert reading.valid
    assert reading.engine is MeasurementEngine.DISCRIMINATOR
    assert reading.deviation_hz == pytest.approx(expected_hz, rel=0.001)
    assert reading.quality.status is ReadingStatus.GOOD
    assert reading.definition == "4FSK outer-symbol deviation"
    assert reading.metadata["capture_symbol_count"] >= 96
    assert reading.metadata["timing_offset_samples"] is not None
    assert reading.metadata["expected_outer_deviation_hz"] == expected_hz


def test_digital_profile_has_useful_accuracy_with_noise() -> None:
    sample_rate = 192_000.0
    profile = MODULE17_M17_PROFILE
    iq = fsk4_iq(
        symbol_rate_hz=profile.symbol_rate_hz,
        outer_deviation_hz=profile.expected_outer_deviation_hz,
        sample_rate_hz=sample_rate,
        symbol_count=2000,
        pulse_shape="raised_cosine",
        snr_db=40.0,
        seed=17,
    )
    frequency = quadrature_demod_frequency(iq, sample_rate)
    reading = profile.measure(frequency[1000:-1000], sample_rate)
    assert reading.valid
    assert reading.deviation_hz == pytest.approx(2400.0, rel=0.05)


def test_digital_profile_rejects_capture_without_enough_symbols() -> None:
    with pytest.raises(DiscriminatorError, match="96 symbols"):
        MODULE17_M17_PROFILE.measure(np.linspace(-1000.0, 1000.0, 64), 192_000.0)


def test_digital_profile_removes_carrier_offset_from_deviation() -> None:
    sample_rate = 48_000.0
    profile = MODULE17_M17_PROFILE
    iq = fsk4_iq(
        symbol_rate_hz=profile.symbol_rate_hz,
        outer_deviation_hz=profile.expected_outer_deviation_hz,
        sample_rate_hz=sample_rate,
        symbol_count=1000,
        pulse_shape="raised_cosine",
        frequency_offset_hz=137.0,
        snr_db=45.0,
        seed=7,
    )
    frequency = quadrature_demod_frequency(iq, sample_rate)
    reading = profile.measure(frequency[200:-200], sample_rate)
    assert reading.valid
    assert reading.deviation_hz == pytest.approx(2400.0, rel=0.01)
    assert reading.metadata["symbol_levels_hz"][0] > -2400.0


def test_generic_profile_filters_noisy_sinusoidal_fm() -> None:
    sample_rate = 192_000.0
    iq = sinusoidal_fm_iq(
        tone_frequency_hz=1000.0,
        deviation_hz=2400.0,
        sample_rate_hz=sample_rate,
        duration_s=0.25,
        snr_db=35.0,
        seed=17,
    )
    frequency = quadrature_demod_frequency(iq, sample_rate)
    reading = GENERIC_FM_PROFILE.measure(frequency, sample_rate)

    assert reading.valid
    assert reading.deviation_hz == pytest.approx(2400.0, rel=0.02)
    assert reading.quality.status is ReadingStatus.MARGINAL
    assert reading.metadata["raw_deviation_hz"] > reading.deviation_hz
    assert reading.metadata["smoothing_window_samples"] > 0


def test_generic_profile_measures_clean_sinusoidal_fm() -> None:
    sample_rate = 192_000.0
    iq = sinusoidal_fm_iq(
        tone_frequency_hz=1000.0,
        deviation_hz=1500.0,
        sample_rate_hz=sample_rate,
        duration_s=0.2,
    )
    frequency = quadrature_demod_frequency(iq, sample_rate)
    reading = GENERIC_FM_PROFILE.measure(frequency, sample_rate)
    assert reading.deviation_hz == pytest.approx(1500.0, rel=0.02)
    assert reading.profile_id == "generic-fm"


def test_generic_profile_without_sample_rate_has_unknown_quality() -> None:
    reading = GENERIC_FM_PROFILE.measure(np.linspace(-1000.0, 1000.0, 1000))
    assert not reading.valid
    assert reading.quality.status is ReadingStatus.UNKNOWN


def test_digital_profile_rejects_noise_only_capture() -> None:
    sample_rate = 192_000.0
    rng = np.random.default_rng(5)
    iq = rng.normal(size=40_001) + 1j * rng.normal(size=40_001)
    frequency = quadrature_demod_frequency(iq, sample_rate)
    reading = MODULE17_M17_PROFILE.measure(frequency, sample_rate)
    assert not reading.valid
    assert reading.deviation_hz is None
    assert reading.quality.status is ReadingStatus.INVALID


def test_generic_profile_accepts_zero_deviation_carrier() -> None:
    reading = GENERIC_FM_PROFILE.measure(np.zeros(20_000), 192_000.0)
    assert reading.valid
    assert reading.deviation_hz == 0.0
    assert reading.quality.status is ReadingStatus.GOOD


def test_generic_profile_rejects_filter_spread_clicks() -> None:
    frequency = np.zeros(20_000)
    frequency[::2000] = 10_000.0
    reading = GENERIC_FM_PROFILE.measure(frequency, 192_000.0)
    assert not reading.valid
    assert reading.deviation_hz is None
    assert reading.quality.status is ReadingStatus.INVALID


def test_generic_profile_rejects_flat_channel_spectrum() -> None:
    reading = GENERIC_FM_PROFILE.measure(
        np.zeros(20_000),
        192_000.0,
        carrier_flatness=0.6,
    )
    assert not reading.valid
    assert reading.deviation_hz is None
    assert reading.quality.status is ReadingStatus.INVALID
    assert "no FM carrier" in " ".join(reading.quality.issues)


def test_generic_profile_supports_disabling_smoothing() -> None:
    profile = GenericFmProfile(smoothing_bandwidth_hz=None)
    reading = profile.measure(np.zeros(20_000), 192_000.0)
    assert reading.valid
    assert reading.deviation_hz == 0.0
    assert reading.quality.status is ReadingStatus.MARGINAL
