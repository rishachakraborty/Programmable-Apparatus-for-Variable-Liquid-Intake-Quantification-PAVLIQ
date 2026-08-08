"""
gui_cue.py — full-parameter editor for a cue set.

A cue set is any combination of auditory, visual, olfactory and
user-defined stimuli presented together. This module provides one editor
that exposes every parameter of every modality, and both trial cues and
block-transition cues use it. Because there is a single implementation,
the parameters available in one context are always identical to those
available in the other.

All enabled modalities within a set are hardware-triggered from a single
staged batch, so they share one onset timestamp rather than being issued
sequentially.
"""

from __future__ import annotations

from typing import Optional

from PyQt6.QtCore import Qt
from PyQt6.QtGui import QFont
from PyQt6.QtWidgets import (
    QCheckBox, QComboBox, QDialog, QDialogButtonBox, QDoubleSpinBox,
    QFormLayout, QGroupBox, QHBoxLayout, QLabel, QLineEdit, QPushButton,
    QSpinBox, QVBoxLayout, QWidget,
)

from task_design import CueSet, LedCue, OlfactoryCue, OtherCue, SpeakerCue

LED_CHANNELS = [("w", "White"), ("b", "Blue"), ("g", "Green")]


def _mono(sz=9) -> QFont:
    f = QFont("Menlo", sz)
    f.setStyleHint(QFont.StyleHint.Monospace)
    return f


class SpeakerCueEditor(QGroupBox):
    """Auditory stimulus parameters."""

    def __init__(self):
        super().__init__("Auditory stimulus")
        self.setCheckable(True)
        f = QFormLayout(self)

        self.duration = QSpinBox()
        self.duration.setRange(1, 60000)
        self.duration.setValue(500)
        self.duration.setSuffix(" ms")

        self.tone_hz = QSpinBox()
        self.tone_hz.setRange(20, 40000)
        self.tone_hz.setSingleStep(500)
        self.tone_hz.setValue(10000)
        self.tone_hz.setSuffix(" Hz")

        self.click_train = QCheckBox("Amplitude-modulate the carrier "
                                     "(click train)")
        self.click_train.setChecked(True)

        self.click_hz = QSpinBox()
        self.click_hz.setRange(1, 1000)
        self.click_hz.setValue(50)
        self.click_hz.setSuffix(" Hz")

        self.loudness = QSpinBox()
        self.loudness.setRange(0, 50)
        self.loudness.setValue(50)
        self.loudness.setSuffix(" % duty")

        f.addRow("Stimulus duration", self.duration)
        f.addRow("Carrier frequency", self.tone_hz)
        f.addRow("", self.click_train)
        f.addRow("Modulation rate", self.click_hz)
        f.addRow("Output amplitude", self.loudness)
        f.addRow(QLabel(
            "<i>Carrier frequency identifies the stimulus. Modulation rate "
            "is an independent dimension, commonly used to encode reward "
            "magnitude. Output amplitude is the square-wave duty cycle "
            "driving the transducer, not a calibrated sound pressure "
            "level; measure with a sound level meter if intensity is an "
            "experimental variable.</i>"))

        self.click_train.stateChanged.connect(self._sync)
        self._sync()

    def _sync(self):
        self.click_hz.setEnabled(self.click_train.isChecked())

    def load(self, cue: Optional[SpeakerCue]):
        self.setChecked(cue is not None)
        if cue is None:
            return
        self.duration.setValue(cue.duration_ms)
        self.tone_hz.setValue(cue.tone_hz)
        self.click_train.setChecked(cue.click_train)
        self.click_hz.setValue(cue.click_hz)
        self.loudness.setValue(cue.volume)
        self._sync()

    def dump(self) -> Optional[SpeakerCue]:
        if not self.isChecked():
            return None
        return SpeakerCue(duration_ms=self.duration.value(),
                          tone_hz=self.tone_hz.value(),
                          click_train=self.click_train.isChecked(),
                          click_hz=self.click_hz.value(),
                          volume=self.loudness.value())


