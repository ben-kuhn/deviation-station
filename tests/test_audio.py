import numpy as np
import pytest

from deviation_meter.audio import AudioToneError, generate_tone_pcm16


def test_generate_tone_pcm16() -> None:
    data = generate_tone_pcm16(1000.0, 0.25, sample_rate_hz=48_000, level=0.25)
    samples = np.frombuffer(data, dtype="<i2")
    assert samples.size == 12_000
    assert samples[0] == 0
    assert samples[-1] == 0
    assert np.max(np.abs(samples.astype(np.int32))) < 8_192
    rms = np.sqrt(np.mean(samples.astype(np.float64) ** 2))
    assert rms == pytest.approx(0.25 * 32767.0 / np.sqrt(2.0), rel=0.02)


def test_tone_rejects_invalid_frequency() -> None:
    with pytest.raises(AudioToneError):
        generate_tone_pcm16(0.0, 1.0)
    with pytest.raises(AudioToneError):
        generate_tone_pcm16(24_000.0, 1.0, sample_rate_hz=48_000)
