# GNU Radio Deviation Meter — Project Plan

## 1. Project summary

Build a GNU Radio-based FM deviation instrument with two complementary measurement engines sharing a common SoapySDR receive chain:

1. **Discriminator-based estimator:** continuous/live deviation readings for arbitrary FM waveforms, with initial profiles targeted at **MMDVM/DMR** and **Module17/M17** signals. Digital-mode profiles will define how meaningful deviation is derived from instantaneous frequency rather than relying on a raw, noise-sensitive maximum.
2. **Bessel-null estimator:** accurate steady-tone test points for traditional bench work and closed-loop sound-card modem calibration.

The application will serve two related use cases:

- **Bench instrument:** measure radios, digital-voice modems, and other transmitters using a large analog-style meter, digital readout, and waterfall.
- **Sound-card modem calibrator:** measure transmit deviation and, where the modem and audio system permit, automatically adjust PulseAudio/PipeWire playback levels to achieve a supported deviation target.

The main window will have two tabs at the top:

- **Measure** — interactive readings and assisted bench operation.
- **Auto-Calibrate** — guarded, closed-loop calibration of sound-card modems.

The application is intended for nearly fullscreen use at 1280×720 or higher, with primary information readable from across a room. Optional tone generation and networked Hamlib `rigctld` integration will support bidirectional frequency control and PTT.

The initial receive-hardware interface will be **SoapySDR**. RTL-SDR devices will be accessed through their SoapySDR driver rather than through a direct RTL-SDR source block. Other SDRs will be supported through installed, compatible SoapySDR drivers and validated as hardware becomes available.

## 2. Goals and scope

### Initial goals

- Make MMDVM/DMR and Module17/M17 deviation measurement the primary digital-mode validation targets.
- Implement the discriminator-based and Bessel-null measurement engines as first-class components of the initial architecture.
- Provide live discriminator readings and clearly defined digital-mode metrics.
- Provide discrete Bessel-null readings for steady sinusoidal modulation.
- Use Python, GNU Radio 3.10 or newer, SoapySDR, and PyQt.
- Provide a software simulator for sinusoidal FM and representative digital/4FSK waveforms.
- Default the Bessel workflow to a 1 kHz tone, with configurable tone frequency and at least the first two carrier nulls.
- Provide application-generated audio and support external test sources.
- Use network `rigctld` as the only Hamlib/rig-control integration, including on-demand frequency reading, optional confirmed frequency writes, and optional PTT.
- Provide an always-visible receive-frequency field in a shared bottom bar, with **Read from Rig** and an opt-in rig-frequency link for compatible radios.
- Use a dark background with **white primary markings, labels, ticks, and numerals**.
- Clearly identify the engine, equipment/protocol profile, and measurement definition behind every reading.
- Automatically calibrate compatible sound-card modem levels with explicit operator start, limits, cancellation, and a results summary.
- Preserve manual-assisted workflows for equipment and modems without programmatic control.
- Provide a reproducible Nix flake/dev shell and package for the application.
- Add the application to the neighboring `../nix-ham-packages` overlay as an installable NixOS package.

### Initial limitations

- The first release will not provide laboratory-grade or traceable accuracy.
- Direct dependencies on device-specific RTL-SDR source blocks are excluded.
- Simultaneous multi-channel or multi-transmitter measurement is excluded.
- Unattended transmission without timeout, cancellation, and PTT safety interlocks is excluded.
- A generic discriminator peak reading will not be presented as protocol-correct digital-mode deviation until the relevant profile has been validated.
- Normal data bursts, alternating tones, and digital waveforms must not be treated as steady sinusoidal tones for Bessel-null calculations.
- MMDVM describes a hardware/software platform rather than one modulation format; its active mode must be selected before applying a digital-mode measurement profile.
- Repeater assembly, controller integration, and repeater commissioning are outside this project. The deviation meter is an upstream dependency for separate downstream repeater work and must remain usable without access to that equipment.

## 3. Measurement engines

### 3.1 Bessel-null engine

For a carrier frequency-modulated by a sinusoidal tone, define the modulation index as:

\[
\beta = \frac{\Delta f}{f_m}
\]

where:

- \(\Delta f\) is the peak frequency deviation.
- \(f_m\) is the modulation-tone frequency.
- \(\beta\) is the modulation index.

The RF carrier amplitude is proportional to \(J_0(\beta)\). At specific modulation indices, the carrier passes through a null. When a known null is reached:

\[
\Delta f = \beta_{\text{null}} f_m
\]

For a 1 kHz modulation tone, useful carrier-null points include:

| Carrier null | Modulation index \(\beta_{\text{null}}\) | Resulting deviation |
|---|---:|---:|
| First \(J_0\) null | 2.4048256 | 2.4048 kHz |
| Second \(J_0\) null | 5.5200781 | 5.5201 kHz |
| Third \(J_0\) null | 8.6537279 | 8.6537 kHz |

Initial presets will use the first null near 2.5 kHz narrowband-FM testing and the second null near 5 kHz amateur-FM testing.

When the application controls the tone generator, it can place a chosen null at a desired target:

\[
f_m = \frac{\Delta f_{\text{target}}}{\beta_{\text{null}}}
\]

