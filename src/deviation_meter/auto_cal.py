"""Closed-loop Bessel-null seeking for the Auto-Calibrate tab."""

from __future__ import annotations

import math
import time
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from enum import Enum
from typing import Protocol

from .bessel import BesselNullDetector, BesselProgress, carrier_null_index
from .measurement import DeviationReading


class AutoCalError(ValueError):
    """Raised when an auto-calibration request has invalid parameters."""


class AutoCalState(str, Enum):
    IDLE = "IDLE"
    WAITING_FOR_CARRIER = "WAITING FOR CARRIER"
    COARSE_SWEEP = "COARSE SWEEP"
    REFINING = "REFINING"
    RECOVERING = "RECOVERING CARRIER"
    HOLDING = "HOLDING NULL"
    COMPLETE = "COMPLETE"
    FAILED = "FAILED"
    CANCELLED = "CANCELLED"

    @property
    def terminal(self) -> bool:
        return self in (
            AutoCalState.COMPLETE,
            AutoCalState.FAILED,
            AutoCalState.CANCELLED,
        )


@dataclass(frozen=True, slots=True)
class AutoCalConfig:
    """Bounded search parameters for one internal-generator run."""

    tone_frequency_hz: float = 1_000.0
    null_index: int = 1
    min_level: float = 0.03
    max_level: float = 1.0
    coarse_steps: int = 14
    fine_steps: int = 9
    settle_s: float = 0.25
    recovery_s: float = 0.40
    hold_s: float = 0.60
    carrier_timeout_s: float = 4.0
    minimum_depth_db: float = 15.0
    max_duration_s: float = 18.0

    def __post_init__(self) -> None:
        if not math.isfinite(self.tone_frequency_hz) or self.tone_frequency_hz <= 0:
            raise AutoCalError("tone_frequency_hz must be finite and positive")
        carrier_null_index(self.null_index)
        if not 0.0 <= self.min_level < self.max_level <= 1.0:
            raise AutoCalError("audio levels must satisfy 0 <= min < max <= 1")
        if self.coarse_steps < 3:
            raise AutoCalError("coarse_steps must be at least 3")
        if self.fine_steps < 3:
            raise AutoCalError("fine_steps must be at least 3")
        for name in (
            "settle_s",
            "recovery_s",
            "hold_s",
            "carrier_timeout_s",
            "max_duration_s",
        ):
            value = getattr(self, name)
            if not math.isfinite(value) or value <= 0.0:
                raise AutoCalError(f"{name} must be finite and positive")
        if not math.isfinite(self.minimum_depth_db) or self.minimum_depth_db <= 10.0:
            raise AutoCalError("minimum_depth_db must be finite and above 10 dB")


class AutoCalIO(Protocol):
    """Hardware effects used by the controller, supplied by the UI."""

    def key_ptt(self) -> bool: ...

    def unkey_ptt(self) -> bool: ...

    def start_tone(self, level: float) -> None: ...

    def stop_tone(self) -> None: ...

    def set_level(self, level: float) -> None: ...


class CarrierMonitor(Protocol):
    """Subset of :class:`BesselNullDetector` used by the controller."""

    def update(self, spectrum_db: Sequence[float]) -> BesselProgress: ...

    def capture_manual(self) -> BesselProgress: ...

    def set_min_null_depth_db(self, min_null_depth_db: float) -> None: ...


@dataclass(frozen=True, slots=True)
class AutoCalProgress:
    state: AutoCalState
    message: str
    level: float | None = None
    best_depth_db: float | None = None
    best_level: float | None = None
    captured: DeviationReading | None = None
    bessel: BesselProgress | None = None
    elapsed_s: float = 0.0
    step: int = 0
    total_steps: int = 0

    @property
    def running(self) -> bool:
        return self.state not in (
            AutoCalState.IDLE,
            AutoCalState.COMPLETE,
            AutoCalState.FAILED,
            AutoCalState.CANCELLED,
        )

    @property
    def fraction(self) -> float:
        if self.total_steps <= 0:
            return 0.0
        return min(1.0, max(0.0, self.step / self.total_steps))


