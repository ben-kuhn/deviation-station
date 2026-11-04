"""PyQt6 live measurement interface."""

from __future__ import annotations

import math
import os
import time
from collections import deque
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import numpy as np
from PyQt6.QtCore import QPointF, QRectF, QSettings, Qt, QTimer
from PyQt6.QtGui import (
    QColor,
    QFont,
    QFontMetrics,
    QImage,
    QPainter,
    QPen,
    QPolygonF,
)
from PyQt6.QtWidgets import (
    QCheckBox,
    QComboBox,
    QDialog,
    QDialogButtonBox,
    QDoubleSpinBox,
    QFileDialog,
    QFormLayout,
    QFrame,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QMainWindow,
    QMessageBox,
    QProgressBar,
    QPushButton,
    QSizePolicy,
    QSpinBox,
    QTabWidget,
    QVBoxLayout,
    QWidget,
)

from .audio import AudioToneError, TonePlayer
from .auto_cal import (
    AutoCalConfig,
    AutoCalController,
    AutoCalError,
    AutoCalProgress,
    AutoCalState,
)
from .bessel import BesselNullDetector, BesselProgress, carrier_null_index
from .direwolf import (
    DirewolfCalibration,
    DirewolfConfig,
    DirewolfError,
    DirewolfProfile,
    parse_direwolf_config,
    profile_tone_hz,
)
from .calibration import (
    BesselVerificationRecord,
    CalibrationError,
    TargetDeviationRecord,
    save_bessel_verification,
    save_target_deviation,
)
from .gr_live import (
    GnuRadioLiveConfig,
    GnuRadioLiveError,
    GnuRadioLiveSession,
)
from .live import LiveSnapshot
from .measurement import ReadingStatus
from .rehearsal import (
    BesselRehearsalPlant,
    RehearsalIO,
    TargetRehearsalPlant,
)
from .remote_direwolf import RemoteDirewolfCalibration, RemoteDirewolfConfig
from .reference import (
    BESSEL_REFERENCE_DETAIL,
    GENERIC_FM_REFERENCE,
    PROFILE_REFERENCES,
    DeviationReference,
    bessel_reference,
    reference_for_profile,
)
from .rigctl import RigctldClient, RigctldError
from .ssh import SshError
from .target_cal import (
    TargetCalConfig,
    TargetCalController,
    TargetCalError,
    TargetCalProgress,
    TargetCalState,
)
from .xara import XaraCalibration, XaraConfig, XaraError

WHITE = QColor("#f5f5f5")
MUTED = QColor("#b7bcc2")
AMBER = QColor("#ffbf47")
RED = QColor("#ff6868")
DARK = QColor("#07090c")
PANEL = QColor("#0d1117")
GRID = QColor("#343b44")

SOURCE_SIMULATOR = "simulator"
SOURCE_LIVE = "live"

_REMOTE_HELP_TEXT = (
    "Remote Direwolf is used by the Auto-Calibrate generator. The SDR stays "
    "local and must hear the remote transmitter over RF; no SDR IQ is sent "
    "over SSH.\n\n"
    "• SSH must work non-interactively with key authentication, for example "
    "ssh -o BatchMode=yes host true.\n"
    "• The remote host needs direwolf, PipeWire, pw-dump, wpctl, timeout, and "
    "pgrep. The remote user's PipeWire session must be available.\n"
    "• The selected Direwolf file is a local template. Its ADEVICE, ARATE, "
    "ACHANNELS, and MODEM values are uploaded; ADEVICE must exist remotely.\n"
    "• Stop any normal Direwolf instance on the remote host before starting.\n"
    "• The remote rigctld host and port must be reachable from this machine "
    "and from the remote host, and rigctld must listen on a reachable "
    "address. Do not use 127.0.0.1 unless both ends are the same machine.\n"
    "• Remote Direwolf owns PTT; the application verifies PTT through the "
    "network and uses a remote timeout watchdog as a backstop.\n"
    "• SSH forwarding is not required or used."
)



def _settings_bool(value: object, default: bool = False) -> bool:
    if value is None:
        return default
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        return value.strip().lower() in {"1", "true", "yes", "on"}
    return bool(value)


@dataclass(slots=True)
class DeviceSettings:
    device_args: str = "driver=rtlsdr"
    sample_rate_hz: float = 240_000.0
    lo_offset_hz: float = 50_000.0
    gain_db: float = 0.0
    bandwidth_hz: float = 200_000.0
    audio_output: str = ""
    tone_level: float = 0.25
    ptt_timeout_s: float = 20.0
    rig_host: str = "127.0.0.1"
    rig_port: int = 4532
    direwolf_remote: bool = False
    direwolf_ssh_host: str = ""
    direwolf_remote_rig_host: str = ""
    direwolf_remote_rig_port: int = 4532


class _AutoCalHardware:
    """Bridge controller effects onto the main window's guarded hardware."""

    def __init__(self, window: "MainWindow") -> None:
        self._window = window

    def key_ptt(self) -> bool:
        return self._window._key_ptt()

    def unkey_ptt(self) -> bool:
        return self._window._unkey_ptt()

    def start_tone(self, level: float) -> None:
        self._window._start_auto_tone(level)

    def stop_tone(self) -> None:
        self._window._stop_tone()

    def set_level(self, level: float) -> None:
        self._window._set_auto_tone_level(level)


class _DirewolfAutoCalHardware:
    """Expose a Direwolf process as the auto-calibration audio source."""

    def __init__(
        self,
        window: "MainWindow",
        adapter: DirewolfCalibration,
    ) -> None:
        self._window = window
        self._adapter = adapter

    def key_ptt(self) -> bool:
        return self._window._key_direwolf_calibration(self._adapter)

    def unkey_ptt(self) -> bool:
        return self._window._unkey_direwolf_calibration(self._adapter)

    def start_tone(self, level: float) -> None:
        self._adapter.set_level(level)

    def stop_tone(self) -> None:
        self._adapter.set_level(0.0)

    def set_level(self, level: float) -> None:
        self._adapter.set_level(level)


class _RemoteDirewolfAutoCalHardware:
    """Expose a remote SSH-controlled Direwolf process as the audio source."""

    def __init__(
        self,
        window: "MainWindow",
        adapter: RemoteDirewolfCalibration,
    ) -> None:
        self._window = window
        self._adapter = adapter

    def key_ptt(self) -> bool:
        return self._window._key_remote_direwolf_calibration(self._adapter)

    def unkey_ptt(self) -> bool:
        return self._window._unkey_remote_direwolf_calibration(self._adapter)

    def start_tone(self, level: float) -> None:
        self._adapter.set_level(level)

    def stop_tone(self) -> None:
        self._adapter.set_level(0.0)

    def set_level(self, level: float) -> None:
        self._adapter.set_level(level)


class _XaraAutoCalHardware:
    """Expose an existing xara TUNE session as the calibration source."""

    def __init__(
        self,
        window: "MainWindow",
        adapter: XaraCalibration,
    ) -> None:
        self._window = window
        self._adapter = adapter

    def key_ptt(self) -> bool:
        return self._window._key_xara_calibration(self._adapter)

    def unkey_ptt(self) -> bool:
        return self._window._unkey_xara_calibration(self._adapter)

    def start_tone(self, level: float) -> None:
        self._adapter.set_level(level)

    def stop_tone(self) -> None:
        self._adapter.mute()

    def set_level(self, level: float) -> None:
        self._adapter.set_level(level)


class AnalogMeter(QWidget):
    """Large white-on-dark analog deviation meter."""

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setMinimumSize(480, 300)
        self._value_hz: float | None = None
        self._max_hz = 3000.0
        self._status = "WAITING"
        self._reference_marks: list[tuple[float, str]] = []

    def set_value(self, value_hz: float | None, status: str = "") -> None:
        self._value_hz = value_hz
        self._status = status
        self.update()

    def set_range_hz(self, maximum_hz: float) -> None:
        self._max_hz = float(maximum_hz)
        self.update()

    def set_reference_marks(self, marks) -> None:
        self._reference_marks = [
            (float(hz), str(label))
            for hz, label in marks
            if math.isfinite(float(hz)) and float(hz) > 0.0
        ]
        self.update()

    def paintEvent(self, event) -> None:  # noqa: N802
        del event
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        painter.fillRect(self.rect(), DARK)

        width = float(self.width())
        height = float(self.height())
        unit = max(0.75, min(width / 800.0, height / 500.0))
        center = QPointF(width / 2.0, height * 0.82)
        radius = min(width * 0.45, height * 0.68)
        start_degrees = 180.0
        sweep_degrees = -180.0
        arc_rect = QRectF(
            center.x() - radius,
            center.y() - radius,
            radius * 2.0,
            radius * 2.0,
        )
        painter.setPen(QPen(WHITE, 2.5 * unit))
        painter.drawArc(arc_rect, int(start_degrees * 16), int(sweep_degrees * 16))

        if self._max_hz <= 3500.0:
            step_hz = 500.0
        elif self._max_hz <= 6500.0:
            step_hz = 1000.0
        else:
            step_hz = 2000.0
        label_font = QFont("DejaVu Sans")
        label_font.setPixelSize(max(10, int(round(16.0 * unit))))
        label_font.setBold(True)
        value_font = QFont("DejaVu Sans")
        value_font.setPixelSize(max(9, int(round(14.0 * unit))))
        label_width = 70.0 * unit
        label_height = 26.0 * unit
        painter.setFont(label_font)
        value = 0.0
        while value <= self._max_hz + 0.1:
            fraction = value / self._max_hz
            angle_degrees = start_degrees + fraction * sweep_degrees
            angle = math.radians(angle_degrees)
            direction = QPointF(math.cos(angle), -math.sin(angle))
            outer = center + direction * radius
            inner = center + direction * (radius - 18.0 * unit)
            painter.setPen(QPen(WHITE, 2.2 * unit))
            painter.drawLine(inner, outer)
            label_point = center + direction * (radius - 42.0 * unit)
            label = f"{value / 1000.0:g}"
            painter.setFont(value_font)
            painter.drawText(
                QRectF(
                    label_point.x() - label_width / 2.0,
                    label_point.y() - label_height / 2.0,
                    label_width,
                    label_height,
                ),
                Qt.AlignmentFlag.AlignCenter,
                label,
            )
            painter.setFont(label_font)
            value += step_hz

        minor_count = int(self._max_hz / (step_hz / 2.0))
        for index in range(minor_count + 1):
            fraction = index / minor_count
            angle_degrees = start_degrees + fraction * sweep_degrees
            angle = math.radians(angle_degrees)
            direction = QPointF(math.cos(angle), -math.sin(angle))
            painter.setPen(QPen(MUTED, 1.2 * unit))
            painter.drawLine(
                center + direction * (radius - 9.0 * unit),
                center + direction * radius,
            )

        reference_font = QFont("DejaVu Sans")
        reference_font.setPixelSize(max(9, int(round(10.0 * unit))))
        reference_font.setBold(True)
        for reference_hz, reference_label in self._reference_marks:
            if reference_hz > self._max_hz:
                continue
            fraction = reference_hz / self._max_hz
            angle_degrees = start_degrees + fraction * sweep_degrees
            angle = math.radians(angle_degrees)
            direction = QPointF(math.cos(angle), -math.sin(angle))
            painter.setPen(QPen(AMBER, 3.0 * unit))
            painter.drawLine(
                center + direction * (radius - 20.0 * unit),
                center + direction * (radius + 2.0 * unit),
            )
            side = QPointF(-math.sin(angle), -math.cos(angle)) * (4.0 * unit)
            painter.setPen(Qt.PenStyle.NoPen)
            painter.setBrush(AMBER)
            painter.drawPolygon(
                QPolygonF(
                    [
                        center + direction * (radius + 10.0 * unit),
                        center + direction * (radius + 1.0 * unit) + side,
                        center + direction * (radius + 1.0 * unit) - side,
                    ]
                )
            )
            painter.setFont(reference_font)
            painter.setPen(AMBER)
            label_width = 58.0 * unit
            label_height = 18.0 * unit
            label_point = center + direction * (radius + 24.0 * unit)
            label_x = min(
                max(label_point.x() - label_width / 2.0, 2.0),
                width - label_width - 2.0,
            )
            label_y = min(
                max(label_point.y() - label_height / 2.0, 2.0),
                height - label_height - 2.0,
            )
            painter.drawText(
                QRectF(label_x, label_y, label_width, label_height),
                Qt.AlignmentFlag.AlignCenter,
                reference_label,
            )
        painter.setBrush(Qt.BrushStyle.NoBrush)

        range_font = QFont("DejaVu Sans")
        range_font.setPixelSize(max(10, int(round(16.0 * unit))))
        painter.setFont(range_font)
        painter.setPen(MUTED)
        painter.drawText(
            QRectF(0.0, center.y() + 24.0 * unit, width, 28.0 * unit),
            Qt.AlignmentFlag.AlignHCenter | Qt.AlignmentFlag.AlignTop,
            f"0 – {self._max_hz / 1000.0:g} kHz",
        )

        if self._value_hz is not None:
            fraction = min(max(self._value_hz / self._max_hz, 0.0), 1.0)
            angle_degrees = start_degrees + fraction * sweep_degrees
            angle = math.radians(angle_degrees)
            length = radius - 26.0
            needle_tip = center + QPointF(math.cos(angle), -math.sin(angle)) * length
            side = QPointF(-math.sin(angle), -math.cos(angle)) * (7.0 * unit)
            painter.setPen(Qt.PenStyle.NoPen)
            painter.setBrush(WHITE)
            painter.drawPolygon(
                QPolygonF(
                    [
                        center + side,
                        needle_tip,
                        center - side,
                    ]
                )
            )
            painter.setBrush(DARK)
            painter.setPen(QPen(WHITE, 2.0 * unit))
            painter.drawEllipse(center, 11.0 * unit, 11.0 * unit)

        status_font = QFont("DejaVu Sans")
        status_font.setPixelSize(max(11, int(round(17.0 * unit))))
        status_font.setBold(True)
        painter.setPen(WHITE if self._status in ("", "LIVE") else AMBER)
        painter.setFont(status_font)
        painter.drawText(
            QRectF(0.0, height - 38.0 * unit, width, 28.0 * unit),
            Qt.AlignmentFlag.AlignCenter,
            self._status,
        )
        painter.end()