The tone must remain within the audio passband of the complete transmit path. With a modem-supplied fixed tone, only the corresponding discrete null targets are available; the UI must not imply that an arbitrary target can be selected.

The Bessel engine produces valid readings at discrete nulls. Between nulls it displays acquisition state and holds the previous valid reading rather than presenting an uncalibrated continuous value.

### 3.2 Discriminator-based engine

The discriminator engine will convert complex I/Q to instantaneous frequency and derive deviation over a configurable observation window:

```text
Channelized I/Q
      |
Quadrature FM demodulator
      |
Instantaneous frequency in Hz
      |
Amplitude/SNR gate and center-frequency correction
      |
Profile-specific deviation estimator
      |
Live reading, average, and peak hold
```

The shared receive chain will provide adequate bandwidth and a suitably linear-phase channel filter so the measurement path does not unnecessarily distort the digital waveform.

Initial estimator types will include:

- **Robust peak deviation:** derive high and low instantaneous-frequency percentiles around an estimated center over a capture window. Avoid raw single-sample maxima, which are dominated by noise and demodulator clicks.
- **Symmetric peak deviation:** for appropriately symmetric waveforms, estimate deviation from the separation of high and low frequency extrema while rejecting residual carrier-offset error.
- **Digital symbol-level estimation:** for validated M-FSK/4FSK profiles, identify symbol-level clusters and report the protocol-relevant outer-symbol deviation and, where useful, level spacing. This can be more stable and meaningful than the largest instantaneous transition or pulse-shaping overshoot.

Each digital profile will define:

- Protocol/mode and expected waveform.
- Required channel bandwidth and sample rate.
- Capture duration and activity gating.
- Center-frequency estimation method.
- Percentile, clustering, or symbol-level algorithm.
- Reported deviation definition and target range.
- Averaging and peak-hold behavior.
- Validation fixtures and expected results.

Initial equipment/profile work will focus on:

- **MMDVM/DMR:** select the active digital mode as part of the profile; validate the relevant DMR 4FSK metric rather than applying one generic MMDVM target.
- **Module17/M17:** implement and validate an M17-specific 4FSK profile, including outer-symbol deviation and enough captured symbols for stable readings.

The UI will distinguish live discriminator measurements from held Bessel-null measurements. Discriminator mode can update continuously while a transmission is active, with configurable averaging and peak hold.

## 4. Operating modes

### 4.1 Measure tab: interactive bench instrument

The Measure tab will support:

- Explicit measurement-engine and equipment/profile selection.
- Live discriminator readings for MMDVM, Module17/M17, and generic FM.
- Bessel-null readings for steady sinusoidal test tones.
- Manual transmitter keying and external modulation.
- Application-generated sine-wave tones through a selected sound-card output.
- Optional bidirectional frequency synchronization and PTT through network `rigctld`.
- Manual level sweeps, automatic null capture, and assisted sweeps of the application's own tone generator.
- Saved profiles for MMDVM/DMR, Module17/M17, radios, and common test setups.

For compatible radios, **Read from Rig** will obtain the current frequency through `rigctld` and populate the same receive-frequency field used for manual entry. An opt-in **Link Rig Frequency** mode will write committed keyboard entries to the rig, read the value back for confirmation, and then apply the confirmed frequency to the SDR. Manual entry remains SDR-only by default and remains available for equipment without rig control.

A Digirig Lite-style bench profile will use its USB audio device for application-generated tones and `rigctld` for PTT where that interface can be configured appropriately. Manual/external PTT remains available; the application will not implement a second, direct serial-rig backend.

### 4.2 Auto-Calibrate tab: sound-card modem calibration

The Auto-Calibrate tab will provide:

- Modem/profile and measurement-engine selection.
- Receive-frequency and target-deviation confirmation.
- Sound-card sink and application-stream selection.
- Tone/null settings for Bessel calibration or a repeatable test waveform for discriminator calibration.
- `rigctld` or modem PTT ownership settings.
- Maximum audio level, sweep time, and continuous transmit time.
- Explicit **Start Calibration** and always-available **Cancel/Unkey** controls.
- Large current-state, measurement, null-depth where applicable, and audio-level displays.
- A final report containing the result, controlled setting, engine/profile, warnings, and relevant device information.

The first automation proof will use the internal tone generator and Bessel engine, followed by Direwolf. The controller architecture will also support discriminator feedback from the start so compatible fixed or repeatable modem waveforms can be calibrated against a measured deviation target.

VARA, xara, and QTSoundModem adapters will follow after their calibration and control capabilities have been verified. A generic external/manual-source workflow will remain available.

## 5. Measurement workflows

### 5.1 Common setup and frequency entry

1. Select the SoapySDR device and configure its initial gain and sample rate.
2. Enter the transmitter frequency manually, use **Read from Rig**, or enable **Link Rig Frequency** and commit a keyboard entry to a compatible rig through `rigctld`.
3. Connect the transmitter through a suitable dummy load, attenuator, or sampling coupler.
4. Select the equipment/profile and measurement engine.
5. Verify carrier lock, signal quality, receiver bandwidth, and absence of overload before accepting readings.

The entered, rig-read, or confirmed rig-written frequency is the actual transmitter frequency, not the SDR's internally offset LO frequency. The application manages offset tuning internally.

