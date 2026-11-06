# GNU Radio Deviation Meter

A GNU Radio-based FM deviation instrument for amateur-radio bench work and
sound-card modem calibration.

The application combines two measurement engines:

- **Discriminator-based measurement** for live readings from arbitrary FM
  waveforms, including MMDVM/DMR and Module17/M17 digital transmissions.
- **Bessel-null measurement** for accurate steady-tone measurements and
  closed-loop sound-card calibration.

The UI provides a large white-on-dark analog meter, digital deviation readout,
waterfall, shared receive-frequency controls, and separate **Measure** and
**Auto-Calibrate** tabs.

See [PROJECT_PLAN.md](PROJECT_PLAN.md) for the full design and implementation
roadmap and [CHANGELOG.md](CHANGELOG.md) for release notes.

## Current status

The measurement and internal-generator calibration foundation is implemented:

- Bessel-null calculations
- Sinusoidal FM and 4FSK simulators
- Discriminator estimators for robust peak and digital symbol-level deviation
- MMDVM/DMR and Module17/M17 profile definitions
- Common measurement/quality result model
- Raw SoapySDR CF32 IQ capture with sidecar metadata
- Nix flake, packaged Python environment, and hardware-free tests

- Offline CF32 analysis with LO-offset correction, channel filtering,
  resampling, activity detection, DMR burst handling, and JSON reports
- GNU Radio live flowgraph for SoapySDR input, offset translation, channel
  filtering, resampling, quadrature demodulation, and FFT
- M17 and DMR live measurement profiles
- Live Bessel carrier lock and first/second \(J_0\) null capture
- Optional application-generated calibration tone with live level control
- Guarded `rigctld` PTT with automatic unkey timeout
- Guided Auto-Calibrate sweep with coarse search, null refinement, hold, and
  final capture using the internal tone generator, a Direwolf steady tone, or
  xara TUNE
- Bounded PipeWire stream-volume control for the Direwolf calibration process
- SSH remote-Direwolf calibration using remote PipeWire volume control and a
  remote `rigctld` PTT endpoint
- xara command-port control for its 1500 Hz ALC test tone
- Discriminator target-deviation control using the Generic FM profile
- Hardware-free Bessel and target calibration rehearsals with the Simulator source
- Persistent Bessel verification and discriminator target records, with
  matching human-readable Markdown reports
- Fullscreen two-tab measurement UI with analog meter, digital readout, and
  waterfall
- Network `rigctld` frequency read/write from the shared bottom bar

The Auto-Calibrate tab supports the internal tone generator, Direwolf steady
mark/space tones (local or remote over SSH), and xara TUNE. VARA and
QTSoundModem adapters are not implemented yet.

## Development on NixOS

The project provides a Nix flake with GNU Radio's wrapped Python environment,
SoapySDR, PyQt6, Hamlib, and test tooling.

```bash
nix develop
pytest
nix run . -- doctor
nix run . -- demo --profile m17
```

Build the application package:

```bash
nix build
result/bin/deviation-meter demo
```

The executable provides the graphical live measurement interface, simulator
and demo modes, and raw IQ capture/offline analysis tools.

## Live measurement UI

Launch the desktop interface:

```bash
nix run . -- gui
```

The application starts in simulator mode so the display can be checked without
an SDR. Select **Live SDR** for hardware operation and use **SDR / Rig…** to
configure the SoapySDR device arguments, sample rate, LO offset, gain,
bandwidth, and `rigctld` address.

The live receive chain is a GNU Radio flowgraph. The SDR center is placed above
the signal by the configured LO offset, then GNU Radio performs frequency
translation, channel filtering/decimation, quadrature demodulation, and FFT.
Python consumes the channelized I/Q and demodulated frequency for profile
measurement and UI updates. The live source sample rate must be an integer
multiple of 48 kHz; 240 kHz is the default.

### GNOME Wayland title-bar buttons

GNOME/Mutter does not provide server-side decorations to native Wayland
clients. Qt therefore draws client-side decorations, and Qt's default
Adwaita-style title bar may omit minimize/maximize buttons even when the window
requests them. The application sets `QT_WAYLAND_DECORATION=bradient` before
creating the Qt application so those buttons are present. An explicit
`QT_WAYLAND_DECORATION` set by the user is respected.

