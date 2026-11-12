import pytest

from deviation_station.reference import (
    GENERIC_FM_REFERENCE,
    MMDVM_DMR_REFERENCE,
    MODULE17_M17_REFERENCE,
    PROFILE_REFERENCES,
    bessel_reference,
    reference_for_profile,
)


def test_digital_reference_values_match_profiles() -> None:
    assert MMDVM_DMR_REFERENCE.nominal_hz == 1944.0
    assert MMDVM_DMR_REFERENCE.inner_hz == 648.0
    assert MODULE17_M17_REFERENCE.nominal_hz == 2400.0
    assert MODULE17_M17_REFERENCE.inner_hz == 800.0
    assert MMDVM_DMR_REFERENCE.meter_marks == ((1944.0, "1.944k"),)
    assert MODULE17_M17_REFERENCE.meter_marks == ((2400.0, "2.4k"),)


def test_fm_reference_has_narrow_and_wide_marks() -> None:
    assert GENERIC_FM_REFERENCE.nominal_hz is None
    assert GENERIC_FM_REFERENCE.meter_marks == (
        (2500.0, "NB 2.5"),
        (5000.0, "WB 5.0"),
    )
    assert "2.5 kHz" in GENERIC_FM_REFERENCE.summary
    assert "5.0 kHz" in GENERIC_FM_REFERENCE.summary


def test_reference_lookup_is_profile_keyed() -> None:
    assert reference_for_profile("mmdvm-dmr") is MMDVM_DMR_REFERENCE
    assert reference_for_profile("module17-m17") is MODULE17_M17_REFERENCE
    assert reference_for_profile("generic-fm") is GENERIC_FM_REFERENCE
    assert reference_for_profile("unknown") is None
    assert set(PROFILE_REFERENCES) == {
        "mmdvm-dmr",
        "module17-m17",
        "generic-fm",
    }


def test_bessel_reference_uses_selected_tone_and_null() -> None:
    first = bessel_reference(1, 1000.0)
    second = bessel_reference(2, 1200.0)
    assert first.nominal_hz == pytest.approx(2404.825557695773)
    assert second.nominal_hz == pytest.approx(5520.078110286311 * 1.2)
    assert first.meter_marks[0][1] == "J0"
    assert first.meter_marks[0][0] == pytest.approx(2404.825557695773)
    assert "2.405 kHz" in first.summary
    assert "1.200 kHz tone" in second.summary
