"""
event_log.py — session event log.

Design decisions worth knowing:

CSV is written INCREMENTALLY and flushed on every event. A session is
40+ minutes of an animal's drinking behaviour; if Python dies at minute
39 you keep 39 minutes of data rather than nothing. The .npz is written
at the end as a convenience for analysis, not as the record of truth.

Every event carries BOTH clocks. The Arduino millisecond counter is the
one to analyse with — it is the clock the hardware actually acted on,
and it has no USB jitter in it. The host timestamp exists so you can
align with wall-clock records like a camera or a lab notebook, and it
should never be used for latency measurements.

Event names are also written as numeric ids with a legend at the top of
the file, so the codes stay interpretable years later without needing
the config that produced them.
"""

from __future__ import annotations

import csv
import json
import os
import time
from dataclasses import asdict
from datetime import datetime
from typing import Optional

from task_design import jsonable

try:
    import numpy as np
except ImportError:      # CSV still works without numpy
    np = None

NPZ_DTYPE = [
    ("arduino_ms", "i8"), ("host_unix", "f8"), ("event_id", "i4"),
    ("event_name", "U24"), ("channel", "U2"), ("d1", "i8"), ("d2", "i8"),
    ("trial", "i4"), ("block", "U28"), ("ratio", "i4"),
]

CSV_HEADER = ["arduino_ms", "host_unix", "host_iso", "event_id",
              "event_name", "channel", "d1", "d2", "trial", "block", "ratio"]


class EventLog:

    def __init__(self, folder: str, subject: str, session,
                 link_info: Optional[dict] = None):
        os.makedirs(folder, exist_ok=True)
        stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        safe = "".join(c for c in subject if c.isalnum() or c in "-_") or "subject"
        self.stem = f"{safe}_{stamp}"
        self.folder = folder
        self.csv_path = os.path.join(folder, self.stem + "_events.csv")
        self.npz_path = os.path.join(folder, self.stem + "_events.npz")
        self.meta_path = os.path.join(folder, self.stem + "_session.json")

        self.session = session
        self.rows: list[tuple] = []
        self.closed = False

        # Numeric ids come from the session's registry so the same event
        # means the same number across sessions of the same task.
        self.legend: dict[str, int] = {}
        for key, num in session.cue_registry.items():
            if key.startswith("event:"):
                self.legend[key.split(":", 1)[1]] = num
        self._next_id = (max(self.legend.values()) + 1) if self.legend else 1

        self._write_meta(link_info or {})
        self._open_csv()

    # ---- ids ----

    def event_id(self, name: str) -> int:
        """Assign ids to anything not in the registry, so an unexpected
        firmware event is still recorded rather than silently dropped."""
        if name not in self.legend:
            self.legend[name] = self._next_id
            self._next_id += 1
            self._append_legend_note(name)
        return self.legend[name]

    # ---- files ----

    def _write_meta(self, link_info: dict) -> None:
        meta = {
            "written_at": datetime.now().isoformat(timespec="seconds"),
            "stem": self.stem,
            "seed": self.session.seed,
            "n_planned_trials": self.session.n_trials,
            "cue_registry": self.session.cue_registry,
            "link": link_info,
            "config": jsonable(asdict(self.session.config)),
            "planned_trials": [jsonable(asdict(t))
                               for t in self.session.trials],
        }
        with open(self.meta_path, "w") as f:
            json.dump(meta, f, indent=2, default=str)

    def _open_csv(self) -> None:
        self._fh = open(self.csv_path, "w", newline="")
        self._fh.write(f"# session {self.stem}\n")
        self._fh.write(f"# seed {self.session.seed}\n")
        self._fh.write("# arduino_ms is the hardware clock: analyse with "
                       "this one.\n")
        self._fh.write("# host_unix is the computer clock, for aligning with "
                       "cameras or notes only.\n")
        self._fh.write("# EVENT ID LEGEND\n")
        for name, num in sorted(self.legend.items(), key=lambda kv: kv[1]):
            self._fh.write(f"#   {num} = {name}\n")
        self._fh.write("# END LEGEND\n")
        self._writer = csv.writer(self._fh)
        self._writer.writerow(CSV_HEADER)
        self._fh.flush()

    def _append_legend_note(self, name: str) -> None:
        # The legend block is already past, so record the addition inline
        # rather than rewriting the file mid-session.
        if not self.closed:
            self._fh.write(f"# LEGEND ADDED {self.legend[name]} = {name}\n")
            self._fh.flush()

    # ---- writing ----

    def write(self, event, trial: int = -1, block: str = "",
              ratio: int = 0) -> tuple:
        eid = self.event_id(event.type)
        host = event.host_time or time.time()
        row = (int(event.t_ms), float(host), eid, event.type, event.ch,
               int(event.d1), int(event.d2), int(trial), block, int(ratio))
        self.rows.append(row)
        self._writer.writerow([
            row[0], f"{row[1]:.6f}",
            datetime.fromtimestamp(host).isoformat(timespec="milliseconds"),
            row[2], row[3], row[4], row[5], row[6], row[7], row[8], row[9]])
        # Flushed on every event: a crash costs the last event, not the
        # session.
        self._fh.flush()
        return row

    def note(self, text: str) -> None:
        self._fh.write(f"# NOTE {datetime.now().isoformat(timespec='seconds')} "
                       f"{text}\n")
        self._fh.flush()

    def close(self) -> None:
        if self.closed:
            return
        self.closed = True
        try:
            self._fh.flush()
            self._fh.close()
        except Exception:
            pass
        if np is not None and self.rows:
            arr = np.array(self.rows, dtype=NPZ_DTYPE)
            np.savez_compressed(
                self.npz_path, events=arr,
                legend=json.dumps(self.legend),
                seed=str(self.session.seed))

    # ---- summaries ----

    def counts(self) -> dict:
        out: dict = {}
        for r in self.rows:
            out[r[3]] = out.get(r[3], 0) + 1
        return out

    def __len__(self) -> int:
        return len(self.rows)