Manual frequency entry retunes only the SDR by default. With **Link Rig Frequency** enabled, a committed entry will instead follow this sequence:

1. Verify that PTT is off and no automatic calibration is active.
2. Write the requested frequency through `rigctld`.
3. Read the rig frequency back and check that it is valid and acceptably close to the requested value.
4. Apply the confirmed frequency to the SDR and common measurement state.
5. Report write/readback failures without claiming that the rig or complete receive path was successfully retuned.

A successful SDR-only entry, rig-frequency read, or confirmed linked rig write will use the same receive update path:

- Stop active automatic calibration before retuning.
- Retune the SoapySDR source, including the intended internal LO offset.
- Recenter the analysis chain and waterfall.
- Reset carrier lock and measurement acquisition.
- Clear or visibly invalidate the previous reading.

Rig synchronization will not use continuous background polling initially. Rig writes occur only after an explicit, validated keyboard commit with the link enabled; incomplete keystrokes must never change the rig frequency.

### 5.2 Discriminator measurement

1. Select the appropriate generic, MMDVM/DMR, or Module17/M17 profile.
2. Acquire the signal and establish its center-frequency estimate.
3. Capture instantaneous frequency while amplitude and signal quality are valid.
4. Apply the profile's robust-peak or digital-symbol estimator over the configured window.
5. Display live deviation, averaging/peak hold, capture quality, and the exact measurement definition.
6. Invalidate or freeze the reading when the transmission ends or quality falls below the profile threshold.

For digital profiles, capture enough symbols or repeated test patterns before declaring a stable result. Raw instantaneous-frequency captures should be optionally recordable for offline debugging and validation.

### 5.3 Bessel-null measurement

1. With modulation disabled, acquire and lock to the carrier.
2. Select the tone frequency and carrier null.
3. Key the transmitter manually or through an enabled, guarded PTT profile.
4. Apply a clean sinusoidal tone and slowly increase modulation or audio drive.
5. Detect and validate the selected local carrier minimum.
6. Calculate deviation and hold the result until reset, retuned, or replaced.

### 5.4 Closed-loop automatic calibration

Two feedback strategies will share the same guarded calibration state machine:

- **Bessel null seeking:** adjust audio level to bracket and refine the selected carrier null. This does not require a continuous deviation estimate.
- **Discriminator target control:** adjust audio level from the error between a validated discriminator reading and the target. This requires a stable, repeatable test waveform and a monotonic audio-level/deviation relationship.

The common sequence is:

1. Validate the SDR, frequency, rig/PTT, audio route, modem adapter, measurement profile, target, and safety limits.
2. Record the original audio-server setting and identify exactly which level will change.
3. Verify carrier lock and signal quality.
4. Start the test waveform and PTT using the ownership mode defined by the modem profile.
5. Begin at a low level and make bounded adjustments, allowing audio and receiver measurements to settle after each change.
6. Use null bracketing/refinement or damped target-error control as appropriate.
7. Confirm a stable result with no overload, clipping, lost lock, or ambiguous measurement.
8. Stop the waveform, unkey, and save the result.
9. On cancellation, timeout, lost signal, or failure, unkey and restore the original audio setting unless the operator explicitly keeps the last tested value.

Successful calibration may leave the selected per-application audio level at its new value. The report must distinguish audio-server changes from modem-internal settings and must not promise persistence across modem restarts without verification.

## 6. User-interface design

### 6.1 Shared window structure

Two large tabs will appear at the top:

```text
[ Measure ] [ Auto-Calibrate ]
```

A shared bottom bar, outside the switching tab content, will keep the receive-frequency field, **Read from Rig** button, **Link Rig Frequency** control, and essential RF/PTT status visible on either tab. Advanced SDR, `rigctld`, and calibration settings will use separate dialogs where necessary.

The application will target nearly fullscreen operation at **1280×720 or higher**. At 720p, bottom controls may use two compact fixed-height rows, but frequency and Cancel/Unkey must remain visible without scrolling when relevant.

### 6.2 Measure tab

```text
┌──────────────────────────────────────────┬─────────────────────────┐
│ [ Measure ] [ Auto-Calibrate ]           │                         │
├──────────────────────────────────────────┼─────────────────────────┤
│                                          │       DEVIATION         │
│                                          │       2.41 kHz          │
│         Large analog-style meter         │                         │
│                                          │ DISCRIMINATOR · M17     │
│                                          │ Outer-symbol deviation  │
│                                          ├─────────────────────────┤
│                                          │                         │
│                                          │  Scrolling waterfall    │
│                                          │  centered on carrier    │
│                                          │                         │
├──────────────────────────────────────────┴─────────────────────────┤
│ Receive frequency: [ 145.500000 ] MHz [Read from Rig] [Link Rig]   │
│ Engine: Discriminator | Profile: Module17/M17 | TX ACTIVE | Good SNR │
└────────────────────────────────────────────────────────────────────┘
```

For Bessel operation, the lower controls instead expose tone, null, Arm, Reset, and Hold. The digital pane will explicitly say **BESSEL NULL** and show the selected null and tone. The same meter may display live discriminator values or held Bessel results, but never without identifying their source.

### 6.3 Auto-Calibrate tab

