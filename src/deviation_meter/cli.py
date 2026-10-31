"""Command-line entry point for measurement, capture, and the live GUI."""

from __future__ import annotations

import argparse
import json
import math
import sys
import time
from collections.abc import Sequence
from pathlib import Path

from . import __version__
from .analysis import AnalysisError, analyze_capture, write_analysis_report
from .bessel import deviation_from_carrier_null
from .capture import (
    CaptureConfig,
    CaptureError,
    CaptureResult,
    SoapyIQSource,
    capture_iq,
    list_soapy_devices,
)
from .discriminator import quadrature_demod_frequency
from .profiles import (
    GENERIC_FM_PROFILE,
    MODULE17_M17_PROFILE,
    MMDVM_DMR_PROFILE,
)
from .simulator import fsk4_iq, sinusoidal_fm_iq


def _parse_frequency(text: str) -> float:
    """Parse a frequency such as 145.5M, 145.5MHz, or 145500000."""

    normalized = text.strip().lower().replace(" ", "")
    factors = (
        ("ghz", 1e9),
        ("mhz", 1e6),
        ("khz", 1e3),
        ("hz", 1.0),
        ("g", 1e9),
        ("m", 1e6),
        ("k", 1e3),
    )
    factor = 1.0
    for suffix, candidate in factors:
        if normalized.endswith(suffix):
            normalized = normalized[: -len(suffix)]
            factor = candidate
            break
    try:
        value = float(normalized) * factor
    except ValueError as exc:
        raise argparse.ArgumentTypeError(f"invalid frequency: {text}") from exc
    if not math.isfinite(value) or value <= 0.0:
        raise argparse.ArgumentTypeError("frequency must be finite and greater than zero")
    return value


def _parse_positive_float(text: str) -> float:
    try:
        value = float(text)
    except ValueError as exc:
        raise argparse.ArgumentTypeError(f"invalid number: {text}") from exc
    if not math.isfinite(value) or value <= 0.0:
        raise argparse.ArgumentTypeError("value must be finite and greater than zero")
    return value


def _add_capture_parser(subparsers: argparse._SubParsersAction) -> None:
    capture = subparsers.add_parser(
        "capture",
        help="capture or inspect SDR IQ data",
    )
    capture_subparsers = capture.add_subparsers(
        dest="capture_command",
        required=True,
    )

    devices = capture_subparsers.add_parser(
        "devices",
        help="list SoapySDR devices",
    )
    devices.add_argument(
        "--device-args",
        default="",
        help='optional SoapySDR filter/arguments, e.g. "driver=rtlsdr"',
    )

    iq = capture_subparsers.add_parser(
        "iq",
        help="record raw complex-float32 IQ to a .cf32 file",
    )
    iq.add_argument("-o", "--output", type=Path, required=True)
    iq.add_argument("--device-args", default="", help="SoapySDR device arguments")
    iq.add_argument(
        "--sample-rate",
        type=_parse_frequency,
        default=1_024_000.0,
        help="SDR sample rate (default: 1.024M)",
    )
    iq.add_argument(
        "--center-frequency",
        "--center",
        type=_parse_frequency,
        required=True,
        help="SDR center/LO frequency",
    )
    iq.add_argument(
        "--signal-frequency",
        type=_parse_frequency,
        help="expected transmitter frequency for LO-offset metadata",
    )
    iq.add_argument(
        "--duration",
        type=_parse_positive_float,
        default=5.0,
        help="capture duration in seconds (default: 5)",
    )
    iq.add_argument("--channel", type=int, default=0)
    iq.add_argument("--gain-db", type=float, help="manual gain in dB")
    iq.add_argument(
        "--bandwidth",
        type=_parse_frequency,
        help="SDR analog bandwidth in Hz (optional)",
    )
    iq.add_argument("--antenna", help="antenna name")
    iq.add_argument(
        "--agc",
        action="store_true",
        help="enable receiver AGC (normally leave disabled)",
    )
    iq.add_argument(
        "--chunk-samples",
        type=int,
        default=65_536,
        help="streaming buffer size in complex samples",
    )
    iq.add_argument(
        "--countdown",
        type=float,
        default=3.0,
        help="seconds before recording starts (default: 3)",
    )
    iq.add_argument(
        "--overwrite",
        action="store_true",
        help="replace an existing capture and metadata file",
    )
    iq.add_argument("--mode", help="fixture mode label, e.g. M17 or DMR")
    iq.add_argument(
        "--expected-deviation",
        type=_parse_frequency,
        help="expected peak/outer deviation in Hz, if known",
    )
    iq.add_argument("--transmitter", help="transmitter make/model")
    iq.add_argument("--power", help="transmitter power setting")
    iq.add_argument("--attenuation-db", type=float)
    iq.add_argument("--notes", default="")


