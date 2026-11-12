"""Raw IQ capture support for creating reproducible RF fixtures."""

from __future__ import annotations

import json
import math
import os
import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import StrEnum
from pathlib import Path
from typing import Any, Protocol

import numpy as np
from numpy.typing import NDArray

from . import APPLICATION_NAME, __version__

ComplexArray = NDArray[np.complex64]
DEFAULT_CHUNK_SAMPLES = 65_536
DEFAULT_TIMEOUT_US = 1_000_000
DEFAULT_MAX_CONSECUTIVE_TIMEOUTS = 10


class CaptureError(RuntimeError):
    """Raised when an IQ capture cannot be completed."""


class ReadStatus(StrEnum):
    """Outcome of one stream read."""

    DATA = "data"
    TIMEOUT = "timeout"
    OVERFLOW = "overflow"


@dataclass(frozen=True, slots=True)
class IQRead:
    """Result from a streaming IQ source."""

    status: ReadStatus
    sample_count: int = 0
    overflow: bool = False

    def __post_init__(self) -> None:
        if self.sample_count < 0:
            raise ValueError("sample_count cannot be negative")


class IQSource(Protocol):
    """Minimal streaming interface used by the recorder and test doubles."""

    @property
    def sample_rate_hz(self) -> float: ...

    @property
    def metadata(self) -> Mapping[str, Any]: ...

    def read_into(self, buffer: ComplexArray, timeout_us: int) -> IQRead: ...

    def close(self) -> None: ...


@dataclass(frozen=True, slots=True)
class SoapyDeviceConfig:
    """Receiver settings shared by capture and live measurement."""

    sample_rate_hz: float
    center_frequency_hz: float
    device_args: str = ""
    channel: int = 0
    gain_db: float | None = None
    bandwidth_hz: float | None = None
    antenna: str | None = None
    agc: bool = False

    def __post_init__(self) -> None:
        for name in ("sample_rate_hz", "center_frequency_hz"):
            value = float(getattr(self, name))
            if not math.isfinite(value) or value <= 0.0:
                raise CaptureError(f"{name} must be finite and greater than zero")
        if self.gain_db is not None and not math.isfinite(float(self.gain_db)):
            raise CaptureError("gain_db must be finite when provided")
        if self.bandwidth_hz is not None:
            bandwidth = float(self.bandwidth_hz)
            if not math.isfinite(bandwidth) or bandwidth <= 0.0:
                raise CaptureError(
                    "bandwidth_hz must be finite and greater than zero when provided"
                )
        if isinstance(self.channel, bool) or not isinstance(self.channel, int):
            raise CaptureError("channel must be an integer")
        if self.channel < 0:
            raise CaptureError("channel cannot be negative")