The Auto-Calibrate tab will favor a guided workflow:

- Modem/profile, measurement engine, audio stream, PTT ownership, target, and test-signal configuration.
- Large states such as **READY**, **CHECKING CARRIER**, **CAPTURING**, **SWEEPING**, **REFINING**, **VERIFYING**, **COMPLETE**, and **FAILED**.
- Live deviation or null-depth indication and a compact spectrum/waterfall.
- Current controlled audio level and target.
- Prominent Start before activation and Cancel/Unkey while active.
- Results with final setting and warnings or failure reasons.

### 6.4 Visual design

- Dark background with white meter ticks, scale labels, pointer, primary text, and digital numerals.
- White, rather than green, as the default high-contrast foreground color.
- Restrained amber or red for warnings and invalid states; color will not be the sole status indicator.
- Meter occupies approximately two-thirds of the Measure tab's main content width.
- Digital deviation is the largest text in the upper-right pane.
- Waterfall occupies the lower-right pane with a separate high-contrast color map.
- Meter animation may be lightly smoothed, but acquisition, PTT, and warning indicators must respond promptly.

### 6.5 Frequency-entry and rig-synchronization behavior

- Default display unit: MHz with sufficient precision for amateur-radio channels.
- Allow direct keyboard entry and clearly identify invalid or out-of-range values.
- Make the field convenient for mouse or touchscreen use.
- Show the actual transmitter frequency, never the internal SDR LO offset.
- Apply manual input on Enter, validated focus-out, or an explicit tune action rather than retuning on every keystroke.
- Keep **Link Rig Frequency** off by default; with the link off, manual entry affects only the SDR.
- With the link on, write only complete, validated frequency commits and read the rig back before displaying a confirmed synchronized frequency.
- Clearly show whether the current value came from manual entry, a rig read, or a confirmed rig write.
- Disable **Read from Rig** and the rig-frequency link with an explanatory status when `rigctld` is unavailable or lacks the required capability.
- Inhibit rig-frequency writes while PTT is active or an automatic calibration is running.
- Treat a rig-read or confirmed rig-written frequency exactly like a confirmed SDR-only entry for retuning and measurement invalidation.

### 6.6 Suggested meter ranges

- 0–3 kHz
- 0–6 kHz
- 0–10 kHz

Additional ranges can be supported when higher-deviation modes are validated.

## 7. System architecture

### 7.1 Recommended stack

- **GNU Radio:** SoapySDR streaming, channelization, frequency translation, filtering, FFT processing, quadrature demodulation, and audio-tone generation.
- **SoapySDR:** the sole SDR receive-hardware abstraction.
- **Python:** application coordination, measurement profiles, estimators, automation state machines, and configuration.
- **PyQt:** tabs, fullscreen UI, custom meter, and calibration workflow.
- **Network `rigctld` client:** rig-frequency reading, confirmed frequency writing, and PTT using Hamlib's network service.
- **PulseAudio-compatible audio API:** per-application stream and sink control under PulseAudio or PipeWire-pulse.
- **NumPy/SciPy as needed:** numerical processing, clustering, and Bessel constants.

Custom C++ GNU Radio blocks are not expected to be necessary initially.

### 7.2 Shared receive chain and measurement engines

```text
SoapySDR source or software simulator
                    |
          Offset-tuned receive chain
                    |
        Channel filter and decimation
                    |
          +---------+----------+
          |                    |
   High-resolution FFT    Quadrature FM demodulator
          |                    |
 Carrier acquisition      Instantaneous frequency
          |                    |
 Carrier-power/null       Amplitude/SNR gate and center correction
          |                    |
   Bessel-null engine     Profile-specific discriminator engine
          |                    |
          +---------+----------+
                    |
       Common reading/quality/result model
                    |
       Meter, readout, and calibration controller
```

The FFT branch also supplies the waterfall. Both engines will emit a common result structure containing value, units, timestamp, engine, profile, measurement definition, quality, and validity so the UI and automation cannot silently mix incompatible readings.

### 7.3 Bench transmit/control chain

```text
GNU Radio sine source -> level control -> selected sound-card output
                                               |
                                        Radio audio input

Application -> network rigctld -> compatible radio/interface frequency and PTT
```

The SDR and audio branches must be paced independently; the audio sink must not throttle the receive chain.

### 7.4 Modem calibration control chain

```text
Modem test signal -> PulseAudio/PipeWire stream -> radio audio input
                              ^
                              |
RF -> SDR -> selected measurement engine -> calibration controller
                                                   |
                                     bounded audio-level adjustments
```

Each modem profile must declare whether the modem or application owns PTT. Both must never control PTT simultaneously.

### 7.5 Device and integration abstraction

- Use SoapySDR from the first working prototype and treat RTL-SDR as one Soapy-backed device type.
- Keep simulator and SDR inputs behind a common source-selection interface.
- Put network rig control, audio-server control, modem control, and measurement engines behind small adapter interfaces.
- Use capability detection so unsupported automation becomes clear manual instructions.
- Do not claim compatibility with an SDR, digital mode, or modem until its required behavior has been tested.

## 8. Rig control, audio routing, and PTT safety

### 8.1 Network `rigctld` only

