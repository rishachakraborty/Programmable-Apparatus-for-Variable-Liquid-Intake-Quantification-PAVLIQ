"""
settings.py — hardware settings that survive closing the application.

The problem this solves: spout angles, lick thresholds, pump zeroes and
solenoid identities all live in the board's RAM. Disconnect and they are
gone, so a five minute break costs a full recalibration.

What is stored here is a STARTING POINT, not a substitute for
calibrating. Lick baselines in particular drift with humidity and saliva
within a single session, and restoring yesterday's numbers without
re-measuring will produce a detector that is quietly wrong. The GUI
labels restored values as restored, and `age_days` is exposed so it can
say how stale they are.

Written atomically: a crash mid-write leaves the previous file intact
rather than a truncated one, because a corrupt settings file that loads
as "no calibration" is worse than an old one.
"""

from __future__ import annotations

import json
import os
import tempfile
import time
from dataclasses import asdict, dataclass, field
from typing import Optional

APP_DIR_NAME = "mouse_task"
FILE_NAME = "hardware_settings.json"
SCHEMA = 1


def default_dir() -> str:
    base = os.environ.get("XDG_CONFIG_HOME")
    if not base:
        base = os.path.join(os.path.expanduser("~"), ".config")
    return os.path.join(base, APP_DIR_NAME)


def default_path() -> str:
    return os.path.join(default_dir(), FILE_NAME)


@dataclass
class SpoutSettings:
    present: bool = True
    retracted_angle: Optional[int] = None
    drinking_angle: Optional[int] = None
    slew_deg_s: int = 400
    soft_min: int = 0
    soft_max: int = 180
    extend_dir: int = 1
    idle_detach_ms: int = 500


@dataclass
class LickSettings:
    present: bool = True
    baseline: Optional[float] = None
    sd: Optional[float] = None
    on_delta: Optional[float] = None
    off_delta: Optional[float] = None
    polarity: int = -1
    min_on_ms: int = 5
    min_off_ms: int = 25
    refractory_ms: int = 15


@dataclass
class PumpSettings:
    present: bool = False
    sps: int = 600
    accel: int = 3000
    nl_per_step: int = 180
    soft_min: int = 0
    soft_max: int = 40000
    aspirate_sign: int = 1
    syringe_ml: float = 20.0


@dataclass
class SolenoidSettings:
    present: bool = True
    liquid: str = ""
    spout: str = "l"


@dataclass
class HardwareSettings:
    schema: int = SCHEMA
    saved_at: float = 0.0
    port: str = ""
    firmware: str = ""

    spouts: dict = field(default_factory=dict)      # "l"/"c"/"r"
    licks: dict = field(default_factory=dict)
    pumps: dict = field(default_factory=dict)
    solenoids: dict = field(default_factory=dict)   # "1".."8"

    n_solenoids: int = 4
    calibration: dict = field(default_factory=dict)  # CalibrationSet json
    stepper_calibration: dict = field(default_factory=dict)
    purge: dict = field(default_factory=dict)
    theme: str = "light"
    last_data_dir: str = ""

    # ---- convenience ----

    def spout(self, ch: str) -> SpoutSettings:
        d = self.spouts.get(ch)
        return SpoutSettings(**d) if d else SpoutSettings()

    def lick(self, ch: str) -> LickSettings:
        d = self.licks.get(ch)
        return LickSettings(**d) if d else LickSettings()

    def pump(self, ch: str) -> PumpSettings:
        d = self.pumps.get(ch)
        return PumpSettings(**d) if d else PumpSettings()

    def solenoid(self, n: int) -> SolenoidSettings:
        d = self.solenoids.get(str(n))
        return SolenoidSettings(**d) if d else SolenoidSettings()

    def set_spout(self, ch: str, s: SpoutSettings) -> None:
        self.spouts[ch] = asdict(s)

    def set_lick(self, ch: str, s: LickSettings) -> None:
        self.licks[ch] = asdict(s)

    def set_pump(self, ch: str, s: PumpSettings) -> None:
        self.pumps[ch] = asdict(s)

    def set_solenoid(self, n: int, s: SolenoidSettings) -> None:
        self.solenoids[str(n)] = asdict(s)

    @property
    def age_days(self) -> Optional[float]:
        if not self.saved_at:
            return None
        return (time.time() - self.saved_at) / 86400.0

    def staleness_note(self) -> str:
        age = self.age_days
        if age is None:
            return "nothing saved yet"
        if age < 1 / 24:
            return "saved less than an hour ago"
        if age < 1:
            return f"saved {age * 24:.0f} hours ago"
        return (f"saved {age:.0f} days ago \u2014 re-run the lick calibration "
                f"before trusting it, baselines drift")

    # ---- io ----

    def save(self, path: Optional[str] = None) -> str:
        path = path or default_path()
        os.makedirs(os.path.dirname(path), exist_ok=True)
        self.saved_at = time.time()
        self.schema = SCHEMA
        # Atomic: write beside the target, then replace. A corrupt file
        # that loads as "no calibration" is worse than an old one.
        fd, tmp = tempfile.mkstemp(dir=os.path.dirname(path), suffix=".tmp")
        try:
            with os.fdopen(fd, "w") as f:
                json.dump(asdict(self), f, indent=2)
            os.replace(tmp, path)
        except Exception:
            if os.path.exists(tmp):
                os.unlink(tmp)
            raise
        return path

    @staticmethod
    def load(path: Optional[str] = None) -> "HardwareSettings":
        path = path or default_path()
        if not os.path.exists(path):
            return HardwareSettings()
        try:
            with open(path) as f:
                d = json.load(f)
        except (json.JSONDecodeError, OSError):
            # Never let a damaged file stop the application starting.
            return HardwareSettings()
        if d.get("schema", 0) != SCHEMA:
            # A future version can migrate here. For now, keep only the
            # fields that still exist rather than guessing at old ones.
            d = {k: v for k, v in d.items()
                 if k in HardwareSettings.__dataclass_fields__}
            d["schema"] = SCHEMA
        known = HardwareSettings.__dataclass_fields__
        return HardwareSettings(**{k: v for k, v in d.items() if k in known})


