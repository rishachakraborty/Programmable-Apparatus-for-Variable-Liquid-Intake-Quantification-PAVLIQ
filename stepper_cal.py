"""
stepper_cal.py — volume to steps for the syringe pumps.

Why steps and not time: a stepper's displacement is set by the number of
steps, full stop. Time is only steps divided by speed, so a table keyed
on time silently becomes wrong the moment anyone changes the speed. The
table stores steps; the GUI shows the time that implies at whatever
speed is currently configured.

Why syringe size is a column: plunger displacement per step is fixed by
the lead screw, but the VOLUME that displacement moves scales with the
barrel's cross-section. A 30 mL barrel is wider than a 20 mL one, so the
same steps deliver more. Measuring both and interpolating covers the
sizes in between.

Interpolation is two-stage: within a syringe size, between measured
volumes; then between the two nearest syringe sizes. Anything outside
the measured range is reported as extrapolated rather than used quietly.
"""

from __future__ import annotations

import json
from bisect import bisect_left
from dataclasses import dataclass, field
from typing import Optional

# Nominal barrel inner diameters, mm. Used only to sanity-check that a
# measured table scales roughly with area, never to replace measurement.
NOMINAL_BORE_MM = {1.0: 4.7, 3.0: 8.7, 5.0: 12.1, 10.0: 14.5,
                   20.0: 19.1, 30.0: 21.7, 50.0: 26.7, 60.0: 26.7}


def _interp(pairs, x) -> tuple:
    """Linear interpolation over sorted (x, y). Returns (y, quality)."""
    if not pairs:
        return (None, "none")
    if len(pairs) == 1:
        x0, y0 = pairs[0]
        return ((y0 * x / x0, "extrapolated") if x0 else (None, "none"))

    xs = [p[0] for p in pairs]
    if x in xs:
        return (pairs[xs.index(x)][1], "exact")

    i = bisect_left(xs, x)
    if i == 0:
        (x0, y0), (x1, y1) = pairs[0], pairs[1]
    elif i >= len(pairs):
        (x0, y0), (x1, y1) = pairs[-2], pairs[-1]
    else:
        (x0, y0), (x1, y1) = pairs[i - 1], pairs[i]

    if x1 == x0:
        return (y0, "exact")
    y = y0 + (y1 - y0) * (x - x0) / (x1 - x0)
    inside = xs[0] <= x <= xs[-1]
    return (y, "interpolated" if inside else "extrapolated")


