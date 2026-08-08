"""
gui_experiment.py — the two hardware-facing pages.

InitTab   connect, exercise every actuator, capture spout positions,
          calibrate lick sensors, confirm readiness
RunTab    start a session, write the event log, watch a live raster

Both are deliberately separate from the setup pages. Task design should
be possible on a laptop with no rig; running a session should not require
re-entering a task.
"""

from __future__ import annotations

import os
import time
from collections import deque
from typing import Optional

import pyqtgraph as pg
from PyQt6.QtCore import Qt, QTimer, pyqtSignal
from PyQt6.QtGui import QFont
from PyQt6.QtWidgets import (
    QApplication, QCheckBox, QProgressBar, QComboBox, QDoubleSpinBox, QFileDialog, QFormLayout,
    QGridLayout, QGroupBox, QHBoxLayout, QLabel, QLineEdit, QMessageBox,
    QPlainTextEdit, QPushButton, QScrollArea, QSpinBox, QSplitter,
    QVBoxLayout, QWidget,
)

import theme
from theme import btn_active, btn_busy, btn_normal, btn_ready, c as tc
from arduino_link import ArduinoLink, ArduinoError, OUTCOME_NAMES
from event_log import EventLog
from session_runner import RunState, SessionRunner
from calibration import CalibrationSet
from stepper_cal import StepperCalibration
from settings import (HardwareSettings, capture_from_board, restore_to_board)
from gui_calibration import (CalibrationPanel, ModulePanel, StepperPanel,
                             StepperTablePanel)

SPOUT_NAMES = {"l": "Left", "c": "Center", "r": "Right"}


def INK():    return tc("ink")
def PAPER():  return tc("paper")
def RULE():   return tc("rule")
def MUTED():  return tc("muted")
def C_A():    return tc("liquid_a")
def C_B():    return tc("liquid_b")
def C_REWARD(): return tc("ok")


def _mono(sz=9) -> QFont:
    f = QFont("Menlo", sz)
    f.setStyleHint(QFont.StyleHint.Monospace)
    return f


def _btn(text, fn) -> QPushButton:
    b = QPushButton(text)
    b.clicked.connect(fn)
    return b


# =====================================================================
# Initialization page
# =====================================================================

class SpoutPanel(QGroupBox):
    """
    Position capture for one spout.

    Two positions are stored on the board and they are INDEPENDENT: the
    drinking position (SVEXT) and the retracted position (SVZERO). Each
    capture button writes only its own, reads the board back, and shows
    what the board now holds. Reading back matters - if the board refuses
    a value the display would otherwise keep showing the number you
    typed, and you would find out at the first trial instead of here.
    """

    def __init__(self, ch: str, get_link):
        super().__init__(f"{SPOUT_NAMES[ch]} spout")
        self.ch = ch
        self.get_link = get_link
        g = QGridLayout(self)

        self.pos = QLabel("\u2014")
        self.pos.setFont(_mono(13))
        g.addWidget(QLabel("Now at"), 0, 0)
        g.addWidget(self.pos, 0, 1)

        self.lbl_drink = QLabel("drinking: not set")
        self.lbl_retr = QLabel("retracted: \u2014")
        for w in (self.lbl_drink, self.lbl_retr):
            w.setFont(_mono(10))
        g.addWidget(self.lbl_drink, 0, 2)
        g.addWidget(self.lbl_retr, 0, 3)
        self.note = QLabel("")
        self.note.setFont(_mono(9))
        g.addWidget(self.note, 0, 4, 1, 2)

        self.step = QSpinBox(); self.step.setRange(1, 90); self.step.setValue(10)
        g.addWidget(QLabel("Increment"), 1, 0)
        g.addWidget(self.step, 1, 1)
        g.addWidget(_btn("Advance", self.fwd), 1, 2)
        g.addWidget(_btn("Withdraw", self.back), 1, 3)

        self.angle = QSpinBox(); self.angle.setRange(0, 180); self.angle.setValue(90)
        g.addWidget(QLabel("Target angle"), 2, 0)
        g.addWidget(self.angle, 2, 1)
        g.addWidget(_btn("Go", self.goto), 2, 2)
        g.addWidget(_btn("Read", self.read), 2, 3)

        # Soft limits and the 10 degree minimum exist to catch typos during
        # a session. They get in the way when you are deliberately dialling
        # a spout in, so they are overridable here - but 0-180 is a hard
        # limit and stays enforced.
        self.override = QCheckBox("Ignore soft limits and the 10\u00b0 minimum")
        self.override.setChecked(True)
        self.override.setToolTip(
            "Lets you move in small increments and past the configured soft "
            "limits.\nThe 0-180 hardware range is always enforced.")
        g.addWidget(self.override, 2, 4, 1, 2)

        self.b_drink = _btn("Store as delivery position", self.capture_extend)
        self.b_retr = _btn("Store as withdrawn position", self.capture_retract)
        g.addWidget(_btn("Drive to withdrawn position", self.goto_retracted), 3, 0, 1, 2)
        g.addWidget(self.b_drink, 3, 2, 1, 2)
        g.addWidget(self.b_retr, 3, 4, 1, 2)

        self.slew = QSpinBox(); self.slew.setRange(20, 2000); self.slew.setValue(400)
        g.addWidget(QLabel("Angular rate (deg/s)"), 4, 0)
        g.addWidget(self.slew, 4, 1)
        g.addWidget(_btn("Apply speed", self.set_slew), 4, 2)
        g.addWidget(_btn("De-energise", self.detach), 4, 3)
        g.addWidget(_btn("Drive to delivery position", self.goto_drinking), 4, 4, 1, 2)

    # ---- plumbing ----

    def _safe(self, fn, *a, **k):
        if self.get_link() is None:
            self.note.setText("not connected")
            return None
        try:
            return fn(*a, **k)
        except ArduinoError as e:
            self.note.setText(f"refused: {e}")
            self.note.setStyleSheet(f"color: {tc('bad')}")
            return None
        except Exception as e:
            self.note.setText(str(e))
            self.note.setStyleSheet(f"color: {tc('bad')}")
            return None

    def _forced(self) -> bool:
        return self.override.isChecked()

    # ---- motion ----

    def goto_retracted(self):
        self._safe(lambda: self.get_link().servo_init(self.ch))
        QTimer.singleShot(600, self.read)

    def fwd(self):
        self._safe(lambda: self.get_link().servo_forward(
            self.ch, self.step.value(), force=self._forced()))
        QTimer.singleShot(450, self.read)

    def back(self):
        self._safe(lambda: self.get_link().servo_back(
            self.ch, self.step.value(), force=self._forced()))
        QTimer.singleShot(450, self.read)

    def goto(self):
        self._safe(lambda: self.get_link().servo_write(
            self.ch, self.angle.value(), self._forced()))
        QTimer.singleShot(450, self.read)

    def goto_drinking(self):
        s = self._safe(lambda: self.get_link().servo_read(self.ch))
        if s is None:
            return
        if not s.extend_set:
            self.note.setText("no drinking position captured yet")
            self.note.setStyleSheet(f"color: {tc('bad')}")
            return
        self._safe(lambda: self.get_link().servo_write(
            self.ch, s.extend_angle, True))
        QTimer.singleShot(600, self.read)

    def set_slew(self):
        self._safe(lambda: self.get_link().servo_slew(self.ch, self.slew.value()))

    def detach(self):
        self._safe(lambda: self.get_link().servo_detach(self.ch))

    # ---- capture ----

    def _capture(self, which: str, btn):
        s = self._safe(lambda: self.get_link().servo_read(self.ch))
        if s is None:
            return
        want = s.current
        setter = (self.get_link().servo_set_extended if which == "drink"
                  else self.get_link().servo_set_retracted)
        if self._safe(lambda: setter(self.ch, want, force=self._forced())) is None:
            return

        # Read back rather than trusting the write. If the board clamped or
        # refused, the display must show what the board actually holds.
        after = self._safe(lambda: self.get_link().servo_read(self.ch))
        if after is None:
            return
        got = after.extend_angle if which == "drink" else after.zero_angle
        ok = (got == want)
        self._show(after)
        if ok:
            btn_ready(btn)
            QTimer.singleShot(1400, lambda: btn_normal(btn))
            self.note.setText(f"{'drinking' if which == 'drink' else 'retracted'}"
                              f" set to {got}\u00b0")
            self.note.setStyleSheet(f"color: {tc('ok')}")
        else:
            self.note.setText(f"board kept {got}\u00b0, not {want}\u00b0")
            self.note.setStyleSheet(f"color: {tc('bad')}")

    def capture_extend(self):
        self._capture("drink", self.b_drink)

    def capture_retract(self):
        self._capture("retract", self.b_retr)

    # ---- display ----

    def read(self):
        s = self._safe(lambda: self.get_link().servo_read(self.ch))
        if s is not None:
            self._show(s)

    def _show(self, s):
        self.pos.setText(f"{s.current}\u00b0")
        self.angle.setValue(s.current)
        self.slew.setValue(s.slew)

        if s.extend_set:
            self.lbl_drink.setText(f"drinking: {s.extend_angle}\u00b0")
            self.lbl_drink.setStyleSheet(f"color: {tc('ok')}")
        else:
            self.lbl_drink.setText("drinking: NOT SET")
            self.lbl_drink.setStyleSheet(f"color: {tc('bad')}")

        self.lbl_retr.setText(f"retracted: {s.zero_angle}\u00b0")
        self.lbl_retr.setStyleSheet(f"color: {tc('muted')}")
        if not s.pos_known:
            self.note.setText("position unverified \u2014 move to retracted once")
            self.note.setStyleSheet(f"color: {tc('warn')}")


