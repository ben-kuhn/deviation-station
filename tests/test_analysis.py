import json
from pathlib import Path

import numpy as np
import pytest

from deviation_meter.analysis import (
    AnalysisError,
    analyze_capture,
    channel_spectral_flatness,
    load_capture,
    measure_channelized_frequency,
    write_analysis_report,
)
from deviation_meter.discriminator import quadrature_demod_frequency
from deviation_meter.profiles import GENERIC_FM_PROFILE
from deviation_meter.simulator import fsk4_iq


def _write_fixture(
    path: Path,
    samples: np.ndarray,
    *,
    sample_rate_hz: float,
    center_frequency_hz: float,
    signal_frequency_hz: float,
) -> None:
    samples.astype("<c8", copy=False).tofile(path)
    metadata = {
        "format": {
            "name": "cf32",
            "dtype": "complex64",
            "byte_order": "little",
            "header_bytes": 0,
        },
        "requested": {
            "center_frequency_hz": center_frequency_hz,
            "signal_frequency_hz": signal_frequency_hz,
            "sample_rate_hz": sample_rate_hz,
        },
        "device": {
            "actual_center_frequency_hz": center_frequency_hz,
            "actual_sample_rate_hz": sample_rate_hz,
        },
        "result": {
            "captured_samples": len(samples),
            "sample_rate_hz": sample_rate_hz,
            "completed": True,
            "clipped_fraction": 0.0,
            "overflow_count": 0,
        },
        "user_metadata": {"mode": "M17"},
    }
    Path(str(path) + ".json").write_text(json.dumps(metadata, indent=2) + "\n")


def _noise(sample_count: int, seed: int, amplitude: float = 0.002) -> np.ndarray:
    rng = np.random.default_rng(seed)
    return amplitude * (
        rng.standard_normal(sample_count) + 1j * rng.standard_normal(sample_count)
    )


def _m17_fixture(path: Path) -> None:
    rate = 240_000.0
    center = 443_507_000.0
    signal = 443_457_000.0
    lead = _noise(int(0.3 * rate), 11)
    payload = fsk4_iq(
        symbol_rate_hz=4800.0,
        outer_deviation_hz=2400.0,
        sample_rate_hz=rate,
        symbol_count=4_800,
        pulse_shape="raised_cosine",
        frequency_offset_hz=-50_000.0,
        seed=12,
    )
    trail = _noise(int(0.3 * rate), 13)
    samples = np.concatenate([lead, payload, trail]).astype(np.complex64)
    _write_fixture(
        path,
        samples,
        sample_rate_hz=rate,
        center_frequency_hz=center,
        signal_frequency_hz=signal,
    )


def _dmr_fixture(path: Path) -> None:
    rate = 240_000.0
    center = 443_507_000.0
    signal = 443_457_000.0
    rng = np.random.default_rng(42)
    samples = _noise(int(1.2 * rate), 42, amplitude=0.002)
    for index in range(6):
        burst = fsk4_iq(
            symbol_rate_hz=4800.0,
            outer_deviation_hz=1944.0,
            sample_rate_hz=rate,
            symbol_count=144,
            pulse_shape="raised_cosine",
            frequency_offset_hz=-50_000.0,
            seed=100 + index,
        )
        start = int((0.10 + index * 0.16) * rate)
        samples[start : start + burst.size] += burst
    _write_fixture(
        path,
        samples.astype(np.complex64),
        sample_rate_hz=rate,
        center_frequency_hz=center,
        signal_frequency_hz=signal,
    )


def test_analyze_m17_synthetic_capture(tmp_path: Path) -> None:
    capture_path = tmp_path / "m17.cf32"
    _m17_fixture(capture_path)

    result = analyze_capture(capture_path, profile_name="m17")

    assert len(result.segments) == 1
    assert result.selected is not None
    assert result.selected.deviation_hz == pytest.approx(2400.0, rel=0.03)
    assert result.selected.quality.status.value in {"good", "marginal"}
    assert result.channel.shift_hz == pytest.approx(-50_000.0)
    report = result.as_dict()
    assert report["capture"]["warnings"] == []
    assert report["analysis"]["profile_id"] == "module17-m17"
    assert report["selected_reading"]["deviation_hz"] == pytest.approx(
        result.selected.deviation_hz
    )

    report_path = tmp_path / "report.json"
    write_analysis_report(result, report_path)
    assert json.loads(report_path.read_text())["analysis"]["profile_id"] == "module17-m17"


