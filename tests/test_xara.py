import socket
import threading
import time

import pytest

from deviation_meter.xara import XaraCalibration, XaraClient, XaraConfig, XaraError


class FakeXaraServer:
    def __init__(self) -> None:
        self.listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self.listener.bind(("127.0.0.1", 0))
        self.listener.listen()
        self.port = self.listener.getsockname()[1]
        self.drive = -5
        self.commands: list[str] = []
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._run, daemon=True)
        self._thread.start()

    def _run(self) -> None:
        connection, _ = self.listener.accept()
        with connection:
            connection.sendall(b"BUSY ON\r")
            buffer = b""
            while not self._stop.is_set():
                chunk = connection.recv(4096)
                if not chunk:
                    break
                buffer += chunk
                while b"\r" in buffer:
                    raw, buffer = buffer.split(b"\r", 1)
                    command = raw.decode("ascii")
                    self.commands.append(command)
                    if command == "VERSION":
                        response = "VERSION VARA HF v4.9.0"
                    elif command == "TUNE ?":
                        response = f"TUNE {self.drive}"
                    elif command.startswith("TUNE OFF"):
                        self.drive = -5
                        response = "OK"
                    elif command.startswith("TUNE "):
                        self.drive = int(command.split()[1])
                        response = "OK"
                    else:
                        response = "WRONG"
                    connection.sendall((response + "\r").encode("ascii"))
                    if command.startswith("TUNE OFF"):
                        connection.sendall(b"PTT OFF\r")
                    elif command.startswith("TUNE "):
                        connection.sendall(b"PTT ON\r")

    def close(self) -> None:
        self._stop.set()
        try:
            socket.create_connection(("127.0.0.1", self.port), timeout=1).close()
        except OSError:
            pass
        self.listener.close()
        self._thread.join(timeout=1)


def test_xara_client_controls_tune_and_drive() -> None:
    server = FakeXaraServer()
    client = XaraClient("127.0.0.1", server.port, timeout_s=1.0)
    try:
        client.connect()
        assert client.version() == "VERSION VARA HF v4.9.0"
        assert client.tune(-12.4) == -12
        assert client.wait_ptt(True) is True
        assert client.tune_query() == -12
        client.tune_off()
        assert client.wait_ptt(False) is True
        server.close()
    finally:
        client.close()
    assert server.commands == [
        "VERSION",
        "TUNE -12",
        "TUNE ?",
        "TUNE OFF",
    ]


class FakeClient:
    def __init__(self, *, connected: bool = True) -> None:
        self.connected = connected
        self.commands: list[str] = []
        self.closed = False
        self.fail_connect = False
        self.fail_tune_off = False
        self.ptt_state = False
        self.ptt_on_result = True
        self.ptt_off_result = True

    def connect(self) -> None:
        if self.fail_connect:
            raise XaraError("no xara")
        self.connected = True

    def version(self) -> str:
        return "VERSION VARA HF v4.9.0"

    def tune(self, drive_db: float) -> int:
        drive = int(round(drive_db))
        self.commands.append(f"TUNE {drive}")
        return drive

    def tune_off(self) -> None:
        if self.fail_tune_off:
            raise XaraError("tune off failed")
        self.commands.append("TUNE OFF")

    def wait_ptt(self, expected: bool, timeout_s: float = 2.0) -> bool:
        del timeout_s
        if expected:
            self.ptt_state = self.ptt_on_result
        else:
            self.ptt_state = not self.ptt_off_result
        return self.ptt_state is expected

    def close(self) -> None:
        self.closed = True
        self.connected = False


def test_xara_calibration_maps_level_to_bounded_drive() -> None:
    client = FakeClient()
    calibration = XaraCalibration(
        XaraConfig(),
        client_factory=lambda *args, **kwargs: client,
    )

    assert calibration.start(0.5) == -15
    assert client.commands == ["TUNE -15"]
    assert client.ptt_state is True
    assert calibration.set_level(0.0) == -30
    assert calibration.set_level(1.0) == 0
    assert calibration.mute() == -30
    calibration.stop()
    assert "TUNE OFF" in client.commands
    assert client.closed is True


def test_xara_calibration_unkeys_when_ptt_is_not_confirmed() -> None:
    client = FakeClient()
    client.ptt_on_result = False

    calibration = XaraCalibration(
        XaraConfig(),
        client_factory=lambda *args, **kwargs: client,
    )

    with pytest.raises(XaraError, match="PTT ON"):
        calibration.start(0.0)
    assert "TUNE OFF" in client.commands
    assert client.closed is True


def test_xara_config_rejects_bad_values() -> None:
    with pytest.raises(XaraError):
        XaraConfig(port=0)
    with pytest.raises(XaraError):
        XaraConfig(min_drive_db=0.0, max_drive_db=0.0)
    with pytest.raises(XaraError):
        XaraConfig(max_drive_db=1.0)
    with pytest.raises(XaraError):
        XaraConfig(tone_frequency_hz=0.0)
