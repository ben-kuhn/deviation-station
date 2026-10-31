import subprocess
from pathlib import Path

import pytest

from deviation_meter.direwolf import (
    DirewolfCalibration,
    DirewolfConfig,
    DirewolfError,
    build_calibration_config,
    parse_direwolf_config,
    probe_direwolf,
    profile_tone_hz,
)
from deviation_meter.pipewire import StreamNode


def _write_config(tmp_path: Path, body: str) -> Path:
    tmp_path.mkdir(parents=True, exist_ok=True)
    path = tmp_path / "direwolf.conf"
    path.write_text(body, encoding="utf-8")
    return path


def test_parse_standard_1200_config(tmp_path: Path) -> None:
    config = _write_config(
        tmp_path,
        """
# comment
ADEVICE digirig-ts2000
ARATE 48000
ACHANNELS 1
CHANNEL 0
MYCALL N0CALL
MODEM 1200
""",
    )
    profile = parse_direwolf_config(config)
    assert profile.audio_device == "digirig-ts2000"
    assert profile.sample_rate_hz == 48_000
    assert profile.channels == 1
    assert profile.baud == 1200
    assert profile.mark_hz == 1200.0
    assert profile.space_hz == 2200.0
    assert profile_tone_hz(profile, "mark") == 1200.0
    assert profile_tone_hz(profile, "space") == 2200.0


def test_parse_custom_tones_and_generated_config(tmp_path: Path) -> None:
    config = _write_config(
        tmp_path,
        "ADEVICE - plughw:1,0\nACHANNELS 1\nMODEM 1200 1500:2500\n",
    )
    profile = parse_direwolf_config(config)
    assert profile.audio_device == "- plughw:1,0"
    assert profile.mark_hz == 1500.0
    assert profile.space_hz == 2500.0
    generated = build_calibration_config(
        profile,
        rig_host="127.0.0.1",
        rig_port=4532,
    )
    assert "ADEVICE - plughw:1,0" in generated
    assert "MODEM 1200 1500:2500" in generated
    assert "PTT RIG 2 127.0.0.1:4532" in generated
    assert "AGWPORT 0" in generated
    assert "KISSPORT 0" in generated
    assert "TXDELAY 100" in generated


def test_parse_rejects_missing_or_unsupported_audio_config(tmp_path: Path) -> None:
    missing_device = _write_config(tmp_path / "a", "MODEM 1200\n")
    with pytest.raises(DirewolfError, match="ADEVICE"):
        parse_direwolf_config(missing_device)

    stereo = _write_config(
        tmp_path / "b",
        "ADEVICE hw:1,0\nACHANNELS 2\nMODEM 1200\n",
    )
    with pytest.raises(DirewolfError, match="ACHANNELS 1"):
        parse_direwolf_config(stereo)

    baseband = _write_config(
        tmp_path / "c",
        "ADEVICE hw:1,0\nACHANNELS 1\nMODEM 9600\n",
    )
    with pytest.raises(DirewolfError, match="no AFSK"):
        parse_direwolf_config(baseband)


def test_probe_direwolf_requires_calibration_option() -> None:
    def runner(command, **kwargs):
        del kwargs
        return subprocess.CompletedProcess(
            command,
            0,
            "Dire Wolf Release 1.8.1, November 2025\n    -x Send Xmit level "
            "calibration tones.\n",
            "",
        )

    assert probe_direwolf(runner=runner) == "1.8.1"

    def no_x(command, **kwargs):
        del kwargs
        return subprocess.CompletedProcess(command, 0, "Dire Wolf Release 1.8.1\n", "")

    with pytest.raises(DirewolfError, match="-x"):
        probe_direwolf(runner=no_x)


class FakeProcess:
    pid = 1234

    def __init__(self, command, **kwargs) -> None:
        self.command = command
        self.kwargs = kwargs
        self.returncode = None
        self.signals = []
        self.terminated = False
        self.killed = False

    def poll(self):
        return self.returncode

    def send_signal(self, sig) -> None:
        self.signals.append(sig)
        self.returncode = 0

    def wait(self, timeout=None):
        del timeout
        return self.returncode

    def terminate(self) -> None:
        self.terminated = True
        self.returncode = -15

    def kill(self) -> None:
        self.killed = True
        self.returncode = -9


