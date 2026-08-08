"""
gui_calibration.py — lookup table editor, stepper box, module discovery.

Kept out of gui_experiment.py because the hardware page was already the
longest file in the project and these three are self-contained.
"""

from __future__ import annotations

import os
from typing import Optional

from PyQt6.QtCore import Qt, QTimer
from PyQt6.QtGui import QFont
from PyQt6.QtWidgets import (
    QAbstractItemView, QCheckBox, QComboBox, QDoubleSpinBox, QFileDialog,
    QGridLayout, QGroupBox, QHBoxLayout, QLabel, QMessageBox, QPlainTextEdit,
    QPushButton, QSizePolicy, QSpinBox, QTableWidget, QTableWidgetItem,
    QVBoxLayout, QWidget,
)

from calibration import CalibrationSet, LookupTable
from stepper_cal import StepperCalibration, StepperTable
from theme import btn_normal, btn_ready, c as tc

SPOUT_NAMES = {"l": "Left", "c": "Center", "r": "Right"}


def _mono(sz=9) -> QFont:
    f = QFont("Menlo", sz)
    f.setStyleHint(QFont.StyleHint.Monospace)
    return f


def _btn(text, fn) -> QPushButton:
    b = QPushButton(text)
    b.clicked.connect(fn)
    return b


# =====================================================================

class LookupTableWidget(QGroupBox):
    """
    Editable (volume, open time) pairs for one solenoid.

    Rows are added by hand from gravimetric measurements. The preview
    line underneath answers the question the table exists for: what does
    the rig actually do if a trial asks for this volume.
    """

    def __init__(self, title: str, table: LookupTable, on_change=None):
        super().__init__(title)
        self.table = table
        self.on_change = on_change
        v = QVBoxLayout(self)

        self.grid = QTableWidget(0, 2)
        self.grid.setHorizontalHeaderLabels(["Volume \u00b5L", "Open time ms"])
        self.grid.verticalHeader().setVisible(False)
        self.grid.horizontalHeader().setStretchLastSection(True)
        self.grid.setColumnWidth(0, 130)
        self.grid.verticalHeader().setDefaultSectionSize(28)
        self.grid.setMinimumHeight(190)
        self.grid.setSizePolicy(QSizePolicy.Policy.Expanding,
                                QSizePolicy.Policy.Expanding)
        self.grid.setSelectionBehavior(
            QAbstractItemView.SelectionBehavior.SelectRows)
        self.grid.itemChanged.connect(self._pull)
        v.addWidget(self.grid)

        row = QHBoxLayout()
        row.addWidget(_btn("Add row", self.add_row))
        row.addWidget(_btn("Remove selected", self.remove_selected))
        row.addStretch()
        v.addLayout(row)

        test = QHBoxLayout()
        self.probe = QDoubleSpinBox()
        self.probe.setRange(0.01, 500.0)
        self.probe.setDecimals(2)
        self.probe.setValue(3.0)
        self.probe.setSuffix(" \u00b5L")
        self.probe.valueChanged.connect(self._preview)
        test.addWidget(QLabel("What would"))
        test.addWidget(self.probe)
        test.addWidget(QLabel("give?"))
        self.answer = QLabel("\u2014")
        self.answer.setFont(_mono(10))
        test.addWidget(self.answer, 1)
        v.addLayout(test)

        self.warn = QLabel("")
        self.warn.setWordWrap(True)
        self.warn.setFont(_mono(9))
        v.addWidget(self.warn)

        self.push(table)

    # ---- data ----

    def push(self, table: LookupTable) -> None:
        self.table = table
        self.grid.blockSignals(True)
        self.grid.setRowCount(0)
        for ul, ms in table.points:
            self._append(ul, ms)
        self.grid.blockSignals(False)
        self._preview()

    def _append(self, ul, ms) -> None:
        r = self.grid.rowCount()
        self.grid.insertRow(r)
        self.grid.setItem(r, 0, QTableWidgetItem(f"{ul:g}"))
        self.grid.setItem(r, 1, QTableWidgetItem(f"{ms:g}"))

    def add_row(self) -> None:
        self.grid.blockSignals(True)
        self._append(0, 0)
        self.grid.blockSignals(False)
        self.grid.editItem(self.grid.item(self.grid.rowCount() - 1, 0))

    def remove_selected(self) -> None:
        rows = sorted({i.row() for i in self.grid.selectedIndexes()},
                      reverse=True)
        for r in rows:
            self.grid.removeRow(r)
        self._pull()

    def _pull(self) -> None:
        pairs = []
        for r in range(self.grid.rowCount()):
            a, b = self.grid.item(r, 0), self.grid.item(r, 1)
            if a is None or b is None:
                continue
            try:
                pairs.append((float(a.text()), float(b.text())))
            except ValueError:
                continue
        self.table.set_points(pairs)
        self._preview()
        if self.on_change:
            self.on_change()

    def _preview(self) -> None:
        ms, quality = self.table.ms_for(self.probe.value())
        if ms is None:
            self.answer.setText("no table yet")
            self.answer.setStyleSheet(f"color: {tc('bad')}")
        else:
            colour = {"exact": tc("ok"), "interpolated": tc("ok"),
                      "extrapolated": tc("warn")}.get(quality, tc("bad"))
            note = {"exact": "measured directly",
                    "interpolated": "between measured points",
                    "extrapolated": "OUTSIDE the measured range \u2014 "
                                    "a guess, measure a point near it"}
            self.answer.setText(f"{ms:.1f} ms   ({note.get(quality, quality)})")
            self.answer.setStyleSheet(f"color: {colour}")

        w = self.table.warnings()
        self.warn.setText(("  \u00b7  ".join(w)) if w else "")
        self.warn.setStyleSheet(f"color: {tc('warn')}")