def test_analyze_dmr_burst_capture(tmp_path: Path) -> None:
    capture_path = tmp_path / "dmr.cf32"
    _dmr_fixture(capture_path)

    result = analyze_capture(capture_path, profile_name="dmr")

    assert len(result.segments) >= 5
    assert result.selected is not None
    assert result.selected.deviation_hz == pytest.approx(1944.0, rel=0.05)
    assert all(item.segment.duration_s >= 0.02 for item in result.segments)

    summary = result.summary()
    assert summary is not None
    assert summary["total_segments"] >= 5
    assert summary["valid_segments"] == summary["total_segments"]
    assert summary["median_deviation_hz"] == pytest.approx(1944.0, rel=0.05)
    assert summary["maximum_deviation_hz"] >= summary["minimum_deviation_hz"]


def test_analyze_noise_only_capture_has_no_segments(tmp_path: Path) -> None:
    path = tmp_path / "noise.cf32"
    _write_fixture(
        path,
        _noise(240_000, 21).astype(np.complex64),
        sample_rate_hz=240_000.0,
        center_frequency_hz=443_507_000.0,
        signal_frequency_hz=443_457_000.0,
    )
    result = analyze_capture(path, profile_name="m17")
    assert result.segments == ()
    assert result.selected is None


def test_load_capture_rejects_truncated_file(tmp_path: Path) -> None:
    path = tmp_path / "broken.cf32"
    _m17_fixture(path)
    path.write_bytes(path.read_bytes()[:-8])
    with pytest.raises(AnalysisError, match="size mismatch"):
        load_capture(path)


def test_load_capture_requires_signal_frequency(tmp_path: Path) -> None:
    path = tmp_path / "nosignal.cf32"
    _m17_fixture(path)
    metadata_path = Path(str(path) + ".json")
    metadata = json.loads(metadata_path.read_text())
    metadata["requested"]["signal_frequency_hz"] = None
    metadata_path.write_text(json.dumps(metadata))
    with pytest.raises(AnalysisError, match="signal frequency"):
        load_capture(path)


def test_analyze_rejects_unknown_profile(tmp_path: Path) -> None:
    path = tmp_path / "m17.cf32"
    _m17_fixture(path)
    with pytest.raises(AnalysisError, match="unknown profile"):
        analyze_capture(path, profile_name="c4fm")


def test_generic_live_profile_rejects_noise_only_window() -> None:
    sample_rate = 48_000.0
    rng = np.random.default_rng(12)
    iq = (
        rng.standard_normal(24_000) + 1j * rng.standard_normal(24_000)
    ).astype(np.complex64)
    demod = quadrature_demod_frequency(iq, sample_rate)
    frequency = np.concatenate(([demod[0]], demod))

    assert channel_spectral_flatness(iq) > 0.5
    analysis = measure_channelized_frequency(
        GENERIC_FM_PROFILE,
        iq,
        frequency,
        sample_rate,
        assume_continuous_if_flat=True,
    )
    assert analysis.selected is None


def test_generic_live_profile_accepts_fm_tone_window() -> None:
    sample_rate = 48_000.0
    samples = np.arange(24_000) / sample_rate
    phase = (
        2.0 * np.pi * 1_000.0 * samples
        + (2_500.0 / 1_500.0) * np.sin(2.0 * np.pi * 1_500.0 * samples)
    )
    iq = np.exp(1j * phase).astype(np.complex64)
    demod = quadrature_demod_frequency(iq, sample_rate)
    frequency = np.concatenate(([demod[0]], demod))

    assert channel_spectral_flatness(iq) < 0.1
    analysis = measure_channelized_frequency(
        GENERIC_FM_PROFILE,
        iq,
        frequency,
        sample_rate,
        assume_continuous_if_flat=True,
    )
    reading = analysis.selected
    assert reading is not None
    assert reading.valid
    assert reading.deviation_hz == pytest.approx(2_400.0, rel=0.05)
