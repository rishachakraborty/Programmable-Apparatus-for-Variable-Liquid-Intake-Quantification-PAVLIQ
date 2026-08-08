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
from gui_cue import describe_cue, edit_cue
from task_design import (
    BlockSpec, CueSet, LedCue, OlfactoryCue, OperantDesign, OtherCue,
    RandomRewardConfig, SessionConfig, SpeakerCue, TrialType, audit_session,
    generate_session, resolve_durations,
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


def _default_trial_cue() -> CueSet:
    return CueSet(speaker=SpeakerCue(duration_ms=500, tone_hz=10000,
                                     click_train=True, click_hz=50, volume=50))


def _default_block_cue() -> CueSet:
    return CueSet(led=LedCue(channel="w", duration_ms=2000, brightness=255))


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

# One row per trial type. The stimulus is edited in a dedicated dialog
# that exposes every parameter of every modality, so the columns here
# stay readable regardless of how many modalities a design uses.
TT_COLS = ["Name", "Liquid", "Stimulus", "Reward volume", "Reward probability"]


class TrialTypeTable(QTableWidget):
    changed = pyqtSignal()

    def __init__(self):
        super().__init__(0, len(TT_COLS))
        self.setHorizontalHeaderLabels(TT_COLS)
        self.verticalHeader().setVisible(False)
        self.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self.horizontalHeader().setStretchLastSection(True)
        for i, w in enumerate((150, 150, 300, 150, 160)):
            self.setColumnWidth(i, w)
        self.verticalHeader().setDefaultSectionSize(32)
        self.setMinimumHeight(300)
        self.setSizePolicy(QSizePolicy.Policy.Expanding,
                           QSizePolicy.Policy.Expanding)
        self._liquids: list[str] = []
        self._volumes: list = [1.0, 3.0, 6.0, 10.0]
        self._cues: list = []

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

    def set_volumes(self, volumes) -> None:
        """Offer the volumes covered by the solenoid calibration table."""
        self._volumes = list(volumes)
        for r in range(self.rowCount()):
            w = self.cellWidget(r, 3)
            if w is not None:
                self._fill_volumes(w, self._value_of(w))

    def _fill_volumes(self, combo, current):
        combo.blockSignals(True)
        combo.clear()
        for v in self._volumes:
            combo.addItem(f"{v:g}")
        combo.addItem("Other\u2026")
        combo.setCurrentText(f"{float(current):g}")
        combo.blockSignals(False)

    @staticmethod
    def _value_of(combo) -> float:
        try:
            return float(combo.currentText())
        except ValueError:
            return 3.0

    def add_row(self, tt: Optional[dict] = None) -> None:
        tt = tt or {}
        r = self.rowCount()
        self.insertRow(r)

        name = QLineEdit(tt.get("label", f"type_{r + 1}"))
        name.textChanged.connect(self.changed)
        self.setCellWidget(r, 0, name)

        liquid = QComboBox()
        liquid.setEditable(True)
        liquid.setMinimumWidth(140)
        liquid.addItems(self._liquids)
        liquid.setCurrentText(tt.get("liquid", self._liquids[0]
                                     if self._liquids else ""))
        liquid.currentTextChanged.connect(self.changed)
        self.setCellWidget(r, 1, liquid)

        cue = tt.get("cue") or _default_trial_cue()
        while len(self._cues) <= r:
            self._cues.append(CueSet())
        self._cues[r] = cue
        btn = QPushButton(describe_cue(cue))
        btn.clicked.connect(lambda _=False, row=r: self._edit_cue(row))
        self.setCellWidget(r, 2, btn)

        vol = QComboBox()
        vol.setEditable(True)
        vol.setMinimumWidth(130)
        self._fill_volumes(vol, tt.get("volume_ul", 3.0))
        vol.currentTextChanged.connect(self.changed)
        self.setCellWidget(r, 3, vol)

        pct = QDoubleSpinBox()
        pct.setRange(0.0, 100.0)
        pct.setDecimals(1)
        pct.setSuffix(" %")
        pct.setValue(float(tt.get("reward_contingency_pct", 100.0)))
        pct.valueChanged.connect(self.changed)
        self.setCellWidget(r, 4, pct)

        self.changed.emit()

    def _edit_cue(self, row: int) -> None:
        label = self.cellWidget(row, 0).text() or f"trial type {row + 1}"
        new = edit_cue(self, self._cues[row], f"Stimulus for {label}")
        if new is None:
            return
        self._cues[row] = new
        self.cellWidget(row, 2).setText(describe_cue(new))
        self.changed.emit()

    def remove_selected(self) -> None:
        rows = sorted({i.row() for i in self.selectedIndexes()}, reverse=True)
        for r in rows:
            self.removeRow(r)
            if r < len(self._cues):
                self._cues.pop(r)
        self._rebind()
        self.changed.emit()

    def _rebind(self) -> None:
        """Reconnect stimulus buttons after rows shift."""
        for r in range(self.rowCount()):
            btn = self.cellWidget(r, 2)
            if btn is None:
                continue
            try:
                btn.clicked.disconnect()
            except TypeError:
                pass
            btn.clicked.connect(lambda _=False, row=r: self._edit_cue(row))

    def to_dicts(self) -> list[dict]:
        out = []
        for r in range(self.rowCount()):
            out.append({
                "label": self.cellWidget(r, 0).text().strip(),
                "liquid": self.cellWidget(r, 1).currentText().strip(),
                "cue": self._cues[r] if r < len(self._cues) else CueSet(),
                "volume_ul": self._value_of(self.cellWidget(r, 3)),
                "reward_contingency_pct": self.cellWidget(r, 4).value(),
            })
        return out

    def labels(self) -> list[str]:
        return [d["label"] for d in self.to_dicts() if d["label"]]


# =====================================================================
# Block editor
# =====================================================================

class BlockEditor(QGroupBox):
    """
    One block definition.

    A block may be included in or excluded from the experiment without
    being deleted, so alternative designs can be compared without
    re-entering their parameters. Trial-type composition is either
    distributed uniformly across the selected types or specified
    explicitly per type.
    """
    changed = pyqtSignal()
    remove_requested = pyqtSignal(object)
    move_requested = pyqtSignal(object, int)

    def __init__(self, index: int):
        super().__init__(f"Block {index + 1}")
        grid = QGridLayout(self)

        self.enabled = QCheckBox("Include in experiment")
        self.enabled.setChecked(True)
        self.enabled.setToolTip(
            "Excluded blocks are retained with all their parameters but "
            "contribute no trials to the generated session.")
        grid.addWidget(self.enabled, 0, 0, 1, 2)

        self.name = QLineEdit(f"block_{index + 1}")
        self.kind = QComboBox()
        self.kind.addItems(["single", "choice"])
        self.kind.setMinimumWidth(140)
        self.n_trials = QSpinBox()
        self.n_trials.setRange(1, 100000)
        self.n_trials.setValue(100)

        grid.addWidget(QLabel("Identifier"), 0, 2)
        grid.addWidget(self.name, 0, 3)
        grid.addWidget(QLabel("Structure"), 0, 4)
        grid.addWidget(self.kind, 0, 5)
        grid.addWidget(QLabel("Total trials"), 0, 6)
        grid.addWidget(self.n_trials, 0, 7)

        self.n_options = QComboBox()
        self.n_options.addItems(["Automatic", "2 simultaneous options",
                                 "3 simultaneous options",
                                 "4 simultaneous options"])
        self.n_options.setMinimumWidth(210)
        self.n_options.setToolTip(
            "Number of reinforcers presented concurrently on each trial. "
            "Two reinforcers cannot be presented concurrently if they are "
            "delivered through the same spout, so the selected trial types "
            "must span at least this many spouts. Automatic uses the "
            "largest number the selected trial types can satisfy.")
        self.liquids = QLineEdit()
        self.liquids.setPlaceholderText(
            "reinforcers in this block, comma separated")
        grid.addWidget(QLabel("Reinforcers"), 1, 0)
        grid.addWidget(self.liquids, 1, 1, 1, 3)
        grid.addWidget(QLabel("Concurrent options"), 1, 4)
        grid.addWidget(self.n_options, 1, 5, 1, 3)

        # ---- trial composition ----
        comp = QGroupBox("Trial-type composition")
        cv = QVBoxLayout(comp)
        self.uniform = QCheckBox(
            "Distribute total trials uniformly across selected trial types")
        self.uniform.setChecked(True)
        self.uniform.setToolTip(
            "When enabled, the total above is divided as evenly as "
            "possible among the selected types. When disabled, the trial "
            "count for each type is specified individually and the total "
            "is their sum.")
        self.uniform.stateChanged.connect(self._uniform_changed)
        cv.addWidget(self.uniform)

        self.types = QTableWidget(0, 3)
        self.types.setHorizontalHeaderLabels(
            ["Include", "Trial type", "Trials"])
        self.types.verticalHeader().setVisible(False)
        self.types.horizontalHeader().setStretchLastSection(True)
        for i, w in enumerate((90, 260, 120)):
            self.types.setColumnWidth(i, w)
        self.types.verticalHeader().setDefaultSectionSize(28)
        self.types.setMinimumHeight(180)
        cv.addWidget(self.types)

        self.total_note = QLabel("")
        self.total_note.setFont(_mono(9))
        cv.addWidget(self.total_note)
        grid.addWidget(comp, 2, 0, 1, 8)

        # ---- transition stimulus ----
        cue_box = QGroupBox("Block-transition stimulus")
        ch = QHBoxLayout(cue_box)
        self.uncued = QCheckBox("Unsignalled transition")
        self.uncued.setToolTip(
            "No stimulus marks the change of block. Used in reversal "
            "designs, where the contingency change must be detected from "
            "outcomes rather than from an explicit signal.")
        self.uncued.stateChanged.connect(self._uncued_changed)
        ch.addWidget(self.uncued)
        self.cue = _default_block_cue()
        self.b_cue = QPushButton(describe_cue(self.cue))
        self.b_cue.clicked.connect(self._edit_cue)
        ch.addWidget(QLabel("Stimulus"))
        ch.addWidget(self.b_cue, 1)
        grid.addWidget(cue_box, 3, 0, 1, 8)

        row = QHBoxLayout()
        row.addWidget(_btn_local("Move up", lambda: self.move_requested.emit(self, -1)))
        row.addWidget(_btn_local("Move down", lambda: self.move_requested.emit(self, 1)))
        row.addStretch()
        rm = QPushButton("Delete this block")
        rm.clicked.connect(lambda: self.remove_requested.emit(self))
        row.addWidget(rm)
        hold = QWidget(); hold.setLayout(row)
        grid.addWidget(hold, 4, 0, 1, 8)

        for w in (self.name, self.liquids):
            w.textChanged.connect(self.changed)
        self.kind.currentTextChanged.connect(self.changed)
        self.n_options.currentIndexChanged.connect(self.changed)
        self.n_trials.valueChanged.connect(self._recount)
        self.enabled.stateChanged.connect(self.changed)
        self.types.itemChanged.connect(self._recount)

        self._uniform_changed()

    # ---- stimulus ----

    def _edit_cue(self):
        new = edit_cue(self, self.cue,
                       f"Block-transition stimulus for {self.name.text()}")
        if new is None:
            return
        self.cue = new
        self.b_cue.setText(describe_cue(new))
        self.changed.emit()

    def _uncued_changed(self):
        on = not self.uncued.isChecked()
        self.b_cue.setEnabled(on)
        self.changed.emit()

    # ---- composition ----

    def refresh_types(self, labels: list[str]) -> None:
        """Rebuild the trial-type list, preserving selections and counts."""
        prev = {lab: (inc, n) for lab, inc, n in self._rows()}
        self.types.blockSignals(True)
        self.types.setRowCount(0)
        for lab in labels:
            r = self.types.rowCount()
            self.types.insertRow(r)
            inc = QTableWidgetItem()
            inc.setFlags(Qt.ItemFlag.ItemIsUserCheckable
                         | Qt.ItemFlag.ItemIsEnabled)
            was_in = prev.get(lab, (False, 0))[0]
            inc.setCheckState(Qt.CheckState.Checked if was_in
                              else Qt.CheckState.Unchecked)
            self.types.setItem(r, 0, inc)

            name = QTableWidgetItem(lab)
            name.setFlags(Qt.ItemFlag.ItemIsEnabled)
            self.types.setItem(r, 1, name)

            cnt = QTableWidgetItem(str(prev.get(lab, (False, 0))[1]))
            self.types.setItem(r, 2, cnt)
        self.types.blockSignals(False)
        self._recount()

    def _rows(self):
        for r in range(self.types.rowCount()):
            inc = self.types.item(r, 0)
            nm = self.types.item(r, 1)
            cnt = self.types.item(r, 2)
            if inc is None or nm is None:
                continue
            try:
                n = int(float(cnt.text())) if cnt and cnt.text() else 0
            except ValueError:
                n = 0
            yield nm.text(), inc.checkState() == Qt.CheckState.Checked, n

    def _uniform_changed(self):
        editable = not self.uniform.isChecked()
        self.n_trials.setEnabled(self.uniform.isChecked())
        self.types.blockSignals(True)
        for r in range(self.types.rowCount()):
            it = self.types.item(r, 2)
            if it is None:
                continue
            flags = (Qt.ItemFlag.ItemIsEnabled | Qt.ItemFlag.ItemIsSelectable)
            if editable:
                flags |= Qt.ItemFlag.ItemIsEditable
            it.setFlags(flags)
        self.types.blockSignals(False)
        self._recount()

    def _recount(self):
        """Recompute and display per-type trial counts."""
        rows = list(self._rows())
        selected = [lab for lab, inc, _ in rows if inc]

        self.types.blockSignals(True)
        if self.uniform.isChecked():
            n = self.n_trials.value()
            k = len(selected)
            base, rem = (n // k, n % k) if k else (0, 0)
            for r, (lab, inc, _) in enumerate(rows):
                give = 0
                if inc:
                    i = selected.index(lab)
                    give = base + (1 if i < rem else 0)
                it = self.types.item(r, 2)
                if it is not None:
                    it.setText(str(give))
            total = n if selected else 0
        else:
            for r, (lab, inc, n) in enumerate(rows):
                if not inc:
                    it = self.types.item(r, 2)
                    if it is not None:
                        it.setText("0")
            total = sum(n for _lab, inc, n in self._rows() if inc)
        self.types.blockSignals(False)

        if not selected:
            self.total_note.setText("No trial types selected: this block "
                                    "will contribute no trials.")
        else:
            self.total_note.setText(
                f"{len(selected)} trial types selected, {total} trials total"
                + ("" if self.uniform.isChecked()
                   else "  (total is the sum of the counts above)"))
        self.changed.emit()

    # ---- serialisation ----

    def to_dict(self) -> dict:
        rows = list(self._rows())
        counts = {lab: n for lab, inc, n in rows if inc}
        total = (self.n_trials.value() if self.uniform.isChecked()
                 else sum(counts.values()))
        return {
            "enabled": self.enabled.isChecked(),
            "label": self.name.text().strip(),
            "kind": self.kind.currentText(),
            "liquids": [x.strip() for x in self.liquids.text().split(",")
                        if x.strip()],
            "n_trials": max(0, total),
            "trial_type_labels": [lab for lab, inc, _ in rows if inc],
            "trial_type_counts": counts,
            "uniform": self.uniform.isChecked(),
            "n_options": (None if self.n_options.currentIndex() == 0
                          else self.n_options.currentIndex() + 1),
            "uncued": self.uncued.isChecked(),
            "cue": None if self.uncued.isChecked() else self.cue,
        }

    def from_dict(self, d: dict) -> None:
        self.enabled.setChecked(d.get("enabled", True))
        self.name.setText(d.get("label", ""))
        self.kind.setCurrentText(d.get("kind", "single"))
        self.liquids.setText(", ".join(d.get("liquids", [])))
        self.n_trials.setValue(max(1, d.get("n_trials", 100)))
        self.uniform.setChecked(d.get("uniform", True))
        idx = d.get("n_options")
        self.n_options.setCurrentIndex(0 if idx is None else max(0, idx - 1))
        self.uncued.setChecked(d.get("uncued", False))
        cue = d.get("cue")
        if isinstance(cue, CueSet):
            self.cue = cue
            self.b_cue.setText(describe_cue(cue))

        want = set(d.get("trial_type_labels", []))
        counts = d.get("trial_type_counts", {})
        self.types.blockSignals(True)
        for r in range(self.types.rowCount()):
            lab = self.types.item(r, 1).text()
            self.types.item(r, 0).setCheckState(
                Qt.CheckState.Checked if lab in want else Qt.CheckState.Unchecked)
            self.types.item(r, 2).setText(str(counts.get(lab, 0)))
        self.types.blockSignals(False)
        self._uniform_changed()


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
        g = QGroupBox("Reinforcers")
        f = QFormLayout(g)
        self.liquids = QLineEdit("alcohol, water")
        self.liquids.textChanged.connect(self._liquids_changed)
        f.addRow("Reinforcer identifiers (comma separated)", self.liquids)
        f.addRow(QLabel("<i>These names appear wherever a liquid is chosen. "
                        "Commas separate names, so a name cannot contain "
                        "one.</i>"))
        lay.addWidget(g)

        # ---- solenoids ----
        g = QGroupBox("Solenoids")
        v = QVBoxLayout(g)
        v.addWidget(QLabel(
            "What each gate carries and where it goes. Nothing about "
            "calibration lives here — that is measured once on the "
            "Hardware tab and applies wherever the solenoid is used."))
        self.sol_table = QTableWidget(0, 4)
        self.sol_table.setHorizontalHeaderLabels(
            ["Solenoid", "In use", "Liquid", "Goes to"])
        self.sol_table.verticalHeader().setVisible(False)
        self.sol_table.horizontalHeader().setStretchLastSection(True)
        self.sol_table.verticalHeader().setDefaultSectionSize(32)
        for i, w in enumerate((100, 80, 200, 160)):
            self.sol_table.setColumnWidth(i, w)
        self.sol_table.setMinimumHeight(230)
        self.sol_table.setSizePolicy(QSizePolicy.Policy.Expanding,
                                     QSizePolicy.Policy.Expanding)
        self.sol_widgets: list = []
        v.addWidget(self.sol_table)

        row = QHBoxLayout()
        row.addWidget(_btn_local("Add solenoid", self.add_solenoid))
        row.addWidget(_btn_local("Remove last", self.remove_solenoid))
        row.addStretch()
        row.addWidget(QLabel("<i>Up to 8. Untick In use for a gate that is "
                             "not wired — it will refuse to open.</i>"))
        v.addLayout(row)
        lay.addWidget(g)

        # ---- trial types ----        # ---- trial types ----
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
        g = QGroupBox("Block definitions")
        self.block_box = QVBoxLayout(g)
        self.blocks: list[BlockEditor] = []
        row = QHBoxLayout()
        b_add = QPushButton("Add block definition")
        b_add.clicked.connect(lambda: self._add_block())
        row.addWidget(b_add)
        row.addStretch()
        row.addWidget(QLabel(
            "<i>Definitions are retained whether or not they are included "
            "in the experiment.</i>"))
        self.block_box.addLayout(row)
        lay.addWidget(g)

        # ---- experiment composition ----
        g = QGroupBox("Experiment composition")
        v = QVBoxLayout(g)
        v.addWidget(QLabel(
            "Blocks included in the experiment, in the order defined "
            "above. Block presentation order is redrawn independently at "
            "each reinforcement-schedule level unless order randomisation "
            "is disabled."))
        self.exp_summary = QPlainTextEdit()
        self.exp_summary.setReadOnly(True)
        self.exp_summary.setFont(_mono(9))
        self.exp_summary.setMinimumHeight(160)
        v.addWidget(self.exp_summary)
        lay.addWidget(g)

        # ---- timing ----
        g = QGroupBox("Temporal parameters")
        f = QFormLayout(g)
        self.cue_reward = QSpinBox(); self.cue_reward.setRange(0, 60000); self.cue_reward.setValue(1000)
        self.omission = QSpinBox(); self.omission.setRange(0, 120000); self.omission.setValue(5000)
        self.retract_delay = QSpinBox(); self.retract_delay.setRange(0, 60000); self.retract_delay.setValue(1000)
        self.gate = QSpinBox(); self.gate.setRange(0, 10000); self.gate.setValue(500)
        self.iti_mean = QDoubleSpinBox(); self.iti_mean.setRange(0.1, 600); self.iti_mean.setValue(8.0)
        self.iti_min = QDoubleSpinBox(); self.iti_min.setRange(0.0, 600); self.iti_min.setValue(3.0)
        self.iti_max = QDoubleSpinBox(); self.iti_max.setRange(0.1, 600); self.iti_max.setValue(30.0)
        f.addRow("Cue-to-reinforcer delay (ms)", self.cue_reward)
        f.addRow("Response window / omission criterion (ms)", self.omission)
        f.addRow("Post-reinforcement retraction delay (ms)", self.retract_delay)
        f.addRow("Required lick-free interval before trial onset (ms)", self.gate)
        f.addRow("Inter-trial interval: exponential scale (s)", self.iti_mean)
        f.addRow("Inter-trial interval: lower bound (s)", self.iti_min)
        f.addRow("Inter-trial interval: upper bound (s)", self.iti_max)
        f.addRow(QLabel(
            "<i>Inter-trial intervals are drawn from an exponential distribution "
            "with the scale above, offset by the lower bound and truncated at "
            "the upper bound. Truncation reduces the realised mean below "
            "lower bound plus scale; the generated-session summary reports "
            "the realised value, which is the one to cite.</i>"))
        lay.addWidget(g)

        # ---- operant ----
        g = QGroupBox("Reinforcement schedule")
        f = QFormLayout(g)
        self.op_mode = QComboBox()
        self.op_mode.addItems(["none", "fixed", "variable", "progressive"])
        self.op_mode.setMinimumWidth(180)
        self.op_mode.setCurrentText("progressive")
        self.op_fixed = QSpinBox(); self.op_fixed.setRange(1, 500); self.op_fixed.setValue(1)
        self.op_mean = QSpinBox(); self.op_mean.setRange(1, 500); self.op_mean.setValue(3)
        self.op_set = QLineEdit("1, 2, 4, 8")
        f.addRow("Schedule type", self.op_mode)
        f.addRow("Fixed ratio: responses per reinforcer", self.op_fixed)
        f.addRow("Variable ratio: mean responses per reinforcer", self.op_mean)
        f.addRow("Progressive ratio: response requirements", self.op_set)
        f.addRow(QLabel(
            "<i>Under a progressive-ratio schedule the total trial count is "
            "divided evenly among the listed response requirements. Every "
            "block is presented at each requirement before the requirement "
            "advances, so schedule level is not confounded with block "
            "order.</i>"))
        lay.addWidget(g)

        # ---- behaviour toggles ----
        g = QGroupBox("Trial structure")
        f = QFormLayout(g)
        self.retraction = QCheckBox("Withdraw spouts within the trial")
        self.retraction.setChecked(True)
        self.retraction.setToolTip(
            "Off leaves both spouts out for the whole trial. Suits "
            "habituation or free access, but it also removes what stops "
            "an animal sampling both spouts on a choice trial, so choice "
            "data from such a session means something different.")
        self.rand_sides = QCheckBox("Counterbalance reinforcer-to-spout assignment across trials")
        self.rand_sides.setChecked(True)
        self.rand_sides.setToolTip(
            "On, a liquid alternates sides under the balance and repeat "
            "limits below. Off pins each liquid to one spout, which lets "
            "side preference masquerade as liquid preference.")
        self.purge_on = QCheckBox("Purge the delivery line when a spout's reinforcer changes")
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

        # ---- random rewards ----
        g = QGroupBox("Cue-independent reinforcement")
        f = QFormLayout(g)
        f.addRow(QLabel(
            "<i>Three separate controls. They can be combined, and each is "
            "logged distinctly so they are never pooled in analysis.</i>"))

        self.free_on = QCheckBox("Unsignalled reinforcement during the inter-trial interval")
        self.free_on.setToolTip(
            "Unsignalled drops at random times in the ITI. The standard "
            "control for whether the animal is working for the reward or "
            "just consuming it.")
        self.free_rate = QDoubleSpinBox(); self.free_rate.setRange(0.05, 60)
        self.free_rate.setValue(1.0); self.free_rate.setSuffix(" per min")
        self.free_ul = QDoubleSpinBox(); self.free_ul.setRange(0.1, 100)
        self.free_ul.setValue(3.0); self.free_ul.setSuffix(" \u00b5L")
        self.free_max = QSpinBox(); self.free_max.setRange(1, 10)
        self.free_max.setValue(1)
        f.addRow(self.free_on)
        r1 = QHBoxLayout()
        for lab, w in (("Poisson rate", self.free_rate), ("Volume", self.free_ul),
                       ("Maximum per interval", self.free_max)):
            r1.addWidget(QLabel(lab)); r1.addWidget(w)
        r1.addStretch()
        hold1 = QWidget(); hold1.setLayout(r1)
        f.addRow("", hold1)

        self.uncued_on = QCheckBox("Unsignalled trials (stimulus omitted)")
        self.uncued_on.setToolTip(
            "The response requirement still applies. Asks whether the "
            "animal needs the cue at all.")
        self.uncued_pct = QDoubleSpinBox(); self.uncued_pct.setRange(0, 100)
        self.uncued_pct.setValue(10.0); self.uncued_pct.setSuffix(" %")
        f.addRow(self.uncued_on)
        f.addRow("   proportion of trials", self.uncued_pct)

        self.decouple_on = QCheckBox("Magnitude decoupled from the discriminative stimulus")
        self.decouple_on.setToolTip(
            "Breaks the cue-to-magnitude mapping on a fraction of trials, "
            "drawing from the other amounts that liquid uses.")
        self.decouple_pct = QDoubleSpinBox(); self.decouple_pct.setRange(0, 100)
        self.decouple_pct.setValue(10.0); self.decouple_pct.setSuffix(" %")
        f.addRow(self.decouple_on)
        f.addRow("   proportion of trials", self.decouple_pct)
        lay.addWidget(g)

        # ---- randomization ----
        g = QGroupBox("Sequence randomisation")
        f = QFormLayout(g)
        self.max_repeat = QSpinBox(); self.max_repeat.setRange(1, 20); self.max_repeat.setValue(3)
        self.balance = QSpinBox(); self.balance.setRange(2, 200); self.balance.setValue(20)
        self.rand_blocks = QCheckBox("Randomise block order at each schedule level")
        self.rand_blocks.setChecked(True)
        self.seed = QLineEdit(); self.seed.setPlaceholderText("leave empty to pick one at random")
        f.addRow("Maximum consecutive repetitions", self.max_repeat)
        f.addRow("Counterbalancing window (trials)", self.balance)
        f.addRow("", self.rand_blocks)
        f.addRow("Random seed (blank to generate)", self.seed)
        lay.addWidget(g)
        # ---- hardware sequence ----
        g = QGroupBox("Trial event sequence")
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
            self.tt.add_row({
                "label": f"alc_{hz}", "liquid": "alcohol", "volume_ul": ul,
                "cue": CueSet(speaker=SpeakerCue(
                    duration_ms=500, tone_hz=12000, click_train=True,
                    click_hz=hz, volume=50))})
            self.tt.add_row({
                "label": f"wat_{hz}", "liquid": "water", "volume_ul": ul,
                "cue": CueSet(speaker=SpeakerCue(
                    duration_ms=500, tone_hz=5000, click_train=True,
                    click_hz=hz, volume=50))})
        for i, (name, kind, liqs, ch) in enumerate([
                ("alcohol_only", "single", "alcohol", "w"),
                ("water_only", "single", "water", "b"),
                ("choice", "choice", "alcohol, water", "g")]):
            b = self._add_block()
            b.name.setText(name)
            b.kind.setCurrentText(kind)
            b.liquids.setText(liqs)
            b.cue = CueSet(led=LedCue(channel=ch, duration_ms=2000,
                                      brightness=255))
            b.b_cue.setText(describe_cue(b.cue))
            want = ("alc" if liqs == "alcohol" else
                    "wat" if liqs == "water" else "")
            b.types.blockSignals(True)
            for j in range(b.types.rowCount()):
                t = b.types.item(j, 1).text()
                on = t.startswith(want) if want else True
                b.types.item(j, 0).setCheckState(
                    Qt.CheckState.Checked if on else Qt.CheckState.Unchecked)
            b.types.blockSignals(False)
            b._recount()
        for liq, sp in (("water", "l"), ("alcohol", "l"),
                        ("water", "r"), ("alcohol", "r")):
            self.add_solenoid(liq, sp, True)

    # ---- wiring ----

    def add_solenoid(self, liquid: str = "", spout: str = "l",
                     present: bool = True) -> None:
        r = self.sol_table.rowCount()
        if r >= 16:
            QMessageBox.information(
                self, "Channel limit reached",
                "The controller is configured for 16 solenoid channels. "
                "Raising this requires assigning further digital output "
                "pins in the controller configuration.")
            return
        self.sol_table.insertRow(r)
        it = QTableWidgetItem(str(r + 1))
        it.setFlags(Qt.ItemFlag.ItemIsEnabled)
        self.sol_table.setItem(r, 0, it)

        active = QCheckBox(); active.setChecked(present)
        liq = QComboBox(); liq.setEditable(True); liq.setMinimumWidth(170)
        liq.addItems(self._liquid_names())
        if liquid:
            liq.setCurrentText(liquid)
        sp = QComboBox(); sp.addItems(["Left", "Center", "Right"])
        sp.setMinimumWidth(150)
        sp.setCurrentIndex("lcr".index(spout[:1]))
        self.sol_table.setCellWidget(r, 1, active)
        self.sol_table.setCellWidget(r, 2, liq)
        self.sol_table.setCellWidget(r, 3, sp)
        self.sol_widgets.append((active, liq, sp))
        for w in (active,):
            w.stateChanged.connect(self._preview_sequence)
        liq.currentTextChanged.connect(self._preview_sequence)
        sp.currentIndexChanged.connect(self._preview_sequence)

    def remove_solenoid(self) -> None:
        if not self.sol_widgets:
            return
        self.sol_table.removeRow(self.sol_table.rowCount() - 1)
        self.sol_widgets.pop()
        self._preview_sequence()

    def _liquid_names(self) -> list[str]:
        return [s.strip() for s in self.liquids.text().split(",") if s.strip()]

    def _liquids_changed(self) -> None:
        names = self._liquid_names()
        self.tt.set_liquids(names)
        for _, liquid, _ in self.sol_widgets:
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
        vols = self.calibration.known_volumes()
        if vols:
            self.tt.set_volumes(vols)
        self._refresh_experiment()

    def _add_block(self) -> BlockEditor:
        b = BlockEditor(len(self.blocks))
        b.remove_requested.connect(self._remove_block)
        b.move_requested.connect(self._move_block)
        b.changed.connect(self._refresh_experiment)
        b.refresh_types(self.tt.labels())
        self.blocks.append(b)
        self.block_box.insertWidget(self.block_box.count() - 1, b)
        self._refresh_experiment()
        return b

    def _remove_block(self, b: BlockEditor) -> None:
        self.blocks.remove(b)
        b.setParent(None)
        self._relabel_blocks()
        self._refresh_experiment()

    def _move_block(self, b: BlockEditor, delta: int) -> None:
        i = self.blocks.index(b)
        j = i + delta
        if not (0 <= j < len(self.blocks)):
            return
        self.blocks[i], self.blocks[j] = self.blocks[j], self.blocks[i]
        for w in self.blocks:
            self.block_box.removeWidget(w)
        for k, w in enumerate(self.blocks):
            self.block_box.insertWidget(k + 1, w)
        self._relabel_blocks()
        self._refresh_experiment()

    def _relabel_blocks(self) -> None:
        for i, b in enumerate(self.blocks):
            b.setTitle(f"Block {i + 1}")

    def _refresh_experiment(self) -> None:
        """Summarise which blocks contribute to the generated session."""
        if not hasattr(self, "exp_summary"):
            return
        lines, total = [], 0
        for i, b in enumerate(self.blocks):
            d = b.to_dict()
            mark = "included" if d["enabled"] and d["n_trials"] > 0 else "excluded"
            if mark == "included":
                total += d["n_trials"]
            comp = ", ".join(f"{k}\u00d7{v}" for k, v
                             in sorted(d["trial_type_counts"].items()) if v)
            lines.append(
                f"{i + 1}. {d['label'] or '(unnamed)':<18} {mark:<9} "
                f"{d['kind']:<7} {d['n_trials']:>5} trials   {comp}")
        lines.append("")
        lines.append(f"Total trials in the experiment: {total}")
        self.exp_summary.setPlainText("\n".join(lines))

    # ---- config assembly ----

    def to_config(self) -> SessionConfig:
        types = []
        for d in self.tt.to_dicts():
            types.append(TrialType(
                label=d["label"], liquid=d["liquid"], cue=d["cue"],
                volume_ul=d["volume_ul"],
                reward_contingency_pct=d["reward_contingency_pct"]))

        blocks = []
        for b in self.blocks:
            d = b.to_dict()
            if not d["enabled"] or d["n_trials"] <= 0:
                continue
            cue = d["cue"]
            if cue is not None and cue.is_empty():
                cue = None
            blocks.append(BlockSpec(
                label=d["label"], kind=d["kind"], liquids=d["liquids"],
                n_trials=d["n_trials"],
                trial_type_labels=d["trial_type_labels"], cue=cue,
                n_options=d.get("n_options"),
                trial_type_counts=d.get("trial_type_counts") or None))

        sol_map, active = {}, set()
        for i, (on, liquid, spout) in enumerate(self.sol_widgets):
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
            random_reward=RandomRewardConfig(
                free_enabled=self.free_on.isChecked(),
                free_rate_per_min=self.free_rate.value(),
                free_volume_ul=self.free_ul.value(),
                free_max_per_trial=self.free_max.value(),
                uncued_enabled=self.uncued_on.isChecked(),
                uncued_pct=self.uncued_pct.value(),
                decouple_enabled=self.decouple_on.isChecked(),
                decouple_pct=self.decouple_pct.value()),
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
                           "spout": "lcr"[s.currentIndex()]}
                          for a, l, s in self.sol_widgets],
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
            "random_reward": {"free": self.free_on.isChecked(),
                              "rate": self.free_rate.value(),
                              "ul": self.free_ul.value(),
                              "max": self.free_max.value(),
                              "uncued": self.uncued_on.isChecked(),
                              "uncued_pct": self.uncued_pct.value(),
                              "decouple": self.decouple_on.isChecked(),
                              "decouple_pct": self.decouple_pct.value()},
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

        while self.sol_widgets:
            self.remove_solenoid()
        for row in d.get("solenoids", [])[:16]:
            self.add_solenoid(row.get("liquid", ""), row.get("spout", "l"),
                              row.get("active", True))

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

        rr = d.get("random_reward", {})
        self.free_on.setChecked(rr.get("free", False))
        self.free_rate.setValue(rr.get("rate", 1.0))
        self.free_ul.setValue(rr.get("ul", 3.0))
        self.free_max.setValue(rr.get("max", 1))
        self.uncued_on.setChecked(rr.get("uncued", False))
        self.uncued_pct.setValue(rr.get("uncued_pct", 10.0))
        self.decouple_on.setChecked(rr.get("decouple", False))
        self.decouple_pct.setValue(rr.get("decouple_pct", 10.0))

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
