"""GNU Radio streaming receiver for the live measurement interface."""

from __future__ import annotations

import math
import threading
import time
from dataclasses import dataclass
from typing import Any

import numpy as np
from gnuradio import analog, blocks, fft, filter, gr, soapy
from scipy.signal import firwin

from .analysis import (
    AnalysisError,
    ChannelizedSignal,
    profile_for_name,
    measure_channelized_frequency,
    summarize_segments,
)
from .live import LiveSnapshot
from .simulator import fsk4_iq
from .tone import ToneEstimate, detect_tone_hz

FFT_SIZE = 1_024
SPECTRUM_UPDATE_VECTORS = 5


class GnuRadioLiveError(RuntimeError):
    """Raised when the GNU Radio live receiver cannot start."""


@dataclass(frozen=True, slots=True)
class GnuRadioLiveConfig:
    profile_name: str
    simulator: bool
    device_args: str
    sample_rate_hz: float
    center_frequency_hz: float
    signal_frequency_hz: float
    gain_db: float
    bandwidth_hz: float
    target_sample_rate_hz: float = 48_000.0
    window_s: float = 0.6
    hop_s: float = 0.3
    mode: str = "digital"
    bessel_tone_hz: float = 1_000.0


class LiveState:
    """Thread-safe latest-result holder shared by GR blocks and the UI."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._spectrum = tuple([-120.0] * FFT_SIZE)
        self._spectrum_sequence = 0
        self._snapshot: LiveSnapshot | None = None
        self._error: str | None = None
        self._audio = np.empty(0, dtype=np.float64)

    def set_spectrum(self, values: tuple[float, ...]) -> None:
        with self._lock:
            self._spectrum = values
            self._spectrum_sequence += 1

    def set_audio(self, samples) -> None:
        with self._lock:
            self._audio = np.asarray(samples, dtype=np.float64).copy()

    def audio(self) -> np.ndarray:
        with self._lock:
            return self._audio.copy()

    def spectrum(self) -> tuple[float, ...]:
        with self._lock:
            return self._spectrum

    def spectrum_snapshot(self) -> tuple[int, tuple[float, ...]]:
        with self._lock:
            return self._spectrum_sequence, self._spectrum

    def publish(self, snapshot: LiveSnapshot) -> None:
        with self._lock:
            self._snapshot = snapshot

    def latest_snapshot(self) -> LiveSnapshot | None:
        with self._lock:
            return self._snapshot

    def set_error(self, message: str) -> None:
        with self._lock:
            self._error = message

    def take_error(self) -> str | None:
        with self._lock:
            error = self._error
            self._error = None
            return error


class SpectrumSink(gr.sync_block):
    """Consume shifted FFT vectors and publish dB spectra."""

    def __init__(self, state: LiveState) -> None:
        gr.sync_block.__init__(
            self,
            name="deviation_meter_spectrum_sink",
            in_sig=[(np.complex64, FFT_SIZE)],
            out_sig=None,
        )
        self._state = state
        self._vectors = 0

    def work(self, input_items, output_items):
        del output_items
        self._vectors += len(input_items[0])
        if self._vectors >= SPECTRUM_UPDATE_VECTORS:
            self._vectors = 0
            values = np.asarray(input_items[0][-1], dtype=np.complex128)
            magnitude = np.abs(values) / FFT_SIZE
            db = 20.0 * np.log10(np.maximum(magnitude, 1e-9))
            self._state.set_spectrum(
                tuple(float(v) for v in np.clip(db, -120.0, 0.0))
            )
        return len(input_items[0])


class MeasurementSink(gr.sync_block):
    """Consume channelized complex samples and GNU Radio-demodulated Hz."""

    def __init__(
        self,
        state: LiveState,
        profile_name: str,
        sample_rate_hz: float,
        *,
        window_s: float,
        hop_s: float,
    ) -> None:
        self._profile = profile_for_name(profile_name)
        self._sample_rate = float(sample_rate_hz)
        self._window_samples = max(1_024, int(round(window_s * self._sample_rate)))
        self._hop_samples = max(256, int(round(hop_s * self._sample_rate)))
        self._complex_buffer = np.empty(0, dtype=np.complex128)
        self._frequency_buffer = np.empty(0, dtype=np.float64)
        self._state = state
        gr.sync_block.__init__(
            self,
            name="deviation_meter_measurement_sink",
            in_sig=[np.complex64, np.float32],
            out_sig=None,
        )

    def work(self, input_items, output_items):
        del output_items
        try:
            count = min(len(input_items[0]), len(input_items[1]))
            if count < 1:
                return 0
            self._complex_buffer = np.concatenate(
                (
                    self._complex_buffer,
                    np.asarray(input_items[0][:count], dtype=np.complex128),
                )
            )
            self._frequency_buffer = np.concatenate(
                (
                    self._frequency_buffer,
                    np.asarray(input_items[1][:count], dtype=np.float64),
                )
            )
            while (
                self._complex_buffer.size >= self._window_samples
                and self._frequency_buffer.size >= self._window_samples
            ):
                complex_window = self._complex_buffer[: self._window_samples]
                frequency_window = self._frequency_buffer[: self._window_samples]
                analysis = measure_channelized_frequency(
                    self._profile,
                    complex_window,
                    frequency_window,
                    self._sample_rate,
                    assume_continuous_if_flat=True,
                )
                summary = summarize_segments(analysis.segments)
                reading = analysis.selected
                reporting = reading.deviation_hz if reading is not None else None
                if summary is not None and summary["valid_segments"] > 1:
                    reporting = float(summary["median_deviation_hz"])
                valid_segments = (
                    int(summary["valid_segments"]) if summary is not None else 0
                )
                snr_db = None
                if analysis.selected_index is not None:
                    snr_db = analysis.segments[analysis.selected_index].segment.snr_db
                elif analysis.segments:
                    snr_db = max(
                        segment.segment.snr_db for segment in analysis.segments
                    )
                self._state.publish(
                    LiveSnapshot(
                        timestamp_s=time.time(),
                        profile_id=self._profile.profile_id,
                        reading=reading,
                        reporting_deviation_hz=reporting,
                        segment_count=len(analysis.segments),
                        valid_segment_count=valid_segments,
                        summary=summary,
                        spectrum_db=self._state.spectrum(),
                        window_duration_s=(
                            self._window_samples / self._sample_rate
                        ),
                        snr_db=(
                            None
                            if snr_db is None or not math.isfinite(snr_db)
                            else float(snr_db)
                        ),
                    )
                )
                self._complex_buffer = self._complex_buffer[self._hop_samples :]
                self._frequency_buffer = self._frequency_buffer[self._hop_samples :]
            audio_size = min(
                self._frequency_buffer.size,
                int(round(self._sample_rate)),
            )
            if audio_size:
                self._state.set_audio(self._frequency_buffer[-audio_size:])
            return count
        except Exception as exc:  # pragma: no cover - exercised with bad inputs
            self._state.set_error(f"{exc.__class__.__name__}: {exc}")
            return count if "count" in locals() else 0


def _simulated_waveform(
    profile_name: str,
    sample_rate_hz: float,
    lo_offset_hz: float,
    *,
    mode: str,
    bessel_tone_hz: float,
) -> np.ndarray:
    if mode == "bessel":
        duration_s = 8.0
        hold_s = 1.5
        beta_start = 0.5
        beta_end = 7.0
        sample_count = int(round(duration_s * sample_rate_hz))
        t = np.arange(sample_count, dtype=np.float64) / sample_rate_hz
        sweep_t = np.clip((t - hold_s) / (duration_s - hold_s), 0.0, 1.0)
        beta = beta_start + (beta_end - beta_start) * sweep_t
        phase = (
            -2.0 * math.pi * lo_offset_hz * t
            + beta * np.sin(2.0 * math.pi * bessel_tone_hz * t)
        )
        signal = np.exp(1j * phase)
        rng = np.random.default_rng(23)
        sigma = math.sqrt(0.5 / (10.0 ** (45.0 / 10.0)))
        noise = sigma * (
            rng.standard_normal(sample_count)
            + 1j * rng.standard_normal(sample_count)
        )
        return (signal + noise).astype(np.complex64)

    if mode != "digital":
        raise GnuRadioLiveError(f"unknown simulator mode: {mode}")
    profile = profile_for_name(profile_name)
    duration_s = 2.0
    symbol_count = int(round(profile.symbol_rate_hz * duration_s))
    if sample_rate_hz / profile.symbol_rate_hz >= 4:
        return fsk4_iq(
            symbol_rate_hz=profile.symbol_rate_hz,
            outer_deviation_hz=profile.expected_outer_deviation_hz,
            sample_rate_hz=sample_rate_hz,
            symbol_count=symbol_count,
            pulse_shape="raised_cosine",
            frequency_offset_hz=-lo_offset_hz,
            snr_db=45.0,
            seed=17,
        )
    raise GnuRadioLiveError("simulator sample rate is too low for the selected profile")


class GnuRadioLiveSession:
    """Own and run the GNU Radio receive flowgraph."""

    def __init__(self, config: GnuRadioLiveConfig) -> None:
        if config.sample_rate_hz <= 0.0 or config.target_sample_rate_hz <= 0.0:
            raise GnuRadioLiveError("sample rates must be positive")
        self.config = config
        self.state = LiveState()
        self._top_block: gr.top_block | None = None
        self._block_refs: list[Any] = []
        self._running = False

    @property
    def running(self) -> bool:
        return self._running

    def start(self) -> None:
        if self._running:
            return
        config = self.config
        decimation_float = config.sample_rate_hz / config.target_sample_rate_hz
        decimation = int(round(decimation_float))
        if decimation < 1 or not math.isclose(
            decimation_float,
            decimation,
            rel_tol=0.0,
            abs_tol=1e-6,
        ):
            raise GnuRadioLiveError(
                "source sample rate must be an integer multiple of 48 kHz "
                "for the GNU Radio live chain"
            )
        if decimation * config.target_sample_rate_hz != config.sample_rate_hz:
            raise GnuRadioLiveError("invalid live decimation ratio")

        target_rate = config.target_sample_rate_hz
        if config.mode not in ("digital", "bessel"):
            raise GnuRadioLiveError(f"unknown live measurement mode: {config.mode}")
        if config.mode == "bessel" and (
            not math.isfinite(config.bessel_tone_hz) or config.bessel_tone_hz <= 0.0
        ):
            raise GnuRadioLiveError("Bessel tone frequency must be positive")
        offset_hz = config.center_frequency_hz - config.signal_frequency_hz
        if offset_hz <= 0.0 or offset_hz >= config.sample_rate_hz / 2.0:
            raise GnuRadioLiveError(
                "in this live chain the SDR center must be above the signal"
            )

        cutoff_hz = min(config.bandwidth_hz / 2.0, 0.45 * target_rate)
        taps = firwin(
            257,
            cutoff_hz,
            fs=config.sample_rate_hz,
            window="blackmanharris",
        ).astype(np.float32)

        try:
            top_block = gr.top_block("deviationstation-live", catch_exceptions=True)
            if config.simulator:
                waveform = _simulated_waveform(
                    config.profile_name,
                    config.sample_rate_hz,
                    offset_hz,
                    mode=config.mode,
                    bessel_tone_hz=config.bessel_tone_hz,
                )
                source: Any = blocks.vector_source_c(
                    waveform.tolist(),
                    True,
                    1,
                    [],
                )
                throttle = blocks.throttle(
                    gr.sizeof_gr_complex,
                    config.sample_rate_hz,
                    True,
                )
                top_block.connect(source, throttle)
                channel_source = throttle
                actual_rate = config.sample_rate_hz
            else:
                source = soapy.source(
                    config.device_args or "driver=rtlsdr",
                    "fc32",
                    1,
                    "",
                    "",
                    [""],
                    [""],
                )
                source.set_sample_rate(0, config.sample_rate_hz)
                source.set_frequency(0, config.center_frequency_hz)
                source.set_gain_mode(0, False)
                source.set_gain(0, config.gain_db)
                if config.bandwidth_hz > 0.0:
                    source.set_bandwidth(0, config.bandwidth_hz)
                actual_rate = float(source.get_sample_rate(0))
                if not math.isclose(
                    actual_rate,
                    config.sample_rate_hz,
                    rel_tol=0.0,
                    abs_tol=1.0,
                ):
                    raise GnuRadioLiveError(
                        f"SDR selected {actual_rate:.0f} Hz instead of "
                        f"{config.sample_rate_hz:.0f} Hz"
                    )
                channel_source = source

            actual_decimation = int(round(actual_rate / target_rate))
            if not math.isclose(
                actual_rate / target_rate,
                actual_decimation,
                rel_tol=0.0,
                abs_tol=1e-6,
            ):
                raise GnuRadioLiveError(
                    "actual SDR sample rate is not an integer multiple of 48 kHz"
                )

            xlate = filter.freq_xlating_fir_filter_ccc(
                actual_decimation,
                taps.tolist(),
                -offset_hz,
                actual_rate,
            )
            vector = blocks.stream_to_vector(gr.sizeof_gr_complex, FFT_SIZE)
            fft_block = fft.fft_vcc(
                FFT_SIZE,
                True,
                fft.window.blackmanharris(FFT_SIZE),
                True,
            )
            spectrum = SpectrumSink(self.state)
            block_refs: list[Any] = [
                source,
                channel_source,
                xlate,
                vector,
                fft_block,
                spectrum,
            ]

            top_block.connect(channel_source, xlate)
            top_block.connect((xlate, 0), (vector, 0))
            top_block.connect(vector, fft_block)
            top_block.connect(fft_block, spectrum)

            if config.mode == "digital":
                demod = analog.quadrature_demod_cf(target_rate / (2.0 * math.pi))
                measurement = MeasurementSink(
                    self.state,
                    config.profile_name,
                    target_rate,
                    window_s=config.window_s,
                    hop_s=config.hop_s,
                )
                top_block.connect((xlate, 0), (measurement, 0))
                top_block.connect(xlate, demod)
                top_block.connect((demod, 0), (measurement, 1))
                block_refs.extend([demod, measurement])
            # Python-defined blocks are not retained by the C++ flowgraph.
            # Keep every object alive for the life of the session.
            self._block_refs = block_refs
            self._top_block = top_block
            top_block.start()
            self._running = True
        except Exception as exc:
            if self._top_block is not None:
                try:
                    self._top_block.stop()
                    self._top_block.wait()
                except Exception:
                    pass
                self._top_block = None
            self._running = False
            if isinstance(exc, GnuRadioLiveError):
                raise
            raise GnuRadioLiveError(f"failed to start GNU Radio receiver: {exc}") from exc

    def estimate_tone(
        self,
        *,
        min_hz: float = 300.0,
        max_hz: float = 5_000.0,
    ) -> ToneEstimate:
        """Estimate the dominant modulation tone from live demodulated audio."""

        samples = self.state.audio()
        return detect_tone_hz(
            samples,
            self.config.target_sample_rate_hz,
            min_hz=min_hz,
            max_hz=max_hz,
        )

    def stop(self) -> None:
        top_block = self._top_block
        self._top_block = None
        self._running = False
        if top_block is not None:
            try:
                top_block.stop()
                top_block.wait()
            except Exception:
                pass
        self._block_refs = []