# ---------------------------------------------------------------------
# Board <-> settings
# ---------------------------------------------------------------------

def capture_from_board(link, settings: HardwareSettings,
                       spouts=("l", "c", "r")) -> list:
    """Read the board's current state into settings. Returns notes."""
    notes = []
    settings.port = link.port_name or ""
    settings.firmware = link.firmware_id or ""

    for ch in spouts:
        try:
            sv = link.servo_read(ch)
            settings.set_spout(ch, SpoutSettings(
                present=getattr(sv, "present", True),
                retracted_angle=sv.zero_angle,
                drinking_angle=sv.extend_angle if sv.extend_set else None,
                slew_deg_s=sv.slew, soft_min=sv.soft_min,
                soft_max=sv.soft_max, extend_dir=sv.extend_dir,
                idle_detach_ms=sv.idle_detach_ms))
        except Exception as e:
            notes.append(f"spout {ch}: {e}")

        try:
            lk = link.lick_read(ch)
            if lk.calibrated:
                settings.set_lick(ch, LickSettings(
                    present=getattr(lk, "present", True),
                    baseline=lk.baseline, sd=lk.sd, on_delta=lk.on_delta,
                    off_delta=lk.off_delta, polarity=lk.polarity))
        except Exception as e:
            notes.append(f"lick {ch}: {e}")

        try:
            st = link.stepper_read(ch)
            settings.set_pump(ch, PumpSettings(
                present=st.present, sps=st.sps, accel=st.accel,
                nl_per_step=st.nl_per_step, soft_min=st.soft_min,
                soft_max=st.soft_max, aspirate_sign=st.aspirate_sign,
                syringe_ml=settings.pump(ch).syringe_ml))
        except Exception as e:
            notes.append(f"pump {ch}: {e}")

    try:
        for sol in link.solenoid_get_all():
            settings.set_solenoid(sol.index, SolenoidSettings(
                present=getattr(sol, "present", True),
                liquid=sol.liquid if sol.liquid != "UNSET" else "",
                spout=sol.spout.lower() if sol.spout != "NONE" else "l"))
    except Exception as e:
        notes.append(f"solenoids: {e}")

    return notes


def restore_to_board(link, settings: HardwareSettings,
                     spouts=("l", "c", "r"),
                     include_lick: bool = True) -> list:
    """
    Push saved settings back to the board.

    Positions, limits and identities restore cleanly: they describe the
    apparatus, which has not changed. Lick thresholds are restored only
    when asked for, and the caller is expected to say plainly that they
    are yesterday's numbers.
    """
    notes = []
    for ch in spouts:
        sp = settings.spout(ch)
        try:
            link.servo_set_present(ch, sp.present)
            if not sp.present:
                continue
            link.servo_limits(ch, sp.soft_min, sp.soft_max)
            link.servo_direction(ch, sp.extend_dir)
            link.servo_slew(ch, sp.slew_deg_s)
            link.servo_idle_detach(ch, sp.idle_detach_ms)
            if sp.retracted_angle is not None:
                link.servo_set_retracted(ch, sp.retracted_angle, force=True)
            if sp.drinking_angle is not None:
                link.servo_set_extended(ch, sp.drinking_angle, force=True)
        except Exception as e:
            notes.append(f"spout {ch}: {e}")

        lk = settings.lick(ch)
        try:
            link.lick_set_present(ch, lk.present)
            if include_lick and lk.present and lk.on_delta:
                link.lick_set_thresholds(ch, lk.on_delta, lk.off_delta,
                                         lk.polarity)
                link.lick_timing(lk.min_on_ms, lk.min_off_ms, lk.refractory_ms)
        except Exception as e:
            notes.append(f"lick {ch}: {e}")

        pm = settings.pump(ch)
        try:
            link.stepper_set_present(ch, pm.present)
            if pm.present:
                link.stepper_speed(ch, pm.sps)
                link.stepper_accel(ch, pm.accel)
                link.stepper_calibrate(ch, pm.nl_per_step)
                link.stepper_limits(ch, pm.soft_min, pm.soft_max)
                link.stepper_direction(ch, pm.aspirate_sign)
        except Exception as e:
            notes.append(f"pump {ch}: {e}")

    for n in range(1, settings.n_solenoids + 1):
        s = settings.solenoid(n)
        try:
            link.solenoid_set_present(n, s.present)
            if s.present and s.liquid:
                link.solenoid_identity(n, s.liquid, s.spout)
        except Exception as e:
            notes.append(f"solenoid {n}: {e}")

    if settings.spouts or settings.solenoids:
        notes.append(
            "Positions and identities restored. Pump zero is NOT restored: "
            "counted steps mean nothing across a power cycle, so zero each "
            "pump at its home stop.")
    return notes
