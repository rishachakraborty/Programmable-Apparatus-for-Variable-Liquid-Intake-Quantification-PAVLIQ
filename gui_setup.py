"""
gui_setup.py — task setup window with a live Example Task preview.

    pip install PyQt6 pyqtgraph
    python gui_setup.py

Nothing here talks to the Arduino. This window builds a SessionConfig,
generates a session, audits it, and draws it. The experiment pages come
next; keeping setup hardware-free means you can design a task on a
laptop with no rig attached.
"""

from __future__ import annotations

import json
import sys
from typing import Optional

import pyqtgraph as pg
from PyQt6.QtCore import Qt, QTimer, pyqtSignal
from PyQt6.QtGui import QColor, QFont
from PyQt6.QtWidgets import (
    QAbstractItemView, QSizePolicy, QApplication, QCheckBox, QComboBox, QDoubleSpinBox,
    QFileDialog, QFormLayout, QGridLayout, QGroupBox, QHBoxLayout, QLabel,
    QLineEdit, QListWidget, QListWidgetItem, QMainWindow, QMessageBox,
    QPlainTextEdit, QPushButton, QScrollArea, QSpinBox, QSplitter,
    QTabWidget, QTableWidget, QTableWidgetItem, QVBoxLayout, QWidget,
)

import theme
from theme import c as tc
from calibration import CalibrationSet
from task_design import (
    BlockSpec, CueSet, LedCue, OlfactoryCue, OperantDesign, OtherCue,
    SessionConfig, SpeakerCue, TrialType, audit_session, generate_session,
    resolve_durations,
)

# Colour comes from theme.py so light and dark stay consistent across
# every tab. Liquid identity is the only saturated colour in the app.
def INK():        return tc("ink")
def PAPER():      return tc("paper")
def RULE():       return tc("rule")
def MUTED():      return tc("muted")
def C_LIQUID_A(): return tc("liquid_a")
def C_LIQUID_B(): return tc("liquid_b")
def C_CUE():      return tc("cue")
def C_ITI():      return tc("iti")
def C_REWARD():   return tc("ok")
def C_BLOCK():    return tc("warn")

SPOUT_LABELS = {"l": "Left", "c": "Center", "r": "Right"}


def _btn_local(text, fn) -> QPushButton:
    b = QPushButton(text)
    b.clicked.connect(fn)
    return b


def _mono(size=10) -> QFont:
    f = QFont("Menlo" if sys.platform == "darwin" else "Consolas", size)
    f.setStyleHint(QFont.StyleHint.Monospace)
    return f


# =====================================================================
# Trial type table
# =====================================================================

# One row per trial type. The four leading checkboxes are the cue
# modalities: nothing forces a trial to be a tone, and any combination
# is legal as long as at least one is ticked.
TT_COLS = ["Name", "Liquid", "Tone", "Light", "Odour", "Other",
           "Tone Hz", "Clicks", "Click Hz", "Loudness", "Cue ms",
           "LED", "Reward \u00b5L", "Reward %"]


