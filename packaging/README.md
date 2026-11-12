# Distribution packaging

DeviationStation is a pure-Python application. The distribution packages install
the wheel under `/usr/lib/deviation-station`, provide `/usr/bin/devstation` and
the legacy `/usr/bin/deviation-meter` alias, and install the desktop entry and
documentation.

GNU Radio, SoapySDR, and Qt 6 remain distribution-provided runtime
dependencies. The packages do not bundle Python, GNU Radio, or SDR drivers.

## Debian and RPM

Build both packages with nfpm:

```bash
python -m build --wheel
NFPM_BIN=/path/to/nfpm ./packaging/build-nfpm.sh \
  --version 0.1.0 \
  --deb-arch amd64 \
  --rpm-arch x86_64 \
  --output-dir dist/packages
```

The architecture values are separate because Debian and RPM use different
names:

| Target | Debian | RPM |
|---|---|---|
| 64-bit x86 | `amd64` | `x86_64` |
| 32-bit x86 | `i386` | `i686` |
| 64-bit ARM | `arm64` | `aarch64` |
| 32-bit ARM | `armhf` | `armv7hl` |

The GitHub workflow builds all four target pairs on an x86_64 runner. The
resulting packages are architecture-specific even though the Python payload is
the same.

The Debian package depends on Python, NumPy, and SciPy. GNU Radio, PyQt6, and
the SoapySDR Python bindings are recommendations so headless/demo installs stay
small. The RPM uses the same core dependencies and recommends GNU Radio,
`python3-pyqt6`, and `python3-SoapySDR`.

## Arch Linux

The native package is in [`arch/PKGBUILD`](arch/PKGBUILD). Build it with the
usual Arch tooling:

```bash
cd packaging/arch
makepkg -si
```

The PKGBUILD targets `x86_64`, `i686`, `aarch64`, and `armv7h`.

## CI

`.github/workflows/packages.yml`:

- builds Debian and RPM packages for all four architecture pairs;
- builds the Arch package in an Arch container;
- smoke-tests the x86_64 Debian/RPM packages on Debian, Ubuntu, Fedora, and
  openSUSE;
- publishes the packages as GitHub release assets for `v*` tags.
