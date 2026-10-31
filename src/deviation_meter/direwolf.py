"""Bounded Direwolf calibration-tone process for Bessel measurements."""

from __future__ import annotations

import math
import os
import re
import shutil
import signal
import subprocess
import tempfile
import time
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

from .pipewire import (
    StreamNode,
    set_stream_mute,
    set_stream_volume,
    wait_for_output_stream,
)
from .rigctl import RigctldClient, RigctldError


class DirewolfError(RuntimeError):
    """Raised when a Direwolf calibration process cannot be prepared or run."""


@dataclass(frozen=True, slots=True)
class DirewolfProfile:
    audio_device: str
    sample_rate_hz: int
    channels: int
    baud: int
    mark_hz: float
    space_hz: float

    @property
    def modem_line(self) -> str:
        return f"{self.baud} {self.mark_hz:g}:{self.space_hz:g}"


@dataclass(frozen=True, slots=True)
class DirewolfConfig:
    base_config: Path
    tone: str = "mark"
    rig_host: str = "127.0.0.1"
    rig_port: int = 4532
    executable: str = "direwolf"

    def __post_init__(self) -> None:
        if self.tone not in ("mark", "space"):
            raise DirewolfError("tone must be 'mark' or 'space'")
        if not self.rig_host or any(char.isspace() for char in self.rig_host):
            raise DirewolfError("rig host must be non-empty and contain no spaces")
        if not 1 <= self.rig_port <= 65535:
            raise DirewolfError("rig port must be between 1 and 65535")
        if not self.executable:
            raise DirewolfError("Direwolf executable cannot be empty")


def _numeric_prefix(value: str, name: str) -> int:
    try:
        result = int(value)
    except ValueError as exc:
        raise DirewolfError(f"invalid {name}: {value!r}") from exc
    if result <= 0:
        raise DirewolfError(f"{name} must be positive")
    return result


def parse_direwolf_config(path: Path) -> DirewolfProfile:
    """Read the audio and AFSK settings needed for a calibration run."""

    candidate = Path(path)
    if not candidate.is_file():
        raise DirewolfError(f"Direwolf config not found: {candidate}")
    try:
        lines = candidate.read_text(encoding="utf-8", errors="replace").splitlines()
    except OSError as exc:
        raise DirewolfError(f"cannot read Direwolf config: {exc}") from exc

    audio_device: str | None = None
    sample_rate = 48_000
    channels = 1
    baud: int | None = None
    mark_hz = 0.0
    space_hz = 0.0
    for raw in lines:
        line = raw.split("#", 1)[0].strip()
        if not line:
            continue
        parts = line.split()
        command = parts[0].upper()
        values = parts[1:]
        if command == "ADEVICE":
            if not values:
                raise DirewolfError("ADEVICE has no device name")
            audio_device = " ".join(values)
        elif command == "ARATE":
            if values:
                sample_rate = _numeric_prefix(values[0], "ARATE")
        elif command == "ACHANNELS":
            if values:
                channels = _numeric_prefix(values[0], "ACHANNELS")
        elif command == "MODEM" and baud is None:
            if not values:
                raise DirewolfError("MODEM has no speed")
            first = values[0].upper()
            if first in ("AIS", "EAS"):
                raise DirewolfError(
                    "Direwolf -x calibration requires an AFSK modem, not AIS/EAS"
                )
            baud = _numeric_prefix(values[0], "MODEM speed")
            for value in values[1:]:
                if ":" not in value:
                    continue
                mark_text, space_text = value.split(":", 1)
                try:
                    mark_hz = float(mark_text)
                    space_hz = float(space_text)
                except ValueError as exc:
                    raise DirewolfError(
                        f"invalid MODEM tone pair: {value!r}"
                    ) from exc
                break

    if audio_device is None:
        raise DirewolfError(
            "Direwolf config needs an explicit ADEVICE; calibration will not "
            "guess the transmit sound device"
        )
    if baud is None:
        raise DirewolfError("Direwolf config has no MODEM setting")
    if channels != 1:
        raise DirewolfError(
            "Direwolf calibration currently requires ACHANNELS 1"
        )
    if mark_hz == 0.0 and space_hz == 0.0:
        if baud == 1200:
            mark_hz, space_hz = 1200.0, 2200.0
        elif baud == 300:
            mark_hz, space_hz = 1600.0, 1800.0
    if mark_hz <= 0.0 or space_hz <= 0.0:
        raise DirewolfError(
            f"Direwolf modem {baud} has no AFSK mark/space tones; "
            "-x calibration is unavailable for this modem"
        )
    return DirewolfProfile(
        audio_device=audio_device,
        sample_rate_hz=sample_rate,
        channels=channels,
        baud=baud,
        mark_hz=mark_hz,
        space_hz=space_hz,
    )


