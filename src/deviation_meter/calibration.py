"""Persistent calibration and verification records."""

from __future__ import annotations

import json
import math
import os
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Mapping

from . import APPLICATION_NAME, __version__
from .measurement import DeviationReading, MeasurementEngine


class CalibrationError(ValueError):
    """Raised when a calibration record cannot be created or saved."""


def default_calibration_dir() -> Path:
    """Return the per-user calibration directory.

    The legacy application directory is intentionally retained so existing
    calibration records remain discoverable after the DeviationStation rename.
    """

    data_home = os.environ.get("XDG_DATA_HOME")
    base = Path(data_home) if data_home else Path.home() / ".local" / "share"
    return base / "gnuradio-deviation-meter" / "calibrations"


@dataclass(frozen=True, slots=True)
class BesselVerificationRecord:
    """Verified Bessel-null measurement tied to the equipment setup."""

    receive_frequency_hz: float
    tone_frequency_hz: float
    null_index: int
    reading: DeviationReading
    device: Mapping[str, Any]
    audio_output: str = ""
    notes: str = ""
    created_utc: str = field(
        default_factory=lambda: datetime.now(tz=timezone.utc).isoformat()
    )

    def __post_init__(self) -> None:
        if not math.isfinite(self.receive_frequency_hz) or self.receive_frequency_hz <= 0:
            raise CalibrationError("receive_frequency_hz must be positive")
        if not math.isfinite(self.tone_frequency_hz) or self.tone_frequency_hz <= 0:
            raise CalibrationError("tone_frequency_hz must be positive")
        if self.null_index < 1:
            raise CalibrationError("null_index must be one or greater")
        if self.reading.engine is not MeasurementEngine.BESSEL:
            raise CalibrationError("verification record requires a Bessel reading")
        if not self.reading.valid or self.reading.deviation_hz is None:
            raise CalibrationError("verification record requires a valid Bessel reading")

    def as_dict(self) -> dict[str, Any]:
        return {
            "schema_version": 1,
            "kind": "bessel-null-verification",
            "created_utc": self.created_utc,
            "application": {
                "name": APPLICATION_NAME,
                "version": __version__,
            },
            "receive_frequency_hz": self.receive_frequency_hz,
            "tone_frequency_hz": self.tone_frequency_hz,
            "null_index": self.null_index,
            "verified_deviation_hz": self.reading.deviation_hz,
            "reading": self.reading.as_dict(),
            "device": dict(self.device),
            "audio_output": self.audio_output,
            "notes": self.notes,
        }


@dataclass(frozen=True, slots=True)
class TargetDeviationRecord:
    """A discriminator-controlled audio level calibrated to a target."""

    receive_frequency_hz: float
    target_deviation_hz: float
    reading: DeviationReading
    device: Mapping[str, Any]
    source: str = ""
    audio_output: str = ""
    notes: str = ""
    created_utc: str = field(
        default_factory=lambda: datetime.now(tz=timezone.utc).isoformat()
    )

    def __post_init__(self) -> None:
        if (
            not math.isfinite(self.receive_frequency_hz)
            or self.receive_frequency_hz <= 0
        ):
            raise CalibrationError("receive_frequency_hz must be positive")
        if (
            not math.isfinite(self.target_deviation_hz)
            or self.target_deviation_hz <= 0
        ):
            raise CalibrationError("target_deviation_hz must be positive")
        if self.reading.engine is not MeasurementEngine.DISCRIMINATOR:
            raise CalibrationError("target record requires a discriminator reading")
        if not self.reading.valid or self.reading.deviation_hz is None:
            raise CalibrationError("target record requires a valid reading")

    def as_dict(self) -> dict[str, Any]:
        achieved = float(self.reading.deviation_hz or 0.0)
        return {
            "schema_version": 1,
            "kind": "discriminator-target-calibration",
            "created_utc": self.created_utc,
            "application": {
                "name": APPLICATION_NAME,
                "version": __version__,
            },
            "receive_frequency_hz": self.receive_frequency_hz,
            "target_deviation_hz": self.target_deviation_hz,
            "achieved_deviation_hz": achieved,
            "error_hz": achieved - self.target_deviation_hz,
            "reading": self.reading.as_dict(),
            "device": dict(self.device),
            "source": self.source,
            "audio_output": self.audio_output,
            "notes": self.notes,
        }


