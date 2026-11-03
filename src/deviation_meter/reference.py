"""Nominal deviation references shown in the UI.

These are guidance values, not pass/fail limits. Digital references are the
normal outer-symbol deviations for the selected 4FSK profile; analog FM values
are common narrowband/wideband speech references.
"""

from __future__ import annotations

from dataclasses import dataclass

from .bessel import carrier_null_index
from .profiles import (
    GENERIC_FM_PROFILE,
    MMDVM_DMR_PROFILE,
    MODULE17_M17_PROFILE,
)


@dataclass(frozen=True, slots=True)
class DeviationReference:
    profile_id: str
    label: str
    nominal_hz: float | None
    inner_hz: float | None = None
    symbol_rate_hz: float | None = None
    meter_marks: tuple[tuple[float, str], ...] = ()
    summary: str = ""
    short: str = ""
    detail: str = ""

    @property
    def tooltip(self) -> str:
        return self.detail or self.summary


MMDVM_DMR_REFERENCE = DeviationReference(
    profile_id=MMDVM_DMR_PROFILE.profile_id,
    label="MMDVM / DMR",
    nominal_hz=MMDVM_DMR_PROFILE.expected_outer_deviation_hz,
    inner_hz=MMDVM_DMR_PROFILE.expected_inner_deviation_hz,
    symbol_rate_hz=MMDVM_DMR_PROFILE.symbol_rate_hz,
    meter_marks=((MMDVM_DMR_PROFILE.expected_outer_deviation_hz, "1.944k"),),
    summary="1.944 kHz outer · 0.648 kHz inner",
    short="1.944 kHz outer",
    detail=(
        "ETSI DMR 4FSK at 4800 symbols/s. The nominal outer-symbol deviation "
        "is 1.944 kHz (inner 0.648 kHz). The meter shows outer-symbol "
        "deviation, not an arbitrary instantaneous peak."
    ),
)

MODULE17_M17_REFERENCE = DeviationReference(
    profile_id=MODULE17_M17_PROFILE.profile_id,
    label="Module17 / M17",
    nominal_hz=MODULE17_M17_PROFILE.expected_outer_deviation_hz,
    inner_hz=MODULE17_M17_PROFILE.expected_inner_deviation_hz,
    symbol_rate_hz=MODULE17_M17_PROFILE.symbol_rate_hz,
    meter_marks=((MODULE17_M17_PROFILE.expected_outer_deviation_hz, "2.4k"),),
    summary="2.400 kHz outer · 0.800 kHz inner",
    short="2.400 kHz outer",
    detail=(
        "M17 4FSK at 4800 symbols/s. The nominal outer-symbol deviation is "
        "2.400 kHz (inner 0.800 kHz). The meter shows outer-symbol deviation."
    ),
)

GENERIC_FM_REFERENCE = DeviationReference(
    profile_id=GENERIC_FM_PROFILE.profile_id,
    label="Generic FM",
    nominal_hz=None,
    meter_marks=((2500.0, "NB 2.5"), (5000.0, "WB 5.0")),
    summary="±2.5 kHz narrowband · ±5.0 kHz wideband",
    short="±2.5 kHz NB / ±5.0 kHz WB",
    detail=(
        "Typical amateur FM peak deviation: about ±2.5 kHz narrowband and "
        "±5.0 kHz wideband. Speech peaks vary with audio and radio settings, "
        "so these are reference values rather than pass/fail limits. Use a "
        "Bessel null for an absolute steady-tone measurement."
    ),
)

PROFILE_REFERENCES: dict[str, DeviationReference] = {
    reference.profile_id: reference
    for reference in (
        MODULE17_M17_REFERENCE,
        MMDVM_DMR_REFERENCE,
        GENERIC_FM_REFERENCE,
    )
}


def reference_for_profile(profile_id: str) -> DeviationReference | None:
    return PROFILE_REFERENCES.get(profile_id)


BESSEL_REFERENCE_DETAIL = (
    "Bessel J0 nulls are absolute steady-tone references: the first null is "
    "at 2.4048 × the modulation-tone frequency and the second at 5.5201 × "
    "the tone frequency. The selected tone and null determine the expected "
    "deviation exactly; no SDR amplitude correction is applied."
)


def bessel_reference(
    null_index: int,
    tone_frequency_hz: float,
) -> DeviationReference:
    coefficient = carrier_null_index(null_index)
    nominal_hz = coefficient * tone_frequency_hz
    return DeviationReference(
        profile_id="bessel",
        label=f"Bessel J0 null {null_index}",
        nominal_hz=nominal_hz,
        meter_marks=((nominal_hz, "J0"),),
        summary=(
            f"{nominal_hz / 1000.0:.3f} kHz at "
            f"{tone_frequency_hz / 1000.0:.3f} kHz tone"
        ),
        short=f"{nominal_hz / 1000.0:.3f} kHz",
        detail=BESSEL_REFERENCE_DETAIL,
    )
