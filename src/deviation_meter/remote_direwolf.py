"""Remote Direwolf calibration process controlled over SSH."""

from __future__ import annotations

import re
import shlex
import time
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol

from .direwolf import (
    DirewolfError,
    build_calibration_config,
    parse_direwolf_config,
    profile_tone_hz,
)
from .pipewire import (
    StreamNode,
    read_pw_dump,
    set_stream_mute,
    set_stream_volume,
    wait_for_output_stream,
)
from .rigctl import RigctldClient, RigctldError
from .ssh import SshError, SshTransport


@dataclass(frozen=True, slots=True)
class RemoteDirewolfConfig:
    """SSH destination, local config template, and remote PTT endpoint."""

    ssh_host: str
    base_config: Path
    remote_rig_host: str
    remote_rig_port: int = 4532
    tone: str = "mark"
    executable: str = "direwolf"
    max_duration_s: float = 30.0

    def __post_init__(self) -> None:
        if not self.ssh_host or any(char.isspace() for char in self.ssh_host):
            raise DirewolfError("remote SSH host must be non-empty without whitespace")
        if self.ssh_host.startswith("-"):
            raise DirewolfError("remote SSH host must not start with '-'")
        if self.tone not in ("mark", "space"):
            raise DirewolfError("tone must be 'mark' or 'space'")
        if not self.remote_rig_host or any(
            char.isspace() for char in self.remote_rig_host
        ):
            raise DirewolfError("remote rig host must be non-empty without whitespace")
        if not 1 <= self.remote_rig_port <= 65535:
            raise DirewolfError("remote rig port must be between 1 and 65535")
        if not self.executable:
            raise DirewolfError("remote Direwolf executable cannot be empty")
        if self.max_duration_s <= 0:
            raise DirewolfError("remote maximum duration must be positive")


class RemoteProcessTransport(Protocol):
    def run(self, argv, **kwargs): ...

    def run_shell(self, command: str, **kwargs): ...

    def read_text(self, path: str) -> str: ...

    def write_text(self, path: str, text: str) -> None: ...

    def remove(self, path: str) -> None: ...

    def pipewire_runner(self, argv, **kwargs): ...

    def close(self) -> None: ...