@dataclass
class StepperTable:
    """
    Measured (syringe_ml, volume_ul, steps) triples for one pump.

    Points for different syringe sizes coexist in one table; lookups pick
    the relevant curves automatically.
    """
    points: list = field(default_factory=list)
    label: str = ""
    syringe_ml: float = 20.0        # what is currently fitted

    # ---- editing ----

    def set_points(self, triples) -> None:
        clean: dict = {}
        for row in triples:
            try:
                ml, ul, steps = (float(row[0]), float(row[1]), float(row[2]))
            except (TypeError, ValueError, IndexError):
                continue
            if ml > 0 and ul > 0 and steps > 0:
                clean[(round(ml, 3), round(ul, 4))] = steps
        self.points = sorted((ml, ul, st) for (ml, ul), st in clean.items())

    def add_point(self, ml: float, ul: float, steps: float) -> None:
        self.set_points(self.points + [(ml, ul, steps)])

    @property
    def sizes(self) -> list:
        return sorted({p[0] for p in self.points})

    def curve_for(self, ml: float) -> list:
        return sorted((p[1], p[2]) for p in self.points if p[0] == ml)

    def __len__(self) -> int:
        return len(self.points)

    # ---- lookup ----

    def steps_for(self, ul: float, syringe_ml: Optional[float] = None) -> tuple:
        """
        Steps needed to move this volume. Returns (steps, quality).

        Two stages: interpolate volume within each bracketing syringe
        size, then interpolate between those two answers.
        """
        ml = self.syringe_ml if syringe_ml is None else syringe_ml
        sizes = self.sizes
        if not sizes:
            return (None, "none")

        if ml in sizes:
            steps, q = _interp(self.curve_for(ml), ul)
            return ((int(round(steps)), q) if steps else (None, "none"))

        if len(sizes) == 1:
            # One size measured. Scale by barrel area if both bores are
            # known, otherwise refuse rather than pretend.
            steps, q = _interp(self.curve_for(sizes[0]), ul)
            if steps is None:
                return (None, "none")
            b0, b1 = NOMINAL_BORE_MM.get(sizes[0]), NOMINAL_BORE_MM.get(ml)
            if b0 and b1:
                steps *= (b0 / b1) ** 2
            return (int(round(steps)), "extrapolated")

        # Bracket the requested size and blend the two curves.
        lo = max([s for s in sizes if s <= ml], default=sizes[0])
        hi = min([s for s in sizes if s >= ml], default=sizes[-1])
        s_lo, q_lo = _interp(self.curve_for(lo), ul)
        s_hi, q_hi = _interp(self.curve_for(hi), ul)
        if s_lo is None or s_hi is None:
            return (None, "none")

        if hi == lo:
            steps = s_lo
        else:
            f = (ml - lo) / (hi - lo)
            steps = s_lo + (s_hi - s_lo) * f

        inside_size = sizes[0] <= ml <= sizes[-1]
        quality = "interpolated"
        if "extrapolated" in (q_lo, q_hi) or not inside_size:
            quality = "extrapolated"
        elif q_lo == q_hi == "exact" and lo == hi:
            quality = "exact"
        return (int(round(steps)), quality)

    def ul_for_steps(self, steps: float,
                     syringe_ml: Optional[float] = None) -> Optional[float]:
        ml = self.syringe_ml if syringe_ml is None else syringe_ml
        sizes = self.sizes
        if not sizes:
            return None
        near = min(sizes, key=lambda s: abs(s - ml))
        inv = sorted((st, ul) for ul, st in self.curve_for(near))
        val, _q = _interp(inv, steps)
        return val

    def nl_per_step(self, syringe_ml: Optional[float] = None) -> Optional[int]:
        """
        Single slope for the firmware, from the two largest measured
        points. The board only needs it for manual jogging; every purge
        during a session is sent as an explicit step count from the full
        table, so the curve is never lost to the simpler model.
        """
        ml = self.syringe_ml if syringe_ml is None else syringe_ml
        sizes = self.sizes
        if not sizes:
            return None
        near = min(sizes, key=lambda s: abs(s - ml))
        curve = self.curve_for(near)
        if len(curve) < 2:
            if len(curve) == 1:
                ul, st = curve[0]
                return int(round(ul * 1000.0 / st)) if st else None
            return None
        (u0, s0), (u1, s1) = curve[-2], curve[-1]
        if s1 == s0:
            return None
        return int(round((u1 - u0) * 1000.0 / (s1 - s0)))

    def seconds_for(self, ul: float, steps_per_sec: int,
                    syringe_ml: Optional[float] = None) -> Optional[float]:
        """Time the move will take at a given speed. Derived, never stored:
        change the speed and this changes, the step count does not."""
        steps, _q = self.steps_for(ul, syringe_ml)
        if steps is None or steps_per_sec <= 0:
            return None
        return steps / float(steps_per_sec)

    def warnings(self) -> list:
        out = []
        if not self.points:
            out.append("no points measured")
            return out
        for ml in self.sizes:
            curve = self.curve_for(ml)
            if len(curve) == 1:
                out.append(f"{ml:g} mL has one point; everything else for "
                           f"that syringe is a straight line through zero")
            for i in range(1, len(curve)):
                if curve[i][1] <= curve[i - 1][1]:
                    out.append(f"{ml:g} mL: {curve[i][0]:g} \u00b5L needs no "
                               f"more steps than {curve[i-1][0]:g} \u00b5L")
        if self.syringe_ml not in self.sizes and len(self.sizes) == 1:
            out.append(f"fitted for a {self.syringe_ml:g} mL syringe but only "
                       f"{self.sizes[0]:g} mL was measured; the conversion is "
                       f"scaled by nominal bore, not measured")
        return out

    def to_json(self) -> dict:
        return {"label": self.label, "syringe_ml": self.syringe_ml,
                "points": [list(p) for p in self.points]}

    @staticmethod
    def from_json(d: dict) -> "StepperTable":
        t = StepperTable(label=d.get("label", ""),
                         syringe_ml=float(d.get("syringe_ml", 20.0)))
        t.set_points(d.get("points", []))
        return t


@dataclass
class StepperCalibration:
    """One table per pump, keyed by spout, plus a shared mode."""
    shared: bool = True
    shared_table: StepperTable = field(default_factory=StepperTable)
    tables: dict = field(default_factory=dict)

    def __post_init__(self):
        for ch in ("l", "c", "r"):
            self.tables.setdefault(ch, StepperTable(label=f"pump {ch}"))

    def table_for(self, ch: str) -> StepperTable:
        if self.shared:
            return self.shared_table
        return self.tables.setdefault(ch, StepperTable(label=f"pump {ch}"))

    def steps_for(self, ch: str, ul: float,
                  syringe_ml: Optional[float] = None) -> tuple:
        return self.table_for(ch).steps_for(ul, syringe_ml)

    def to_json(self) -> dict:
        return {"shared": self.shared,
                "shared_table": self.shared_table.to_json(),
                "tables": {k: v.to_json() for k, v in self.tables.items()}}

    @staticmethod
    def from_json(d: dict) -> "StepperCalibration":
        sc = StepperCalibration(shared=bool(d.get("shared", True)))
        sc.shared_table = StepperTable.from_json(d.get("shared_table", {}))
        for k, v in (d.get("tables") or {}).items():
            sc.tables[k] = StepperTable.from_json(v)
        return sc

    def push_to_board(self, link, spouts=("l", "c", "r")) -> list:
        notes = []
        for ch in spouts:
            t = self.table_for(ch)
            nl = t.nl_per_step()
            if nl is None:
                notes.append(f"pump {ch}: nothing to send, table is empty")
                continue
            try:
                link.stepper_calibrate(ch, nl)
                notes.append(f"pump {ch}: {nl} nL/step "
                             f"({t.syringe_ml:g} mL syringe)")
            except Exception as e:
                notes.append(f"pump {ch}: failed, {e}")
        return notes
