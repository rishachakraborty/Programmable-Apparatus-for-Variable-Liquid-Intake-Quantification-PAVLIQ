"""
theme.py — one palette, two modes.

Colour carries meaning in this application, so the two modes are not an
inversion of each other. Liquid identity and state colours are chosen
separately for each background: the rust/blue pair that reads clearly on
paper turns muddy on near-black, so dark mode uses lighter, less
saturated versions at the same hue.

Everything reads from ACTIVE, which is swapped by set_mode(). Widgets
that draw with pyqtgraph have to be told to repaint; the Qt stylesheet
handles the rest.
"""

from __future__ import annotations

LIGHT = {
    "name": "light",
    "ink": "#1c1f26", "paper": "#f7f6f3", "panel": "#ffffff",
    "rule": "#d5d2cb", "muted": "#7b7871",
    "liquid_a": "#b5462f", "liquid_b": "#2f6fb5", "liquid_c": "#6a6a6a",
    "cue": "#3f4550", "iti": "#c9c6bf",
    "ok": "#1f7a4d", "warn": "#8d6e2f", "bad": "#b5462f",
    "busy": "#8d6e2f", "ready": "#1f7a4d",
    "accent": "#1c1f26", "accent_text": "#f7f6f3",
    "selection": "#cfd8e8",
}

DARK = {
    "name": "dark",
    "ink": "#e8e6e1", "paper": "#16181d", "panel": "#1e2128",
    "rule": "#343943", "muted": "#8b8f99",
    # Same hues, lifted and desaturated so they survive a dark ground.
    "liquid_a": "#e8785c", "liquid_b": "#68a8e8", "liquid_c": "#9aa0aa",
    "cue": "#aeb4c0", "iti": "#3a3f49",
    "ok": "#4cc38a", "warn": "#d9a441", "bad": "#e8785c",
    "busy": "#d9a441", "ready": "#4cc38a",
    "accent": "#e8e6e1", "accent_text": "#16181d",
    "selection": "#2c3444",
}

ACTIVE = dict(LIGHT)
_listeners: list = []


def set_mode(name: str) -> dict:
    ACTIVE.clear()
    ACTIVE.update(DARK if name == "dark" else LIGHT)
    for fn in list(_listeners):
        try:
            fn(ACTIVE)
        except Exception:
            pass
    return ACTIVE


def on_change(fn) -> None:
    """Register a repaint callback. pyqtgraph items do not pick up Qt
    stylesheet changes, so anything drawn has to redraw itself."""
    _listeners.append(fn)


def c(key: str) -> str:
    return ACTIVE.get(key, "#888888")


def liquid_colours(names) -> dict:
    """Stable colour per liquid, in first-seen order."""
    keys = ("liquid_a", "liquid_b", "liquid_c")
    return {n: c(keys[i]) if i < len(keys) else c("muted")
            for i, n in enumerate(names)}


def stylesheet() -> str:
    p = ACTIVE
    return f"""
QWidget {{ background: {p['paper']}; color: {p['ink']}; }}
QScrollArea, QSplitter {{ border: none; }}
QGroupBox {{ border: 1px solid {p['rule']}; border-radius: 4px;
             margin-top: 15px; padding-top: 12px; font-weight: 600; }}
QGroupBox::title {{ subcontrol-origin: margin; left: 10px; padding: 0 5px;
                    color: {p['muted']}; }}
QLabel {{ background: transparent; }}

QPushButton {{ background: {p['panel']}; border: 1px solid {p['rule']};
               border-radius: 4px; padding: 5px 12px; min-height: 17px; }}
QPushButton:hover {{ border-color: {p['ink']}; }}
QPushButton:pressed {{ background: {p['selection']}; }}
QPushButton:disabled {{ color: {p['rule']}; border-color: {p['rule']}; }}
QPushButton:default {{ background: {p['accent']}; color: {p['accent_text']};
                       border-color: {p['accent']}; }}

/* Dropdowns were unreadably narrow. A floor here fixes every one of
   them at once, including those built inside table cells. */
QComboBox {{ min-width: 132px; padding: 3px 6px; }}
QComboBox QAbstractItemView {{ background: {p['panel']}; color: {p['ink']};
                               selection-background-color: {p['selection']};
                               border: 1px solid {p['rule']}; }}
QComboBox::drop-down {{ width: 18px; border-left: 1px solid {p['rule']}; }}

QLineEdit, QSpinBox, QDoubleSpinBox, QComboBox, QPlainTextEdit,
QTextEdit, QListWidget, QTableWidget, QTreeWidget {{
    background: {p['panel']}; border: 1px solid {p['rule']};
    border-radius: 3px; padding: 3px;
    selection-background-color: {p['selection']};
    selection-color: {p['ink']}; }}
QSpinBox, QDoubleSpinBox {{ min-width: 74px; }}

QTableWidget {{ gridline-color: {p['rule']}; }}
QHeaderView::section {{ background: {p['paper']}; border: none;
                        border-bottom: 1px solid {p['rule']};
                        border-right: 1px solid {p['rule']};
                        padding: 5px; color: {p['muted']}; }}
QTableWidget QLineEdit, QTableWidget QComboBox,
QTableWidget QSpinBox, QTableWidget QDoubleSpinBox {{ border: none; }}

QTabBar::tab {{ padding: 8px 20px; background: transparent;
                color: {p['muted']}; }}
QTabBar::tab:selected {{ color: {p['ink']};
                         border-bottom: 2px solid {p['ink']}; }}
QTabWidget::pane {{ border: none; }}

QCheckBox, QRadioButton {{ background: transparent; spacing: 6px; }}
QScrollBar:vertical {{ background: {p['paper']}; width: 11px; }}
QScrollBar::handle:vertical {{ background: {p['rule']}; border-radius: 5px;
                               min-height: 24px; }}
QScrollBar:horizontal {{ background: {p['paper']}; height: 11px; }}
QScrollBar::handle:horizontal {{ background: {p['rule']};
                                 border-radius: 5px; min-width: 24px; }}
QScrollBar::add-line, QScrollBar::sub-line {{ height: 0; width: 0; }}
QToolTip {{ background: {p['panel']}; color: {p['ink']};
            border: 1px solid {p['rule']}; padding: 4px; }}
QMessageBox {{ background: {p['paper']}; }}
"""


# ---- button state helpers -------------------------------------------
# A button that looks identical while it is working and when it is ready
# gives no feedback at all, which is the complaint these fix.

def btn_busy(btn, text: str | None = None) -> None:
    btn.setEnabled(False)
    btn.setStyleSheet(f"background: {c('busy')}; color: {c('accent_text')};"
                      f"border-color: {c('busy')};")
    if text:
        btn.setText(text)


def btn_ready(btn, text: str | None = None) -> None:
    btn.setEnabled(True)
    btn.setStyleSheet(f"background: {c('ready')}; color: {c('accent_text')};"
                      f"border-color: {c('ready')};")
    if text:
        btn.setText(text)


def btn_normal(btn, text: str | None = None) -> None:
    btn.setEnabled(True)
    btn.setStyleSheet("")
    if text:
        btn.setText(text)


def btn_active(btn, text: str | None = None) -> None:
    """For a latched hardware state, e.g. a solenoid held open."""
    btn.setEnabled(True)
    btn.setStyleSheet(f"background: {c('bad')}; color: {c('accent_text')};"
                      f"border-color: {c('bad')};")
    if text:
        btn.setText(text)