class CalibrationPanel(QGroupBox):
    """Shared or per-solenoid tables, plus block-switch purge amounts."""

    def __init__(self, calibration: CalibrationSet, get_link, on_change=None):
        super().__init__("Volume to open time")
        self.cal = calibration
        self.get_link = get_link
        self.on_change = on_change
        v = QVBoxLayout(self)

        v.addWidget(QLabel(
            "Measure gravimetrically: fire one solenoid a known number of "
            "times into a tared boat and weigh it (1 mg \u2248 1 \u00b5L). A "
            "solenoid is not linear \u2014 opening and closing take a few "
            "milliseconds of partial flow \u2014 so several points beat one "
            "slope, especially at the small volumes."))

        row = QHBoxLayout()
        self.shared = QCheckBox("One table for all four solenoids")
        self.shared.setChecked(calibration.shared)
        self.shared.setToolTip(
            "Off gives each solenoid its own table. Worth doing when the "
            "liquids differ: alcohol and water do not flow alike at these "
            "volumes.")
        self.shared.stateChanged.connect(self._mode_changed)
        row.addWidget(self.shared)
        self.which = QComboBox()
        self.which.addItems([f"Solenoid {n}" for n in (1, 2, 3, 4)])
        self.which.setMinimumWidth(150)
        self.which.currentIndexChanged.connect(self._show_selected)
        row.addWidget(self.which)
        row.addWidget(_btn("Copy this table to all", self.copy_to_all))
        row.addStretch()
        row.addWidget(_btn("Load\u2026", self.load))
        row.addWidget(_btn("Save\u2026", self.save))
        self.b_send = _btn("Send to board", self.send)
        row.addWidget(self.b_send)
        v.addLayout(row)

        self.editor = LookupTableWidget("Measured points",
                                        calibration.shared_table,
                                        on_change=self._changed)
        v.addWidget(self.editor)

        self.status = QLabel("")
        self.status.setFont(_mono(9))
        self.status.setWordWrap(True)
        v.addWidget(self.status)

        purge = QGroupBox("Block switch amounts")
        pg_ = QGridLayout(purge)
        pg_.addWidget(QLabel(
            "<i>Used when the liquid at a spout changes. <b>Aspirate</b> "
            "must exceed the dead volume of tip plus tubing, or the old "
            "solution is still there when the animal licks. <b>Pulses</b> "
            "is how many separate opens make up one refill \u2014 short "
            "bursts wet the tip and clear bubbles better than one long "
            "open, but a tight burst train heats the solenoid, so set "
            "pulses to 1 and raise the volume for a single sustained pour. "
            "<b>Cycles</b> is how many complete aspirate-then-refill "
            "repetitions each purge performs.</i>"),
            0, 0, 1, 8)
        self.vac_ul = QDoubleSpinBox(); self.vac_ul.setRange(1, 2000)
        self.vac_ul.setValue(54.0); self.vac_ul.setSuffix(" \u00b5L")
        self.fill_ul = QDoubleSpinBox(); self.fill_ul.setRange(0.1, 200)
        self.fill_ul.setValue(4.0); self.fill_ul.setSuffix(" \u00b5L")
        self.pulses = QSpinBox(); self.pulses.setRange(1, 20); self.pulses.setValue(3)
        self.cycles = QSpinBox(); self.cycles.setRange(1, 10); self.cycles.setValue(2)
        self.use_pump = QCheckBox("Aspirate with the syringe pump")
        self.use_pump.setChecked(True)
        self.use_pump.setToolTip(
            "When enabled, the pump withdraws the dead volume before the "
            "line is refilled. When disabled, the line is cleared by "
            "dispensing the newly selected reinforcer through it in the "
            "retracted position; this requires no pump but discards more "
            "liquid and reaches a given purity more slowly.")
        self.parallel = QCheckBox("Purge all spouts concurrently")
        self.parallel.setChecked(True)
        self.parallel.setToolTip(
            "Needs one pump per spout. Roughly halves the switch, which "
            "matters when a side swap fires between trials.")
        self.gap_ms = QSpinBox(); self.gap_ms.setRange(0, 5000)
        self.gap_ms.setValue(150); self.gap_ms.setSuffix(" ms")
        self.gap_ms.setToolTip(
            "Rest between pulses. Also the solenoid's cooling time, so do "
            "not drop it to zero on a long burst train.")
        for i, (lab, w) in enumerate([
                ("Aspirate", self.vac_ul), ("Refill per pulse", self.fill_ul),
                ("Pulses", self.pulses), ("Cycles", self.cycles)]):
            pg_.addWidget(QLabel(lab), 1, i * 2)
            pg_.addWidget(w, 1, i * 2 + 1)
        pg_.addWidget(QLabel("Gap between pulses"), 2, 0)
        pg_.addWidget(self.gap_ms, 2, 1)
        pg_.addWidget(self.use_pump, 2, 2, 1, 2)
        pg_.addWidget(self.parallel, 2, 4, 1, 3)
        self.purge_note = QLabel("")
        self.purge_note.setFont(_mono(9))
        self.purge_note.setWordWrap(True)
        pg_.addWidget(self.purge_note, 3, 0, 1, 8)
        for w in (self.vac_ul, self.fill_ul, self.pulses, self.cycles):
            w.valueChanged.connect(self._purge_summary)
        self._purge_summary()
        v.addWidget(purge)

        self._mode_changed()

    # ---- behaviour ----

    def _changed(self):
        if self.on_change:
            self.on_change()

    def _mode_changed(self):
        self.cal.shared = self.shared.isChecked()
        self.which.setEnabled(not self.cal.shared)
        self._show_selected()
        self._changed()

    def _show_selected(self):
        n = self.which.currentIndex() + 1
        table = (self.cal.shared_table if self.cal.shared
                 else self.cal.table_for(n))
        title = ("Measured points \u2014 shared" if self.cal.shared
                 else f"Measured points \u2014 solenoid {n}")
        self.editor.setTitle(title)
        self.editor.push(table)

    def copy_to_all(self):
        src = self.editor.table
        for n in (1, 2, 3, 4):
            self.cal.table_for(n).set_points(list(src.points))
        self.cal.shared_table.set_points(list(src.points))
        self.status.setText("Copied to all four solenoids.")
        self._changed()

    def send(self):
        link = self.get_link()
        if link is None:
            QMessageBox.information(self, "Not connected",
                                    "Connect to the board first.")
            return
        notes = self.cal.push_to_board(link)
        self.status.setText(
            "\n".join(notes) +
            "\n\nThe board stores one slope per solenoid, for manual "
            "dispensing here. Rewards during a session are sent as explicit "
            "millisecond values from the full table, so the curve is not "
            "lost to the board's simpler model.")
        btn_ready(self.b_send)
        QTimer.singleShot(1200, lambda: btn_normal(self.b_send))

    def save(self):
        path, _ = QFileDialog.getSaveFileName(self, "Save calibration", "",
                                              "JSON (*.json)")
        if path:
            self.cal.save(path)
            self.status.setText(f"Saved to {path}")

    def load(self):
        path, _ = QFileDialog.getOpenFileName(self, "Load calibration", "",
                                              "JSON (*.json)")
        if not path:
            return
        loaded = CalibrationSet.load(path)
        self.cal.shared = loaded.shared
        self.cal.shared_table = loaded.shared_table
        self.cal.tables = loaded.tables
        self.shared.setChecked(loaded.shared)
        self._mode_changed()
        self.status.setText(f"Loaded from {path}")

    def _purge_summary(self):
        total = (self.fill_ul.value() * self.pulses.value()
                 * self.cycles.value())
        vac = self.vac_ul.value() * self.cycles.value()
        self.purge_note.setText(
            f"Each purge draws {vac:g} \u00b5L of waste and pushes "
            f"{total:g} \u00b5L of fresh liquid through, as "
            f"{self.cycles.value()} \u00d7 {self.pulses.value()} opens per "
            f"spout.")

    def purge_settings(self) -> dict:
        return {"purge_vac_ul": self.vac_ul.value(),
                "purge_gap_ms": self.gap_ms.value(),
                "purge_use_pump": self.use_pump.isChecked(),
                "purge_fill_ul": self.fill_ul.value(),
                "purge_pulses": self.pulses.value(),
                "purge_cycles": self.cycles.value(),
                "purge_parallel": self.parallel.isChecked()}


