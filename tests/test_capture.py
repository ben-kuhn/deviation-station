import json
from pathlib import Path

import numpy as np
import pytest

from types import SimpleNamespace

from deviation_station.capture import (
    CaptureConfig,
    CaptureError,
    IQRead,
    ReadStatus,
    SoapyIQSource,
    capture_iq,
)


class FakeIQSource:
    """Deterministic streaming fixture used only to test the recorder plumbing."""

    def __init__(
        self,
        samples: np.ndarray,
        *,
        sample_rate_hz: float = 10_000.0,
        timeouts: bool = False,
        interrupt_after: int | None = None,
        overflow_reads: set[int] | None = None,
    ) -> None:
        self._samples = np.asarray(samples, dtype=np.complex64)
        self._rate = sample_rate_hz
        self._position = 0
        self._read_count = 0
        self._timeouts = timeouts
        self._interrupt_after = interrupt_after
        self._overflow_reads = overflow_reads or set()
        self.closed = False

    @property
    def sample_rate_hz(self) -> float:
        return self._rate

    @property
    def metadata(self):
        return {"backend": "test-double", "driver": "fake"}

    def read_into(self, buffer: np.ndarray, timeout_us: int) -> IQRead:
        del timeout_us
        if self.closed:
            raise AssertionError("read after close")
        if self._timeouts:
            return IQRead(status=ReadStatus.TIMEOUT)
        if (
            self._interrupt_after is not None
            and self._read_count >= self._interrupt_after
        ):
            raise KeyboardInterrupt
        self._read_count += 1
        if self._read_count in self._overflow_reads:
            return IQRead(status=ReadStatus.OVERFLOW, overflow=True)
        remaining = self._samples.size - self._position
        count = min(buffer.size, remaining)
        if count <= 0:
            return IQRead(status=ReadStatus.TIMEOUT)
        buffer[:count] = self._samples[self._position : self._position + count]
        self._position += count
        return IQRead(status=ReadStatus.DATA, sample_count=count)

    def close(self) -> None:
        self.closed = True


def _config(
    output: Path,
    *,
    duration_s: float = 0.5,
    sample_rate_hz: float = 10_000.0,
    overwrite: bool = False,
    **overrides,
) -> CaptureConfig:
    return CaptureConfig(
        output=output,
        sample_rate_hz=sample_rate_hz,
        center_frequency_hz=145_600_000.0,
        signal_frequency_hz=145_500_000.0,
        duration_s=duration_s,
        overwrite=overwrite,
        metadata={"mode": "M17"},
        **overrides,
    )


def test_capture_writes_cf32_and_metadata(tmp_path: Path) -> None:
    rng = np.random.default_rng(5)
    samples = (
        rng.normal(scale=0.2, size=5_000)
        + 1j * rng.normal(scale=0.2, size=5_000)
    ).astype(np.complex64)
    source = FakeIQSource(samples)
    progress: list[tuple[int, int, float]] = []

    result = capture_iq(
        _config(tmp_path / "capture.cf32"),
        source,
        progress=lambda captured, target, elapsed: progress.append(
            (captured, target, elapsed)
        ),
    )

    assert result.completed
    assert not result.cancelled
    assert result.captured_samples == 5_000
    assert result.output.stat().st_size == 5_000 * 8
    stored = np.fromfile(result.output, dtype="<c8")
    assert stored == pytest.approx(samples)
    assert source.closed
    assert progress

    metadata = json.loads(result.metadata_path.read_text())
    assert metadata["format"]["name"] == "cf32"
    assert metadata["format"]["byte_order"] == "little"
    assert metadata["requested"]["lo_offset_hz"] == pytest.approx(100_000.0)
    assert metadata["result"]["completed"] is True
    assert metadata["result"]["captured_samples"] == 5_000
    assert metadata["user_metadata"]["mode"] == "M17"
    assert metadata["device"]["backend"] == "test-double"


