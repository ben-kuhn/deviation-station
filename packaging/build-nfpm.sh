#!/usr/bin/env bash
# Build Debian and RPM packages from the pure-Python wheel.
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
BUILD_DIR="$(mktemp -d)"
trap 'rm -rf "$BUILD_DIR"' EXIT

VERSION="0000"
DEB_ARCH="amd64"
RPM_ARCH="x86_64"
OUTPUT_DIR="$ROOT/dist/packages"
WHEEL=""

usage() {
  cat <<'EOF'
Usage: packaging/build-nfpm.sh [options]

Options:
  --version VERSION      Package version (default: read pyproject.toml)
  --deb-arch ARCH        Debian architecture (default: amd64)
  --rpm-arch ARCH        RPM architecture (default: x86_64)
  --output-dir DIR       Output directory (default: dist/packages)
  --wheel PATH           Use an existing wheel instead of building one
  -h, --help             Show this help

The package contains the wheel plus /usr/bin/devstation, the desktop entry,
and documentation.
EOF
}

while (($#)); do
  case "$1" in
    --version) VERSION="$2"; shift 2 ;;
    --deb-arch) DEB_ARCH="$2"; shift 2 ;;
    --rpm-arch) RPM_ARCH="$2"; shift 2 ;;
    --output-dir) OUTPUT_DIR="$2"; shift 2 ;;
    --wheel) WHEEL="$2"; shift 2 ;;
    -h|--help) usage; exit 0 ;;
    *) echo "unknown option: $1" >&2; usage >&2; exit 2 ;;
  esac
done

if [[ "$VERSION" == "0000" ]]; then
  VERSION="$(python3 - "$ROOT/pyproject.toml" <<'PY'
import sys, tomllib
with open(sys.argv[1], "rb") as f:
    print(tomllib.load(f)["project"]["version"])
PY
)"
fi

NFPM_BIN="${NFPM_BIN:-nfpm}"
command -v "$NFPM_BIN" >/dev/null 2>&1 || {
  echo "nfpm not found; set NFPM_BIN or install nfpm" >&2
  exit 1
}

STAGE="$BUILD_DIR/root"
mkdir -p "$STAGE/usr/bin" \
  "$STAGE/usr/lib/deviation-station" \
  "$STAGE/usr/share/applications" \
  "$STAGE/usr/share/licenses/deviation-station" \
  "$STAGE/usr/share/doc/deviation-station"

if [[ -z "$WHEEL" ]]; then
  mkdir -p "$BUILD_DIR/wheel"
  python3 -m build --wheel --outdir "$BUILD_DIR/wheel"
  WHEEL="$(find "$BUILD_DIR/wheel" -maxdepth 1 -name '*.whl' -print -quit)"
fi
[[ -n "$WHEEL" && -f "$WHEEL" ]] || {
  echo "wheel not found" >&2
  exit 1
}

# Extract the pure-Python wheel without requiring pip in the target build.
python3 - "$WHEEL" "$STAGE/usr/lib/deviation-station" <<'PY'
import sys, zipfile
with zipfile.ZipFile(sys.argv[1]) as wheel:
    wheel.extractall(sys.argv[2])
PY

install -Dm755 "$ROOT/packaging/devstation" "$STAGE/usr/bin/devstation"
install -Dm644 "$ROOT/nix/deviationstation.desktop" \
  "$STAGE/usr/share/applications/deviationstation.desktop"
install -Dm644 "$ROOT/LICENSE" \
  "$STAGE/usr/share/licenses/deviation-station/LICENSE"
install -Dm644 "$ROOT/README.md" \
  "$STAGE/usr/share/doc/deviation-station/README.md"
install -Dm644 "$ROOT/INSTALL.md" \
  "$STAGE/usr/share/doc/deviation-station/INSTALL.md"

mkdir -p "$BUILD_DIR/config"
common_contents="  - src: $STAGE
    dst: /
    type: tree"

cat > "$BUILD_DIR/config/deb.yaml" <<EOF
name: deviation-station
arch: $DEB_ARCH
platform: linux
version: $VERSION
section: hamradio
priority: optional
maintainer: Ben Kuhn <ku0hn@ku0hn.radio>
vendor: DeviationStation
homepage: https://github.com/ben-kuhn/deviation-station
license: GPL-3.0-or-later
description: |
  A GNU Radio FM deviation instrument for bench work, repeaters, and
  sound-card modem calibration.
depends:
  - "python3 (>= 3.11)"
  - python3-numpy
  - python3-scipy
recommends:
  - gnuradio
  - python3-pyqt6
  - python3-soapysdr
contents:
$common_contents
EOF

cat > "$BUILD_DIR/config/rpm.yaml" <<EOF
name: deviation-station
arch: $RPM_ARCH
platform: linux
version: $VERSION
release: 1
section: hamradio
priority: optional
maintainer: Ben Kuhn <ku0hn@ku0hn.radio>
vendor: DeviationStation
homepage: https://github.com/ben-kuhn/deviation-station
license: GPL-3.0-or-later
description: |
  A GNU Radio FM deviation instrument for bench work, repeaters, and
  sound-card modem calibration.
depends:
  - "python3 >= 3.11"
  - python3-numpy
  - python3-scipy
recommends:
  - gnuradio
  - python3-pyqt6
  - python3-SoapySDR
contents:
$common_contents
EOF

mkdir -p "$OUTPUT_DIR"
DEB_TARGET="$OUTPUT_DIR/deviation-station_${VERSION}_${DEB_ARCH}.deb"
RPM_TARGET="$OUTPUT_DIR/deviation-station-${VERSION}-1.${RPM_ARCH}.rpm"

"$NFPM_BIN" package -f "$BUILD_DIR/config/deb.yaml" -p deb -t "$DEB_TARGET"
"$NFPM_BIN" package -f "$BUILD_DIR/config/rpm.yaml" -p rpm -t "$RPM_TARGET"

printf '%s\n%s\n' "$DEB_TARGET" "$RPM_TARGET"
