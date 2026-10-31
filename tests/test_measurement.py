import json

import pytest

from deviation_meter.measurement import (
    DeviationReading,
    MeasurementEngine,
    MeasurementQuality,
    ReadingStatus,
)


def test_reading_serialization_contains_engine_and_definition() -> None:
    reading = DeviationReading(
        deviation_hz=2404.8,
        engine=MeasurementEngine.BESSEL,
        profile_id="bessel-j0-1",
        definition="first carrier null",
        quality=MeasurementQuality(
            status=ReadingStatus.GOOD,
            snr_db=40.0,
            sample_count=4096,
        ),
        timestamp_s=123.5,
        metadata={"tone_hz": 1000.0},
    )

    payload = json.loads(json.dumps(reading.as_dict()))
    assert payload["engine"] == "bessel"
    assert payload["definition"] == "first carrier null"
    assert payload["valid"] is True
    assert payload["metadata"]["tone_hz"] == 1000.0
    assert payload["quality"]["status"] == "good"


def test_invalid_reading_is_not_valid() -> None:
    reading = DeviationReading(
        deviation_hz=1000.0,
        engine=MeasurementEngine.DISCRIMINATOR,
        profile_id="generic-fm",
        definition="robust peak",
        quality=MeasurementQuality(status=ReadingStatus.INVALID),
    )
    assert reading.valid is False


def test_missing_deviation_is_not_valid() -> None:
    reading = DeviationReading(
        deviation_hz=None,
        engine=MeasurementEngine.BESSEL,
        profile_id="bessel-j0-1",
        definition="first carrier null",
    )
    assert reading.valid is False


def test_default_quality_does_not_make_a_reading_valid() -> None:
    reading = DeviationReading(
        deviation_hz=2400.0,
        engine=MeasurementEngine.DISCRIMINATOR,
        profile_id="generic-fm",
        definition="robust peak",
    )
    assert reading.quality.status is ReadingStatus.UNKNOWN
    assert not reading.valid


def test_overload_cannot_be_good_quality() -> None:
    with pytest.raises(ValueError):
        MeasurementQuality(status=ReadingStatus.GOOD, overload=True)


def test_negative_deviation_is_rejected() -> None:
    with pytest.raises(ValueError):
        DeviationReading(
            deviation_hz=-1.0,
            engine=MeasurementEngine.DISCRIMINATOR,
            profile_id="generic-fm",
            definition="robust peak",
        )