def test_capture_rejects_existing_output_and_closes_source(tmp_path: Path) -> None:
    output = tmp_path / "capture.cf32"
    output.write_bytes(b"existing")
    source = FakeIQSource(np.zeros(10, dtype=np.complex64))
    with pytest.raises(CaptureError, match="already exists"):
        capture_iq(_config(output), source)
    assert source.closed


def test_capture_finalizes_metadata_on_interrupt(tmp_path: Path) -> None:
    rng = np.random.default_rng(6)
    samples = (rng.normal(size=5_000) + 1j * rng.normal(size=5_000)).astype(
        np.complex64
    )
    source = FakeIQSource(samples, interrupt_after=1)
    output = tmp_path / "interrupted.cf32"

    result = capture_iq(_config(output, chunk_samples=1024), source)

    assert result.cancelled
    assert not result.completed
    assert result.captured_samples > 0
    assert output.stat().st_size == result.captured_samples * 8
    metadata = json.loads(result.metadata_path.read_text())
    assert metadata["result"]["cancelled"] is True
    assert metadata["result"]["completed"] is False
    assert source.closed


def test_capture_writes_metadata_when_stream_times_out(tmp_path: Path) -> None:
    output = tmp_path / "timeout.cf32"
    source = FakeIQSource(
        np.zeros(10, dtype=np.complex64),
        timeouts=True,
    )
    with pytest.raises(CaptureError, match="timed out"):
        capture_iq(
            _config(output, max_consecutive_timeouts=2),
            source,
        )
    metadata_path = output.with_name(output.name + ".json")
    assert metadata_path.exists()
    metadata = json.loads(metadata_path.read_text())
    assert metadata["result"]["completed"] is False
    assert "timed out" in metadata["result"]["error"]
    assert source.closed


def test_capture_records_overflow_and_clipping_statistics(tmp_path: Path) -> None:
    samples = np.full(2_000, 1.2 + 0.0j, dtype=np.complex64)
    source = FakeIQSource(samples, overflow_reads={1})
    result = capture_iq(
        _config(tmp_path / "clipped.cf32", duration_s=0.2),
        source,
    )
    assert result.overflow_count == 1
    assert result.clipped_fraction == pytest.approx(1.0)
    assert result.peak_magnitude == pytest.approx(1.2, rel=1e-6)


def test_capture_config_validates_parameters(tmp_path: Path) -> None:
    with pytest.raises(CaptureError):
        _config(tmp_path / "bad.cf32", sample_rate_hz=0.0)
    with pytest.raises(CaptureError):
        _config(tmp_path / "bad.cf32", duration_s=0.0)
    with pytest.raises(CaptureError):
        _config(tmp_path / "bad.cf32", chunk_samples=10)


class _FakeSoapyConstants:
    SOAPY_SDR_TIMEOUT = -1
    SOAPY_SDR_OVERFLOW = -4


class _FakeSoapyDevice:
    def __init__(self, ret: int, flags: int = 0) -> None:
        self.ret = ret
        self.flags = flags

    def readStream(self, stream, buffers, count, timeoutUs):
        del stream, buffers, count, timeoutUs
        return SimpleNamespace(ret=self.ret, flags=self.flags)


def _soapy_source_with_result(ret: int, flags: int = 0) -> SoapyIQSource:
    source = SoapyIQSource.__new__(SoapyIQSource)
    source._soapy = _FakeSoapyConstants()
    source._device = _FakeSoapyDevice(ret, flags)
    source._stream = object()
    source._closed = False
    source._rate = 1_000_000.0
    source._metadata = {}
    return source


def test_positive_soapy_flags_are_not_overflows() -> None:
    source = _soapy_source_with_result(1024, flags=36)
    read = source.read_into(np.empty(1024, dtype=np.complex64), 1_000)
    assert read.status is ReadStatus.DATA
    assert read.sample_count == 1024
    assert read.overflow is False


def test_negative_soapy_overflow_code_is_reported() -> None:
    source = _soapy_source_with_result(-4)
    read = source.read_into(np.empty(1024, dtype=np.complex64), 1_000)
    assert read.status is ReadStatus.OVERFLOW
    assert read.overflow is True