class LedCueEditor(QGroupBox):
    """Visual stimulus parameters."""

    def __init__(self):
        super().__init__("Visual stimulus")
        self.setCheckable(True)
        f = QFormLayout(self)

        self.channel = QComboBox()
        for _code, name in LED_CHANNELS:
            self.channel.addItem(name)
        self.channel.setMinimumWidth(160)

        self.duration = QSpinBox()
        self.duration.setRange(1, 60000)
        self.duration.setValue(1000)
        self.duration.setSuffix(" ms")

        self.pulsing = QCheckBox("Modulate the output (flicker)")

        self.pulse_hz = QSpinBox()
        self.pulse_hz.setRange(1, 100)
        self.pulse_hz.setValue(10)
        self.pulse_hz.setSuffix(" Hz")

        self.brightness = QSpinBox()
        self.brightness.setRange(0, 255)
        self.brightness.setValue(255)
        self.brightness.setSuffix(" / 255")

        f.addRow("Emitter", self.channel)
        f.addRow("Stimulus duration", self.duration)
        f.addRow("", self.pulsing)
        f.addRow("Modulation rate", self.pulse_hz)
        f.addRow("Output intensity", self.brightness)
        f.addRow(QLabel(
            "<i>Intensity is set by pulse-width modulation at a 122 Hz "
            "carrier, above the rodent flicker-fusion threshold, so a "
            "non-modulated stimulus is perceived as continuous. The "
            "modulation rate above is an additional, slower envelope "
            "imposed on that carrier.</i>"))

        self.pulsing.stateChanged.connect(self._sync)
        self._sync()

    def _sync(self):
        self.pulse_hz.setEnabled(self.pulsing.isChecked())

    def load(self, cue: Optional[LedCue]):
        self.setChecked(cue is not None)
        if cue is None:
            return
        codes = [c for c, _ in LED_CHANNELS]
        self.channel.setCurrentIndex(codes.index(cue.channel[:1])
                                     if cue.channel[:1] in codes else 0)
        self.duration.setValue(cue.duration_ms)
        self.pulsing.setChecked(cue.pulsing)
        self.pulse_hz.setValue(max(1, cue.pulse_hz))
        self.brightness.setValue(cue.brightness)
        self._sync()

    def dump(self) -> Optional[LedCue]:
        if not self.isChecked():
            return None
        return LedCue(channel=LED_CHANNELS[self.channel.currentIndex()][0],
                      duration_ms=self.duration.value(),
                      pulsing=self.pulsing.isChecked(),
                      pulse_hz=self.pulse_hz.value(),
                      brightness=self.brightness.value())


class OlfactoryCueEditor(QGroupBox):
    """Olfactory stimulus parameters."""

    def __init__(self):
        super().__init__("Olfactory stimulus")
        self.setCheckable(True)
        self.setChecked(False)
        f = QFormLayout(self)

        self.channel = QLineEdit("a")
        self.channel.setMaximumWidth(90)
        self.duration = QSpinBox()
        self.duration.setRange(1, 60000)
        self.duration.setValue(500)
        self.duration.setSuffix(" ms")
        self.label = QLineEdit("odour")

        f.addRow("Odour channel", self.channel)
        f.addRow("Stimulus duration", self.duration)
        f.addRow("Descriptive label", self.label)
        f.addRow(QLabel(
            "<i>Issues an olfactometer command to the controller. If the "
            "installed firmware does not implement olfactometer control, "
            "the command is rejected and the rejection is written to the "
            "event log, so trials that requested an unavailable stimulus "
            "are identifiable during analysis rather than silently "
            "indistinguishable from delivered ones.</i>"))

    def load(self, cue: Optional[OlfactoryCue]):
        self.setChecked(cue is not None)
        if cue is None:
            return
        self.channel.setText(cue.channel)
        self.duration.setValue(cue.duration_ms)
        self.label.setText(cue.label)

    def dump(self) -> Optional[OlfactoryCue]:
        if not self.isChecked():
            return None
        return OlfactoryCue(channel=self.channel.text().strip() or "a",
                            duration_ms=self.duration.value(),
                            label=self.label.text().strip() or "odour")