class TrialTypeTable(QTableWidget):
    changed = pyqtSignal()

    def __init__(self):
        super().__init__(0, len(TT_COLS))
        self.setHorizontalHeaderLabels(TT_COLS)
        self.verticalHeader().setVisible(False)
        self.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self.horizontalHeader().setStretchLastSection(True)
        self.setColumnWidth(0, 130)
        for i in range(1, len(TT_COLS)):
            self.setColumnWidth(i, 116)
        self.verticalHeader().setDefaultSectionSize(30)
        # Eight rows visible instead of one. A table you have to scroll to
        # see a single row in is unusable for comparing trial types, which
        # is the whole reason they are in a table.
        self.setMinimumHeight(280)
        self.setSizePolicy(QSizePolicy.Policy.Expanding,
                           QSizePolicy.Policy.Expanding)
        self._liquids: list[str] = []
        self._volumes: list = [1.0, 3.0, 6.0, 10.0]

    def set_liquids(self, liquids: list[str]) -> None:
        self._liquids = liquids
        for r in range(self.rowCount()):
            combo = self.cellWidget(r, 1)
            cur = combo.currentText()
            combo.blockSignals(True)
            combo.clear()
            combo.addItems(liquids)
            if cur:
                combo.setCurrentText(cur)
            combo.blockSignals(False)

    def add_row(self, tt: Optional[dict] = None) -> None:
        tt = tt or {}
        r = self.rowCount()
        self.insertRow(r)

        name = QLineEdit(tt.get("label", f"type_{r + 1}"))
        name.textChanged.connect(self.changed)
        self.setCellWidget(r, 0, name)

        def check(col, on):
            cb = QCheckBox()
            cb.setChecked(on)
            cb.stateChanged.connect(self.changed)
            self.setCellWidget(r, col, cb)
            return cb

        liquid = QComboBox()
        liquid.setEditable(True)
        liquid.setMinimumWidth(130)
        liquid.addItems(self._liquids)
        liquid.setCurrentText(tt.get("liquid", self._liquids[0]
                                     if self._liquids else ""))
        liquid.currentTextChanged.connect(self.changed)
        self.setCellWidget(r, 1, liquid)

        check(2, tt.get("use_tone", True))
        check(3, tt.get("use_led", False))
        check(4, tt.get("use_olf", False))
        check(5, tt.get("use_other", False))

        def spin(lo, hi, val, step=1):
            sb = QSpinBox()
            sb.setRange(lo, hi)
            sb.setSingleStep(step)
            sb.setValue(int(val))
            sb.valueChanged.connect(self.changed)
            return sb

        self.setCellWidget(r, 6, spin(20, 40000, tt.get("tone_hz", 10000), 500))

        clicks = QCheckBox()
        clicks.setChecked(tt.get("click_train", True))
        clicks.stateChanged.connect(self.changed)
        self.setCellWidget(r, 7, clicks)

        self.setCellWidget(r, 8, spin(1, 1000, tt.get("click_hz", 50), 10))
        self.setCellWidget(r, 9, spin(0, 50, tt.get("loudness", 50), 5))
        self.setCellWidget(r, 10, spin(1, 60000, tt.get("duration_ms", 500), 50))

        led = QComboBox()
        led.addItems(["White", "Blue", "Green"])
        led.setMinimumWidth(110)
        led.setCurrentIndex("wbg".index(tt.get("led_channel", "w")[:1]))
        led.currentIndexChanged.connect(self.changed)
        self.setCellWidget(r, 11, led)

        # Volume, not milliseconds. The dropdown offers what the
        # calibration table already covers; Other... accepts anything and
        # is interpolated, which is sound between measured points and a
        # guess outside them.
        vol = QComboBox()
        vol.setEditable(True)
        vol.setMinimumWidth(120)
        self._fill_volumes(vol, tt.get("volume_ul", 3.0))
        vol.currentTextChanged.connect(self.changed)
        self.setCellWidget(r, 12, vol)

        pct = QDoubleSpinBox()
        pct.setRange(0.0, 100.0)
        pct.setDecimals(1)
        pct.setValue(float(tt.get("reward_contingency_pct", 100.0)))
        pct.valueChanged.connect(self.changed)
        self.setCellWidget(r, 13, pct)

        self.changed.emit()

    def _fill_volumes(self, combo, current):
        combo.blockSignals(True)
        combo.clear()
        for v in self._volumes:
            combo.addItem(f"{v:g}")
        combo.addItem("Other\u2026")
        combo.setCurrentText(f"{float(current):g}")
        combo.blockSignals(False)

    def set_volumes(self, volumes) -> None:
        """Offer the volumes the calibration table actually covers."""
        self._volumes = list(volumes)
        for r in range(self.rowCount()):
            w = self.cellWidget(r, 12)
            if w is not None:
                self._fill_volumes(w, self._value_of(w))

    @staticmethod
    def _value_of(combo) -> float:
        try:
            return float(combo.currentText())
        except ValueError:
            return 3.0

    def remove_selected(self) -> None:
        rows = sorted({i.row() for i in self.selectedIndexes()}, reverse=True)
        for r in rows:
            self.removeRow(r)
        self.changed.emit()

    def to_dicts(self) -> list[dict]:
        out = []
        for r in range(self.rowCount()):
            out.append({
                "label": self.cellWidget(r, 0).text().strip(),
                "liquid": self.cellWidget(r, 1).currentText().strip(),
                "use_tone": self.cellWidget(r, 2).isChecked(),
                "use_led": self.cellWidget(r, 3).isChecked(),
                "use_olf": self.cellWidget(r, 4).isChecked(),
                "use_other": self.cellWidget(r, 5).isChecked(),
                "tone_hz": self.cellWidget(r, 6).value(),
                "click_train": self.cellWidget(r, 7).isChecked(),
                "click_hz": self.cellWidget(r, 8).value(),
                "loudness": self.cellWidget(r, 9).value(),
                "duration_ms": self.cellWidget(r, 10).value(),
                "led_channel": "wbg"[self.cellWidget(r, 11).currentIndex()],
                "volume_ul": self._value_of(self.cellWidget(r, 12)),
                "reward_contingency_pct": self.cellWidget(r, 13).value(),
            })
        return out

    def labels(self) -> list[str]:
        return [d["label"] for d in self.to_dicts() if d["label"]]


# =====================================================================
# Block editor
# =====================================================================

