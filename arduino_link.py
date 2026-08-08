"""
arduino_link.py — Python mirror of MouseTaskFirmware v0.4.0.

This is the ONLY module that talks to the serial port. Everything above
it (task builder, GUIs, event log) uses these methods and never touches
raw bytes. Every method here corresponds to exactly one firmware
command, so the debug menu, this API and the GUI all exercise the same
code path on the microcontroller.

Threading model
---------------
A daemon reader thread owns the port and classifies every incoming line:

    E,...   timestamped event  -> event queue + listener callbacks
    R,...   reply to a query   -> response queue
    A,...   command accepted   -> response queue
    X,...   command rejected   -> response queue (or event queue, see below)
    #,...   human-readable     -> info log (and raw-sample callbacks)

Events NEVER go to the response queue. That matters: the firmware emits
LED_ON, SOL_OPEN and LICK_ON asynchronously, and a naive reader would
consume them while waiting for an acknowledgement and then time out.

Clocks
------
Arduino millis() is the master clock. Nothing in the timing of the task
depends on when a message reached the host. `sync_clock()` estimates the
offset between the two using the lowest-round-trip sample of several
probes, so every event can be given a host timestamp for filenames and
wall-clock reporting while analysis stays on the Arduino clock.
"""

from __future__ import annotations

import glob
import os
import queue
import threading
import time
from dataclasses import dataclass, field
from typing import Callable, Optional, Sequence

import serial
from serial.tools import list_ports

FIRMWARE_EXPECTED = "0.7.0"
FW_NAME_HINT = "MouseTaskFirmware"
DEFAULT_BAUD = 115200

# Errors the firmware can raise on its own initiative rather than in
# response to a command. If one of these arrived while we happened to be
# waiting for an acknowledgement, blaming it on the pending command
# would be wrong, so they are routed to the event stream instead.
ASYNC_ERRORS = {
    "SOL_WATCHDOG_FORCED_CLOSE",
}


class ArduinoError(RuntimeError):
    """The firmware rejected a command (an X record)."""


class NotConnectedError(RuntimeError):
    pass


# ---------------------------------------------------------------------
# Records
# ---------------------------------------------------------------------

@dataclass
class Event:
    """One timestamped hardware event."""
    t_ms: int          # Arduino millis() at the moment it happened
    type: str          # LICK_ON, TRIAL_REWARD, SPK_ON, ...
    ch: str            # L / C / R / W / B / G, or "" if not applicable
    d1: int
    d2: int
    host_time: float = 0.0   # time.time() when the line was read

    @property
    def t_s(self) -> float:
        return self.t_ms / 1000.0


@dataclass
class ServoStatus:
    ch: str
    current: int
    target: int
    moving: bool
    slew: int
    extend_dir: int
    attached: bool
    soft_min: int
    soft_max: int
    idle_detach_ms: int
    zero_angle: int
    pos_known: bool
    extend_angle: int
    extend_set: bool
    present: bool = True


@dataclass
class LickStatus:
    ch: str
    baseline: float
    sd: float
    on_delta: float
    off_delta: float
    polarity: int
    calibrated: bool
    enabled: bool
    count: int
    last_raw: int
    present: bool = True

    @property
    def snr(self) -> Optional[float]:
        """Trigger threshold expressed in units of baseline noise."""
        if self.sd <= 0:
            return None
        return self.on_delta / self.sd


@dataclass
class SolenoidStatus:
    index: int          # 1-based, as the user sees it
    liquid: str
    spout: str
    nl_per_ms: int
    is_open: bool
    present: bool = True

    def ms_for_ul(self, microlitres: float) -> Optional[float]:
        if self.nl_per_ms <= 0:
            return None
        return (microlitres * 1000.0) / self.nl_per_ms


@dataclass
class StepperStatus:
    ch: str
    position: int
    remaining: int
    moving: bool
    sps: int
    accel: int
    aspirate_sign: int
    soft_min: int
    soft_max: int
    nl_per_step: int
    hold_ms: int
    enabled: bool
    pos_known: bool
    present: bool

    def steps_for_ul(self, microlitres: float):
        if self.nl_per_step <= 0:
            return None
        return int(round(microlitres * 1000.0 / self.nl_per_step))


@dataclass
class BlockSwitchStatus:
    block_id: int
    state: int
    cycle: int
    spout: str
    outcome: int
    vac_steps: int
    vac_sps: int
    pre_ms: int
    vac_dwell_ms: int
    fill_dwell_ms: int
    post_ms: int
    sequential: bool
    cycles: int
    do_return: bool


BLOCK_OUTCOME_NAMES = {0: "none", 1: "ok", 2: "aborted"}


@dataclass
class TrialStatus:
    trial_id: int
    state: int
    mode: int
    chosen: str
    requirement_met: bool
    outcome: int
    cue_reward_ms: int
    omission_ms: int
    retract_delay_ms: int
    iti_ms: int
    gate_ms: int


OUTCOME_NAMES = {
    0: "none",
    1: "reward",
    2: "reward_withheld",
    3: "omission",
    4: "aborted",
}

TRIAL_STATE_NAMES = {
    0: "idle", 1: "extend", 2: "cue", 3: "respond", 4: "reward",
    5: "consume", 6: "retract", 7: "iti", 8: "gate",
}


@dataclass
class _Response:
    kind: str                       # "A", "R", "X"
    fields: list = field(default_factory=list)


# ---------------------------------------------------------------------
# Link
# ---------------------------------------------------------------------

