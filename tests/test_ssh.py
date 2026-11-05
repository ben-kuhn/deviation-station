import subprocess

import pytest

from deviation_meter.ssh import SshError, SshTransport, SshTunnel


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


def test_ssh_tunnel_builds_loopback_forward() -> None:
    tunnel = SshTunnel("bench-pi", 8300, local_port=18300)
    command = tunnel._ssh_command()
    assert "ExitOnForwardFailure=yes" in command
    assert "127.0.0.1:18300:127.0.0.1:8300" in command
    assert command[-1] == "bench-pi"


def test_ssh_tunnel_reports_early_exit() -> None:
    class FakeProcess:
        def poll(self):
            return 1

        def terminate(self):
            pass

        def wait(self, timeout=None):
            del timeout
            return 1

        def kill(self):
            pass

    tunnel = SshTunnel(
        "bench-pi",
        8300,
        local_port=18301,
        process_factory=lambda *args, **kwargs: FakeProcess(),
    )
    with pytest.raises(SshError, match="exited before becoming ready"):
        tunnel.start()
    assert tunnel.active is False


def test_ssh_tunnel_rejects_invalid_ports() -> None:
    with pytest.raises(SshError):
        SshTunnel("bench-pi", 0)
    with pytest.raises(SshError):
        SshTunnel("bench-pi", 8300, local_port=70000)
    with pytest.raises(SshError):
        SshTunnel("", 8300)