For exact GNOME title-bar placement and server-side decoration behavior, launch
the app through XWayland instead:

```bash
QT_QPA_PLATFORM=xcb ~/.nix-profile/bin/deviation-meter gui
```

### Native file dialogs on Nix

The **Browse…** buttons use Qt's native file chooser. On a GTK desktop that
chooser is provided through Qt's GTK platform theme. A package started outside
a full desktop session may not inherit the desktop's GSettings schema search
path and can abort with:

```text
GLib-GIO-ERROR: No GSettings schemas are installed on the system
```

The packaged wrapper now sets:

- `GSETTINGS_SCHEMA_DIR` to the compiled `gsettings-desktop-schemas` and GTK
  schema directories
- `GSETTINGS_SCHEMAS_PATH` to the same paths for compatibility
- `XDG_DATA_DIRS` to the schema, icon-theme, and shared-MIME data directories

This keeps the native dialog enabled even when launched from a system service
or minimal environment. If running the Python package directly rather than
through `result/bin/deviation-meter` or `nix run`, set `GSETTINGS_SCHEMA_DIR`
to:

```text
${gsettings-desktop-schemas}/share/gsettings-schemas/gsettings-desktop-schemas-<version>/glib-2.0/schemas
:${gtk3}/share/gsettings-schemas/gtk+3-<version>/glib-2.0/schemas
```

Do not rely on `GSETTINGS_SCHEMAS_PATH` alone; GLib's native dialog reads
`GSETTINGS_SCHEMA_DIR`.

The receive-frequency field is always available in the bottom bar. Its fixed
width keeps the full MHz value visible at every responsive scale; the label is
shortened to **RX Freq**. With `rigctld` running, **Read from Rig** imports the
current frequency. Enabling **Link Rig Frequency** writes committed keyboard
entries through `rigctld` and uses the confirmed readback value.

The measure screen provides:

- White-on-dark analog deviation meter
- Large digital outer-symbol deviation readout
- Active-profile nominal deviation reference and amber meter marks
- Generic FM narrow/wide selection and automatic target deviation
- **Deviation Ref…** dialog with M17, DMR, analog FM, and Bessel references
- M17/DMR profile selection
- Live scrolling waterfall
- Per-window segment and DMR burst status
- Simulator and live SoapySDR sources
- Responsive scaling for 720p, 1080p, and arbitrary resized windows
- Bessel carrier-lock and automatic null capture

### Deviation references

The digital pane shows the nominal reference for the active profile, and the
analog meter marks that value in amber. The shared **Deviation Ref…** button in
the bottom bar opens the complete table:

| Profile | Nominal reference | Definition |
|---|---|---|
| Module17 / M17 | 2.400 kHz outer, 0.800 kHz inner | M17 4FSK, 4800 symbols/s |
| MMDVM / DMR | 1.944 kHz outer, 0.648 kHz inner | ETSI DMR 4FSK, 4800 symbols/s |
| Generic FM | ±2.5 kHz narrowband, ±5.0 kHz wideband | Typical analog FM speech peak |
| Bessel J0 | 2.4048 × tone (1st), 5.5201 × tone (2nd) | Absolute steady-tone null |

Digital readings and meter marks use the outer-symbol deviation. Generic FM is
a robust peak measurement; the narrowband/wideband values are common references,
not regulatory or pass/fail limits. Use a Bessel null to validate absolute
accuracy for a steady tone.

### Bessel-null measurement

1. Select **Bessel J0** mode.
2. Set the modulation-tone frequency and choose the first or second \(J_0\) null.
3. Start the receiver with modulation disabled.
4. Press **Relock Carrier** with the unmodulated carrier present.
5. Increase modulation slowly while watching the carrier disappear.
6. The detector captures the selected null automatically after the carrier
   recovers. **Capture** is available as a manual fallback.

The simulator includes a modulation-index sweep for exercising the detector
without RF hardware. The Bessel reading is calculated from the tone frequency
and selected null; no SDR amplitude calibration or correction factor is used.
The live Bessel status line below the tone/null controls shows the selected
null, tone, expected deviation, carrier offset, null depth, and acquisition
state.

When using the built-in tone with live SDR and PTT, adjust the **Level** control
while the tone and PTT are active. A 2nd J0 null requires about 2.3 times the
drive that produces the 1st null. If 100% still does not reach the null, raise
the radio's data-input level or use the Auto-Calibrate Bessel search, which
sweeps the level automatically.