# =====================================================================

class StepperPanel(QGroupBox):
    """
    One syringe pump.

    Counted steps are the only position feedback, so zeroing is not
    optional and the panel says so until it is done.
    """

    def __init__(self, ch: str, get_link):
        super().__init__(f"{SPOUT_NAMES[ch]} syringe pump")
        self.ch = ch
        self.get_link = get_link
        g = QGridLayout(self)

        self.state = QLabel("\u2014")
        self.state.setFont(_mono(10))
        g.addWidget(self.state, 0, 0, 1, 6)

        self.present = QCheckBox("A pump is wired to this spout")
        self.present.setToolTip(
            "Untick for a spout with no pump. It will refuse motion rather "
            "than pretend a purge happened.")
        self.present.stateChanged.connect(self.set_present)
        g.addWidget(self.present, 1, 0, 1, 3)
        g.addWidget(_btn("Read", self.read), 1, 5)

        self.jog = QSpinBox(); self.jog.setRange(1, 20000); self.jog.setValue(200)
        g.addWidget(QLabel("Jog steps"), 2, 0)
        g.addWidget(self.jog, 2, 1)
        g.addWidget(_btn("Aspirate", lambda: self.move(True)), 2, 2)
        g.addWidget(_btn("Dispense", lambda: self.move(False)), 2, 3)
        g.addWidget(_btn("Stop", self.stop), 2, 4)
        self.b_zero = _btn("Zero here", self.zero)
        g.addWidget(self.b_zero, 2, 5)

        self.sps = QSpinBox(); self.sps.setRange(20, 4000); self.sps.setValue(600)
        self.nl = QSpinBox(); self.nl.setRange(1, 100000); self.nl.setValue(180)
        self.lo = QSpinBox(); self.lo.setRange(-200000, 200000); self.lo.setValue(0)
        self.hi = QSpinBox(); self.hi.setRange(1, 400000); self.hi.setValue(40000)
        self.dirn = QComboBox()
        self.dirn.addItems(["Aspirate = increasing", "Aspirate = decreasing"])
        self.dirn.setMinimumWidth(210)
        g.addWidget(QLabel("Speed steps/s"), 3, 0); g.addWidget(self.sps, 3, 1)
        g.addWidget(QLabel("nL per step"), 3, 2);   g.addWidget(self.nl, 3, 3)
        g.addWidget(self.dirn, 3, 4, 1, 2)
        g.addWidget(QLabel("Travel min"), 4, 0);    g.addWidget(self.lo, 4, 1)
        g.addWidget(QLabel("max"), 4, 2);           g.addWidget(self.hi, 4, 3)
        g.addWidget(_btn("Apply settings", self.apply), 4, 4, 1, 2)

        g.addWidget(QLabel(
            "<i>Jog until the plunger reaches its home stop, then Zero here. "
            "Counted steps are the only feedback: an unzeroed pump will "
            "happily drive into the end of the barrel.</i>"), 5, 0, 1, 6)

    def _safe(self, fn):
        if self.get_link() is None:
            return None
        try:
            return fn()
        except Exception as e:
            self.state.setText(str(e))
            self.state.setStyleSheet(f"color: {tc('bad')}")
            return None

    def read(self):
        s = self._safe(lambda: self.get_link().stepper_read(self.ch))
        if s is None:
            return
        self.present.blockSignals(True)
        self.present.setChecked(s.present)
        self.present.blockSignals(False)
        self.sps.setValue(s.sps); self.nl.setValue(max(1, s.nl_per_step))
        self.lo.setValue(s.soft_min); self.hi.setValue(s.soft_max)
        self.dirn.setCurrentIndex(0 if s.aspirate_sign > 0 else 1)

        if not s.present:
            self.state.setText("no pump configured for this spout")
            self.state.setStyleSheet(f"color: {tc('muted')}")
            btn_normal(self.b_zero)
            return
        vol = s.position * s.nl_per_step / 1000.0
        bits = [f"position {s.position} steps ({vol:.0f} \u00b5L)",
                f"travel {s.soft_min}\u2013{s.soft_max}",
                "moving" if s.moving else "idle"]
        self.state.setText("   ".join(bits))
        if s.pos_known:
            self.state.setStyleSheet(f"color: {tc('ok')}")
            btn_normal(self.b_zero)
        else:
            self.state.setText("NOT ZEROED \u2014 " + "   ".join(bits))
            self.state.setStyleSheet(f"color: {tc('bad')}")

    def set_present(self):
        self._safe(lambda: self.get_link().stepper_set_present(
            self.ch, self.present.isChecked()))
        QTimer.singleShot(120, self.read)

    def zero(self):
        if self._safe(lambda: self.get_link().stepper_zero(self.ch)) is not None:
            btn_ready(self.b_zero)
            QTimer.singleShot(1000, lambda: btn_normal(self.b_zero))
        self.read()

    def move(self, aspirate: bool):
        n = self.jog.value()
        fn = (self.get_link().stepper_aspirate if aspirate
              else self.get_link().stepper_dispense)
        self._safe(lambda: fn(self.ch, n))
        QTimer.singleShot(400, self.read)

    def stop(self):
        self._safe(lambda: self.get_link().stepper_stop(self.ch))
        self.read()

    def apply(self):
        link = self.get_link()
        if link is None:
            return
        self._safe(lambda: link.stepper_speed(self.ch, self.sps.value()))
        self._safe(lambda: link.stepper_calibrate(self.ch, self.nl.value()))
        self._safe(lambda: link.stepper_limits(self.ch, self.lo.value(),
                                               self.hi.value()))
        self._safe(lambda: link.stepper_direction(
            self.ch, 1 if self.dirn.currentIndex() == 0 else -1))
        self.read()