@dataclass(frozen=True, slots=True)
class CaptureConfig:
    """Configuration for one raw CF32 IQ capture."""

    output: Path
    sample_rate_hz: float
    center_frequency_hz: float
    duration_s: float
    device_args: str = ""
    channel: int = 0
    gain_db: float | None = None
    bandwidth_hz: float | None = None
    antenna: str | None = None
    agc: bool = False
    signal_frequency_hz: float | None = None
    chunk_samples: int = DEFAULT_CHUNK_SAMPLES
    max_consecutive_timeouts: int = DEFAULT_MAX_CONSECUTIVE_TIMEOUTS
    overwrite: bool = False
    metadata: Mapping[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        object.__setattr__(self, "output", Path(self.output))
        for name in ("sample_rate_hz", "center_frequency_hz", "duration_s"):
            value = float(getattr(self, name))
            if not math.isfinite(value) or value <= 0.0:
                raise CaptureError(f"{name} must be finite and greater than zero")
        if self.signal_frequency_hz is not None:
            signal = float(self.signal_frequency_hz)
            if not math.isfinite(signal) or signal <= 0.0:
                raise CaptureError(
                    "signal_frequency_hz must be finite and greater than zero"
                )
        if self.gain_db is not None:
            gain = float(self.gain_db)
            if not math.isfinite(gain):
                raise CaptureError("gain_db must be finite when provided")
        if self.bandwidth_hz is not None:
            bandwidth = float(self.bandwidth_hz)
            if not math.isfinite(bandwidth) or bandwidth <= 0.0:
                raise CaptureError(
                    "bandwidth_hz must be finite and greater than zero when provided"
                )
        if isinstance(self.channel, bool) or not isinstance(self.channel, int):
            raise CaptureError("channel must be an integer")
        if self.channel < 0:
            raise CaptureError("channel cannot be negative")
        if isinstance(self.chunk_samples, bool) or not isinstance(self.chunk_samples, int):
            raise CaptureError("chunk_samples must be an integer")
        if not 1_024 <= self.chunk_samples <= 1_048_576:
            raise CaptureError("chunk_samples must be between 1024 and 1048576")
        if (
            isinstance(self.max_consecutive_timeouts, bool)
            or not isinstance(self.max_consecutive_timeouts, int)
            or self.max_consecutive_timeouts < 1
        ):
            raise CaptureError("max_consecutive_timeouts must be a positive integer")

    def device_config(self) -> SoapyDeviceConfig:
        return SoapyDeviceConfig(
            sample_rate_hz=self.sample_rate_hz,
            center_frequency_hz=self.center_frequency_hz,
            device_args=self.device_args,
            channel=self.channel,
            gain_db=self.gain_db,
            bandwidth_hz=self.bandwidth_hz,
            antenna=self.antenna,
            agc=self.agc,
        )


@dataclass(frozen=True, slots=True)
class CaptureResult:
    """Summary and sidecar metadata for a capture."""

    output: Path
    metadata_path: Path
    captured_samples: int
    target_samples: int
    sample_rate_hz: float
    duration_s: float
    peak_magnitude: float
    rms_magnitude: float
    clipped_fraction: float
    overflow_count: int
    read_timeouts: int
    completed: bool
    cancelled: bool
    error: str | None
    metadata: Mapping[str, Any]


def _utc_now() -> str:
    return datetime.now(tz=timezone.utc).isoformat()


def _metadata_path(output: Path) -> Path:
    return output.with_name(output.name + ".json")


def _write_json_atomic(path: Path, payload: Mapping[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + ".tmp")
    encoded = json.dumps(payload, indent=2, sort_keys=True) + "\n"
    temporary.write_text(encoded, encoding="utf-8")
    os.replace(temporary, path)


def capture_iq(
    config: CaptureConfig,
    source: IQSource,
    *,
    progress: Callable[[int, int, float], None] | None = None,
) -> CaptureResult:
    """Stream IQ from ``source`` into a raw little-endian CF32 file.

    Ctrl-C finalizes the partial capture and metadata instead of leaving an
    unlabelled file. Other failures also write a metadata sidecar before being
    re-raised.
    """

    output = config.output
    metadata_path = _metadata_path(output)
    if not config.overwrite and (output.exists() or metadata_path.exists()):
        source.close()
        raise CaptureError(f"capture already exists: {output}")

    rate = float(source.sample_rate_hz)
    if not math.isfinite(rate) or rate <= 0.0:
        source.close()
        raise CaptureError("source sample rate must be finite and greater than zero")
    target_samples = max(1, int(round(config.duration_s * rate)))

    output.parent.mkdir(parents=True, exist_ok=True)
    started_utc = _utc_now()
    started_monotonic = time.monotonic()
    captured = 0
    peak = 0.0
    sum_squares = 0.0
    clipped = 0
    overflow_count = 0
    read_timeouts = 0
    consecutive_timeouts = 0
    cancelled = False
    failure: BaseException | None = None
    failure_text: str | None = None

    try:
        mode = "wb" if config.overwrite else "xb"
        with output.open(mode) as handle:
            buffer = np.empty(
                min(config.chunk_samples, target_samples),
                dtype=np.complex64,
            )
            while captured < target_samples:
                remaining = target_samples - captured
                requested = min(buffer.size, remaining)
                read = source.read_into(
                    buffer[:requested],
                    timeout_us=DEFAULT_TIMEOUT_US,
                )
                if read.status is ReadStatus.TIMEOUT:
                    read_timeouts += 1
                    consecutive_timeouts += 1
                    if consecutive_timeouts > config.max_consecutive_timeouts:
                        raise CaptureError("SDR stream timed out repeatedly")
                    continue
                if read.status is ReadStatus.OVERFLOW:
                    overflow_count += 1
                    consecutive_timeouts += 1
                    if consecutive_timeouts > config.max_consecutive_timeouts:
                        raise CaptureError("SDR stream repeatedly overflowed")
                    continue
                if read.sample_count < 1:
                    consecutive_timeouts += 1
                    if consecutive_timeouts > config.max_consecutive_timeouts:
                        raise CaptureError("SDR stream produced no samples")
                    continue
                if read.sample_count > requested:
                    raise CaptureError("SDR source returned more samples than requested")

                consecutive_timeouts = 0
                if read.overflow:
                    overflow_count += 1
                chunk = buffer[: read.sample_count]
                handle.write(chunk.astype("<c8", copy=False).tobytes())
                captured += read.sample_count
                magnitude = np.abs(chunk)
                chunk_peak = float(np.max(magnitude))
                peak = max(peak, chunk_peak)
                sum_squares += float(np.sum(magnitude.astype(np.float64) ** 2))
                clipped += int(np.count_nonzero(magnitude >= 1.0))
                if progress is not None:
                    progress(captured, target_samples, time.monotonic() - started_monotonic)
    except KeyboardInterrupt:
        cancelled = True
    except BaseException as exc:
        failure = exc
        failure_text = f"{exc.__class__.__name__}: {exc}"
    finally:
        try:
            source.close()
        except Exception:
            if failure is None:
                failure_text = "failed to close SDR stream"
                failure = CaptureError(failure_text)

    finished_utc = _utc_now()
    duration = captured / rate
    rms = math.sqrt(sum_squares / captured) if captured else 0.0
    clipped_fraction = clipped / captured if captured else 0.0
    completed = (
        failure is None
        and not cancelled
        and captured >= target_samples
    )

    metadata: dict[str, Any] = {
        "schema_version": 1,
        "application": {
            "name": APPLICATION_NAME,
            "version": __version__,
        },
        "format": {
            "name": "cf32",
            "dtype": "complex64",
            "byte_order": "little",
            "layout": "interleaved I,Q",
            "header_bytes": 0,
        },
        "started_utc": started_utc,
        "finished_utc": finished_utc,
        "requested": {
            "sample_rate_hz": config.sample_rate_hz,
            "center_frequency_hz": config.center_frequency_hz,
            "signal_frequency_hz": config.signal_frequency_hz,
            "lo_offset_hz": (
                config.center_frequency_hz - config.signal_frequency_hz
                if config.signal_frequency_hz is not None
                else None
            ),
            "duration_s": config.duration_s,
            "gain_db": config.gain_db,
            "bandwidth_hz": config.bandwidth_hz,
            "antenna": config.antenna,
            "agc": config.agc,
            "channel": config.channel,
        },
        "device": dict(source.metadata),
        "result": {
            "captured_samples": captured,
            "target_samples": target_samples,
            "sample_rate_hz": rate,
            "duration_s": duration,
            "completed": completed,
            "cancelled": cancelled,
            "error": failure_text,
            "peak_magnitude": peak,
            "rms_magnitude": rms,
            "clipped_fraction": clipped_fraction,
            "overflow_count": overflow_count,
            "read_timeouts": read_timeouts,
        },
        "user_metadata": dict(config.metadata),
    }

    try:
        _write_json_atomic(metadata_path, metadata)
    except Exception as exc:
        raise CaptureError(f"failed to write capture metadata: {exc}") from exc

    result = CaptureResult(
        output=output,
        metadata_path=metadata_path,
        captured_samples=captured,
        target_samples=target_samples,
        sample_rate_hz=rate,
        duration_s=duration,
        peak_magnitude=peak,
        rms_magnitude=rms,
        clipped_fraction=clipped_fraction,
        overflow_count=overflow_count,
        read_timeouts=read_timeouts,
        completed=completed,
        cancelled=cancelled,
        error=failure_text,
        metadata=metadata,
    )
    if failure is not None:
        raise failure
    return result


def _load_soapy() -> Any:
    try:
        import SoapySDR  # noqa: PLC0415
    except ImportError as exc:
        raise CaptureError(
            "SoapySDR Python bindings are unavailable; install the Nix package "
            "or the SoapySDR Python module"
        ) from exc
    return SoapySDR


def list_soapy_devices(device_args: str = "") -> list[dict[str, Any]]:
    """Return SoapySDR devices visible to the current process."""

    soapy = _load_soapy()
    try:
        if device_args:
            devices = soapy.Device.enumerate(device_args)
        else:
            devices = soapy.Device.enumerate()
    except Exception as exc:
        raise CaptureError(f"SoapySDR device enumeration failed: {exc}") from exc
    return [dict(device) for device in devices]


class SoapyIQSource:
    """SoapySDR-backed CF32 receive stream."""

    def __init__(self, config: SoapyDeviceConfig):
        self._soapy = _load_soapy()
        self._device: Any = None
        self._stream: Any = None
        self._closed = False
        self._rate = float(config.sample_rate_hz)
        self._metadata: dict[str, Any] = {
            "backend": "SoapySDR",
            "device_args": config.device_args,
            "channel": config.channel,
        }

        try:
            if config.device_args:
                device = self._soapy.Device(config.device_args)
            else:
                device = self._soapy.Device()
            self._device = device
            rx = self._soapy.SOAPY_SDR_RX
            channel = config.channel

            device.setSampleRate(rx, channel, self._rate)
            device.setFrequency(rx, channel, config.center_frequency_hz)
            if config.bandwidth_hz is not None:
                device.setBandwidth(rx, channel, config.bandwidth_hz)
            if config.antenna is not None:
                device.setAntenna(rx, channel, config.antenna)
            device.setGainMode(rx, channel, bool(config.agc))
            if not config.agc and config.gain_db is not None:
                device.setGain(rx, channel, config.gain_db)

            actual_rate = float(device.getSampleRate(rx, channel))
            actual_center = float(device.getFrequency(rx, channel))
            self._rate = actual_rate
            self._metadata.update(
                {
                    "driver": self._safe_get(device.getDriverKey),
                    "hardware": self._safe_get(device.getHardwareKey),
                    "hardware_info": self._safe_mapping(device.getHardwareInfo),
                    "actual_sample_rate_hz": actual_rate,
                    "actual_center_frequency_hz": actual_center,
                    "actual_bandwidth_hz": self._safe_float(
                        device.getBandwidth,
                        rx,
                        channel,
                    ),
                    "actual_gain_db": self._safe_float(
                        device.getGain,
                        rx,
                        channel,
                    ),
                    "gain_mode": "automatic" if config.agc else "manual",
                    "antenna": self._safe_get(
                        device.getAntenna,
                        rx,
                        channel,
                    ),
                }
            )

            stream = device.setupStream(
                rx,
                self._soapy.SOAPY_SDR_CF32,
                [channel],
            )
            self._stream = stream
            device.activateStream(stream)
        except Exception as exc:
            self.close()
            raise CaptureError(f"failed to initialize SoapySDR receiver: {exc}") from exc

    @staticmethod
    def _safe_get(function: Callable[..., Any], *args: Any) -> Any:
        try:
            return function(*args)
        except Exception:
            return None

    @staticmethod
    def _safe_float(function: Callable[..., Any], *args: Any) -> float | None:
        value = SoapyIQSource._safe_get(function, *args)
        try:
            return float(value) if value is not None else None
        except (TypeError, ValueError):
            return None

    @staticmethod
    def _safe_mapping(function: Callable[..., Any]) -> dict[str, Any]:
        value = SoapyIQSource._safe_get(function)
        if not isinstance(value, Mapping):
            return {}
        return {str(key): str(item) for key, item in value.items()}

    @property
    def sample_rate_hz(self) -> float:
        return self._rate

    @property
    def metadata(self) -> Mapping[str, Any]:
        return dict(self._metadata)

    def read_into(self, buffer: ComplexArray, timeout_us: int) -> IQRead:
        if self._closed or self._device is None or self._stream is None:
            raise CaptureError("SDR stream is not active")

        result = self._device.readStream(
            self._stream,
            [buffer],
            len(buffer),
            timeoutUs=timeout_us,
        )
        count = int(result.ret)
        if count >= 0:
            # Positive stream flags (HAS_TIME, MORE_FRAGMENTS, etc.) are not
            # errors. SOAPY_SDR_OVERFLOW is a negative return code, not a flag.
            return IQRead(status=ReadStatus.DATA, sample_count=count)
        if count == int(self._soapy.SOAPY_SDR_TIMEOUT):
            return IQRead(status=ReadStatus.TIMEOUT)
        if count == int(self._soapy.SOAPY_SDR_OVERFLOW):
            return IQRead(status=ReadStatus.OVERFLOW, overflow=True)

        message = self._safe_get(self._soapy.errToStr, count)
        raise CaptureError(f"SoapySDR read failed ({count}): {message or 'unknown error'}")

    def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        if self._device is not None and self._stream is not None:
            try:
                self._device.deactivateStream(self._stream)
            except Exception:
                pass
            try:
                self._device.closeStream(self._stream)
            except Exception:
                pass
        if self._device is not None:
            try:
                self._device.close()
            except Exception:
                pass
        self._stream = None
        self._device = None
