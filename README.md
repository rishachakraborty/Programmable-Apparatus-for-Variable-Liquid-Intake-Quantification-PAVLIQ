# Head-Fixed Operant Behaviour Control System

A controller firmware and desktop application for running cued operant
tasks in head-fixed rodents. The system delivers metered liquid
reinforcers through retractable spouts, presents multimodal
discriminative stimuli, detects licking, and records a timestamped event
log suitable for direct analysis.

The design supports single-option and concurrent-choice procedures with
an arbitrary number of reinforcers, fixed, variable and progressive
ratio schedules, blocked designs with signalled or unsignalled
transitions, and cue-independent reinforcement controls.

---

## Contents

1. [Architecture](#architecture)
2. [Requirements and installation](#requirements-and-installation)
3. [Hardware](#hardware)
4. [Firmware reference](#firmware-reference)
5. [Application reference](#application-reference)
6. [Operating procedure](#operating-procedure)
7. [Data format](#data-format)
8. [Calibration](#calibration)
9. [Troubleshooting](#troubleshooting)

---

## Architecture

Control is divided between a microcontroller and a host application
along a deliberate boundary.

**The controller executes trials.** Once a trial is defined, the
microcontroller runs it to completion without host involvement: it
presents the stimulus, counts licks, resolves the choice, withdraws the
unchosen spouts, opens the delivery valve and manages the inter-trial
interval.

**The host defines sessions.** The application generates the trial
sequence, performs all randomisation, records the event log and renders
the interface.

The reason for this division is latency. Host-to-controller
communication over USB is subject to operating-system scheduling and
exhibits variable delays of several milliseconds to occasionally much
longer. Any decision placed on the host side of that link — for example,
"a lick was detected, therefore withdraw the alternative spout" — would
inherit that variability. Placing the responsive loop on the
microcontroller makes reinforcement latency a property of the apparatus
rather than of the computer running it.

All event timestamps originate from the microcontroller clock and are
captured at the moment the event occurs, before transmission, so serial
latency never propagates into recorded times.

```
  Application                          Controller
  ───────────                          ──────────
  session generation      ──ARM/TRGO──▶  stimulus presentation
  randomisation                          lick detection (1 kHz)
  event logging           ◀──events───    choice resolution
  visualisation                          spout actuation
                                         valve timing
                                         inter-trial interval
```

---

## Requirements and installation

**Controller:** Arduino Mega 2560. No external libraries beyond those
bundled with the Arduino IDE (`Servo`, `EEPROM`).

**Host:** Python 3.10 or later.

```
pip install pyserial PyQt6 pyqtgraph numpy
```

**Installing the firmware.** Place every `.h`, `.cpp` and the `.ino`
file in a single directory named `MouseTaskFirmware`, open the `.ino` in
the Arduino IDE and upload. Serial communication runs at 115200 baud.

**Launching the application.**

```
python gui_main.py
```

The application verifies the firmware version on connection and refuses
to proceed on a mismatch, since a protocol difference would otherwise
produce silent misbehaviour rather than an error.

---

## Hardware

| Function | Connection |
|---|---|
| Visual stimuli | Digital 2 (white), 3 (blue), 4 (green) |
| Auditory stimuli | Digital 5 (right), 6 (left) |
| Solenoid valves | Digital 12, 11, 10, 9, then 35–49 |
| Spout actuators | Digital 23 (left), 22 (centre), 25 (right) |
| Lick sensors | Analogue A13 (left), A14 (centre), A15 (right) |
| Syringe pumps | Digital 26–28 (left), 29–31 (right), 32–34 (centre) |

**Hardware timer allocation.** Timer 0 provides the millisecond clock.
Timer 2 generates visual-stimulus intensity by software pulse-width
modulation. Timers 3 and 4 generate the two auditory channels. Timer 5
is claimed by the servo library.

Two consequences follow. First, the two auditory channels have
independent timers, which is what permits different carrier frequencies
and modulation rates to be presented simultaneously on opposite sides —
a requirement of any concurrent-choice procedure. The Arduino `tone()`
function cannot do this, as it drives only one pin at a time, and is not
used anywhere in this firmware. Second, because Timers 0 and 3 own the
visual-stimulus pins, hardware PWM is unavailable there; intensity is
instead produced by a 31.25 kHz interrupt yielding a 122 Hz carrier with
256 levels, above the rodent flicker-fusion threshold.

**Channel counts** are limited by available pins, not by the software.
Solenoid channels are configured for 16 and spouts for 3; both can be
extended by assigning further pins in the controller configuration.
Channels that are not populated are marked absent and refuse to actuate,
so declaring more than are fitted is harmless.

---

## Firmware reference

### `MouseTaskFirmware.ino`
Entry point. Initialises every subsystem and runs a non-blocking main
loop. No subsystem is permitted to block, because lick sampling and
stimulus modulation both depend on the loop iterating promptly.
Actuators are deliberately not driven at startup, so a controller reset
mid-experiment does not move hardware against the subject.

### `config.h`
All pin assignments, channel counts, timing limits and default
parameters. The single file to edit when adapting to different wiring.

### `proto.h` / `proto.cpp`
Line-oriented serial protocol. Commands are comma-separated ASCII.
Responses carry a leading type character:

| Prefix | Meaning |
|---|---|
| `E` | Timestamped event — record this |
| `R` | Reply to a query |
| `A` | Command accepted |
| `X` | Command rejected, with reason |
| `#` | Human-readable comment |

### `led.h` / `led.cpp`
Visual stimuli. Software PWM from a Timer 2 interrupt using direct port
manipulation, providing 256 intensity levels and an optional slower
modulation envelope.

### `speaker.h` / `speaker.cpp`
Auditory stimuli. Each channel is driven by its own 16-bit timer in fast
PWM mode, giving fully independent carrier frequency, modulation rate
and amplitude per side.

### `servos.h` / `servos.cpp`
Spout positioning. Movement is interpolated at a configurable angular
rate rather than commanded directly, because an abrupt full-speed
traverse is mechanically noisy immediately adjacent to a resistive lick
sensor. Two positions are stored per spout — withdrawn and delivery —
and trial-time movement occurs only between them.

Servos are de-energised after a configurable idle period to eliminate
holding-current noise and heating. The idle release is suppressed for
the response window only — the interval from stimulus onset until the
response is resolved — because that is the only interval in which the
subject's behaviour can trigger a movement, and a de-energised actuator
must be re-energised and allowed to settle before it can move. As soon
as the response is resolved or the omission criterion is reached, the
release resumes, so the actuators are unpowered during consumption,
retraction and the inter-trial interval regardless of total trial
duration. The hold also expires on a deadline, so an abnormally
terminated trial cannot leave the motors energised.

Position is the commanded angle. Standard hobby servos provide no
position feedback, so a stalled or externally displaced actuator cannot
be detected. Soft travel limits guard against commanding beyond the
mechanical range; they are overridable during manual positioning, while
the absolute 0–180° range is not.

### `solenoids.h` / `solenoids.cpp`
Valve control. Each channel carries an identity — the reinforcer it
gates and the spout it feeds — persisted in EEPROM. Every open is
watchdogged; manual flushes close automatically after 15 seconds, so an
unattended open valve cannot empty a reservoir.

Valve transitions assert a brief blanking window during which lick
samples are discarded, since switching transients would otherwise appear
as licks.

### `lick.h` / `lick.cpp`
Contact detection by resistance change. No thresholds are compiled in.
A resistive lick sensor has no absolute scale: its resting value depends
on electrode geometry, wiring, humidity, and residual saliva, and it
drifts within a session. Thresholds are therefore derived from
measurements taken on the apparatus at the start of each session.

Detection combines four independent mechanisms:

- **Hysteresis.** Onset requires a larger excursion than offset, so a
  signal near threshold cannot oscillate.
- **Minimum duration.** A threshold crossing must persist to be
  accepted, rejecting electrical transients. The reported timestamp is
  the original crossing, not the confirmation, so this costs no temporal
  accuracy.
- **Refractory period.** Suppresses re-triggering on contact bounce.
- **Valve blanking.** Samples during valve transitions are discarded.

Baseline is tracked with a slow time constant, but only while idle and
only on samples already close to it, so sustained contact cannot drag
the baseline with it. Channels are sampled round-robin at 1 kHz each.

### `stepper.h` / `stepper.cpp`
Syringe pump control, one axis per spout. Step generation is
non-blocking with trapezoidal acceleration. Drivers are de-energised
between movements. Position is counted steps and is not preserved across
a power cycle, so each axis must be zeroed at its mechanical home before
use.

### `blockswitch.h` / `blockswitch.cpp`
Delivery-line purging. When the reinforcer assigned to a spout changes,
the dead volume in the tubing still holds the previous solution, and the
first response after the change would deliver it under a stimulus
signalling something else.

The purge withdraws the spouts, optionally aspirates the dead volume
with the syringe pump, then dispenses the newly assigned reinforcer
through the line. Aspiration can be disabled, in which case the line is
cleared by dispensing alone; this requires no pump but discards more
liquid and reaches a given purity more slowly. Purges may run
concurrently across spouts where each has its own pump.

### `trial.h` / `trial.cpp`
Trial state machine.

```
extend  → wait for physical arrival, not a fixed delay
cue     → all stimulus modalities fire from one staged batch
respond → first lick resolves choice; alternatives withdraw
          licks accumulate toward the response requirement
reward  → delivered at the later of the cue-to-reinforcer delay
          or requirement completion
consume → retraction delay
retract → inter-trial interval
gate    → require a lick-free interval before the trial ends
```

Extension waits for the actuators to arrive rather than for a timer, so
reinforcement latency reflects the subject rather than servo speed.
Trials on which reinforcement is withheld by the probability setting are
identical in every other respect — stimulus, extension, lick counting,
retraction timing — so withholding is not predictable from anything but
the absence of liquid.

### `commands.h` / `commands.cpp`
Command dispatch, and the staging mechanism used for synchronous
stimulus onset. Commands may be staged and then released together, so
all components of a compound stimulus begin within one loop iteration
and share a timestamp; sequential commands would be separated by
milliseconds.

### `debug.h` / `debug.cpp`
Interactive serial menu for bench testing. The menu constructs the same
command strings the application sends and submits them through the same
dispatcher, so the two paths cannot diverge.

---

## Application reference

### `arduino_link.py`
The sole module that accesses the serial port. Every method corresponds
to one controller command. A background reader thread classifies
incoming lines, routing asynchronous events to an event queue and
command responses to a separate response queue — events must never be
consumed by code awaiting an acknowledgement.

`sync_clock()` estimates the offset between controller and host clocks
from the lowest-round-trip sample of several probes.
`readiness_report()` verifies every prerequisite for a session and
separates blocking problems from advisory warnings.

### `task_design.py`
Session generation. Contains no hardware or interface code, so
randomisation can be inspected and verified independently.

**Stimuli.** `CueSet` holds any combination of auditory, visual,
olfactory and user-defined stimuli. Trials and block transitions use the
same structure, so any modality is available in either context.

**Concurrent choice.** The number of simultaneously available options is
not fixed. The only constraint is physical: two options cannot be
presented concurrently if they are delivered through the same spout.
Valid option sets are enumerated from that constraint alone. Given four
reinforcers, two per spout, a choice block yields all four cross-spout
pairings and excludes the two same-spout pairings. With a third spout
populated, three-way trials are generated without configuration change.

**Randomisation.** Sequences are constructed by partitioning into
counterbalancing windows, allocating each window a proportional share of
every item, and shuffling within the window. This enforces balance
structurally rather than relying on a whole-sequence shuffle, which
routinely leaves one item concentrated in one half of a session. A
repair pass resolves residual run-length violations by swapping within
windows, so correcting run length cannot disturb balance. Run-length
constraints carry across block boundaries.

**Reinforcement probability** is applied as an exact count rather than
an independent draw per trial, so a 50% setting over 24 trials yields
exactly 12.

**Inter-trial intervals** are drawn from a truncated exponential by
inverse transform sampling. The realised mean is below the requested
scale because of truncation; the session summary reports the realised
value.

**Cue-independent reinforcement** provides three dissociable controls:
unsignalled reinforcement at Poisson-distributed times within the
inter-trial interval; trials presented without any stimulus; and trials
on which delivered magnitude is drawn independently of the stimulus.
Each is flagged separately in the log.

**`audit_session()`** re-derives the constraints from the generated
output rather than trusting the generator, and should be inspected on
every session — randomisation faults leave no trace in the data itself.

### `calibration.py`
Volume-to-open-duration tables for the valves. A solenoid is not
linear: opening and closing occupy several milliseconds of partial flow,
so a single slope through the origin is least accurate at small volumes,
which is where graded reinforcer magnitudes typically lie. Measured
points are interpolated, and every lookup reports whether the result was
measured, interpolated or extrapolated.

### `stepper_cal.py`
Volume-to-displacement tables for the pumps, stored in steps rather than
time. Displacement is determined by step count; elapsed time is merely
step count divided by rate, so a time-keyed table becomes invalid
whenever the rate changes. Syringe barrel diameter is a table dimension,
since displaced volume per step scales with cross-sectional area;
interpolation between measured barrel sizes is two-stage.

### `settings.py`
Persists hardware configuration between sessions to a user-level JSON
file, written atomically. Spout positions, travel limits, valve
identities and pump parameters restore directly, as they describe the
apparatus. Lick thresholds are restored only on request and reported
with their age, since baselines drift. Pump origin is never restored,
because counted steps carry no meaning across a power cycle.

### `event_log.py`
Session recording. The CSV is written incrementally and flushed on every
event, so an interruption costs one event rather than a session. A
compressed array is written at close for convenience. Numeric event
identifiers are accompanied by a legend in the file header, so records
remain interpretable without the configuration that produced them.

### `session_runner.py`
Steps a generated session through the controller. Non-blocking: `tick()`
performs bounded work and returns, and advances only when the controller
reports a trial has ended, so the host cannot run ahead of the hardware.
A purge that the controller refuses does not proceed to the trial, since
that would deliver the previous reinforcer under the new stimulus.

### `theme.py`
Light and dark palettes. The two are not inversions of each other:
colours that discriminate well on a light ground lose separation on a
dark one, so each mode specifies its own values at matched hues.

### `gui_cue.py`
Stimulus editor exposing every parameter of every modality. One
implementation serves both trial stimuli and block-transition stimuli.

### `gui_setup.py`
Task definition and session preview. Includes the reinforcer list, valve
identities, trial-type table, block definitions, experiment composition,
temporal parameters, reinforcement schedule, randomisation constraints
and cue-independent controls, together with a generated event-sequence
preview and a raster of the planned session.

### `gui_calibration.py`
Valve and pump calibration tables, purge parameters, pump control, and
a capability scan that queries the connected controller rather than
inspecting source files — the source present on disk is not necessarily
what is installed on the device.

### `gui_experiment.py`
Controller connection, hardware configuration and session execution,
including live raster display and running summary statistics.

### `gui_main.py`
Assembles the four tabs and owns the objects shared between them.

---

## Operating procedure

### Defining a task

1. **Reinforcers** — list the solutions in use.
2. **Solenoids** — add one channel per valve; set the reinforcer it
   gates and the spout it feeds. Deselect channels not populated.
3. **Trial types** — one row per stimulus-outcome contingency. Open the
   stimulus editor to configure any combination of modalities. Set the
   reinforcer volume and delivery probability.
4. **Blocks** — define structure, reinforcers, trial-type composition
   and transition stimulus. Composition is either uniform across
   selected types or specified per type. Blocks may be excluded from the
   experiment without being deleted.
5. **Temporal parameters, schedule, randomisation, cue-independent
   controls.**
6. **Generate** — the session is produced, audited and displayed.

### Preparing the apparatus

1. Connect to the controller.
2. Restore stored configuration if available.
3. Position each spout and store its withdrawn and delivery positions.
4. Calibrate each lick sensor: acquire baseline with no contact, then
   acquire contact level with sustained contact.
5. Zero each pump at its mechanical home.
6. Verify configuration and resolve any blocking problems.
7. Store the configuration.

### Running

Enter a subject identifier and output directory, then start. The session
verifies readiness before beginning. Pausing takes effect after the
current trial completes rather than interrupting it.

---

## Data format

Three files per session:

```
<subject>_<timestamp>_events.csv      written continuously
<subject>_<timestamp>_events.npz      compressed array
<subject>_<timestamp>_session.json    seed, configuration, planned trials
```

Columns: `arduino_ms`, `host_unix`, `host_iso`, `event_id`,
`event_name`, `channel`, `d1`, `d2`, `trial`, `block`, `ratio`.

**Analyse using `arduino_ms`.** It is the clock on which the hardware
acted and contains no communication jitter. `host_unix` exists to align
with externally timed records such as video and should not be used for
latency measurement.

The session file contains the random seed. Re-entering it regenerates an
identical session.

---

## Calibration

### Valves

Deliver a known number of openings into a tared vessel and weigh the
result; 1 mg is approximately 1 µL. Enter several volume–duration pairs
spanning the range in use. Prefer requesting volumes that appear in the
table; intermediate values are interpolated, and values beyond the
measured range are reported as extrapolated.

### Lick sensors

Acquire baseline with nothing contacting the spout, then acquire contact
level with contact maintained throughout the window. The second
measurement determines detection polarity from the direction of the
observed change and sizes thresholds to the weakest sustained contact
rather than to the mean, which prevents a single sustained contact from
being fragmented into several detections.

The reported signal-to-noise ratio is the onset threshold in units of
baseline noise. Values below approximately four indicate that false
detections are likely.

### Pumps

Displace a known number of steps and weigh the delivered volume. Record
barrel size alongside each measurement.

---

## Troubleshooting

**The controller is not listed.** Enumeration through system APIs can be
incomplete; the application also inspects device nodes directly and
provides a scan that queries each candidate port. A serial port held by
another application, including a serial monitor, cannot be opened
concurrently. Charge-only USB cables are a frequent cause and are
externally indistinguishable from data cables.

**An actuator vibrates without moving.** Either the commanded position
is beyond mechanical travel, in which case set soft limits to the
measured range, or the supply cannot sustain the stall current, in which
case power the actuators independently of the controller board with a
common ground.

**A sustained contact registers as several licks.** Increase the
contact-offset confirmation window. Rodent licking occurs at 7–10 Hz, so
genuine inter-lick intervals are 50–100 ms and a confirmation window of
up to 40 ms cannot merge separate licks.

**Actuators become warm during long sessions.** Holding current is
applied only for the response window. If that window is long, the
advisory issued when the session starts indicates the motors will be
energised for that interval on every trial; shorten the window, or fit
actuators rated for continuous holding torque.

**Withdrawal after a choice is inconsistent.** Withdrawal is commanded
on the first detected lick, and the latency from detection to command is
recorded in the event log for verification. Inconsistency at this step
almost always reflects inconsistent contact detection rather than
actuation. Confirm that the reported signal-to-noise ratio is
comfortably above four, that the return path to the subject is a low
and stable resistance — an intermittent ground through the head-fixation
hardware produces exactly this pattern — and that thresholds were
acquired with contact quality representative of a licking subject rather
than a fingertip, which is considerably more variable.

**Reinforcement is delivered but not consumed.** The controller can
confirm that a valve opened, not that liquid reached the spout. Verify
delivery visually during the first trials of a session and confirm the
lines are primed.