class ArduinoLink:

    def __init__(self, port: Optional[str] = None, baud: int = DEFAULT_BAUD,
                 timeout: float = 2.0):
        self.port_name = port
        self.baud = baud
        self.timeout = timeout

        self._ser: Optional[serial.Serial] = None
        self._reader: Optional[threading.Thread] = None
        self._stop = threading.Event()
        self._send_lock = threading.Lock()

        self._responses: "queue.Queue[_Response]" = queue.Queue()
        self.events: "queue.Queue[Event]" = queue.Queue()

        self._event_listeners: list[Callable[[Event], None]] = []
        self._raw_listeners: list[Callable[[str, int, int], None]] = []
        self._info_listeners: list[Callable[[str], None]] = []

        self.info_log: list[str] = []
        self.firmware_id: str = ""

        # Clock mapping: host_time ≈ arduino_ms/1000 + clock_offset
        self.clock_offset: Optional[float] = None
        self.clock_uncertainty: Optional[float] = None

    # ---------------- connection ----------------

    # Ports that exist on every Mac and are never the rig. Listing them
    # is not harmful, but burying the real device among them is.
    _NOISE = ("bluetooth", "debug-console", "wlan-debug", "airpods")

    @staticmethod
    def _glob_devices() -> list[str]:
        """
        Find serial devices by looking at /dev directly.

        pyserial enumerates through IOKit on macOS and through sysfs on
        Linux, and both can miss a device that is plainly present as a
        file. The Arduino IDE finds boards this way and so do we. On
        macOS only /dev/cu.* is used, never /dev/tty.*: opening a tty
        device blocks waiting for carrier detect, which a USB board
        never asserts, so the open would hang.
        """
        pats = ["/dev/cu.*",                       # macOS
                "/dev/ttyACM*", "/dev/ttyUSB*",    # Linux
                "/dev/serial/by-id/*"]             # Linux, stable names
        out = []
        for pat in pats:
            for dev in glob.glob(pat):
                if os.path.exists(dev) and dev not in out:
                    out.append(dev)
        return out

    @classmethod
    def list_serial_ports(cls) -> list[tuple[str, str]]:
        """
        Every port worth trying, from pyserial AND from /dev.

        The two sources are merged rather than one being preferred,
        because each finds devices the other misses.
        """
        seen: dict[str, str] = {}
        try:
            for p in list_ports.comports():
                seen[p.device] = p.description or ""
        except Exception:
            pass

        for dev in cls._glob_devices():
            if dev not in seen:
                seen[dev] = "found in /dev"

        def rank(item):
            dev, desc = item
            blob = f"{dev} {desc}".lower()
            if any(n in blob for n in cls._NOISE):
                return 2
            if any(k in blob for k in ("usbmodem", "usbserial", "acm",
                                       "arduino", "wch", "ch34")):
                return 0
            return 1

        return sorted(seen.items(), key=rank)

    @staticmethod
    def autodetect_port() -> Optional[str]:
        """
        Best guess at the Mega, from USB descriptors.

        Matches on VID as well as text. Clone boards often report a
        generic description like "USB Serial" while still using a known
        USB-serial chip, so text alone misses them. Prefer an explicit
        port in production; this is a convenience, not a guarantee.
        """
        KNOWN_VIDS = {
            0x2341,   # Arduino
            0x2A03,   # Arduino (arduino.org era)
            0x1A86,   # QinHeng CH340 / CH341
            0x0403,   # FTDI
            0x10C4,   # Silicon Labs CP210x
            0x1B4F,   # SparkFun
            0x239A,   # Adafruit
        }
        KEYWORDS = ("arduino", "mega", "usb serial", "usb-serial", "ch340",
                    "ch341", "wchusbserial", "usbmodem", "usbserial",
                    "ftdi", "cp210", "acm")

        scored = []
        described = set()
        try:
            for p in list_ports.comports():
                described.add(p.device)
                blob = (f"{p.device} {p.description} {p.manufacturer or ''} "
                        f"{getattr(p, 'product', '') or ''} {p.hwid}").lower()
                score = 0
                if getattr(p, "vid", None) in KNOWN_VIDS:
                    score += 10
                if any(k in blob for k in KEYWORDS):
                    score += 5
                if "arduino" in blob or "mega" in blob:
                    score += 5
                if any(k in blob for k in ArduinoLink._NOISE):
                    score -= 20
                if score > 0:
                    scored.append((score, p.device))
        except Exception:
            pass

        # Devices pyserial did not report at all. On macOS this is often
        # where the board actually is, so they must be candidates rather
        # than an afterthought.
        for dev in ArduinoLink._glob_devices():
            if dev in described:
                continue
            low = dev.lower()
            if any(n in low for n in ArduinoLink._NOISE):
                continue
            score = 0
            if "usbmodem" in low or "usbserial" in low:
                score += 12
            elif "acm" in low or "ttyusb" in low:
                score += 10
            elif "wch" in low or "ch34" in low:
                score += 8
            if score:
                scored.append((score, dev))

        scored.sort(reverse=True)
        return scored[0][1] if scored else None

    @staticmethod
    def probe_ports(baud: int = DEFAULT_BAUD, wait: float = 2.5) -> list[str]:
        """
        Open every port in turn and ask which one is running our firmware.

        Slower than autodetect_port but definitive: it does not care what
        the USB descriptor claims. Returns the ports that answered.
        """
        found = []
        for dev, _desc in ArduinoLink.list_serial_ports():
            low = dev.lower()
            if any(n in low for n in ArduinoLink._NOISE):
                continue          # opening Bluetooth ports can hang
            try:
                ser = serial.Serial(dev, baud, timeout=0.2)
            except Exception:
                continue
            try:
                time.sleep(wait)          # board resets when the port opens
                ser.reset_input_buffer()
                ser.write(b"ID\n")
                ser.flush()
                deadline = time.time() + 2.0
                while time.time() < deadline:
                    line = ser.readline().decode("utf-8", errors="replace")
                    if line.startswith("R,ID,") and FW_NAME_HINT in line:
                        found.append(dev)
                        break
            except Exception:
                pass
            finally:
                try:
                    ser.close()
                except Exception:
                    pass
        return found

    def connect(self, wait_for_boot: float = 2.5,
                verify_firmware: bool = True) -> str:
        if self.port_name is None:
            self.port_name = self.autodetect_port()
            if self.port_name is None:
                raise NotConnectedError(
                    "No serial port found. Pass port= explicitly; "
                    f"available: {self.list_serial_ports()}")

        self._ser = serial.Serial(self.port_name, self.baud,
                                  timeout=0.1, write_timeout=2.0)

        # Opening the port toggles DTR, which resets the Mega into its
        # bootloader. Anything sent during this window is swallowed.
        time.sleep(wait_for_boot)
        self._ser.reset_input_buffer()

        self._stop.clear()
        self._reader = threading.Thread(target=self._read_loop,
                                        name="arduino-reader", daemon=True)
        self._reader.start()

        self.firmware_id = self.identify()
        if verify_firmware and FIRMWARE_EXPECTED not in self.firmware_id:
            raise ArduinoError(
                f"Firmware mismatch: board reports {self.firmware_id!r}, "
                f"this API expects {FIRMWARE_EXPECTED}. Re-upload the sketch.")

        self.sync_clock()
        return self.firmware_id

    def disconnect(self, safe_state: bool = True) -> None:
        if self._ser is not None and self._ser.is_open and safe_state:
            try:
                self.stop_all()
            except Exception:
                pass
        self._stop.set()
        if self._reader is not None:
            self._reader.join(timeout=1.0)
        if self._ser is not None:
            try:
                self._ser.close()
            finally:
                self._ser = None

    @property
    def connected(self) -> bool:
        return self._ser is not None and self._ser.is_open

    def __enter__(self):
        self.connect()
        return self

    def __exit__(self, *exc):
        self.disconnect()

    # ---------------- reader thread ----------------

    def _read_loop(self) -> None:
        buf = b""
        while not self._stop.is_set():
            try:
                chunk = self._ser.read(256)
            except Exception:
                break
            if not chunk:
                continue
            buf += chunk
            while b"\n" in buf:
                raw, buf = buf.split(b"\n", 1)
                line = raw.decode("utf-8", errors="replace").strip()
                if line:
                    self._classify(line)

    def _classify(self, line: str) -> None:
        kind, _, rest = line.partition(",")

        if kind == "E":
            parts = rest.split(",")
            if len(parts) >= 5:
                try:
                    ev = Event(t_ms=int(parts[0]), type=parts[1], ch=parts[2],
                               d1=int(parts[3]), d2=int(parts[4]),
                               host_time=time.time())
                except ValueError:
                    return
                self.events.put(ev)
                for fn in list(self._event_listeners):
                    try:
                        fn(ev)
                    except Exception:
                        pass
            return

        if kind == "#":
            self.info_log.append(rest)
            if rest.startswith("RAW,"):
                p = rest.split(",")
                if len(p) >= 4:
                    try:
                        ch, t_ms, val = p[1], int(p[2]), int(p[3])
                    except ValueError:
                        return
                    for fn in list(self._raw_listeners):
                        try:
                            fn(ch, t_ms, val)
                        except Exception:
                            pass
                return
            for fn in list(self._info_listeners):
                try:
                    fn(rest)
                except Exception:
                    pass
            return

        if kind == "X" and rest.split(",")[0] in ASYNC_ERRORS:
            # Firmware raised this on its own initiative. Surface it as an
            # event rather than blaming whatever command is pending.
            self.events.put(Event(t_ms=0, type="FIRMWARE_FAULT", ch="",
                                  d1=0, d2=0, host_time=time.time()))
            self.info_log.append(f"ASYNC FAULT: {rest}")
            return

        if kind in ("A", "R", "X"):
            self._responses.put(_Response(kind, rest.split(",")))

    # ---------------- listeners ----------------

    def add_event_listener(self, fn: Callable[[Event], None]) -> None:
        self._event_listeners.append(fn)

    def remove_event_listener(self, fn) -> None:
        if fn in self._event_listeners:
            self._event_listeners.remove(fn)

    def add_raw_listener(self, fn: Callable[[str, int, int], None]) -> None:
        """fn(channel, arduino_ms, adc_value) for LKRAW streams."""
        self._raw_listeners.append(fn)

    def add_info_listener(self, fn: Callable[[str], None]) -> None:
        self._info_listeners.append(fn)

    def drain_events(self) -> list[Event]:
        out = []
        while True:
            try:
                out.append(self.events.get_nowait())
            except queue.Empty:
                return out

    # ---------------- command plumbing ----------------

    def _write(self, cmd: str) -> None:
        if not self.connected:
            raise NotConnectedError("Not connected to the Arduino.")
        self._ser.write((cmd + "\n").encode("ascii"))
        self._ser.flush()

    def send(self, cmd: str, *, expect_ack: bool = True,
             reply_key: Optional[str] = None, reply_count: int = 1,
             timeout: Optional[float] = None) -> list[list[str]]:
        """
        Send one command and collect its response.

        expect_ack   wait for A,<verb>
        reply_key    also collect R,<reply_key>,... records
        reply_count  how many such records to expect (SOLGET,0 returns 4)

        Raises ArduinoError on X. Returns the reply records' fields.
        """
        timeout = self.timeout if timeout is None else timeout

        with self._send_lock:
            # Discard any stale responses so a previous timeout cannot
            # leak into this command's result.
            while True:
                try:
                    self._responses.get_nowait()
                except queue.Empty:
                    break

            self._write(cmd)

            replies: list[list[str]] = []
            got_ack = not expect_ack
            deadline = time.time() + timeout

            while time.time() < deadline:
                remaining = max(0.01, deadline - time.time())
                try:
                    resp = self._responses.get(timeout=remaining)
                except queue.Empty:
                    break

                if resp.kind == "X":
                    raise ArduinoError(
                        f"{','.join(resp.fields)}  (command: {cmd})")
                if resp.kind == "A":
                    got_ack = True
                elif resp.kind == "R":
                    if reply_key is None or resp.fields[0] == reply_key:
                        replies.append(resp.fields)

                enough = (reply_key is None) or (len(replies) >= reply_count)
                if got_ack and enough:
                    return replies

            raise TimeoutError(
                f"No complete response to {cmd!r} within {timeout:.1f}s "
                f"(ack={got_ack}, replies={len(replies)}/{reply_count})")

    # ---------------- housekeeping ----------------

    def ping(self) -> bool:
        r = self.send("PING", expect_ack=False, reply_key="PING")
        return r[0][1] == "1"

    def identify(self) -> str:
        r = self.send("ID", expect_ack=False, reply_key="ID")
        return ",".join(r[0][1:])

    def sync_clock(self, probes: int = 9) -> float:
        """
        Estimate host_time - arduino_seconds.

        Uses the probe with the shortest round trip, on the reasoning
        that the fastest exchange is the one least distorted by USB
        scheduling. Returns the offset in seconds.
        """
        best_rtt = float("inf")
        best_offset = 0.0
        for _ in range(probes):
            t0 = time.time()
            r = self.send("SYNC", expect_ack=False, reply_key="SYNC")
            t1 = time.time()
            rtt = t1 - t0
            ard_s = int(r[0][1]) / 1000.0
            if rtt < best_rtt:
                best_rtt = rtt
                best_offset = (t0 + rtt / 2.0) - ard_s
            time.sleep(0.01)
        self.clock_offset = best_offset
        self.clock_uncertainty = best_rtt / 2.0
        return best_offset

    def to_host_time(self, arduino_ms: int) -> Optional[float]:
        if self.clock_offset is None:
            return None
        return arduino_ms / 1000.0 + self.clock_offset

    def status(self) -> list[list[str]]:
        return self.send("STATUS", expect_ack=False, reply_key=None,
                         reply_count=0, timeout=1.0)

    def stop_all(self) -> None:
        """Gates closed, cues off, servos halted."""
        self.send("STOPALL")

    def help(self) -> list[str]:
        before = len(self.info_log)
        try:
            self.send("HELP", expect_ack=False, reply_count=0, timeout=1.5)
        except TimeoutError:
            pass
        return self.info_log[before:]

    # ---------------- LEDs ----------------

    def led(self, ch: str, duration_ms: int, *, pulsing: bool = False,
            pulse_hz: int = 0, brightness: int = 255) -> None:
        self.send(f"LED,{ch},{int(duration_ms)},{1 if pulsing else 0},"
                  f"{int(pulse_hz)},{int(brightness)}")

    def led_stop(self, ch: str) -> None:
        self.send(f"LEDSTOP,{ch}")

    @staticmethod
    def led_cmd(ch: str, duration_ms: int, *, pulsing: bool = False,
                pulse_hz: int = 0, brightness: int = 255) -> str:
        """Build the command string without sending it (for arming)."""
        return (f"LED,{ch},{int(duration_ms)},{1 if pulsing else 0},"
                f"{int(pulse_hz)},{int(brightness)}")

    # ---------------- speakers ----------------

    def speaker(self, ch: str, duration_ms: int, tone_hz: int, *,
                click_train: bool = False, click_hz: int = 0,
                volume: int = 50, click_on_us: int = 0) -> None:
        self.send(self.speaker_cmd(ch, duration_ms, tone_hz,
                                   click_train=click_train, click_hz=click_hz,
                                   volume=volume, click_on_us=click_on_us))

    def speaker_stop(self, ch: str) -> None:
        self.send(f"SPKSTOP,{ch}")

    @staticmethod
    def speaker_cmd(ch: str, duration_ms: int, tone_hz: int, *,
                    click_train: bool = False, click_hz: int = 0,
                    volume: int = 50, click_on_us: int = 0) -> str:
        return (f"SPK,{ch},{int(duration_ms)},{int(tone_hz)},"
                f"{1 if click_train else 0},{int(click_hz)},{int(volume)},"
                f"{int(click_on_us)}")

    # ---------------- synchronised cues ----------------

    def arm(self, cue_cmd: str) -> None:
        """
        Stage a cue. Staged cues all start inside one pass of the
        firmware's loop, tens of microseconds apart, sharing a single
        millisecond timestamp. Two separate commands cannot achieve
        that, which is why choice trials must use this.
        """
        self.send(f"ARM,{cue_cmd}")

    def go(self) -> None:
        self.send("GO", expect_ack=False, reply_count=0, timeout=1.0)

    def disarm(self) -> None:
        self.send("DISARM")

    # ---------------- servos ----------------

    def servo_init(self, ch: str = "all") -> None:
        self.send(f"SVINIT,{ch}", timeout=5.0)

    def servo_read(self, ch: str) -> ServoStatus:
        f = self.send(f"SVREAD,{ch}", expect_ack=False, reply_key="SERVO")[0]
        return ServoStatus(ch=f[1], current=int(f[2]), target=int(f[3]),
                           moving=f[4] == "1", slew=int(f[5]),
                           extend_dir=int(f[6]), attached=f[7] == "1",
                           soft_min=int(f[8]), soft_max=int(f[9]),
                           idle_detach_ms=int(f[10]), zero_angle=int(f[11]),
                           pos_known=f[12] == "1", extend_angle=int(f[13]),
                           extend_set=f[14] == "1",
                           present=(f[15] == "1") if len(f) > 15 else True)

    def servo_write(self, ch: str, angle: int, force: bool = False) -> bool:
        """force also overrides the soft limits and the 10 degree minimum.
        The 0-180 hardware range is always enforced by the firmware."""
        self.send(f"SVWRITE,{ch},{int(angle)},{1 if force else 0}")
        return True

    def servo_forward(self, ch: str, degrees: int,
                      force: bool = False) -> bool:
        self.send(f"SVFWD,{ch},{int(degrees)},{1 if force else 0}")
        return True

    def servo_back(self, ch: str, degrees: int, force: bool = False) -> bool:
        self.send(f"SVBACK,{ch},{int(degrees)},{1 if force else 0}")
        return True

    def servo_stop(self, ch: str) -> None:
        self.send(f"SVSTOP,{ch}")

    def servo_slew(self, ch: str, deg_per_s: int) -> None:
        self.send(f"SVSLEW,{ch},{int(deg_per_s)}")

    def servo_direction(self, ch: str, direction: int) -> None:
        self.send(f"SVDIR,{ch},{int(direction)}")

    def servo_limits(self, ch: str, lo: int, hi: int) -> None:
        self.send(f"SVLIMIT,{ch},{int(lo)},{int(hi)}")

    def servo_idle_detach(self, ch: str, ms: int) -> None:
        self.send(f"SVIDLE,{ch},{int(ms)}")

    def servo_set_retracted(self, ch: str, angle: int,
                            force: bool = False) -> bool:
        self.send(f"SVZERO,{ch},{int(angle)},{1 if force else 0}",
                  reply_key="SERVO")
        return True

    def servo_set_extended(self, ch: str, angle: int,
                           force: bool = False) -> bool:
        self.send(f"SVEXT,{ch},{int(angle)},{1 if force else 0}",
                  reply_key="SERVO")
        return True

    def servo_microseconds(self, ch: str, us: int) -> None:
        """Raw pulse width. Use to find real mechanical stops."""
        self.send(f"SVUS,{ch},{int(us)}")

    def servo_attach(self, ch: str) -> None:
        self.send(f"SVATTACH,{ch}")

    def servo_detach(self, ch: str) -> None:
        self.send(f"SVDETACH,{ch}")

    def servos_off(self) -> None:
        """Panic: every servo limp."""
        self.send("SVOFF")

    def wait_for_servos(self, timeout: float = 5.0) -> bool:
        """Poll until nothing is moving. Trial-time code should not need
        this; the firmware already waits internally."""
        deadline = time.time() + timeout
        while time.time() < deadline:
            if not any(self.servo_read(c).moving for c in ("l", "c", "r")):
                return True
            time.sleep(0.02)
        return False

    # ---------------- solenoids ----------------

    def solenoid_identity(self, index: int, liquid: str, spout: str) -> None:
        if "," in liquid:
            raise ValueError("Liquid name cannot contain a comma; "
                             "it is the protocol's field separator.")
        self.send(f"SOLID,{int(index)},{liquid},{spout}", reply_key="SOL")

    def solenoid_get(self, index: int) -> SolenoidStatus:
        f = self.send(f"SOLGET,{int(index)}", expect_ack=False,
                      reply_key="SOL")[0]
        return SolenoidStatus(index=int(f[1]), liquid=f[2], spout=f[3],
                              nl_per_ms=int(f[4]), is_open=f[5] == "1",
                              present=(f[6] == "1") if len(f) > 6 else True)

    N_SOLENOIDS = 8

    def solenoid_get_all(self) -> list[SolenoidStatus]:
        rows = self.send("SOLGET,0", expect_ack=False, reply_key="SOL",
                         reply_count=self.N_SOLENOIDS, timeout=3.0)
        return [SolenoidStatus(index=int(f[1]), liquid=f[2], spout=f[3],
                               nl_per_ms=int(f[4]), is_open=f[5] == "1",
                               present=(f[6] == "1") if len(f) > 6 else True)
                for f in rows]

    def solenoid_set_present(self, index: int, present: bool) -> None:
        self.send(f"SOLPRESENT,{int(index)},{1 if present else 0}",
                  reply_key="SOL")

    def servo_set_present(self, ch: str, present: bool) -> None:
        self.send(f"SVPRESENT,{ch},{1 if present else 0}", reply_key="SERVO")

    def lick_set_present(self, ch: str, present: bool) -> None:
        self.send(f"LKPRESENT,{ch},{1 if present else 0}", reply_key="LICK")

    def solenoid_calibrate(self, index: int, nl_per_ms: int) -> None:
        """
        Calibration in nanolitres per millisecond of open time.
        Measure by weighing the output of many long opens:
        20 x SOLDISP,n,1000 yielding 24 mg -> 24000 nL / 20000 ms = 1.2
        -> pass 1200. Each solenoid differs; calibrate them separately.
        """
        self.send(f"SOLCAL,{int(index)},{int(nl_per_ms)}")

    def solenoid_open(self, index: int) -> None:
        """Manual flush. Watchdogged at 60 s by the firmware."""
        self.send(f"SOLOPEN,{int(index)}")

    def solenoid_close(self, index: int) -> None:
        self.send(f"SOLCLOSE,{int(index)}")

    def solenoid_dispense_ms(self, index: int, ms: int) -> None:
        self.send(f"SOLDISP,{int(index)},{int(ms)}")

    def solenoid_dispense_ul(self, index: int, microlitres: float) -> None:
        """Requires the solenoid to have been calibrated."""
        self.send(f"SOLVOL,{int(index)},{int(round(microlitres * 1000))}")

    # ---------------- lick sensors ----------------

    def lick_read(self, ch: str) -> LickStatus:
        f = self.send(f"LKREAD,{ch}", expect_ack=False, reply_key="LICK")[0]
        return self._parse_lick(f)

    def lick_read_all(self) -> list[LickStatus]:
        rows = self.send("LKALL", expect_ack=False, reply_key="LICK",
                         reply_count=3)
        return [self._parse_lick(f) for f in rows]

    @staticmethod
    def _parse_lick(f: Sequence[str]) -> LickStatus:
        return LickStatus(ch=f[1], baseline=float(f[2]), sd=float(f[3]),
                          on_delta=float(f[4]), off_delta=float(f[5]),
                          polarity=int(f[6]), calibrated=f[7] == "1",
                          enabled=f[8] == "1", count=int(f[9]),
                          last_raw=int(f[10]),
                          present=(f[11] == "1") if len(f) > 11 else True)

    def lick_stream_raw(self, ch: str, ms: int = 10000) -> bool:
        """Starts a raw ADC stream. Subscribe with add_raw_listener()."""
        self.send(f"LKRAW,{ch},{int(ms)}")
        return True

    def lick_stop_stream(self) -> bool:
        self.send("LKSTOP")
        return True

    def lick_calibrate_baseline(self, ch: str, ms: int = 2000,
                                wait: bool = True) -> Optional[LickStatus]:
        """Spout must be UNTOUCHED for the whole window."""
        self.send(f"LKCAL,{ch},{int(ms)}")
        if not wait:
            return None
        f = self._await_reply("LICK", timeout=ms / 1000.0 + 3.0)
        return self._parse_lick(f)

    def lick_calibrate_touch(self, ch: str, ms: int = 2000,
                             wait: bool = True) -> Optional[LickStatus]:
        """Contact must be HELD for the whole window. This is the
        calibration that matters: it measures how big a real contact is
        and derives polarity from which way the signal actually moved."""
        self.send(f"LKTOUCH,{ch},{int(ms)}")
        if not wait:
            return None
        f = self._await_reply("LICK", timeout=ms / 1000.0 + 3.0)
        return self._parse_lick(f)

    def _await_reply(self, key: str, timeout: float) -> list[str]:
        """Wait for a delayed R record that arrives long after the ack."""
        deadline = time.time() + timeout
        with self._send_lock:
            while time.time() < deadline:
                try:
                    resp = self._responses.get(
                        timeout=max(0.01, deadline - time.time()))
                except queue.Empty:
                    break
                if resp.kind == "X":
                    raise ArduinoError(",".join(resp.fields))
                if resp.kind == "R" and resp.fields[0] == key:
                    return resp.fields
        raise TimeoutError(f"No delayed R,{key} record within {timeout:.1f}s")

    def lick_enable(self, ch: str, enabled: bool = True) -> None:
        self.send(f"{'LKON' if enabled else 'LKOFF'},{ch}")

    def lick_reset_count(self, ch: str) -> bool:
        self.send(f"LKRESET,{ch}")
        return True

    def lick_reset_all_counts(self) -> bool:
        self.send("LKZERO")
        return True

    def lick_timing(self, min_on_ms: int = 5, min_off_ms: int = 25,
                    refractory_ms: int = 15) -> tuple[int, int, int]:
        """
        min_off_ms is the important one. Raise it if a single sustained
        contact fragments into several licks. Mice lick at 7-10 Hz, so
        genuine inter-lick gaps are 50-100 ms; 40 is still safe.
        """
        f = self.send(f"LKTIME,{int(min_on_ms)},{int(min_off_ms)},"
                      f"{int(refractory_ms)}", reply_key="LICKTIME")[0]
        return int(f[1]), int(f[2]), int(f[3])

    def lick_set_thresholds(self, ch: str, on_delta: float, off_delta: float,
                            polarity: int = 0) -> None:
        """Manual override, for when calibration cannot capture the case."""
        self.send(f"LKSET,{ch},{int(on_delta)},{int(off_delta)},"
                  f"{int(polarity)}", reply_key="LICK")

    # ---------------- trials ----------------

    def trial_new(self, trial_id: int, choice: bool) -> None:
        self.send(f"TRNEW,{int(trial_id)},{1 if choice else 0}")

    def trial_spout(self, ch: str, solenoid: int, dispense_ms: int,
                    ratio: int, rewarded: bool) -> None:
        self.send(f"TRSPOUT,{ch},{int(solenoid)},{int(dispense_ms)},"
                  f"{int(ratio)},{1 if rewarded else 0}")

    def trial_timing(self, cue_reward_ms: int, omission_ms: int,
                     retract_delay_ms: int, iti_ms: int) -> None:
        self.send(f"TRTIME,{int(cue_reward_ms)},{int(omission_ms)},"
                  f"{int(retract_delay_ms)},{int(iti_ms)}")

    def trial_gate(self, ms: int) -> None:
        """Quiet period with no licking required before a trial may end."""
        self.send(f"TRGATE,{int(ms)}")

    def trial_go(self) -> None:
        self.send("TRGO")

    def trial_abort(self) -> None:
        self.send("TRABORT")

    def trial_state(self) -> TrialStatus:
        f = self.send("TRSTATE", expect_ack=False, reply_key="TRIAL")[0]
        return TrialStatus(trial_id=int(f[1]), state=int(f[2]),
                           mode=int(f[3]), chosen=f[4],
                           requirement_met=f[5] == "1", outcome=int(f[6]),
                           cue_reward_ms=int(f[7]), omission_ms=int(f[8]),
                           retract_delay_ms=int(f[9]), iti_ms=int(f[10]),
                           gate_ms=int(f[11]))

    def run_trial(self, *, trial_id: int, choice: bool,
                  cues: Sequence[str],
                  spouts: Sequence[dict],
                  cue_reward_ms: int, omission_ms: int,
                  retract_delay_ms: int, iti_ms: int,
                  wait: bool = False,
                  timeout: Optional[float] = None) -> Optional[int]:
        """
        Define and launch one trial.

        cues    already-built command strings, e.g.
                [link.speaker_cmd("l", 500, 10000, click_train=True,
                                  click_hz=50)]
                They are armed, so they begin together.
        spouts  [{"ch": "l", "solenoid": 1, "dispense_ms": 50,
                  "ratio": 3, "rewarded": True}, ...]

        With wait=False (the default and the right choice during a
        session) this returns immediately and the caller consumes
        events. The firmware runs the trial without further host
        involvement, so nothing about reward timing depends on the
        host's scheduler.
        """
        self.disarm()
        for c in cues:
            self.arm(c)

        self.trial_new(trial_id, choice)
        for s in spouts:
            self.trial_spout(s["ch"], s["solenoid"], s["dispense_ms"],
                             s["ratio"], s["rewarded"])
        self.trial_timing(cue_reward_ms, omission_ms, retract_delay_ms, iti_ms)
        self.trial_go()

        if not wait:
            return None
        return self.wait_for_trial_end(trial_id, timeout=timeout)

    def wait_for_trial_end(self, trial_id: int,
                           timeout: Optional[float] = None) -> int:
        """Block until TRIAL_END for this id. Returns the outcome code.
        Intended for bench testing, not for driving a session."""
        deadline = time.time() + (timeout if timeout else 120.0)
        while time.time() < deadline:
            try:
                ev = self.events.get(timeout=0.2)
            except queue.Empty:
                continue
            if ev.type == "TRIAL_END" and ev.d1 == trial_id:
                return ev.d2
        raise TimeoutError(f"Trial {trial_id} did not end within the timeout.")

    # ---------------- steppers ----------------

    def stepper_read(self, ch: str) -> StepperStatus:
        f = self.send(f"STPREAD,{ch}", expect_ack=False, reply_key="STEP")[0]
        return self._parse_step(f)

    def stepper_read_all(self) -> list[StepperStatus]:
        rows = self.send("STPALL", expect_ack=False, reply_key="STEP",
                         reply_count=3)
        return [self._parse_step(f) for f in rows]

    @staticmethod
    def _parse_step(f) -> StepperStatus:
        return StepperStatus(
            ch=f[1], position=int(f[2]), remaining=int(f[3]),
            moving=f[4] == "1", sps=int(f[5]), accel=int(f[6]),
            aspirate_sign=int(f[7]), soft_min=int(f[8]), soft_max=int(f[9]),
            nl_per_step=int(f[10]), hold_ms=int(f[11]),
            enabled=f[12] == "1", pos_known=f[13] == "1",
            present=f[14] == "1")

    def stepper_zero(self, ch: str) -> None:
        """Call with the plunger at its home stop. Counted steps are the
        only position feedback there is, so do this every session."""
        self.send(f"STPZERO,{ch}")

    def stepper_aspirate(self, ch: str, steps: int) -> None:
        self.send(f"STPASP,{ch},{int(steps)}")

    def stepper_dispense(self, ch: str, steps: int) -> None:
        self.send(f"STPDIS,{ch},{int(steps)}")

    def stepper_move_to(self, ch: str, pos: int) -> None:
        self.send(f"STPGOTO,{ch},{int(pos)}")

    def stepper_volume_ul(self, ch: str, microlitres: float,
                          aspirate: bool = True) -> None:
        self.send(f"STPVOL,{ch},{int(round(microlitres * 1000))},"
                  f"{1 if aspirate else 0}")

    def stepper_stop(self, ch: str) -> None:
        self.send(f"STPSTOP,{ch}")

    def stepper_speed(self, ch: str, sps: int) -> None:
        self.send(f"STPSPS,{ch},{int(sps)}")

    def stepper_accel(self, ch: str, sps2: int) -> None:
        self.send(f"STPACC,{ch},{int(sps2)}")

    def stepper_direction(self, ch: str, aspirate_sign: int) -> None:
        self.send(f"STPDIR,{ch},{int(aspirate_sign)}")

    def stepper_limits(self, ch: str, lo: int, hi: int) -> None:
        self.send(f"STPLIM,{ch},{int(lo)},{int(hi)}")

    def stepper_calibrate(self, ch: str, nl_per_step: int) -> None:
        """Nanolitres of plunger displacement per step. Confirm
        gravimetrically the way the solenoid table was built."""
        self.send(f"STPCAL,{ch},{int(nl_per_step)}")

    def stepper_hold(self, ch: str, ms: int) -> None:
        self.send(f"STPHOLD,{ch},{int(ms)}")

    def stepper_set_present(self, ch: str, present: bool) -> None:
        self.send(f"STPPRESENT,{ch},{1 if present else 0}", reply_key="STEP")

    def stepper_enable(self, ch: str, on: bool) -> None:
        self.send(f"{'STPON' if on else 'STPOFF'},{ch}")

    # ---------------- block switch ----------------

    def block_switch(self, *, block_id: int, spouts, vac_steps: int,
                     vac_sps: int = 400, pre_ms: int = 250,
                     vac_dwell_ms: int = 500, fill_dwell_ms: int = 500,
                     post_ms: int = 250, sequential: bool = True,
                     cycles: int = 2, do_return: bool = False) -> None:
        """
        Purge and refill the spout dead space.

        spouts: [{"ch": "l", "solenoid": 2, "fill_ms": 60,
                  "pulses": 3, "gap_ms": 150}, ...]

        Returns as soon as the firmware accepts it. Wait for the
        BLOCK_END event before the next trial; the firmware refuses a
        trial meanwhile, but the host should not sequence itself off an
        error.
        """
        self.send(f"BSNEW,{int(block_id)}")
        for s in spouts:
            self.send(f"BSSPOUT,{s['ch']},{int(s['solenoid'])},"
                      f"{int(s['fill_ms'])},{int(s.get('pulses', 3))},"
                      f"{int(s.get('gap_ms', 150))}")
        self.send(f"BSVAC,{int(vac_steps)},{int(vac_sps)}")
        self.send(f"BSTIME,{int(pre_ms)},{int(vac_dwell_ms)},"
                  f"{int(fill_dwell_ms)},{int(post_ms)}")
        self.send(f"BSMODE,{1 if sequential else 0}")
        self.send(f"BSCYCLES,{int(cycles)}")
        self.send(f"BSRETURN,{1 if do_return else 0}")
        self.send("BSGO")

    def block_abort(self) -> None:
        self.send("BSABORT")

    def block_state(self) -> BlockSwitchStatus:
        f = self.send("BSSTATE", expect_ack=False, reply_key="BLOCK")[0]
        return BlockSwitchStatus(
            block_id=int(f[1]), state=int(f[2]), cycle=int(f[3]),
            spout=f[4], outcome=int(f[5]), vac_steps=int(f[6]),
            vac_sps=int(f[7]), pre_ms=int(f[8]), vac_dwell_ms=int(f[9]),
            fill_dwell_ms=int(f[10]), post_ms=int(f[11]),
            sequential=f[12] == "1", cycles=int(f[13]),
            do_return=f[14] == "1")

    # ---------------- readiness ----------------

    def readiness_report(self, spouts: Sequence[str] = ("l", "r")) -> dict:
        """
        Check everything a session needs before an animal is on the rig.
        Returns {"ready": bool, "problems": [...], "warnings": [...]}.
        """
        problems: list[str] = []
        warnings: list[str] = []

        for ch in spouts:
            sv = self.servo_read(ch)
            if not sv.present:
                # Not on this rig. Demanding a calibration for hardware
                # that does not exist is the friction this flag removes.
                continue
            if not sv.extend_set:
                problems.append(f"Servo {ch.upper()}: extended position never set "
                                f"(SVEXT). Trials will refuse to start.")
            if not sv.pos_known:
                warnings.append(f"Servo {ch.upper()}: position unverified. "
                                f"Run servo_init() before trusting angles.")
            if sv.zero_angle <= 2 or sv.zero_angle >= 178:
                warnings.append(
                    f"Servo {ch.upper()}: retracted angle {sv.zero_angle} is at the "
                    f"edge of travel; many servos jam against an internal "
                    f"stop there and buzz.")

            lk = self.lick_read(ch)
            if not lk.present:
                problems.append(f"Spout {ch.upper()} is in use but its lick "
                                f"sensor is marked absent.")
            elif not lk.calibrated:
                problems.append(f"Lick sensor {ch.upper()}: not calibrated.")
            elif not lk.enabled:
                warnings.append(f"Lick sensor {ch.upper()}: calibrated but disabled.")
            elif lk.snr is not None and lk.snr < 4:
                warnings.append(
                    f"Lick sensor {ch.upper()}: trigger threshold is only "
                    f"{lk.snr:.1f}x baseline noise. Expect false licks.")

        for sol in self.solenoid_get_all():
            if not sol.present:
                continue
            if sol.spout == "NONE" or not sol.liquid or sol.liquid == "UNSET":
                warnings.append(f"Solenoid {sol.index}: identity not set.")
            elif sol.nl_per_ms == 0:
                warnings.append(f"Solenoid {sol.index} ({sol.liquid}): not "
                                f"calibrated; dispense by volume unavailable.")

        for ch in spouts:
            try:
                st = self.stepper_read(ch)
            except Exception:
                continue
            if not st.present:
                warnings.append(
                    f"Spout {ch.upper()}: no syringe pump configured. Block "
                    f"switches on this spout will be refused.")
            elif not st.pos_known:
                problems.append(
                    f"Pump {ch.upper()}: not zeroed. Run stepper_zero() with "
                    f"the plunger at its home stop \u2014 counted steps are "
                    f"the only position feedback there is.")
            elif st.nl_per_step == 0:
                warnings.append(f"Pump {ch.upper()}: not calibrated.")

        if self.clock_offset is None:
            warnings.append("Clock never synchronised.")

        return {"ready": not problems, "problems": problems,
                "warnings": warnings}