class BlockEditor(QGroupBox):
    changed = pyqtSignal()
    remove_requested = pyqtSignal(object)

    def __init__(self, index: int):
        super().__init__(f"Block {index + 1}")
        self.setCheckable(False)
        grid = QGridLayout(self)

        self.name = QLineEdit(f"block_{index + 1}")
        self.kind = QComboBox()
        self.kind.addItems(["single", "choice"])
        self.kind.setMinimumWidth(130)
        self.n_trials = QSpinBox()
        self.n_trials.setRange(1, 100000)
        self.n_trials.setValue(100)

        self.liquids = QLineEdit()
        self.liquids.setPlaceholderText("alcohol            (choice: alcohol, water)")

        grid.addWidget(QLabel("Name"), 0, 0)
        grid.addWidget(self.name, 0, 1)
        grid.addWidget(QLabel("Type"), 0, 2)
        grid.addWidget(self.kind, 0, 3)
        grid.addWidget(QLabel("Trials"), 0, 4)
        grid.addWidget(self.n_trials, 0, 5)
        grid.addWidget(QLabel("Liquids"), 1, 0)
        grid.addWidget(self.liquids, 1, 1, 1, 5)

        grid.addWidget(QLabel("Trial types in this block"), 2, 0, 1, 2)
        self.types = QListWidget()
        self.types.setSelectionMode(
            QAbstractItemView.SelectionMode.MultiSelection)
        self.types.setMinimumHeight(150)
        grid.addWidget(self.types, 3, 0, 1, 6)

        cue_box = QGroupBox("Cue at block onset")
        cue_form = QHBoxLayout(cue_box)
        # A block is no more obliged to be an LED than a trial is obliged
        # to be a tone. Tick whatever combination the design calls for.
        self.cue_on = QCheckBox("LED")
        self.cue_on.setChecked(True)
        self.cue_tone = QCheckBox("Tone")
        self.cue_olf = QCheckBox("Odour")
        self.cue_other = QCheckBox("Other")
        self.cue_tone_hz = QSpinBox(); self.cue_tone_hz.setRange(20, 40000)
        self.cue_tone_hz.setValue(8000)
        self.cue_ch = QComboBox()
        self.cue_ch.addItems(["White LED", "Blue LED", "Green LED"])
        self.cue_ch.setMinimumWidth(140)
        self.cue_ms = QSpinBox(); self.cue_ms.setRange(1, 60000); self.cue_ms.setValue(2000)
        self.cue_pulse = QCheckBox("Pulsing")
        self.cue_hz = QSpinBox(); self.cue_hz.setRange(1, 100); self.cue_hz.setValue(10)
        self.cue_bright = QSpinBox(); self.cue_bright.setRange(0, 255); self.cue_bright.setValue(255)
        for w in (self.cue_on, self.cue_ch, QLabel("ms"), self.cue_ms,
                  self.cue_pulse, QLabel("Hz"), self.cue_hz,
                  QLabel("Bright"), self.cue_bright,
                  self.cue_tone, QLabel("tone Hz"), self.cue_tone_hz,
                  self.cue_olf, self.cue_other):
            cue_form.addWidget(w)
        cue_form.addStretch()
        grid.addWidget(cue_box, 4, 0, 1, 6)

        rm = QPushButton("Remove this block")
        rm.clicked.connect(lambda: self.remove_requested.emit(self))
        grid.addWidget(rm, 5, 5)

        for w in (self.name, self.liquids):
            w.textChanged.connect(self.changed)
        self.kind.currentTextChanged.connect(self.changed)
        for w in (self.n_trials, self.cue_ms, self.cue_hz, self.cue_bright):
            w.valueChanged.connect(self.changed)
        for w in (self.cue_on, self.cue_pulse, self.cue_tone, self.cue_olf,
                  self.cue_other):
            w.stateChanged.connect(self.changed)
        self.cue_tone_hz.valueChanged.connect(self.changed)
        self.types.itemSelectionChanged.connect(self.changed)

    def refresh_types(self, labels: list[str]) -> None:
        keep = {i.text() for i in self.types.selectedItems()}
        self.types.blockSignals(True)
        self.types.clear()
        for lab in labels:
            it = QListWidgetItem(lab)
            self.types.addItem(it)
            if lab in keep:
                it.setSelected(True)
        self.types.blockSignals(False)

    def to_dict(self) -> dict:
        return {
            "label": self.name.text().strip(),
            "kind": self.kind.currentText(),
            "liquids": [s.strip() for s in self.liquids.text().split(",")
                        if s.strip()],
            "n_trials": self.n_trials.value(),
            "trial_type_labels": [i.text() for i in self.types.selectedItems()],
            "cue": {"led": self.cue_on.isChecked(),
                    "channel": "wbg"[self.cue_ch.currentIndex()],
                    "duration_ms": self.cue_ms.value(),
                    "pulsing": self.cue_pulse.isChecked(),
                    "pulse_hz": self.cue_hz.value(),
                    "brightness": self.cue_bright.value(),
                    "tone": self.cue_tone.isChecked(),
                    "tone_hz": self.cue_tone_hz.value(),
                    "olf": self.cue_olf.isChecked(),
                    "other": self.cue_other.isChecked()},
        }

    def from_dict(self, d: dict) -> None:
        self.name.setText(d.get("label", ""))
        self.kind.setCurrentText(d.get("kind", "single"))
        self.liquids.setText(", ".join(d.get("liquids", [])))
        self.n_trials.setValue(d.get("n_trials", 100))
        want = set(d.get("trial_type_labels", []))
        for i in range(self.types.count()):
            self.types.item(i).setSelected(self.types.item(i).text() in want)
        c = d.get("cue")
        self.cue_on.setChecked(bool(c and c.get("led", True)))
        if c:
            self.cue_tone.setChecked(c.get("tone", False))
            self.cue_olf.setChecked(c.get("olf", False))
            self.cue_other.setChecked(c.get("other", False))
            self.cue_tone_hz.setValue(c.get("tone_hz", 8000))
            self.cue_ch.setCurrentIndex("wbg".index(c.get("channel", "w")[:1]))
            self.cue_ms.setValue(c.get("duration_ms", 2000))
            self.cue_pulse.setChecked(c.get("pulsing", False))
            self.cue_hz.setValue(c.get("pulse_hz", 10))
            self.cue_bright.setValue(c.get("brightness", 255))


# =====================================================================
# Setup tab
# =====================================================================

