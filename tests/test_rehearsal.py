import pytest

from deviation_station.auto_cal import AutoCalConfig, AutoCalController, AutoCalState
from deviation_station.bessel import BesselNullDetector
from deviation_station.rehearsal import (
    BesselRehearsalPlant,
    RehearsalIO,
    TargetRehearsalPlant,
)
from deviation_station.target_cal import (
    TargetCalConfig,
    TargetCalController,
    TargetCalState,
)


def test_bessel_rehearsal_reaches_first_null() -> None:
    plant = BesselRehearsalPlant(
        tone_frequency_hz=1_000.0,
        null_index=1,
        null_level=0.40,
    )
    io = RehearsalIO(plant)
    clock = [0.0]
    detector = BesselNullDetector(
        tone_frequency_hz=1_000.0,
        null_index=1,
        sample_rate_hz=48_000.0,
    )
    controller = AutoCalController(
        AutoCalConfig(
            tone_frequency_hz=1_000.0,
            null_index=1,
            min_level=0.05,
            max_level=0.9,
            coarse_steps=10,
            fine_steps=5,
            settle_s=0.30,
            recovery_s=0.20,
            hold_s=0.20,
            carrier_timeout_s=0.5,
            max_duration_s=10.0,
        ),
        io,
        detector=detector,
        clock=lambda: clock[0],
    )
    progress = controller.start()
    for _ in range(500):
        if progress.state.terminal:
            break
        clock[0] += 0.05
        progress = controller.update(plant.spectrum())
    assert progress.state is AutoCalState.COMPLETE
    assert progress.captured is not None
    assert progress.captured.deviation_hz == pytest.approx(2404.825557695773)
    assert progress.best_level == pytest.approx(0.40, abs=0.03)
    assert io.key_count == 1
    assert io.unkey_count == 1
    assert plant.keyed is False
    assert plant.tone_on is False


def test_target_rehearsal_reaches_requested_deviation() -> None:
    plant = TargetRehearsalPlant()
    io = RehearsalIO(plant)
    clock = [0.0]
    controller = TargetCalController(
        TargetCalConfig(
            target_hz=2_000.0,
            tolerance_hz=40.0,
            min_level=0.05,
            max_level=1.0,
            coarse_steps=6,
            samples_per_step=2,
            settle_s=0.05,
            measurement_timeout_s=0.5,
            max_iterations=5,
            max_duration_s=10.0,
        ),
        io,
        clock=lambda: clock[0],
    )
    progress = controller.start()
    for _ in range(200):
        if progress.state.terminal:
            break
        clock[0] += 0.05
        progress = controller.update(plant.reading())
    assert progress.state is TargetCalState.COMPLETE
    assert progress.reading is not None
    assert progress.reading.deviation_hz == pytest.approx(2_000.0, abs=45.0)
    assert progress.reading.quality.status.value == "good"
    assert io.key_count == 1
    assert io.unkey_count == 1
    assert plant.keyed is False
    assert plant.tone_on is False