# =====================================================================

class StepperTablePanel(QGroupBox):
    """
    Volume to steps, measured per syringe size.

    Steps rather than time: displacement is set by step count, and time
    is only steps divided by speed, so a time-keyed table silently breaks
    when the speed changes. The time each move takes is shown below,
    derived from whatever speed is configured.
    """

    def __init__(self, calibration: StepperCalibration, get_link,
                 get_speed=None):
        super().__init__("Pump volume to steps")
        self.cal = calibration
        self.get_link = get_link
        self.get_speed = get_speed or (lambda: 600)
        v = QVBoxLayout(self)

        v.addWidget(QLabel(
            "Aspirate a known number of steps into a tared boat and weigh "
            "it. Barrel width sets how much volume a step moves, so a "
            "30 mL syringe delivers more per step than a 20 mL one — "
            "measure each size you use and anything between is "
            "interpolated."))

        row = QHBoxLayout()
        self.shared = QCheckBox("One table for all pumps")
        self.shared.setChecked(calibration.shared)
        self.shared.stateChanged.connect(self._mode_changed)
        row.addWidget(self.shared)
        self.which = QComboBox()
        self.which.addItems(["Left pump", "Center pump", "Right pump"])
        self.which.setMinimumWidth(160)
        self.which.currentIndexChanged.connect(self._show_selected)
        row.addWidget(self.which)
        row.addWidget(QLabel("Syringe fitted"))
        self.syringe = QDoubleSpinBox()
        self.syringe.setRange(0.5, 200.0); self.syringe.setDecimals(1)
        self.syringe.setValue(20.0); self.syringe.setSuffix(" mL")
        self.syringe.valueChanged.connect(self._syringe_changed)
        row.addWidget(self.syringe)
        row.addStretch()
        self.b_send = _btn("Send to board", self.send)
        row.addWidget(self.b_send)
        v.addLayout(row)

        self.grid = QTableWidget(0, 3)
        self.grid.setHorizontalHeaderLabels(
            ["Syringe mL", "Volume \u00b5L", "Steps"])
        self.grid.verticalHeader().setVisible(False)
        self.grid.horizontalHeader().setStretchLastSection(True)
        for i, w in enumerate((130, 140, 140)):
            self.grid.setColumnWidth(i, w)
        self.grid.verticalHeader().setDefaultSectionSize(28)
        self.grid.setMinimumHeight(200)
        self.grid.setSizePolicy(QSizePolicy.Policy.Expanding,
                                QSizePolicy.Policy.Expanding)
        self.grid.setSelectionBehavior(
            QAbstractItemView.SelectionBehavior.SelectRows)
        self.grid.itemChanged.connect(self._pull)
        v.addWidget(self.grid)

        r2 = QHBoxLayout()
        r2.addWidget(_btn("Add row", self.add_row))
        r2.addWidget(_btn("Remove selected", self.remove_selected))
        r2.addStretch()
        self.probe = QDoubleSpinBox()
        self.probe.setRange(0.1, 5000.0); self.probe.setValue(54.0)
        self.probe.setSuffix(" \u00b5L")
        self.probe.valueChanged.connect(self._preview)
        r2.addWidget(QLabel("What would"))
        r2.addWidget(self.probe)
        r2.addWidget(QLabel("need?"))
        v.addLayout(r2)

        self.answer = QLabel("\u2014")
        self.answer.setFont(_mono(10))
        v.addWidget(self.answer)
        self.warn = QLabel("")
        self.warn.setFont(_mono(9)); self.warn.setWordWrap(True)
        v.addWidget(self.warn)

        self._mode_changed()

    def _table(self) -> StepperTable:
        ch = "lcr"[self.which.currentIndex()]
        return (self.cal.shared_table if self.cal.shared
                else self.cal.table_for(ch))

    def _mode_changed(self):
        self.cal.shared = self.shared.isChecked()
        self.which.setEnabled(not self.cal.shared)
        self._show_selected()

    def _show_selected(self):
        t = self._table()
        self.syringe.blockSignals(True)
        self.syringe.setValue(t.syringe_ml)
        self.syringe.blockSignals(False)
        self.grid.blockSignals(True)
        self.grid.setRowCount(0)
        for ml, ul, st in t.points:
            self._append(ml, ul, st)
        self.grid.blockSignals(False)
        self._preview()

    def _syringe_changed(self):
        self._table().syringe_ml = self.syringe.value()
        self._preview()

    def _append(self, ml, ul, st):
        r = self.grid.rowCount()
        self.grid.insertRow(r)
        for c_, val in enumerate((ml, ul, st)):
            self.grid.setItem(r, c_, QTableWidgetItem(f"{val:g}"))

    def add_row(self):
        self.grid.blockSignals(True)
        self._append(self.syringe.value(), 0, 0)
        self.grid.blockSignals(False)
        self.grid.editItem(self.grid.item(self.grid.rowCount() - 1, 1))

    def remove_selected(self):
        for r in sorted({i.row() for i in self.grid.selectedIndexes()},
                        reverse=True):
            self.grid.removeRow(r)
        self._pull()

    def _pull(self):
        rows = []
        for r in range(self.grid.rowCount()):
            cells = [self.grid.item(r, c_) for c_ in range(3)]
            if any(x is None for x in cells):
                continue
            try:
                rows.append(tuple(float(x.text()) for x in cells))
            except ValueError:
                continue
        self._table().set_points(rows)
        self._preview()

    def _preview(self):
        t = self._table()
        steps, quality = t.steps_for(self.probe.value(), self.syringe.value())
        if steps is None:
            self.answer.setText("no table yet")
            self.answer.setStyleSheet(f"color: {tc('bad')}")
        else:
            sps = max(1, self.get_speed())
            secs = steps / float(sps)
            colour = {"exact": tc("ok"), "interpolated": tc("ok"),
                      "extrapolated": tc("warn")}.get(quality, tc("bad"))
            note = {"exact": "measured directly",
                    "interpolated": "between measured points",
                    "extrapolated": "OUTSIDE the measured range \u2014 a "
                                    "guess, measure a point near it"}
            self.answer.setText(
                f"{steps} steps   \u2248 {secs:.2f} s at {sps} steps/s   "
                f"({note.get(quality, quality)})")
            self.answer.setStyleSheet(f"color: {colour}")
        w = t.warnings()
        self.warn.setText("  \u00b7  ".join(w) if w else "")
        self.warn.setStyleSheet(f"color: {tc('warn')}")

    def send(self):
        link = self.get_link()
        if link is None:
            QMessageBox.information(self, "Not connected",
                                    "Connect to the board first.")
            return
        notes = self.cal.push_to_board(link)
        self.warn.setText(" | ".join(notes))
        btn_ready(self.b_send)
        QTimer.singleShot(1200, lambda: btn_normal(self.b_send))