Unkeying PTT stops the built-in tone and resets any pending—but not yet
recovered—Bessel acquisition. This prevents a later carrier return from being
mistaken for recovery from an old null. A result that was already captured
before PTT off is preserved.

The Bessel controls can play the selected tone through the audio output chosen
under **SDR / Rig…**, and key a compatible transmitter through network
`rigctld`. PTT is explicitly opt-in, has a configurable maximum transmit time,
and is released on stop, mode changes, safety timeout, and normal application
exit. The transmitter must still be operated legally and with appropriate RF
attenuation or a dummy load.

### Auto-Calibrate

The **Auto-Calibrate** tab supports two methods:

- **Bessel null** — finds and captures a selected \(J_0\) carrier null.
- **Target deviation** — adjusts the audio level until the Generic FM
  discriminator reading reaches a target and tolerance. The target follows the
  selected mode automatically: M17 2.400 kHz, DMR 1.944 kHz, FM narrow
  2.500 kHz, or FM wide 5.000 kHz. It remains editable for custom targets.

For either method:

1. Select **Live SDR** and set the shared receive frequency.
2. Put the transmitter in **FM** on that frequency. If **Link Rig Frequency**
   is enabled, the application writes and confirms the frequency; otherwise it
   requires the `rigctld` frequency to match within 1 kHz.
3. Choose the generator, method, target/null settings, and bounded level range.
4. Press **Start Auto-Cal**.

The Bessel controller keys PTT through `rigctld`, waits for an unmodulated
carrier, runs a coarse level sweep, refines around the selected null, recovers
the carrier, and holds the refined level for a final capture. It unkeys on
completion, cancellation, measurement stop, application exit, or the PTT
safety timeout.

The target controller starts at 50% audio level, measures the live Generic FM
discriminator reading, searches upward or downward to bracket the target,
interpolates, and refines until the requested tolerance is met. It saves a
`discriminator-target-calibration` record. Discriminator target readings are
relative measurements; verify absolute accuracy against a Bessel null.

With the **Simulator** source, Start Auto-Cal runs a deterministic
hardware-free rehearsal of either method. Rehearsal uses a synthetic carrier
null or discriminator plant, exercises the same controller and UI states, and
does not key a transmitter or allow result saving. Use **Live SDR** for an
actual calibration and record.

For **Direwolf steady tone**, select a Direwolf configuration and choose the
mark or space tone. The adapter reads only the `ADEVICE`, `ARATE`,
`ACHANNELS`, and `MODEM` settings, then generates a temporary calibration
configuration. It never edits the normal Direwolf configuration. The
generated configuration uses:

- a mono AFSK modem with explicit mark/space tones,
- `PTT RIG 2` against the configured `rigctld`,
- no AGW/KISS network servers.

Direwolf itself keys PTT and runs in `-x` steady-tone mode. The application
discovers that process's own PipeWire playback stream and controls only its
volume. Alternating mark/space tones and packet traffic are not used for
Bessel measurements.

Direwolf requires exclusive access to the audio device. Stop any normal
Direwolf instance before starting a calibration run. The adapter refuses to
start if another Direwolf process is present, and it restores PTT off when the
calibration process is stopped.

#### Remote Direwolf over SSH

The local application can run Direwolf on a remote host while keeping the SDR
and receiver local. This uses only SSH control and remote audio-volume
commands; it does **not** carry SDR IQ over the network. The receiver still has
to hear the remote transmitter over RF.

On the remote host, install Direwolf, PipeWire, `pw-dump`, `wpctl`, `timeout`,
and `pgrep`. Configure key-based SSH access and confirm it works
non-interactively:

```bash
ssh -o BatchMode=yes remote-host true
```

In **SDR / Rig…**:

1. Enable **Run Direwolf on a remote host via SSH**.
2. Enter the SSH host or `~/.ssh/config` alias.
3. Enter the remote `rigctld` **port**.

The remote `rigctld` host is derived from the SSH host. It must be reachable
from both this machine and the remote host; do not use a loopback SSH target
unless both ends are the same machine. The local `rigctld` fields continue to
control the manual tone/PTT path and local xara calibration.