def test_calibration_process_owns_stream_and_cleans_up(tmp_path: Path) -> None:
    config = _write_config(
        tmp_path,
        "ADEVICE digirig-ts2000\nARATE 48000\nACHANNELS 1\nMODEM 1200\n",
    )
    process = FakeProcess([])
    events: list[tuple] = []
    stream = StreamNode(
        node_id=97,
        client_id=51,
        process_id=1234,
        application="PipeWire ALSA [direwolf]",
        binary="direwolf",
        node_name="alsa_playback.direwolf",
        media_class="Stream/Output/Audio",
    )

    def process_factory(command, **kwargs):
        process.command = command
        process.kwargs = kwargs
        return process

    def stream_waiter(pid, *, binary, timeout_s):
        events.append(("wait", pid, binary, timeout_s))
        return stream

    probe_calls = []

    def runner(command, **kwargs):
        del kwargs
        probe_calls.append(command)
        return subprocess.CompletedProcess(
            command,
            0,
            "Dire Wolf Release 1.8.1\n    -x Send Xmit level calibration tones.\n",
            "",
        )

    calibration = DirewolfCalibration(
        DirewolfConfig(base_config=config, tone="mark"),
        runner=runner,
        process_factory=process_factory,
        process_checker=lambda: [],
        stream_waiter=stream_waiter,
        volume_setter=lambda node, level: events.append(("volume", node, level)),
        mute_setter=lambda node, muted: events.append(("mute", node, muted)),
        rig_unkey=lambda: events.append(("unkey",)) or True,
        ptt_verifier=lambda: True,
        sleeper=lambda seconds: None,
    )

    returned = calibration.start(0.0)
    assert returned == stream
    assert calibration.running is True
    assert process.command == [
        "direwolf",
        "-c",
        str(calibration._temp_dir / "calibration.conf"),
        "-x",
        "m",
        "-t",
        "0",
        "-q",
        "hd",
    ]
    assert probe_calls == [["direwolf", "-h"]]
    assert events[0] == ("wait", 1234, "direwolf", 3.0)
    assert ("mute", 97, False) in events
    assert ("volume", 97, 0.0) in events

    calibration.set_level(0.6)
    assert ("volume", 97, 0.6) in events

    calibration.stop()
    assert calibration.running is False
    assert ("unkey",) in events
    assert process.signals
    assert calibration._temp_dir is None


def test_calibration_stops_when_ptt_is_not_confirmed(tmp_path: Path) -> None:
    config = _write_config(
        tmp_path,
        "ADEVICE digirig-ts2000\nACHANNELS 1\nMODEM 1200\n",
    )
    process = FakeProcess([])
    stream = StreamNode(
        node_id=97,
        client_id=51,
        process_id=1234,
        application="PipeWire ALSA [direwolf]",
        binary="direwolf",
        node_name="alsa_playback.direwolf",
        media_class="Stream/Output/Audio",
    )
    unkeyed: list[bool] = []

    def runner(command, **kwargs):
        del kwargs
        return subprocess.CompletedProcess(
            command,
            0,
            "Dire Wolf Release 1.8.1\n    -x Send Xmit level calibration tones.\n",
            "",
        )

    calibration = DirewolfCalibration(
        DirewolfConfig(base_config=config),
        runner=runner,
        process_factory=lambda command, **kwargs: process,
        process_checker=lambda: [],
        stream_waiter=lambda *args, **kwargs: stream,
        volume_setter=lambda node, level: None,
        mute_setter=lambda node, muted: None,
        rig_unkey=lambda: unkeyed.append(True) or True,
        ptt_verifier=lambda: False,
        sleeper=lambda seconds: None,
    )

    with pytest.raises(DirewolfError, match="PTT on"):
        calibration.start(0.0)
    assert unkeyed == [True]
    assert process.signals


def test_calibration_refuses_competing_direwolf(tmp_path: Path) -> None:
    config = _write_config(
        tmp_path,
        "ADEVICE digirig-ts2000\nACHANNELS 1\nMODEM 1200\n",
    )
    calibration = DirewolfCalibration(
        DirewolfConfig(base_config=config),
        process_checker=lambda: [999],
        sleeper=lambda seconds: None,
    )
    with pytest.raises(DirewolfError, match="existing Direwolf"):
        calibration.start(0.0)