class ModulePanel(QGroupBox):
    """
    What the firmware on this board can actually do.

    Discovered by asking the board for its HELP text rather than by
    reading source files: the source on disk is not necessarily what was
    flashed, and the difference is exactly the kind of thing that wastes
    an afternoon. Modules the firmware does not implement show as
    unavailable rather than silently missing.
    """

    KNOWN = [
        ("LED",    "Lights",       "LED,"),
        ("SPK",    "Speakers",     "SPK,"),
        ("SV",     "Spout servos", "SVINIT"),
        ("SOL",    "Solenoids",    "SOLID"),
        ("LK",     "Lick sensors", "LKCAL"),
        ("STP",    "Syringe pumps", "STPREAD"),
        ("BS",     "Block switch", "BSNEW"),
        ("OLF",    "Olfactometer", "OLF,"),
    ]

    def __init__(self, get_link):
        super().__init__("What this board supports")
        self.get_link = get_link
        v = QVBoxLayout(self)
        v.addWidget(_btn("Ask the board", self.scan))
        self.out = QPlainTextEdit()
        self.out.setReadOnly(True)
        self.out.setFont(_mono(9))
        self.out.setMinimumHeight(150)
        v.addWidget(self.out)
        self.out.setPlainText(
            "Press Ask the board to list the command families the flashed "
            "firmware implements.\n\n"
            "Anything shown as not available needs a firmware module: add "
            "the .h/.cpp pair, wire it into commands.cpp, and it will "
            "appear here on the next scan.")

    def scan(self):
        link = self.get_link()
        if link is None:
            QMessageBox.information(self, "Not connected",
                                    "Connect to the board first.")
            return
        try:
            lines = link.help()
        except Exception as e:
            self.out.setPlainText(f"Could not read the board's help: {e}")
            return
        blob = "\n".join(lines)
        rows = []
        for _prefix, name, probe in self.KNOWN:
            ok = probe in blob
            rows.append(f"{'available    ' if ok else 'NOT AVAILABLE'}  {name}")
        rows.append("")
        rows.append("Not available means the flashed firmware has no such "
                    "command. Add the module, wire it into commands.cpp, "
                    "reflash, and scan again.")
        self.out.setPlainText("\n".join(rows))