Select a Direwolf configuration file in Auto-Calibrate as usual. In remote
mode that file is a **local template**: the application reads its `ADEVICE`,
`ARATE`, `ACHANNELS`, and `MODEM` settings, then uploads a generated temporary
configuration to the remote host. The `ADEVICE` name must exist on the remote
host. Stop any normal remote Direwolf instance before starting.

The remote adapter:

- starts `direwolf -x` in a private remote temporary directory,
- discovers the remote Direwolf PipeWire playback stream and controls only its
  volume,
- uses remote `rigctld` for Direwolf PTT and verifies PTT state over the network,
- sets a remote `timeout` watchdog that includes the PTT safety timeout plus
  the stream-startup margin, so the remote transmitter unkeys even if the SSH
  connection or local application disappears,
- removes the temporary remote files and confirms PTT off on completion.

The application queries the remote rig frequency/mode and uses it for the
preflight check when the remote Direwolf generator is selected. While that
generator is selected, **Read from Rig** and **Link Rig Frequency** also use
the remote rig endpoint; the local rig fields remain in use for manual and
local xara calibration. Remote calibration requires the same FM-mode and
frequency-match rules as local calibration.

If startup fails, check non-interactive SSH, the remote `PATH` for
`direwolf`/`pw-dump`/`wpctl`, the remote `XDG_RUNTIME_DIR`/PipeWire session, and
whether another remote Direwolf process is still running. The remote fields
have tooltips and the **Remote requirements…** button in SDR / Rig repeats the
full checklist.

The SSH transport and remote process lifecycle are unit-tested with a simulated
remote host. A real remote-site test still needs to confirm RF reception, remote
audio routing, PTT readback, and the remote timeout watchdog with the actual
station.

For **xara TUNE**, start xara separately with radio mode and `rigctld` PTT,
for example:

```bash
xara --call YOURCALL --fm wide --radio \
  --ptt rigctld:127.0.0.1:4532 \
  --capture <pipewire-source> --playback <pipewire-sink> \
  --port 8300 --kiss-port off
```

Enter the xara host and **VARA cmd port** in the Auto-Calibrate tab. This is
xara's `--port` VARA-compatible command/API port (normally 8300), not its web
UI `--ui-port`. The application connects to that command port, verifies the
xara VERSION response, and uses `TUNE -30..0 dB` for level control. The 3–100%
level range maps onto that drive range. `TUNE OFF` is sent at completion, and
xara also stops tuning when the command connection closes.

The xara row's **Test** button connects and reads VERSION/TUNE state and rigctld
PTT readback without starting TUNE or transmitting.

xara permits one command client at a time. Close other command-port clients
before calibration. The xara TUNE tone is 1500 Hz and is not full-scale, so the
radio’s data-input level may need to be raised to obtain a deep Bessel null.

#### Remote xara on the BBS

The xara TUNE source can run on the BBS while the SDR remains local. In
Auto-Calibrate, select xara and choose a transport:

- **Direct TCP** — enter the BBS host and command port. xara must be started
  with `--addr` set to an address reachable from this machine.
- **SSH tunnel** — enter the BBS SSH host (and optional SSH user) in the xara
  row and leave xara bound to its default `127.0.0.1:8300`; the application
  forwards the VARA command port through that host. This is the safer choice
  because the xara command port is unauthenticated.

Remote xara derives the rigctld host from the xara/SSH host. Use the
**Rigctld port** field in the xara row to set the remote rigctld port. xara
itself must use the same rigctld for PTT, for example:

```bash
xara --call YOURCALL --fm wide --radio \
  --ptt rigctld:XARA_HOST:4532 \
  --capture <pipewire-source> --playback <pipewire-sink> \
  --port 8300 --kiss-port off
```

The local SDR must hear the BBS transmitter. Only xara command traffic crosses
SSH; no SDR IQ is transported. The application verifies xara VERSION/PTT,
controls `TUNE -30..0 dB`, and sends `TUNE OFF` at completion.

### VARA and QtSoundModem — manual-assisted

VARA’s TUNE button and drive slider are GUI controls. Its documented TCP
command port carries session/data commands, not a scriptable steady-tone
calibration command. VARA AutoTune pings and ordinary traffic are not valid
Bessel tones. QtSoundModem is likewise a GUI application without a stable
headless test-tone interface.

