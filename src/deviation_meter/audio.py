"""Audio tone generation for Bessel calibration."""

from __future__ import annotations

import math

import numpy as np
from PyQt6.QtCore import QBuffer, QByteArray, QIODevice, QObject, QTimer, pyqtSignal
from PyQt6.QtMultimedia import QAudio, QAudioFormat, QAudioSink, QMediaDevices


class AudioToneError(RuntimeError):
    """Raised when an output tone cannot be played."""


def generate_tone_pcm16(
    frequency_hz: float,
    duration_s: float,
    *,
    sample_rate_hz: int = 48_000,
    level: float = 0.25,
) -> bytes:
    """Generate a mono int16 sine tone with short anti-click fades."""

    if not math.isfinite(frequency_hz) or frequency_hz <= 0.0:
        raise AudioToneError("tone frequency must be positive")
    if not math.isfinite(duration_s) or duration_s <= 0.0:
        raise AudioToneError("tone duration must be positive")
    if sample_rate_hz < 8_000:
        raise AudioToneError("audio sample rate is too low")
    if not math.isfinite(level) or not 0.0 < level <= 1.0:
        raise AudioToneError("tone level must be between 0 and 1")
    if frequency_hz >= sample_rate_hz / 2.0:
        raise AudioToneError("tone frequency must be below Nyquist")

    count = max(1, int(round(duration_s * sample_rate_hz)))
    t = np.arange(count, dtype=np.float64) / sample_rate_hz
    samples = level * np.sin(2.0 * math.pi * frequency_hz * t)
    fade_count = min(count // 2, int(round(0.005 * sample_rate_hz)))
    if fade_count > 1:
        fade = np.linspace(0.0, 1.0, fade_count, endpoint=False)
        samples[:fade_count] *= fade
        samples[-fade_count:] *= fade[::-1]
    pcm = np.clip(samples * 32767.0, -32768.0, 32767.0).astype("<i2")
    return pcm.tobytes()


class TonePlayer(QObject):
    """Play a calibration tone through an explicitly selected Qt audio sink."""

    finished = pyqtSignal()

    def __init__(self, parent: QObject | None = None) -> None:
        super().__init__(parent)
        self._sink: QAudioSink | None = None
        self._buffer: QBuffer | None = None
        self._level = 0.25
        self._duration_timer = QTimer(self)
        self._duration_timer.setSingleShot(True)
        self._duration_timer.timeout.connect(self._on_duration_complete)

    @staticmethod
    def output_devices() -> list[str]:
        return [device.description() for device in QMediaDevices.audioOutputs()]

    @staticmethod
    def default_output_name() -> str:
        device = QMediaDevices.defaultAudioOutput()
        return "" if device is None or device.isNull() else device.description()

    @property
    def active(self) -> bool:
        return self._sink is not None

    @property
    def level(self) -> float:
        return self._level

    def set_level(self, level: float) -> None:
        """Adjust the playing tone amplitude without restarting its buffer."""

        if not math.isfinite(level) or not 0.0 < level <= 1.0:
            raise AudioToneError("tone level must be between 0 and 1")
        self._level = float(level)
        if self._sink is not None:
            self._sink.setVolume(self._level)

    def start(
        self,
        *,
        frequency_hz: float,
        device_name: str = "",
        duration_s: float = 20.0,
        level: float = 0.25,
    ) -> None:
        if self.active:
            raise AudioToneError("tone is already playing")
        if not math.isfinite(level) or not 0.0 < level <= 1.0:
            raise AudioToneError("tone level must be between 0 and 1")
        devices = QMediaDevices.audioOutputs()
        if not devices:
            raise AudioToneError("no audio output devices are available")
        if device_name:
            selected = next(
                (device for device in devices if device.description() == device_name),
                None,
            )
            if selected is None:
                raise AudioToneError(f"audio output not found: {device_name}")
        else:
            selected = QMediaDevices.defaultAudioOutput()
            if selected is None or selected.isNull():
                selected = devices[0]

        audio_format = QAudioFormat()
        audio_format.setSampleRate(48_000)
        audio_format.setChannelCount(1)
        audio_format.setSampleFormat(QAudioFormat.SampleFormat.Int16)
        if not selected.isFormatSupported(audio_format):
            raise AudioToneError(
                f"{selected.description()} does not support 48 kHz mono int16 audio"
            )

        data = generate_tone_pcm16(
            frequency_hz,
            duration_s,
            level=1.0,
        )
        buffer = QBuffer(self)
        buffer.setData(QByteArray(data))
        if not buffer.open(QIODevice.OpenModeFlag.ReadOnly):
            raise AudioToneError("could not open audio buffer")

        sink = QAudioSink(selected, audio_format, self)
        sink.setVolume(level)
        self._buffer = buffer
        self._sink = sink
        self._level = float(level)
        sink.start(buffer)
        if sink.error() != QAudio.Error.NoError:
            self.stop()
            raise AudioToneError(f"audio output failed: {sink.error()}")
        self._duration_timer.start(int(round(duration_s * 1000.0)))

    def _on_duration_complete(self) -> None:
        self.stop()
        self.finished.emit()

    def stop(self) -> None:
        self._duration_timer.stop()
        sink = self._sink
        buffer = self._buffer
        self._sink = None
        self._buffer = None
        if sink is not None:
            try:
                sink.stop()
            except Exception:
                pass
            sink.deleteLater()
        if buffer is not None:
            try:
                buffer.close()
            except Exception:
                pass
            buffer.deleteLater()