def _add_analysis_parser(subparsers: argparse._SubParsersAction) -> None:
    analyze = subparsers.add_parser(
        "analyze",
        help="channelize and measure a recorded CF32 capture",
    )
    analyze.add_argument("capture", type=Path, help="path to the .cf32 capture")
    analyze.add_argument(
        "--profile",
        choices=("m17", "dmr"),
        required=True,
        help="digital modulation profile",
    )
    analyze.add_argument(
        "--target-sample-rate",
        type=_parse_frequency,
        default=48_000.0,
        help="analysis sample rate; must be an integer multiple of 4800 (default: 48k)",
    )
    analyze.add_argument(
        "--bandwidth",
        type=_parse_frequency,
        default=20_000.0,
        help="channel low-pass bandwidth (default: 20k)",
    )
    analyze.add_argument(
        "--signal-frequency",
        type=_parse_frequency,
        help="override the signal frequency stored in capture metadata",
    )
    analyze.add_argument(
        "--activity-threshold-db",
        type=float,
        default=10.0,
        help="activity threshold above the estimated noise floor (default: 10 dB)",
    )
    analyze.add_argument(
        "--report",
        type=Path,
        help="report path (default: <capture>.report.json)",
    )
    analyze.add_argument(
        "--verbose",
        action="store_true",
        help="list every detected burst/segment",
    )


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="deviation-meter",
        description=(
            "GNU Radio FM deviation meter with Bessel-null and "
            "discriminator-based measurement, live SoapySDR reception, and "
            "network rigctld control."
        ),
    )
    parser.add_argument("--version", action="version", version=__version__)
    subparsers = parser.add_subparsers(dest="command", required=True)

    demo = subparsers.add_parser(
        "demo",
        help="run hardware-free measurement demonstrations",
    )
    demo.add_argument(
        "--profile",
        choices=("m17", "dmr", "generic"),
        default="m17",
        help="digital/generic profile used for the discriminator demonstration",
    )
    demo.add_argument(
        "--snr-db",
        type=float,
        default=60.0,
        help="simulated signal-to-noise ratio (default: 60)",
    )
    demo.add_argument("--seed", type=int, default=17, help="simulation random seed")
    subparsers.add_parser(
        "doctor",
        help="report whether runtime Python and GNU Radio dependencies are available",
    )
    _add_capture_parser(subparsers)
    _add_analysis_parser(subparsers)
    subparsers.add_parser(
        "gui",
        help="open the live measurement interface",
    )
    return parser


def _doctor() -> int:
    checks = (
        ("numpy", "numpy"),
        ("gnuradio", "gnuradio"),
        ("gnuradio soapy", "gnuradio.soapy"),
        ("SoapySDR", "SoapySDR"),
        ("PyQt6", "PyQt6.QtWidgets"),
    )
    failed = False
    for label, module_name in checks:
        try:
            module = __import__(module_name, fromlist=["*"])
        except Exception as exc:
            print(f"{label}: unavailable ({exc.__class__.__name__}: {exc})")
            failed = True
            continue
        version = getattr(module, "__version__", None)
        if version is None and module_name == "gnuradio":
            version = getattr(module, "version", lambda: None)()
        print(f"{label}: available" + (f" ({version})" if version else ""))
    return 1 if failed else 0


