import subprocess

import pytest

from deviation_station.ssh import SshError, SshTransport


def _result(command, returncode=0, stdout="", stderr=""):
    return subprocess.CompletedProcess(command, returncode, stdout, stderr)


def test_ssh_transport_quotes_remote_commands() -> None:
    calls: list[list[str]] = []

    def runner(command, **kwargs):
        calls.append(list(command))
        return _result(command, stdout="ok\n")

    transport = SshTransport("bench-pi", runner=runner)
    result = transport.run(["wpctl", "set-volume", "97", "0.5"])
    assert result.stdout == "ok\n"
    assert calls
    assert calls[0][-2] == "bench-pi"
    remote = calls[0][-1]
    assert "wpctl set-volume 97 0.5" in remote
    assert "XDG_RUNTIME_DIR" in remote
    assert "DBUS_SESSION_BUS_ADDRESS" in remote


def test_ssh_transport_reports_remote_failure() -> None:
    def runner(command, **kwargs):
        return _result(command, returncode=7, stderr="not allowed\n")

    transport = SshTransport("bench-pi", runner=runner)
    with pytest.raises(SshError, match="not allowed"):
        transport.run(["pw-dump"])


def test_ssh_transport_writes_quoted_remote_file() -> None:
    calls = []

    def runner(command, **kwargs):
        calls.append((list(command), kwargs.get("input")))
        return _result(command)

    transport = SshTransport("bench-pi", runner=runner)
    transport.write_text("/tmp/a b.conf", "hello")
    command, stdin = calls[-1]
    assert "a b.conf" in command[-1]
    assert stdin == "hello"


def test_ssh_transport_rejects_unsafe_hosts() -> None:
    with pytest.raises(SshError):
        SshTransport("")
    with pytest.raises(SshError):
        SshTransport("-oProxyCommand=bad")
    with pytest.raises(SshError):
        SshTransport("host name")
