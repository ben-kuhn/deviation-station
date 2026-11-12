import pytest

from deviation_station.cli import _build_parser, _parse_frequency, main


def test_parser_uses_devstation_branding() -> None:
    parser = _build_parser()
    assert parser.prog == "devstation"
    assert "DeviationStation" in parser.description


def test_demo_runs_m17_measurement(capsys) -> None:
    exit_code = main(["demo", "--profile", "m17", "--snr-db", "60", "--seed", "1"])
    captured = capsys.readouterr()
    assert exit_code == 0
    assert "Bessel J0 null 1" in captured.out
    assert "Module17 / M17 4FSK reading" in captured.out


def test_no_arguments_runs_demo(capsys) -> None:
    exit_code = main([])
    captured = capsys.readouterr()
    assert exit_code == 0
    assert "Bessel J0 null 1" in captured.out


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("145500000", 145_500_000.0),
        ("145.5M", 145_500_000.0),
        ("145.5 MHz", 145_500_000.0),
        ("1024k", 1_024_000.0),
        ("1.024GHz", 1_024_000_000.0),
    ],
)
def test_frequency_argument_parsing(text: str, expected: float) -> None:
    assert _parse_frequency(text) == pytest.approx(expected)
