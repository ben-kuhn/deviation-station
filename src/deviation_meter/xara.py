"""Control an existing xara modem's 1500 Hz TUNE source."""

from __future__ import annotations

import math
import socket
import time
from collections.abc import Callable
from dataclasses import dataclass

from .rigctl import RigctldClient, RigctldError


class XaraError(RuntimeError):
    """Raised when the xara command port or TUNE state cannot be controlled."""


@dataclass(frozen=True, slots=True)
class XaraConfig:
    host: str = "127.0.0.1"
    port: int = 8300
    min_drive_db: float = -30.0
    max_drive_db: float = 0.0
    rig_host: str = "127.0.0.1"
    rig_port: int = 4532
    timeout_s: float = 1.0

    def __post_init__(self) -> None:
        if not self.host or any(char.isspace() for char in self.host):
            raise XaraError("xara host must be non-empty and contain no spaces")
        if not 1 <= self.port <= 65535:
            raise XaraError("xara command port must be between 1 and 65535")
        if not 1 <= self.rig_port <= 65535:
            raise XaraError("rig port must be between 1 and 65535")
        if not (
            math.isfinite(self.min_drive_db)
            and math.isfinite(self.max_drive_db)
            and -30.0 <= self.min_drive_db < self.max_drive_db <= 0.0
        ):
            raise XaraError("xara drive range must satisfy -30 <= min < max <= 0")
        if not math.isfinite(self.timeout_s) or self.timeout_s <= 0.0:
            raise XaraError("xara timeout must be finite and positive")


class XaraClient:
    """Line-oriented client for xara's VARA-compatible command port."""

    def __init__(
        self,
        host: str,
        port: int,
        *,
        timeout_s: float = 1.0,
    ) -> None:
        if not host or any(char.isspace() for char in host):
            raise XaraError("xara host must be non-empty and contain no spaces")
        if not 1 <= port <= 65535:
            raise XaraError("xara command port must be between 1 and 65535")
        if not math.isfinite(timeout_s) or timeout_s <= 0.0:
            raise XaraError("xara timeout must be finite and positive")
        self.host = host
        self.port = port
        self.timeout_s = float(timeout_s)
        self._socket: socket.socket | None = None
        self._buffer = b""

    @property
    def connected(self) -> bool:
        return self._socket is not None

    def connect(self) -> None:
        if self._socket is not None:
            return
        try:
            connection = socket.create_connection(
                (self.host, self.port),
                timeout=self.timeout_s,
            )
            connection.settimeout(self.timeout_s)
        except OSError as exc:
            raise XaraError(
                f"cannot connect to xara command port "
                f"{self.host}:{self.port}: {exc}"
            ) from exc
        self._socket = connection
        self._buffer = b""

    _ASYNC_PREFIXES = (
        "BUSY ",
        "PTT ",
        "BITRATE ",
        "SN ",
        "BUFFER ",
        "CONNECTED ",
        "REGISTERED ",
        "LINK ",
    )
    _ASYNC_LINES = {
        "PENDING",
        "CANCELPENDING",
        "DISCONNECTED",
        "IAMALIVE",
        "UNENCRYPTED LINK",
        "ENCRYPTION DISABLED",
    }

    def _read_line(self) -> str:
        if self._socket is None:
            raise XaraError("xara command port is not connected")
        while b"\r" not in self._buffer and b"\n" not in self._buffer:
            try:
                chunk = self._socket.recv(4096)
            except socket.timeout as exc:
                raise XaraError("xara command response timed out") from exc
            except OSError as exc:
                raise XaraError(f"xara command read failed: {exc}") from exc
            if not chunk:
                raise XaraError("xara command port closed unexpectedly")
            self._buffer += chunk
        separators = [
            index
            for index in (self._buffer.find(b"\r"), self._buffer.find(b"\n"))
            if index >= 0
        ]
        split = min(separators)
        raw = self._buffer[:split]
        self._buffer = self._buffer[split + 1 :]
        if self._buffer.startswith(b"\n"):
            self._buffer = self._buffer[1:]
        try:
            return raw.decode("ascii").strip()
        except UnicodeDecodeError as exc:
            raise XaraError("xara returned a non-ASCII response") from exc

    def _read_response(self) -> str:
        for _ in range(32):
            line = self._read_line()
            if not line:
                continue
            if line in self._ASYNC_LINES:
                continue
            if line.startswith(self._ASYNC_PREFIXES):
                continue
            return line
        raise XaraError("too many asynchronous xara status lines")

    def command(self, command: str) -> str:
        if self._socket is None:
            raise XaraError("xara command port is not connected")
        if not command or "\r" in command or "\n" in command:
            raise XaraError("invalid xara command")
        try:
            self._socket.sendall((command + "\r").encode("ascii"))
        except (OSError, UnicodeError) as exc:
            raise XaraError(f"xara command write failed: {exc}") from exc
        return self._read_response()

    def version(self) -> str:
        response = self.command("VERSION")
        if not response.startswith("VERSION "):
            raise XaraError(f"unexpected xara VERSION response: {response!r}")
        return response

    def tune(self, drive_db: float) -> int:
        if not math.isfinite(drive_db):
            raise XaraError("xara drive must be finite")
        drive = max(-30, min(0, int(round(drive_db))))
        response = self.command(f"TUNE {drive}")
        if response != "OK":
            raise XaraError(f"xara TUNE failed: {response!r}")
        return drive

    def tune_off(self) -> None:
        if self._socket is None:
            return
        response = self.command("TUNE OFF")
        if response != "OK":
            raise XaraError(f"xara TUNE OFF failed: {response!r}")

    def tune_query(self) -> int:
        response = self.command("TUNE ?")
        if not response.startswith("TUNE "):
            raise XaraError(f"unexpected xara TUNE query response: {response!r}")
        try:
            return int(round(float(response.split(maxsplit=1)[1])))
        except (IndexError, ValueError) as exc:
            raise XaraError(f"invalid xara TUNE query response: {response!r}") from exc

    def close(self) -> None:
        connection = self._socket
        self._socket = None
        self._buffer = b""
        if connection is not None:
            try:
                connection.close()
            except OSError:
                pass