- Connect to an existing or operator-started `rigctld` instance over TCP; do not open the rig directly through a local Hamlib library.
- Do not add a separate application-owned serial RTS/DTR backend. If a radio/interface uses serial PTT, it must be exposed through the configured `rigctld` service or operated manually.
- Read rig frequency on demand and populate the shared receive-frequency field.
- When the operator enables the rig-frequency link, write committed frequencies through `rigctld`, read back the result, and use the confirmed value for the SDR.
- Change only frequency through this feature; do not implicitly change rig mode, VFO, power, or other settings.
- Refuse rig-frequency writes while transmitting and surface unsupported writes or readback mismatches as errors.
- Use the same connection for optional PTT when supported by the configured rig.
- Display connection, rig, frequency-source, link state, and PTT-ownership state before enabling automatic operation.
- Avoid background frequency polling initially.

### 8.2 PulseAudio and PipeWire control

- Use the PulseAudio-compatible control interface first to support PulseAudio and PipeWire-pulse.
- Prefer the selected modem's **per-application playback stream** over master sink volume.
- Offer a dedicated virtual-sink profile for repeatable routing and isolation.
- Change master sink volume only after explicit operator selection and warning.
- Keep other gain stages fixed and include them in the report where possible.
- Detect direct-ALSA or other paths that bypass the selected audio server; these require modem-specific control or manual adjustment.
- Bound adjustments and do not exceed 100%/0 dB by default merely to reach a target.

### 8.3 Transmission safeguards

- Require explicit operator activation for automated PTT.
- Default to manual PTT until a `rigctld` profile has been tested.
- Provide maximum step count, sweep duration, and continuous-transmission timers.
- Keep Cancel/Unkey available throughout a sequence.
- Unkey on completion, cancellation, errors, lost preconditions, and normal application shutdown.
- Start at low audio amplitude and prohibit unbounded level increases.
- Prevent concurrent modem/application PTT ownership and multiple sessions controlling the same resources.
- Encourage dummy-load testing.

## 9. Equipment and modem integration strategy

### 9.1 MMDVM/DMR signal profile — initial measurement target

- Create signal profiles that include the active digital mode rather than treating MMDVM as one waveform.
- Start with repeatable transmitted fixtures and record I/Q for offline analysis.
- Validate the discriminator's protocol-relevant metric against known synthetic levels and, where available, an independent reference.
- Report deviation and capture quality for the operator; do not integrate with or automate MMDVM/repeater configuration.

### 9.2 Module17/M17 signal profile — initial measurement target

- Generate or capture representative M17 4FSK test transmissions.
- Implement an M17-specific symbol-level/outer-deviation estimator with activity and quality gating.
- Validate level clustering, center-offset rejection, capture duration, and repeatability.
- Show the selected M17 measurement definition prominently so it cannot be confused with generic instantaneous peak deviation.

### 9.3 Internal tone generator — first automation target

The application controls tone, level, and PTT, making this the simplest end-to-end proof of Bessel calibration and a useful reference for checking discriminator scaling.

### 9.4 Direwolf — first external sound-modem target

Direwolf's documented `-x` transmit-calibration option supports alternating tones, steady mark/space tones, and PTT-only operation in current documentation. The integration will:

- Use a steady tone for Bessel measurement; alternating tones or packets require the discriminator path.
- Detect or ask for the Direwolf version and supported arguments.
- Use an operator-approved configuration without modifying the normal configuration file.
- Determine actual tone frequencies from the selected modem profile and verify them in testing.
- Define PTT ownership and identify the correct audio playback stream.
- Support a remote Direwolf host over SSH without carrying SDR IQ: the remote
  process owns remote PTT, its remote PipeWire stream is volume-controlled,
  and the local application verifies PTT through a network `rigctld` endpoint.

### 9.5 VARA

Investigate VARA calibration as a dedicated integration spike:

- Determine whether calibration provides a steady sinusoid, another repeatable waveform, or only AutoTune/data behavior.
- Verify start, stop, and monitoring through a supported control interface.
- Choose between VARA's drive control and its audio-server stream as the controlled variable.
- Test its PulseAudio/PipeWire visibility, including Linux/Wine deployments.
- Select Bessel or discriminator feedback according to the verified waveform.

VARA AutoTune pings or ordinary traffic must not be treated as steady Bessel tones.

**Status:** VARA’s TUNE and drive controls are GUI-only, and its documented TCP
command port does not expose a scriptable steady-tone command. VARA remains a
manual-assisted source using the Measure tab’s Bessel controls.

### 9.6 xara

Assess xara's VARA-compatible control interface and calibration behavior. Reuse a VARA adapter only if the required commands, waveform, and behavior are compatible. Otherwise, provide a separate adapter or manual-assisted profile.

**Status:** Implemented. xara exposes a persistent command port and
`TUNE -30..0 dB`, `TUNE OFF`, and `TUNE ?`; the 1500 Hz ALC tone is available as
a bounded calibration source. The adapter verifies modem-originated PTT
`PTT ON`/`PTT OFF` notifications; the external rigctld is used only for
frequency/mode checks are optional. Remote VARA/xara uses its command port
directly; there is no built-in SSH transport, though users can run their own
port forwarding. The UI is labeled **VARA/xara** to allow testing against VARA
builds that expose compatible TUNE and PTT commands.