class AutoCalController:
    """Guide an internal tone through a selected J0 null and capture it."""

    _SEARCH_CAPTURE_DB = 100.0
    _DIP_ENTER_DB = 6.0
    _DIP_EXIT_DB = 3.0

    def __init__(
        self,
        config: AutoCalConfig,
        io: AutoCalIO,
        *,
        detector: CarrierMonitor | None = None,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self.config = config
        self._io = io
        self._clock = clock
        self.detector: CarrierMonitor = detector or BesselNullDetector(
            tone_frequency_hz=config.tone_frequency_hz,
            null_index=config.null_index,
            sample_rate_hz=48_000.0,
        )
        self.detector.set_min_null_depth_db(self._SEARCH_CAPTURE_DB)
        self._state = AutoCalState.IDLE
        self._message = "Idle"
        self._started_at: float | None = None
        self._step_deadline = 0.0
        self._recovery_deadline = 0.0
        self._level: float | None = None
        self._coarse_levels: tuple[float, ...] = ()
        self._fine_levels: tuple[float, ...] = ()
        self._coarse_index = 0
        self._fine_index = 0
        self._step = 0
        self._ptt_keyed = False
        self._tone_active = False
        self._in_dip = False
        self._dip_min_depth = 0.0
        self._dip_level = 0.0
        self._candidates: list[tuple[float, float]] = []
        self._best_depth: float | None = None
        self._best_level: float | None = None
        self._selected_depth: float | None = None
        self._selected_level: float | None = None
        self._recovery_next = ""
        self._captured: DeviationReading | None = None
        self._last_bessel: BesselProgress | None = None
        self._total_steps = config.coarse_steps + config.fine_steps + 2

    @property
    def state(self) -> AutoCalState:
        return self._state

    @property
    def tone_frequency_hz(self) -> float:
        return self.config.tone_frequency_hz

    @property
    def null_index(self) -> int:
        return self.config.null_index

    def progress(self) -> AutoCalProgress:
        elapsed = (
            max(0.0, self._clock() - self._started_at)
            if self._started_at is not None
            else 0.0
        )
        return AutoCalProgress(
            state=self._state,
            message=self._message,
            level=self._level,
            best_depth_db=self._best_depth,
            best_level=self._best_level,
            captured=self._captured,
            bessel=self._last_bessel,
            elapsed_s=elapsed,
            step=self._step,
            total_steps=self._total_steps,
        )

    def start(self) -> AutoCalProgress:
        """Key PTT and wait for an unmodulated carrier."""

        if self._state not in (
            AutoCalState.IDLE,
            AutoCalState.COMPLETE,
            AutoCalState.FAILED,
            AutoCalState.CANCELLED,
        ):
            raise AutoCalError("auto-calibration is already running")
        self._reset()
        self._started_at = self._clock()
        self._set_state(
            AutoCalState.WAITING_FOR_CARRIER,
            "Keying transmitter; waiting for unmodulated carrier",
        )
        try:
            keyed = self._io.key_ptt()
        except Exception as exc:
            return self._fail(f"PTT key failed: {exc}")
        if not keyed:
            return self._fail("PTT readback did not confirm transmission")
        self._ptt_keyed = True
        return self.progress()

    def update(self, spectrum_db: Sequence[float]) -> AutoCalProgress:
        """Consume one spectrum update and advance the search."""

        if self._state in (AutoCalState.IDLE,) or self._state.terminal:
            return self.progress()
        now = self._clock()
        assert self._started_at is not None
        if now - self._started_at > self.config.max_duration_s:
            return self._fail("auto-calibration exceeded its maximum duration")
        try:
            bessel = self.detector.update(spectrum_db)
        except Exception as exc:
            return self._fail(f"carrier detector failed: {exc}")
        self._last_bessel = bessel

        if self._state is AutoCalState.WAITING_FOR_CARRIER:
            if now - self._started_at > self.config.carrier_timeout_s:
                return self._fail("no carrier detected before timeout")
            if bessel.carrier_locked:
                self._begin_coarse(now)
            return self.progress()

        if self._state is AutoCalState.COARSE_SWEEP:
            self._track_dip(bessel)
            if now >= self._step_deadline:
                self._advance_coarse(now)
            return self.progress()

        if self._state is AutoCalState.REFINING:
            self._track_fine(bessel)
            if now >= self._step_deadline:
                self._advance_fine(now)
            return self.progress()

        if self._state is AutoCalState.RECOVERING:
            if now >= self._recovery_deadline:
                if self._recovery_next == "fine":
                    self._begin_fine(now)
                else:
                    self._begin_hold(now)
            return self.progress()

        if self._state is AutoCalState.HOLDING:
            self._track_fine(bessel)
            if now >= self._step_deadline:
                self._capture(now)
            return self.progress()

        return self.progress()

    def cancel(self, reason: str = "cancelled by operator") -> AutoCalProgress:
        """Stop the tone and transmitter if a run is active."""

        if self._state.terminal:
            return self.progress()
        if self._state is AutoCalState.IDLE:
            return self.progress()
        cleanup_error = self._cleanup()
        self._set_state(AutoCalState.CANCELLED, f"Auto-calibration {reason}")
        if cleanup_error:
            self._message = f"{self._message}; {cleanup_error}"
        return self.progress()

    def _reset(self) -> None:
        self._message = ""
        self._step_deadline = 0.0
        self._recovery_deadline = 0.0
        self._level = None
        self._coarse_levels = ()
        self._fine_levels = ()
        self._coarse_index = 0
        self._fine_index = 0
        self._step = 0
        self._ptt_keyed = False
        self._tone_active = False
        self._in_dip = False
        self._dip_min_depth = 0.0
        self._dip_level = 0.0
        self._candidates = []
        self._best_depth = None
        self._best_level = None
        self._selected_depth = None
        self._selected_level = None
        self._recovery_next = ""
        self._captured = None
        self._last_bessel = None
        self.detector.set_min_null_depth_db(self._SEARCH_CAPTURE_DB)

    def _set_state(self, state: AutoCalState, message: str) -> None:
        self._state = state
        self._message = message

    def _begin_coarse(self, now: float) -> None:
        span = self.config.max_level - self.config.min_level
        self._coarse_levels = tuple(
            self.config.min_level + span * index / (self.config.coarse_steps - 1)
            for index in range(self.config.coarse_steps)
        )
        self._coarse_index = 0
        self._step = 0
        self._step_deadline = now + self.config.settle_s
        self._set_state(
            AutoCalState.COARSE_SWEEP,
            f"Carrier locked; coarse level sweep 1/{self.config.coarse_steps}",
        )
        if not self._start_tone(self._coarse_levels[0]):
            return

    def _advance_coarse(self, now: float) -> None:
        self._coarse_index += 1
        self._step = self._coarse_index
        if self._coarse_index >= len(self._coarse_levels):
            self._finish_dip()
            if len(self._candidates) < self.config.null_index:
                self._fail(
                    f"only {len(self._candidates)} carrier null(s) found; "
                    f"requested null {self.config.null_index}"
                )
                return
            depth, level = self._candidates[self.config.null_index - 1]
            self._selected_depth = depth
            self._selected_level = level
            self._best_depth = depth
            self._best_level = level
            self._stop_tone()
            self._recovery_next = "fine"
            self._recovery_deadline = now + self.config.recovery_s
            self._set_state(
                AutoCalState.RECOVERING,
                f"Null {self.config.null_index} found at {level * 100.0:.1f}%; "
                "recovering carrier before refinement",
            )
            return
        level = self._coarse_levels[self._coarse_index]
        if not self._set_level(level):
            return
        self._step_deadline = now + self.config.settle_s
        self._message = (
            f"Coarse level sweep {self._coarse_index + 1}/"
            f"{self.config.coarse_steps}"
        )

    def _begin_fine(self, now: float) -> None:
        assert self._selected_level is not None
        assert self._selected_depth is not None
        coarse_span = (
            self.config.max_level - self.config.min_level
        ) / (self.config.coarse_steps - 1)
        span = coarse_span * 1.25
        lower = max(self.config.min_level, self._selected_level - span)
        upper = min(self.config.max_level, self._selected_level + span)
        if upper - lower < 1e-6:
            lower = max(self.config.min_level, upper - coarse_span)
        self._fine_levels = tuple(
            lower + (upper - lower) * index / (self.config.fine_steps - 1)
            for index in range(self.config.fine_steps)
        )
        self._fine_index = 0
        self._step = self.config.coarse_steps
        self._best_depth = self._selected_depth
        self._best_level = self._selected_level
        if not self._start_tone(self._fine_levels[0]):
            return
        self._step_deadline = now + self.config.settle_s
        self._set_state(
            AutoCalState.REFINING,
            f"Refining around {self._selected_level * 100.0:.1f}%",
        )

    def _advance_fine(self, now: float) -> None:
        self._fine_index += 1
        self._step = self.config.coarse_steps + self._fine_index
        if self._fine_index >= len(self._fine_levels):
            assert self._best_level is not None
            self._stop_tone()
            self._recovery_next = "hold"
            self._recovery_deadline = now + self.config.recovery_s
            self._set_state(
                AutoCalState.RECOVERING,
                f"Holding refined level {self._best_level * 100.0:.1f}%; "
                "recovering carrier",
            )
            return
        level = self._fine_levels[self._fine_index]
        if not self._set_level(level):
            return
        self._step_deadline = now + self.config.settle_s
        self._message = (
            f"Refining {self._fine_index + 1}/{self.config.fine_steps}"
        )

    def _begin_hold(self, now: float) -> None:
        assert self._best_level is not None
        if not self._start_tone(self._best_level):
            return
        self._step_deadline = now + max(self.config.hold_s, self.config.settle_s)
        self._set_state(
            AutoCalState.HOLDING,
            f"Holding {self._best_level * 100.0:.1f}% for final capture",
        )

    def _track_dip(self, progress: BesselProgress) -> None:
        depth = progress.null_depth_db
        if depth is None or self._level is None:
            return
        if not self._in_dip:
            if depth >= self._DIP_ENTER_DB:
                self._in_dip = True
                self._dip_min_depth = depth
                self._dip_level = self._level
            return
        if depth > self._dip_min_depth:
            self._dip_min_depth = depth
            self._dip_level = self._level
        if depth <= self._DIP_EXIT_DB:
            self._finish_dip()

    def _finish_dip(self) -> None:
        if not self._in_dip:
            return
        self._candidates.append((self._dip_min_depth, self._dip_level))
        self._in_dip = False
        self._dip_min_depth = 0.0

    def _track_fine(self, progress: BesselProgress) -> None:
        depth = progress.null_depth_db
        if depth is None or self._level is None:
            return
        if self._best_depth is None or depth > self._best_depth:
            self._best_depth = depth
            self._best_level = self._level

    def _capture(self, now: float) -> None:
        del now
        try:
            self.detector.set_min_null_depth_db(self.config.minimum_depth_db)
            captured = self.detector.capture_manual()
        except Exception as exc:
            self._fail(f"final null capture failed: {exc}")
            return
        self._last_bessel = captured
        if captured.captured is None:
            depth = captured.null_depth_db
            depth_text = f"{depth:.1f} dB" if depth is not None else "no depth"
            self._fail(f"held null was not deep enough ({depth_text})")
            return
        self._captured = captured.captured
        cleanup_error = self._cleanup()
        depth = float(self._captured.metadata.get("null_depth_db", 0.0))
        message = (
            f"Captured J0 null {self.config.null_index} at "
            f"{(self._best_level or 0.0) * 100.0:.1f}% level, "
            f"{depth:.1f} dB depth"
        )
        span = self.config.max_level - self.config.min_level
        if (
            self._best_level is not None
            and min(
                self._best_level - self.config.min_level,
                self.config.max_level - self._best_level,
            )
            <= span * 0.05
        ):
            message += "; null is at a level-range limit, adjust system audio for headroom"
        if cleanup_error:
            self._set_state(AutoCalState.FAILED, f"{message}; {cleanup_error}")
        else:
            self._set_state(AutoCalState.COMPLETE, message)

    def _start_tone(self, level: float) -> bool:
        self._level = level
        try:
            self._io.start_tone(level)
            self._tone_active = True
            return True
        except Exception as exc:
            self._fail(f"tone output failed: {exc}")
            return False

    def _stop_tone(self) -> None:
        if not self._tone_active:
            return
        self._tone_active = False
        try:
            self._io.stop_tone()
        except Exception:
            pass

    def _set_level(self, level: float) -> bool:
        self._level = level
        try:
            self._io.set_level(level)
            return True
        except Exception as exc:
            self._fail(f"audio level control failed: {exc}")
            return False

    def _cleanup(self) -> str:
        errors: list[str] = []
        self._stop_tone()
        if self._ptt_keyed:
            self._ptt_keyed = False
            try:
                if not self._io.unkey_ptt():
                    errors.append("PTT unkey readback failed")
            except Exception as exc:
                errors.append(f"PTT unkey failed: {exc}")
        return "; ".join(errors)

    def _fail(self, reason: str) -> AutoCalProgress:
        cleanup_error = self._cleanup()
        message = f"Auto-calibration failed: {reason}"
        if cleanup_error:
            message = f"{message}; {cleanup_error}"
        self._set_state(AutoCalState.FAILED, message)
        return self.progress()
