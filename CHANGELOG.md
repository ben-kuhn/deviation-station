# Changelog

## 0.1.0 — 2026-10-31

Initial release.

### Measurement

- GNU Radio live SoapySDR receive chain with offset tuning, filtering,
  decimation, quadrature demodulation, and FFT.
- Bessel-null measurements for the first and second \(J_0\) carrier nulls.
- Discriminator-based measurements for Module17/M17, MMDVM/DMR, and Generic FM.
- Shared quality model distinguishing GOOD, MARGINAL, UNKNOWN, and INVALID
  readings.
- Live analog meter, digital readout, waterfall, and responsive two-tab UI.

### Auto-Calibrate

- Internal-tone Bessel null seek with coarse search, refinement, carrier
  recovery, hold, and final capture.
- Internal-tone discriminator target-deviation control with bounded search,
  interpolation, tolerance checking, and target records.
- Direwolf steady-tone adapter using a generated temporary configuration and
  bounded PipeWire stream-volume control.
- xara `TUNE` adapter over its native command port.
- Deterministic hardware-free Bessel and target rehearsals using the Simulator
  source.
- Guarded network `rigctld` PTT, configurable safety timeout, automatic unkey,
  and cleanup on cancellation, source/mode changes, and exit.

### Records and tooling

- JSON calibration records with matching human-readable Markdown reports.
- SoapySDR CF32 IQ capture with metadata and clipping/overflow reporting.
- Offline CF32 analysis for M17 and DMR.
- Nix flake, packaged application, development shell, and hardware-free test
  suite.

### Fixed

- The packaged GUI now supplies GSettings schemas, GTK schemas, icon themes,
  and shared MIME data to the native file chooser. The wrapper sets
  `GSETTINGS_SCHEMA_DIR` (and `GSETTINGS_SCHEMAS_PATH` for compatibility)
  to the compiled schema directories and adds the desktop data directories
  to `XDG_DATA_DIRS`. This prevents an abort in the GTK dialog when launched
  from an environment without desktop schemas.

### Known limitations

- VARA and QtSoundModem remain manual-assisted; their GUI-only TUNE controls
  are not automated.
- C4FM/System Fusion has no dedicated profile yet.
- Discriminator target readings are relative; Bessel nulls remain the absolute
  reference.
- Practical null depth depends on the radio/audio input level. Shallow nulls
  are reported as MARGINAL or rejected rather than accepted silently.
- Direwolf calibration requires the normal Direwolf instance to release the
  audio device.
- xara TUNE is 1500 Hz and uses a 1 dB-quantized drive range, which can limit
  achievable Bessel null depth.