def _bessel_demo() -> list[str]:
    tone_hz = 1000.0
    readings = []
    for null_index in (1, 2):
        deviation = deviation_from_carrier_null(tone_hz, null_index)
        readings.append(
            f"Bessel J0 null {null_index}: {deviation / 1000.0:.4f} kHz "
            f"at {tone_hz / 1000.0:.3f} kHz tone"
        )
    return readings


def _discriminator_demo(profile_name: str, snr_db: float, seed: int) -> tuple[list[str], float | None]:
    if profile_name == "generic":
        sample_rate_hz = 192_000.0
        iq = sinusoidal_fm_iq(
            tone_frequency_hz=1000.0,
            deviation_hz=2400.0,
            sample_rate_hz=sample_rate_hz,
            duration_s=0.25,
            snr_db=snr_db,
            seed=seed,
        )
        frequency = quadrature_demod_frequency(iq, sample_rate_hz)
        reading = GENERIC_FM_PROFILE.measure(frequency, sample_rate_hz)
        lines = [
            f"Simulated sinusoidal FM: 2.400 kHz peak deviation",
            f"Discriminator robust-peak reading: "
            f"{reading.deviation_hz / 1000.0:.4f} kHz ({reading.quality.status.value})",
        ]
        return lines, reading.deviation_hz

    if profile_name == "dmr":
        profile = MMDVM_DMR_PROFILE
        expected_deviation_hz = profile.expected_outer_deviation_hz
        label = "DMR"
    elif profile_name == "m17":
        profile = MODULE17_M17_PROFILE
        expected_deviation_hz = profile.expected_outer_deviation_hz
        label = "M17"
    else:
        raise ValueError(f"unsupported profile: {profile_name}")

    sample_rate_hz = 192_000.0
    iq = fsk4_iq(
        symbol_rate_hz=profile.symbol_rate_hz,
        outer_deviation_hz=expected_deviation_hz,
        sample_rate_hz=sample_rate_hz,
        symbol_count=2000,
        pulse_shape="raised_cosine",
        snr_db=snr_db,
        seed=seed,
    )
    frequency = quadrature_demod_frequency(iq, sample_rate_hz)
    # Let the frequency estimate settle away from the simulated filter edges.
    reading = profile.measure(frequency[1000:-1000], sample_rate_hz)
    deviation_khz = (
        reading.deviation_hz / 1000.0 if reading.deviation_hz is not None else None
    )
    lines = [
        f"Simulated {label} 4FSK: {expected_deviation_hz / 1000.0:.4f} kHz outer deviation",
        f"{profile.label} reading: "
        f"{deviation_khz:.4f} kHz ({reading.quality.status.value})"
        if deviation_khz is not None
        else f"{profile.label} reading: invalid ({reading.quality.status.value})",
    ]
    return lines, reading.deviation_hz


def _capture_devices(device_args: str) -> int:
    try:
        devices = list_soapy_devices(device_args)
    except CaptureError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    if not devices:
        print("No SoapySDR devices found.")
        return 1
    for index, device in enumerate(devices):
        if index:
            print()
        print(json.dumps(device, indent=2, sort_keys=True))
    return 0


def _capture_user_metadata(args: argparse.Namespace) -> dict[str, object]:
    metadata: dict[str, object] = {}
    for key, value in (
        ("mode", args.mode),
        ("expected_deviation_hz", args.expected_deviation),
        ("transmitter", args.transmitter),
        ("power_setting", args.power),
        ("attenuation_db", args.attenuation_db),
        ("notes", args.notes),
    ):
        if value not in (None, ""):
            metadata[key] = value
    return metadata


def _print_capture_result(result: CaptureResult) -> None:
    state = "complete" if result.completed else "incomplete"
    print(f"Capture {state}: {result.output}")
    print(
        f"  {result.captured_samples:,} samples, "
        f"{result.duration_s:.3f} s at {result.sample_rate_hz:,.0f} Hz"
    )
    print(
        f"  peak={result.peak_magnitude:.3f}, "
        f"RMS={result.rms_magnitude:.4f}, "
        f"clipped={100.0 * result.clipped_fraction:.4f}%"
    )
    if result.overflow_count:
        print(f"  stream overflows: {result.overflow_count}", file=sys.stderr)
    if result.read_timeouts:
        print(f"  read timeouts: {result.read_timeouts}", file=sys.stderr)
    if result.peak_magnitude >= 1.0 or result.clipped_fraction > 0.001:
        print(
            "  warning: capture may be clipped; reduce SDR gain or increase attenuation",
            file=sys.stderr,
        )
    print(f"  metadata: {result.metadata_path}")
    if result.cancelled:
        print("  capture interrupted; partial file and metadata were retained")
    if result.error:
        print(f"  capture error: {result.error}", file=sys.stderr)


