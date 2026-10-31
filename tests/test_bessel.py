import math

import numpy as np
import pytest

from deviation_meter.bessel import (
    BESSEL_J0_ZEROS,
    BesselNullDetector,
    BesselNullError,
    carrier_null_index,
    carrier_null_tone_frequency,
    deviation_from_carrier_null,
)
from deviation_meter.measurement import MeasurementEngine
from deviation_meter.simulator import sinusoidal_fm_iq


def _normalized_carrier_amplitude(iq: np.ndarray) -> float:
    window = np.hanning(iq.size)
    spectrum = np.fft.fft(iq * window)
    return float(abs(spectrum[0]) / np.sum(window))


def test_known_first_j0_zero() -> None:
    assert carrier_null_index(1) == pytest.approx(2.404825557695773, rel=0, abs=1e-15)


def test_known_carrier_null_deviations_for_one_khz_tone() -> None:
    assert deviation_from_carrier_null(1000.0, 1) == pytest.approx(2404.825557695773)
    assert deviation_from_carrier_null(1000.0, 2) == pytest.approx(5520.078110286311)


def test_tone_frequency_for_target_deviation() -> None:
    tone = carrier_null_tone_frequency(3000.0, 1)
    assert tone == pytest.approx(3000.0 / BESSEL_J0_ZEROS[0])
    assert deviation_from_carrier_null(tone, 1) == pytest.approx(3000.0)


def test_detector_capture_threshold_can_be_changed() -> None:
    detector = BesselNullDetector(tone_frequency_hz=1000.0, null_index=1)
    detector.set_min_null_depth_db(40.0)
    assert detector.min_null_depth_db == 40.0
    with pytest.raises(BesselNullError):
        detector.set_min_null_depth_db(detector.rearm_db)


def test_null_index_is_one_based_and_validated() -> None:
    with pytest.raises(BesselNullError):
        carrier_null_index(0)
    with pytest.raises(BesselNullError):
        carrier_null_index(True)  # type: ignore[arg-type]
    with pytest.raises(BesselNullError):
        carrier_null_index(len(BESSEL_J0_ZEROS) + 1)


@pytest.mark.parametrize("bad_tone", [0.0, -1.0, math.inf, math.nan])
def test_tone_frequency_must_be_positive_and_finite(bad_tone: float) -> None:
    with pytest.raises(BesselNullError):
        deviation_from_carrier_null(bad_tone, 1)


@pytest.mark.parametrize("bad_target", [0.0, -1.0, math.inf, math.nan])
def test_target_deviation_must_be_positive_and_finite(bad_target: float) -> None:
    with pytest.raises(BesselNullError):
        carrier_null_tone_frequency(bad_target, 1)


@pytest.mark.parametrize("null_index", [1, 2])
def test_simulated_fm_has_actual_spectral_carrier_null(null_index: int) -> None:
    tone_hz = 1000.0
    null_deviation = deviation_from_carrier_null(tone_hz, null_index)
    common = {
        "tone_frequency_hz": tone_hz,
        "sample_rate_hz": 48_000.0,
        "duration_s": 1.0,
    }
    at_null = sinusoidal_fm_iq(deviation_hz=null_deviation, **common)
    before_null = sinusoidal_fm_iq(deviation_hz=null_deviation - 100.0, **common)
    after_null = sinusoidal_fm_iq(deviation_hz=null_deviation + 100.0, **common)

    null_amplitude = _normalized_carrier_amplitude(at_null)
    before_amplitude = _normalized_carrier_amplitude(before_null)
    after_amplitude = _normalized_carrier_amplitude(after_null)

    assert null_amplitude < 0.005
    assert before_amplitude > 20.0 * null_amplitude
    assert after_amplitude > 20.0 * null_amplitude


def _carrier_spectrum(power_db: float, *, bin_index: int = 512) -> np.ndarray:
    spectrum = np.full(1_024, -100.0)
    spectrum[bin_index - 1 : bin_index + 2] = power_db
    return spectrum


def test_bessel_detector_locks_carrier() -> None:
    detector = BesselNullDetector(tone_frequency_hz=1000.0, null_index=1)
    spectrum = np.full(1_024, -100.0)
    spectrum[513] = -12.0
    progress = detector.lock_carrier(spectrum)
    assert progress.carrier_locked
    assert progress.carrier_offset_hz == pytest.approx(46.875)
    assert progress.baseline_db == pytest.approx(-12.0)


def test_bessel_detector_captures_first_carrier_null() -> None:
    detector = BesselNullDetector(tone_frequency_hz=1000.0, null_index=1)
    detector.lock_carrier(_carrier_spectrum(-10.0))
    captured = None
    for level in (-10, -25, -60, -90, -70, -40, -20, -10):
        captured = detector.update(_carrier_spectrum(level)).captured
    assert captured is not None
    assert captured.engine is MeasurementEngine.BESSEL
    assert captured.deviation_hz == pytest.approx(2404.8256, rel=1e-6)
    assert captured.definition == "J0 carrier null 1"


def test_bessel_detector_counts_to_second_null() -> None:
    detector = BesselNullDetector(tone_frequency_hz=1000.0, null_index=2)
    detector.lock_carrier(_carrier_spectrum(-10.0))
    captured = None
    sequence = (
        (-10, -30, -70, -90, -70, -40, -10)
        + (-10, -30, -70, -90, -70, -40, -10)
    )
    for level in sequence:
        captured = detector.update(_carrier_spectrum(level)).captured
    assert captured is not None
    assert captured.deviation_hz == pytest.approx(5520.0781, rel=1e-6)
    assert captured.definition == "J0 carrier null 2"


def test_bessel_detector_rejects_shallow_dip() -> None:
    detector = BesselNullDetector(
        tone_frequency_hz=1000.0,
        null_index=1,
        min_null_depth_db=20.0,
    )
    detector.lock_carrier(_carrier_spectrum(-10.0))
    for level in (-10, -15, -18, -15, -10):
        progress = detector.update(_carrier_spectrum(level))
    assert progress.captured is None
    assert progress.nulls_seen == 0


def test_bessel_detector_manual_capture_requires_deep_nulls() -> None:
    detector = BesselNullDetector(tone_frequency_hz=1000.0, null_index=1)
    detector.lock_carrier(_carrier_spectrum(-10.0))
    detector.update(_carrier_spectrum(-12.0))
    assert detector.capture_manual().captured is None
    detector.update(_carrier_spectrum(-70.0))
    captured = detector.capture_manual().captured
    assert captured is not None
    assert captured.deviation_hz == pytest.approx(2404.8256, rel=1e-6)