class SetupTab(QWidget):
    session_ready = pyqtSignal(object, object)   # Session, audit dict

    def __init__(self, calibration=None):
        super().__init__()
        self.calibration = calibration or CalibrationSet()
        self._last_session = None
        outer = QVBoxLayout(self)

        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        body = QWidget()
        lay = QVBoxLayout(body)
        scroll.setWidget(body)
        outer.addWidget(scroll, 1)

        # ---- liquids ----
        g = QGroupBox("Liquids")
        f = QFormLayout(g)
        self.liquids = QLineEdit("alcohol, water")
        self.liquids.textChanged.connect(self._liquids_changed)
        f.addRow("Names, separated by commas", self.liquids)
        f.addRow(QLabel("<i>These names appear wherever a liquid is chosen. "
                        "Commas separate names, so a name cannot contain "
                        "one.</i>"))
        lay.addWidget(g)

        # ---- solenoids ----
        g = QGroupBox("Solenoids")
        v = QVBoxLayout(g)
        v.addWidget(QLabel(
            "Each solenoid gates one liquid to one spout. Calibration is "
            "nanolitres per millisecond of open time \u2014 weigh the output "
            "of twenty 1-second opens and divide."))
        self.sol_table = QTableWidget(4, 5)
        self.sol_table.setHorizontalHeaderLabels(
            ["Solenoid", "In use", "Liquid", "Goes to", "nL per ms"])
        self.sol_table.verticalHeader().setVisible(False)
        self.sol_table.horizontalHeader().setStretchLastSection(True)
        self.sol_table.verticalHeader().setDefaultSectionSize(32)
        for i, w in enumerate((90, 70, 190, 150, 130)):
            self.sol_table.setColumnWidth(i, w)
        self.sol_widgets = []
        for r in range(4):
            it = QTableWidgetItem(str(r + 1))
            it.setFlags(Qt.ItemFlag.ItemIsEnabled)
            self.sol_table.setItem(r, 0, it)
            active = QCheckBox(); active.setChecked(r < 4)
            liquid = QComboBox(); liquid.setEditable(True)
            liquid.setMinimumWidth(150)
            spout = QComboBox()
            spout.addItems(["Left", "Center", "Right"])
            spout.setMinimumWidth(140)
            cal = QSpinBox(); cal.setRange(0, 1000000); cal.setValue(0)
            self.sol_table.setCellWidget(r, 1, active)
            self.sol_table.setCellWidget(r, 2, liquid)
            self.sol_table.setCellWidget(r, 3, spout)
            self.sol_table.setCellWidget(r, 4, cal)
            self.sol_widgets.append((active, liquid, spout, cal))
        self.sol_table.setMinimumHeight(190)
        v.addWidget(self.sol_table)
        lay.addWidget(g)

        # ---- trial types ----
        g = QGroupBox("Trial types")
        v = QVBoxLayout(g)
        v.addWidget(QLabel(
            "Click rate tells the animal how much is coming; pour duration "
            "delivers it. Keep them consistent or the cue is a lie."))
        self.tt = TrialTypeTable()
        self.tt.changed.connect(self._types_changed)
        v.addWidget(self.tt)
        row = QHBoxLayout()
        b_add = QPushButton("Add trial type"); b_add.clicked.connect(lambda: self.tt.add_row())
        b_del = QPushButton("Remove selected"); b_del.clicked.connect(self.tt.remove_selected)
        row.addWidget(b_add); row.addWidget(b_del); row.addStretch()
        v.addLayout(row)
        lay.addWidget(g)

        # ---- blocks ----
        g = QGroupBox("Blocks")
        self.block_box = QVBoxLayout(g)
        self.blocks: list[BlockEditor] = []
        row = QHBoxLayout()
        b_add = QPushButton("Add block"); b_add.clicked.connect(lambda: self._add_block())
        row.addWidget(b_add); row.addStretch()
        self.block_box.addLayout(row)
        lay.addWidget(g)

        # ---- timing ----
        g = QGroupBox("Timing")
        f = QFormLayout(g)
        self.cue_reward = QSpinBox(); self.cue_reward.setRange(0, 60000); self.cue_reward.setValue(1000)
        self.omission = QSpinBox(); self.omission.setRange(0, 120000); self.omission.setValue(5000)
        self.retract_delay = QSpinBox(); self.retract_delay.setRange(0, 60000); self.retract_delay.setValue(1000)
        self.gate = QSpinBox(); self.gate.setRange(0, 10000); self.gate.setValue(500)
        self.iti_mean = QDoubleSpinBox(); self.iti_mean.setRange(0.1, 600); self.iti_mean.setValue(8.0)
        self.iti_min = QDoubleSpinBox(); self.iti_min.setRange(0.0, 600); self.iti_min.setValue(3.0)
        self.iti_max = QDoubleSpinBox(); self.iti_max.setRange(0.1, 600); self.iti_max.setValue(30.0)
        f.addRow("Cue to reward, ms", self.cue_reward)
        f.addRow("Give up after, ms", self.omission)
        f.addRow("Wait before retracting, ms", self.retract_delay)
        f.addRow("Quiet period before next trial, ms", self.gate)
        f.addRow("Interval between trials \u2014 mean, s", self.iti_mean)
        f.addRow("Shortest interval, s", self.iti_min)
        f.addRow("Longest interval, s", self.iti_max)
        f.addRow(QLabel(
            "<i>Intervals are drawn from an exponential and cut off at the "
            "longest value, so the mean you actually get is a little below "
            "the one you ask for. The preview reports it.</i>"))
        lay.addWidget(g)

        # ---- operant ----
        g = QGroupBox("How many licks earn a reward")
        f = QFormLayout(g)
        self.op_mode = QComboBox()
        self.op_mode.addItems(["none", "fixed", "variable", "progressive"])
        self.op_mode.setMinimumWidth(180)
        self.op_mode.setCurrentText("progressive")
        self.op_fixed = QSpinBox(); self.op_fixed.setRange(1, 500); self.op_fixed.setValue(1)
        self.op_mean = QSpinBox(); self.op_mean.setRange(1, 500); self.op_mean.setValue(3)
        self.op_set = QLineEdit("1, 2, 4, 8")
        f.addRow("Schedule", self.op_mode)
        f.addRow("Fixed ratio", self.op_fixed)
        f.addRow("Variable ratio, average", self.op_mean)
        f.addRow("Progressive ratios", self.op_set)
        f.addRow(QLabel(
            "<i>Progressive: trials divide evenly across the ratios, and "
            "every block runs at each ratio before it advances.</i>"))
        lay.addWidget(g)

        # ---- behaviour toggles ----
        g = QGroupBox("How a trial behaves")
        f = QFormLayout(g)
        self.retraction = QCheckBox("Retract spouts during a trial")
        self.retraction.setChecked(True)
        self.retraction.setToolTip(
            "Off leaves both spouts out for the whole trial. Suits "
            "habituation or free access, but it also removes what stops "
            "an animal sampling both spouts on a choice trial, so choice "
            "data from such a session means something different.")
        self.rand_sides = QCheckBox("Move each liquid between spouts")
        self.rand_sides.setChecked(True)
        self.rand_sides.setToolTip(
            "On, a liquid alternates sides under the balance and repeat "
            "limits below. Off pins each liquid to one spout, which lets "
            "side preference masquerade as liquid preference.")
        self.purge_on = QCheckBox("Purge the line when a spout changes liquid")
        self.purge_on.setChecked(True)
        self.purge_on.setToolTip(
            "Without this the first lick after a switch delivers the "
            "previous solution while the cue says otherwise.")
        f.addRow(self.retraction)
        f.addRow(self.rand_sides)
        f.addRow(self.purge_on)

        self.other_name = QLineEdit("other")
        self.other_cmd = QLineEdit()
        self.other_cmd.setPlaceholderText("raw command, e.g. VIB,1,200")
        f.addRow("Name of the \"other\" modality", self.other_name)
        f.addRow("Command it sends", self.other_cmd)
        f.addRow(QLabel(
            "<i>Ticking Other on a trial or block sends this command. If "
            "the firmware has no such command the rejection is logged, so "
            "the data shows which trials asked for something the rig could "
            "not do.</i>"))
        for w in (self.retraction, self.rand_sides, self.purge_on):
            w.stateChanged.connect(self._preview_sequence)
        lay.addWidget(g)

        # ---- randomization ----
        g = QGroupBox("Shuffling")
        f = QFormLayout(g)
        self.max_repeat = QSpinBox(); self.max_repeat.setRange(1, 20); self.max_repeat.setValue(3)
        self.balance = QSpinBox(); self.balance.setRange(2, 200); self.balance.setValue(20)
        self.rand_blocks = QCheckBox("Shuffle block order at each ratio")
        self.rand_blocks.setChecked(True)
        self.seed = QLineEdit(); self.seed.setPlaceholderText("leave empty to pick one at random")
        f.addRow("Most repeats in a row", self.max_repeat)
        f.addRow("Balance sides within every N trials", self.balance)
        f.addRow("", self.rand_blocks)
        f.addRow("Seed", self.seed)
        lay.addWidget(g)
        # ---- hardware sequence ----
        g = QGroupBox("What the hardware will do, trial by trial")
        v = QVBoxLayout(g)
        v.addWidget(QLabel(
            "Generated from the same fields the runner uses, so this "
            "cannot drift from what the rig actually does."))
        row = QHBoxLayout()
        self.seq_kind = QComboBox()
        self.seq_kind.addItems(["A choice trial", "A single-spout trial",
                                "A trial that needs a purge"])
        self.seq_kind.setMinimumWidth(230)
        self.seq_kind.currentIndexChanged.connect(self._preview_sequence)
        row.addWidget(QLabel("Show"))
        row.addWidget(self.seq_kind)
        row.addWidget(_btn_local("Refresh", self._preview_sequence))
        row.addStretch()
        v.addLayout(row)
        self.seq_text = QPlainTextEdit()
        self.seq_text.setReadOnly(True)
        self.seq_text.setFont(_mono(9))
        self.seq_text.setMinimumHeight(230)
        v.addWidget(self.seq_text)
        lay.addWidget(g)

        lay.addStretch()

        # ---- action bar ----
        bar = QHBoxLayout()
        self.b_build = QPushButton("Save and preview")
        self.b_build.setDefault(True)
        self.b_build.clicked.connect(self.build)
        b_save = QPushButton("Save settings to file"); b_save.clicked.connect(self.save_json)
        b_load = QPushButton("Load settings"); b_load.clicked.connect(self.load_json)
        bar.addWidget(self.b_build); bar.addStretch()
        bar.addWidget(b_save); bar.addWidget(b_load)
        outer.addLayout(bar)

        self._defaults()
        vols = self.calibration.known_volumes()
        if vols:
            self.tt.set_volumes(vols)
        QTimer.singleShot(0, self._preview_sequence)

    # ---- defaults matching the two-choice alcohol/water task ----

    def _defaults(self) -> None:
        self._liquids_changed()
        for hz, ul in ((50, 1.0), (100, 3.0), (200, 6.0), (400, 10.0)):
            self.tt.add_row({"label": f"alc_{hz}", "liquid": "alcohol",
                             "tone_hz": 12000, "click_hz": hz,
                             "volume_ul": ul, "use_tone": True})
            self.tt.add_row({"label": f"wat_{hz}", "liquid": "water",
                             "tone_hz": 5000, "click_hz": hz,
                             "volume_ul": ul, "use_tone": True})
        for i, (name, kind, liqs, ch) in enumerate([
                ("alcohol_only", "single", "alcohol", "w"),
                ("water_only", "single", "water", "b"),
                ("choice", "choice", "alcohol, water", "g")]):
            b = self._add_block()
            b.name.setText(name)
            b.kind.setCurrentText(kind)
            b.liquids.setText(liqs)
            b.cue_ch.setCurrentIndex("wbg".index(ch))
            want = ("alc" if liqs == "alcohol" else
                    "wat" if liqs == "water" else "")
            for j in range(b.types.count()):
                t = b.types.item(j).text()
                b.types.item(j).setSelected(t.startswith(want) if want else True)
        for r, (liq, sp) in enumerate([("water", "l"), ("alcohol", "l"),
                                       ("water", "r"), ("alcohol", "r")]):
            self.sol_widgets[r][1].setCurrentText(liq)
            self.sol_widgets[r][2].setCurrentIndex("lcr".index(sp))

    # ---- wiring ----

    def _liquid_names(self) -> list[str]:
        return [s.strip() for s in self.liquids.text().split(",") if s.strip()]

    def _liquids_changed(self) -> None:
        names = self._liquid_names()
        self.tt.set_liquids(names)
        for _, liquid, _, _ in self.sol_widgets:
            cur = liquid.currentText()
            liquid.blockSignals(True)
            liquid.clear(); liquid.addItems(names)
            if cur:
                liquid.setCurrentText(cur)
            liquid.blockSignals(False)

    def _types_changed(self) -> None:
        labels = self.tt.labels()
        for b in self.blocks:
            b.refresh_types(labels)

    def _add_block(self) -> BlockEditor:
        b = BlockEditor(len(self.blocks))
        b.remove_requested.connect(self._remove_block)
        b.refresh_types(self.tt.labels())
        self.blocks.append(b)
        self.block_box.insertWidget(self.block_box.count() - 1, b)
        return b

    def _remove_block(self, b: BlockEditor) -> None:
        self.blocks.remove(b)
        b.setParent(None)

    # ---- config assembly ----

    def to_config(self) -> SessionConfig:
        types = []
        for d in self.tt.to_dicts():
            cs = CueSet()
            if d["use_tone"]:
                cs.speaker = SpeakerCue(
                    duration_ms=d["duration_ms"], tone_hz=d["tone_hz"],
                    click_train=d["click_train"], click_hz=d["click_hz"],
                    volume=d["loudness"])
            if d["use_led"]:
                cs.led = LedCue(channel=d["led_channel"],
                                duration_ms=d["duration_ms"])
            if d["use_olf"]:
                cs.olfactory = OlfactoryCue(duration_ms=d["duration_ms"])
            if d["use_other"]:
                cs.other = OtherCue(name=self.other_name.text().strip() or "other",
                                    duration_ms=d["duration_ms"],
                                    raw_command=self.other_cmd.text().strip())
            types.append(TrialType(
                label=d["label"], liquid=d["liquid"], cue=cs,
                volume_ul=d["volume_ul"],
                reward_contingency_pct=d["reward_contingency_pct"]))

        blocks = []
        for b in self.blocks:
            d = b.to_dict()
            c = d["cue"] or {}
            cue = CueSet()
            if c.get("led"):
                cue.led = LedCue(channel=c["channel"],
                                 duration_ms=c["duration_ms"],
                                 pulsing=c["pulsing"], pulse_hz=c["pulse_hz"],
                                 brightness=c["brightness"])
            if c.get("tone"):
                cue.speaker = SpeakerCue(duration_ms=c["duration_ms"],
                                         tone_hz=c.get("tone_hz", 8000),
                                         click_train=False)
            if c.get("olf"):
                cue.olfactory = OlfactoryCue(duration_ms=c["duration_ms"])
            if c.get("other"):
                cue.other = OtherCue(
                    name=self.other_name.text().strip() or "other",
                    duration_ms=c["duration_ms"],
                    raw_command=self.other_cmd.text().strip())
            if cue.is_empty():
                cue = None
            blocks.append(BlockSpec(
                label=d["label"], kind=d["kind"], liquids=d["liquids"],
                n_trials=d["n_trials"],
                trial_type_labels=d["trial_type_labels"], cue=cue))

        sol_map, active = {}, set()
        for i, (on, liquid, spout, _) in enumerate(self.sol_widgets):
            if on.isChecked() and liquid.currentText().strip():
                sp = "lcr"[spout.currentIndex()]
                sol_map[(liquid.currentText().strip(), sp)] = i + 1
                active.add(sp)

        mode = self.op_mode.currentText()
        ratio_set = [int(x) for x in self.op_set.text().replace(",", " ").split()
                     if x.strip().isdigit()] or [1]

        seed_txt = self.seed.text().strip()
        return SessionConfig(
            trial_types=types, blocks=blocks, solenoid_map=sol_map,
            active_spouts=sorted(active, key=lambda s: "lcr".index(s)),
            iti_mean_s=self.iti_mean.value(), iti_min_s=self.iti_min.value(),
            iti_max_s=self.iti_max.value(),
            cue_reward_delay_ms=self.cue_reward.value(),
            omission_window_ms=self.omission.value(),
            iti_retract_delay_ms=self.retract_delay.value(),
            quiet_gate_ms=self.gate.value(),
            max_repeat=self.max_repeat.value(),
            balance_window=self.balance.value(),
            operant=OperantDesign(mode=mode, ratio=self.op_fixed.value(),
                                  mean_ratio=self.op_mean.value(),
                                  ratio_set=ratio_set),
            randomize_block_order=self.rand_blocks.isChecked(),
            randomize_sides=self.rand_sides.isChecked(),
            use_retraction=self.retraction.isChecked(),
            purge_on_liquid_change=self.purge_on.isChecked(),
            seed=int(seed_txt) if seed_txt.isdigit() else None)

    def build(self) -> None:
        cfg = self.to_config()
        problems = cfg.validate()
        if problems:
            QMessageBox.warning(
                self, "This task cannot run yet",
                "Fix these before previewing:\n\n  \u2022 " +
                "\n  \u2022 ".join(problems))
            return
        sess = generate_session(cfg)
        notes = resolve_durations(sess, self.calibration)
        self._last_session = sess
        self._preview_sequence()
        if notes:
            QMessageBox.warning(
                self, "Check the calibration",
                "The task will run, but these amounts are not backed by "
                "measurements:\n\n  \u2022 " + "\n  \u2022 ".join(notes))
        self.session_ready.emit(sess, audit_session(sess))

    def _preview_sequence(self):
        """Draw the hardware order for a representative trial."""
        try:
            cfg = self.to_config()
            if cfg.validate():
                self.seq_text.setPlainText(
                    "Fill in the task above, then press Save and preview.")
                return
            sess = self._last_session
            if sess is None or sess.config is not cfg:
                sess = generate_session(cfg)
                resolve_durations(sess, self.calibration)
        except Exception as e:
            self.seq_text.setPlainText(f"Cannot build a preview yet: {e}")
            return

        idx = self.seq_kind.currentIndex()
        if idx == 0:
            t = next((x for x in sess.trials if x.choice), None)
        elif idx == 1:
            t = next((x for x in sess.trials if not x.choice), None)
        else:
            t = next((x for x in sess.trials if x.needs_purge), None)
        if t is None:
            self.seq_text.setPlainText(
                "No trial of that kind in the current task.")
            return

        head = (f"trial {t.index}   block {t.block_label}   "
                f"FR {t.ratio}   {'choice' if t.choice else 'single spout'}")
        lines = [head, "-" * len(head)]
        for t_ms, desc in t.hardware_sequence(cfg):
            stamp = f"{t_ms:>7} ms" if t_ms is not None else "  on event"
            lines.append(f"{stamp}   {desc}")
        lines.append("")
        lines.append(f"purges in this session: "
                     f"{sum(1 for x in sess.trials if x.needs_purge)} of "
                     f"{sess.n_trials} trials")
        self.seq_text.setPlainText("\n".join(lines))

    # ---- persistence ----

    def save_json(self) -> None:
        path, _ = QFileDialog.getSaveFileName(self, "Save settings", "",
                                              "JSON (*.json)")
        if not path:
            return
        data = {
            "liquids": self.liquids.text(),
            "solenoids": [{"active": a.isChecked(), "liquid": l.currentText(),
                           "spout": "lcr"[s.currentIndex()],
                           "nl_per_ms": c.value()}
                          for a, l, s, c in self.sol_widgets],
            "trial_types": self.tt.to_dicts(),
            "blocks": [b.to_dict() for b in self.blocks],
            "timing": {"cue_reward": self.cue_reward.value(),
                       "omission": self.omission.value(),
                       "retract_delay": self.retract_delay.value(),
                       "gate": self.gate.value(),
                       "iti_mean": self.iti_mean.value(),
                       "iti_min": self.iti_min.value(),
                       "iti_max": self.iti_max.value()},
            "operant": {"mode": self.op_mode.currentText(),
                        "fixed": self.op_fixed.value(),
                        "mean": self.op_mean.value(),
                        "set": self.op_set.text()},
            "behaviour": {"retraction": self.retraction.isChecked(),
                          "randomize_sides": self.rand_sides.isChecked(),
                          "purge": self.purge_on.isChecked(),
                          "other_name": self.other_name.text(),
                          "other_cmd": self.other_cmd.text()},
            "shuffle": {"max_repeat": self.max_repeat.value(),
                        "balance": self.balance.value(),
                        "randomize_blocks": self.rand_blocks.isChecked(),
                        "seed": self.seed.text()},
        }
        with open(path, "w") as f:
            json.dump(data, f, indent=2)

    def load_json(self) -> None:
        path, _ = QFileDialog.getOpenFileName(self, "Load settings", "",
                                              "JSON (*.json)")
        if not path:
            return
        with open(path) as f:
            d = json.load(f)

        self.liquids.setText(d.get("liquids", ""))
        self._liquids_changed()

        for r, s in enumerate(d.get("solenoids", [])[:4]):
            a, l, sp, c = self.sol_widgets[r]
            a.setChecked(s.get("active", True))
            l.setCurrentText(s.get("liquid", ""))
            sp.setCurrentIndex("lcr".index(s.get("spout", "l")[:1]))
            c.setValue(int(s.get("nl_per_ms", 0)))

        while self.tt.rowCount():
            self.tt.removeRow(0)
        for t in d.get("trial_types", []):
            self.tt.add_row(t)

        for b in list(self.blocks):
            self._remove_block(b)
        for bd in d.get("blocks", []):
            b = self._add_block()
            b.refresh_types(self.tt.labels())
            b.from_dict(bd)

        t = d.get("timing", {})
        self.cue_reward.setValue(t.get("cue_reward", 1000))
        self.omission.setValue(t.get("omission", 5000))
        self.retract_delay.setValue(t.get("retract_delay", 1000))
        self.gate.setValue(t.get("gate", 500))
        self.iti_mean.setValue(t.get("iti_mean", 8.0))
        self.iti_min.setValue(t.get("iti_min", 3.0))
        self.iti_max.setValue(t.get("iti_max", 30.0))

        o = d.get("operant", {})
        self.op_mode.setCurrentText(o.get("mode", "none"))
        self.op_fixed.setValue(o.get("fixed", 1))
        self.op_mean.setValue(o.get("mean", 3))
        self.op_set.setText(o.get("set", "1, 2, 4, 8"))

        bh = d.get("behaviour", {})
        self.retraction.setChecked(bh.get("retraction", True))
        self.rand_sides.setChecked(bh.get("randomize_sides", True))
        self.purge_on.setChecked(bh.get("purge", True))
        self.other_name.setText(bh.get("other_name", "other"))
        self.other_cmd.setText(bh.get("other_cmd", ""))

        s = d.get("shuffle", {})
        self.max_repeat.setValue(s.get("max_repeat", 3))
        self.balance.setValue(s.get("balance", 20))
        self.rand_blocks.setChecked(s.get("randomize_blocks", True))
        self.seed.setText(str(s.get("seed", "")))
        self._preview_sequence()


