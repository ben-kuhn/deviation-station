import subprocess
from pathlib import Path

import pytest

from deviation_meter.direwolf import DirewolfError
from deviation_meter.pipewire import StreamNode
from deviation_meter.remote_direwolf import (
    RemoteDirewolfCalibration,
    RemoteDirewolfConfig,
)


class FakeTransport:
    def __init__(self) -> None:
        self.commands: list[tuple[list[str], object]] = []
        self.shells: list[str] = []
        self.files: dict[str, str] = {}
        self.closed = False
        self.existing_pids = ""
        self.version_output = (
            "Dire Wolf Release 1.8.1, November 2025\n"
            "Dire Wolf version 1.8\n"
            "Options:\n"
            "-x calibration tone mode\n"
        )

    def run(self, argv, **kwargs):
        command = list(argv)
        self.commands.append((command, kwargs.get("input")))
        text = ""
        if command[:2] == ["mktemp", "-d"]:
            text = "/tmp/deviation-meter-direwolf.test\n"
        elif command and command[0] == "direwolf" and command[1] == "-h":
            text = self.version_output
        return subprocess.CompletedProcess(command, 0, text, "")

    def run_shell(self, command: str, **kwargs):
        self.shells.append(command)
        text = ""
        if "pgrep -x direwolf" in command:
            text = self.existing_pids
        elif "nohup sh" in command:
            text = "4321\n"
        return subprocess.CompletedProcess(["sh", "-c", command], 0, text, "")

    def read_text(self, path: str) -> str:
        return self.files.get(path, "")

    def write_text(self, path: str, text: str) -> None:
        self.files[path] = text

    def remove(self, path: str) -> None:
        self.commands.append((["rm", "-rf", path], None))

    def pipewire_runner(self, argv, **kwargs):
        command = list(argv)
        self.commands.append((command, None))
        return subprocess.CompletedProcess(command, 0, "", "")

    def close(self) -> None:
        self.closed = True


class FakePtt:
    def __init__(self) -> None:
        self.keyed = True
        self.set_calls: list[bool] = []

    def get_ptt(self) -> bool:
        return self.keyed

    def set_ptt(self, enabled: bool) -> bool:
        self.set_calls.append(enabled)
        self.keyed = enabled
        return enabled


def _config(tmp_path: Path, **overrides) -> RemoteDirewolfConfig:
    base = tmp_path / "direwolf.conf"
    base.write_text(
        "ADEVICE digirig-remote\nARATE 48000\nACHANNELS 1\nMODEM 1200\n",
        encoding="utf-8",
    )
    values = {
        "ssh_host": "bench-pi",
        "base_config": base,
        "remote_rig_host": "127.0.0.1",
        "remote_rig_port": 4532,
        "tone": "mark",
    }
    values.update(overrides)
    return RemoteDirewolfConfig(**values)


def test_remote_direwolf_starts_and_cleans_up(tmp_path: Path) -> None:
    transport = FakeTransport()
    stream = StreamNode(
        node_id=97,
        client_id=51,
        process_id=4321,
        application="PipeWire ALSA [direwolf]",
        binary="direwolf",
        node_name="alsa_playback.direwolf",
        media_class="Stream/Output/Audio",
    )
    events: list[tuple] = []
    ptt = FakePtt()

    def stream_waiter(pid, **kwargs):
        events.append(("wait", pid, kwargs))
        return stream

    calibration = RemoteDirewolfCalibration(
        _config(tmp_path),
        transport=transport,
        stream_waiter=stream_waiter,
        volume_setter=lambda node, level, **kwargs: events.append(
            ("volume", node, level)
        ),
        mute_setter=lambda node, muted, **kwargs: events.append(
            ("mute", node, muted)
        ),
        ptt_client_factory=lambda *args, **kwargs: ptt,
        sleeper=lambda seconds: None,
    )

    assert calibration.start(0.0) == stream
    config_path = "/tmp/deviation-meter-direwolf.test/calibration.conf"
    script_path = "/tmp/deviation-meter-direwolf.test/run.sh"
    assert "PTT RIG 2 127.0.0.1:4532" in transport.files[config_path]
    assert "-x m" in transport.files[script_path]
    assert "timeout --signal=INT" in transport.files[script_path]
    assert ("mute", 97, False) in events
    assert ("volume", 97, 0.0) in events
    assert calibration.set_level(0.4) is None
    assert ("volume", 97, 0.4) in events

    calibration.stop()
    assert ptt.keyed is False
    assert transport.closed is True
    assert any(command[0][:2] == ["kill", "-INT"] for command in transport.commands)


def test_remote_direwolf_refuses_existing_remote_instance(tmp_path: Path) -> None:
    transport = FakeTransport()
    transport.existing_pids = "999\n"
    calibration = RemoteDirewolfCalibration(
        _config(tmp_path),
        transport=transport,
        stream_waiter=lambda *args, **kwargs: (_ for _ in ()).throw(
            AssertionError("should not wait")
        ),
        ptt_client_factory=lambda *args, **kwargs: FakePtt(),
        sleeper=lambda seconds: None,
    )
    with pytest.raises(DirewolfError, match="existing remote Direwolf"):
        calibration.start(0.0)


def test_remote_direwolf_stops_on_stream_failure(tmp_path: Path) -> None:
    transport = FakeTransport()
    ptt = FakePtt()

    def failing_waiter(*args, **kwargs):
        raise DirewolfError("no remote playback stream")

    calibration = RemoteDirewolfCalibration(
        _config(tmp_path),
        transport=transport,
        stream_waiter=failing_waiter,
        ptt_client_factory=lambda *args, **kwargs: ptt,
        sleeper=lambda seconds: None,
    )
    with pytest.raises(DirewolfError, match="no remote playback"):
        calibration.start(0.0)
    assert ptt.keyed is False
    assert transport.closed is True
