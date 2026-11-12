import json
import subprocess
from collections.abc import Callable

import pytest

from deviation_station.pipewire import (
    PipeWireError,
    find_output_stream,
    get_stream_volume,
    parse_pw_dump,
    parse_volume,
    set_stream_mute,
    set_stream_volume,
    wait_for_output_stream,
)


def _dump() -> str:
    objects = [
        {
            "id": 51,
            "type": "PipeWire:Interface:Client",
            "info": {
                "props": {
                    "application.name": "PipeWire ALSA [direwolf]",
                    "application.process.binary": "direwolf",
                    "application.process.id": 4242,
                    "target.object": "alsa_output.usb",
                }
            },
        },
        {
            "id": 97,
            "type": "PipeWire:Interface:Node",
            "info": {
                "props": {
                    "application.name": "PipeWire ALSA [direwolf]",
                    "client.id": 51,
                    "media.class": "Stream/Output/Audio",
                    "node.name": "alsa_playback.direwolf",
                }
            },
        },
        {
            "id": 48,
            "type": "PipeWire:Interface:Node",
            "info": {
                "props": {
                    "media.class": "Audio/Sink",
                    "node.name": "alsa_output.usb",
                }
            },
        },
    ]
    return json.dumps(objects)


def test_parse_pw_dump_finds_output_process() -> None:
    streams = parse_pw_dump(_dump())
    assert len(streams) == 1
    stream = streams[0]
    assert stream.node_id == 97
    assert stream.client_id == 51
    assert stream.process_id == 4242
    assert stream.binary == "direwolf"
    assert stream.is_output is True
    assert stream.target_object == "alsa_output.usb"
    assert find_output_stream(4242, streams=streams) == stream


def test_parse_volume_and_mute_state() -> None:
    assert parse_volume("Volume: 0.88\n") == (0.88, False)
    assert parse_volume("Volume: 1.00 [MUTED]\n") == (1.0, True)
    with pytest.raises(PipeWireError):
        parse_volume("not a volume")


def _runner_factory(
    calls: list[list[str]],
    result: Callable[[list[str]], subprocess.CompletedProcess],
) -> Callable[..., subprocess.CompletedProcess]:
    def runner(command, **kwargs):
        del kwargs
        calls.append(list(command))
        return result(list(command))

    return runner


def test_volume_commands_are_bounded() -> None:
    calls: list[list[str]] = []
    runner = _runner_factory(
        calls,
        lambda command: subprocess.CompletedProcess(command, 0, "Volume: 0.50\n", ""),
    )
    set_stream_volume(97, 0.5, runner=runner)
    set_stream_mute(97, True, runner=runner)
    assert get_stream_volume(97, runner=runner) == (0.5, False)
    assert calls == [
        ["wpctl", "set-volume", "97", "0.500"],
        ["wpctl", "set-mute", "97", "1"],
        ["wpctl", "get-volume", "97"],
    ]
    with pytest.raises(PipeWireError):
        set_stream_volume(97, 1.1, runner=runner)
    with pytest.raises(PipeWireError):
        set_stream_volume(97, -0.1, runner=runner)


def test_wait_for_output_stream_polls_until_available() -> None:
    clock = [0.0]
    calls = 0

    def read_streams():
        nonlocal calls
        calls += 1
        if calls < 3:
            return []
        return parse_pw_dump(_dump())

    def sleeper(seconds: float) -> None:
        clock[0] += seconds

    stream = wait_for_output_stream(
        4242,
        read_streams=read_streams,
        clock=lambda: clock[0],
        sleeper=sleeper,
        timeout_s=1.0,
    )
    assert stream.node_id == 97
    assert calls == 3