Use the **Measure** tab for these modems:

1. Start the modem’s steady TUNE/test tone, initially at low drive.
2. Select **Bessel J0**, set the actual tone frequency, and start the live SDR.
3. Use **Relock Carrier** before modulation, or lock at the low-drive level.
4. Adjust the modem drive until the selected carrier null appears.
5. Use **Capture** and **Save Result** to record the verification.

Do not treat a VARA AutoTune ping, a packet burst, or alternating modem tones
as a steady Bessel source.

The run completes only after a real null capture. A shallow or missing null is
reported as a failure rather than converted into a measurement. The resulting
record identifies the selected null, tone frequency, measured null depth, and
the refined audio level.

Press **Save Result** after a null or target is captured to write a JSON record
and a matching human-readable Markdown report under:

```text
$XDG_DATA_HOME/gnuradio-deviation-meter/calibrations
```

or `~/.local/share/gnuradio-deviation-meter/calibrations` when `XDG_DATA_HOME`
is unset.

## Recording IQ fixtures

List connected SoapySDR devices:

```bash
nix run . -- capture devices --device-args "driver=rtlsdr"
```

Record raw complex-float32 IQ:

```bash
nix run . -- capture iq \
  -o captures/m17-handheld.cf32 \
  --device-args "driver=rtlsdr" \
  --sample-rate 1.024M \
  --center-frequency 145.600M \
  --signal-frequency 145.500M \
  --bandwidth 250k \
  --gain-db 20 \
  --duration 5 \
  --mode M17 \
  --expected-deviation 2400 \
  --transmitter "radio model" \
  --power low \
  --attenuation-db 60
```

The SDR center should normally be offset from the transmitter by roughly
100 kHz to avoid the RTL-SDR DC spike. The corresponding JSON sidecar records
the requested and actual device settings, sample count, clipping, overflow,
and user-provided fixture metadata:

```text
captures/m17-handheld.cf32
captures/m17-handheld.cf32.json
```

The `.cf32` file has no header and contains interleaved little-endian
complex64 I/Q samples:

```python
import numpy as np
iq = np.fromfile("captures/m17-handheld.cf32", dtype="<c8")
```

No PTT or transmitter control is performed by the capture command.

## Analyzing a capture

Analyze an M17 capture:

```bash
nix run . -- analyze captures/m17-handheld.cf32 \
  --profile m17 \
  --report captures/m17-handheld.report.json
```

Analyze DMR:

```bash
nix run . -- analyze captures/dmr-handheld.cf32 \
  --profile dmr \
  --report captures/dmr-handheld.report.json
```

The analyzer:

- Reads the CF32 data and validates the sidecar metadata.
- Removes the recorded LO offset.
- Resamples to 48 kHz and applies a channel filter.
- Detects active transmissions and DMR TDMA bursts.
- Measures symbol levels and outer-symbol deviation.
- Writes active-segment quality and measurement details to the JSON report.

Absolute accuracy has not been independently calibrated yet. The JSON result
reports estimated deviation, symbol levels, cluster quality, timing, SNR, and
any capture warnings.

## Python package layout

```text
src/deviation_meter/
  analysis.py       Offline channelization and capture analysis
  audio.py           Calibration-tone generation and live level control
  auto_cal.py        Guarded Bessel-null search controller
  bessel.py         Bessel-null constants and calculations
  capture.py        SoapySDR raw IQ capture and metadata
  direwolf.py       Temporary Direwolf calibration-tone process
  discriminator.py  Instantaneous-frequency measurements
  pipewire.py       PipeWire stream discovery and bounded volume control
  remote_direwolf.py SSH-controlled remote Direwolf calibration source
  ssh.py             Quoted SSH command transport with connection reuse
  gr_live.py        GNU Radio streaming receive and measurement flowgraph
  live.py           Shared live-measurement snapshot types
  measurement.py    Common reading and quality types
  profiles.py       Digital-mode measurement profiles
  rehearsal.py      Deterministic hardware-free calibration plants
  rigctl.py         Minimal network rigctld client
  simulator.py      Complex-baseband FM/4FSK signal generation
  target_cal.py     Discriminator target-deviation controller
  ui.py             PyQt6 measurement interface
  xara.py           xara command-port TUNE control
  cli.py            Command-line interface
tests/
```
