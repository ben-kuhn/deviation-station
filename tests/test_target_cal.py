import math

import pytest

from deviation_meter.measurement import (
    DeviationReading,
    MeasurementEngine,
    MeasurementQuality,
    ReadingStatus,
)
from deviation_meter.target_cal import (
    TargetCalConfig,
    TargetCalController,
    TargetCalError,
    TargetCalState,
)


class FakeIO:
    def __init__(self) -> None:
        self.keyed = False
        self.tone_on = False
        self.level = 0.0
        self.key_count = 0
        self.unkey_count = 0
        self.levels: list[float] = []

    def key_ptt(self) -> bool:
        self.key_count += 1
        self.keyed = True
        return True

    def unkey_ptt(self) -> bool:
        self.unkey_count += 1
        self.keyed = False
        return True

    def start_tone(self, level: float) -> None:
        self.tone_on = True
        self.level = level

    def stop_tone(self) -> None:
        self.tone_on = False

    def set_level(self, level: float) -> None:
        self.level = level
        self.levels.append(level)


def _reading(value: float, status: ReadingStatus = ReadingStatus.GOOD) -> DeviationReading:
    return DeviationReading(
        deviation_hz=value,
        engine=MeasurementEngine.DISCRIMINATOR,
        profile_id="generic-fm",
        definition="robust instantaneous-frequency peak",
        quality=MeasurementQuality(status=status, sample_count=128),
        metadata={"source": "test"},
    )


def _config(**overrides) -> TargetCalConfig:
    values = {
        "target_hz": 2000.0,
        "tolerance_hz": 30.0,
        "relative_tolerance": 0.01,
        "min_level": 0.03,
        "max_level": 1.0,
        "coarse_steps": 6,
        "samples_per_step": 2,
        "settle_s": 0.10,
        "measurement_timeout_s": 1.0,
        "max_iterations": 5,
        "max_duration_s": 12.0,
    }
    values.update(overrides)
    return TargetCalConfig(**values)


def _run(controller, io, clock, *, curve, limit=100, status=ReadingStatus.GOOD):
    progress = controller.start()
    for _ in range(limit):
        if progress.state.terminal:
            break
        clock[0] += 0.25
        progress = controller.update(_reading(curve(io.level), status))
    return progress


def test_target_control_brackets_and_refines_linear_level() -> None:
    io = FakeIO()
    clock = [0.0]
    controller = TargetCalController(
        _config(),
        io,
        clock=lambda: clock[0],
    )
    progress = _run(controller, io, clock, curve=lambda level: level * 4000.0)

    assert progress.state is TargetCalState.COMPLETE
    assert progress.reading is not None
    assert progress.reading.deviation_hz == pytest.approx(2000.0, abs=30.0)
    assert progress.level == pytest.approx(0.5, abs=0.03)
    assert progress.error_hz is not None and abs(progress.error_hz) <= 30.0
    assert progress.reading.metadata["target_deviation_hz"] == 2000.0
    assert io.keyed is False
    assert io.tone_on is False


def test_target_control_handles_nonlinear_level_curve() -> None:
    io = FakeIO()
    clock = [0.0]
    controller = TargetCalController(
        _config(target_hz=1800.0),
        io,
        clock=lambda: clock[0],
    )
    progress = _run(
        controller,
        io,
        clock,
        curve=lambda level: 5200.0 * math.pow(max(level, 1e-9), 1.35),
    )
    assert progress.state is TargetCalState.COMPLETE
    assert progress.reading is not None
    assert progress.reading.deviation_hz == pytest.approx(1800.0, abs=40.0)


def test_target_control_fails_when_target_is_above_range() -> None:
    io = FakeIO()
    clock = [0.0]
    controller = TargetCalController(
        _config(target_hz=3000.0),
        io,
        clock=lambda: clock[0],
    )
    progress = _run(controller, io, clock, curve=lambda level: level * 1000.0)
    assert progress.state is TargetCalState.FAILED
    assert "maximum" in progress.message
    assert io.keyed is False
    assert io.tone_on is False


def test_target_control_fails_when_target_is_below_minimum() -> None:
    io = FakeIO()
    clock = [0.0]
    controller = TargetCalController(
        _config(target_hz=1500.0),
        io,
        clock=lambda: clock[0],
    )
    progress = _run(controller, io, clock, curve=lambda level: 2000.0 + level * 1000.0)
    assert progress.state is TargetCalState.FAILED
    assert "minimum" in progress.message


def test_target_control_times_out_without_valid_readings() -> None:
    io = FakeIO()
    clock = [0.0]
    controller = TargetCalController(
        _config(measurement_timeout_s=0.3),
        io,
        clock=lambda: clock[0],
    )
    progress = controller.start()
    for _ in range(10):
        clock[0] += 0.1
        progress = controller.update(None)
    assert progress.state is TargetCalState.FAILED
    assert "no valid" in progress.message
    assert io.keyed is False


def test_target_control_cancel_stops_tone_and_ptt() -> None:
    io = FakeIO()
    clock = [0.0]
    controller = TargetCalController(_config(), io, clock=lambda: clock[0])
    controller.start()
    clock[0] += 0.2
    controller.update(_reading(100.0))
    progress = controller.cancel()
    assert progress.state is TargetCalState.CANCELLED
    assert io.keyed is False
    assert io.tone_on is False


def test_target_control_accepts_marginal_readings() -> None:
    io = FakeIO()
    clock = [0.0]
    controller = TargetCalController(_config(), io, clock=lambda: clock[0])
    progress = _run(
        controller,
        io,
        clock,
        curve=lambda level: level * 4000.0,
        status=ReadingStatus.MARGINAL,
    )
    assert progress.state is TargetCalState.COMPLETE
    assert progress.reading is not None
    assert progress.reading.quality.status is ReadingStatus.MARGINAL


def test_target_config_rejects_invalid_ranges() -> None:
    with pytest.raises(TargetCalError):
        _config(min_level=0.8, max_level=0.2)
    with pytest.raises(TargetCalError):
        _config(target_hz=0.0)
    with pytest.raises(TargetCalError):
        _config(samples_per_step=0)
