import math

import pytest

from deviation_station.auto_cal import (
    AutoCalConfig,
    AutoCalController,
    AutoCalError,
    AutoCalState,
)
from deviation_station.bessel import BesselProgress
from deviation_station.measurement import (
    DeviationReading,
    MeasurementEngine,
    MeasurementQuality,
    ReadingStatus,
)


class FakeIO:
    def __init__(self) -> None:
        self.keyed = False
        self.tone_on = False
        self.level: float | None = None
        self.key_count = 0
        self.unkey_count = 0
        self.tone_start_count = 0
        self.fail_tone = False

    def key_ptt(self) -> bool:
        self.key_count += 1
        self.keyed = True
        return True

    def unkey_ptt(self) -> bool:
        self.unkey_count += 1
        self.keyed = False
        return True

    def start_tone(self, level: float) -> None:
        if self.fail_tone:
            raise RuntimeError("tone unavailable")
        self.tone_start_count += 1
        self.tone_on = True
        self.level = level

    def stop_tone(self) -> None:
        self.tone_on = False

    def set_level(self, level: float) -> None:
        self.level = level


class FakeMonitor:
    def __init__(self, io: FakeIO, depth_for_level) -> None:
        self.io = io
        self.depth_for_level = depth_for_level
        self.locked = True
        self.minimum = 100.0
        self.last_depth = 0.0

    def set_min_null_depth_db(self, value: float) -> None:
        self.minimum = float(value)

    def update(self, spectrum_db) -> BesselProgress:
        del spectrum_db
        depth = (
            self.depth_for_level(float(self.io.level))
            if self.io.tone_on and self.io.level is not None
            else 0.0
        )
        self.last_depth = depth
        return BesselProgress(
            state="SEEKING NULL" if depth >= 3.0 else "ARMED",
            carrier_locked=self.locked,
            tone_frequency_hz=1_000.0,
            null_index=1,
            null_depth_db=depth,
        )

    def capture_manual(self) -> BesselProgress:
        captured = None
        if self.last_depth >= self.minimum:
            status = (
                ReadingStatus.GOOD
                if self.last_depth >= 25.0
                else ReadingStatus.MARGINAL
            )
            captured = DeviationReading(
                deviation_hz=2_404.825557695773,
                engine=MeasurementEngine.BESSEL,
                profile_id="bessel-j0-1",
                definition="J0 carrier null 1",
                quality=MeasurementQuality(
                    status=status,
                    carrier_locked=True,
                ),
                metadata={"null_depth_db": self.last_depth},
            )
        return BesselProgress(
            state="CAPTURED" if captured is not None else "NULL NOT DEEP ENOUGH",
            carrier_locked=True,
            tone_frequency_hz=1_000.0,
            null_index=1,
            null_depth_db=self.last_depth,
            captured=captured,
        )


def _config(**overrides) -> AutoCalConfig:
    values = {
        "tone_frequency_hz": 1_000.0,
        "null_index": 1,
        "min_level": 0.03,
        "max_level": 1.0,
        "coarse_steps": 21,
        "fine_steps": 9,
        "settle_s": 0.10,
        "recovery_s": 0.20,
        "hold_s": 0.10,
        "carrier_timeout_s": 0.50,
        "max_duration_s": 12.0,
    }
    values.update(overrides)
    return AutoCalConfig(**values)


def _run(controller: AutoCalController, clock: list[float], seconds: float = 15.0):
    progress = controller.start()
    while not progress.state.terminal and clock[0] < seconds:
        clock[0] += 0.05
        progress = controller.update(tuple([-120.0] * 1024))
    return progress


def _gaussian(level: float, center: float, width: float = 0.05) -> float:
    return 28.0 * math.exp(-((level - center) / width) ** 2)


