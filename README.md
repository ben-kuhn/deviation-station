# DeviationStation

A GNU Radio FM deviation instrument for bench work, repeaters, and sound-card
modem calibration.

DeviationStation measures FM deviation in two ways:

- **Discriminator measurement** for live FM, M17, and DMR signals.
- **Bessel-null measurement** for accurate steady-tone calibration.

The desktop UI provides a large analog-style deviation meter, digital readout,
waterfall, and guided calibration.

## Install

### Nix

Run directly from the repository:

```bash
nix run github:ben-kuhn/deviation-station -- demo
nix run github:ben-kuhn/deviation-station -- gui
```

Or from a local checkout:

```bash
nix run . -- gui
nix build
result/bin/devstation gui
```

NixOS users can install `pkgs.deviation-station` from
[nix-ham-packages](https://github.com/ben-kuhn/nix-ham-packages).

### Non-Nix

See [INSTALL.md](INSTALL.md) for Python, Conda, wheel, and distribution
packaging instructions. GNU Radio, SoapySDR, and Qt 6 are system dependencies.

```bash
python3 -m venv --system-site-packages .venv
. .venv/bin/activate
python -m pip install ".[gui]"
devstation doctor
```

The installed command is `devstation`. The older `deviation-meter` command
remains available as a compatibility alias.

## Use

Check the radio stack:

```bash
devstation doctor
```

Run a hardware-free demonstration:

```bash
devstation demo --profile m17
```

Open the graphical instrument:

```bash
devstation gui
```

The UI starts in simulator mode. Select **Live SDR**, open **SDR / Rig…**, and
set the SoapySDR device arguments, sample rate, receive frequency, gain,
bandwidth, and optional `rigctld` endpoint.

### Finding and setting SoapySDR arguments

The **Device args** field is passed directly to SoapySDR. It selects the SDR
driver and, when needed, a specific device or serial number. The default is:

```text
driver=rtlsdr
```

List the devices SoapySDR can see:

```bash
devstation capture devices
```

Filter the list to one driver:

```bash
devstation capture devices --device-args "driver=rtlsdr"
```

`devstation doctor` checks the Python stack; `devstation capture devices` checks
whether SoapySDR can actually see hardware. More detail is available from
SoapySDR’s utilities:

```bash
SoapySDRUtil --info
SoapySDRUtil --find
SoapySDRUtil --probe="driver=rtlsdr"
```

Use the driver name and serial reported by `--find` in the device arguments.
Common examples:

| Hardware | Device arguments |
|---|---|
| RTL-SDR | `driver=rtlsdr` |
| Multiple RTL-SDRs | `driver=rtlsdr,serial=00000001` |
| Airspy | `driver=airspy` |
| Airspy HF+ | `driver=airspyhf` |
| HackRF | `driver=hackrf` |
| SDRplay | `driver=sdrplay` |
| LimeSDR | `driver=lime` |
| USRP / UHD | `driver=uhd` |
| PlutoSDR | `driver=plutosdr,uri=ip:192.168.2.1` |

Driver names depend on the SoapySDR modules installed on your system. If a
device is missing, check `SoapySDRUtil --info`, the USB connection, the
distribution’s SoapySDR module package, and the device permissions/udev rules.
On non-Nix systems, the Python SoapySDR bindings and the hardware driver modules
must both be installed.

For CLI capture, pass the same string with `--device-args`:

```bash
devstation capture iq \
  --device-args "driver=rtlsdr,serial=00000001" \
  --center-frequency 145.600M \
  --signal-frequency 145.500M \
  --sample-rate 240k \
  --duration 5 \
  -o captures/signal.cf32
```

The sample rate must be an integer multiple of 48 kHz. The live GUI defaults to
240 kHz; `capture iq` defaults to 1.024 MHz. Choose a bandwidth appropriate for
the signal and driver; a wider bandwidth usually costs CPU without improving an
FM deviation measurement.

For RTL-SDR receivers, offset the SDR center frequency from the signal by about
100 kHz to avoid the DC spike. In the GUI, set **LO offset** to `50 kHz` or more;
the application places the SDR center above the receive frequency. For CLI
capture, set `--center-frequency` about 100 kHz above `--signal-frequency`.

Gain is driver-specific. Start with a low fixed gain and increase it until the
waterfall shows a clean signal without clipping. If the device supports
automatic gain, it can be selected through the driver’s SoapySDR arguments or
the application’s gain control.

## Measurement modes

| Mode | Reference |
|---|---|
| Module17 / M17 | 2.400 kHz outer symbol |
| MMDVM / DMR | 1.944 kHz outer symbol |
| Generic FM | 2.5 kHz narrowband / 5.0 kHz wideband |
| Bessel J0 | 2.4048 × tone (first null), 5.5201 × tone (second null) |

Digital readings and meter marks use the outer-symbol deviation. Generic FM is
a robust peak measurement. Use a Bessel null to validate absolute accuracy for
a steady tone.

### Bessel-null measurement

1. Select **Bessel J0**.
2. Set the modulation-tone frequency and choose the first or second \(J_0\)
   null.
3. Start the receiver with modulation off.
4. Press **Relock Carrier** while the unmodulated carrier is present.
5. Increase modulation slowly until the carrier null appears.
6. The null is captured automatically after carrier recovery. **Capture** is a
   manual fallback.

The Bessel reading is calculated from the tone frequency and selected null; no
separate SDR amplitude calibration is required.

### Auto-Calibrate

The **Auto-Calibrate** tab can find a Bessel null or adjust an audio source to a
target deviation. Targets follow the selected mode automatically and remain
editable for custom values.

Supported tone/calibration sources:

- Internal tone generator
- Direwolf steady mark or space tone, locally or over SSH
- VARA/xara `TUNE` over the command port

The application uses `rigctld` for PTT where required, with a configurable
transmit timeout and automatic unkey on stop, cancel, error, and exit. Operate
the transmitter legally and use a dummy load or suitable attenuation.

Remote VARA/xara operation is supported over a direct network connection. The
application does not create SSH tunnels; run your own forwarding when the modem
is bound to loopback.

## Capture and analyze

List SoapySDR devices:

```bash
devstation capture devices --device-args "driver=rtlsdr"
```

Record raw IQ:

```bash
devstation capture iq \
  -o captures/m17.cf32 \
  --device-args "driver=rtlsdr" \
  --center-frequency 145.600M \
  --signal-frequency 145.500M \
  --sample-rate 1.024M \
  --duration 5
```

Analyze an M17 or DMR capture:

```bash
devstation analyze captures/m17.cf32 --profile m17 --report captures/m17.report.json
devstation analyze captures/dmr.cf32 --profile dmr --report captures/dmr.report.json
```

Capture files are headerless interleaved little-endian complex-float32 IQ. A
JSON sidecar records the device settings and capture quality.

## Calibration records

Saved Bessel and target-calibration results are written as JSON with a matching
Markdown report under:

```text
$XDG_DATA_HOME/gnuradio-deviation-meter/calibrations
```

or `~/.local/share/gnuradio-deviation-meter/calibrations` when
`XDG_DATA_HOME` is unset. The legacy directory name is retained so existing
records remain available.

## Development

```bash
nix develop
pytest
nix build
```

See [CHANGELOG.md](CHANGELOG.md) for release notes and
[INSTALL.md](INSTALL.md) for non-Nix installation.

## License

GPL-3.0-or-later. See [LICENSE](LICENSE).