### 9.7 QTSoundModem

Initially support manual-assisted calibration and explicit audio-stream selection. Investigate a supported test-signal trigger and PTT ownership model before implementing automatic control. Avoid GUI automation when no stable CLI, IPC, or configuration interface exists.

**Status:** QtSoundModem 0.76 is GUI-only with no stable headless test-tone
interface. It remains a manual-assisted Bessel source using the Measure tab.

### 9.8 Generic modem/manual mode

Allow the operator to start a supported test signal while the application measures and, when possible, adjusts the selected stream. This keeps the instrument useful while specific integrations remain incomplete.

## 10. Signal-processing requirements

### 10.1 Offset tuning and DC avoidance

Direct-conversion receivers commonly produce a DC artifact at the tuned frequency. This can hide a Bessel carrier null and contaminate discriminator center-frequency estimation.

The receiver will offset its LO from the transmitter and use digital frequency translation. The offset must keep the carrier and required sidebands within usable, alias-free source bandwidth.

### 10.2 Common carrier/signal acquisition

- Estimate carrier offset before measurement where practical.
- Track or correct center frequency without confusing offset with deviation.
- Use fixed SDR gain with AGC disabled where possible.
- Detect low SNR, overload, loss of signal, and excessive drift.
- Select analysis bandwidth and filtering appropriate to the active profile.

### 10.3 Bessel-null processing

- Use a windowed FFT with sufficient resolution to separate carrier and sidebands.
- Apply short-time smoothing without averaging away the null.
- Detect a local minimum followed by recovery rather than storing the lowest historical value.
- Use the selected null number for deviation calculation.
- During controlled higher-order sweeps, count sequential nulls to avoid selecting an earlier one.
- Reject ambiguous or inadequately deep nulls.

### 10.4 Discriminator processing

- Scale quadrature-demodulator output to hertz using the actual analysis sample rate and a validated calibration factor.
- Gate samples using signal amplitude/quality to suppress noise clicks and invalid capture intervals.
- Remove residual center-frequency offset using an appropriate unmodulated-carrier or waveform-level estimate.
- Use robust percentiles, histogram/cluster analysis, or symbol-level estimates instead of raw maxima.
- Ensure the observation window contains enough symbols or test-pattern repetitions.
- Distinguish instantaneous pulse-shaping overshoot from protocol-relevant symbol deviation.
- Prevent channel filters from clipping instantaneous-frequency excursions or materially changing the measured waveform.
- Expose capture duration, quality, averaging, and peak-hold state to the UI.
- Support recording calibrated instantaneous-frequency traces for test-fixture development.

### 10.5 Cross-checking the two engines

A known sinusoidal Bessel-null signal provides an excellent independent check of discriminator scaling. Where the audio path and receiver support both methods, compare the discriminator result at a known null and use the result to validate or refine the discriminator calibration profile.

## 11. Calibration and accuracy

Neither engine requires absolute SDR amplitude calibration, but each has distinct accuracy requirements.

### 11.1 Shared calibration

- Avoid overload and compression and use fixed receiver gain.
- Provide optional SDR PPM/frequency correction.
- Verify adequate SNR and analysis bandwidth.
- Compare end-to-end readings with a known FM generator or service monitor where available.

### 11.2 Bessel-specific calibration

- Verify tone frequency because deviation scales directly with it.
- Use an undistorted sine below audio limiting.
- Avoid companders and aggressive speech processing.
- Filtering or pre-emphasis changes required drive but is acceptable when the tone remains sinusoidal.

### 11.3 Discriminator-specific calibration

- Verify instantaneous-frequency scaling from analysis sample rate and demodulator gain.
- Remember that SDR sample-clock error can scale discriminator deviation readings; apply device correction where needed.
- Use a known FM deviation source or Bessel-null cross-check to establish a per-profile scale correction if necessary.
- Validate center-offset removal, channel-filter response, robust percentiles, and symbol-level clustering separately.
- Validate each digital profile with recorded fixtures before comparing it with RF hardware.

Initial engineering targets, not guaranteed specifications, are:

- Bessel: within 1–2% in clean, high-SNR simulated tests and approximately 5% in a practical amateur RF setup.
- Discriminator: within approximately 5% against a known FM reference after scale validation, with protocol-specific digital metrics required to be repeatable and clearly defined before hardware acceptance.

## 12. RF test safety

The preferred transmitter test setup is:

```text
Transmitter -> suitable attenuator or sampling coupler -> SDR
                         |
                    Dummy load as required
```

Never connect a transmitter output directly to an SDR. A nearby receive antenna may be useful for low-power experiments, but a dummy load and properly rated sampling path are safer and more repeatable. Coupling and attenuation requirements depend on transmitter power and SDR and must be documented before hardware testing.

## 13. NixOS packaging and development environment

The application will be developed and packaged as a first-class NixOS application. GNU Radio on NixOS is delivered through a wrapped Python environment rather than a conventional system Python installation; the package must therefore run under a Python environment that can import both the application dependencies and GNU Radio's Python bindings.

### 13.1 Application repository

The main repository will provide:

