"""Minimal network client for Hamlib rigctld."""

from __future__ import annotations

import math
import socket


class RigctldError(RuntimeError):
    """Raised when rigctld is unavailable or rejects a command."""


class RigctldClient:
    """Read and write frequency through an existing rigctld instance."""

    def __init__(
        self,
        host: str = "127.0.0.1",
        port: int = 4532,
        *,
        timeout_s: float = 1.0,
    ) -> None:
        if not host:
            raise RigctldError("rigctld host cannot be empty")
        if not 1 <= port <= 65535:
            raise RigctldError("rigctld port must be between 1 and 65535")
        if not math.isfinite(timeout_s) or timeout_s <= 0.0:
            raise RigctldError("rigctld timeout must be finite and positive")
        self.host = host
        self.port = port
        self.timeout_s = timeout_s

    def _response_lines(
        self,
        command: str,
        line_count: int = 1,
        *,
        stop_on_rprt: bool = False,
    ) -> list[str]:
        if not command or "\n" in command or "\r" in command:
            raise RigctldError("invalid rigctld command")
        if line_count < 1:
            raise RigctldError("line_count must be positive")
        try:
            with socket.create_connection(
                (self.host, self.port),
                timeout=self.timeout_s,
            ) as connection:
                connection.settimeout(self.timeout_s)
                stream = connection.makefile("rwb")
                stream.write((command + "\n").encode("ascii"))
                stream.flush()
                responses = []
                for _ in range(line_count):
                    response = stream.readline()
                    if not response:
                        break
                    responses.append(response)
                    if stop_on_rprt and response.startswith(b"RPRT"):
                        break
        except (OSError, UnicodeError) as exc:
            raise RigctldError(
                f"cannot communicate with rigctld at {self.host}:{self.port}: {exc}"
            ) from exc
        if not responses or not responses[0]:
            raise RigctldError("rigctld returned an empty response")
        lines: list[str] = []
        for response in responses:
            if not response:
                raise RigctldError("rigctld returned an incomplete response")
            try:
                lines.append(response.decode("ascii").strip())
            except UnicodeDecodeError as exc:
                raise RigctldError(
                    "rigctld returned a non-ASCII response"
                ) from exc
        return lines

    def _command(self, command: str) -> str:
        return self._response_lines(command)[0]

    def get_frequency(self) -> int:
        """Return the current rig frequency in Hz."""

        response = self._command("f")
        if response.startswith("RPRT"):
            raise RigctldError(f"rigctld frequency read failed: {response}")
        try:
            frequency = int(round(float(response)))
        except ValueError as exc:
            raise RigctldError(f"invalid rigctld frequency response: {response!r}") from exc
        if frequency <= 0:
            raise RigctldError("rigctld returned a non-positive frequency")
        return frequency

    def set_frequency(self, frequency_hz: int) -> int:
        """Set frequency, then read it back and return the confirmed value."""

        if isinstance(frequency_hz, bool) or not isinstance(frequency_hz, int):
            raise RigctldError("frequency_hz must be an integer")
        if frequency_hz <= 0:
            raise RigctldError("frequency_hz must be positive")
        response = self._command(f"F {frequency_hz}")
        if response != "RPRT 0":
            raise RigctldError(f"rigctld frequency write failed: {response}")
        return self.get_frequency()

    def get_mode(self) -> tuple[str, int]:
        """Return the rig mode name and passband width."""

        lines = self._response_lines("m", 2, stop_on_rprt=True)
        if lines[0].startswith("RPRT"):
            raise RigctldError(f"rigctld mode read failed: {lines[0]}")
        if len(lines) < 2:
            raise RigctldError("rigctld returned an incomplete mode response")
        mode = lines[0].strip().upper()
        if not mode:
            raise RigctldError("rigctld returned an empty mode")
        try:
            passband = int(round(float(lines[1])))
        except ValueError as exc:
            raise RigctldError(
                f"invalid rigctld passband response: {lines[1]!r}"
            ) from exc
        return mode, passband

    def get_ptt(self) -> bool:
        """Return the rig's current PTT state."""

        response = self._command("t")
        if response.startswith("RPRT"):
            raise RigctldError(f"rigctld PTT read failed: {response}")
        try:
            value = int(response)
        except ValueError as exc:
            raise RigctldError(f"invalid rigctld PTT response: {response!r}") from exc
        if value not in (0, 1):
            raise RigctldError(f"invalid rigctld PTT state: {value}")
        return bool(value)

    def set_ptt(self, enabled: bool) -> bool:
        """Set PTT, then read it back and return the confirmed state."""

        if not isinstance(enabled, bool):
            raise RigctldError("enabled must be a boolean")
        response = self._command(f"T {1 if enabled else 0}")
        if response != "RPRT 0":
            raise RigctldError(f"rigctld PTT write failed: {response}")
        return self.get_ptt()
