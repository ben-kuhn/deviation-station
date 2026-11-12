"""Discover and bound the volume of PipeWire playback streams."""

from __future__ import annotations

import json
import math
import re
import subprocess
import time
from collections.abc import Callable
from dataclasses import dataclass


class PipeWireError(RuntimeError):
    """Raised when PipeWire stream discovery or control fails."""


@dataclass(frozen=True, slots=True)
class StreamNode:
    node_id: int
    client_id: int | None
    process_id: int | None
    application: str
    binary: str
    node_name: str
    media_class: str
    target_object: str | None = None

    @property
    def is_output(self) -> bool:
        return self.media_class == "Stream/Output/Audio"


def parse_pw_dump(text: str) -> list[StreamNode]:
    """Parse a ``pw-dump`` JSON document into audio stream nodes."""

    try:
        objects = json.loads(text)
    except json.JSONDecodeError as exc:
        raise PipeWireError(f"invalid pw-dump JSON: {exc}") from exc
    if not isinstance(objects, list):
        raise PipeWireError("pw-dump did not return a list")

    clients: dict[int, dict] = {}
    for item in objects:
        if not isinstance(item, dict) or item.get("type") != "PipeWire:Interface:Client":
            continue
        props = item.get("info", {}).get("props", {})
        try:
            client_id = int(item.get("id"))
        except (TypeError, ValueError):
            continue
        if isinstance(props, dict):
            clients[client_id] = props

    streams: list[StreamNode] = []
    for item in objects:
        if not isinstance(item, dict) or item.get("type") != "PipeWire:Interface:Node":
            continue
        props = item.get("info", {}).get("props", {})
        if not isinstance(props, dict):
            continue
        media_class = str(props.get("media.class", ""))
        if media_class not in ("Stream/Output/Audio", "Stream/Input/Audio"):
            continue
        try:
            node_id = int(item.get("id"))
        except (TypeError, ValueError):
            continue
        client_id: int | None = None
        try:
            client_id = int(props.get("client.id"))
        except (TypeError, ValueError):
            pass
        client_props = clients.get(client_id, {}) if client_id is not None else {}
        process_id: int | None = None
        for value in (
            client_props.get("application.process.id"),
            props.get("application.process.id"),
        ):
            try:
                process_id = int(value)
                break
            except (TypeError, ValueError):
                continue
        application = str(
            props.get("application.name")
            or client_props.get("application.name")
            or ""
        )
        binary = str(
            client_props.get("application.process.binary")
            or props.get("application.process.binary")
            or ""
        )
        node_name = str(props.get("node.name") or "")
        target = client_props.get("target.object") or props.get("target.object")
        streams.append(
            StreamNode(
                node_id=node_id,
                client_id=client_id,
                process_id=process_id,
                application=application,
                binary=binary,
                node_name=node_name,
                media_class=media_class,
                target_object=str(target) if target is not None else None,
            )
        )
    return streams


def read_pw_dump(
    *,
    runner: Callable[..., subprocess.CompletedProcess] = subprocess.run,
) -> list[StreamNode]:
    try:
        result = runner(
            ["pw-dump"],
            capture_output=True,
            text=True,
            timeout=5.0,
            check=False,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        raise PipeWireError(f"cannot run pw-dump: {exc}") from exc
    if result.returncode != 0:
        message = result.stderr.strip() or f"exit status {result.returncode}"
        raise PipeWireError(f"pw-dump failed: {message}")
    return parse_pw_dump(result.stdout)


def find_output_stream(
    process_id: int,
    *,
    binary: str = "direwolf",
    streams: list[StreamNode] | None = None,
    runner: Callable[..., subprocess.CompletedProcess] = subprocess.run,
) -> StreamNode | None:
    nodes = streams if streams is not None else read_pw_dump(runner=runner)
    for node in nodes:
        if not node.is_output:
            continue
        if node.process_id == process_id:
            return node
    for node in nodes:
        if node.is_output and node.binary == binary:
            return node
    return None


def wait_for_output_stream(
    process_id: int,
    *,
    binary: str = "direwolf",
    timeout_s: float = 3.0,
    interval_s: float = 0.05,
    read_streams: Callable[[], list[StreamNode]] = read_pw_dump,
    clock: Callable[[], float] = time.monotonic,
    sleeper: Callable[[float], None] = time.sleep,
) -> StreamNode:
    if not math.isfinite(timeout_s) or timeout_s <= 0.0:
        raise PipeWireError("stream discovery timeout must be positive")
    deadline = clock() + timeout_s
    last_error: Exception | None = None
    while clock() < deadline:
        try:
            node = find_output_stream(
                process_id,
                binary=binary,
                streams=read_streams(),
            )
        except Exception as exc:  # keep polling through transient restarts
            last_error = exc
            node = None
        if node is not None:
            return node
        sleeper(interval_s)
    detail = f": {last_error}" if last_error is not None else ""
    raise PipeWireError(
        f"no {binary} playback stream appeared within {timeout_s:.1f} s{detail}"
    )


def parse_volume(text: str) -> tuple[float, bool]:
    match = re.search(r"Volume:\s*([0-9]+(?:\.[0-9]+)?)", text)
    if match is None:
        raise PipeWireError(f"cannot parse wpctl volume response: {text.strip()!r}")
    try:
        volume = float(match.group(1))
    except ValueError as exc:
        raise PipeWireError("invalid wpctl volume value") from exc
    return volume, "[MUTED]" in text


def get_stream_volume(
    node_id: int,
    *,
    runner: Callable[..., subprocess.CompletedProcess] = subprocess.run,
) -> tuple[float, bool]:
    try:
        result = runner(
            ["wpctl", "get-volume", str(node_id)],
            capture_output=True,
            text=True,
            timeout=3.0,
            check=False,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        raise PipeWireError(f"cannot run wpctl: {exc}") from exc
    if result.returncode != 0:
        message = result.stderr.strip() or f"exit status {result.returncode}"
        raise PipeWireError(f"wpctl get-volume failed: {message}")
    return parse_volume(result.stdout)


def _wpctl(
    arguments: list[str],
    *,
    runner: Callable[..., subprocess.CompletedProcess],
) -> None:
    try:
        result = runner(
            ["wpctl", *arguments],
            capture_output=True,
            text=True,
            timeout=3.0,
            check=False,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        raise PipeWireError(f"cannot run wpctl: {exc}") from exc
    if result.returncode != 0:
        message = result.stderr.strip() or f"exit status {result.returncode}"
        raise PipeWireError(f"wpctl {' '.join(arguments)} failed: {message}")


def set_stream_volume(
    node_id: int,
    volume: float,
    *,
    runner: Callable[..., subprocess.CompletedProcess] = subprocess.run,
) -> None:
    if not math.isfinite(volume) or not 0.0 <= volume <= 1.0:
        raise PipeWireError("stream volume must be finite and between 0 and 1")
    _wpctl(
        ["set-volume", str(node_id), f"{volume:.3f}"],
        runner=runner,
    )


def set_stream_mute(
    node_id: int,
    muted: bool,
    *,
    runner: Callable[..., subprocess.CompletedProcess] = subprocess.run,
) -> None:
    _wpctl(["set-mute", str(node_id), "1" if muted else "0"], runner=runner)
