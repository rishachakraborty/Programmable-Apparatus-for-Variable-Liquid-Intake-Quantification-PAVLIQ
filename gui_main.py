"""
gui_main.py — the whole application.

    pip install PyQt6 pyqtgraph pyserial numpy
    python gui_main.py

Four tabs, in the order you use them:

    Task setup     design the task; no hardware needed
    Example task   preview the generated session
    Hardware       connect, position the spouts, calibrate, check cues
    Run            start a session, write the log, watch the raster

The session generated on the setup tab is what the Run tab executes, so
the plan you previewed is the plan that runs.
"""

from __future__ import annotations

import sys

from PyQt6.QtCore import QSettings
from PyQt6.QtWidgets import (
    QApplication, QCheckBox, QMainWindow, QMessageBox, QTabWidget, QWidget,
)

import theme
from calibration import CalibrationSet
from gui_experiment import InitTab, RunTab
from gui_setup import ExampleTab, SetupTab


class MainWindow(QMainWindow):
    def __init__(self):
        super().__init__()
        self.setWindowTitle("Mouse behaviour \u2014 two-choice preference task")
        self.resize(1420, 950)

        self.session = None
        self.link = None
        # One calibration object shared by every tab: the setup page reads
        # it to convert volumes, the hardware page edits it.
        self.calibration = CalibrationSet()

        self.setup = SetupTab(calibration=self.calibration)
        self.example = ExampleTab()
        self.init = InitTab(get_config=lambda: (self.session.config
                                                if self.session else None),
                            calibration=self.calibration)
        self.run = RunTab(get_link=lambda: self.link,
                          get_session=lambda: self.session,
                          get_calibration=lambda: self.calibration)

        tabs = QTabWidget()
        # Dark mode lives on the tab bar rather than in a menu: rigs are
        # often run in a dim room and the switch gets used often.
        self.dark = QCheckBox("Dark mode")
        self.dark.setChecked(theme.ACTIVE.get("name") == "dark")
        self.dark.stateChanged.connect(self._toggle_dark)
        tabs.setCornerWidget(self.dark)
        tabs.addTab(self.setup, "Task setup")
        tabs.addTab(self.example, "Example task")
        tabs.addTab(self.init, "Hardware")
        tabs.addTab(self.run, "Run")
        self.setCentralWidget(tabs)
        self.tabs = tabs

        def on_ready(sess, audit):
            # Rebuilding the task mid-session would mean the log's stored
            # plan no longer matched what was running, so refuse.
            if self.run.runner is not None and self.run.tick_timer.isActive():
                QMessageBox.warning(
                    self, "A session is running",
                    "Stop the session before changing the task. The log "
                    "stores the plan it was started with, and changing it "
                    "now would leave a record that does not match what the "
                    "animal did.")
                return
            self.session = sess
            self.example.show_session(sess, audit)
            tabs.setCurrentWidget(self.example)

        self.setup.session_ready.connect(on_ready)
        self.init.link_changed.connect(self._on_link)

    def _toggle_dark(self):
        mode = "dark" if self.dark.isChecked() else "light"
        theme.set_mode(mode)
        app = QApplication.instance()
        if app is not None:
            app.setStyleSheet(theme.stylesheet())
        QSettings("mouse_task", "gui").setValue("theme", mode)

    def _on_link(self, link):
        self.link = link

    def closeEvent(self, e):
        if self.run.tick_timer.isActive():
            ans = QMessageBox.question(
                self, "A session is running",
                "Closing will stop the session and close the log. "
                "Data already written is kept. Close anyway?")
            if ans != QMessageBox.StandardButton.Yes:
                e.ignore()
                return
            self.run._finish()
        if self.link is not None:
            # Sends STOPALL on the way out: gates closed, cues off.
            self.link.disconnect()
        e.accept()


def main():
    app = QApplication(sys.argv)
    saved = QSettings("mouse_task", "gui").value("theme", "light")
    theme.set_mode(saved if saved in ("light", "dark") else "light")
    app.setStyleSheet(theme.stylesheet())
    w = MainWindow()
    w.show()
    sys.exit(app.exec())


if __name__ == "__main__":
    main()
