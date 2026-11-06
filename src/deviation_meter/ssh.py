"""Small SSH transport for remote calibration helpers."""

from __future__ import annotations

import hashlib
import os
import shlex
import subprocess
import tempfile
from collections.abc import Callable, Sequence


class SshError(RuntimeError):
    """Raised when an SSH command cannot be executed safely."""


class SshTransport:
    """Run tightly quoted commands on a preconfigured SSH destination."""

    def __init__(
        self,
        host: str,
        *,
        executable: str = "ssh",
        connect_timeout_s: float = 5.0,
        control_persist_s: float = 60.0,
        runner: Callable[..., subprocess.CompletedProcess] = subprocess.run,
    ) -> None:
        if not host or any(character.isspace() for character in host):
            raise SshError("SSH host must be non-empty and contain no whitespace")
        if host.startswith("-"):
            raise SshError("SSH host must not start with '-'")
        if not executable:
            raise SshError("SSH executable cannot be empty")
        if connect_timeout_s <= 0 or control_persist_s <= 0:
            raise SshError("SSH timeouts must be positive")
        self.host = host
        self.executable = executable
        self.connect_timeout_s = float(connect_timeout_s)
        self.control_persist_s = float(control_persist_s)
        self._runner = runner
        digest = hashlib.sha1(f"{host}:{os.getpid()}".encode()).hexdigest()[:16]
        self.control_path = os.path.join(
            tempfile.gettempdir(),
            f"deviation-meter-ssh-{digest}",
        )

    def _ssh_command(self) -> list[str]:
        return [
            self.executable,
            "-o",
            "BatchMode=yes",
            "-o",
            f"ConnectTimeout={int(round(self.connect_timeout_s))}",
            "-o",
            "ServerAliveInterval=5",
            "-o",
            "ServerAliveCountMax=2",
            "-o",
            "ControlMaster=auto",
            "-o",
            f"ControlPath={self.control_path}",
            "-o",
            f"ControlPersist={int(round(self.control_persist_s))}",
            self.host,
        ]

    @staticmethod
    def _remote_prefix() -> str:
        return (
            'export XDG_RUNTIME_DIR="${XDG_RUNTIME_DIR:-/run/user/$(id -u)}"; '
            'export DBUS_SESSION_BUS_ADDRESS="${DBUS_SESSION_BUS_ADDRESS:-'
            'unix:path=$XDG_RUNTIME_DIR/bus}"; '
        )

    def run(
        self,
        argv: Sequence[str],
        *,
        input_text: str | None = None,
        timeout: float = 10.0,
        check: bool = True,
    ) -> subprocess.CompletedProcess:
        if not argv:
            raise SshError("remote command cannot be empty")
        if timeout <= 0:
            raise SshError("remote command timeout must be positive")
        remote = self._remote_prefix() + shlex.join(list(argv))
        try:
            result = self._runner(
                [*self._ssh_command(), remote],
                input=input_text,
                capture_output=True,
                text=True,
                timeout=timeout,
                check=False,
            )
        except (OSError, subprocess.SubprocessError) as exc:
            raise SshError(f"SSH command failed: {exc}") from exc
        if check and result.returncode != 0:
            detail = (result.stderr or result.stdout or "").strip()
            raise SshError(
                f"remote command failed ({result.returncode}): "
                f"{detail or shlex.join(list(argv))}"
            )
        return result

    def run_shell(
        self,
        command: str,
        *,
        timeout: float = 10.0,
        check: bool = True,
    ) -> subprocess.CompletedProcess:
        if not command:
            raise SshError("remote shell command cannot be empty")
        return self.run(
            ["sh", "-c", command],
            timeout=timeout,
            check=check,
        )

    def read_text(self, path: str) -> str:
        return self.run(["cat", "--", path], timeout=10.0).stdout

    def write_text(self, path: str, text: str) -> None:
        self.run(
            ["sh", "-c", 'umask 077; cat > "$1"', "sh", path],
            input_text=text,
            timeout=10.0,
        )

    def remove(self, path: str) -> None:
        self.run(["rm", "-rf", "--", path], timeout=10.0, check=False)

    def pipewire_runner(self, argv: Sequence[str], **kwargs) -> subprocess.CompletedProcess:
        """Adapter for :mod:`deviation_meter.pipewire` command helpers."""

        timeout = float(kwargs.get("timeout", 5.0) or 5.0)
        return self.run(
            list(argv),
            timeout=timeout,
            check=False,
        )

    def close(self) -> None:
        try:
            self._runner(
                [
                    self.executable,
                    "-o",
                    f"ControlPath={self.control_path}",
                    "-O",
                    "exit",
                    self.host,
                ],
                capture_output=True,
                text=True,
                timeout=5.0,
                check=False,
            )
        except (OSError, subprocess.SubprocessError):
            pass
