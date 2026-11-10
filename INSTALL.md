# Installing DeviationStation without Nix

DeviationStation is a Python application with two hard system dependencies for
live measurements:

- GNU Radio, including its Python bindings
- SoapySDR, including a driver for the SDR in use

The desktop interface also requires Qt 6 / PyQt6. The simulator, demo, and
offline analysis paths work without an SDR, but GNU Radio is still required to
open the GUI because the live receive chain is a GNU Radio flowgraph.

The Python package itself is pure Python. GNU Radio and SoapySDR remain
system-provided components; they are not bundled into the wheel.

## Recommended: Linux with a system GNU Radio

Install GNU Radio, SoapySDR, Qt 6, and PipeWire/PulseAudio development/runtime
packages using your distribution. Package names vary; on Debian/Ubuntu the
relevant packages are generally `gnuradio`, the GNU Radio Python bindings,
`soapysdr-tools`, the SoapySDR Python bindings, `libsoapysdr-dev`, `libpulse-dev`,
and the Qt 6 runtime. On Fedora, use the GNU Radio, SoapySDR, and Qt 6 packages;
on Arch, use `gnuradio`, `soapysdr`, and `qt6-base`.

Create a virtual environment that can see the system GNU Radio installation:

```bash
git clone https://github.com/ben-kuhn/deviation-station.git
cd deviation-station
python3 -m venv --system-site-packages .venv
. .venv/bin/activate
python -m pip install --upgrade pip
python -m pip install ".[gui]"
```

Install the SoapySDR Python module if it is not already available:

```bash
python -m pip install SoapySDR
```

If your distribution’s SoapySDR Python bindings are installed system-wide, the
`--system-site-packages` venv makes them visible. Verify everything:

```bash
devstation doctor
devstation demo
devstation gui
```

`devstation` is the installed command. The legacy `deviation-meter` command is
provided as a compatibility alias.

## Conda or Radioconda

GNU Radio and SoapySDR are also available through Conda environments. Install
the radio stack first, then install DeviationStation into the same environment:

```bash
conda create -n deviation-station -c conda-forge gnuradio soapysdr pyqt
conda activate deviation-station
python -m pip install ".[gui]"
devstation doctor
```

## Build a wheel or source distribution

From a checkout:

```bash
python -m pip install build
python -m build
```

This produces a pure-Python wheel and source archive under `dist/`. Install the
wheel into the target environment:

```bash
python -m pip install dist/deviation_station-*.whl
```

The Python package name is `deviation-station`; the import package remains
`deviation_meter` for compatibility with existing calibration tooling and
records.

## Desktop entry

The repository includes a desktop entry:

```bash
install -Dm644 nix/deviationstation.desktop \
  ~/.local/share/applications/deviationstation.desktop
```

The entry runs `devstation gui`. If the executable is not on the desktop
session’s `PATH`, edit the `Exec=` line to use its absolute path.

## Distribution packaging notes

A distribution package should depend on:

- `gnuradio` with Python bindings
- `SoapySDR` with the desired hardware drivers
- Python 3.11 or newer
- `numpy` and `scipy`
- Qt 6 and PyQt6 for the GUI
- PipeWire or PulseAudio for modem/audio calibration

Install the Python sources into the distribution’s site-packages and expose
the `devstation` console entry point. The `deviation-meter` entry point is
intentional compatibility surface. No direct ALSA access is required by the
application’s normal modem-audio workflow.

## macOS and Windows

The simulator, demo, and offline analysis code are portable Python. Live
SoapySDR/GNU Radio operation is currently Linux-first and has not been validated
on macOS or Windows. Treat those platforms as unsupported for radio hardware
until a tested GNU Radio/SoapySDR installation path is documented.
