"""
session_runner.py — steps a generated session through the firmware.

Non-blocking by construction. `tick()` is called from a GUI timer, does
a bounded amount of work, and returns. Nothing here sleeps or waits on
serial, because the whole point of the firmware/host split is that the
host is never inside the reward loop. If this module blocked for 200 ms
it would not break a trial — the Arduino would carry on regardless —
but the GUI would stutter and the log would arrive in bursts.

The runner advances only when the firmware says a trial has ended, so
Python can never get ahead of the hardware.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from enum import Enum
from typing import Callable, Optional

from arduino_link import ArduinoLink, Event
from event_log import EventLog


class RunState(Enum):
    IDLE = "idle"
    PURGING = "purging lines"
    BLOCK_CUE = "block cue"
    TRIAL = "trial running"
    PAUSED = "paused"
    FINISHED = "finished"
    ABORTED = "aborted"


@dataclass
class TrialRecord:
    """What actually happened, assembled from firmware events."""
    index: int
    block: str
    ratio: int
    choice: bool
    planned_spouts: list
    cue_ms: Optional[int] = None        # Arduino ms of cue onset
    chosen: Optional[str] = None
    choice_latency_ms: Optional[int] = None
    fr_met_latency_ms: Optional[int] = None
    outcome: Optional[int] = None
    licks: list = field(default_factory=list)   # (t_rel_ms, spout, is_on)
    reward_t_rel: Optional[int] = None
    uncued: bool = False
    decoupled: bool = False
    free_given: int = 0


class SessionRunner:

    def __init__(self, link: ArduinoLink, session, log: EventLog,
                 on_trial_update: Optional[Callable[[TrialRecord], None]] = None,
                 on_state_change: Optional[Callable[[RunState], None]] = None,
                 calibration=None):
        self.link = link
        self.session = session
        self.log = log
        self.cfg = session.config
        self.on_trial_update = on_trial_update
        self.on_state_change = on_state_change
        # Used to turn purge fill volumes into open times. Optional, so
        # the runner still works on a rig with no tables yet.
        self.calibration = calibration

        self.state = RunState.IDLE
        self.next_index = 0
        self.records: dict[int, TrialRecord] = {}
        self.current: Optional[TrialRecord] = None
        self._block_cue_until = 0.0
        self._pending_pause = False
        self._purge_deadline = 0.0
        self.purges_done = 0
        self._free_queue: list = []
        self.free_given = 0
        self.started_at: Optional[float] = None
        self.finished_at: Optional[float] = None
        self.last_error: Optional[str] = None

    # ---- control ----

    def _set_state(self, s: RunState) -> None:
        if s is not self.state:
            self.state = s
            self.log.note(f"state -> {s.value}")
            if self.on_state_change:
                self.on_state_change(s)

    def start(self) -> None:
        if self.state in (RunState.TRIAL, RunState.BLOCK_CUE):
            return
        self.started_at = self.started_at or time.time()
        self.link.trial_gate(self.cfg.quiet_gate_ms)
        self._pending_pause = False
        self._set_state(RunState.IDLE)

    def pause(self) -> None:
        """Finish the trial in progress, then stop. Never cuts a trial
        short: a half-delivered reward is worse than a delay."""
        self._pending_pause = True
        self.log.note("pause requested; will stop after this trial")

    def resume(self) -> None:
        self._pending_pause = False
        if self.state is RunState.PAUSED:
            self._set_state(RunState.IDLE)

    def abort(self) -> None:
        try:
            self.link.block_abort()
            self.link.trial_abort()
            self.link.stop_all()
        except Exception as e:
            self.last_error = str(e)
        self.finished_at = time.time()
        self._set_state(RunState.ABORTED)

    @property
    def progress(self) -> tuple[int, int]:
        return self.next_index, self.session.n_trials

    # ---- main loop ----

    def tick(self) -> None:
        self._drain_events()
        self._serve_free_rewards()

        if self.state in (RunState.FINISHED, RunState.ABORTED,
                          RunState.PAUSED):
            return

        if self.state is RunState.PURGING:
            # Advanced by the BLOCK_END event. The deadline is a
            # backstop only: if the board never reports one, sitting
            # here forever would silently end the session, so we give up
            # and carry on with a note in the log rather than hang.
            if time.time() > self._purge_deadline:
                self.log.note("purge did not report BLOCK_END; continuing")
                self._after_purge()
            return

        if self.state is RunState.BLOCK_CUE:
            if time.time() >= self._block_cue_until:
                self._launch_trial()
            return

        if self.state is RunState.TRIAL:
            return          # advanced only by a TRIAL_END event

        # IDLE: decide what happens next
        if self.next_index >= self.session.n_trials:
            self.finished_at = time.time()
            self._set_state(RunState.FINISHED)
            return

        if self._pending_pause:
            self._set_state(RunState.PAUSED)
            return

        planned = self.session.trials[self.next_index]

        # Purge before the cue, never after. The whole point is that the
        # dead space holds the right solution at the moment the animal
        # is told what to expect.
        if (planned.needs_purge and self.cfg.purge_on_liquid_change
                and not getattr(planned, "_purged", False)):
            planned._purged = True
            if self._start_purge(planned):
                return

        if planned.is_block_start and planned.block_cue is not None:
            cs = planned.block_cue
            try:
                self._fire_cueset(cs)
            except Exception as e:
                self.last_error = str(e)
                self.log.note(f"block cue failed: {e}")
            # The block cue is a separate signal from the trial cue, so we
            # let it finish before the trial begins rather than overlapping
            # them into one ambiguous stimulus.
            self._block_cue_until = (time.time()
                                     + cs.max_duration_ms() / 1000.0)
            self._set_state(RunState.BLOCK_CUE)
            return

        self._launch_trial()

    def _serve_free_rewards(self) -> None:
        """Deliver any scheduled unsignalled reward whose time has come."""
        if not self._free_queue:
            return
        now = time.time()
        due = [d for d in self._free_queue if d[0] <= now]
        if not due:
            return
        self._free_queue = [d for d in self._free_queue if d[0] > now]
        for _t, spout, ul, trial_idx in due:
            sol = self._solenoid_for(spout, trial_idx)
            if sol is None:
                continue
            ms = self._fill_ms(sol, ul)
            try:
                self.link.solenoid_dispense_ms(sol, int(round(ms)))
                self.free_given += 1
                if self.current:
                    self.current.free_given += 1
                self.log.note(f"free reward {ul:g} uL at {spout} "
                              f"(solenoid {sol}, {ms:.0f} ms)")
            except Exception as e:
                self.log.note(f"free reward failed: {e}")

    def _solenoid_for(self, spout: str,
                      trial_idx: Optional[int] = None) -> Optional[int]:
        """Which gate feeds this spout. Resolved against the trial the
        drop was scheduled from, not the current one, so a late drop
        still uses the line that was primed for it."""
        i = self.next_index if trial_idx is None else trial_idx
        i = max(0, min(i, self.session.n_trials - 1))
        for sp in self.session.trials[i].spouts:
            if sp.spout == spout:
                return sp.solenoid
        return None

    def _fire_cueset(self, cs) -> None:
        """
        Fire every modality in a cue set together.

        Staged with ARM so they share one onset and one timestamp. Two
        separate commands would be milliseconds apart, which for a
        compound cue means the animal receives two events rather than
        one.
        """
        cmds = []
        if cs.led:
            cmds.append(self.link.led_cmd(
                cs.led.channel, cs.led.duration_ms, pulsing=cs.led.pulsing,
                pulse_hz=cs.led.pulse_hz, brightness=cs.led.brightness))
        if cs.speaker:
            sp = cs.speaker
            cmds.append(self.link.speaker_cmd(
                "l", sp.duration_ms, sp.tone_hz, click_train=sp.click_train,
                click_hz=sp.click_hz, volume=sp.volume))
        if not cmds:
            return
        self.link.disarm()
        for cmd in cmds:
            self.link.arm(cmd)
        self.link.go()

        # Modalities the hardware does not implement yet are sent anyway
        # so the rejection lands in the log. A cue that silently did
        # nothing would be indistinguishable from one that worked.
        for extra in (cs.olfactory, cs.other):
            if extra is None:
                continue
            cmd = extra.command()
            if not cmd:
                continue
            try:
                self.link.send(cmd, expect_ack=True, timeout=0.6)
            except Exception as e:
                self.log.note(f"cue modality not available: {cmd} -> {e}")

    def _start_purge(self, planned) -> bool:
        """Purge and refill the lines whose liquid is about to change."""
        cfg = self.cfg
        spouts = []
        for spout, solenoid in planned.purge_spouts:
            ms, _q = None, None
            try:
                ms = self._fill_ms(solenoid, cfg.purge_fill_ul)
            except Exception:
                ms = 60.0
            spouts.append({"ch": spout, "solenoid": solenoid,
                           "fill_ms": int(round(ms)),
                           "pulses": cfg.purge_pulses, "gap_ms": 150})
        if not spouts:
            return False

        steps = self._vac_steps(spouts[0]["ch"], cfg.purge_vac_ul)
        try:
            self.link.block_switch(
                block_id=planned.index, spouts=spouts, vac_steps=steps,
                cycles=cfg.purge_cycles, sequential=not cfg.purge_parallel,
                use_pump=cfg.purge_use_pump,
                gap_ms=getattr(cfg, "purge_gap_ms", 150))
        except Exception as e:
            # Do not proceed as if the lines were clean. The next trial
            # would deliver the previous block's solution under the new
            # cue, which is the exact error this machinery prevents.
            self.last_error = f"purge refused: {e}"
            self.log.note(f"PURGE FAILED before trial {planned.index}: {e}")
            return False

        n_cycles = max(1, cfg.purge_cycles)
        self._purge_deadline = time.time() + 15.0 + 8.0 * n_cycles * len(spouts)
        self._set_state(RunState.PURGING)
        return True

    def _fill_ms(self, solenoid: int, ul: float) -> float:
        cal = getattr(self, "calibration", None)
        if cal is None:
            return 60.0
        ms, _q = cal.ms_for(solenoid, ul)
        return ms if ms else 60.0

    def _vac_steps(self, ch: str, ul: float) -> int:
        nl_per_step = getattr(self, "_nl_per_step", {}).get(ch)
        if not nl_per_step:
            try:
                nl_per_step = self.link.stepper_read(ch).nl_per_step
            except Exception:
                nl_per_step = 180
            if not hasattr(self, "_nl_per_step"):
                self._nl_per_step = {}
            self._nl_per_step[ch] = nl_per_step
        return max(1, int(round(ul * 1000.0 / max(1, nl_per_step))))

    def _after_purge(self) -> None:
        self.purges_done += 1
        self._set_state(RunState.IDLE)

    def _launch_trial(self) -> None:
        planned = self.session.trials[self.next_index]
        rec = TrialRecord(index=planned.index, block=planned.block_label,
                          ratio=planned.ratio, choice=planned.choice,
                          planned_spouts=list(planned.spouts),
                          uncued=planned.uncued, decoupled=planned.decoupled)
        self.records[planned.index] = rec
        self.current = rec

        # Per-spout tones carry the side information, so they are built
        # per spout. Other modalities are single-channel and are fired
        # once for the trial as a whole.
        cues = []
        extras = []
        # An uncued trial runs identically in every other respect: spouts
        # out, licks counted, reward on the usual requirement. Only the
        # signal is withheld.
        for s in ([] if planned.uncued else planned.spouts):
            cs = s.cue
            if cs.speaker:
                sp = cs.speaker
                cues.append(ArduinoLink.speaker_cmd(
                    s.spout, sp.duration_ms, sp.tone_hz,
                    click_train=sp.click_train, click_hz=sp.click_hz,
                    volume=sp.volume))
            if cs.led:
                cues.append(ArduinoLink.led_cmd(
                    cs.led.channel, cs.led.duration_ms,
                    pulsing=cs.led.pulsing, pulse_hz=cs.led.pulse_hz,
                    brightness=cs.led.brightness))
            for extra in (cs.olfactory, cs.other):
                if extra is not None and extra.command():
                    extras.append(extra.command())

        spouts = []
        for s in planned.spouts:
            ms = s.dispense_ms
            if ms is None:
                # Volume with no calibration behind it. Log it and give
                # nothing rather than invent a duration.
                self.log.note(f"trial {planned.index} spout {s.spout}: "
                              f"{s.volume_ul} uL has no calibration; "
                              f"reward withheld")
            spouts.append({"ch": s.spout, "solenoid": s.solenoid,
                           "dispense_ms": int(round(ms)) if ms else 0,
                           "ratio": planned.ratio,
                           "rewarded": s.rewarded and bool(ms)})

        try:
            self.link.run_trial(
                trial_id=planned.index, choice=planned.choice,
                cues=cues, spouts=spouts,
                cue_reward_ms=self.cfg.cue_reward_delay_ms,
                omission_ms=self.cfg.omission_window_ms,
                retract_delay_ms=(self.cfg.iti_retract_delay_ms
                                  if self.cfg.use_retraction else 0),
                iti_ms=int(planned.iti_s * 1000), wait=False)
            for cmd in extras:
                try:
                    self.link.send(cmd, expect_ack=True, timeout=0.6)
                except Exception as e:
                    self.log.note(f"cue modality not available: {cmd} -> {e}")
        except Exception as e:
            # Do not silently skip. A trial that failed to launch is a
            # hole in the session, and the log has to show it.
            self.last_error = str(e)
            self.log.note(f"TRIAL {planned.index} FAILED TO START: {e}")
            self.next_index += 1
            self._set_state(RunState.IDLE)
            return

        if planned.uncued:
            self.log.note(f"trial {planned.index}: uncued (random-reward "
                          f"control)")
        if planned.decoupled:
            self.log.note(f"trial {planned.index}: amount decoupled from cue")

        # Free rewards land during the ITI. They are queued here and
        # fired by tick() against the wall clock. The queue is EXTENDED,
        # not replaced: a drop scheduled late in one ITI must survive the
        # next trial being set up, or it is silently dropped.
        base = (time.time() + self.cfg.cue_reward_delay_ms / 1000.0
                + (self.cfg.iti_retract_delay_ms / 1000.0
                   if self.cfg.use_retraction else 0.0))
        for t_s, sp, ul in planned.free_rewards:
            self._free_queue.append((base + t_s, sp, ul, planned.index))

        self._set_state(RunState.TRIAL)

    # ---- events ----

    def _drain_events(self) -> None:
        for ev in self.link.drain_events():
            self._handle(ev)

    def _handle(self, ev: Event) -> None:
        rec = self.current
        idx = rec.index if rec else -1
        self.log.write(ev, trial=idx,
                       block=rec.block if rec else "",
                       ratio=rec.ratio if rec else 0)

        if ev.type == "BLOCK_END" and self.state is RunState.PURGING:
            self._after_purge()
            return

        if rec is None:
            return

        if ev.type == "TRIAL_CUE":
            rec.cue_ms = ev.t_ms

        elif ev.type == "TRIAL_CHOICE":
            rec.chosen = ev.ch
            rec.choice_latency_ms = ev.d1

        elif ev.type == "TRIAL_FR_MET":
            rec.fr_met_latency_ms = ev.d2

        elif ev.type in ("TRIAL_REWARD", "TRIAL_NOREWARD"):
            if rec.cue_ms is not None:
                rec.reward_t_rel = ev.t_ms - rec.cue_ms

        elif ev.type in ("LICK_ON", "LICK_OFF"):
            if rec.cue_ms is not None:
                rec.licks.append((ev.t_ms - rec.cue_ms, ev.ch,
                                  ev.type == "LICK_ON"))

        elif ev.type == "TRIAL_END":
            rec.outcome = ev.d2
            if self.on_trial_update:
                self.on_trial_update(rec)
            self.current = None
            self.next_index = max(self.next_index, ev.d1 + 1)
            self._set_state(RunState.IDLE)
            return

        elif ev.type == "BLOCK_END":
            if self.state is RunState.PURGING:
                self._after_purge()
            return

        elif ev.type == "FIRMWARE_FAULT":
            self.last_error = "firmware reported a fault; see the log"

        if self.on_trial_update:
            self.on_trial_update(rec)

    # ---- live summary ----

    def summary(self) -> dict:
        done = [r for r in self.records.values() if r.outcome is not None]
        rewarded = sum(1 for r in done if r.outcome == 1)
        withheld = sum(1 for r in done if r.outcome == 2)
        omitted = sum(1 for r in done if r.outcome == 3)

        # Preference is only meaningful on trials where an alternative
        # existed. Counting single-spout trials in the numerator while
        # dividing by choice trials produces percentages above 100.
        def liquid_taken(r):
            if not r.chosen:
                return None
            return next((s.liquid for s in r.planned_spouts
                         if s.spout == r.chosen.lower()), None)

        by_liquid: dict = {}          # choice trials only
        taken_any: dict = {}          # every trial with a response
        for r in done:
            liq = liquid_taken(r)
            if not liq:
                continue
            taken_any[liq] = taken_any.get(liq, 0) + 1
            if r.choice:
                by_liquid[liq] = by_liquid.get(liq, 0) + 1

        n_choice = sum(by_liquid.values())
        pref = None
        if n_choice:
            top = max(by_liquid, key=by_liquid.get)
            pref = (top, 100.0 * by_liquid[top] / n_choice)

        lat = [r.choice_latency_ms for r in done
               if r.choice_latency_ms is not None]
        return {
            "completed": len(done),
            "planned": self.session.n_trials,
            "rewarded": rewarded,
            "withheld": withheld,
            "omitted": omitted,
            "omission_rate": (100.0 * omitted / len(done)) if done else 0.0,
            "choices_by_liquid": by_liquid,
            "n_choice_trials": n_choice,
            "consumed_by_liquid": taken_any,
            "preference": pref,
            "median_choice_latency_ms": (sorted(lat)[len(lat) // 2]
                                        if lat else None),
            "purges_done": self.purges_done,
            "free_rewards_given": self.free_given,
            "uncued_done": sum(1 for r in done if r.uncued),
            "decoupled_done": sum(1 for r in done if r.decoupled),
            "elapsed_s": (time.time() - self.started_at)
                         if self.started_at else 0.0,
        }