- `flake.nix` and a committed `flake.lock` pinning the development nixpkgs revision.
- A development shell containing GNU Radio, SoapySDR with the RTL-SDR plugin, Python, NumPy/SciPy, PyQt6, PipeWire/PulseAudio client libraries, Hamlib, and test tooling.
- A `packages.default` build that installs the `deviation-meter` executable, desktop entry, icon, and application data.
- A package that wires the application into GNU Radio's wrapped Python environment, including the required `PYTHONPATH` and Qt runtime hooks, rather than assuming that the system Python can import `gnuradio`.
- SoapySDR module/plugin paths needed to discover RTL-SDR and other installed devices.
- A `checks` derivation that runs the simulator, Bessel, discriminator, and controller tests without requiring radio hardware.
- `nix run` support for launching the application directly from the repository.

The package will not embed development-only fixtures, test data, or local absolute paths.

### 13.2 `../nix-ham-packages` overlay integration

After the source is published and tagged, add the application to the neighboring overlay repository:

```text
../nix-ham-packages/
  gnuradio-deviation-meter/
    default.nix
```

The overlay entry will:

- Fetch a tagged source revision with `fetchFromGitHub`.
- Build with `callPackage` and use Nixpkgs' GNU Radio, SoapySDR, Qt, and Python environments.
- Install the `deviation-meter` executable as the package's `mainProgram`.
- Include a desktop entry so the application can be launched from a graphical session.
- Preserve compatibility with the overlay's existing nixpkgs input rather than depending on the application repository's flake pin.
- Allow local development by overriding `src` with the working tree without committing vendored source to `nix-ham-packages`.

The root `default.nix` will expose the package as:

```nix
gnuradio-deviation-meter = prev.callPackage ./gnuradio-deviation-meter { };
```

The overlay README will list the package, its purpose, and the installation attribute. The application README will document both `nix run` from the project repository and installation through the ham-packages overlay.

### 13.3 Packaging acceptance checks

- `nix build` produces a working application closure from a clean checkout.
- `nix run` starts the simulator-backed application without manually configuring Python paths.
- The packaged application imports GNU Radio, SoapySDR, NumPy, and PyQt successfully in a clean runtime environment.
- The overlay builds the same tagged source with its own nixpkgs revision.
- Hardware-free tests run through `nix flake check` or the package's test phase.
- The application launch does not require a development shell or globally installed Python packages.

## 14. Implementation phases

### Phase 1 — Project scaffold, Nix environment, and dual-engine simulators

- Initialize the Python package, test layout, and application entry point.
- Add the flake, development shell, and initial package derivation using GNU Radio's wrapped Python environment.
- Implement Bessel calculations and known sinusoidal-FM test signals.
- Implement instantaneous-frequency simulation and representative 4FSK waveforms.
- Create initial MMDVM-mode and M17 waveform fixtures with known expected metrics.
- Add noise, frequency offset, amplitude variation, and sample-clock error.
- Save representative I/Q, spectra, and instantaneous-frequency regression fixtures.
- Verify the hardware-free test suite through the Nix development environment.

### Phase 2 — SoapySDR shared receive chain

- Implement SoapySDR discovery, selection, and configuration.
- Test RTL-SDR exclusively through its SoapySDR driver.
- Add offset tuning, channel filtering, decimation, FFT/waterfall, and quadrature demodulation.
- Verify correct manual, rig-read, and confirmed rig-written transmitter frequencies versus internal LO frequency.
- Test additional Soapy-compatible SDRs as available.

### Phase 3 — Both measurement engines and Measure tab

- Implement the common result/quality model.
- Add Bessel carrier acquisition, null capture, and held readings.
- Add discriminator gating, center correction, robust-peak estimation, averaging, and peak hold.
- Add initial MMDVM-mode and Module17/M17 digital profiles.
- Build the two-tab shell, white-on-dark meter, digital pane, waterfall, and shared frequency/rig-synchronization bar.
- Add profile-aware nominal deviation references and a reference dialog.
  **Status:** Implemented. The digital pane and amber meter marks show the
  active profile reference; the bottom-bar dialog lists M17, DMR, analog FM,
  and Bessel relationships.
- Validate both engines in simulation and the complete layout at 1280×720 and 1920×1080.

### Phase 4 — Capture-backed MMDVM/DMR and M17 validation

- Capture and analyze transmissions from available M17/DMR radios and bench sources.
- Refine protocol-specific metrics using recorded M17 and DMR fixtures before live operation.
- Compare against known references where available; no repeater or external system integration is required.
- Verify repeatable readings and document supported modes, test-signal requirements, limitations, and target definitions.

### Phase 5 — Assisted bench operation

- Add the internal audio-tone generator and level control.
- Implement network `rigctld` frequency reading, opt-in linked writes with readback confirmation, and optional PTT.
- Add transmission timeouts, cancellation, and ownership safeguards.
- Create and test a Digirig Lite-style profile where supported by `rigctld`.
- Cross-check discriminator scaling against known Bessel-null signals.

### Phase 6 — Auto-Calibrate tab and closed-loop controllers

- Implement the guided Auto-Calibrate UI and common guarded state machine.
- Add PulseAudio-compatible stream discovery, selection, bounded control, and restoration.
- Prove Bessel null seeking with the internal generator and simulator.
  **Status:** Implemented for the internal generator and for deterministic
  hardware-free Bessel rehearsals with the Simulator source.