class DigitalPane(QFrame):
    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setFrameShape(QFrame.Shape.StyledPanel)
        self.setStyleSheet(
            f"QFrame {{ background: {PANEL.name()}; border: 1px solid {GRID.name()}; }}"
        )
        layout = QVBoxLayout(self)
        layout.setContentsMargins(18, 14, 18, 14)
        layout.setSpacing(2)

        self.title_label = QLabel("DEVIATION")
        self.title_label.setStyleSheet(
            "border: none; color: #f5f5f5; letter-spacing: 3px;"
        )
        layout.addWidget(self.title_label)

        self.value_label = QLabel("--")
        self.value_label.setStyleSheet("border: none; color: #f5f5f5;")
        self.value_label.setFont(QFont("DejaVu Sans", 58, QFont.Weight.Bold))
        self.value_label.setAlignment(
            Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter
        )
        layout.addWidget(self.value_label, 1)

        self.definition_label = QLabel("No signal")
        self.definition_label.setStyleSheet("border: none; color: #f5f5f5;")
        self.definition_label.setFont(QFont("DejaVu Sans", 13))
        self.definition_label.setAlignment(Qt.AlignmentFlag.AlignRight)
        layout.addWidget(self.definition_label)

        self.status_label = QLabel("WAITING FOR RF")
        self.status_label.setStyleSheet("border: none; color: #b7bcc2;")
        self.status_label.setAlignment(Qt.AlignmentFlag.AlignRight)
        layout.addWidget(self.status_label)
        self.definition_label.setWordWrap(True)
        self.reference_label = QLabel("")
        self.reference_label.setStyleSheet(
            "border: none; color: #ffbf47; letter-spacing: 0px;"
        )
        self.reference_label.setFont(QFont("DejaVu Sans", 10))
        self.reference_label.setAlignment(Qt.AlignmentFlag.AlignRight)
        self.reference_label.setWordWrap(False)
        layout.addWidget(self.reference_label)
        self._ui_scale = 1.0
        self._update_fonts()

    @staticmethod
    def _pixel_font(size: float, bold: bool = False) -> QFont:
        font = QFont("DejaVu Sans")
        font.setPixelSize(max(9, int(round(size))))
        font.setBold(bold)
        return font

    def set_ui_scale(self, scale: float) -> None:
        self._ui_scale = max(0.8, min(1.8, float(scale)))
        self._update_fonts()
        self._fit_value_font()

    def _update_fonts(self) -> None:
        height = max(1, self.height())
        title_size = max(12.0, height * 0.075)
        value_size = max(28.0, height * 0.36)
        detail_size = max(12.0, height * 0.075)
        status_size = max(11.0, height * 0.065)
        reference_size = max(9.0, height * 0.055)
        self.title_label.setFont(self._pixel_font(title_size, True))
        self.value_label.setFont(self._pixel_font(value_size, True))
        self.definition_label.setFont(self._pixel_font(detail_size))
        self.reference_label.setFont(self._pixel_font(reference_size, True))
        self.status_label.setFont(self._pixel_font(status_size))

    def _fit_value_font(self) -> None:
        text = self.value_label.text() or "8.888 kHz"
        available = max(80, self.width() - 40)
        size = max(
            28,
            int(round(max(1, self.height()) * 0.36)),
            int(round(75.0 * self._ui_scale)),
        )
        while size > 24:
            font = self._pixel_font(size, True)
            if QFontMetrics(font).horizontalAdvance(text) <= available:
                break
            size -= 2
        self.value_label.setFont(self._pixel_font(size, True))

    def resizeEvent(self, event) -> None:  # noqa: N802
        super().resizeEvent(event)
        self._update_fonts()
        self._fit_value_font()

    def _show_readout(
        self,
        value: str,
        definition: str,
        status: str,
        *,
        color: str = "#f5f5f5",
    ) -> None:
        self.value_label.setText(value)
        self.definition_label.setText(definition)
        self.status_label.setText(status)
        self.status_label.setStyleSheet(f"border: none; color: {color};")
        self._fit_value_font()

    def set_reference(
        self,
        reference: DeviationReference | None,
        *,
        prefix: str = "REF",
    ) -> None:
        if reference is None:
            self.reference_label.setText("")
            self.reference_label.setToolTip("")
            return
        self.reference_label.setText(
            f"{prefix} · {reference.label} · "
            f"{reference.short or reference.summary}"
        )
        self.reference_label.setToolTip(reference.tooltip)

    def set_bessel(self, progress: BesselProgress) -> None:
        self.set_reference(
            bessel_reference(progress.null_index, progress.tone_frequency_hz),
            prefix="ABSOLUTE REF",
        )
        definition = (
            f"BESSEL · J0 null {progress.null_index} · "
            f"{progress.tone_frequency_hz / 1000.0:.3f} kHz tone"
        )
        depth = (
            f" · depth {progress.null_depth_db:.0f} dB"
            if progress.null_depth_db is not None
            else ""
        )
        offset = (
            f" · carrier {progress.carrier_offset_hz:+.0f} Hz"
            if progress.carrier_offset_hz is not None
            else ""
        )
        if progress.captured is not None:
            reading = progress.captured
            captured_depth = reading.metadata.get("null_depth_db")
            captured_depth_text = (
                f" · depth {float(captured_depth):.0f} dB"
                if captured_depth is not None
                else ""
            )
            color = (
                "#ffbf47"
                if reading.quality.status is ReadingStatus.MARGINAL
                else "#f5f5f5"
            )
            self._show_readout(
                f"{reading.deviation_hz / 1000.0:.3f} kHz",
                definition,
                f"NULL CAPTURED{captured_depth_text}{offset}",
                color=color,
            )
            return
        self._show_readout(
            "—",
            definition,
            f"{progress.state}{depth}{offset}",
            color=("#ffbf47" if progress.carrier_locked else "#b7bcc2"),
        )

    def set_target(
        self,
        reading: object | None,
        *,
        target_hz: float,
        state: str,
    ) -> None:
        self.set_reference(GENERIC_FM_REFERENCE)
        deviation = getattr(reading, "deviation_hz", None)
        if reading is None or deviation is None:
            self._show_readout(
                "—",
                f"DISCRIMINATOR · target {target_hz / 1000.0:.3f} kHz",
                state,
                color="#ffbf47",
            )
            return
        quality = getattr(reading, "quality", None)
        status = (
            quality.status.value.upper()
            if quality is not None
            else "UNKNOWN"
        )
        color = "#f5f5f5"
        if quality is not None and quality.status is ReadingStatus.MARGINAL:
            color = "#ffbf47"
        elif quality is not None and quality.status is ReadingStatus.INVALID:
            color = "#ff6868"
        self._show_readout(
            f"{deviation / 1000.0:.3f} kHz",
            f"DISCRIMINATOR · target {target_hz / 1000.0:.3f} kHz",
            f"{state} · {status}",
            color=color,
        )

    def set_measurement(
        self,
        snapshot: LiveSnapshot | None,
        *,
        source_label: str,
        profile_id: str | None = None,
    ) -> None:
        if snapshot is not None:
            reference = reference_for_profile(snapshot.profile_id)
        elif profile_id is not None:
            reference = reference_for_profile(profile_id)
        else:
            reference = None
        if reference is not None:
            self.set_reference(reference)
        if snapshot is None or snapshot.reporting_deviation_hz is None:
            self.value_label.setText("—")
            self.definition_label.setText(source_label)
            self.status_label.setText("WAITING FOR RF")
            self.status_label.setStyleSheet("border: none; color: #b7bcc2;")
            self._fit_value_font()
            return

        value = snapshot.reporting_deviation_hz
        self.value_label.setText(f"{value / 1000.0:.3f} kHz")
        self._fit_value_font()
        profile = {
            "mmdvm-dmr": "DMR",
            "module17-m17": "M17",
            "generic-fm": "GENERIC FM",
        }.get(snapshot.profile_id, snapshot.profile_id.upper())
        measurement_name = (
            "robust peak deviation"
            if snapshot.profile_id == "generic-fm"
            else "outer-symbol deviation"
        )
        if snapshot.summary is not None and snapshot.summary["valid_segments"] > 1:
            detail = (
                f"{profile} • median of {snapshot.summary['valid_segments']} segments"
            )
        else:
            detail = f"{profile} • {measurement_name}"
        self.definition_label.setText(f"{source_label} · {detail}")
        quality = "VALID"
        color = "#f5f5f5"
        if snapshot.reading is not None:
            quality = snapshot.reading.quality.status.value.upper()
            if snapshot.reading.quality.status is ReadingStatus.MARGINAL:
                color = "#ffbf47"
            elif snapshot.reading.quality.status is ReadingStatus.INVALID:
                color = "#ff6868"
        snr = "" if snapshot.snr_db is None else f" · SNR {snapshot.snr_db:.0f} dB"
        self.status_label.setText(f"{quality}{snr}")
        self.status_label.setStyleSheet(f"border: none; color: {color};")


class WaterfallWidget(QWidget):
    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setMinimumSize(280, 180)
        self._rows: deque[np.ndarray] = deque(maxlen=140)

    def add_spectrum(self, spectrum_db: tuple[float, ...]) -> None:
        if not spectrum_db:
            return
        db = np.asarray(spectrum_db, dtype=np.float64)
        norm = np.clip((db + 100.0) / 100.0, 0.0, 1.0)
        red = np.clip((norm - 0.62) / 0.38, 0.0, 1.0)
        green = np.power(norm, 1.8)
        blue = 0.25 + 0.75 * np.power(norm, 0.7)
        row = np.stack(
            [
                (255.0 * red),
                (255.0 * green),
                (255.0 * blue),
            ],
            axis=1,
        ).astype(np.uint8)
        self._rows.append(row)
        self.update()

    def paintEvent(self, event) -> None:  # noqa: N802
        del event
        painter = QPainter(self)
        painter.fillRect(self.rect(), QColor("#020305"))
        painter.setPen(QPen(GRID, 1.0))
        painter.drawRect(self.rect().adjusted(0, 0, -1, -1))
        if not self._rows:
            painter.setPen(MUTED)
            painter.setFont(QFont("DejaVu Sans", 12))
            painter.drawText(self.rect(), Qt.AlignmentFlag.AlignCenter, "NO SPECTRUM")
            painter.end()
            return
        image_data = np.stack(list(self._rows), axis=0)
        image = QImage(
            image_data.data,
            image_data.shape[1],
            image_data.shape[0],
            int(image_data.strides[0]),
            QImage.Format.Format_RGB888,
        ).copy()
        painter.drawImage(self.rect().adjusted(1, 1, -1, -1), image)
        painter.setPen(QPen(WHITE, 1.0))
        center_x = self.width() / 2.0
        painter.drawLine(QPointF(center_x, 1), QPointF(center_x, self.height() - 1))
        unit = max(0.8, min(self.width() / 420.0, self.height() / 260.0))
        label_font = QFont("DejaVu Sans")
        label_font.setPixelSize(max(9, int(round(11.0 * unit))))
        painter.setFont(label_font)
        painter.setPen(MUTED)
        painter.drawText(
            QRectF(8.0, self.height() - 26.0 * unit, 100.0 * unit, 20.0 * unit),
            Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter,
            "−24 kHz",
        )
        painter.drawText(
            QRectF(
                self.width() - 108.0 * unit,
                self.height() - 26.0 * unit,
                100.0 * unit,
                20.0 * unit,
            ),
            Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter,
            "+24 kHz",
        )
        painter.end()


class DeviationReferenceDialog(QDialog):
    """Show nominal deviation references and the Bessel absolute reference."""

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setWindowTitle("Deviation references")
        self.setMinimumWidth(620)
        self.resize(620, 360)
        layout = QVBoxLayout(self)

        intro = QLabel(
            "Nominal guidance for the available profiles. These are not "
            "pass/fail limits; transmitter drive, audio processing, and "
            "measurement bandwidth still matter."
        )
        intro.setWordWrap(True)
        intro.setStyleSheet("color: #b7bcc2;")
        layout.addWidget(intro)

        form = QFormLayout()
        form.setSpacing(8)
        for reference in PROFILE_REFERENCES.values():
            value = reference.summary
            if reference.symbol_rate_hz is not None:
                value += f" · {reference.symbol_rate_hz:g} sym/s"
            value_label = QLabel(value)
            value_label.setWordWrap(True)
            value_label.setToolTip(reference.tooltip)
            form.addRow(reference.label, value_label)

        bessel = QLabel(
            "Δf = 2.4048 × tone (1st J0), 5.5201 × tone (2nd J0). "
            "Absolute steady-tone reference; no SDR amplitude correction."
        )
        bessel.setWordWrap(True)
        bessel.setToolTip(BESSEL_REFERENCE_DETAIL)
        form.addRow("Bessel J0", bessel)
        layout.addLayout(form)

        definition = QLabel(
            "Digital readings are outer-symbol deviations. Generic FM is a "
            "robust peak measurement. A Bessel null is the absolute reference "
            "for a steady tone; use it to validate discriminator readings."
        )
        definition.setWordWrap(True)
        definition.setStyleSheet("color: #b7bcc2;")
        layout.addWidget(definition)

        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Close)
        buttons.rejected.connect(self.reject)
        buttons.clicked.connect(self.accept)
        layout.addWidget(buttons)


class DeviceDialog(QDialog):
    def __init__(self, settings: DeviceSettings, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setWindowTitle("SDR and rig settings")
        self.setMinimumWidth(640)
        form = QFormLayout(self)

        self.device_args = QLineEdit(settings.device_args)
        form.addRow("Soapy device arguments", self.device_args)
        self.sample_rate = QDoubleSpinBox()
        self.sample_rate.setRange(0.1, 3.2)
        self.sample_rate.setDecimals(3)
        self.sample_rate.setSuffix(" MHz")
        self.sample_rate.setValue(settings.sample_rate_hz / 1e6)
        form.addRow("SDR sample rate", self.sample_rate)
        self.lo_offset = QDoubleSpinBox()
        self.lo_offset.setRange(0.005, 0.4)
        self.lo_offset.setDecimals(3)
        self.lo_offset.setSuffix(" MHz")
        self.lo_offset.setValue(settings.lo_offset_hz / 1e6)
        form.addRow("Signal below LO by", self.lo_offset)
        self.gain = QDoubleSpinBox()
        self.gain.setRange(-50.0, 60.0)
        self.gain.setDecimals(1)
        self.gain.setSuffix(" dB")
        self.gain.setValue(settings.gain_db)
        form.addRow("Manual gain", self.gain)
        self.bandwidth = QDoubleSpinBox()
        self.bandwidth.setRange(0.02, 2.0)
        self.bandwidth.setDecimals(0)
        self.bandwidth.setSuffix(" kHz")
        self.bandwidth.setValue(settings.bandwidth_hz / 1e3)
        form.addRow("RF bandwidth", self.bandwidth)
        self.audio_output = QComboBox()
        self.audio_output.addItem("System default", "")
        for device_name in TonePlayer.output_devices():
            self.audio_output.addItem(device_name, device_name)
        audio_index = self.audio_output.findData(settings.audio_output)
        if audio_index >= 0:
            self.audio_output.setCurrentIndex(audio_index)
        form.addRow("Tone output", self.audio_output)
        self.tone_level = QDoubleSpinBox()
        self.tone_level.setRange(1.0, 100.0)
        self.tone_level.setDecimals(0)
        self.tone_level.setSuffix(" %")
        self.tone_level.setValue(settings.tone_level * 100.0)
        form.addRow("Tone level", self.tone_level)
        self.ptt_timeout = QDoubleSpinBox()
        self.ptt_timeout.setRange(2.0, 60.0)
        self.ptt_timeout.setDecimals(0)
        self.ptt_timeout.setSuffix(" s")
        self.ptt_timeout.setValue(settings.ptt_timeout_s)
        form.addRow("PTT safety timeout", self.ptt_timeout)
        self.rig_host = QLineEdit(settings.rig_host)
        form.addRow("rigctld host", self.rig_host)
        self.rig_port = QSpinBox()
        self.rig_port.setRange(1, 65535)
        self.rig_port.setValue(settings.rig_port)
        form.addRow("rigctld port", self.rig_port)
        self.direwolf_remote = QCheckBox(
            "Run Direwolf on a remote host via SSH"
        )
        self.direwolf_remote.setChecked(settings.direwolf_remote)
        self.direwolf_remote.setToolTip(
            "Use SSH to run the Auto-Calibrate Direwolf source remotely. "
            "Only control and remote stream volume cross SSH; SDR IQ stays "
            "local. Open Remote requirements… for the full checklist."
        )
        form.addRow("Remote Direwolf", self.direwolf_remote)
        self.direwolf_ssh_host = QLineEdit(settings.direwolf_ssh_host)
        self.direwolf_ssh_host.setPlaceholderText("SSH host or ~/.ssh/config alias")
        self.direwolf_ssh_host.setToolTip(
            "SSH destination for the remote Direwolf host. BatchMode key "
            "authentication is required; password prompts are not supported."
        )
        form.addRow("SSH host", self.direwolf_ssh_host)
        self.direwolf_remote_rig_host = QLineEdit(
            settings.direwolf_remote_rig_host
        )
        self.direwolf_remote_rig_host.setPlaceholderText(
            "rigctld host reachable from this machine and the remote host"
        )
        self.direwolf_remote_rig_host.setToolTip(
            "The remote rigctld must be reachable from this machine and from "
            "the remote Direwolf host. rigctld must listen on a reachable "
            "interface. Do not use 127.0.0.1 unless both ends are local."
        )
        form.addRow("Remote rigctld host", self.direwolf_remote_rig_host)
        self.direwolf_remote_rig_port = QSpinBox()
        self.direwolf_remote_rig_port.setRange(1, 65535)
        self.direwolf_remote_rig_port.setValue(
            settings.direwolf_remote_rig_port
        )
        self.direwolf_remote_rig_port.setToolTip(
            "Port used by the remote rigctld for frequency and PTT control."
        )
        form.addRow("Remote rigctld port", self.direwolf_remote_rig_port)
        self.direwolf_remote_note = QLabel(
            "SSH control only; no SDR IQ. The local SDR must hear the remote "
            "transmitter. The config is a local template; its ADEVICE must "
            "exist remotely. Stop normal remote Direwolf first."
        )
        self.direwolf_remote_note.setWordWrap(True)
        self.direwolf_remote_note.setMinimumHeight(48)
        self.direwolf_remote_note.setStyleSheet("color: #b7bcc2;")
        self.direwolf_remote_note.setToolTip(_REMOTE_HELP_TEXT)
        form.addRow(self.direwolf_remote_note)
        self.direwolf_remote_help_button = QPushButton(
            "Remote requirements…"
        )
        self.direwolf_remote_help_button.setToolTip(
            "Show SSH, remote PipeWire, rigctld, ADEVICE, RF, and PTT "
            "requirements for remote Direwolf."
        )
        self.direwolf_remote_help_button.clicked.connect(self._show_remote_help)
        form.addRow(self.direwolf_remote_help_button)
        self.direwolf_remote.toggled.connect(self._update_remote_fields)

        buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Ok
            | QDialogButtonBox.StandardButton.Cancel
        )
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        form.addRow(buttons)
        self._update_remote_fields(self.direwolf_remote.isChecked())

    def _update_remote_fields(self, enabled: bool) -> None:
        for widget in (
            self.direwolf_ssh_host,
            self.direwolf_remote_rig_host,
            self.direwolf_remote_rig_port,
            self.direwolf_remote_note,
        ):
            widget.setEnabled(enabled)

    def _show_remote_help(self) -> None:
        QMessageBox.information(
            self,
            "Remote Direwolf requirements",
            _REMOTE_HELP_TEXT,
        )

    def settings(self) -> DeviceSettings:
        return DeviceSettings(
            device_args=self.device_args.text().strip(),
            sample_rate_hz=self.sample_rate.value() * 1e6,
            lo_offset_hz=self.lo_offset.value() * 1e6,
            gain_db=self.gain.value(),
            bandwidth_hz=self.bandwidth.value() * 1e3,
            audio_output=str(self.audio_output.currentData()),
            tone_level=self.tone_level.value() / 100.0,
            ptt_timeout_s=self.ptt_timeout.value(),
            rig_host=self.rig_host.text().strip() or "127.0.0.1",
            rig_port=self.rig_port.value(),
            direwolf_remote=self.direwolf_remote.isChecked(),
            direwolf_ssh_host=self.direwolf_ssh_host.text().strip(),
            direwolf_remote_rig_host=(
                self.direwolf_remote_rig_host.text().strip()
            ),
            direwolf_remote_rig_port=self.direwolf_remote_rig_port.value(),
        )