class LickPanel(QGroupBox):
    """
    Calibration for one lick sensor.

    Calibration takes seconds and the board says nothing until it
    finishes, so the buttons carry the state: amber while the window is
    open, green when the result is in. Without that there is no way to
    know whether to keep holding contact.
    """

    def __init__(self, ch: str, get_link):
        super().__init__(f"{SPOUT_NAMES[ch]} lick sensor")
        self.ch = ch
        self.get_link = get_link
        self._busy = False
        v = QVBoxLayout(self)

        self.status = QLabel("not calibrated")
        self.status.setFont(_mono(10))
        v.addWidget(self.status)

        self.bar = QProgressBar()
        self.bar.setTextVisible(True)
        self.bar.setRange(0, 100)
        self.bar.setValue(0)
        self.bar.setMaximumHeight(14)
        self.bar.hide()
        v.addWidget(self.bar)

        row = QHBoxLayout()
        self.ms = QSpinBox(); self.ms.setRange(500, 20000); self.ms.setValue(2000)
        self.ms.setSuffix(" ms")
        self.b_base = _btn("1. Acquire baseline", self.cal_base)
        self.b_touch = _btn("2. Acquire contact level", self.cal_touch)
        self.b_read = _btn("Read", self.read)
        self.b_reset = _btn("Reset counter", self.reset_count)
        row.addWidget(QLabel("Window")); row.addWidget(self.ms)
        for w in (self.b_base, self.b_touch, self.b_read, self.b_reset):
            row.addWidget(w)
        row.addStretch()
        v.addLayout(row)

        v.addWidget(QLabel(
            "<i>Step 1 with nothing touching the spout. Step 2 with contact "
            "held for the whole window. Repeat every session \u2014 the "
            "resting value drifts with humidity and saliva.</i>"))

        self._timer = QTimer(self)
        self._timer.timeout.connect(self._tick_progress)
        self._t0 = 0.0
        self._dur = 0.0

    # ---- progress ----

    def _start_progress(self, ms: int, btn, label: str):
        self._busy = True
        self._t0 = time.time()
        self._dur = ms / 1000.0
        self.bar.setValue(0)
        self.bar.setFormat(label + "  %p%")
        self.bar.show()
        for b in (self.b_base, self.b_touch, self.b_read, self.b_reset):
            b.setEnabled(False)
        btn_busy(btn)
        self._timer.start(60)

    def _tick_progress(self):
        frac = (time.time() - self._t0) / max(0.001, self._dur)
        self.bar.setValue(min(100, int(frac * 100)))

    def _end_progress(self, ok: bool):
        self._timer.stop()
        self.bar.setValue(100)
        QTimer.singleShot(700, self.bar.hide)
        self._busy = False
        for b in (self.b_base, self.b_touch, self.b_reset):
            btn_normal(b)
            b.setEnabled(True)
        if ok:
            btn_ready(self.b_read)      # stays green: a result is waiting
        else:
            btn_normal(self.b_read)
        self.b_read.setEnabled(True)

    # ---- actions ----

    def _safe(self, fn):
        try:
            return fn()
        except ArduinoError as e:
            QMessageBox.warning(self, "Calibration refused", str(e))
        except Exception as e:
            QMessageBox.warning(self, "Something went wrong", str(e))
        return None

    def _run_cal(self, which: str, btn):
        link = self.get_link()
        if link is None:
            QMessageBox.information(self, "Not connected",
                                    "Connect to the board first.")
            return
        ms = self.ms.value()
        self._start_progress(
            ms, btn,
            "measuring resting" if which == "base" else "holding contact")
        QApplication.processEvents()
        fn = (link.lick_calibrate_baseline if which == "base"
              else link.lick_calibrate_touch)
        r = self._safe(lambda: fn(self.ch, ms))
        self._end_progress(r is not None)
        if r:
            self._show(r)

    def cal_base(self):
        self._run_cal("base", self.b_base)

    def cal_touch(self):
        self._run_cal("touch", self.b_touch)

    def read(self):
        link = self.get_link()
        if link is None:
            return
        r = self._safe(lambda: link.lick_read(self.ch))
        if r:
            self._show(r)
            btn_normal(self.b_read)

    def reset_count(self):
        link = self.get_link()
        if link is None:
            return
        if self._safe(lambda: link.lick_reset_count(self.ch)) is not None:
            btn_ready(self.b_reset)
            QTimer.singleShot(900, lambda: btn_normal(self.b_reset))
            self.read()

    def _show(self, s):
        if not s.calibrated:
            self.status.setText(f"not calibrated   (raw {s.last_raw})")
            self.status.setStyleSheet(f"color: {tc('bad')}")
            return
        snr = f"{s.snr:.1f}\u00d7 noise" if s.snr else "n/a"
        arrow = "rises" if s.polarity > 0 else "falls"
        self.status.setText(
            f"resting {s.baseline:.0f} \u00b1{s.sd:.1f}    trigger "
            f"{s.on_delta:.0f} ({snr})    signal {arrow} on contact    "
            f"licks counted {s.count}")
        weak = s.snr is not None and s.snr < 4
        self.status.setStyleSheet(f"color: {tc('bad') if weak else tc('ok')}")


