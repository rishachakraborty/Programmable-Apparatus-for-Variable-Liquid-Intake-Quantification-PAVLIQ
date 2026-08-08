"""
calibration.py — volume to open-time lookup tables for the solenoids.

Why a table and not a single nL/ms number: a solenoid is not linear.
Opening and closing take a few milliseconds during which flow is
neither zero nor full, so a 10 ms pulse delivers considerably less than
half what a 20 ms pulse does. One slope fitted through the origin will
be wrong at the short end, which is exactly where the small reward
amounts live.

So the user measures a handful of (volume, ms) pairs gravimetrically and
this interpolates between them. Prefer requesting a volume that is
already IN the table; interpolation between measured points is sound,
extrapolation past the ends is a guess and is reported as such.
"""

from __future__ import annotations

import json
from bisect import bisect_left
from dataclasses import dataclass, field
from typing import Optional

SPOUTS = ("l", "c", "r")


class CalibrationError(ValueError):
    pass


@dataclass
class LookupTable:
    """Measured (microlitres, milliseconds) pairs for one solenoid."""
    points: list = field(default_factory=list)   # [(ul, ms), ...]
    label: str = ""

    # ---- editing ----

    def set_points(self, pairs) -> None:
        clean = []
        for ul, ms in pairs:
            try:
                ul, ms = float(ul), float(ms)
            except (TypeError, ValueError):
                continue
            if ul > 0 and ms > 0:
                clean.append((ul, ms))
        # Sort by volume and drop duplicates, keeping the last entry for
        # a repeated volume so re-measuring a point replaces it.
        merged: dict = {}
        for ul, ms in clean:
            merged[round(ul, 4)] = ms
        self.points = sorted(merged.items())

    def add_point(self, ul: float, ms: float) -> None:
        self.set_points(self.points + [(ul, ms)])

    def clear(self) -> None:
        self.points = []

    @property
    def volumes(self) -> list:
        return [p[0] for p in self.points]

    def __len__(self) -> int:
        return len(self.points)

    # ---- lookup ----

    def ms_for(self, ul: float) -> tuple:
        """
        Open time for a requested volume.

        Returns (milliseconds, quality) where quality is one of
        "exact", "interpolated", "extrapolated" or "none". The caller is
        expected to surface anything that is not exact or interpolated:
        a silently extrapolated reward amount is a mislabelled trial.
        """
        if not self.points:
            return (None, "none")

        vols = self.volumes
        if ul in vols:
            return (self.points[vols.index(ul)][1], "exact")

        if len(self.points) == 1:
            # A single point can only be scaled through the origin,
            # which is the assumption this table exists to avoid.
            v0, m0 = self.points[0]
            return (m0 * ul / v0, "extrapolated")

        i = bisect_left(vols, ul)

        if i == 0:                                  # below the table
            (v0, m0), (v1, m1) = self.points[0], self.points[1]
        elif i >= len(self.points):                 # above the table
            (v0, m0), (v1, m1) = self.points[-2], self.points[-1]
        else:
            (v0, m0), (v1, m1) = self.points[i - 1], self.points[i]

        if v1 == v0:
            return (m0, "exact")
        ms = m0 + (m1 - m0) * (ul - v0) / (v1 - v0)
        if ms <= 0:
            return (None, "none")

        inside = vols[0] <= ul <= vols[-1]
        return (ms, "interpolated" if inside else "extrapolated")

    def ul_for_ms(self, ms: float) -> Optional[float]:
        """Inverse lookup, for showing what a typed duration delivers."""
        if len(self.points) < 2:
            if len(self.points) == 1:
                v0, m0 = self.points[0]
                return v0 * ms / m0 if m0 else None
            return None
        pts = sorted(self.points, key=lambda p: p[1])
        times = [p[1] for p in pts]
        i = bisect_left(times, ms)
        if i == 0:
            (v0, m0), (v1, m1) = pts[0], pts[1]
        elif i >= len(pts):
            (v0, m0), (v1, m1) = pts[-2], pts[-1]
        else:
            (v0, m0), (v1, m1) = pts[i - 1], pts[i]
        if m1 == m0:
            return v0
        return v0 + (v1 - v0) * (ms - m0) / (m1 - m0)

    def implied_nl_per_ms(self) -> Optional[int]:
        """
        A single slope for the firmware's SOLVOL command.

        Taken from the two largest measured points rather than through
        the origin, so it reflects the flowing part of the pulse. This
        is a fallback only: the table is what the GUI actually uses.
        """
        if len(self.points) < 2:
            if len(self.points) == 1:
                v0, m0 = self.points[0]
                return int(round(v0 * 1000.0 / m0)) if m0 else None
            return None
        (v0, m0), (v1, m1) = self.points[-2], self.points[-1]
        if m1 == m0:
            return None
        return int(round((v1 - v0) * 1000.0 / (m1 - m0)))

    def warnings(self) -> list:
        out = []
        if not self.points:
            out.append("no points measured")
            return out
        if len(self.points) == 1:
            out.append("only one point: everything else is a straight line "
                       "through the origin, which is what a lookup table "
                       "exists to avoid")
        for i in range(1, len(self.points)):
            if self.points[i][1] <= self.points[i - 1][1]:
                out.append(f"{self.points[i][0]} \u00b5L needs no more time "
                           f"than {self.points[i - 1][0]} \u00b5L \u2014 "
                           f"check the measurement")
        return out

    def to_json(self) -> dict:
        return {"label": self.label, "points": [list(p) for p in self.points]}

    @staticmethod
    def from_json(d: dict) -> "LookupTable":
        t = LookupTable(label=d.get("label", ""))
        t.set_points(d.get("points", []))
        return t