def profile_tone_hz(profile: DirewolfProfile, tone: str) -> float:
    if tone == "mark":
        return profile.mark_hz
    if tone == "space":
        return profile.space_hz
    raise DirewolfError("tone must be 'mark' or 'space'")


def build_calibration_config(
    profile: DirewolfProfile,
    *,
    rig_host: str,
    rig_port: int,
) -> str:
    """Generate a minimal temporary config with no network servers."""

    if not 1 <= rig_port <= 65535:
        raise DirewolfError("rig port must be between 1 and 65535")
    if not rig_host or any(char.isspace() for char in rig_host):
        raise DirewolfError("rig host must be non-empty and contain no spaces")
    return "\n".join(
        (
            "# Generated by gnuradio-deviation-meter; do not edit.",
            f"ADEVICE {profile.audio_device}",
            f"ARATE {profile.sample_rate_hz}",
            "ACHANNELS 1",
            "CHANNEL 0",
            "MYCALL N0CALL",
            f"MODEM {profile.modem_line}",
            "TXDELAY 100",
            "DWAIT 0",
            f"PTT RIG 2 {rig_host}:{rig_port}",
            "AGWPORT 0",
            "KISSPORT 0",
            "",
        )
    )


def probe_direwolf(
    executable: str = "direwolf",
    *,
    runner: Callable[..., subprocess.CompletedProcess] = subprocess.run,
) -> str:
    """Return the version string after confirming -x support."""

    try:
        result = runner(
            [executable, "-h"],
            capture_output=True,
            text=True,
            timeout=5.0,
            check=False,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        raise DirewolfError(f"cannot run {executable}: {exc}") from exc
    output = (result.stdout or "") + (result.stderr or "")
    if "Dire Wolf" not in output:
        raise DirewolfError(f"{executable} did not identify itself as Direwolf")
    if not re.search(r"(?m)^\s*-x\b", output):
        raise DirewolfError("this Direwolf build does not advertise -x support")
    match = re.search(r"Release\s+([0-9][0-9A-Za-z.\-]*)", output)
    if match is None:
        raise DirewolfError("cannot determine Direwolf version")
    return match.group(1)


def _running_direwolf_pids() -> list[int]:
    try:
        result = subprocess.run(
            ["pgrep", "-x", "direwolf"],
            capture_output=True,
            text=True,
            timeout=3.0,
            check=False,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        raise DirewolfError(f"cannot check for running Direwolf instances: {exc}") from exc
    if result.returncode not in (0, 1):
        raise DirewolfError("pgrep failed while checking for Direwolf instances")
    pids: list[int] = []
    for line in result.stdout.splitlines():
        try:
            pids.append(int(line.strip()))
        except ValueError:
            continue
    return pids


class DirewolfCalibration:
    """Own one generated Direwolf tone process and its PipeWire stream."""

    def __init__(
        self,
        config: DirewolfConfig,
        *,
        runner: Callable[..., subprocess.CompletedProcess] = subprocess.run,
        process_factory: Callable[..., subprocess.Popen] = subprocess.Popen,
        process_checker: Callable[[], list[int]] = _running_direwolf_pids,
        stream_waiter: Callable[..., StreamNode] = wait_for_output_stream,
        volume_setter: Callable[[int, float], None] = set_stream_volume,
        mute_setter: Callable[[int, bool], None] = set_stream_mute,
        rig_unkey: Callable[[], bool] | None = None,
        ptt_verifier: Callable[[], bool] | None = None,
        sleeper: Callable[[float], None] = time.sleep,
    ) -> None:
        self.config = config
        self._runner = runner
        self._process_factory = process_factory
        self._process_checker = process_checker
        self._stream_waiter = stream_waiter
        self._volume_setter = volume_setter
        self._mute_setter = mute_setter
        self._sleeper = sleeper
        self._rig_unkey = rig_unkey or self._default_rig_unkey
        self._ptt_verifier = ptt_verifier or self._default_ptt_verifier
        self._process: subprocess.Popen | None = None
        self._stream: StreamNode | None = None
        self._temp_dir: Path | None = None
        self._log_handle = None
        self._version: str | None = None

    def _default_ptt_verifier(self) -> bool:
        client = RigctldClient(self.config.rig_host, self.config.rig_port)
        last_error: str | None = None
        for _ in range(10):
            try:
                if client.get_ptt():
                    return True
                last_error = "rigctld reports PTT off"
            except RigctldError as exc:
                last_error = str(exc)
            self._sleeper(0.2)
        raise DirewolfError(
            f"Direwolf did not key the transmitter: {last_error or 'PTT off'}"
        )

    def _default_rig_unkey(self) -> bool:
        client = RigctldClient(self.config.rig_host, self.config.rig_port)
        last_error: str | None = None
        for _ in range(5):
            try:
                client.set_ptt(False)
                if not client.get_ptt():
                    return True
                last_error = "PTT readback still reports keyed"
            except RigctldError as exc:
                last_error = str(exc)
            self._sleeper(0.2)
        if last_error:
            raise DirewolfError(f"could not confirm unkey: {last_error}")
        return False

    @property
    def version(self) -> str | None:
        return self._version

    @property
    def stream(self) -> StreamNode | None:
        return self._stream

    @property
    def running(self) -> bool:
        return self._process is not None and self._process.poll() is None

    def start(self, level: float = 0.0) -> StreamNode:
        if self.running:
            raise DirewolfError("a Direwolf calibration process is already running")
        if not math.isfinite(level) or not 0.0 <= level <= 1.0:
            raise DirewolfError("initial stream level must be between 0 and 1")
        pids = self._process_checker()
        if pids:
            pid_text = ", ".join(str(pid) for pid in pids)
            raise DirewolfError(
                "stop the existing Direwolf instance before calibration "
                f"(PID {pid_text}); the calibration process needs exclusive audio"
            )
        profile = parse_direwolf_config(self.config.base_config)
        tone_hz = profile_tone_hz(profile, self.config.tone)
        if tone_hz <= 0.0:
            raise DirewolfError("selected Direwolf tone is not positive")
        self._version = probe_direwolf(self.config.executable, runner=self._runner)

        self._temp_dir = Path(
            tempfile.mkdtemp(prefix="deviation-meter-direwolf-")
        )
        config_path = self._temp_dir / "calibration.conf"
        config_path.write_text(
            build_calibration_config(
                profile,
                rig_host=self.config.rig_host,
                rig_port=self.config.rig_port,
            ),
            encoding="utf-8",
        )
        log_path = self._temp_dir / "direwolf.log"
        self._log_handle = log_path.open("w", encoding="utf-8")
        command = [
            self.config.executable,
            "-c",
            str(config_path),
            "-x",
            "m" if self.config.tone == "mark" else "s",
            "-t",
            "0",
            "-q",
            "hd",
        ]
        try:
            self._process = self._process_factory(
                command,
                stdout=self._log_handle,
                stderr=subprocess.STDOUT,
                start_new_session=True,
            )
        except OSError as exc:
            self._cleanup_files()
            raise DirewolfError(f"cannot start Direwolf: {exc}") from exc

        try:
            self._stream = self._stream_waiter(
                self._process.pid,
                binary="direwolf",
                timeout_s=3.0,
            )
            self._mute_setter(self._stream.node_id, False)
            self._volume_setter(self._stream.node_id, level)
            if not self._ptt_verifier():
                raise DirewolfError("Direwolf did not confirm PTT on")
        except Exception:
            self.stop()
            raise
        return self._stream

    def set_level(self, level: float) -> None:
        if self._stream is None:
            raise DirewolfError("Direwolf output stream is not available")
        self._volume_setter(self._stream.node_id, level)

    def stop(self) -> None:
        process = self._process
        self._process = None
        self._stream = None
        if process is not None:
            try:
                if process.poll() is None:
                    process.send_signal(signal.SIGINT)
                    try:
                        process.wait(timeout=3.0)
                    except subprocess.TimeoutExpired:
                        process.terminate()
                        try:
                            process.wait(timeout=2.0)
                        except subprocess.TimeoutExpired:
                            process.kill()
                            process.wait(timeout=2.0)
            except (OSError, subprocess.SubprocessError):
                pass
        try:
            self._rig_unkey()
        finally:
            self._cleanup_files()

    def log_tail(self, lines: int = 12) -> str:
        if self._temp_dir is None:
            return ""
        log_path = self._temp_dir / "direwolf.log"
        try:
            content = log_path.read_text(encoding="utf-8", errors="replace")
        except OSError:
            return ""
        return "\n".join(content.splitlines()[-lines:])

    def _cleanup_files(self) -> None:
        if self._log_handle is not None:
            try:
                self._log_handle.close()
            except OSError:
                pass
            self._log_handle = None
        if self._temp_dir is not None:
            shutil.rmtree(self._temp_dir, ignore_errors=True)
            self._temp_dir = None
