"""Small SSH transport for remote calibration helpers."""

from __future__ import annotations

import hashlib
import os
import shlex
import socket
import subprocess
import tempfile
import time
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


def _free_local_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


class SshTunnel:
    """Local SSH port-forward for a service bound to loopback remotely."""

    def __init__(
        self,
        host: str,
        remote_port: int,
        *,
        remote_host: str = "127.0.0.1",
        executable: str = "ssh",
        connect_timeout_s: float = 5.0,
        local_port: int | None = None,
        process_factory: Callable[..., subprocess.Popen] = subprocess.Popen,
        port_allocator: Callable[[], int] = _free_local_port,
    ) -> None:
        if not host or any(character.isspace() for character in host):
            raise SshError("SSH tunnel host must be non-empty and contain no whitespace")
        if host.startswith("-"):
            raise SshError("SSH tunnel host must not start with '-'")
        if not executable:
            raise SshError("SSH executable cannot be empty")
        if not 1 <= remote_port <= 65535:
            raise SshError("remote tunnel port must be between 1 and 65535")
        if not remote_host or any(character.isspace() for character in remote_host):
            raise SshError("remote tunnel host must be non-empty without whitespace")
        if connect_timeout_s <= 0:
            raise SshError("SSH tunnel timeout must be positive")
        chosen_port = int(local_port) if local_port is not None else port_allocator()
        if not 1 <= chosen_port <= 65535:
            raise SshError("local tunnel port must be between 1 and 65535")
        self.host = host
        self.remote_port = int(remote_port)
        self.remote_host = remote_host
        self.executable = executable
        self.connect_timeout_s = float(connect_timeout_s)
        self.local_port = chosen_port
        self._process_factory = process_factory
        self._process: subprocess.Popen | None = None
        self._stderr_file: tempfile._TemporaryFileWrapper | None = None

    def _ssh_command(self) -> list[str]:
        connect_timeout = max(1, int(round(self.connect_timeout_s)))
        return [
            self.executable,
            "-o",
            "BatchMode=yes",
            "-o",
            "ExitOnForwardFailure=yes",
            "-o",
            f"ConnectTimeout={connect_timeout}",
            "-o",
            "ServerAliveInterval=5",
            "-o",
            "ServerAliveCountMax=2",
            "-N",
            "-L",
            f"127.0.0.1:{self.local_port}:{self.remote_host}:{self.remote_port}",
            self.host,
        ]

    @property
    def active(self) -> bool:
        process = self._process
        return process is not None and process.poll() is None

    def start(self) -> int:
        if self.active:
            return self.local_port
        stderr_file = tempfile.TemporaryFile(mode="w+", encoding="utf-8")
        try:
            process = self._process_factory(
                self._ssh_command(),
                stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL,
                stderr=stderr_file,
                text=True,
            )
        except OSError as exc:
            stderr_file.close()
            raise SshError(f"cannot start SSH tunnel: {exc}") from exc
        self._process = process
        self._stderr_file = stderr_file
        deadline = time.monotonic() + self.connect_timeout_s
        while time.monotonic() < deadline:
            if process.poll() is not None:
                detail = self._stderr_text()
                self.stop()
                raise SshError(
                    "SSH tunnel exited before becoming ready"
                    + (f": {detail}" if detail else "")
                )
            try:
                with socket.create_connection(
                    ("127.0.0.1", self.local_port),
                    timeout=0.2,
                ):
                    return self.local_port
            except OSError:
                time.sleep(0.05)
        detail = self._stderr_text()
        self.stop()
        raise SshError(
            "SSH tunnel did not become ready"
            + (f": {detail}" if detail else "")
        )

    def _stderr_text(self) -> str:
        stderr_file = self._stderr_file
        if stderr_file is None:
            return ""
        try:
            stderr_file.flush()
            stderr_file.seek(0)
            return stderr_file.read().strip()
        except (OSError, ValueError):
            return ""

    def stop(self) -> None:
        process = self._process
        self._process = None
        stderr_file = self._stderr_file
        self._stderr_file = None
        if process is not None:
            try:
                if process.poll() is None:
                    process.terminate()
                    try:
                        process.wait(timeout=3.0)
                    except subprocess.TimeoutExpired:
                        process.kill()
                        process.wait(timeout=2.0)
            except (OSError, subprocess.SubprocessError):
                pass
        if stderr_file is not None:
            try:
                stderr_file.close()
            except OSError:
                pass

    def __enter__(self) -> "SshTunnel":
        self.start()
        return self

    def __exit__(self, exc_type, exc, traceback) -> None:
        del exc_type, exc, traceback
        self.stop()
