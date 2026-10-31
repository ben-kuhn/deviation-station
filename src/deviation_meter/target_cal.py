"""Closed-loop discriminator target-deviation calibration."""

from __future__ import annotations

import math
import statistics
import time
from collections.abc import Callable, Sequence
from dataclasses import dataclass, replace
from enum import Enum

from .auto_cal import AutoCalIO
from .measurement import DeviationReading


class TargetCalError(ValueError):
    """Raised when target calibration parameters are invalid."""


class TargetCalState(str, Enum):
    IDLE = "IDLE"
    SETTLING = "SETTLING"
    COARSE = "COARSE SWEEP"
    REFINING = "REFINING"
    COMPLETE = "COMPLETE"
    FAILED = "FAILED"
    CANCELLED = "CANCELLED"

    @property
    def terminal(self) -> bool:
        return self in (
            TargetCalState.COMPLETE,
            TargetCalState.FAILED,
            TargetCalState.CANCELLED,
        )


@dataclass(frozen=True, slots=True)
class TargetCalConfig:
    target_hz: float = 3_000.0
    tolerance_hz: float = 50.0
    relative_tolerance: float = 0.02
    min_level: float = 0.03
    max_level: float = 1.0
    coarse_steps: int = 6
    samples_per_step: int = 2
    settle_s: float = 0.45
    measurement_timeout_s: float = 3.5
    max_iterations: int = 4
    max_duration_s: float = 18.0

    def __post_init__(self) -> None:
        if not math.isfinite(self.target_hz) or self.target_hz <= 0.0:
            raise TargetCalError("target_hz must be finite and positive")
        if not math.isfinite(self.tolerance_hz) or self.tolerance_hz <= 0.0:
            raise TargetCalError("tolerance_hz must be finite and positive")
        if not 0.0 <= self.relative_tolerance <= 0.25:
            raise TargetCalError("relative_tolerance must be between 0 and 0.25")
        if not 0.0 <= self.min_level < self.max_level <= 1.0:
            raise TargetCalError("levels must satisfy 0 <= min < max <= 1")
        if self.coarse_steps < 2:
            raise TargetCalError("coarse_steps must be at least 2")
        if self.samples_per_step < 1:
            raise TargetCalError("samples_per_step must be at least 1")
        if self.max_iterations < 1:
            raise TargetCalError("max_iterations must be at least 1")
        for name in (
            "settle_s",
            "measurement_timeout_s",
            "max_duration_s",
        ):
            value = getattr(self, name)
            if not math.isfinite(value) or value <= 0.0:
                raise TargetCalError(f"{name} must be finite and positive")


@dataclass(frozen=True, slots=True)
class TargetCalProgress:
    state: TargetCalState
    message: str
    target_hz: float
    level: float | None = None
    measured_hz: float | None = None
    error_hz: float | None = None
    reading: DeviationReading | None = None
    elapsed_s: float = 0.0
    step: int = 0
    total_steps: int = 0
    fraction: float = 0.0

    @property
    def running(self) -> bool:
        return not self.state.terminal and self.state is not TargetCalState.IDLE