class InitTab(QWidget):
    link_changed = pyqtSignal(object)

    def __init__(self, get_config, calibration=None, settings=None,
                 stepper_calibration=None):
        super().__init__()
        self.get_config = get_config
        self.link: Optional[ArduinoLink] = None
        self.calibration = calibration or CalibrationSet()
        self.stepper_cal = stepper_calibration or StepperCalibration()
        self.settings = settings or HardwareSettings()
        self._raw = deque(maxlen=1200)

        outer = QVBoxLayout(self)
        scroll = QScrollArea(); scroll.setWidgetResizable(True)
        body = QWidget(); lay = QVBoxLayout(body); scroll.setWidget(body)
        outer.addWidget(scroll, 1)

        # ---- connection ----
        g = QGroupBox("Controller connection")
        row = QHBoxLayout(g)
        # Editable: autodetection guesses from USB descriptors, and a
        # clone board or an unusual driver can present a description
        # nothing recognises. Typing the path must always be possible.
        self.port = QComboBox()
        self.port.setEditable(True)
        self.port.setMinimumWidth(320)
        self.port.setToolTip(
            "Pick a detected port, or type one:\n"
            "  macOS   /dev/cu.usbmodem1101\n"
            "  Linux   /dev/ttyACM0  or  /dev/ttyUSB0\n"
            "  Windows COM3")
        self.b_conn = _btn("Connect", self.toggle_connect)
        self.conn_label = QLabel("not connected")
        self.conn_label.setFont(_mono(9))
        row.addWidget(QLabel("Port")); row.addWidget(self.port)
        row.addWidget(_btn("Refresh list", self.refresh_ports))
        row.addWidget(self.b_conn)
        row.addWidget(_btn("Scan every port", self.probe))
        row.addWidget(_btn("No board found?", self.port_help))
        row.addWidget(self.conn_label, 1)
        lay.addWidget(g)

        self.port_hint = QLabel("")
        self.port_hint.setWordWrap(True)
        lay.addWidget(self.port_hint)
        self.refresh_ports()

        # ---- saved settings ----
        g = QGroupBox("Stored hardware configuration")
        v = QVBoxLayout(g)
        self.settings_note = QLabel(self.settings.staleness_note())
        self.settings_note.setFont(_mono(9))
        self.settings_note.setWordWrap(True)
        v.addWidget(self.settings_note)
        row = QHBoxLayout()
        row.addWidget(_btn("Save what is on the board now", self.save_settings))
        self.b_restore = _btn("Restore positions and identities",
                              lambda: self.restore_settings(False))
        row.addWidget(self.b_restore)
        row.addWidget(_btn("Restore lick thresholds too",
                           lambda: self.restore_settings(True)))
        row.addStretch()
        v.addLayout(row)
        v.addWidget(QLabel(
            "<i>Positions and identities describe the apparatus and restore "
            "cleanly. Lick thresholds are yesterday's numbers \u2014 "
            "baselines drift with humidity and saliva, so re-run the two "
            "calibration steps before an animal goes on. Pump zero is "
            "never restored: counted steps mean nothing across a power "
            "cycle.</i>"))
        lay.addWidget(g)

        # ---- spouts ----
        g = QGroupBox("Spout positioning")
        v = QVBoxLayout(g)
        v.addWidget(QLabel(
            "Capture a drinking position for every spout the task uses. "
            "Trials refuse to start without it."))
        self.spouts = {}
        for ch in ("l", "c", "r"):
            p = SpoutPanel(ch, lambda: self.link)
            self.spouts[ch] = p
            v.addWidget(p)
        row = QHBoxLayout()
        row.addWidget(_btn("Retract all", self.retract_all))
        row.addWidget(_btn("All servos limp", self.servos_off))
        row.addStretch()
        v.addLayout(row)
        lay.addWidget(g)

        # ---- solenoids ----
        g = QGroupBox("Solenoids")
        v = QVBoxLayout(g)
        v.addWidget(QLabel(
            "Send identities pushes the liquid, spout and calibration from "
            "the Task setup tab onto the board."))
        v.addWidget(_btn("Send identities to board", self.push_identities))
        self.sol_status = QPlainTextEdit(); self.sol_status.setReadOnly(True)
        self.sol_status.setFont(_mono(9)); self.sol_status.setMaximumHeight(90)
        v.addWidget(self.sol_status)
        grid = QGridLayout()
        self.sol_ms = QSpinBox(); self.sol_ms.setRange(1, 5000); self.sol_ms.setValue(50)
        self.sol_ul = QDoubleSpinBox(); self.sol_ul.setRange(0.1, 500); self.sol_ul.setValue(3.0)
        grid.addWidget(QLabel("Open duration (ms)"), 0, 0); grid.addWidget(self.sol_ms, 0, 1)
        grid.addWidget(QLabel("or \u00b5L"), 0, 4); grid.addWidget(self.sol_ul, 0, 5)
        self.sol_btns = {}
        for i in range(4):
            n = i + 1
            b_open = _btn("Open", lambda _, n=n: self.sol(n, "open"))
            b_close = _btn("Close", lambda _, n=n: self.sol(n, "close"))
            self.sol_btns[n] = (b_open, b_close)
            grid.addWidget(QLabel(f"<b>{n}</b>"), 1, i * 4)
            grid.addWidget(b_open, 1, i * 4 + 1)
            grid.addWidget(b_close, 1, i * 4 + 2)
            grid.addWidget(_btn("Pour", lambda _, n=n: self.sol(n, "ms")), 2, i * 4 + 1)
            grid.addWidget(_btn("Pour \u00b5L", lambda _, n=n: self.sol(n, "ul")), 2, i * 4 + 2)
        self._paint_sol_buttons({})
        v.addLayout(grid)
        v.addWidget(QLabel(
            "<i>Open is watchdogged at 60 seconds by the board, so a "
            "forgotten flush cannot empty a reservoir.</i>"))
        lay.addWidget(g)

        # ---- volume calibration ----
        self.cal_panel = CalibrationPanel(self.calibration, lambda: self.link)
        lay.addWidget(self.cal_panel)

        # ---- pump volume table ----
        self.step_table = StepperTablePanel(
            self.stepper_cal, lambda: self.link,
            get_speed=lambda: self.steppers["l"].sps.value()
            if hasattr(self, "steppers") else 600)
        lay.addWidget(self.step_table)

        # ---- syringe pumps ----
        g = QGroupBox("Syringe pump actuators")
        v = QVBoxLayout(g)
        v.addWidget(QLabel(
            "One pump per spout. These pull the vacuum that clears the "
            "dead space when the liquid at a spout changes."))
        self.steppers = {}
        for ch in ("l", "c", "r"):
            p = StepperPanel(ch, lambda: self.link)
            self.steppers[ch] = p
            v.addWidget(p)
        lay.addWidget(g)

        # ---- lick sensors ----
        g = QGroupBox("Lick sensors")
        v = QVBoxLayout(g)
        self.licks = {}
        for ch in ("l", "c", "r"):
            p = LickPanel(ch, lambda: self.link)
            self.licks[ch] = p
            v.addWidget(p)
        row = QHBoxLayout()
        self.watch_ch = QComboBox()
        self.watch_ch.addItems(["Left", "Center", "Right"])
        self.watch_ch.setMinimumWidth(150)
        self.b_watch = _btn("Start acquisition", self.watch_start)
        row.addWidget(QLabel("Stream unprocessed signal from"))
        row.addWidget(self.watch_ch)
        row.addWidget(self.b_watch)
        row.addWidget(_btn("Stop", self.watch_stop))
        self.min_off = QSpinBox(); self.min_off.setRange(1, 80); self.min_off.setValue(25)
        self.min_off.setSuffix(" ms")
        row.addWidget(QLabel("Contact-offset confirmation window"))
        row.addWidget(self.min_off)
        row.addWidget(_btn("Apply", self.apply_timing))
        row.addStretch()
        v.addLayout(row)

        pg.setConfigOption("background", tc("paper"))
        pg.setConfigOption("foreground", tc("ink"))
        self.raw_plot = pg.PlotWidget()
        self.raw_plot.setMinimumHeight(230)
        self.raw_plot.setLabel("left", "ADC counts")
        self.raw_plot.setLabel("bottom", "seconds")
        self.raw_plot.showGrid(x=True, y=True, alpha=0.2)
        self.raw_curve = self.raw_plot.plot(pen=pg.mkPen(tc("ink"), width=1))
        # Threshold guides, drawn once calibration is known: seeing where
        # the trace sits relative to them is the whole point of watching.
        self.raw_base = pg.InfiniteLine(angle=0, pen=pg.mkPen(
            tc("muted"), width=1, style=Qt.PenStyle.DashLine))
        self.raw_on = pg.InfiniteLine(angle=0, pen=pg.mkPen(
            tc("ok"), width=1, style=Qt.PenStyle.DashLine))
        for it in (self.raw_base, self.raw_on):
            it.hide()
            self.raw_plot.addItem(it)
        v.addWidget(self.raw_plot)
        self.raw_note = QLabel(
            "Press Start watching, then touch and release the spout. If the "
            "trace does not move, it is wiring \u2014 no calibration can "
            "invent a signal.")
        self.raw_note.setWordWrap(True)
        v.addWidget(self.raw_note)
        lay.addWidget(g)

        # ---- cue check ----
        g = QGroupBox("Stimulus verification")
        grid = QGridLayout(g)
        self.led_ms = QSpinBox(); self.led_ms.setRange(1, 60000); self.led_ms.setValue(1000)
        self.led_br = QSpinBox(); self.led_br.setRange(0, 255); self.led_br.setValue(255)
        self.led_pulse = QCheckBox("Pulsing")
        self.led_hz = QSpinBox(); self.led_hz.setRange(1, 100); self.led_hz.setValue(10)
        grid.addWidget(QLabel("LED ms"), 0, 0); grid.addWidget(self.led_ms, 0, 1)
        grid.addWidget(QLabel("Brightness"), 0, 2); grid.addWidget(self.led_br, 0, 3)
        grid.addWidget(self.led_pulse, 0, 4)
        grid.addWidget(QLabel("Hz"), 0, 5); grid.addWidget(self.led_hz, 0, 6)
        for i, ch in enumerate(("w", "b", "g")):
            grid.addWidget(_btn(f"{ch.upper()} LED",
                                lambda _, c=ch: self.test_led(c)), 1, i)

        self.tone_hz = QSpinBox(); self.tone_hz.setRange(20, 40000); self.tone_hz.setValue(10000)
        self.tone_ms = QSpinBox(); self.tone_ms.setRange(1, 60000); self.tone_ms.setValue(500)
        self.click = QCheckBox("Click train"); self.click.setChecked(True)
        self.click_hz = QSpinBox(); self.click_hz.setRange(1, 1000); self.click_hz.setValue(50)
        self.vol = QSpinBox(); self.vol.setRange(0, 50); self.vol.setValue(50)
        grid.addWidget(QLabel("Tone Hz"), 2, 0); grid.addWidget(self.tone_hz, 2, 1)
        grid.addWidget(QLabel("ms"), 2, 2); grid.addWidget(self.tone_ms, 2, 3)
        grid.addWidget(self.click, 2, 4)
        grid.addWidget(QLabel("Clicks Hz"), 2, 5); grid.addWidget(self.click_hz, 2, 6)
        grid.addWidget(QLabel("Volume"), 2, 7); grid.addWidget(self.vol, 2, 8)
        grid.addWidget(_btn("Left speaker", lambda: self.test_spk("l")), 3, 0)
        grid.addWidget(_btn("Right speaker", lambda: self.test_spk("r")), 3, 1)
        grid.addWidget(_btn("Both at once, as in a choice trial",
                            self.test_both), 3, 2, 1, 3)
        grid.addWidget(_btn("Stop everything", self.stop_all), 3, 6)
        lay.addWidget(g)

        # ---- firmware modules ----
        self.modules = ModulePanel(lambda: self.link)
        lay.addWidget(self.modules)

        # ---- readiness ----
        g = QGroupBox("Pre-session verification")
        v = QVBoxLayout(g)
        v.addWidget(_btn("Verify configuration", self.check_ready))
        self.ready_text = QPlainTextEdit(); self.ready_text.setReadOnly(True)
        self.ready_text.setFont(_mono(9)); self.ready_text.setMinimumHeight(120)
        v.addWidget(self.ready_text)
        lay.addWidget(g)
        lay.addStretch()

        self._raw_timer = QTimer(self)
        self._raw_timer.timeout.connect(self._redraw_raw)
        theme.on_change(lambda _p: self._restyle())

    def _restyle(self):
        """pyqtgraph draws with explicit pens, so it does not follow the Qt
        stylesheet and has to be repainted by hand."""
        try:
            self.raw_plot.setBackground(tc("paper"))
            self.raw_curve.setPen(pg.mkPen(tc("ink"), width=1))
            for p_, col in ((self.raw_base, "muted"), (self.raw_on, "ok")):
                p_.setPen(pg.mkPen(tc(col), width=1,
                                   style=Qt.PenStyle.DashLine))
            for pnl in self.spouts.values():
                pnl.read()
        except Exception:
            pass

    # ---- connection ----

    def refresh_ports(self):
        self.port.clear()
        ports = ArduinoLink.list_serial_ports()
        for dev, desc in ports:
            self.port.addItem(f"{dev}   {desc}", dev)

        if not ports:
            # An empty dropdown with no explanation is the worst possible
            # answer here: it looks like the program is broken when the
            # usual cause is a charge-only USB cable.
            self.port_hint.setText(
                f"<span style='color:{tc('bad')}'>No serial ports found.</span> "
                "Usual causes, in order: a charge-only USB cable (they look "
                "identical to data cables — try another), the board not "
                "plugged in, or a missing driver for a clone board. "
                "Press <b>No board found?</b> for the full list of checks.")
            return

        guess = ArduinoLink.autodetect_port()
        if guess:
            for i in range(self.port.count()):
                if self.port.itemData(i) == guess:
                    self.port.setCurrentIndex(i)
            self.port_hint.setText(
                f"{len(ports)} port(s) found; {guess} looks like the board.")
        else:
            self.port_hint.setText(
                f"{len(ports)} port(s) found, but none look like an Arduino. "
                "Pick the most likely one and press Connect — the guess "
                "is only based on the USB description, and it is often wrong "
                "for clone boards. You can also type a port path directly.")

    def toggle_connect(self):
        if self.link is not None:
            self.disconnect()
            return
        dev = self.port.currentData()
        if not dev:
            # Typed rather than chosen. Take the first whitespace-separated
            # token so pasting a whole list entry also works.
            typed = self.port.currentText().strip()
            dev = typed.split()[0] if typed else ""
        if not dev:
            QMessageBox.warning(
                self, "No port chosen",
                "Pick a port from the list, or type one:\n\n"
                "  macOS    /dev/cu.usbmodem1101\n"
                "  Linux    /dev/ttyACM0\n"
                "  Windows  COM3")
            return
        link = ArduinoLink(port=dev)
        self.conn_label.setText("connecting; the board resets on open ...")
        try:
            fw = link.connect()
        except Exception as e:
            self.conn_label.setText("not connected")
            msg = str(e)
            low = msg.lower()
            if "firmware mismatch" in low:
                tip = ("The board answered but is running different firmware. "
                       "Re-upload the sketch from the MouseTaskFirmware "
                       "folder.")
            elif "permission" in low or "access" in low:
                tip = ("Permission denied. On Linux: "
                       "sudo usermod -a -G dialout $USER, then log out and "
                       "back in. On Windows this usually means another "
                       "program has the port.")
            elif "busy" in low or "resource" in low or "in use" in low:
                tip = ("The port is held by another program. Close the "
                       "Arduino IDE Serial Monitor — only one program "
                       "can hold a port.")
            elif "no complete response" in low or "timeout" in low:
                tip = ("The port opened but the board did not answer. Either "
                       "this is not the Arduino, or the sketch is not "
                       "uploaded. Open the Serial Monitor at 115200 and type "
                       "ID; you should see R,ID,MouseTaskFirmware 0.4.0.")
            else:
                tip = ("Close the Arduino IDE Serial Monitor if it is open, "
                       "and check the cable is a data cable rather than "
                       "charge-only.")
            QMessageBox.critical(self, "Could not connect",
                                 f"{msg}\n\n{tip}\n\n"
                                 f"For a full diagnosis, run:\n"
                                 f"    python find_port.py")
            return
        self.link = link
        link.add_raw_listener(lambda ch, t, v: self._raw.append((t, v)))
        self.b_conn.setText("Disconnect")
        off = link.clock_offset or 0.0
        unc = (link.clock_uncertainty or 0.0) * 1000
        self.conn_label.setText(f"{fw}   clock \u00b1{unc:.1f} ms")
        self.link_changed.emit(link)
        for p in self.spouts.values():
            p.read()
        for p in self.licks.values():
            p.read()
        for p in self.steppers.values():
            p.read()
        self.refresh_solenoids()

    def probe(self):
        """Open every port and ask which is running our firmware. Slower
        than guessing from USB descriptors, but definitive."""
        self.port_hint.setText("Scanning every port; each takes a few "
                               "seconds because the board resets on open ...")
        QApplication.processEvents()
        try:
            hits = ArduinoLink.probe_ports()
        except Exception as e:
            self.port_hint.setText(f"Scan failed: {e}")
            return
        if not hits:
            self.port_hint.setText(
                f"<span style='color:{tc('bad')}'>No port answered.</span> "
                "Either the sketch is not uploaded, or the board is not "
                "reachable. Press <b>No board found?</b> for the checks.")
            return
        self.port.setCurrentText(hits[0])
        self.port_hint.setText(
            f"Found the firmware on {hits[0]}. Press Connect.")

    def port_help(self):
        ports = ArduinoLink.list_serial_ports()
        found = ("\n".join(f"   {d}   {desc}" for d, desc in ports)
                 if ports else "   (none)")
        QMessageBox.information(
            self, "Finding the board",
            "Ports this computer can see right now:\n"
            f"{found}\n\n"
            "If the board is missing, check in this order:\n\n"
            "1. The USB cable. Charge-only cables look identical to data "
            "cables and are the most common cause. Try another one.\n\n"
            "2. Close the Arduino IDE Serial Monitor. Only one program can "
            "hold a port.\n\n"
            "3. Does the Arduino IDE see the board under Tools > Port? If "
            "not, the problem is drivers or hardware, not this program.\n\n"
            "4. Clone boards with a CH340 chip need the CH340 driver.\n\n"
            "5. On Linux you may need to be in the dialout group:\n"
            "   sudo usermod -a -G dialout $USER\n\n"
            "6. pyserial must be installed in the same Python running this "
            "program. Run  python find_port.py  to check.\n\n"
            "You can always type a port path directly into the box.")

    def disconnect(self):
        if self.link:
            self.link.disconnect()
        self.link = None
        self.b_conn.setText("Connect")
        self.conn_label.setText("not connected")
        self.link_changed.emit(None)

    def save_settings(self):
        if not self._need():
            return
        notes = capture_from_board(self.link, self.settings)
        self.settings.calibration = self.calibration.to_json()
        self.settings.stepper_calibration = self.stepper_cal.to_json()
        self.settings.purge = self.cal_panel.purge_settings()
        try:
            path = self.settings.save()
        except Exception as e:
            QMessageBox.warning(self, "Could not save", str(e))
            return
        self.settings_note.setText(f"Saved to {path}")
        if notes:
            self.settings_note.setText(
                f"Saved to {path}\nSome values could not be read: "
                + "; ".join(notes))

    def restore_settings(self, include_lick: bool):
        if not self._need():
            return
        notes = restore_to_board(self.link, self.settings,
                                 include_lick=include_lick)
        for p in self.spouts.values():
            p.read()
        for p in self.licks.values():
            p.read()
        for p in self.steppers.values():
            p.read()
        self.refresh_solenoids()
        head = ("Restored, including lick thresholds. Re-run the two "
                "calibration steps anyway." if include_lick
                else "Restored positions and identities.")
        self.settings_note.setText(head + "  " + "  ".join(notes))
        btn_ready(self.b_restore)
        QTimer.singleShot(1200, lambda: btn_normal(self.b_restore))

    def _need(self) -> bool:
        if self.link is None:
            QMessageBox.information(self, "Not connected",
                                    "Connect to the board first.")
            return False
        return True

    def _safe(self, fn):
        if not self._need():
            return None
        try:
            return fn()
        except ArduinoError as e:
            QMessageBox.warning(self, "The board refused that", str(e))
        except Exception as e:
            QMessageBox.warning(self, "Something went wrong", str(e))
        return None

    # ---- actions ----

    def retract_all(self):
        self._safe(lambda: self.link.servo_init("all"))
        QTimer.singleShot(900, lambda: [p.read() for p in self.spouts.values()])

    def servos_off(self):
        self._safe(lambda: self.link.servos_off())

    def stop_all(self):
        self._safe(lambda: self.link.stop_all())

    def push_identities(self):
        cfg = self.get_config()
        if cfg is None:
            QMessageBox.information(
                self, "No task yet",
                "Fill in the Task setup tab and press Save and preview "
                "first, so the solenoid identities are known.")
            return
        if not self._need():
            return
        inv = {v: k for k, v in cfg.solenoid_map.items()}
        for n in range(1, 5):
            if n not in inv:
                continue
            liquid, spout = inv[n]
            self._safe(lambda n=n, l=liquid, s=spout:
                       self.link.solenoid_identity(n, l, s))
        self.refresh_solenoids()

    def refresh_solenoids(self):
        rows = self._safe(lambda: self.link.solenoid_get_all())
        if not rows:
            return
        out = []
        for s in rows:
            cal = f"{s.nl_per_ms} nL/ms" if s.nl_per_ms else "NOT CALIBRATED"
            out.append(f"{s.index}  {s.liquid or 'unset':<12} "
                       f"{SPOUT_NAMES.get(s.spout.lower(), s.spout):<8} {cal}")
        self.sol_status.setPlainText("\n".join(out))
        self._paint_sol_buttons({s.index: s.is_open for s in rows})

    def _paint_sol_buttons(self, open_state: dict):
        """Open is red while the gate is actually open; Close is green
        while it is shut. The colour tracks the hardware, not the last
        button pressed - a watchdog close has to be visible too."""
        for n, (b_open, b_close) in self.sol_btns.items():
            if open_state.get(n):
                btn_active(b_open, "OPEN")
                btn_normal(b_close, "Close")
            else:
                btn_normal(b_open, "Open")
                btn_ready(b_close, "Closed")

    def sol(self, n: int, what: str):
        if what == "open":
            self._safe(lambda: self.link.solenoid_open(n))
        elif what == "close":
            self._safe(lambda: self.link.solenoid_close(n))
        elif what == "ms":
            self._safe(lambda: self.link.solenoid_dispense_ms(n, self.sol_ms.value()))
        else:
            self._safe(lambda: self.link.solenoid_dispense_ul(n, self.sol_ul.value()))
        QTimer.singleShot(120, self.refresh_solenoids)

    def watch_start(self):
        if not self._need():
            return
        ch = "lcr"[self.watch_ch.currentIndex()]
        self._raw.clear()
        self._raw_t0 = None
        try:
            self.link.lick_stream_raw(ch, 120000)
        except ArduinoError as e:
            QMessageBox.warning(self, "The board refused that", str(e))
            return
        except Exception as e:
            QMessageBox.warning(self, "Something went wrong", str(e))
            return

        # Guides from whatever calibration this channel already has.
        try:
            st = self.link.lick_read(ch)
            if st.calibrated:
                self.raw_base.setPos(st.baseline)
                self.raw_on.setPos(st.baseline + st.polarity * st.on_delta)
                self.raw_base.show(); self.raw_on.show()
            else:
                self.raw_base.hide(); self.raw_on.hide()
        except Exception:
            pass

        btn_busy(self.b_watch, "Watching\u2026")
        self.raw_note.setText("Streaming. Touch and release the spout.")
        self._raw_timer.start(100)

    def watch_stop(self):
        self._raw_timer.stop()
        btn_normal(self.b_watch, "Start watching")
        if self.link is not None:
            self._safe(lambda: self.link.lick_stop_stream())
        self.raw_note.setText("Stopped.")

    def _redraw_raw(self):
        if not self._raw:
            return
        ts = [t for t, _ in self._raw]
        vs = [v for _, v in self._raw]
        t0 = ts[0]
        self.raw_curve.setData([(t - t0) / 1000.0 for t in ts], vs)
        lo, hi = min(vs), max(vs)
        pad = max(4, (hi - lo) * 0.2)
        self.raw_plot.setYRange(lo - pad, hi + pad)
        span = (ts[-1] - t0) / 1000.0
        self.raw_plot.setXRange(max(0.0, span - 12.0), max(1.0, span))
        self.raw_note.setText(
            f"Streaming \u00b7 {len(self._raw)} samples \u00b7 "
            f"range {lo}\u2013{hi} counts"
            + ("   (flat: check wiring)" if hi - lo < 3 else ""))

    def apply_timing(self):
        self._safe(lambda: self.link.lick_timing(5, self.min_off.value(), 15))

    def test_led(self, ch: str):
        self._safe(lambda: self.link.led(
            ch, self.led_ms.value(), pulsing=self.led_pulse.isChecked(),
            pulse_hz=self.led_hz.value(), brightness=self.led_br.value()))

    def _spk_cmd(self, ch: str) -> str:
        return ArduinoLink.speaker_cmd(
            ch, self.tone_ms.value(), self.tone_hz.value(),
            click_train=self.click.isChecked(), click_hz=self.click_hz.value(),
            volume=self.vol.value())

    def test_spk(self, ch: str):
        self._safe(lambda: self.link.speaker(
            ch, self.tone_ms.value(), self.tone_hz.value(),
            click_train=self.click.isChecked(),
            click_hz=self.click_hz.value(), volume=self.vol.value()))

    def test_both(self):
        # Armed, not sent twice: two separate commands would be
        # milliseconds apart, which for a two-tone choice cue is a real
        # confound rather than a cosmetic one.
        def go():
            self.link.disarm()
            self.link.arm(self._spk_cmd("l"))
            self.link.arm(self._spk_cmd("r"))
            self.link.go()
        self._safe(go)

    def check_ready(self):
        cfg = self.get_config()
        spouts = tuple(cfg.active_spouts) if cfg else ("l", "r")
        rep = self._safe(lambda: self.link.readiness_report(spouts))
        if rep is None:
            return
        out = []
        for p in rep["problems"]:
            out.append(f"MUST FIX   {p}")
        for w in rep["warnings"]:
            out.append(f"check      {w}")
        if not out:
            out.append("Everything the session needs is in place.")
        self.ready_text.setPlainText("\n".join(out))
        self.refresh_solenoids()