class OtherCueEditor(QGroupBox):
    """User-defined stimulus modality."""

    def __init__(self):
        super().__init__("Additional stimulus modality")
        self.setCheckable(True)
        self.setChecked(False)
        f = QFormLayout(self)

        self.name = QLineEdit("other")
        self.duration = QSpinBox()
        self.duration.setRange(1, 60000)
        self.duration.setValue(500)
        self.duration.setSuffix(" ms")
        self.command = QLineEdit()
        self.command.setPlaceholderText("controller command, e.g. VIB,1,200")

        f.addRow("Modality name", self.name)
        f.addRow("Stimulus duration", self.duration)
        f.addRow("Controller command", self.command)
        f.addRow(QLabel(
            "<i>Transmits an arbitrary command string to the controller, "
            "allowing a stimulus modality to be added without modifying "
            "this application. The command must correspond to one "
            "implemented in the controller firmware; unrecognised commands "
            "are rejected and logged.</i>"))

    def load(self, cue: Optional[OtherCue]):
        self.setChecked(cue is not None)
        if cue is None:
            return
        self.name.setText(cue.name)
        self.duration.setValue(cue.duration_ms)
        self.command.setText(cue.raw_command)

    def dump(self) -> Optional[OtherCue]:
        if not self.isChecked():
            return None
        return OtherCue(name=self.name.text().strip() or "other",
                        duration_ms=self.duration.value(),
                        raw_command=self.command.text().strip())


class CueSetEditor(QWidget):
    """
    Complete editor for one cue set.

    All four modality editors are always present and independently
    enabled. Every parameter of every modality is available regardless of
    whether the cue set marks a trial or a block transition.
    """

    def __init__(self, cue: Optional[CueSet] = None):
        super().__init__()
        v = QVBoxLayout(self)
        v.setContentsMargins(0, 0, 0, 0)

        self.speaker = SpeakerCueEditor()
        self.led = LedCueEditor()
        self.olfactory = OlfactoryCueEditor()
        self.other = OtherCueEditor()
        for w in (self.speaker, self.led, self.olfactory, self.other):
            v.addWidget(w)

        self.summary = QLabel("")
        self.summary.setWordWrap(True)
        self.summary.setFont(_mono(9))
        v.addWidget(self.summary)

        for w in (self.speaker, self.led, self.olfactory, self.other):
            w.toggled.connect(self._update_summary)

        self.load(cue)

    def load(self, cue: Optional[CueSet]):
        cue = cue or CueSet()
        self.speaker.load(cue.speaker)
        self.led.load(cue.led)
        self.olfactory.load(cue.olfactory)
        self.other.load(cue.other)
        self._update_summary()

    def dump(self) -> CueSet:
        return CueSet(speaker=self.speaker.dump(), led=self.led.dump(),
                      olfactory=self.olfactory.dump(), other=self.other.dump())

    def _update_summary(self):
        cue = self.dump()
        if cue.is_empty():
            self.summary.setText(
                "No stimulus enabled. This event will occur without any "
                "signal to the subject.")
        else:
            self.summary.setText(
                f"Enabled: {', '.join(cue.modalities)}. All enabled "
                f"modalities are triggered from a single staged batch and "
                f"therefore share one onset timestamp. Longest component: "
                f"{cue.max_duration_ms()} ms.")


class CueSetDialog(QDialog):
    """Modal wrapper around CueSetEditor."""

    def __init__(self, cue: Optional[CueSet], title: str, parent=None):
        super().__init__(parent)
        self.setWindowTitle(title)
        self.setMinimumWidth(560)
        v = QVBoxLayout(self)
        self.editor = CueSetEditor(cue)
        v.addWidget(self.editor)
        buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Ok
            | QDialogButtonBox.StandardButton.Cancel)
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        v.addWidget(buttons)

    def result_cue(self) -> CueSet:
        return self.editor.dump()


def edit_cue(parent, cue: Optional[CueSet], title: str) -> Optional[CueSet]:
    """Open the editor. Returns the edited cue set, or None if cancelled."""
    dlg = CueSetDialog(cue, title, parent)
    if dlg.exec() == QDialog.DialogCode.Accepted:
        return dlg.result_cue()
    return None


def describe_cue(cue: Optional[CueSet]) -> str:
    """Short single-line description for a table cell or button label."""
    if cue is None or cue.is_empty():
        return "none"
    return ", ".join(cue.modalities)