class TargetCalController:
    """Bracket and refine a discriminator reading to a target deviation."""

    def __init__(
        self,
        config: TargetCalConfig,
        io: AutoCalIO,
        *,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self.config = config
        self._io = io
        self._clock = clock
        self._state = TargetCalState.IDLE
        self._message = "Idle"
        self._started_at: float | None = None
        self._settle_deadline = 0.0
        self._last_valid_at: float | None = None
        self._level: float | None = None
        self._samples: list[float] = []
        self._last_reading: DeviationReading | None = None
        self._measured_hz: float | None = None
        self._error_hz: float | None = None
        self._final_reading: DeviationReading | None = None
        self._coarse_levels: tuple[float, ...] = ()
        self._coarse_index = 0
        self._coarse_direction = 0
        self._low: tuple[float, float] | None = None
        self._high: tuple[float, float] | None = None
        self._iteration = 0
        self._step = 0
        self._ptt_keyed = False
        self._tone_active = False
        self._total_steps = config.coarse_steps + config.max_iterations

    def progress(self) -> TargetCalProgress:
        elapsed = (
            max(0.0, self._clock() - self._started_at)
            if self._started_at is not None
            else 0.0
        )
        denominator = max(1, self._total_steps)
        return TargetCalProgress(
            state=self._state,
            message=self._message,
            target_hz=self.config.target_hz,
            level=self._level,
            measured_hz=self._measured_hz,
            error_hz=self._error_hz,
            reading=self._final_reading or self._last_reading,
            elapsed_s=elapsed,
            step=self._step,
            total_steps=self._total_steps,
            fraction=min(1.0, self._step / denominator),
        )

    def start(self) -> TargetCalProgress:
        if self._state is not TargetCalState.IDLE and not self._state.terminal:
            raise TargetCalError("target calibration is already running")
        self._reset()
        self._started_at = self._clock()
        self._state = TargetCalState.SETTLING
        self._message = "Keying transmitter and starting low-level tone"
        try:
            if not self._io.key_ptt():
                return self._fail("PTT readback did not confirm transmission")
            self._ptt_keyed = True
            self._level = min(
                self.config.max_level,
                max(self.config.min_level, 0.5),
            )
            self._io.start_tone(self._level)
            self._tone_active = True
            self._settle_deadline = self._started_at + self.config.settle_s
            self._last_valid_at = self._started_at
        except Exception as exc:
            return self._fail(f"start failed: {exc}")
        return self.progress()

    def update(
        self,
        reading: DeviationReading | None,
        *,
        now: float | None = None,
    ) -> TargetCalProgress:
        if self._state is TargetCalState.IDLE or self._state.terminal:
            return self.progress()
        current = self._clock() if now is None else now
        assert self._started_at is not None
        if current - self._started_at > self.config.max_duration_s:
            return self._fail("target calibration exceeded its maximum duration")
        if (
            reading is None
            or reading.deviation_hz is None
            or not reading.valid
        ):
            if (
                self._last_valid_at is not None
                and current - self._last_valid_at > self.config.measurement_timeout_s
            ):
                return self._fail("no valid discriminator reading before timeout")
            return self.progress()
        self._last_valid_at = current
        self._last_reading = reading
        if current < self._settle_deadline:
            return self.progress()
        self._samples.append(float(reading.deviation_hz))
        if len(self._samples) < self.config.samples_per_step:
            return self.progress()

        measured = float(statistics.median(self._samples))
        self._samples.clear()
        self._measured_hz = measured
        self._error_hz = measured - self.config.target_hz
        if self._state is TargetCalState.SETTLING:
            self._state = TargetCalState.COARSE
        if self._state is TargetCalState.COARSE:
            self._process_coarse(measured, current)
        elif self._state is TargetCalState.REFINING:
            self._process_refining(measured, current)
        return self.progress()

    def cancel(self, reason: str = "cancelled by operator") -> TargetCalProgress:
        if self._state is TargetCalState.IDLE or self._state.terminal:
            return self.progress()
        cleanup_error = self._cleanup()
        self._state = TargetCalState.CANCELLED
        self._message = f"Target calibration {reason}"
        if cleanup_error:
            self._message = f"{self._message}; {cleanup_error}"
        return self.progress()

    def _reset(self) -> None:
        self._message = ""
        self._settle_deadline = 0.0
        self._last_valid_at = None
        self._level = None
        self._samples = []
        self._last_reading = None
        self._measured_hz = None
        self._error_hz = None
        self._final_reading = None
        self._coarse_levels = ()
        self._coarse_index = 0
        self._coarse_direction = 0
        self._low = None
        self._high = None
        self._iteration = 0
        self._step = 0
        self._ptt_keyed = False
        self._tone_active = False

    def _tolerance_hz(self) -> float:
        return max(
            self.config.tolerance_hz,
            self.config.target_hz * self.config.relative_tolerance,
        )

    def _coarse_levels_to(self, end_level: float) -> tuple[float, ...]:
        assert self._level is not None
        start = self._level
        if self.config.coarse_steps <= 1:
            return (start,)
        return tuple(
            start
            + (end_level - start)
            * index
            / (self.config.coarse_steps - 1)
            for index in range(self.config.coarse_steps)
        )

    def _process_coarse(self, measured: float, now: float) -> None:
        if self._level is None:
            self._fail("internal level state is missing")
            return
        error = measured - self.config.target_hz
        if abs(error) <= self._tolerance_hz():
            self._complete(measured)
            return
        if not self._coarse_levels:
            if measured < self.config.target_hz:
                self._low = (self._level, measured)
                self._coarse_direction = 1
                self._coarse_levels = self._coarse_levels_to(
                    self.config.max_level
                )
            else:
                self._high = (self._level, measured)
                self._coarse_direction = -1
                self._coarse_levels = self._coarse_levels_to(
                    self.config.min_level
                )
            self._coarse_index = 1
            self._step = 1
            if len(self._coarse_levels) <= 1:
                self._fail(
                    "target is outside the available audio-level range"
                )
                return
            self._set_level(self._coarse_levels[self._coarse_index], now)
            direction = "upward" if self._coarse_direction > 0 else "downward"
            self._message = (
                f"Coarse {direction} sweep 2/{self.config.coarse_steps}"
            )
            return

        if measured < self.config.target_hz:
            self._low = (self._level, measured)
            if self._high is not None:
                self._begin_refining(now)
                return
        else:
            self._high = (self._level, measured)
            if self._low is not None:
                self._begin_refining(now)
                return

        self._coarse_index += 1
        self._step = self._coarse_index
        if self._coarse_index >= len(self._coarse_levels):
            if self._coarse_direction > 0:
                self._fail("target is above the maximum audio level")
            else:
                self._fail("target is below the minimum audio level")
            return
        self._set_level(self._coarse_levels[self._coarse_index], now)
        direction = "upward" if self._coarse_direction > 0 else "downward"
        self._message = (
            f"Coarse {direction} sweep {self._coarse_index + 1}/"
            f"{self.config.coarse_steps}"
        )

    def _begin_refining(self, now: float) -> None:
        assert self._low is not None
        assert self._high is not None
        self._state = TargetCalState.REFINING
        self._iteration = 1
        self._step = self.config.coarse_steps
        self._message = "Target bracketed; refining level"
        level = self._interpolated_level()
        self._set_level(level, now)

    def _process_refining(self, measured: float, now: float) -> None:
        assert self._low is not None
        assert self._high is not None
        if self._level is None:
            self._fail("internal level state is missing")
            return
        error = measured - self.config.target_hz
        self._error_hz = error
        if abs(error) <= self._tolerance_hz():
            self._complete(measured)
            return
        if measured < self.config.target_hz:
            self._low = (self._level, measured)
        else:
            self._high = (self._level, measured)
        self._iteration += 1
        self._step = self.config.coarse_steps + self._iteration - 1
        if self._iteration > self.config.max_iterations:
            self._fail(
                f"could not reach target within tolerance "
                f"(last {measured:.0f} Hz)"
            )
            return
        level = self._interpolated_level()
        if abs(level - self._level) < 1e-4:
            self._fail(
                f"level resolution cannot meet target "
                f"(last {measured:.0f} Hz)"
            )
            return
        self._set_level(level, now)
        self._message = (
            f"Refining {self._iteration}/{self.config.max_iterations} · "
            f"{measured:.0f} Hz"
        )

    def _interpolated_level(self) -> float:
        assert self._low is not None
        assert self._high is not None
        (low_level, low_value) = self._low
        (high_level, high_value) = self._high
        span = high_value - low_value
        if abs(span) < 1e-9:
            return 0.5 * (low_level + high_level)
        fraction = (self.config.target_hz - low_value) / span
        level = low_level + fraction * (high_level - low_level)
        return min(max(level, self.config.min_level), self.config.max_level)

    def _set_level(self, level: float, now: float) -> None:
        self._level = level
        self._samples.clear()
        self._settle_deadline = now + self.config.settle_s
        try:
            self._io.set_level(level)
        except Exception as exc:
            self._fail(f"audio level control failed: {exc}")

    def _complete(self, measured: float) -> None:
        if self._last_reading is None:
            self._fail("no discriminator reading available for completion")
            return
        error = measured - self.config.target_hz
        final = replace(
            self._last_reading,
            deviation_hz=measured,
            metadata={
                **dict(self._last_reading.metadata),
                "target_deviation_hz": self.config.target_hz,
                "target_error_hz": error,
                "target_tolerance_hz": self._tolerance_hz(),
                "target_level": self._level,
            },
        )
        self._final_reading = final
        cleanup_error = self._cleanup()
        self._state = TargetCalState.COMPLETE
        self._message = (
            f"Target {self.config.target_hz:.0f} Hz reached at "
            f"{measured:.0f} Hz"
        )
        if cleanup_error:
            self._state = TargetCalState.FAILED
            self._message = f"{self._message}; {cleanup_error}"

    def _cleanup(self) -> str:
        errors: list[str] = []
        if self._tone_active:
            self._tone_active = False
            try:
                self._io.stop_tone()
            except Exception as exc:
                errors.append(f"tone stop failed: {exc}")
        if self._ptt_keyed:
            self._ptt_keyed = False
            try:
                if not self._io.unkey_ptt():
                    errors.append("PTT unkey readback failed")
            except Exception as exc:
                errors.append(f"PTT unkey failed: {exc}")
        return "; ".join(errors)

    def _fail(self, reason: str) -> TargetCalProgress:
        cleanup_error = self._cleanup()
        self._state = TargetCalState.FAILED
        self._message = f"Target calibration failed: {reason}"
        if cleanup_error:
            self._message = f"{self._message}; {cleanup_error}"
        return self.progress()