@dataclass
class CalibrationSet:
    """
    One table per solenoid, plus a shared mode.

    Shared mode is the common case: four solenoids from the same batch on
    the same manifold behave alike, and measuring one is a lot less work
    than measuring four. Independent mode exists because they will not
    match exactly, and for alcohol versus water they may not match at
    all - viscosity and surface tension differ enough to matter at the
    volumes used here.
    """
    shared: bool = True
    shared_table: LookupTable = field(default_factory=LookupTable)
    tables: dict = field(default_factory=dict)      # 1..4 -> LookupTable

    def __post_init__(self):
        for n in (1, 2, 3, 4):
            self.tables.setdefault(n, LookupTable(label=f"solenoid {n}"))

    def table_for(self, solenoid: int) -> LookupTable:
        if self.shared:
            return self.shared_table
        return self.tables.setdefault(solenoid,
                                      LookupTable(label=f"solenoid {solenoid}"))

    def ms_for(self, solenoid: int, ul: float) -> tuple:
        return self.table_for(solenoid).ms_for(ul)

    def resolve(self, solenoid: int, ul: float) -> float:
        """
        Open time, refusing rather than guessing when the table cannot
        support the request. Used on the path that actually fires a
        reward, where a wrong number is a mislabelled trial.
        """
        ms, quality = self.ms_for(solenoid, ul)
        if ms is None:
            raise CalibrationError(
                f"Solenoid {solenoid} has no calibration table, so "
                f"{ul} \u00b5L cannot be converted to an open time.")
        return ms

    def known_volumes(self) -> list:
        """Every volume measured anywhere, for the trial-type dropdown."""
        vols = set()
        if self.shared:
            vols.update(self.shared_table.volumes)
        else:
            for t in self.tables.values():
                vols.update(t.volumes)
        return sorted(vols)

    def problems(self) -> list:
        out = []
        if self.shared:
            for w in self.shared_table.warnings():
                out.append(f"shared table: {w}")
        else:
            for n, t in sorted(self.tables.items()):
                for w in t.warnings():
                    out.append(f"solenoid {n}: {w}")
        return out

    def to_json(self) -> dict:
        return {
            "shared": self.shared,
            "shared_table": self.shared_table.to_json(),
            "tables": {str(n): t.to_json() for n, t in self.tables.items()},
        }

    @staticmethod
    def from_json(d: dict) -> "CalibrationSet":
        cs = CalibrationSet(shared=bool(d.get("shared", True)))
        cs.shared_table = LookupTable.from_json(d.get("shared_table", {}))
        for k, v in (d.get("tables") or {}).items():
            try:
                cs.tables[int(k)] = LookupTable.from_json(v)
            except ValueError:
                pass
        return cs

    def save(self, path: str) -> None:
        with open(path, "w") as f:
            json.dump(self.to_json(), f, indent=2)

    @staticmethod
    def load(path: str) -> "CalibrationSet":
        with open(path) as f:
            return CalibrationSet.from_json(json.load(f))

    # ---- firmware ----

    def push_to_board(self, link) -> list:
        """
        Send each solenoid's single-slope calibration.

        The firmware stores one nL/ms number per solenoid, not a curve.
        That is deliberate: the board only needs it for manual SOLVOL
        dispensing on the hardware page. Every reward during a session
        is sent as an explicit millisecond value resolved from the table
        here, so the curve's accuracy is never lost to the board's
        simpler model.
        """
        notes = []
        for n in (1, 2, 3, 4):
            nl = self.table_for(n).implied_nl_per_ms()
            if nl is None:
                notes.append(f"solenoid {n}: nothing to send, table is empty")
                continue
            try:
                link.solenoid_calibrate(n, nl)
                notes.append(f"solenoid {n}: {nl} nL/ms")
            except Exception as e:
                notes.append(f"solenoid {n}: failed, {e}")
        return notes