def test_auto_cal_finds_and_refines_first_null() -> None:
    io = FakeIO()
    monitor = FakeMonitor(io, lambda level: _gaussian(level, 0.603))
    clock = [0.0]
    controller = AutoCalController(
        _config(),
        io,
        detector=monitor,
        clock=lambda: clock[0],
    )

    progress = _run(controller, clock)

    assert progress.state is AutoCalState.COMPLETE
    assert progress.captured is not None
    assert progress.captured.deviation_hz == pytest.approx(2_404.825557695773)
    assert progress.best_level == pytest.approx(0.603, abs=0.025)
    assert progress.best_depth_db is not None and progress.best_depth_db >= 25.0
    assert io.keyed is False
    assert io.tone_on is False
    assert io.tone_start_count == 3


def test_auto_cal_selects_requested_second_null() -> None:
    io = FakeIO()

    def depth(level: float) -> float:
        return max(_gaussian(level, 0.38), _gaussian(level, 0.82))

    monitor = FakeMonitor(io, depth)
    clock = [0.0]
    controller = AutoCalController(
        _config(null_index=2),
        io,
        detector=monitor,
        clock=lambda: clock[0],
    )

    progress = _run(controller, clock)

    assert progress.state is AutoCalState.COMPLETE
    assert progress.captured is not None
    assert progress.best_level == pytest.approx(0.82, abs=0.04)
    assert io.tone_start_count == 3


def test_auto_cal_fails_honestly_when_requested_null_is_missing() -> None:
    io = FakeIO()
    monitor = FakeMonitor(io, lambda level: _gaussian(level, 0.40))
    clock = [0.0]
    controller = AutoCalController(
        _config(null_index=2),
        io,
        detector=monitor,
        clock=lambda: clock[0],
    )

    progress = _run(controller, clock)

    assert progress.state is AutoCalState.FAILED
    assert "only 1 carrier null" in progress.message
    assert progress.captured is None
    assert io.keyed is False
    assert io.tone_on is False


def test_auto_cal_times_out_without_carrier() -> None:
    io = FakeIO()
    monitor = FakeMonitor(io, lambda level: _gaussian(level, 0.5))
    monitor.locked = False
    clock = [0.0]
    controller = AutoCalController(
        _config(carrier_timeout_s=0.30),
        io,
        detector=monitor,
        clock=lambda: clock[0],
    )

    progress = _run(controller, clock)

    assert progress.state is AutoCalState.FAILED
    assert "no carrier" in progress.message
    assert io.tone_start_count == 0
    assert io.keyed is False


def test_auto_cal_cancel_stops_tone_and_ptt() -> None:
    io = FakeIO()
    monitor = FakeMonitor(io, lambda level: _gaussian(level, 0.5))
    clock = [0.0]
    controller = AutoCalController(
        _config(),
        io,
        detector=monitor,
        clock=lambda: clock[0],
    )
    progress = controller.start()
    for _ in range(20):
        clock[0] += 0.05
        progress = controller.update(tuple([-120.0] * 1024))
        if progress.level is not None:
            break
    assert progress.running

    progress = controller.cancel()

    assert progress.state is AutoCalState.CANCELLED
    assert io.keyed is False
    assert io.tone_on is False
    assert io.unkey_count == 1


def test_auto_cal_fails_cleanly_when_tone_cannot_start() -> None:
    io = FakeIO()
    io.fail_tone = True
    monitor = FakeMonitor(io, lambda level: _gaussian(level, 0.5))
    clock = [0.0]
    controller = AutoCalController(
        _config(),
        io,
        detector=monitor,
        clock=lambda: clock[0],
    )

    progress = _run(controller, clock, seconds=2.0)

    assert progress.state is AutoCalState.FAILED
    assert "tone output failed" in progress.message
    assert io.keyed is False
    assert io.tone_on is False


def test_auto_cal_rejects_invalid_levels() -> None:
    with pytest.raises(AutoCalError):
        _config(min_level=0.6, max_level=0.4)
    with pytest.raises(AutoCalError):
        _config(min_level=-0.1)
    with pytest.raises(AutoCalError):
        _config(coarse_steps=2)
    with pytest.raises(AutoCalError):
        _config(minimum_depth_db=10.0)