class XaraCalibration:
    """Use an existing xara instance's TUNE mode as a calibration source."""

    tone_frequency_hz = 1500.0

    def __init__(
        self,
        config: XaraConfig,
        *,
        client_factory: Callable[..., XaraClient] = XaraClient,
        ptt_on_verifier: Callable[[], bool] | None = None,
        ptt_off_verifier: Callable[[], bool] | None = None,
        sleeper: Callable[[float], None] = time.sleep,
    ) -> None:
        self.config = config
        self._client_factory = client_factory
        self._ptt_on_verifier = ptt_on_verifier or self._default_ptt_on
        self._ptt_off_verifier = ptt_off_verifier or self._default_ptt_off
        self._sleeper = sleeper
        self._client: XaraClient | None = None
        self._drive_db: int | None = None
        self.version = ""

    @property
    def connected(self) -> bool:
        return self._client is not None and self._client.connected

    @property
    def drive_db(self) -> int | None:
        return self._drive_db

    def _default_ptt_on(self) -> bool:
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
        raise XaraError(
            f"xara did not key the transmitter: {last_error or 'PTT off'}"
        )

    def _default_ptt_off(self) -> bool:
        client = RigctldClient(self.config.rig_host, self.config.rig_port)
        for _ in range(10):
            try:
                if not client.get_ptt():
                    return True
                client.set_ptt(False)
            except RigctldError:
                pass
            self._sleeper(0.2)
        return False

    def _drive_for_level(self, level: float) -> int:
        if not math.isfinite(level) or not 0.0 <= level <= 1.0:
            raise XaraError("xara level must be between 0 and 1")
        span = self.config.max_drive_db - self.config.min_drive_db
        return int(round(self.config.min_drive_db + span * level))

    def start(self, level: float = 0.0) -> int:
        if self.connected:
            raise XaraError("xara TUNE is already active")
        client = self._client_factory(
            self.config.host,
            self.config.port,
            timeout_s=self.config.timeout_s,
        )
        try:
            client.connect()
            self.version = client.version()
            drive = client.tune(self._drive_for_level(level))
            if not self._ptt_on_verifier():
                raise XaraError("xara TUNE did not confirm PTT on")
        except Exception:
            try:
                client.tune_off()
            except Exception:
                pass
            client.close()
            raise
        self._client = client
        self._drive_db = drive
        return drive

    def set_level(self, level: float) -> int:
        if self._client is None:
            raise XaraError("xara TUNE is not active")
        drive = self._client.tune(self._drive_for_level(level))
        self._drive_db = drive
        return drive

    def mute(self) -> int:
        return self.set_level(0.0)

    def stop(self) -> bool:
        client = self._client
        self._client = None
        self._drive_db = None
        error: str | None = None
        if client is not None:
            try:
                client.tune_off()
            except XaraError as exc:
                error = str(exc)
            finally:
                client.close()
        if not self._ptt_off_verifier():
            raise XaraError(error or "xara did not confirm PTT off")
        if error:
            raise XaraError(error)
        return True