- Implement and test discriminator target-error control with a repeatable simulated waveform.
  **Status:** Implemented for live Generic FM feedback and for deterministic
  hardware-free target rehearsals with the Simulator source.
  **Status:** Implemented with the live Generic FM discriminator profile and
  bounded bidirectional level search. Unit tests cover linear, nonlinear,
  unreachable, timeout, cancel, and marginal-quality paths. The target follows
  the selected M17, DMR, FM narrow, or FM wide mode automatically and remains
  editable for custom targets.
- Add the Direwolf adapter and test success, cancellation, timeout, low-SNR, and unreachable-target paths.
  **Status:** Implemented with an exclusive generated Direwolf calibration
  process. Unit tests cover process ownership, PTT confirmation, cleanup, and
  competing-instance refusal.
- Add remote Direwolf operation over SSH for sites without the bandwidth for
  remote SDR IQ.
  **Status:** Implemented and unit-tested; remote-site RF, audio-route, PTT
  readback, and watchdog validation are still pending. The local application
  starts a temporary remote Direwolf process, controls only that remote
  PipeWire stream, verifies remote `rigctld` PTT, and relies on both the local
  PTT timer and a remote `timeout` watchdog covering the PTT timeout plus
  stream-startup margin. The selected Direwolf configuration is a local
  template; its `ADEVICE` must exist on the remote host.
- Save machine-readable and human-readable calibration reports.
  **Status:** Implemented. Each Bessel or discriminator target record writes
  JSON and a matching Markdown report.

### Phase 7 — Additional integrations and NixOS distribution

- Investigate and implement VARA, xara, and QTSoundModem support according to verified capabilities.
- Retain generic manual-assisted calibration for unsupported control paths.
- Perform repeated simulated and RF tests across supported devices and profiles.
- Document installation, RF coupling, `rigctld`, audio routing, safety, calibration, and operating procedures.
- Publish a tagged application release consumable by the overlay.
- Add and test `../nix-ham-packages/gnuradio-deviation-meter/default.nix`.
- Register `gnuradio-deviation-meter` in the overlay's root `default.nix` and README.
- Build and launch the application from the overlay's own nixpkgs revision.

## 15. Acceptance criteria

### Shared measurement and hardware

- Uses SoapySDR for RTL-SDR reception without a direct RTL-specific source block.
- Includes functioning Bessel-null and discriminator-based engines with a common result/quality model.
- Clearly identifies engine, profile, measurement definition, and validity for every reading.
- Correctly distinguishes manual, rig-read, and confirmed rig-written transmitter frequencies from internally offset SDR tuning frequency.
- Provides overload, low-SNR, and carrier/signal-lock warnings where reliably detectable.

### Digital-mode profiles

- Produces stable discriminator readings for recorded MMDVM/DMR and M17 fixtures with known expected results.
- Demonstrates repeatable measurements on captures from available radios without requiring repeater hardware.
- Uses validated protocol-specific metrics rather than raw instantaneous maxima.
- Documents the exact deviation definition and test-signal requirements for each supported profile.

### Measure tab and rig integration

- Displays the meter, digital deviation, waterfall, frequency field, and essential controls at 1280×720 without overlap or clipping.
- Uses white primary markings and numerals suitable for distant viewing.
- Supports manual operation without `rigctld`, automatic PTT, or audio-server control.
- Reads frequency on demand from a compatible network `rigctld` service and applies it through the common retune/invalidation path.
- With the rig-frequency link explicitly enabled, writes validated keyboard frequency entries, confirms them by readback, and applies the confirmed value to the SDR.
- Never writes a rig frequency from incomplete input, while PTT is active, or during automatic calibration.
- Uses no direct local-Hamlib or separate application-owned serial-rig backend.

### Auto-Calibrate tab

- Provides a second top-level tab dedicated to sound-card modem calibration.
- Keeps receive frequency, calibration state, and Cancel/Unkey visible during active sequences.
- Adjusts only the explicitly selected audio stream or sink within configured limits.
- Demonstrates Bessel closed-loop calibration with the internal generator and at least one Direwolf steady-tone profile.
- Demonstrates discriminator target control against a repeatable simulated waveform before claiming support for a real non-sinusoidal modem.
- Unkeys and restores or reports audio settings correctly on success, cancellation, timeout, and simulated failures.
- Never treats alternating tones, ordinary data, or AutoTune pings as valid steady-tone Bessel measurements.

### NixOS packaging

- Provides a locked flake with a development shell containing GNU Radio, SoapySDR, Qt, Python dependencies, and test tooling.
- Builds and launches the packaged application with GNU Radio's wrapped Python environment and without relying on globally installed Python packages.
- Runs hardware-free checks through the Nix package/flake.
- Adds `gnuradio-deviation-meter` to `../nix-ham-packages` and its README.
- Successfully builds the tagged application from the overlay's own nixpkgs revision.

### Documentation and tests

- Includes simulator tests for both engines and controller tests with mocked SDR, `rigctld`, PTT, and audio-server adapters.
- Includes recorded digital-waveform regression fixtures and a documented RF validation procedure.
- Includes explicit per-equipment and per-modem compatibility and limitation notes.