# =====================================================================
# Run page
# =====================================================================

class RunTab(QWidget):
    def __init__(self, get_link, get_session, get_calibration=None):
        super().__init__()
        self.get_link = get_link
        self.get_session = get_session
        self.get_calibration = get_calibration or (lambda: None)
        self.runner: Optional[SessionRunner] = None
        self.log: Optional[EventLog] = None
        self._dirty = True

        lay = QVBoxLayout(self)

        # ---- header ----
        g = QGroupBox("Session")
        grid = QGridLayout(g)
        self.subject = QLineEdit("mouse01")
        self.folder = QLineEdit(os.path.expanduser("~/mouse_data"))
        grid.addWidget(QLabel("Subject identifier"), 0, 0); grid.addWidget(self.subject, 0, 1)
        grid.addWidget(QLabel("Output directory"), 0, 2); grid.addWidget(self.folder, 0, 3)
        grid.addWidget(_btn("Browse", self.browse), 0, 4)

        self.b_start = _btn("Start session", self.start)
        self.b_pause = _btn("Pause after current trial", self.pause)
        self.b_stop = _btn("Terminate session", self.stop)
        self.b_pause.setEnabled(False); self.b_stop.setEnabled(False)
        grid.addWidget(self.b_start, 1, 0)
        grid.addWidget(self.b_pause, 1, 1)
        grid.addWidget(self.b_stop, 1, 2)

        self.refresh_s = QDoubleSpinBox()
        self.refresh_s.setRange(1.0, 300.0); self.refresh_s.setValue(60.0)
        self.refresh_s.setSuffix(" s")
        grid.addWidget(QLabel("Raster refresh interval"), 1, 3)
        grid.addWidget(self.refresh_s, 1, 4)

        self.state_label = QLabel("no session loaded")
        self.state_label.setFont(QFont("", 12))
        grid.addWidget(self.state_label, 2, 0, 1, 5)
        lay.addWidget(g)

        # ---- raster + summary ----
        split = QSplitter(Qt.Orientation.Horizontal)
        self.plot = pg.PlotWidget()
        self.plot.setLabel("bottom", "Seconds from cue onset")
        self.plot.setLabel("left", "Trial")
        self.plot.invertY(True)
        self.plot.showGrid(x=True, y=False, alpha=0.15)
        split.addWidget(self.plot)

        side = QWidget(); sv = QVBoxLayout(side)
        sv.addWidget(QLabel("<b>Live summary</b>"))
        self.summary = QPlainTextEdit(); self.summary.setReadOnly(True)
        self.summary.setFont(_mono(9))
        sv.addWidget(self.summary, 2)
        sv.addWidget(QLabel("<b>Recent events</b>"))
        self.tail = QPlainTextEdit(); self.tail.setReadOnly(True)
        self.tail.setFont(_mono(8)); self.tail.setMaximumBlockCount(400)
        sv.addWidget(self.tail, 3)
        split.addWidget(side)
        split.setSizes([880, 420])
        lay.addWidget(split, 1)

        theme.on_change(lambda _p: self._restyle())
        self.tick_timer = QTimer(self)
        self.tick_timer.timeout.connect(self._tick)
        self.draw_timer = QTimer(self)
        self.draw_timer.timeout.connect(self._redraw)

    def _restyle(self):
        try:
            self.plot.setBackground(tc("paper"))
            self._dirty = True
            self._redraw()
        except Exception:
            pass

    # ---- control ----

    def browse(self):
        d = QFileDialog.getExistingDirectory(self, "Where should the data go?",
                                             self.folder.text())
        if d:
            self.folder.setText(d)

    def start(self):
        link = self.get_link()
        sess = self.get_session()
        if link is None:
            QMessageBox.information(self, "Not connected",
                                    "Connect on the Hardware tab first.")
            return
        if sess is None:
            QMessageBox.information(
                self, "No task yet",
                "Build a task on the Task setup tab and press "
                "Save and preview.")
            return

        rep = link.readiness_report(tuple(sess.config.active_spouts))
        if rep["problems"]:
            QMessageBox.critical(
                self, "The rig is not ready",
                "Fix these first:\n\n  \u2022 " +
                "\n  \u2022 ".join(rep["problems"]))
            return
        if rep["warnings"]:
            ans = QMessageBox.question(
                self, "Worth a look first",
                "\n  \u2022 ".join(["These are not blocking. Start anyway?"]
                                   + rep["warnings"]))
            if ans != QMessageBox.StandardButton.Yes:
                return

        try:
            self.log = EventLog(
                self.folder.text(), self.subject.text(), sess,
                link_info={"firmware": link.firmware_id,
                           "port": link.port_name,
                           "clock_offset_s": link.clock_offset,
                           "clock_uncertainty_s": link.clock_uncertainty})
        except Exception as e:
            QMessageBox.critical(self, "Could not open the log", str(e))
            return

        link.lick_reset_all_counts()
        link.drain_events()          # start from a clean slate
        self.plot.clear()
        self._dirty = True

        self.runner = SessionRunner(link, sess, self.log,
                                    on_trial_update=self._on_trial,
                                    on_state_change=self._on_state,
                                    calibration=self.get_calibration())
        self.runner.start()
        self.tick_timer.start(40)
        self.draw_timer.start(int(self.refresh_s.value() * 1000))
        self.b_start.setEnabled(False)
        self.b_pause.setEnabled(True)
        self.b_stop.setEnabled(True)
        self.tail.appendPlainText(f"log: {self.log.csv_path}")

    def pause(self):
        if not self.runner:
            return
        if self.runner.state is RunState.PAUSED:
            self.runner.resume()
            self.b_pause.setText("Pause after current trial")
        else:
            self.runner.pause()
            self.b_pause.setText("Resume")

    def stop(self):
        if not self.runner:
            return
        ans = QMessageBox.question(
            self, "Stop the session?",
            "This aborts the trial in progress, retracts the spouts and "
            "closes the log. Data already written is kept.")
        if ans != QMessageBox.StandardButton.Yes:
            return
        self._finish()

    def _finish(self):
        self.tick_timer.stop()
        self.draw_timer.stop()
        if self.runner:
            self.runner.abort()
        if self.log:
            self.log.close()
            self.tail.appendPlainText(f"closed: {self.log.csv_path}")
            self.tail.appendPlainText(f"        {self.log.npz_path}")
        self.b_start.setEnabled(True)
        self.b_pause.setEnabled(False)
        self.b_stop.setEnabled(False)
        self._redraw()

    # ---- callbacks ----

    def _on_state(self, s: RunState):
        if self.runner:
            done, total = self.runner.progress
            self.state_label.setText(f"{s.value} \u2014 trial {done} of {total}")
        if s in (RunState.FINISHED,):
            self._finish()

    def _on_trial(self, rec):
        self._dirty = True

    def _tick(self):
        if not self.runner:
            return
        before = len(self.log.rows) if self.log else 0
        self.runner.tick()
        if self.log and len(self.log.rows) > before:
            for r in self.log.rows[before:]:
                self.tail.appendPlainText(
                    f"{r[0]:>9} {r[3]:<15} {r[4]:<2} {r[5]:>7} {r[6]:>7}")
        self._update_summary()
        if self.runner.last_error:
            err, self.runner.last_error = self.runner.last_error, None
            self.tail.appendPlainText(f"!! {err}")

    def _update_summary(self):
        if not self.runner:
            return
        s = self.runner.summary()
        lines = [
            f"trials done     {s['completed']} / {s['planned']}",
            f"rewarded        {s['rewarded']}",
            f"reward withheld {s['withheld']}",
            f"no response     {s['omitted']}  ({s['omission_rate']:.0f}%)",
            f"elapsed         {s['elapsed_s'] / 60:.1f} min",
            f"line purges     {s.get('purges_done', 0)}",
        ]
        if s["median_choice_latency_ms"] is not None:
            lines.append(f"median latency  {s['median_choice_latency_ms']} ms")
        if s["consumed_by_liquid"]:
            lines += ["", "took, all trials"]
            for liq, n in sorted(s["consumed_by_liquid"].items(),
                                 key=lambda kv: -kv[1]):
                lines.append(f"  {liq:<14}{n:>5}")
        if s["choices_by_liquid"]:
            lines += ["", f"chose, when both were offered "
                          f"({s['n_choice_trials']} trials)"]
            for liq, n in sorted(s["choices_by_liquid"].items(),
                                 key=lambda kv: -kv[1]):
                pct = 100.0 * n / max(1, s["n_choice_trials"])
                lines.append(f"  {liq:<14}{n:>5}  {pct:>5.0f}%")
        if s["preference"]:
            liq, pct = s["preference"]
            lines += ["", f"preference      {liq}, {pct:.0f}% of free choices"]
        if s["omission_rate"] > 40 and s["completed"] > 10:
            lines += ["", "Over 40% of trials had no response. Check that",
                      "the spouts are in reach and the lick sensors are",
                      "still triggering."]
        self.summary.setPlainText("\n".join(lines))

    # ---- raster ----

    def _redraw(self):
        """Repaint the live raster. Wrapped so a drawing fault can never
        interrupt an session in progress: the plot is a view, and losing
        the view is not a reason to lose the data."""
        if not self.runner or not self._dirty:
            return
        self._dirty = False
        try:
            self._draw_raster()
        except Exception as exc:
            self.tail.appendPlainText(f"!! raster not drawn: {exc}")

    def _draw_raster(self):
        self.plot.clear()

        cfg = self.runner.cfg
        recs = [r for r in self.runner.records.values() if r.cue_ms is not None]
        if not recs:
            return

        liquids = []
        for r in recs:
            for s in r.planned_spouts:
                if s.liquid not in liquids:
                    liquids.append(s.liquid)
        colour = {l: (C_A() if i == 0 else C_B() if i == 1 else tc("muted"))
                  for i, l in enumerate(liquids)}

        cue_x, cue_y, cue_c = [], [], []
        lick_x, lick_y, lick_c = [], [], []
        rew_x, rew_y = [], []
        cho_x, cho_y = [], []

        for r in recs:
            y = r.index
            n = len(r.planned_spouts)
            for k, s in enumerate(r.planned_spouts):
                off = 0.0 if n == 1 else (-0.24 if k == 0 else 0.24)
                cue_x += [0.0, s.cue.max_duration_ms() / 1000.0]
                cue_y += [y + off, y + off]
                cue_c.append(colour[s.liquid])

            spout_liquid = {s.spout: s.liquid for s in r.planned_spouts}
            for t_rel, ch, is_on in r.licks:
                if not is_on:
                    continue
                liq = spout_liquid.get(ch.lower())
                lick_x.append(t_rel / 1000.0)
                lick_y.append(y)
                lick_c.append(colour.get(liq, MUTED))

            if r.reward_t_rel is not None and r.outcome == 1:
                rew_x.append(r.reward_t_rel / 1000.0); rew_y.append(y)
            if r.choice_latency_ms is not None:
                cho_x.append(r.choice_latency_ms / 1000.0); cho_y.append(y)

        for col in set(cue_c):
            xs, ys = [], []
            for i, c in enumerate(cue_c):
                if c == col:
                    xs += cue_x[2 * i:2 * i + 2]
                    ys += cue_y[2 * i:2 * i + 2]
            self.plot.addItem(pg.PlotCurveItem(
                x=xs, y=ys, connect="pairs", pen=pg.mkPen(col, width=3)))

        if lick_x:
            self.plot.addItem(pg.ScatterPlotItem(
                x=lick_x, y=lick_y, symbol="|", size=9,
                pen=[pg.mkPen(c, width=2) for c in lick_c]))
        if cho_x:
            self.plot.addItem(pg.ScatterPlotItem(
                x=cho_x, y=cho_y, symbol="t", size=8,
                brush=pg.mkBrush(tc("ink")), pen=pg.mkPen(None)))
        if rew_x:
            self.plot.addItem(pg.ScatterPlotItem(
                x=rew_x, y=rew_y, symbol="d", size=7,
                brush=pg.mkBrush(C_REWARD()), pen=pg.mkPen(None)))

        self.plot.addItem(pg.InfiniteLine(
            pos=cfg.cue_reward_delay_ms / 1000.0, angle=90,
            pen=pg.mkPen(C_REWARD(), width=1, style=Qt.PenStyle.DashLine)))

        last_block = None
        for r in sorted(recs, key=lambda x: x.index):
            if r.block != last_block:
                last_block = r.block
                self.plot.addItem(pg.InfiniteLine(
                    pos=r.index - 0.5, angle=0, pen=pg.mkPen(RULE(), width=1)))
                t = pg.TextItem(f"  {r.block} FR{r.ratio}", color=tc("muted"),
                                anchor=(0, 0.5))
                t.setPos(-0.4, r.index + 0.5)
                self.plot.addItem(t)

        hi = max(r.index for r in recs)
        self.plot.setXRange(-0.4, cfg.omission_window_ms / 1000.0 + 1)
        self.plot.setYRange(max(0, hi - 60), hi + 2)