def _capture_iq(args: argparse.Namespace) -> int:
    if args.countdown < 0.0:
        print("error: --countdown cannot be negative", file=sys.stderr)
        return 2
    try:
        config = CaptureConfig(
            output=args.output,
            sample_rate_hz=args.sample_rate,
            center_frequency_hz=args.center_frequency,
            duration_s=args.duration,
            device_args=args.device_args,
            channel=args.channel,
            gain_db=args.gain_db,
            bandwidth_hz=args.bandwidth,
            antenna=args.antenna,
            agc=args.agc,
            signal_frequency_hz=args.signal_frequency,
            chunk_samples=args.chunk_samples,
            overwrite=args.overwrite,
            metadata=_capture_user_metadata(args),
        )
    except CaptureError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2

    print(
        f"Center {config.center_frequency_hz / 1e6:.6f} MHz, "
        f"sample rate {config.sample_rate_hz / 1e6:.3f} MS/s, "
        f"duration {config.duration_s:.1f} s"
    )
    if config.signal_frequency_hz is not None:
        print(
            f"Signal {config.signal_frequency_hz / 1e6:.6f} MHz, "
            f"LO offset {(config.center_frequency_hz - config.signal_frequency_hz) / 1e3:.3f} kHz"
        )

    remaining = args.countdown
    try:
        while remaining > 0.0:
            print(
                f"\rStarting capture in {remaining:0.1f} s... ",
                end="",
                file=sys.stderr,
                flush=True,
            )
            interval = min(0.25, remaining)
            time.sleep(interval)
            remaining -= interval
        if args.countdown > 0.0:
            print(file=sys.stderr)
    except KeyboardInterrupt:
        print("\nCapture cancelled before it started.", file=sys.stderr)
        return 130

    print("Opening SDR...", file=sys.stderr)
    try:
        source = SoapyIQSource(config.device_config())
    except CaptureError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1

    if not math.isclose(
        source.sample_rate_hz,
        config.sample_rate_hz,
        rel_tol=1e-3,
        abs_tol=1.0,
    ):
        print(
            f"warning: device selected {source.sample_rate_hz:,.0f} Hz instead of "
            f"the requested {config.sample_rate_hz:,.0f} Hz",
            file=sys.stderr,
        )

    last_progress = 0.0

    def progress(captured: int, target: int, elapsed: float) -> None:
        nonlocal last_progress
        if elapsed - last_progress < 0.5 and captured < target:
            return
        last_progress = elapsed
        print(
            f"\rRecording {captured:,}/{target:,} samples ({elapsed:0.1f} s)... ",
            end="",
            file=sys.stderr,
            flush=True,
        )

    print("Recording. Start the transmitter when ready.", file=sys.stderr)
    try:
        result = capture_iq(config, source, progress=progress)
    except CaptureError as exc:
        print(f"\nerror: {exc}", file=sys.stderr)
        print(
            f"Partial metadata, if any, was written next to {config.output}",
            file=sys.stderr,
        )
        return 1
    finally:
        print(file=sys.stderr)

    _print_capture_result(result)
    return 130 if result.cancelled else 0


