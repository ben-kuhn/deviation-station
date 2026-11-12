import socket
import threading
from collections.abc import Callable

import pytest

from deviation_station.rigctl import RigctldClient, RigctldError


class FakeRigctld:
    def __init__(self, handler: Callable[[str], str]) -> None:
        self._handler = handler
        self._listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self._listener.bind(("127.0.0.1", 0))
        self._listener.listen()
        self._listener.settimeout(0.2)
        self.port = self._listener.getsockname()[1]
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._run, daemon=True)
        self._thread.start()

    def _run(self) -> None:
        while not self._stop.is_set():
            try:
                connection, _ = self._listener.accept()
            except TimeoutError:
                continue
            except OSError:
                break
            with connection:
                stream = connection.makefile("rwb")
                line = stream.readline()
                if not line:
                    continue
                command = line.decode("ascii").strip()
                response = self._handler(command)
                stream.write((response + "\n").encode("ascii"))
                stream.flush()

    def close(self) -> None:
        self._stop.set()
        self._listener.close()
        self._thread.join(timeout=1.0)


def test_rigctld_reads_and_confirms_frequency() -> None:
    state = {"frequency": 145_500_000, "ptt": 0}

    def handler(command: str) -> str:
        if command == "f":
            return str(state["frequency"])
        if command.startswith("F "):
            state["frequency"] = int(command.split()[1])
            return "RPRT 0"
        if command == "t":
            return str(state["ptt"])
        if command.startswith("T "):
            state["ptt"] = int(command.split()[1])
            return "RPRT 0"
        if command == "m":
            return "FM\n12000"
        return "RPRT -1"

    server = FakeRigctld(handler)
    try:
        client = RigctldClient(port=server.port)
        assert client.get_frequency() == 145_500_000
        assert client.set_frequency(145_725_000) == 145_725_000
        assert client.get_mode() == ("FM", 12_000)
        assert client.get_ptt() is False
        assert client.set_ptt(True) is True
        assert client.set_ptt(False) is False
    finally:
        server.close()


def test_rigctld_reports_rejected_commands() -> None:
    server = FakeRigctld(lambda command: "RPRT -1")
    try:
        client = RigctldClient(port=server.port)
        with pytest.raises(RigctldError, match="frequency read failed"):
            client.get_frequency()
        with pytest.raises(RigctldError, match="frequency write failed"):
            client.set_frequency(145_500_000)
    finally:
        server.close()


def test_rigctld_reports_rejected_mode_read() -> None:
    server = FakeRigctld(lambda command: "RPRT -1")
    try:
        client = RigctldClient(port=server.port)
        with pytest.raises(RigctldError, match="mode read failed"):
            client.get_mode()
    finally:
        server.close()


def test_rigctld_rejects_invalid_parameter_values() -> None:
    with pytest.raises(RigctldError):
        RigctldClient(port=0)
    client = RigctldClient()
    with pytest.raises(RigctldError):
        client.set_frequency(0)