class MainWindow(QMainWindow):
    def __init__(self) -> None:
        super().__init__()
        self.setWindowTitle("GNU Radio Deviation Meter")
        self.setMinimumSize(1280, 720)
        self._settings_store = QSettings(
            "gnuradio-deviation-meter",
            "deviation-meter",
        )
        self._device = self._load_device_settings()
        self._session: GnuRadioLiveSession | None = None
        self._bessel_detector: BesselNullDetector | None = None
        self._bessel_spectrum_sequence = -1
        self._bessel_progress: BesselProgress | None = None
        self._auto_controller: AutoCalController | None = None
        self._auto_progress: AutoCalProgress | None = None
        self._target_controller: TargetCalController | None = None
        self._target_progress: TargetCalProgress | None = None
        self._target_snapshot_timestamp = 0.0
        self._auto_running = False
        self._auto_run_method = "bessel"
        self._auto_run_generator = "internal"
        self._auto_run_notes = ""
        self._auto_run_rehearsal = False
        self._rehearsal_timer: QTimer | None = None
        self._rehearsal_plant = None
        self._rehearsal_io: RehearsalIO | None = None
        self._direwolf_calibration: DirewolfCalibration | None = None
        self._remote_direwolf_calibration: (
            RemoteDirewolfCalibration | None
        ) = None
        self._direwolf_profile: DirewolfProfile | None = None
        self._direwolf_profile_error: str | None = None
        self._xara_calibration: XaraCalibration | None = None
        self._ptt_keyed = False
        self._frequency_hz = 443_457_000.0
        self._source_label = "SIMULATOR"
        self._ui_scale = 1.0
        self._build_ui()
        self._apply_style()
        self._load_ui_settings()
        self._apply_responsive_fonts()
        self._poll_timer = QTimer(self)
        self._poll_timer.setInterval(100)
        self._poll_timer.timeout.connect(self._poll_live_session)
        self._tone_player = TonePlayer(self)
        self._tone_player.finished.connect(self._on_tone_finished)
        self._ptt_timer = QTimer(self)
        self._ptt_timer.setSingleShot(True)
        self._ptt_timer.timeout.connect(self._ptt_timeout)
        QTimer.singleShot(250, self._start_if_idle)

    def _start_if_idle(self) -> None:
        if self._auto_running:
            return
        if self._session is None and self.source_combo.currentData() == SOURCE_SIMULATOR:
            self.start_measurement()
        elif self._session is None:
            self.status_label.setText("Ready — press Start to open the SDR")

    def _load_device_settings(self) -> DeviceSettings:
        store = self._settings_store
        return DeviceSettings(
            device_args=str(store.value("device_args", "driver=rtlsdr")),
            sample_rate_hz=float(store.value("sample_rate_hz", 240_000.0)),
            lo_offset_hz=float(store.value("lo_offset_hz", 50_000.0)),
            gain_db=float(store.value("gain_db", 0.0)),
            bandwidth_hz=float(store.value("bandwidth_hz", 200_000.0)),
            audio_output=str(store.value("audio_output", "")),
            tone_level=float(store.value("tone_level", 0.25)),
            ptt_timeout_s=float(store.value("ptt_timeout_s", 20.0)),
            rig_host=str(store.value("rig_host", "127.0.0.1")),
            rig_port=int(store.value("rig_port", 4532)),
            direwolf_remote=_settings_bool(
                store.value("direwolf_remote", False)
            ),
            direwolf_ssh_host=str(store.value("direwolf_ssh_host", "")),
            direwolf_remote_rig_host=str(
                store.value("direwolf_remote_rig_host", "")
            ),
            direwolf_remote_rig_port=int(
                store.value("direwolf_remote_rig_port", 4532)
            ),
        )

    def _build_ui(self) -> None:
        root = QWidget()
        root_layout = QVBoxLayout(root)
        root_layout.setContentsMargins(10, 8, 10, 8)
        root_layout.setSpacing(8)

        self.tabs = QTabWidget()
        self.tabs.setDocumentMode(True)
        root_layout.addWidget(self.tabs, 1)

        measure = QWidget()
        measure_layout = QVBoxLayout(measure)
        measure_layout.setContentsMargins(8, 8, 8, 8)
        measure_layout.setSpacing(8)

        controls = QHBoxLayout()
        controls.setSpacing(8)
        self.mode_label = QLabel("Mode")
        controls.addWidget(self.mode_label)
        self.mode_combo = QComboBox()
        self.mode_combo.addItem("Digital", "digital")
        self.mode_combo.addItem("Bessel J0", "bessel")
        controls.addWidget(self.mode_combo)
        self.profile_label = QLabel("Profile")
        controls.addWidget(self.profile_label)
        self.profile_combo = QComboBox()
        self.profile_combo.addItem("Module17 / M17", "m17")
        self.profile_combo.addItem("MMDVM / DMR", "dmr")
        self.profile_combo.addItem("Generic FM", "generic-fm")
        self.profile_combo.setToolTip(
            "Select the waveform profile. The digital pane shows the nominal "
            "outer-symbol reference for the selected profile; use Deviation "
            "Ref… for all values."
        )
        controls.addWidget(self.profile_combo)
        self.source_label_widget = QLabel("Source")
        controls.addWidget(self.source_label_widget)
        self.source_combo = QComboBox()
        self.source_combo.addItem("Simulator", SOURCE_SIMULATOR)
        self.source_combo.addItem("Live SDR", SOURCE_LIVE)
        controls.addWidget(self.source_combo)
        self.range_label = QLabel("Range")
        controls.addWidget(self.range_label)
        self.range_combo = QComboBox()
        self.range_combo.addItem("0–3 kHz", 3000.0)
        self.range_combo.addItem("0–6 kHz", 6000.0)
        self.range_combo.addItem("0–10 kHz", 10000.0)
        controls.addWidget(self.range_combo)
        controls.addStretch(1)
        self.device_button = QPushButton("SDR / Rig…")
        controls.addWidget(self.device_button)
        self.start_button = QPushButton("Start")
        self.start_button.setMinimumWidth(110)
        controls.addWidget(self.start_button)
        measure_layout.addLayout(controls)

        self.bessel_bar = QWidget()
        bessel_layout = QHBoxLayout(self.bessel_bar)
        bessel_layout.setContentsMargins(0, 0, 0, 0)
        bessel_layout.setSpacing(8)
        self.tone_label = QLabel("Tone")
        bessel_layout.addWidget(self.tone_label)
        self.tone_spin = QDoubleSpinBox()
        self.tone_spin.setRange(0.1, 20.0)
        self.tone_spin.setDecimals(3)
        self.tone_spin.setSuffix(" kHz")
        self.tone_spin.setValue(1.0)
        bessel_layout.addWidget(self.tone_spin)
        self.null_label = QLabel("Null")
        bessel_layout.addWidget(self.null_label)
        self.null_combo = QComboBox()
        self.null_combo.addItem("1st J0", 1)
        self.null_combo.addItem("2nd J0", 2)
        bessel_layout.addWidget(self.null_combo)
        self.relock_button = QPushButton("Relock Carrier")
        bessel_layout.addWidget(self.relock_button)
        self.manual_capture_button = QPushButton("Capture")
        bessel_layout.addWidget(self.manual_capture_button)
        self.reset_bessel_button = QPushButton("Reset Null")
        bessel_layout.addWidget(self.reset_bessel_button)
        self.tone_button = QPushButton("Tone On")
        bessel_layout.addWidget(self.tone_button)
        self.ptt_button = QPushButton("PTT On")
        bessel_layout.addWidget(self.ptt_button)
        self.save_bessel_button = QPushButton("Save Result")
        self.save_bessel_button.setEnabled(False)
        bessel_layout.addWidget(self.save_bessel_button)
        bessel_layout.addStretch(1)
        self.tone_button.setToolTip(
            "Play the calibration tone through the selected audio output"
        )
        self.ptt_button.setToolTip(
            "Key the transmitter through rigctld; automatically unkeys at the safety timeout"
        )
        self.save_bessel_button.setToolTip(
            "Save a Bessel verification record in the user data directory"
        )
        self.bessel_bar.setVisible(False)
        measure_layout.addWidget(self.bessel_bar)

        content = QHBoxLayout()
        content.setSpacing(8)
        self.meter = AnalogMeter()
        content.addWidget(self.meter, 2)
        right = QVBoxLayout()
        right.setSpacing(8)
        self.digital = DigitalPane()
        right.addWidget(self.digital, 1)
        self.waterfall = WaterfallWidget()
        right.addWidget(self.waterfall, 2)
        content.addLayout(right, 1)
        measure_layout.addLayout(content, 1)
        self.tabs.addTab(measure, "Measure")

        auto_tab = QWidget()
        auto_layout = QVBoxLayout(auto_tab)
        auto_layout.setContentsMargins(8, 8, 8, 8)
        auto_layout.setSpacing(8)

        auto_controls = QHBoxLayout()
        auto_controls.setSpacing(8)
        self.auto_method_label = QLabel("Method")
        self.auto_method_combo = QComboBox()
        self.auto_method_combo.addItem("Bessel null", "bessel")
        self.auto_method_combo.addItem("Target deviation", "target")
        self.auto_tone_label = QLabel("Tone")
        auto_controls.addWidget(self.auto_tone_label)
        self.auto_tone_spin = QDoubleSpinBox()
        self.auto_tone_spin.setRange(0.1, 20.0)
        self.auto_tone_spin.setDecimals(3)
        self.auto_tone_spin.setSuffix(" kHz")
        self.auto_tone_spin.setValue(1.0)
        auto_controls.addWidget(self.auto_tone_spin)
        self.auto_null_label = QLabel("Null")
        auto_controls.addWidget(self.auto_null_label)
        self.auto_null_combo = QComboBox()
        self.auto_null_combo.addItem("1st J0", 1)
        self.auto_null_combo.addItem("2nd J0", 2)
        auto_controls.addWidget(self.auto_null_combo)
        self.auto_target_label = QLabel("Target")
        self.auto_target_spin = QDoubleSpinBox()
        self.auto_target_spin.setRange(0.2, 10.0)
        self.auto_target_spin.setDecimals(3)
        self.auto_target_spin.setSuffix(" kHz")
        self.auto_target_spin.setValue(3.0)
        self.auto_tolerance_label = QLabel("Tolerance")
        self.auto_tolerance_spin = QDoubleSpinBox()
        self.auto_tolerance_spin.setRange(5.0, 500.0)
        self.auto_tolerance_spin.setDecimals(0)
        self.auto_tolerance_spin.setSuffix(" Hz")
        self.auto_tolerance_spin.setValue(50.0)
        self.auto_min_label = QLabel("Level range")
        auto_controls.addWidget(self.auto_min_label)
        self.auto_min_spin = QDoubleSpinBox()
        self.auto_min_spin.setRange(1.0, 99.0)
        self.auto_min_spin.setDecimals(0)
        self.auto_min_spin.setSuffix(" %")
        self.auto_min_spin.setValue(3.0)
        auto_controls.addWidget(self.auto_min_spin)
        self.auto_range_separator = QLabel("to")
        auto_controls.addWidget(self.auto_range_separator)
        self.auto_max_spin = QDoubleSpinBox()
        self.auto_max_spin.setRange(2.0, 100.0)
        self.auto_max_spin.setDecimals(0)
        self.auto_max_spin.setSuffix(" %")
        self.auto_max_spin.setValue(100.0)
        auto_controls.addWidget(self.auto_max_spin)
        auto_controls.addStretch(1)
        self.auto_start_button = QPushButton("Start Auto-Cal")
        self.auto_start_button.setMinimumWidth(140)
        auto_controls.addWidget(self.auto_start_button)
        self.auto_cancel_button = QPushButton("Cancel")
        auto_controls.addWidget(self.auto_cancel_button)
        self.auto_save_button = QPushButton("Save Result")
        auto_controls.addWidget(self.auto_save_button)
        auto_layout.addLayout(auto_controls)

        auto_method_bar = QHBoxLayout()
        auto_method_bar.setSpacing(8)
        auto_method_bar.addWidget(self.auto_method_label)
        auto_method_bar.addWidget(self.auto_method_combo)
        auto_method_bar.addWidget(self.auto_target_label)
        auto_method_bar.addWidget(self.auto_target_spin)
        auto_method_bar.addWidget(self.auto_tolerance_label)
        auto_method_bar.addWidget(self.auto_tolerance_spin)
        self.auto_method_detail_label = QLabel("")
        self.auto_method_detail_label.setStyleSheet("color: #b7bcc2;")
        self.auto_method_detail_label.setWordWrap(False)
        auto_method_bar.addWidget(self.auto_method_detail_label, 1)
        auto_layout.addLayout(auto_method_bar)

        auto_generator_bar = QHBoxLayout()
        auto_generator_bar.setSpacing(8)
        self.auto_generator_label = QLabel("Generator")
        auto_generator_bar.addWidget(self.auto_generator_label)
        self.auto_generator_combo = QComboBox()
        self.auto_generator_combo.addItem("Internal tone", "internal")
        self.auto_generator_combo.addItem(
            "Direwolf steady tone",
            "direwolf",
        )
        self.auto_generator_combo.addItem("xara TUNE", "xara")
        auto_generator_bar.addWidget(self.auto_generator_combo)
        self.direwolf_bar = QWidget()
        direwolf_layout = QHBoxLayout(self.direwolf_bar)
        direwolf_layout.setContentsMargins(0, 0, 0, 0)
        direwolf_layout.setSpacing(8)
        self.direwolf_config_label = QLabel("Config")
        direwolf_layout.addWidget(self.direwolf_config_label)
        self.direwolf_config_edit = QLineEdit()
        self.direwolf_config_edit.setPlaceholderText("path to direwolf.conf")
        self.direwolf_config_edit.setMinimumWidth(260)
        self.direwolf_config_edit.setToolTip(
            "Local Direwolf configuration. In remote mode this is a template: "
            "its ADEVICE must exist on the remote host. Only ADEVICE, ARATE, "
            "ACHANNELS, and MODEM are used."
        )
        direwolf_layout.addWidget(self.direwolf_config_edit, 1)
        self.direwolf_browse_button = QPushButton("Browse…")
        direwolf_layout.addWidget(self.direwolf_browse_button)
        self.direwolf_tone_label = QLabel("Tone")
        direwolf_layout.addWidget(self.direwolf_tone_label)
        self.direwolf_tone_combo = QComboBox()
        self.direwolf_tone_combo.addItem("Mark", "mark")
        self.direwolf_tone_combo.addItem("Space", "space")
        direwolf_layout.addWidget(self.direwolf_tone_combo)
        self.direwolf_info_label = QLabel("Select a config to inspect")
        self.direwolf_info_label.setStyleSheet("color: #b7bcc2;")
        self.direwolf_info_label.setWordWrap(False)
        self.direwolf_info_label.setMinimumWidth(160)
        self.direwolf_info_label.setSizePolicy(
            QSizePolicy.Policy.Expanding,
            QSizePolicy.Policy.Preferred,
        )
        direwolf_layout.addWidget(self.direwolf_info_label, 2)
        self.direwolf_bar.setSizePolicy(
            QSizePolicy.Policy.Expanding,
            QSizePolicy.Policy.Fixed,
        )
        self.direwolf_bar.setVisible(False)
        auto_generator_bar.addWidget(self.direwolf_bar, 1)
        self.xara_bar = QWidget()
        xara_layout = QHBoxLayout(self.xara_bar)
        xara_layout.setContentsMargins(0, 0, 0, 0)
        xara_layout.setSpacing(8)
        self.xara_host_label = QLabel("Host")
        xara_layout.addWidget(self.xara_host_label)
        self.xara_host_edit = QLineEdit("127.0.0.1")
        self.xara_host_edit.setMinimumWidth(130)
        xara_layout.addWidget(self.xara_host_edit)
        self.xara_port_label = QLabel("Command port")
        xara_layout.addWidget(self.xara_port_label)
        self.xara_port_spin = QSpinBox()
        self.xara_port_spin.setRange(1, 65535)
        self.xara_port_spin.setValue(8300)
        xara_layout.addWidget(self.xara_port_spin)
        self.xara_info_label = QLabel(
            "Start xara with --radio and --ptt rigctld; TUNE uses 1500 Hz."
        )
        self.xara_info_label.setStyleSheet("color: #b7bcc2;")
        self.xara_info_label.setToolTip(
            "xara TUNE uses a 1500 Hz ALC test tone and drive from -30 to 0 dB."
        )
        self.xara_info_label.setMinimumWidth(200)
        self.xara_info_label.setSizePolicy(
            QSizePolicy.Policy.Expanding,
            QSizePolicy.Policy.Preferred,
        )
        xara_layout.addWidget(self.xara_info_label, 2)
        self.xara_bar.setSizePolicy(
            QSizePolicy.Policy.Expanding,
            QSizePolicy.Policy.Fixed,
        )
        self.xara_bar.setVisible(False)
        auto_generator_bar.addWidget(self.xara_bar, 1)
        auto_generator_bar.addStretch(1)
        auto_layout.addLayout(auto_generator_bar)

        auto_content = QHBoxLayout()
        auto_content.setSpacing(8)
        self.auto_meter = AnalogMeter()
        auto_content.addWidget(self.auto_meter, 2)
        auto_right = QVBoxLayout()
        auto_right.setSpacing(8)
        self.auto_digital = DigitalPane()
        auto_right.addWidget(self.auto_digital, 1)
        auto_info = QFrame()
        auto_info.setStyleSheet(
            f"QFrame {{ background: {PANEL.name()}; "
            f"border: 1px solid {GRID.name()}; }}"
        )
        auto_info_layout = QVBoxLayout(auto_info)
        auto_info_layout.setContentsMargins(14, 10, 14, 10)
        auto_info_layout.setSpacing(4)
        self.auto_state_label = QLabel("IDLE")
        self.auto_state_label.setStyleSheet("border: none; color: #ffbf47;")
        auto_info_layout.addWidget(self.auto_state_label)
        self.auto_level_label = QLabel("Audio level: —")
        self.auto_level_label.setStyleSheet("border: none; color: #f5f5f5;")
        auto_info_layout.addWidget(self.auto_level_label)
        self.auto_depth_label = QLabel("Best null: —")
        self.auto_depth_label.setStyleSheet("border: none; color: #f5f5f5;")
        auto_info_layout.addWidget(self.auto_depth_label)
        self.auto_progress = QProgressBar()
        self.auto_progress.setRange(0, 1000)
        self.auto_progress.setValue(0)
        self.auto_progress.setTextVisible(False)
        self.auto_progress.setStyleSheet(
            "QProgressBar { border: 1px solid #596575; background: #11171e; }"
            "QProgressBar::chunk { background: #ffbf47; }"
        )
        auto_info_layout.addWidget(self.auto_progress)
        self.auto_detail_label = QLabel(
            "Live SDR + rigctld PTT required. The controller keys briefly, "
            "sweeps the internal tone level, refines the selected J0 null, "
            "then unkeys automatically."
        )
        self.auto_detail_label.setWordWrap(True)
        self.auto_detail_label.setStyleSheet("border: none; color: #b7bcc2;")
        auto_info_layout.addWidget(self.auto_detail_label, 1)
        auto_right.addWidget(auto_info, 2)
        auto_content.addLayout(auto_right, 1)
        auto_layout.addLayout(auto_content, 1)

        self.auto_safety_label = QLabel(
            "Use a dummy load or properly rated attenuator. The rig must be on "
            "the displayed receive frequency in FM; enable Link Rig Frequency "
            "to set the frequency automatically."
        )
        self.auto_safety_label.setWordWrap(True)
        self.auto_safety_label.setStyleSheet("color: #ffbf47;")
        auto_layout.addWidget(self.auto_safety_label)
        self.tabs.addTab(auto_tab, "Auto-Calibrate")

        bottom = QFrame()
        bottom.setStyleSheet(
            f"QFrame {{ background: {PANEL.name()}; border: 1px solid {GRID.name()}; }}"
        )
        bottom_layout = QHBoxLayout(bottom)
        bottom_layout.setContentsMargins(12, 6, 12, 6)
        self.frequency_label = QLabel("Receive frequency")
        self.frequency_label.setStyleSheet("border: none;")
        bottom_layout.addWidget(self.frequency_label)
        self.frequency_edit = QLineEdit()
        self.frequency_edit.setMinimumWidth(190)
        bottom_layout.addWidget(self.frequency_edit)
        self.units_label = QLabel("MHz")
        self.units_label.setStyleSheet("border: none;")
        bottom_layout.addWidget(self.units_label)
        self.read_rig_button = QPushButton("Read from Rig")
        bottom_layout.addWidget(self.read_rig_button)
        self.link_rig = QCheckBox("Link Rig Frequency")
        bottom_layout.addWidget(self.link_rig)
        self.reference_button = QPushButton("Deviation Ref…")
        self.reference_button.setToolTip(
            "Show nominal M17, DMR, and analog FM deviation references and "
            "the Bessel absolute-null relationships."
        )
        bottom_layout.addWidget(self.reference_button)
        self.status_label = QLabel("Idle")
        self.status_label.setStyleSheet("border: none; color: #b7bcc2;")
        bottom_layout.addWidget(self.status_label, 1)
        root_layout.addWidget(bottom)

        self.setCentralWidget(root)
        self.mode_combo.currentIndexChanged.connect(self._on_mode_changed)
        self.profile_combo.currentIndexChanged.connect(self._on_profile_changed)
        self.source_combo.currentIndexChanged.connect(self._on_source_changed)
        self.range_combo.currentIndexChanged.connect(self._on_range_changed)
        self.tone_spin.editingFinished.connect(self._on_bessel_settings_changed)
        self.null_combo.currentIndexChanged.connect(self._on_bessel_settings_changed)
        self.relock_button.clicked.connect(self._relock_bessel_carrier)
        self.manual_capture_button.clicked.connect(self._manual_bessel_capture)
        self.reset_bessel_button.clicked.connect(self._reset_bessel_null)
        self.tone_button.clicked.connect(self._toggle_tone)
        self.ptt_button.clicked.connect(self._toggle_ptt)
        self.save_bessel_button.clicked.connect(self._save_bessel_verification)
        self.auto_start_button.clicked.connect(self._start_auto_calibration)
        self.auto_cancel_button.clicked.connect(self._cancel_auto_calibration_from_button)
        self.auto_save_button.clicked.connect(self._save_auto_verification)
        self.auto_method_combo.currentIndexChanged.connect(
            self._on_auto_method_changed
        )
        self.auto_target_spin.valueChanged.connect(
            self._on_auto_method_changed
        )
        self.auto_generator_combo.currentIndexChanged.connect(
            self._on_auto_generator_changed
        )
        self.auto_tone_spin.editingFinished.connect(self._on_auto_tone_changed)
        self.direwolf_config_edit.editingFinished.connect(
            self._refresh_direwolf_info
        )
        self.direwolf_browse_button.clicked.connect(self._browse_direwolf_config)
        self.direwolf_tone_combo.currentIndexChanged.connect(
            self._refresh_direwolf_info
        )
        self.xara_host_edit.editingFinished.connect(self._refresh_xara_info)
        self.xara_port_spin.valueChanged.connect(self._refresh_xara_info)
        self.start_button.clicked.connect(self._toggle_running)
        self.device_button.clicked.connect(self._edit_device)
        self.read_rig_button.clicked.connect(self._read_from_rig)
        self.reference_button.clicked.connect(self._show_reference_dialog)
        self.frequency_edit.editingFinished.connect(self._commit_frequency)

    def _apply_style(self) -> None:
        self.setStyleSheet(
            """
            QMainWindow, QWidget {
                background: #07090c;
                color: #f5f5f5;
                font-family: "DejaVu Sans";
            }
            QTabWidget::pane {
                border: 1px solid #343b44;
                background: #07090c;
            }
            QTabBar::tab {
                background: #10151b;
                color: #b7bcc2;
                padding: 10px 28px;
                border: 1px solid #343b44;
                border-bottom: none;
            }
            QTabBar::tab:selected {
                background: #07090c;
                color: #ffffff;
            }
            QLineEdit, QComboBox, QDoubleSpinBox, QSpinBox {
                background: #11171e;
                color: #ffffff;
                border: 1px solid #4b5563;
                padding: 5px 8px;
                selection-background-color: #35506d;
            }
            QPushButton {
                background: #161d25;
                color: #ffffff;
                border: 1px solid #596575;
                padding: 7px 14px;
                font-weight: bold;
            }
            QPushButton:hover { background: #23303d; }
            QPushButton:pressed { background: #35506d; }
            QPushButton:disabled {
                background: #0d1117;
                color: #68717c;
                border-color: #343b44;
            }
            QDoubleSpinBox:disabled, QComboBox:disabled, QLineEdit:disabled {
                background: #0d1117;
                color: #68717c;
                border-color: #343b44;
            }
            QCheckBox { spacing: 8px; }
            QLabel { background: transparent; }
            """
        )

    @staticmethod
    def _scaled_font(base_pixels: float, scale: float, *, bold: bool = False, mono: bool = False) -> QFont:
        font = QFont("DejaVu Sans Mono" if mono else "DejaVu Sans")
        font.setPixelSize(max(9, int(round(base_pixels * scale))))
        font.setBold(bold)
        return font

    def _apply_responsive_fonts(self) -> None:
        scale = max(0.8, min(1.8, min(self.width() / 1280.0, self.height() / 720.0)))
        self._ui_scale = scale
        if hasattr(self, "digital"):
            self.digital.set_ui_scale(scale)
            self.auto_digital.set_ui_scale(scale)
        control_font = self._scaled_font(14.0, scale)
        for widget in (
            self.mode_label,
            self.mode_combo,
            self.profile_label,
            self.source_label_widget,
            self.range_label,
            self.profile_combo,
            self.source_combo,
            self.range_combo,
            self.device_button,
            self.start_button,
            self.read_rig_button,
            self.reference_button,
            self.link_rig,
            self.frequency_label,
            self.units_label,
            self.status_label,
            self.tone_label,
            self.tone_spin,
            self.null_label,
            self.null_combo,
            self.relock_button,
            self.manual_capture_button,
            self.reset_bessel_button,
            self.tone_button,
            self.ptt_button,
            self.save_bessel_button,
            self.auto_method_label,
            self.auto_method_combo,
            self.auto_method_detail_label,
            self.auto_tone_label,
            self.auto_tone_spin,
            self.auto_null_label,
            self.auto_null_combo,
            self.auto_target_label,
            self.auto_target_spin,
            self.auto_tolerance_label,
            self.auto_tolerance_spin,
            self.auto_min_label,
            self.auto_min_spin,
            self.auto_range_separator,
            self.auto_max_spin,
            self.auto_start_button,
            self.auto_cancel_button,
            self.auto_save_button,
            self.auto_generator_label,
            self.auto_generator_combo,
            self.direwolf_config_label,
            self.direwolf_config_edit,
            self.direwolf_browse_button,
            self.direwolf_tone_label,
            self.direwolf_tone_combo,
            self.direwolf_info_label,
            self.xara_host_label,
            self.xara_host_edit,
            self.xara_port_label,
            self.xara_port_spin,
            self.xara_info_label,
            self.auto_safety_label,
        ):
            widget.setFont(control_font)
        self.frequency_edit.setFont(
            self._scaled_font(27.0, scale, bold=True, mono=True)
        )
        self.tabs.tabBar().setFont(self._scaled_font(18.0, scale, bold=True))
        self.auto_state_label.setFont(
            self._scaled_font(22.0, scale, bold=True)
        )
        self.auto_level_label.setFont(self._scaled_font(16.0, scale))
        self.auto_depth_label.setFont(self._scaled_font(16.0, scale))
        self.auto_detail_label.setFont(self._scaled_font(13.0, scale))

    def resizeEvent(self, event) -> None:  # noqa: N802
        super().resizeEvent(event)
        scale = max(0.8, min(1.8, min(self.width() / 1280.0, self.height() / 720.0)))
        if abs(scale - self._ui_scale) >= 0.02:
            self._apply_responsive_fonts()

    def _load_ui_settings(self) -> None:
        store = self._settings_store
        profile = str(store.value("profile", "m17"))
        index = self.profile_combo.findData(profile)
        if index >= 0:
            self.profile_combo.setCurrentIndex(index)
        source = str(store.value("source", SOURCE_SIMULATOR))
        index = self.source_combo.findData(source)
        if index >= 0:
            self.source_combo.setCurrentIndex(index)
        self.tone_spin.setValue(float(store.value("bessel_tone_khz", 1.0)))
        null_index = int(store.value("bessel_null_index", 1))
        index = self.null_combo.findData(null_index)
        if index >= 0:
            self.null_combo.setCurrentIndex(index)
        self.auto_tone_spin.setValue(float(store.value("auto_tone_khz", 1.0)))
        self.auto_target_spin.setValue(float(store.value("auto_target_khz", 3.0)))
        self.auto_tolerance_spin.setValue(
            float(store.value("auto_tolerance_hz", 50.0))
        )
        auto_method = str(store.value("auto_method", "bessel"))
        method_index = self.auto_method_combo.findData(auto_method)
        if method_index >= 0:
            self.auto_method_combo.setCurrentIndex(method_index)
        auto_null_index = int(store.value("auto_null_index", 1))
        index = self.auto_null_combo.findData(auto_null_index)
        if index >= 0:
            self.auto_null_combo.setCurrentIndex(index)
        self.auto_min_spin.setValue(float(store.value("auto_min_level", 3.0)))
        self.auto_max_spin.setValue(float(store.value("auto_max_level", 100.0)))
        self.direwolf_config_edit.setText(
            str(store.value("direwolf_config", ""))
        )
        self.xara_host_edit.setText(
            str(store.value("xara_host", "127.0.0.1"))
        )
        self.xara_port_spin.setValue(int(store.value("xara_port", 8300)))
        direwolf_tone = str(store.value("direwolf_tone", "mark"))
        tone_index = self.direwolf_tone_combo.findData(direwolf_tone)
        if tone_index >= 0:
            self.direwolf_tone_combo.setCurrentIndex(tone_index)
        auto_generator = str(store.value("auto_generator", "internal"))
        generator_index = self.auto_generator_combo.findData(auto_generator)
        if generator_index >= 0:
            self.auto_generator_combo.setCurrentIndex(generator_index)
        mode = str(store.value("mode", "digital"))
        index = self.mode_combo.findData(mode)
        if index >= 0:
            self.mode_combo.setCurrentIndex(index)
        maximum = float(store.value("meter_range", 3000.0))
        index = self.range_combo.findData(maximum)
        if index >= 0 and not self._is_bessel_mode():
            self.range_combo.setCurrentIndex(index)
        frequency = float(store.value("frequency_mhz", 443.457))
        self.frequency_edit.setText(f"{frequency:.6f}")
        self._frequency_hz = frequency * 1e6
        self.link_rig.setChecked(_settings_bool(store.value("link_rig", False)))
        self.meter.set_range_hz(float(self.range_combo.currentData()))
        self._update_mode_controls()
        self._on_auto_generator_changed()
        self._on_auto_method_changed()
        self._update_auto_control_state()

    def _save_ui_settings(self) -> None:
        store = self._settings_store
        store.setValue("profile", self.profile_combo.currentData())
        store.setValue("source", self.source_combo.currentData())
        store.setValue("mode", self.mode_combo.currentData())
        store.setValue("bessel_tone_khz", self.tone_spin.value())
        store.setValue("bessel_null_index", self.null_combo.currentData())
        store.setValue("auto_tone_khz", self.auto_tone_spin.value())
        store.setValue("auto_method", self.auto_method_combo.currentData())
        store.setValue("auto_target_khz", self.auto_target_spin.value())
        store.setValue("auto_tolerance_hz", self.auto_tolerance_spin.value())
        store.setValue("auto_null_index", self.auto_null_combo.currentData())
        store.setValue("auto_min_level", self.auto_min_spin.value())
        store.setValue("auto_max_level", self.auto_max_spin.value())
        store.setValue("auto_generator", self.auto_generator_combo.currentData())
        store.setValue(
            "direwolf_config",
            self.direwolf_config_edit.text().strip(),
        )
        store.setValue(
            "direwolf_tone",
            self.direwolf_tone_combo.currentData(),
        )
        store.setValue("xara_host", self.xara_host_edit.text().strip())
        store.setValue("xara_port", self.xara_port_spin.value())

        store.setValue("meter_range", float(self.range_combo.currentData()))
        store.setValue("frequency_mhz", self._frequency_hz / 1e6)
        store.setValue("link_rig", self.link_rig.isChecked())
        store.setValue("device_args", self._device.device_args)
        store.setValue("sample_rate_hz", self._device.sample_rate_hz)
        store.setValue("lo_offset_hz", self._device.lo_offset_hz)
        store.setValue("gain_db", self._device.gain_db)
        store.setValue("bandwidth_hz", self._device.bandwidth_hz)
        store.setValue("audio_output", self._device.audio_output)
        store.setValue("tone_level", self._device.tone_level)
        store.setValue("ptt_timeout_s", self._device.ptt_timeout_s)
        store.setValue("rig_host", self._device.rig_host)
        store.setValue("rig_port", self._device.rig_port)
        store.setValue("direwolf_remote", self._device.direwolf_remote)
        store.setValue("direwolf_ssh_host", self._device.direwolf_ssh_host)
        store.setValue(
            "direwolf_remote_rig_host",
            self._device.direwolf_remote_rig_host,
        )
        store.setValue(
            "direwolf_remote_rig_port",
            self._device.direwolf_remote_rig_port,
        )

    def _is_bessel_mode(self) -> bool:
        return self.mode_combo.currentData() == "bessel"

    def _on_mode_changed(self) -> None:
        if self._auto_running:
            self._cancel_auto_calibration("cancelled by measurement mode change")
        self._update_mode_controls()
        if self._session is not None and self._session.running:
            self.start_measurement()
        elif self._is_bessel_mode():
            self._reset_bessel_display()

    def _update_mode_controls(self) -> None:
        bessel = self._is_bessel_mode()
        self.profile_label.setVisible(not bessel)
        self.profile_combo.setVisible(not bessel)
        self.bessel_bar.setVisible(bessel)
        if bessel:
            self._select_bessel_range()
        else:
            self._stop_tone()
            self._stop_ptt()
        self._update_bessel_control_state()
        self._update_measurement_reference()

    def _update_bessel_control_state(self) -> None:
        bessel_running = (
            self._session is not None
            and self._session.running
            and self._is_bessel_mode()
        )
        automatic = self._auto_running
        live = self.source_combo.currentData() == SOURCE_LIVE
        for button in (
            self.relock_button,
            self.manual_capture_button,
            self.reset_bessel_button,
        ):
            button.setEnabled(bessel_running and not automatic)
        self.tone_button.setEnabled(bessel_running and live and not automatic)
        self.ptt_button.setEnabled(bessel_running and live and not automatic)
        self.save_bessel_button.setEnabled(
            bessel_running
            and not automatic
            and self._bessel_progress is not None
            and self._bessel_progress.captured is not None
        )
        self._update_auto_control_state()

    def _update_auto_control_state(self) -> None:
        if not hasattr(self, "auto_start_button"):
            return
        running = self._auto_running
        source = self.source_combo.currentData()
        live = source == SOURCE_LIVE
        generator = (
            self.auto_generator_combo.currentData()
            if hasattr(self, "auto_generator_combo")
            else "internal"
        )
        rehearsal = source == SOURCE_SIMULATOR and generator == "internal"
        generator_ready = True
        if generator == "direwolf":
            generator_ready = (
                self._direwolf_profile is not None
                and self._direwolf_profile_error is None
            )
            if self._device.direwolf_remote:
                generator_ready = generator_ready and bool(
                    self._device.direwolf_ssh_host.strip()
                    and self._device.direwolf_remote_rig_host.strip()
                )
        elif generator == "xara":
            generator_ready = bool(self.xara_host_edit.text().strip())
        self.auto_start_button.setEnabled(
            (live or rehearsal) and not running and generator_ready
        )
        if source == SOURCE_SIMULATOR:
            self.auto_safety_label.setText(
                "Simulator rehearsal: no RF, PTT, or hardware record is used. "
                "Select Live SDR for an actual calibration."
            )
        else:
            self.auto_safety_label.setText(
                "Use a dummy load or properly rated attenuator. The rig must be "
                "on the displayed receive frequency in FM; enable Link Rig "
                "Frequency to set the frequency automatically."
            )
        self.auto_cancel_button.setEnabled(running)
        captured = (
            self._auto_progress is not None
            and self._auto_progress.captured is not None
        ) or (
            self._target_progress is not None
            and self._target_progress.reading is not None
            and self._target_progress.reading.valid
        )
        self.auto_save_button.setEnabled(
            captured and not running and not self._auto_run_rehearsal
        )
        for control in (
            self.auto_method_combo,
            self.auto_null_combo,
            self.auto_target_spin,
            self.auto_tolerance_spin,
            self.auto_min_spin,
            self.auto_max_spin,
            self.auto_generator_combo,
            self.direwolf_config_edit,
            self.direwolf_browse_button,
            self.direwolf_tone_combo,
            self.xara_host_edit,
            self.xara_port_spin,
        ):
            control.setEnabled(not running)
        self.auto_tone_spin.setEnabled(
            not running and generator == "internal"
        )

    def _on_auto_method_changed(self) -> None:
        target = (
            self.auto_method_combo.currentData() == "target"
            if hasattr(self, "auto_method_combo")
            else False
        )
        self.auto_null_label.setVisible(not target)
        self.auto_null_combo.setVisible(not target)
        self.auto_target_label.setVisible(target)
        self.auto_target_spin.setVisible(target)
        self.auto_tolerance_label.setVisible(target)
        self.auto_tolerance_spin.setVisible(target)
        if target:
            target_hz = self.auto_target_spin.value() * 1e3
            self._set_auto_meter_target(target_hz)
            self.auto_method_detail_label.setText(
                "Discriminator target · validate with Bessel"
            )
            self.auto_depth_label.setText(
                f"Target: {target_hz / 1000.0:.3f} kHz"
            )
            self.auto_detail_label.setText(
                "Keys PTT, measures Generic FM deviation, and adjusts the "
                "audio level to the target. This is a discriminator result; "
                "validate absolute accuracy with a Bessel null."
            )
        else:
            self.auto_method_detail_label.setText(
                "Exact absolute reference from selected J0 null"
            )
            self.auto_depth_label.setText("Best null: —")
            self.auto_detail_label.setText(
                "Live SDR + rigctld PTT required. The controller keys "
                "briefly, sweeps the internal tone level, refines the "
                "selected J0 null, then unkeys automatically."
            )
            if self.auto_generator_combo.currentData() == "xara":
                self._set_auto_meter_range(1_500.0, 1)
            elif self.auto_generator_combo.currentData() == "direwolf":
                self._refresh_direwolf_info()
            else:
                self._set_auto_meter_range(
                    self.auto_tone_spin.value() * 1e3,
                    int(self.auto_null_combo.currentData()),
                )
        self._update_auto_control_state()

    def _on_auto_generator_changed(self) -> None:
        generator = self.auto_generator_combo.currentData()
        direwolf = generator == "direwolf"
        xara = generator == "xara"
        self.direwolf_bar.setVisible(direwolf)
        self.xara_bar.setVisible(xara)
        if direwolf:
            self._refresh_direwolf_info()
        elif xara:
            self._direwolf_profile = None
            self._direwolf_profile_error = None
            self.auto_tone_spin.setValue(1.5)
            self._refresh_xara_info()
            self._set_auto_meter_range(
                1_500.0,
                int(self.auto_null_combo.currentData()),
            )
        else:
            self._direwolf_profile = None
            self._direwolf_profile_error = None
            self.direwolf_info_label.setStyleSheet("color: #b7bcc2;")
            self.direwolf_info_label.setText("Using the internal tone generator")
            self._set_auto_meter_range(
                self.auto_tone_spin.value() * 1e3,
                int(self.auto_null_combo.currentData()),
            )
        self._update_auto_control_state()

    def _on_auto_tone_changed(self) -> None:
        if self.auto_generator_combo.currentData() == "internal":
            self._set_auto_meter_range(
                self.auto_tone_spin.value() * 1e3,
                int(self.auto_null_combo.currentData()),
            )

    def _refresh_xara_info(self) -> None:
        if not hasattr(self, "xara_info_label"):
            return
        self.xara_info_label.setText(
            f"{self.xara_host_edit.text().strip() or '127.0.0.1'}:"
            f"{self.xara_port_spin.value()}"
        )
        self._update_auto_control_state()

    def _browse_direwolf_config(self) -> None:
        start = self.direwolf_config_edit.text().strip() or str(Path.home())
        path, _ = QFileDialog.getOpenFileName(
            self,
            "Select Direwolf configuration",
            start,
            "Direwolf config (*.conf);;All files (*)",
        )
        if path:
            self.direwolf_config_edit.setText(path)
            self._refresh_direwolf_info()

    def _refresh_direwolf_info(self) -> None:
        path_text = self.direwolf_config_edit.text().strip()
        self._direwolf_profile = None
        self._direwolf_profile_error = None
        if not path_text:
            self._direwolf_profile_error = "select a Direwolf config"
            self.direwolf_info_label.setStyleSheet("color: #ffbf47;")
            self.direwolf_info_label.setText(
                "Stop the normal Direwolf instance before starting; calibration "
                "needs exclusive audio."
            )
            self._update_auto_control_state()
            return
        try:
            profile = parse_direwolf_config(Path(path_text))
            tone = str(self.direwolf_tone_combo.currentData())
            tone_hz = profile_tone_hz(profile, tone)
        except DirewolfError as exc:
            self._direwolf_profile_error = str(exc)
            self.direwolf_info_label.setStyleSheet("color: #ff6868;")
            self.direwolf_info_label.setText(str(exc))
            self._update_auto_control_state()
            return
        self._direwolf_profile = profile
        self.auto_tone_spin.setValue(tone_hz / 1e3)
        self._set_auto_meter_range(
            tone_hz,
            int(self.auto_null_combo.currentData()),
        )
        remote = self._device.direwolf_remote
        remote_host = self._device.direwolf_ssh_host.strip()
        if remote and not remote_host:
            self._direwolf_profile_error = (
                "configure the remote Direwolf SSH host in SDR / Rig"
            )
            self.direwolf_info_label.setStyleSheet("color: #ffbf47;")
            self.direwolf_info_label.setText(
                "Remote Direwolf is enabled but the SSH host is empty"
            )
            self._update_auto_control_state()
            return
        if remote and not self._device.direwolf_remote_rig_host.strip():
            self._direwolf_profile_error = (
                "configure the remote rigctld host in SDR / Rig"
            )
            self.direwolf_info_label.setStyleSheet("color: #ffbf47;")
            self.direwolf_info_label.setText(
                "Remote Direwolf is enabled but the rigctld host is empty"
            )
            self._update_auto_control_state()
            return
        self.direwolf_info_label.setStyleSheet("color: #b7bcc2;")
        if remote:
            self.direwolf_info_label.setStyleSheet("color: #ffbf47;")
        prefix = f"REMOTE {remote_host} · " if remote else ""
        self.direwolf_info_label.setText(
            f"{prefix}{profile.baud} bd · {tone} {tone_hz / 1e3:.3g} kHz · "
            f"{profile.audio_device}"
        )
        remote_note = (
            " Remote mode: the config file is a local template; its ADEVICE "
            "must exist on the remote host, and remote Direwolf must be stopped. "
            f"{_REMOTE_HELP_TEXT}"
            if remote
            else ""
        )
        self.direwolf_info_label.setToolTip(
            f"Mark {profile.mark_hz:g} Hz, space {profile.space_hz:g} Hz. "
            f"Calibration will use {tone_hz:g} Hz and temporary ADEVICE "
            f"{profile.audio_device}. Stop any running Direwolf first."
            f"{remote_note}"
        )
        self._update_auto_control_state()

    def _select_bessel_range(self) -> None:
        tone_hz = self.tone_spin.value() * 1e3
        null_index = int(self.null_combo.currentData())
        target = carrier_null_index(null_index) * tone_hz
        for index in range(self.range_combo.count()):
            maximum = float(self.range_combo.itemData(index))
            if maximum >= target * 1.05:
                self.range_combo.setCurrentIndex(index)
                return
        self.range_combo.setCurrentIndex(self.range_combo.count() - 1)

    def _on_bessel_settings_changed(self) -> None:
        if not self._is_bessel_mode():
            return
        self._select_bessel_range()
        self._reset_bessel_display()
        if self._session is not None and self._session.running:
            self.start_measurement()

    def _bessel_tone_hz(self) -> float:
        return self.tone_spin.value() * 1e3

    def _null_index(self) -> int:
        return int(self.null_combo.currentData())

    def _reset_bessel_display(self) -> None:
        self._bessel_detector = BesselNullDetector(
            tone_frequency_hz=self._bessel_tone_hz(),
            null_index=self._null_index(),
            sample_rate_hz=48_000.0,
        )
        self._bessel_spectrum_sequence = -1
        self._bessel_progress = BesselProgress(
            state="WAITING FOR CARRIER",
            carrier_locked=False,
            tone_frequency_hz=self._bessel_tone_hz(),
            null_index=self._null_index(),
        )
        self.digital.set_bessel(self._bessel_progress)
        self.meter.set_value(None, "WAITING")
        self.meter.set_reference_marks(
            bessel_reference(
                self._null_index(),
                self._bessel_tone_hz(),
            ).meter_marks
        )
        if hasattr(self, "save_bessel_button"):
            self.save_bessel_button.setEnabled(False)
        if self._session is None:
            self.status_label.setText("Bessel: waiting for receiver")

    def _relock_bessel_carrier(self) -> None:
        if self._session is None or self._bessel_detector is None:
            self.status_label.setText("Bessel: receiver is not running")
            return
        sequence, spectrum = self._session.state.spectrum_snapshot()
        progress = self._bessel_detector.lock_carrier(spectrum)
        self._bessel_spectrum_sequence = sequence
        self._show_bessel_progress(progress, spectrum)

    def _manual_bessel_capture(self) -> None:
        if self._bessel_detector is None:
            return
        progress = self._bessel_detector.capture_manual()
        _, spectrum = (
            self._session.state.spectrum_snapshot()
            if self._session is not None
            else (0, tuple([-120.0] * 1024))
        )
        self._show_bessel_progress(progress, spectrum)

    def _reset_bessel_null(self) -> None:
        self._reset_bessel_display()
        if self._session is not None and self._bessel_detector is not None:
            sequence, spectrum = self._session.state.spectrum_snapshot()
            progress = self._bessel_detector.lock_carrier(spectrum)
            self._bessel_spectrum_sequence = sequence
            self._show_bessel_progress(progress, spectrum)

    def _toggle_tone(self) -> None:
        if self._tone_player.active:
            self._stop_tone()
            return
        if not self.worker_is_running() or not self._is_bessel_mode():
            self.status_label.setText("Start the Bessel receiver before enabling the tone")
            return
        try:
            self._tone_player.start(
                frequency_hz=self._bessel_tone_hz(),
                device_name=self._device.audio_output,
                duration_s=max(10.0, self._device.ptt_timeout_s + 2.0),
                level=self._device.tone_level,
            )
        except AudioToneError as exc:
            self.status_label.setText(f"Tone output failed: {exc}")
            return
        self.tone_button.setText("Tone Off")
        self.status_label.setText(
            f"Tone output {self._bessel_tone_hz() / 1e3:.3f} kHz enabled"
        )

    def _on_tone_finished(self) -> None:
        self.tone_button.setText("Tone On")

    def _stop_tone(self) -> None:
        if hasattr(self, "_tone_player") and self._tone_player.active:
            self._tone_player.stop()
        if hasattr(self, "tone_button"):
            self.tone_button.setText("Tone On")

    def _key_ptt(self) -> bool:
        if self._ptt_keyed:
            return True
        try:
            confirmed = RigctldClient(
                self._device.rig_host,
                self._device.rig_port,
            ).set_ptt(True)
        except RigctldError as exc:
            self.status_label.setText(f"PTT failed: {exc}")
            return False
        if not confirmed:
            self._stop_tone()
            try:
                RigctldClient(
                    self._device.rig_host,
                    self._device.rig_port,
                ).set_ptt(False)
            except RigctldError:
                pass
            self.status_label.setText("PTT readback did not confirm transmission")
            return False
        self._ptt_keyed = True
        self.ptt_button.setText("PTT Off")
        self._ptt_timer.start(int(round(self._device.ptt_timeout_s * 1000.0)))
        self.status_label.setText(
            f"PTT ON — automatic unkey in {self._device.ptt_timeout_s:.0f} s"
        )
        return True

    def _unkey_ptt(self, reason: str | None = None) -> bool:
        if hasattr(self, "_ptt_timer"):
            self._ptt_timer.stop()
        was_keyed = getattr(self, "_ptt_keyed", False)
        self._ptt_keyed = False
        if hasattr(self, "ptt_button"):
            self.ptt_button.setText("PTT On")
        if not was_keyed:
            return True
        last_error: str | None = None
        for attempt in range(8):
            try:
                if RigctldClient(
                    self._device.rig_host,
                    self._device.rig_port,
                ).set_ptt(False):
                    last_error = "readback still reports keyed"
                else:
                    last_error = None
                    break
            except RigctldError as exc:
                last_error = str(exc)
            if attempt < 7:
                time.sleep(0.25)
        if last_error is not None:
            self.status_label.setText(
                f"PTT UNKEY FAILED — CHECK RADIO IMMEDIATELY: {last_error}"
            )
            return False
        if reason:
            self.status_label.setText(reason)
        else:
            self.status_label.setText("PTT OFF")
        return True

    def _stop_ptt(self, reason: str | None = None) -> None:
        self._unkey_ptt(reason)

    def _toggle_ptt(self) -> None:
        if self._ptt_keyed:
            self._stop_ptt()
            return
        if not self.worker_is_running() or not self._is_bessel_mode():
            self.status_label.setText("Start the Bessel receiver before enabling PTT")
            return
        if self.source_combo.currentData() != SOURCE_LIVE:
            self.status_label.setText("PTT is disabled while using the simulator")
            return
        self._key_ptt()

    def _ptt_timeout(self) -> None:
        message = "PTT safety timeout — transmitter unkeyed"
        if self._auto_running:
            self._cancel_auto_calibration(message)
        self._stop_tone()
        self._stop_ptt(message)

    def _save_verification(
        self,
        progress: BesselProgress,
        *,
        notes: str,
    ) -> None:
        if progress.captured is None:
            self.status_label.setText("No captured Bessel result to save")
            return
        record = BesselVerificationRecord(
            receive_frequency_hz=self._frequency_hz,
            tone_frequency_hz=progress.tone_frequency_hz,
            null_index=progress.null_index,
            reading=progress.captured,
            device={
                "device_args": self._device.device_args,
                "sample_rate_hz": self._device.sample_rate_hz,
                "center_frequency_hz": self._frequency_hz + self._device.lo_offset_hz,
                "gain_db": self._device.gain_db,
                "bandwidth_hz": self._device.bandwidth_hz,
                "rig_host": self._device.rig_host,
                "rig_port": self._device.rig_port,
                "direwolf_remote": self._device.direwolf_remote,
                "direwolf_ssh_host": self._device.direwolf_ssh_host,
                "remote_rig_host": self._device.direwolf_remote_rig_host,
                "remote_rig_port": self._device.direwolf_remote_rig_port,
            },
            audio_output=(
                self._device.audio_output or TonePlayer.default_output_name()
            ),
            notes=notes,
        )
        try:
            path = save_bessel_verification(record)
        except (OSError, CalibrationError) as exc:
            self.status_label.setText(f"Could not save verification: {exc}")
            return
        self.status_label.setText(f"Verification saved: {path}")

    def _save_bessel_verification(self) -> None:
        progress = self._bessel_progress
        if progress is None:
            self.status_label.setText("No captured Bessel result to save")
            return
        self._save_verification(
            progress,
            notes="Live Bessel carrier-null verification",
        )

    def _save_auto_verification(self) -> None:
        if self._auto_run_rehearsal:
            self.status_label.setText(
                "Simulator rehearsal results are not saved as hardware records"
            )
            return
        if self._auto_run_method == "target":
            progress = self._target_progress
            if (
                progress is None
                or progress.reading is None
                or not progress.reading.valid
            ):
                self.status_label.setText("No target calibration result to save")
                return
            record = TargetDeviationRecord(
                receive_frequency_hz=self._frequency_hz,
                target_deviation_hz=progress.target_hz,
                reading=progress.reading,
                device={
                    "device_args": self._device.device_args,
                    "sample_rate_hz": self._device.sample_rate_hz,
                    "center_frequency_hz": (
                        self._frequency_hz + self._device.lo_offset_hz
                    ),
                    "gain_db": self._device.gain_db,
                    "bandwidth_hz": self._device.bandwidth_hz,
                    "rig_host": self._device.rig_host,
                    "rig_port": self._device.rig_port,
                    "direwolf_remote": self._device.direwolf_remote,
                    "direwolf_ssh_host": self._device.direwolf_ssh_host,
                    "remote_rig_host": self._device.direwolf_remote_rig_host,
                    "remote_rig_port": self._device.direwolf_remote_rig_port,
                },
                source=self._auto_run_generator,
                audio_output=(
                    self._device.audio_output or TonePlayer.default_output_name()
                ),
                notes=(
                    f"{self._auto_run_notes}; achieved "
                    f"{progress.reading.deviation_hz:.0f} Hz; "
                    f"error {progress.error_hz:+.0f} Hz"
                ),
            )
            try:
                path = save_target_deviation(record)
            except (OSError, CalibrationError) as exc:
                self.status_label.setText(f"Could not save target: {exc}")
                return
            self.status_label.setText(f"Target calibration saved: {path}")
            return
        progress = self._auto_progress
        if progress is None or progress.bessel is None:
            self.status_label.setText("No auto-calibration result to save")
            return
        if progress.best_level is None:
            level = "unknown"
        elif self._auto_run_generator == "xara":
            level = f"{round(-30.0 + 30.0 * progress.best_level)} dB drive"
        else:
            level = f"{progress.best_level * 100.0:.1f}%"
        notes = (
            f"{self._auto_run_notes}; refined level {level}"
            if self._auto_run_notes
            else f"Auto-calibrated Bessel carrier null; refined level {level}"
        )
        if "level-range limit" in progress.message:
            notes += "; null was at a level-range limit"
        self._save_verification(
            progress.bessel,
            notes=notes,
        )

    def _start_auto_calibration(self) -> None:
        if self._auto_running:
            return
        if self.auto_generator_combo.currentData() == "direwolf":
            self._refresh_direwolf_info()
        if self.source_combo.currentData() == SOURCE_SIMULATOR:
            self._start_auto_rehearsal()
            return
        if self.source_combo.currentData() != SOURCE_LIVE:
            self.status_label.setText(
                "Auto-calibration requires Live SDR and rigctld PTT"
            )
            return
        self._auto_run_rehearsal = False
        if self._device.ptt_timeout_s < 19.0:
            self.status_label.setText(
                "Set the PTT safety timeout to at least 19 s for auto-calibration"
            )
            return
        minimum = self.auto_min_spin.value() / 100.0
        maximum = self.auto_max_spin.value() / 100.0
        if minimum >= maximum:
            self.status_label.setText(
                "Auto-calibration level range must have a lower minimum"
            )
            return
        self._stop_tone()
        if self._ptt_keyed and not self._unkey_ptt("PTT OFF"):
            self.status_label.setText(
                "Cannot start auto-calibration while PTT cannot be released"
            )
            return
        generator = str(self.auto_generator_combo.currentData())
        remote_direwolf = (
            self._device.direwolf_remote and generator == "direwolf"
        )
        frequency = self._prepare_auto_frequency(remote=remote_direwolf)
        if frequency is None:
            return
        self._frequency_hz = frequency
        method = str(self.auto_method_combo.currentData())
        self._auto_run_method = method
        if method == "target":
            if not self._ensure_generic_session(frequency):
                return
        else:
            if not self._ensure_bessel_session(frequency):
                return
        maximum_duration = max(
            5.0,
            min(18.0, self._device.ptt_timeout_s - 2.0),
        )
        self._auto_run_generator = generator
        prepared = self._prepare_auto_io(generator, method)
        if prepared is None:
            return
        tone_hz, io, notes = prepared
        self._auto_run_notes = notes

        if method == "target":
            self._auto_progress = None
            target_hz = self.auto_target_spin.value() * 1e3
            self._set_auto_meter_target(target_hz)
            try:
                config = TargetCalConfig(
                    target_hz=target_hz,
                    tolerance_hz=self.auto_tolerance_spin.value(),
                    min_level=minimum,
                    max_level=maximum,
                    max_duration_s=maximum_duration,
                )
            except TargetCalError as exc:
                self.status_label.setText(f"Target calibration settings: {exc}")
                return
            controller = TargetCalController(config, io)
            self._target_controller = controller
            self._auto_controller = None
            self._target_snapshot_timestamp = 0.0
            self._auto_running = True
            self._target_progress = controller.start()
            self._render_target_progress(self._target_progress)
            self._update_bessel_control_state()
            if self._target_progress.state.terminal:
                self._finish_auto_run()
            return

        self._target_controller = None
        self._target_progress = None
        self._set_auto_meter_range(
            tone_hz,
            int(self.auto_null_combo.currentData()),
        )
        try:
            config = AutoCalConfig(
                tone_frequency_hz=tone_hz,
                null_index=int(self.auto_null_combo.currentData()),
                min_level=minimum,
                max_level=maximum,
                carrier_timeout_s=min(4.0, max(1.0, maximum_duration / 3.0)),
                max_duration_s=maximum_duration,
            )
        except AutoCalError as exc:
            self.status_label.setText(f"Auto-calibration settings: {exc}")
            return
        controller = AutoCalController(
            config,
            io,
            detector=BesselNullDetector(
                tone_frequency_hz=config.tone_frequency_hz,
                null_index=config.null_index,
                sample_rate_hz=48_000.0,
            ),
        )
        self._auto_controller = controller
        self._auto_running = True
        self._auto_progress = controller.start()
        self._render_auto_progress(self._auto_progress)
        self._update_bessel_control_state()
        if self._auto_progress.state.terminal:
            self._finish_auto_run()

    def _ensure_bessel_session(self, frequency: float) -> bool:
        self.mode_combo.blockSignals(True)
        index = self.mode_combo.findData("bessel")
        if index >= 0:
            self.mode_combo.setCurrentIndex(index)
        self.mode_combo.blockSignals(False)
        self._update_mode_controls()
        if self._session is not None and self._session.running:
            config = self._session.config
            if (
                config.mode == "bessel"
                and not config.simulator
                and abs(config.signal_frequency_hz - frequency) <= 1.0
            ):
                return True
        self.start_measurement()
        return self._session is not None and self._session.running

    def _ensure_generic_session(self, frequency: float) -> bool:
        self.mode_combo.blockSignals(True)
        mode_index = self.mode_combo.findData("digital")
        if mode_index >= 0:
            self.mode_combo.setCurrentIndex(mode_index)
        self.mode_combo.blockSignals(False)
        self.profile_combo.blockSignals(True)
        profile_index = self.profile_combo.findData("generic-fm")
        if profile_index >= 0:
            self.profile_combo.setCurrentIndex(profile_index)
        self.profile_combo.blockSignals(False)
        self._update_mode_controls()
        if self._session is not None and self._session.running:
            config = self._session.config
            if (
                config.mode == "digital"
                and config.profile_name == "generic-fm"
                and not config.simulator
                and abs(config.signal_frequency_hz - frequency) <= 1.0
            ):
                return True
        self.start_measurement()
        return self._session is not None and self._session.running

    def _prepare_auto_io(self, generator: str, method: str):
        purpose = (
            "discriminator target"
            if method == "target"
            else "Bessel carrier null"
        )
        self._direwolf_calibration = None
        self._remote_direwolf_calibration = None
        self._xara_calibration = None
        if generator == "direwolf":
            profile = self._direwolf_profile
            if profile is None or self._direwolf_profile_error is not None:
                self.status_label.setText(
                    self._direwolf_profile_error
                    or "Select a valid Direwolf configuration first"
                )
                return None
            tone = str(self.direwolf_tone_combo.currentData())
            try:
                tone_hz = profile_tone_hz(profile, tone)
            except DirewolfError as exc:
                self.status_label.setText(str(exc))
                return None
            if self._device.direwolf_remote:
                ssh_host = self._device.direwolf_ssh_host.strip()
                remote_rig_host = (
                    self._device.direwolf_remote_rig_host.strip()
                )
                if not ssh_host:
                    self.status_label.setText(
                        "Configure the remote Direwolf SSH host in SDR / Rig"
                    )
                    return None
                if not remote_rig_host:
                    self.status_label.setText(
                        "Configure the remote rigctld host in SDR / Rig"
                    )
                    return None
                try:
                    adapter = RemoteDirewolfCalibration(
                        RemoteDirewolfConfig(
                            ssh_host=ssh_host,
                            base_config=Path(
                                self.direwolf_config_edit.text().strip()
                            ),
                            tone=tone,
                            remote_rig_host=remote_rig_host,
                            remote_rig_port=(
                                self._device.direwolf_remote_rig_port
                            ),
                            max_duration_s=max(
                                5.0,
                                min(35.0, self._device.ptt_timeout_s + 3.0),
                            ),
                        )
                    )
                except (DirewolfError, SshError) as exc:
                    self.status_label.setText(
                        f"Remote Direwolf settings: {exc}"
                    )
                    return None
                self._remote_direwolf_calibration = adapter
                notes = (
                    f"Auto-calibrated {purpose} via remote Direwolf "
                    f"{profile.baud} baud {tone} tone {tone_hz:g} Hz on "
                    f"{ssh_host}; ADEVICE {profile.audio_device}; "
                    f"remote PTT {remote_rig_host}:"
                    f"{self._device.direwolf_remote_rig_port}"
                )
                return (
                    tone_hz,
                    _RemoteDirewolfAutoCalHardware(self, adapter),
                    notes,
                )
            adapter = DirewolfCalibration(
                DirewolfConfig(
                    base_config=Path(self.direwolf_config_edit.text().strip()),
                    tone=tone,
                    rig_host=self._device.rig_host,
                    rig_port=self._device.rig_port,
                )
            )
            self._direwolf_calibration = adapter
            notes = (
                f"Auto-calibrated {purpose} via Direwolf "
                f"{profile.baud} baud {tone} tone {tone_hz:g} Hz; "
                f"ADEVICE {profile.audio_device}"
            )
            return tone_hz, _DirewolfAutoCalHardware(self, adapter), notes
        if generator == "xara":
            host = self.xara_host_edit.text().strip()
            if not host:
                self.status_label.setText("Enter the xara command host")
                return None
            tone_hz = XaraCalibration.tone_frequency_hz
            adapter = XaraCalibration(
                XaraConfig(
                    host=host,
                    port=self.xara_port_spin.value(),
                    min_drive_db=-30.0,
                    max_drive_db=0.0,
                    rig_host=self._device.rig_host,
                    rig_port=self._device.rig_port,
                )
            )
            self._xara_calibration = adapter
            notes = (
                f"Auto-calibrated {purpose} via xara TUNE "
                f"{tone_hz:g} Hz; command {host}:{self.xara_port_spin.value()}; "
                "drive control is quantized to 1 dB"
            )
            return tone_hz, _XaraAutoCalHardware(self, adapter), notes
        tone_hz = self.auto_tone_spin.value() * 1e3
        return (
            tone_hz,
            _AutoCalHardware(self),
            f"Auto-calibrated {purpose} via internal tone",
        )

    def _start_auto_rehearsal(self) -> None:
        if self._auto_running:
            return
        if self.auto_generator_combo.currentData() != "internal":
            self.status_label.setText(
                "Simulator rehearsal uses the internal generator only"
            )
            return
        minimum = self.auto_min_spin.value() / 100.0
        maximum = self.auto_max_spin.value() / 100.0
        if minimum >= maximum:
            self.status_label.setText(
                "Auto-calibration level range must have a lower minimum"
            )
            return
        self.stop_measurement()
        method = str(self.auto_method_combo.currentData())
        self._auto_run_method = method
        self._auto_run_generator = "internal"
        self._auto_run_rehearsal = True
        self._auto_run_notes = "Simulator rehearsal — no RF or saved hardware record"
        self._direwolf_calibration = None
        self._xara_calibration = None
        self._auto_progress = None
        self._target_progress = None
        if method == "target":
            target_hz = self.auto_target_spin.value() * 1e3
            self._set_auto_meter_target(target_hz)
            try:
                config = TargetCalConfig(
                    target_hz=target_hz,
                    tolerance_hz=self.auto_tolerance_spin.value(),
                    min_level=minimum,
                    max_level=maximum,
                    max_duration_s=18.0,
                )
            except TargetCalError as exc:
                self.status_label.setText(f"Target rehearsal settings: {exc}")
                return
            plant = TargetRehearsalPlant()
            io = RehearsalIO(plant)
            controller = TargetCalController(config, io)
            self._target_controller = controller
            self._auto_controller = None
            self._rehearsal_plant = plant
            self._rehearsal_io = io
            self._auto_running = True
            self._target_progress = controller.start()
            self._render_target_progress(self._target_progress)
        else:
            tone_hz = self.auto_tone_spin.value() * 1e3
            null_index = int(self.auto_null_combo.currentData())
            self._set_auto_meter_range(tone_hz, null_index)
            try:
                config = AutoCalConfig(
                    tone_frequency_hz=tone_hz,
                    null_index=null_index,
                    min_level=minimum,
                    max_level=maximum,
                    carrier_timeout_s=4.0,
                    max_duration_s=18.0,
                )
            except AutoCalError as exc:
                self.status_label.setText(f"Bessel rehearsal settings: {exc}")
                return
            plant = BesselRehearsalPlant(
                tone_frequency_hz=tone_hz,
                null_index=null_index,
            )
            io = RehearsalIO(plant)
            controller = AutoCalController(
                config,
                io,
                detector=BesselNullDetector(
                    tone_frequency_hz=tone_hz,
                    null_index=null_index,
                    sample_rate_hz=48_000.0,
                ),
            )
            self._auto_controller = controller
            self._target_controller = None
            self._rehearsal_plant = plant
            self._rehearsal_io = io
            self._auto_running = True
            self._auto_progress = controller.start()
            self._render_auto_progress(self._auto_progress)
        self._update_bessel_control_state()
        self._start_rehearsal_timer()
        if (
            (self._auto_progress is not None and self._auto_progress.state.terminal)
            or (
                self._target_progress is not None
                and self._target_progress.state.terminal
            )
        ):
            self._finish_auto_run()

    def _start_rehearsal_timer(self) -> None:
        self._stop_rehearsal_timer()
        timer = QTimer(self)
        timer.setInterval(50)
        timer.timeout.connect(self._rehearsal_tick)
        self._rehearsal_timer = timer
        timer.start()

    def _stop_rehearsal_timer(self) -> None:
        timer = self._rehearsal_timer
        self._rehearsal_timer = None
        if timer is not None:
            timer.stop()
            timer.deleteLater()

    def _rehearsal_tick(self) -> None:
        if not self._auto_running:
            self._stop_rehearsal_timer()
            return
        target = self._target_controller
        if target is not None and isinstance(self._rehearsal_plant, TargetRehearsalPlant):
            progress = target.update(self._rehearsal_plant.reading())
            self._render_target_progress(progress)
            if progress.state.terminal:
                self._finish_auto_run()
            return
        controller = self._auto_controller
        if (
            controller is not None
            and isinstance(self._rehearsal_plant, BesselRehearsalPlant)
        ):
            spectrum = self._rehearsal_plant.spectrum()
            progress = controller.update(spectrum)
            self._render_auto_progress(progress)
            self.waterfall.add_spectrum(spectrum)
            if progress.state.terminal:
                self._finish_auto_run()

    def _set_auto_meter_target(self, target_hz: float) -> None:
        self.auto_meter.set_reference_marks(((target_hz, "TGT"),))
        self.auto_meter.setToolTip(
            f"Amber mark: target deviation {target_hz / 1000.0:.3f} kHz. "
            f"{GENERIC_FM_REFERENCE.tooltip}"
        )
        if hasattr(self, "auto_digital"):
            self.auto_digital.set_reference(GENERIC_FM_REFERENCE)
        for maximum in (3_000.0, 6_000.0, 10_000.0):
            if maximum >= target_hz * 1.5:
                self.auto_meter.set_range_hz(maximum)
                return
        self.auto_meter.set_range_hz(10_000.0)

    def _set_auto_meter_range(self, tone_hz: float, null_index: int) -> None:
        reference = bessel_reference(null_index, tone_hz)
        self.auto_meter.set_reference_marks(reference.meter_marks)
        self.auto_meter.setToolTip(
            "Amber mark: selected Bessel null. "
            f"{reference.summary}. {BESSEL_REFERENCE_DETAIL}"
        )
        if hasattr(self, "auto_digital"):
            self.auto_digital.set_reference(reference, prefix="ABSOLUTE REF")
        target = carrier_null_index(null_index) * tone_hz
        for maximum in (3_000.0, 6_000.0, 10_000.0):
            if maximum >= target * 1.05:
                self.auto_meter.set_range_hz(maximum)
                return
        self.auto_meter.set_range_hz(10_000.0)

    def _prepare_auto_frequency(self, *, remote: bool = False) -> float | None:
        try:
            requested = self._desired_frequency_hz()
        except ValueError as exc:
            self.status_label.setText(f"Frequency error: {exc}")
            return None
        if remote:
            rig_host = self._device.direwolf_remote_rig_host.strip()
            rig_port = self._device.direwolf_remote_rig_port
            if not rig_host:
                self.status_label.setText(
                    "Configure the remote rigctld host in SDR / Rig before "
                    "remote Direwolf calibration"
                )
                return None
        else:
            rig_host = self._device.rig_host
            rig_port = self._device.rig_port
        client = RigctldClient(rig_host, rig_port)
        try:
            mode, _ = client.get_mode()
            if mode not in {"FM", "FMN", "PKT"}:
                self.status_label.setText(
                    f"Rig mode is {mode}; switch to FM before auto-calibration"
                )
                return None
            if self.link_rig.isChecked():
                requested = float(client.set_frequency(int(round(requested))))
            else:
                rig_frequency = float(client.get_frequency())
                if abs(rig_frequency - requested) > 1_000.0:
                    self.status_label.setText(
                        f"Rig is on {rig_frequency / 1e6:.6f} MHz, not the "
                        "displayed frequency; tune it or enable Link Rig Frequency"
                    )
                    return None
        except RigctldError as exc:
            label = "Remote rig" if remote else "Rig"
            self.status_label.setText(f"{label} preflight check failed: {exc}")
            return None
        self.frequency_edit.setText(f"{requested / 1e6:.6f}")
        return requested

    def _key_direwolf_calibration(self, adapter: DirewolfCalibration) -> bool:
        adapter.start(0.0)
        self._ptt_keyed = True
        self._ptt_timer.start(int(round(self._device.ptt_timeout_s * 1000.0)))
        self.status_label.setText(
            "Direwolf calibration PTT ON — automatic unkey in "
            f"{self._device.ptt_timeout_s:.0f} s"
        )
        return True

    def _unkey_direwolf_calibration(self, adapter: DirewolfCalibration) -> bool:
        cleanup_error: str | None = None
        try:
            adapter.stop()
        except (DirewolfError, OSError, RuntimeError) as exc:
            cleanup_error = str(exc)
        confirmed = self._unkey_ptt()
        if cleanup_error is not None:
            self.status_label.setText(
                f"Direwolf cleanup failed: {cleanup_error}"
            )
            return False
        return confirmed

    def _key_remote_direwolf_calibration(
        self,
        adapter: RemoteDirewolfCalibration,
    ) -> bool:
        adapter.start(0.0)
        self._ptt_keyed = True
        self._ptt_timer.start(int(round(self._device.ptt_timeout_s * 1000.0)))
        self.status_label.setText(
            "Remote Direwolf calibration PTT ON — automatic unkey in "
            f"{self._device.ptt_timeout_s:.0f} s"
        )
        return True

    def _unkey_remote_direwolf_calibration(
        self,
        adapter: RemoteDirewolfCalibration,
    ) -> bool:
        cleanup_error: str | None = None
        try:
            adapter.stop()
        except (DirewolfError, OSError, RuntimeError, SshError) as exc:
            cleanup_error = str(exc)
        self._ptt_timer.stop()
        self._ptt_keyed = False
        if self._remote_direwolf_calibration is adapter:
            self._remote_direwolf_calibration = None
        if cleanup_error is not None:
            self.status_label.setText(
                f"Remote Direwolf cleanup failed: {cleanup_error}"
            )
            return False
        self.status_label.setText("Remote Direwolf PTT OFF")
        return True

    def _key_xara_calibration(self, adapter: XaraCalibration) -> bool:
        adapter.start(0.0)
        self._ptt_keyed = True
        self._ptt_timer.start(int(round(self._device.ptt_timeout_s * 1000.0)))
        self.status_label.setText(
            "xara TUNE PTT ON — automatic unkey in "
            f"{self._device.ptt_timeout_s:.0f} s"
        )
        return True

    def _unkey_xara_calibration(self, adapter: XaraCalibration) -> bool:
        cleanup_error: str | None = None
        try:
            adapter.stop()
        except (XaraError, OSError, RuntimeError) as exc:
            cleanup_error = str(exc)
        confirmed = self._unkey_ptt()
        if cleanup_error is not None:
            self.status_label.setText(f"xara cleanup failed: {cleanup_error}")
            return False
        return confirmed

    def _cancel_auto_calibration_from_button(self) -> None:
        self._cancel_auto_calibration()

    def _cancel_auto_calibration(self, reason: str = "cancelled by operator") -> None:
        if not self._auto_running:
            return
        target = self._target_controller
        if target is not None:
            progress = target.cancel(reason)
            self._render_target_progress(progress)
            self._finish_auto_run()
            return
        controller = self._auto_controller
        if controller is None:
            return
        progress = controller.cancel(reason)
        self._render_auto_progress(progress)
        self._finish_auto_run()

    def _finish_auto_run(self) -> None:
        self._auto_running = False
        self._stop_rehearsal_timer()
        self._rehearsal_plant = None
        self._rehearsal_io = None
        self._target_controller = None
        self._auto_controller = None
        adapter = self._direwolf_calibration
        self._direwolf_calibration = None
        if adapter is not None:
            try:
                adapter.stop()
            except (DirewolfError, OSError, RuntimeError) as exc:
                self.status_label.setText(f"Direwolf cleanup failed: {exc}")
            self._ptt_timer.stop()
            self._ptt_keyed = False
        remote_adapter = self._remote_direwolf_calibration
        self._remote_direwolf_calibration = None
        if remote_adapter is not None:
            try:
                remote_adapter.stop()
            except (
                DirewolfError,
                OSError,
                RuntimeError,
                SshError,
            ) as exc:
                self.status_label.setText(
                    f"Remote Direwolf cleanup failed: {exc}"
                )
            self._ptt_timer.stop()
            self._ptt_keyed = False
        xara = self._xara_calibration
        self._xara_calibration = None
        if xara is not None:
            try:
                xara.stop()
            except (XaraError, OSError, RuntimeError) as exc:
                self.status_label.setText(f"xara cleanup failed: {exc}")
            self._ptt_timer.stop()
            self._ptt_keyed = False
        self._ptt_timer.stop()
        self._ptt_keyed = False
        self._update_auto_control_state()
        self._update_bessel_control_state()
        if self._auto_run_method != "target":
            self._reset_bessel_display()

    def _render_auto_progress(self, progress: AutoCalProgress) -> None:
        self._auto_progress = progress
        self.auto_progress.setValue(int(round(progress.fraction * 1000.0)))
        self.auto_state_label.setText(progress.state.value)
        if progress.level is None:
            self.auto_level_label.setText("Level: —")
        elif self._auto_run_generator == "xara":
            self.auto_level_label.setText(
                f"Drive: {round(-30.0 + 30.0 * progress.level)} dB"
            )
        else:
            self.auto_level_label.setText(
                f"Audio level: {progress.level * 100.0:.1f}%"
            )
        if progress.best_depth_db is None:
            self.auto_depth_label.setText("Best null: —")
        else:
            if progress.best_level is None:
                level = ""
            elif self._auto_run_generator == "xara":
                level = f" at {round(-30.0 + 30.0 * progress.best_level)} dB drive"
            else:
                level = f" at {progress.best_level * 100.0:.1f}%"
            self.auto_depth_label.setText(
                f"Best null: {progress.best_depth_db:.1f} dB{level}"
            )
        self.auto_detail_label.setText(
            f"{progress.message} · {progress.elapsed_s:.1f} s"
        )
        bessel = progress.bessel or BesselProgress(
            state=progress.state.value,
            carrier_locked=False,
            tone_frequency_hz=(
                self._auto_controller.tone_frequency_hz
                if self._auto_controller is not None
                else self.auto_tone_spin.value() * 1e3
            ),
            null_index=(
                self._auto_controller.null_index
                if self._auto_controller is not None
                else int(self.auto_null_combo.currentData())
            ),
        )
        self.auto_digital.set_bessel(bessel)
        if progress.captured is not None:
            self.auto_meter.set_value(
                progress.captured.deviation_hz,
                "AUTO NULL",
            )
        else:
            self.auto_meter.set_value(None, progress.state.value)
        color = "#ffbf47"
        if progress.state is AutoCalState.FAILED:
            color = "#ff6868"
        elif progress.state is AutoCalState.COMPLETE:
            status = progress.captured.quality.status if progress.captured else None
            color = "#ffbf47" if status is ReadingStatus.MARGINAL else "#f5f5f5"
        self.auto_state_label.setStyleSheet(f"border: none; color: {color};")
        self.status_label.setText(f"AUTO-CAL · {progress.message}")
        self._update_auto_control_state()

    def _render_target_progress(self, progress: TargetCalProgress) -> None:
        self._target_progress = progress
        self.auto_progress.setValue(int(round(progress.fraction * 1000.0)))
        self.auto_state_label.setText(progress.state.value)
        if progress.level is None:
            self.auto_level_label.setText("Level: —")
        elif self._auto_run_generator == "xara":
            self.auto_level_label.setText(
                f"Drive: {round(-30.0 + 30.0 * progress.level)} dB"
            )
        else:
            self.auto_level_label.setText(
                f"Audio level: {progress.level * 100.0:.1f}%"
            )
        if progress.measured_hz is None:
            self.auto_depth_label.setText(
                f"Target: {progress.target_hz / 1000.0:.3f} kHz"
            )
        else:
            error = progress.error_hz or 0.0
            self.auto_depth_label.setText(
                f"Measured {progress.measured_hz / 1000.0:.3f} kHz · "
                f"target {progress.target_hz / 1000.0:.3f} kHz · "
                f"error {error:+.0f} Hz"
            )
        self.auto_detail_label.setText(
            f"{progress.message} · {progress.elapsed_s:.1f} s"
        )
        self.auto_digital.set_target(
            progress.reading,
            target_hz=progress.target_hz,
            state=progress.state.value,
        )
        value = (
            progress.reading.deviation_hz
            if progress.reading is not None
            else progress.measured_hz
        )
        self.auto_meter.set_value(value, progress.state.value)
        color = "#ffbf47"
        if progress.state is TargetCalState.FAILED:
            color = "#ff6868"
        elif progress.state is TargetCalState.COMPLETE:
            status = (
                progress.reading.quality.status
                if progress.reading is not None
                else None
            )
            color = "#ffbf47" if status is ReadingStatus.MARGINAL else "#f5f5f5"
        self.auto_state_label.setStyleSheet(f"border: none; color: {color};")
        self.status_label.setText(f"TARGET-CAL · {progress.message}")
        self._update_auto_control_state()

    def _start_auto_tone(self, level: float) -> None:
        if self._tone_player.active:
            self._tone_player.stop()
        controller = self._auto_controller
        duration = (
            max(10.0, controller.config.max_duration_s + 5.0)
            if controller is not None
            else 20.0
        )
        self._tone_player.start(
            frequency_hz=(
                controller.tone_frequency_hz if controller is not None else 1_000.0
            ),
            device_name=self._device.audio_output,
            duration_s=duration,
            level=level,
        )
        self.tone_button.setText("Tone Off")

    def _set_auto_tone_level(self, level: float) -> None:
        self._tone_player.set_level(level)

    def _show_bessel_progress(
        self,
        progress: BesselProgress,
        spectrum: tuple[float, ...],
    ) -> None:
        self._bessel_progress = progress
        self.digital.set_bessel(progress)
        self.waterfall.add_spectrum(spectrum)
        self._update_bessel_control_state()
        if progress.captured is not None:
            self.meter.set_value(progress.captured.deviation_hz, "NULL CAPTURED")
        else:
            self.meter.set_value(None, progress.state)
        depth_db = progress.null_depth_db
        if progress.captured is not None:
            depth_db = progress.captured.metadata.get("null_depth_db", depth_db)
        depth = f" · depth {float(depth_db):.1f} dB" if depth_db is not None else ""
        offset = (
            f" · carrier {progress.carrier_offset_hz:+.0f} Hz"
            if progress.carrier_offset_hz is not None
            else ""
        )
        self.status_label.setText(
            f"BESSEL · J0 null {progress.null_index} · "
            f"{progress.tone_frequency_hz / 1e3:.3f} kHz"
            f"{offset}{depth} · {progress.state}"
        )

    def _reference_profile_id(self) -> str:
        profile_id = str(self.profile_combo.currentData())
        return {
            "m17": "module17-m17",
            "dmr": "mmdvm-dmr",
        }.get(profile_id, profile_id)

    def _update_measurement_reference(self) -> None:
        if not hasattr(self, "digital"):
            return
        if self._is_bessel_mode():
            reference = bessel_reference(
                self._null_index(),
                self._bessel_tone_hz(),
            )
            self.digital.set_reference(reference, prefix="ABSOLUTE REF")
            self.meter.set_reference_marks(reference.meter_marks)
            self.meter.setToolTip(
                "Amber mark: selected Bessel null. "
                f"{reference.summary}. {BESSEL_REFERENCE_DETAIL}"
            )
            return
        reference = reference_for_profile(self._reference_profile_id())
        self.digital.set_reference(reference)
        self.meter.set_reference_marks(
            reference.meter_marks if reference is not None else ()
        )
        self.meter.setToolTip(
            (
                f"Amber marks: {reference.summary}. {reference.detail}"
                if reference is not None
                else ""
            )
        )

    def _on_profile_changed(self) -> None:
        if self._auto_running:
            self._cancel_auto_calibration("cancelled by profile change")
        self._update_measurement_reference()
        if self._session is not None and self._session.running:
            self.start_measurement()

    def _on_source_changed(self) -> None:
        if self._auto_running:
            self._cancel_auto_calibration("cancelled by source change")
        if self._session is not None and self._session.running:
            self.start_measurement()
        else:
            self._update_auto_control_state()

    def _on_range_changed(self) -> None:
        self.meter.set_range_hz(float(self.range_combo.currentData()))

    def _show_reference_dialog(self) -> None:
        dialog = DeviationReferenceDialog(self)
        dialog.exec()

    def _edit_device(self) -> None:
        if self._auto_running:
            self._cancel_auto_calibration("cancelled by SDR/Rig settings change")
        dialog = DeviceDialog(self._device, self)
        if dialog.exec() == QDialog.DialogCode.Accepted:
            self._device = dialog.settings()
            self.status_label.setText("SDR settings updated")
            if self.auto_generator_combo.currentData() == "direwolf":
                self._refresh_direwolf_info()
            if self.source_combo.currentData() == SOURCE_LIVE:
                self.start_measurement()

    def _desired_frequency_hz(self) -> float:
        try:
            frequency_mhz = float(self.frequency_edit.text().strip())
        except ValueError as exc:
            raise ValueError("enter a receive frequency in MHz") from exc
        if not math.isfinite(frequency_mhz) or not 0.1 <= frequency_mhz <= 6000.0:
            raise ValueError("receive frequency must be between 0.1 and 6000 MHz")
        return frequency_mhz * 1e6

    def _selected_rig_endpoint(self) -> tuple[str, int, bool]:
        if (
            self._device.direwolf_remote
            and self.auto_generator_combo.currentData() == "direwolf"
            and self._device.direwolf_remote_rig_host.strip()
        ):
            return (
                self._device.direwolf_remote_rig_host.strip(),
                self._device.direwolf_remote_rig_port,
                True,
            )
        return self._device.rig_host, self._device.rig_port, False

    def _commit_frequency(self) -> None:
        if self._auto_running:
            self._cancel_auto_calibration("cancelled by frequency change")
        try:
            requested = self._desired_frequency_hz()
        except ValueError as exc:
            self.status_label.setText(f"Frequency error: {exc}")
            return
        if self.link_rig.isChecked():
            rig_host, rig_port, remote = self._selected_rig_endpoint()
            try:
                confirmed = RigctldClient(
                    rig_host,
                    rig_port,
                ).set_frequency(int(round(requested)))
            except RigctldError as exc:
                label = "Remote rig" if remote else "Rig"
                self.status_label.setText(f"{label}: {exc}")
                return
            requested = float(confirmed)
            self.frequency_edit.setText(f"{requested / 1e6:.6f}")
        self._frequency_hz = requested
        if self._session is not None and self._session.running:
            self.start_measurement()
        else:
            self.status_label.setText(
                f"Receive frequency {requested / 1e6:.6f} MHz"
            )

    def _read_from_rig(self) -> None:
        if self._auto_running:
            self._cancel_auto_calibration("cancelled by frequency change")
        rig_host, rig_port, remote = self._selected_rig_endpoint()
        try:
            frequency = RigctldClient(rig_host, rig_port).get_frequency()
        except RigctldError as exc:
            label = "Remote rig" if remote else "rigctld"
            self.status_label.setText(f"{label}: {exc}")
            return
        self.frequency_edit.setText(f"{frequency / 1e6:.6f}")
        self._frequency_hz = float(frequency)
        source = "remote rigctld" if remote else "rigctld"
        self.status_label.setText(
            f"Read {frequency / 1e6:.6f} MHz from {source}"
        )
        if self._session is not None and self._session.running:
            self.start_measurement()

    def worker_is_running(self) -> bool:
        return self._session is not None and self._session.running

    def _toggle_running(self) -> None:
        if self._session is not None and self._session.running:
            self.stop_measurement()
        else:
            self.start_measurement()

    def start_measurement(self) -> None:
        self.stop_measurement()
        try:
            self._frequency_hz = self._desired_frequency_hz()
        except ValueError as exc:
            self.status_label.setText(f"Frequency error: {exc}")
            return
        profile_name = str(self.profile_combo.currentData())
        reference_profile_id = self._reference_profile_id()
        mode = "bessel" if self._is_bessel_mode() else "digital"
        simulator = self.source_combo.currentData() == SOURCE_SIMULATOR
        try:
            session = GnuRadioLiveSession(
                GnuRadioLiveConfig(
                    profile_name=profile_name,
                    simulator=simulator,
                    device_args=self._device.device_args,
                    sample_rate_hz=self._device.sample_rate_hz,
                    center_frequency_hz=(
                        self._frequency_hz + self._device.lo_offset_hz
                    ),
                    signal_frequency_hz=self._frequency_hz,
                    gain_db=self._device.gain_db,
                    bandwidth_hz=self._device.bandwidth_hz,
                    mode=mode,
                    bessel_tone_hz=self._bessel_tone_hz(),
                )
            )
            session.start()
        except GnuRadioLiveError as exc:
            self.status_label.setText(f"Start failed: {exc}")
            self.start_button.setText("Start")
            self.meter.set_value(None, "ERROR")
            self.digital.set_measurement(
                None,
                source_label=self._source_label,
                profile_id=reference_profile_id,
            )
            return

        self._session = session
        self._source_label = "SIMULATOR" if simulator else "LIVE"
        self.start_button.setText("Stop")
        if mode == "bessel":
            self._bessel_detector = BesselNullDetector(
                tone_frequency_hz=self._bessel_tone_hz(),
                null_index=self._null_index(),
                sample_rate_hz=48_000.0,
            )
            self._bessel_spectrum_sequence = -1
            self._reset_bessel_display()
            self.status_label.setText(
                "Bessel: waiting for carrier; remove modulation if needed"
            )
        else:
            self.digital.set_measurement(
                None,
                source_label=self._source_label,
                profile_id=reference_profile_id,
            )
            self.status_label.setText(f"Starting {profile_name.upper()} measurement")
        self.meter.set_value(None, "STARTING")
        self._poll_timer.start()
        self._update_bessel_control_state()

    def stop_measurement(self) -> None:
        if self._auto_running:
            self._cancel_auto_calibration("cancelled because measurement stopped")
        self._stop_tone()
        self._stop_ptt()
        if hasattr(self, "_poll_timer"):
            self._poll_timer.stop()
        session = self._session
        if session is None:
            self._update_bessel_control_state()
            return
        self._session = None
        session.stop()
        self.start_button.setText("Start")
        self.meter.set_value(None, "STOPPED")
        self.digital.set_measurement(None, source_label=self._source_label)
        self._update_bessel_control_state()

    def _poll_live_session(self) -> None:
        session = self._session
        if session is None:
            self._poll_timer.stop()
            return
        error = session.state.take_error()
        if error is not None:
            self.status_label.setText(error)
            self.stop_measurement()
            self.meter.set_value(None, "ERROR")
            return
        if self._is_bessel_mode():
            sequence, spectrum = session.state.spectrum_snapshot()
            if sequence <= 0 or sequence == self._bessel_spectrum_sequence:
                return
            self._bessel_spectrum_sequence = sequence
            if self._auto_running and self._auto_controller is not None:
                progress = self._auto_controller.update(spectrum)
                self._render_auto_progress(progress)
                self.waterfall.add_spectrum(spectrum)
                if progress.state.terminal:
                    self._finish_auto_run()
                return
            if self._bessel_detector is not None:
                progress = self._bessel_detector.update(spectrum)
                self._show_bessel_progress(progress, spectrum)
            return
        snapshot = session.state.latest_snapshot()
        if self._auto_running and self._target_controller is not None:
            if (
                snapshot is not None
                and snapshot.timestamp_s != self._target_snapshot_timestamp
            ):
                self._target_snapshot_timestamp = snapshot.timestamp_s
                progress = self._target_controller.update(snapshot.reading)
                self._render_target_progress(progress)
                self.waterfall.add_spectrum(snapshot.spectrum_db)
                if progress.state.terminal:
                    self._finish_auto_run()
            return
        if snapshot is not None:
            self._on_snapshot(snapshot)

    def _on_snapshot(self, snapshot: LiveSnapshot) -> None:
        value = snapshot.reporting_deviation_hz
        status = "LIVE" if value is not None else "WAITING"
        self.meter.set_value(value, status)
        self.digital.set_measurement(snapshot, source_label=self._source_label)
        self.waterfall.add_spectrum(snapshot.spectrum_db)
        if value is None:
            self.status_label.setText(
                f"{self._source_label} · no active RF · "
                f"{snapshot.segment_count} segments"
            )
        else:
            snr = "" if snapshot.snr_db is None else f" · SNR {snapshot.snr_db:.0f} dB"
            self.status_label.setText(
                f"{self._source_label} · {self._frequency_hz / 1e6:.6f} MHz"
                f"{snr} · {snapshot.valid_segment_count}/"
                f"{snapshot.segment_count} segments valid"
            )

    def closeEvent(self, event) -> None:  # noqa: N802
        self._save_ui_settings()
        self.stop_measurement()
        super().closeEvent(event)


def _configure_qt_decoration_environment() -> None:
    """Select a Qt Wayland decoration that honors min/max window flags."""

    os.environ.setdefault("QT_WAYLAND_DECORATION", "bradient")


def run_gui() -> int:
    from PyQt6.QtWidgets import QApplication
    import sys

    # GNOME/Mutter does not provide server-side decorations to native Wayland
    # clients. Qt therefore draws client-side decorations, and its default
    # Adwaita-style title bar can omit minimize/maximize buttons even when the
    # QWindow flags request them. Bradient honors the window flags. Respect an
    # explicit user override.
    _configure_qt_decoration_environment()
    app = QApplication.instance() or QApplication(sys.argv)
    app.setFont(QFont("DejaVu Sans", 11))
    window = MainWindow()
    window.showMaximized()
    return app.exec()