def _quality_line(reading: DeviationReading) -> str:
    quality = reading.quality
    issues = f" ({'; '.join(quality.issues)})" if quality.issues else ""
    return f"{quality.status.value.upper()}{issues}"


def _device_lines(device: Mapping[str, Any]) -> list[str]:
    return [f"- `{key}`: {value}" for key, value in sorted(device.items())]


def render_markdown(
    record: BesselVerificationRecord | TargetDeviationRecord,
) -> str:
    """Render a compact human-readable calibration report."""

    lines = [
        "# Bessel null verification"
        if isinstance(record, BesselVerificationRecord)
        else "# Discriminator target calibration",
        "",
        f"- **Created:** {record.created_utc}",
        f"- **Receive frequency:** {record.receive_frequency_hz / 1e6:.6f} MHz",
    ]
    if isinstance(record, BesselVerificationRecord):
        depth = record.reading.metadata.get("null_depth_db")
        carrier = record.reading.metadata.get("carrier_offset_hz")
        lines.extend(
            [
                f"- **Tone:** {record.tone_frequency_hz:.3f} Hz",
                f"- **Carrier null:** J0 #{record.null_index}",
                f"- **Verified deviation:** "
                f"{float(record.reading.deviation_hz or 0.0):.3f} Hz",
                f"- **Quality:** {_quality_line(record.reading)}",
            ]
        )
        if depth is not None:
            lines.append(f"- **Null depth:** {float(depth):.2f} dB")
        if carrier is not None:
            lines.append(f"- **Carrier offset:** {float(carrier):+.1f} Hz")
        lines.append(f"- **Audio output:** {record.audio_output or 'system default'}")
    else:
        achieved = float(record.reading.deviation_hz or 0.0)
        lines.extend(
            [
                f"- **Target deviation:** {record.target_deviation_hz:.3f} Hz",
                f"- **Achieved deviation:** {achieved:.3f} Hz",
                f"- **Error:** {achieved - record.target_deviation_hz:+.3f} Hz",
                f"- **Quality:** {_quality_line(record.reading)}",
                f"- **Source:** {record.source or 'unknown'}",
                f"- **Audio output:** {record.audio_output or 'system default'}",
            ]
        )
    lines.extend(["", "## Device", ""])
    lines.extend(_device_lines(record.device))
    if record.notes:
        lines.extend(["", "## Notes", "", record.notes])
    return "\n".join(lines) + "\n"


def _save_record(
    record: BesselVerificationRecord | TargetDeviationRecord,
    *,
    stem: str,
    directory: Path | None = None,
) -> Path:
    target_dir = Path(directory) if directory is not None else default_calibration_dir()
    target_dir.mkdir(parents=True, exist_ok=True)
    normalized = record.created_utc.replace("-", "").replace(":", "").replace("+", "Z")
    json_target = target_dir / f"{normalized}_{stem}.json"
    suffix = 0
    while json_target.exists():
        suffix += 1
        json_target = target_dir / f"{normalized}_{stem}-{suffix}.json"
    markdown_target = json_target.with_suffix(".md")
    json_temporary = json_target.with_name(json_target.name + ".tmp")
    markdown_temporary = markdown_target.with_name(markdown_target.name + ".tmp")
    json_temporary.write_text(
        json.dumps(record.as_dict(), indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    markdown_temporary.write_text(render_markdown(record), encoding="utf-8")
    os.replace(json_temporary, json_target)
    os.replace(markdown_temporary, markdown_target)
    return json_target


def save_target_deviation(
    record: TargetDeviationRecord,
    *,
    directory: Path | None = None,
) -> Path:
    """Write one discriminator target calibration record atomically."""

    return _save_record(
        record,
        stem=f"target-{record.target_deviation_hz:.0f}Hz",
        directory=directory,
    )


def save_bessel_verification(
    record: BesselVerificationRecord,
    *,
    directory: Path | None = None,
) -> Path:
    """Write one verification record atomically and return its JSON path."""

    return _save_record(
        record,
        stem=f"bessel-j0-{record.null_index}_{record.tone_frequency_hz:.0f}Hz",
        directory=directory,
    )
