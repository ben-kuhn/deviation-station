import numpy as np
import pytest

from deviation_meter.tone import ToneError, detect_tone_hz


def test_detect_tone_from_demodulated_sine() -> None:
    sample_rate = 48_000.0
    t = np.arange(int(sample_rate * 0.6)) / sample_rate
    samples = 500.0 * np.sin(2.0 * np.pi * 1_500.0 * t)
    estimate = detect_tone_hz(samples, sample_rate)
    assert estimate.frequency_hz == pytest.approx(1_500.0, abs=1.0)
    assert estimate.valid is True
    assert estimate.confidence_db > 20.0


def test_detect_tone_rejects_flat_audio() -> None:
    with pytest.raises(ToneError):
        detect_tone_hz(np.zeros(4_000), 48_000.0)


def test_detect_tone_rejects_short_audio() -> None:
    with pytest.raises(ToneError):
        detect_tone_hz(np.ones(100), 48_000.0)