# =====================================================================
# Example task tab
# =====================================================================

class ExampleTab(QWidget):
    """
    Every trial is a row, time runs left to right from cue onset.

    The design decision here: rows are drawn in EXECUTION order, so the
    block structure and the ratio ladder are visible as vertical texture
    rather than something you have to read out of a table. Liquid
    identity is the only colour that carries meaning; everything
    structural is grey.
    """

    def __init__(self):
        super().__init__()
        lay = QVBoxLayout(self)

        self.headline = QLabel("Set up a task, then choose Save and preview.")
        self.headline.setFont(QFont("", 13))
        lay.addWidget(self.headline)

        split = QSplitter(Qt.Orientation.Horizontal)
        lay.addWidget(split, 1)

        pg.setConfigOption("background", tc("paper"))
        pg.setConfigOption("foreground", tc("ink"))
        self.plot = pg.PlotWidget()
        self.plot.setLabel("bottom", "Seconds from cue onset")
        self.plot.setLabel("left", "Trial")
        self.plot.invertY(True)
        self.plot.showGrid(x=True, y=False, alpha=0.15)
        split.addWidget(self.plot)

        side = QWidget()
        sv = QVBoxLayout(side)
        sv.addWidget(QLabel("<b>Summary</b>"))
        self.summary = QPlainTextEdit(); self.summary.setReadOnly(True)
        self.summary.setFont(_mono(9))
        sv.addWidget(self.summary, 2)
        sv.addWidget(QLabel("<b>Checks</b>"))
        self.audit = QPlainTextEdit(); self.audit.setReadOnly(True)
        self.audit.setFont(_mono(9))
        sv.addWidget(self.audit, 1)
        split.addWidget(side)
        split.setSizes([900, 380])

        self.sess = None

    # ---- drawing ----

    def show_session(self, sess, audit: dict) -> None:
        self.sess = sess
        cfg = sess.config
        self.plot.clear()

        liquids = []
        for t in sess.trials:
            for s in t.spouts:
                if s.liquid not in liquids:
                    liquids.append(s.liquid)
        colour = {liq: (C_LIQUID_A() if i == 0 else
                        C_LIQUID_B() if i == 1 else tc("liquid_c"))
                  for i, liq in enumerate(liquids)}

        cue_reward = cfg.cue_reward_delay_ms / 1000.0
        retract = cue_reward + cfg.iti_retract_delay_ms / 1000.0

        seg_x, seg_y, seg_pen = [], [], []
        iti_x, iti_y = [], []
        rew_x, rew_y, rew_b = [], [], []

        for t in sess.trials:
            y = t.index
            # cue bars, one per spout, offset so a choice trial reads as two
            n = len(t.spouts)
            for k, s in enumerate(t.spouts):
                off = 0.0 if n == 1 else (-0.22 if k == 0 else 0.22)
                seg_x += [0.0, s.cue.max_duration_ms() / 1000.0]
                seg_y += [y + off, y + off]
                seg_pen.append(colour[s.liquid])
                if s.rewarded:
                    rew_x.append(cue_reward)
                    rew_y.append(y + off)
                    rew_b.append(colour[s.liquid])
            iti_x += [retract, retract + t.iti_s]
            iti_y += [y, y]

        # ITI first, so it sits behind everything
        self.plot.addItem(pg.PlotCurveItem(
            x=iti_x, y=iti_y, connect="pairs",
            pen=pg.mkPen(C_ITI(), width=2)))

        # Cue bars grouped by colour: one item per colour keeps this fast
        # even at a few thousand trials.
        for col in set(seg_pen):
            xs, ys = [], []
            for i, c in enumerate(seg_pen):
                if c == col:
                    xs += seg_x[2 * i:2 * i + 2]
                    ys += seg_y[2 * i:2 * i + 2]
            self.plot.addItem(pg.PlotCurveItem(
                x=xs, y=ys, connect="pairs", pen=pg.mkPen(col, width=3)))

        if rew_x:
            self.plot.addItem(pg.ScatterPlotItem(
                x=rew_x, y=rew_y, symbol="d", size=6,
                brush=pg.mkBrush(C_REWARD()), pen=pg.mkPen(None)))

        self.plot.addItem(pg.InfiniteLine(
            pos=cue_reward, angle=90,
            pen=pg.mkPen(C_REWARD(), width=1, style=Qt.PenStyle.DashLine),
            label="reward", labelOpts={"position": 0.02, "color": C_REWARD()}))
        self.plot.addItem(pg.InfiniteLine(
            pos=cfg.omission_window_ms / 1000.0, angle=90,
            pen=pg.mkPen(MUTED(), width=1, style=Qt.PenStyle.DotLine),
            label="give up", labelOpts={"position": 0.06, "color": MUTED()}))

        # Block boundaries and the ratio ladder
        last_level = None
        for t in sess.trials:
            if not t.is_block_start:
                continue
            self.plot.addItem(pg.InfiniteLine(
                pos=t.index - 0.5, angle=0,
                pen=pg.mkPen(RULE(), width=1)))
            txt = pg.TextItem(f"  {t.block_label}", color=C_BLOCK(),
                              anchor=(0, 0.5))
            txt.setPos(-0.45, t.index + 0.5)
            self.plot.addItem(txt)
            if t.ratio_level != last_level:
                last_level = t.ratio_level
                if cfg.operant.mode == "progressive":
                    fr = cfg.operant.ratio_set[t.ratio_level]
                    lab = pg.TextItem(f"FR {fr}", color=INK(), anchor=(0, 0.5))
                    lab.setPos(-0.45, t.index - 1.5)
                    self.plot.addItem(lab)
                    self.plot.addItem(pg.InfiniteLine(
                        pos=t.index - 0.5, angle=0,
                        pen=pg.mkPen(INK(), width=2)))

        self.plot.setXRange(-0.5, max(6.0, cfg.omission_window_ms / 1000.0 + 2))
        self.plot.setYRange(-1, min(sess.n_trials, 80))

        legend = "   ".join(f"{liq} = {colour[liq]}" for liq in liquids)
        self.headline.setText(
            f"{sess.n_trials} trials \u00b7 seed {sess.seed} \u00b7 {legend}"
            f"  \u2014  scroll to move through the session")

        self._fill_text(sess, audit)

    def _fill_text(self, sess, audit: dict) -> None:
        st = audit["stats"]
        lines = [f"seed              {sess.seed}",
                 f"trials            {sess.n_trials}",
                 f"est. duration     {st['estimated_duration_min']} min",
                 f"ITI mean asked    {sess.config.iti_mean_s} s",
                 f"ITI mean actual   {st['realized_iti_mean_s']} s", ""]
        lines.append("block order as it will run")
        last = None
        for lvl, label, n in sess.block_order():
            if lvl != last and sess.config.operant.mode == "progressive":
                lines.append(f"  FR {sess.config.operant.ratio_set[lvl]}")
                last = lvl
            lines.append(f"    {label:<16}{n:>5}")
        lines += ["", "trials per type"]
        for k, v in sorted(st["counts_by_trial_type"].items()):
            lines.append(f"  {k:<16}{v:>5}")
        lines += ["", "which side each liquid appeared on"]
        for liq, sides in st["side_balance"].items():
            pretty = "  ".join(f"{SPOUT_LABELS[s]} {n}" for s, n in sides.items())
            lines.append(f"  {liq:<16}{pretty}")
        self.summary.setPlainText("\n".join(lines))

        out = []
        for p in audit["problems"]:
            out.append(f"PROBLEM  {p}")
        for w in audit["warnings"]:
            out.append(f"check    {w}")
        if not out:
            out.append("Every constraint held. Sides balanced, no runs "
                       "longer than allowed, reward rates match the "
                       "percentages you set.")
        self.audit.setPlainText("\n".join(out))


# =====================================================================

class MainWindow(QMainWindow):
    def __init__(self):
        super().__init__()
        self.setWindowTitle("Mouse task builder")
        self.resize(1320, 900)
        tabs = QTabWidget()
        self.setup = SetupTab()
        self.example = ExampleTab()
        tabs.addTab(self.setup, "Task setup")
        tabs.addTab(self.example, "Example task")
        self.setCentralWidget(tabs)

        def on_ready(sess, audit):
            self.example.show_session(sess, audit)
            tabs.setCurrentWidget(self.example)

        self.setup.session_ready.connect(on_ready)


def main():
    """Standalone launch of just the design pages. gui_main.py is the
    real entry point; this exists so the task builder can be used on a
    machine with no rig attached."""
    app = QApplication(sys.argv)
    theme.set_mode("light")
    app.setStyleSheet(theme.stylesheet())
    w = MainWindow()
    w.show()
    sys.exit(app.exec())


if __name__ == "__main__":
    main()