class RemoteDirewolfCalibration:
    """Own a remote Direwolf -x process and its remote PipeWire stream."""

    def __init__(
        self,
        config: RemoteDirewolfConfig,
        *,
        transport: RemoteProcessTransport | None = None,
        stream_waiter: Callable[..., StreamNode] = wait_for_output_stream,
        volume_setter: Callable[..., None] = set_stream_volume,
        mute_setter: Callable[..., None] = set_stream_mute,
        ptt_client_factory: Callable[..., RigctldClient] = RigctldClient,
        sleeper: Callable[[float], None] = time.sleep,
    ) -> None:
        self.config = config
        self.transport = transport or SshTransport(config.ssh_host)
        self._stream_waiter = stream_waiter
        self._volume_setter = volume_setter
        self._mute_setter = mute_setter
        self._ptt_client_factory = ptt_client_factory
        self._sleeper = sleeper
        self._stream: StreamNode | None = None
        self._remote_dir: str | None = None
        self._script_pid: int | None = None
        self._log_path: str | None = None
        self.version = ""

    @property
    def stream(self) -> StreamNode | None:
        return self._stream

    def _ptt_client(self) -> RigctldClient:
        return self._ptt_client_factory(
            self.config.remote_rig_host,
            self.config.remote_rig_port,
        )

    def _verify_ptt_on(self) -> bool:
        client = self._ptt_client()
        for _ in range(15):
            try:
                if client.get_ptt():
                    return True
            except RigctldError:
                pass
            self._sleeper(0.2)
        return False

    def _force_ptt_off(self) -> bool:
        client = self._ptt_client()
        for _ in range(10):
            try:
                client.set_ptt(False)
                if not client.get_ptt():
                    return True
            except RigctldError:
                pass
            self._sleeper(0.2)
        return False

    def _check_remote_tools(self) -> None:
        command = (
            f"command -v {shlex.quote(self.config.executable)} >/dev/null && "
            "command -v timeout >/dev/null && "
            "command -v pw-dump >/dev/null && "
            "command -v wpctl >/dev/null"
        )
        result = self.transport.run_shell(command, timeout=10.0, check=False)
        if result.returncode != 0:
            detail = (result.stderr or result.stdout or "").strip()
            raise DirewolfError(
                "remote host is missing direwolf, timeout, pw-dump, or wpctl"
                + (f": {detail}" if detail else "")
            )

    def _probe_remote_direwolf(self) -> str:
        result = self.transport.run(
            [self.config.executable, "-h"],
            timeout=10.0,
            check=False,
        )
        output = (result.stdout or "") + (result.stderr or "")
        if "Dire Wolf" not in output:
            raise DirewolfError(
                "remote Direwolf did not identify itself; check the executable"
            )
        if not re.search(r"(?m)^\s*-x\b", output):
            raise DirewolfError(
                "remote Direwolf does not advertise -x calibration support"
            )
        match = re.search(r"Release\s+([0-9][0-9A-Za-z.\-]*)", output)
        if match is None:
            raise DirewolfError("cannot determine remote Direwolf version")
        return match.group(1)

    def _remote_direwolf_pids(self) -> list[int]:
        result = self.transport.run_shell(
            "command -v pgrep >/dev/null && pgrep -x direwolf || true",
            timeout=5.0,
            check=False,
        )
        pids: list[int] = []
        for line in result.stdout.splitlines():
            try:
                pids.append(int(line.strip()))
            except ValueError:
                continue
        return pids

    def start(self, level: float = 0.0) -> StreamNode:
        if not 0.0 <= level <= 1.0:
            raise DirewolfError("remote stream level must be between 0 and 1")
        if self._script_pid is not None:
            raise DirewolfError("a remote Direwolf calibration is already running")
        profile = parse_direwolf_config(self.config.base_config)
        tone_hz = profile_tone_hz(profile, self.config.tone)
        if tone_hz <= 0:
            raise DirewolfError("selected remote Direwolf tone is not positive")
        self._check_remote_tools()
        version = self._probe_remote_direwolf()
        pids = self._remote_direwolf_pids()
        if pids:
            raise DirewolfError(
                "stop the existing remote Direwolf instance before calibration "
                f"(PID {', '.join(str(pid) for pid in pids)})"
            )
        result = self.transport.run(
            ["mktemp", "-d", "/tmp/deviationstation-direwolf.XXXXXX"],
            timeout=10.0,
        )
        remote_dir = result.stdout.strip()
        if not remote_dir.startswith("/tmp/"):
            raise DirewolfError("remote mktemp did not return a /tmp directory")
        self._remote_dir = remote_dir
        config_path = f"{remote_dir}/calibration.conf"
        script_path = f"{remote_dir}/run.sh"
        self._log_path = f"{remote_dir}/direwolf.log"
        pid_path = f"{remote_dir}/direwolf.pid"
        config_text = build_calibration_config(
            profile,
            rig_host=self.config.remote_rig_host,
            rig_port=self.config.remote_rig_port,
        )
        try:
            self.transport.write_text(config_path, config_text)
            tone_option = "m" if self.config.tone == "mark" else "s"
            script = "\n".join(
                (
                    "#!/bin/sh",
                    "set -eu",
                    'export XDG_RUNTIME_DIR="${XDG_RUNTIME_DIR:-/run/user/$(id -u)}"',
                    'export DBUS_SESSION_BUS_ADDRESS="${DBUS_SESSION_BUS_ADDRESS:-unix:path=$XDG_RUNTIME_DIR/bus}"',
                    f"cfg={shlex.quote(config_path)}",
                    f"log={shlex.quote(self._log_path)}",
                    f"pidfile={shlex.quote(pid_path)}",
                    (
                        f"timeout --signal=INT {self.config.max_duration_s:g}s "
                        f"{shlex.quote(self.config.executable)} "
                        f"-c \"$cfg\" -x {tone_option} -t 0 -q hd "
                        '>"$log" 2>&1 &'
                    ),
                    "child=$!",
                    'echo "$child" > "$pidfile"',
                    'trap \'kill -INT "$child" 2>/dev/null || true\' INT TERM',
                    'wait "$child"',
                    "",
                )
            )
            self.transport.write_text(script_path, script)
            start_result = self.transport.run_shell(
                f"nohup sh {shlex.quote(script_path)} </dev/null "
                ">/dev/null 2>&1 & echo $!",
                timeout=10.0,
            )
        except Exception:
            self._discard_remote_dir()
            raise
        try:
            self._script_pid = int(start_result.stdout.strip().splitlines()[-1])
        except (ValueError, IndexError) as exc:
            self._discard_remote_dir()
            raise DirewolfError("remote Direwolf did not return a process ID") from exc
        try:
            self._stream = self._stream_waiter(
                0,
                binary="direwolf",
                timeout_s=3.0,
                read_streams=lambda: read_pw_dump(
                    runner=self.transport.pipewire_runner
                ),
            )
            self._mute_setter(
                self._stream.node_id,
                False,
                runner=self.transport.pipewire_runner,
            )
            self._volume_setter(
                self._stream.node_id,
                level,
                runner=self.transport.pipewire_runner,
            )
            if not self._verify_ptt_on():
                raise DirewolfError("remote Direwolf did not confirm PTT on")
        except Exception:
            self.stop()
            raise
        self.version = version
        return self._stream

    def set_level(self, level: float) -> None:
        if self._stream is None:
            raise DirewolfError("remote Direwolf stream is not available")
        self._volume_setter(
            self._stream.node_id,
            level,
            runner=self.transport.pipewire_runner,
        )

    def mute(self) -> None:
        self.set_level(0.0)

    def _discard_remote_dir(self) -> None:
        remote_dir = self._remote_dir
        self._remote_dir = None
        self._log_path = None
        if remote_dir:
            try:
                self.transport.remove(remote_dir)
            except (SshError, OSError):
                pass
        try:
            self.transport.close()
        except Exception:
            pass

    def stop(self) -> bool:
        script_pid = self._script_pid
        self._script_pid = None
        self._stream = None
        if script_pid is None and self._remote_dir is None:
            return True
        cleanup_errors: list[str] = []
        if script_pid is not None:
            try:
                self.transport.run(
                    ["kill", "-INT", str(script_pid)],
                    timeout=5.0,
                    check=False,
                )
                self.transport.run_shell(
                    f"for i in $(seq 1 40); do "
                    f"kill -0 {script_pid} 2>/dev/null || exit 0; sleep .1; done; "
                    f"kill -TERM {script_pid} 2>/dev/null || true",
                    timeout=8.0,
                    check=False,
                )
            except (SshError, OSError) as exc:
                cleanup_errors.append(f"remote stop failed: {exc}")
        if not self._force_ptt_off():
            cleanup_errors.append("remote PTT unkey could not be confirmed")
        remote_dir = self._remote_dir
        self._remote_dir = None
        if remote_dir:
            try:
                self.transport.remove(remote_dir)
            except (SshError, OSError) as exc:
                cleanup_errors.append(f"remote cleanup failed: {exc}")
        try:
            self.transport.close()
        except Exception:
            pass
        if cleanup_errors:
            raise DirewolfError("; ".join(cleanup_errors))
        return True

    def log_tail(self, lines: int = 12) -> str:
        if self._log_path is None:
            return ""
        try:
            content = self.transport.read_text(self._log_path)
        except (SshError, OSError):
            return ""
        return "\n".join(content.splitlines()[-lines:])
