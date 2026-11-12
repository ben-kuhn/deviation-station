import time

import pytest

from deviation_station.gr_live import (
    GnuRadioLiveConfig,
    GnuRadioLiveError,
    GnuRadioLiveSession,
)


def test_gnuradio_simulator_produces_live_snapshot() -> None:
    session = GnuRadioLiveSession(
        GnuRadioLiveConfig(
            profile_name="m17",
            simulator=True,
            device_args="",
            sample_rate_hz=240_000.0,
            center_frequency_hz=443_507_000.0,
            signal_frequency_hz=443_457_000.0,
            gain_db=0.0,
            bandwidth_hz=200_000.0,
        )
    )
    try:
        session.start()
        snapshot = None
        deadline = time.monotonic() + 5.0
        while time.monotonic() < deadline:
            time.sleep(0.1)
            error = session.state.take_error()
            assert error is None
            snapshot = session.state.latest_snapshot()
            if snapshot is not None and snapshot.reporting_deviation_hz is not None:
                break
        assert snapshot is not None
        assert snapshot.reporting_deviation_hz is not None
        assert snapshot.reporting_deviation_hz == pytest.approx(2400.0, rel=0.05)
        assert len(snapshot.spectrum_db) == 1024
    finally:
        session.stop()


def test_gnuradio_bessel_simulator_produces_spectrum() -> None:
    session = GnuRadioLiveSession(
        GnuRadioLiveConfig(
            profile_name="m17",
            simulator=True,
            device_args="",
            sample_rate_hz=240_000.0,
            center_frequency_hz=443_507_000.0,
            signal_frequency_hz=443_457_000.0,
            gain_db=0.0,
            bandwidth_hz=200_000.0,
            mode="bessel",
            bessel_tone_hz=1_000.0,
        )
    )
    try:
        session.start()
        deadline = time.monotonic() + 3.0
        sequence = 0
        spectrum = ()
        while time.monotonic() < deadline and sequence == 0:
            time.sleep(0.1)
            sequence, spectrum = session.state.spectrum_snapshot()
            assert session.state.take_error() is None
        assert sequence > 0
        assert len(spectrum) == 1024
        assert max(spectrum) > -40.0
    finally:
        session.stop()


def test_gnuradio_rejects_non_integer_source_rate() -> None:
    session = GnuRadioLiveSession(
        GnuRadioLiveConfig(
            profile_name="m17",
            simulator=True,
            device_args="",
            sample_rate_hz=250_000.0,
            center_frequency_hz=443_507_000.0,
            signal_frequency_hz=443_457_000.0,
            gain_db=0.0,
            bandwidth_hz=200_000.0,
        )
    )
    with pytest.raises(GnuRadioLiveError, match="integer multiple"):
        session.start()
