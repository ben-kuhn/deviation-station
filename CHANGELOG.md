# Changelog

## Unreleased

### Auto-Calibrate

- Added remote Direwolf calibration over SSH. The local application starts a
  temporary remote `direwolf -x` process, controls only its remote PipeWire
  stream volume, uses a remote `rigctld` endpoint for PTT, and enforces a
  remote timeout watchdog in addition to the local PTT safety timer.
- Remote mode uses the selected Direwolf configuration as a local template;
  the remote host must provide the same `ADEVICE` plus Direwolf, PipeWire,
  `pw-dump`, and `wpctl`.
- Remote VARA/xara TUNE uses its command port directly. The VARA/xara host is
  used for its optional rigctld frequency endpoint; the rigctld port can be
  disabled and the transmitter frequency set manually. The built-in
  VARA/xara SSH tunnel was removed; users can run their own SSH port
  forwarding.
- Renamed the generator to **VARA/xara TUNE**. VARA/xara PTT is now owned by
  the modem: the application waits for command-port `PTT ON`/`PTT OFF`
  notifications instead of keying or verifying PTT through a separate
  rigctld. The external rigctld is used only for frequency/mode checks.
- TUNE drive is not a PipeWire volume. It is applied via the command port each
  run. xara persists a changed `drive-db` to its state settings on clean exit
  when a state directory exists; the NixOS `driveDb` option is only the initial
  default and can be overridden by that state file. The app never edits NixOS
  or xara state, and calibration records store the achieved `auto_best_level`
  and quantized `tune_drive_db`.
- Labels the port as the VARA command port (`--port`, not `--ui-port`). Added
  a non-transmitting **Test** action that checks VERSION, TUNE state, and
  modem PTT status.
- Added a configurable VARA/xara TUNE tone frequency, defaulting to xara's
  verified 1500 Hz. The actual tone matters for Bessel nulls; target deviation
  calculations are tone-independent.
- Remote rigctld setup now derives the host from the remote service host
  instead of requiring a separately configured host. The xara row exposes its
  own rigctld port; the shared remote port remains in SDR / Rig.

### Measurement UI

- Added an active-profile nominal deviation reference to the digital pane and
  amber reference marks on the analog meter.
- Moved the detailed live Bessel status (null, tone, expected deviation,
  carrier offset, depth, and state) to a dedicated line below the Bessel
  controls instead of the shared bottom status bar.
- Added a live built-in tone **Level** control to the manual Bessel screen, so
  the operator can sweep drive while PTT is active. The status line explains
  that an ARMED detector needs a level/drive change and displays the expected
  deviation for the selected J0 null.
- Unkeying PTT now stops the built-in tone and discards an incomplete Bessel
  acquisition, preventing a stale null from being captured when a carrier
  returns later. Already-captured results are preserved.
- Added a **Deviation Ref…** dialog covering M17, DMR, analog FM, and Bessel
  references.
- Target-calibration range failures now include the maximum deviation reached
  at maximum drive and a hint to raise TX audio or lower the target.
- Added Generic FM narrow/wide selection. Target deviation now follows the
  selected mode automatically: M17 2.400 kHz, DMR 1.944 kHz, FM narrow
  2.500 kHz, or FM wide 5.000 kHz; custom target entry remains available.
- Expanded remote-Direwolf field tooltips and added a **Remote requirements…**
  checklist covering SSH authentication, remote PipeWire, RF geometry,
  `ADEVICE`, remote `rigctld`, and PTT/watchdog behavior.

### Fixed

- Boolean application settings are parsed explicitly, so a stored `false`
  cannot be mistaken for an enabled option.
- The receive-frequency field now has a fixed width sized for the full
  `MHz` value at the largest responsive font scale, and its label is shortened
  to **RX Freq**. It no longer shrinks and scrolls to the least-significant
  digits.
- GNOME Wayland Qt applications now select Qt's `bradient` client-side
  decoration, which honors the requested minimize/maximize/close window flags.
  Qt's default Adwaita-style decoration could omit minimize and maximize
  buttons even though GNOME was configured to show them. An explicit
  `QT_WAYLAND_DECORATION` override is respected.

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
