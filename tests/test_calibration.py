import json
from pathlib import Path

import pytest

from deviation_meter.calibration import (
    BesselVerificationRecord,
    CalibrationError,
    TargetDeviationRecord,
    save_bessel_verification,
    save_target_deviation,
)
from deviation_meter.measurement import (
    DeviationReading,
    MeasurementEngine,
    MeasurementQuality,
    ReadingStatus,
)


def _reading(*, valid: bool = True) -> DeviationReading:
    return DeviationReading(
        deviation_hz=2404.8256,
        engine=MeasurementEngine.BESSEL,
        profile_id="bessel-j0-1",
        definition="J0 carrier null 1",
        quality=MeasurementQuality(
            status=ReadingStatus.GOOD if valid else ReadingStatus.INVALID,
            carrier_locked=True,
            sample_count=40,
        ),
        metadata={"tone_frequency_hz": 1000.0, "null_depth_db": 35.0},
    )


def test_save_bessel_verification_record(tmp_path: Path) -> None:
    record = BesselVerificationRecord(
        receive_frequency_hz=443_457_000.0,
        tone_frequency_hz=1000.0,
        null_index=1,
        reading=_reading(),
        device={"device_args": "driver=rtlsdr", "sample_rate_hz": 240000.0},
        audio_output="Digirig",
        notes="bench verification",
    )
    path = save_bessel_verification(record, directory=tmp_path)
    payload = json.loads(path.read_text())
    assert payload["application"]["name"] == "deviation-station"
    assert payload["kind"] == "bessel-null-verification"
    assert payload["verified_deviation_hz"] == pytest.approx(2404.8256)
    assert payload["device"]["device_args"] == "driver=rtlsdr"
    assert payload["audio_output"] == "Digirig"
    assert payload["reading"]["engine"] == "bessel"
    markdown = path.with_suffix(".md").read_text()
    assert "# Bessel null verification" in markdown
    assert "Verified deviation" in markdown
    assert "Null depth" in markdown


def test_save_target_deviation_record(tmp_path: Path) -> None:
    reading = DeviationReading(
        deviation_hz=2004.0,
        engine=MeasurementEngine.DISCRIMINATOR,
        profile_id="generic-fm",
        definition="robust instantaneous-frequency peak",
        quality=MeasurementQuality(status=ReadingStatus.GOOD, sample_count=400),
        metadata={"profile_label": "Generic FM"},
    )
    record = TargetDeviationRecord(
        receive_frequency_hz=443_457_000.0,
        target_deviation_hz=2000.0,
        reading=reading,
        device={"device_args": "driver=rtlsdr", "sample_rate_hz": 240000.0},
        source="internal",
        audio_output="Digirig",
        notes="closed-loop target",
    )
    path = save_target_deviation(record, directory=tmp_path)
    payload = json.loads(path.read_text())
    assert payload["kind"] == "discriminator-target-calibration"
    assert payload["target_deviation_hz"] == 2000.0
    assert payload["achieved_deviation_hz"] == 2004.0
    assert payload["error_hz"] == 4.0
    assert payload["source"] == "internal"
    assert payload["reading"]["engine"] == "discriminator"
    markdown = path.with_suffix(".md").read_text()
    assert "# Discriminator target calibration" in markdown
    assert "Target deviation" in markdown
    assert "Achieved deviation" in markdown


def test_target_record_rejects_bessel_reading() -> None:
    with pytest.raises(CalibrationError):
        TargetDeviationRecord(
            receive_frequency_hz=443_000_000.0,
            target_deviation_hz=2000.0,
            reading=_reading(),
            device={},
        )


def test_verification_record_rejects_non_bessel_reading() -> None:
    reading = DeviationReading(
        deviation_hz=2400.0,
        engine=MeasurementEngine.DISCRIMINATOR,
        profile_id="module17-m17",
        definition="4FSK outer-symbol deviation",
        quality=MeasurementQuality(status=ReadingStatus.GOOD),
    )
    with pytest.raises(CalibrationError):
        BesselVerificationRecord(
            receive_frequency_hz=443_000_000.0,
            tone_frequency_hz=1000.0,
            null_index=1,
            reading=reading,
            device={},
        )


def test_verification_record_rejects_invalid_reading() -> None:
    with pytest.raises(CalibrationError):
        BesselVerificationRecord(
            receive_frequency_hz=443_000_000.0,
            tone_frequency_hz=1000.0,
            null_index=1,
            reading=_reading(valid=False),
            device={},
        )