def _analyze_capture(args: argparse.Namespace) -> int:
    try:
        result = analyze_capture(
            args.capture,
            profile_name=args.profile,
            target_sample_rate_hz=args.target_sample_rate,
            channel_bandwidth_hz=args.bandwidth,
            signal_frequency_hz=args.signal_frequency,
            activity_threshold_db=args.activity_threshold_db,
        )
    except AnalysisError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1

    report_path = args.report or args.capture.with_name(
        args.capture.name + ".report.json"
    )
    try:
        write_analysis_report(result, report_path)
    except OSError as exc:
        print(f"error: cannot write report: {exc}", file=sys.stderr)
        return 1

    print(f"Capture: {result.capture.path}")
    print(
        f"  {result.capture.sample_rate_hz:,.0f} Hz input, "
        f"{result.channel.sample_rate_hz:,.0f} Hz analysis, "
        f"shift {result.channel.shift_hz:+,.0f} Hz"
    )
    for warning in result.capture.warnings:
        print(f"  warning: {warning}", file=sys.stderr)

    if not result.segments:
        print("No active RF segment detected.", file=sys.stderr)
        print(f"Report: {report_path}")
        return 1

    print(f"Active segments: {len(result.segments)}")
    if args.verbose:
        for index, item in enumerate(result.segments):
            selected = " *" if result.selected_index == index else ""
            line = (
                f"  {index}: {item.segment.start_s:7.3f}-{item.segment.end_s:7.3f} s "
                f"({item.segment.duration_s:6.3f} s), "
                f"SNR {item.segment.snr_db:5.1f} dB{selected}"
            )
            print(line)
            if item.error:
                print(f"     {item.error}", file=sys.stderr)
    elif result.selected_index is not None:
        item = result.segments[result.selected_index]
        print(
            f"  selected: {item.segment.start_s:7.3f}-{item.segment.end_s:7.3f} s "
            f"({item.segment.duration_s:6.3f} s), "
            f"SNR {item.segment.snr_db:5.1f} dB"
        )

    reading = result.selected
    if reading is None or reading.deviation_hz is None:
        print("No valid digital deviation reading.", file=sys.stderr)
        print(f"Report: {report_path}")
        return 1

    summary = result.summary()
    levels = reading.metadata.get("symbol_levels_hz", ())
    level_text = ", ".join(f"{float(level):+.0f}" for level in levels)
    print(f"{result.profile.label} reading:")
    print(f"  outer-symbol deviation: {reading.deviation_hz:,.1f} Hz")
    if level_text:
        print(f"  symbol levels: {level_text} Hz")
    if summary is not None and summary["total_segments"] > 1:
        print(
            f"  burst statistics: {summary['valid_segments']}/"
            f"{summary['total_segments']} valid, median "
            f"{summary['median_deviation_hz']:,.1f} Hz, "
            f"range {summary['minimum_deviation_hz']:,.1f}-"
            f"{summary['maximum_deviation_hz']:,.1f} Hz, "
            f"sigma {summary['standard_deviation_hz']:,.1f} Hz"
        )
    print(f"  quality: {reading.quality.status.value}")
    if reading.quality.issues:
        for issue in reading.quality.issues:
            print(f"  issue: {issue}", file=sys.stderr)
    print(f"Report: {report_path}")
    return 0


def main(argv: Sequence[str] | None = None) -> int:
    parser = _build_parser()
    arguments = list(argv) if argv is not None else sys.argv[1:]
    if not arguments:
        # Keep a bare `nix run` useful by showing the hardware-free
        # measurement demonstration.
        arguments = ["demo"]
    args = parser.parse_args(arguments)

    if args.command == "demo":
        lines = _bessel_demo()
        discriminator_lines, _ = _discriminator_demo(
            args.profile,
            args.snr_db,
            args.seed,
        )
        lines.extend(discriminator_lines)
        for line in lines:
            print(line)
        return 0

    if args.command == "doctor":
        return _doctor()

    if args.command == "capture":
        if args.capture_command == "devices":
            return _capture_devices(args.device_args)
        if args.capture_command == "iq":
            return _capture_iq(args)
        parser.error(f"unknown capture command: {args.capture_command}")

    if args.command == "analyze":
        return _analyze_capture(args)

    if args.command == "gui":
        try:
            from .ui import run_gui
        except Exception as exc:
            print(f"error: cannot load GUI: {exc}", file=sys.stderr)
            return 1
        return run_gui()

    parser.error(f"unknown command: {args.command}")
    return 2
