"""
task_design.py — task configuration and session generation.

Pure Python. No serial, no hardware, no GUI. That means the whole
randomization scheme can be tested and audited without the rig, and the
Example Task tab can render a session that has never been run.

What this module produces is a fully deterministic PLAN: every trial's
cue parameters, spout assignment, ratio requirement, reward outcome and
ITI, decided before the session starts. Nothing about the plan depends
on the animal's behaviour. Storing the seed reproduces it exactly.
"""

from __future__ import annotations

import json
import math
import random
from dataclasses import dataclass, field, asdict
from typing import Optional, Sequence

SPOUTS = ("l", "c", "r")


class TaskConfigError(ValueError):
    """Configuration cannot produce a valid session."""


# =====================================================================
# Cues
# =====================================================================

@dataclass
class SpeakerCue:
    duration_ms: int = 500
    tone_hz: int = 10000
    click_train: bool = True
    click_hz: int = 50          # this is what encodes reward amount
    volume: int = 50

    def describe(self) -> str:
        if self.click_train:
            return f"{self.tone_hz} Hz @ {self.click_hz} clicks/s, {self.duration_ms} ms"
        return f"{self.tone_hz} Hz continuous, {self.duration_ms} ms"


@dataclass
class OlfactoryCue:
    """
    Placeholder for an olfactometer that is not built yet.

    It emits a command the firmware does not implement, which returns
    X,UNKNOWN_COMMAND and is logged. That is deliberate: a cue that
    silently does nothing would be indistinguishable in the data from
    one that worked, and this way the log shows exactly which trials
    asked for an odour the rig could not deliver.
    """
    channel: str = "a"
    duration_ms: int = 500
    label: str = "odour"

    def describe(self) -> str:
        return f"odour {self.channel} for {self.duration_ms} ms (not built)"

    def command(self) -> str:
        return f"OLF,{self.channel},{int(self.duration_ms)}"


@dataclass
class OtherCue:
    """
    User-defined modality. Carries a name and a raw command string so a
    modality we have not thought of can be driven without a code change.
    """
    name: str = "other"
    duration_ms: int = 500
    raw_command: str = ""

    def describe(self) -> str:
        return f"{self.name} for {self.duration_ms} ms"

    def command(self) -> str:
        return self.raw_command


@dataclass
class LedCue:
    channel: str = "w"          # w / b / g
    duration_ms: int = 1000
    pulsing: bool = False
    pulse_hz: int = 0
    brightness: int = 255

    def describe(self) -> str:
        mode = f"pulsing {self.pulse_hz} Hz" if self.pulsing else "steady"
        return f"{self.channel.upper()} LED {mode}, {self.duration_ms} ms"


# =====================================================================
# Trial and block specifications
# =====================================================================

@dataclass
class CueSet:
    """
    Everything that marks one event, in any combination.

    Trials were previously married to tones and block starts to LEDs.
    Nothing about the hardware requires that: a trial can be cued by a
    light, a block by a tone, or either by both at once. Each modality is
    independently present or absent.

    Per-side speakers are separate because in a choice trial the two
    sides carry DIFFERENT tones simultaneously; the rest of the
    modalities are single-channel.
    """
    speaker: Optional[SpeakerCue] = None
    led: Optional[LedCue] = None
    olfactory: Optional[OlfactoryCue] = None
    other: Optional[OtherCue] = None

    @property
    def modalities(self) -> list:
        out = []
        if self.speaker:   out.append("tone")
        if self.led:       out.append("light")
        if self.olfactory: out.append("odour")
        if self.other:     out.append(self.other.name)
        return out

    def is_empty(self) -> bool:
        return not self.modalities

    def describe(self) -> str:
        if self.is_empty():
            return "no cue"
        bits = []
        if self.speaker:   bits.append(self.speaker.describe())
        if self.led:       bits.append(self.led.describe())
        if self.olfactory: bits.append(self.olfactory.describe())
        if self.other:     bits.append(self.other.describe())
        return " + ".join(bits)

    def max_duration_ms(self) -> int:
        d = [0]
        for c in (self.speaker, self.led, self.olfactory, self.other):
            if c:
                d.append(getattr(c, "duration_ms", 0))
        return max(d)


@dataclass
class TrialType:
    """
    One trial type: a cue, a liquid, an amount, a contingency.

    The amount is a VOLUME. Open time is derived from the solenoid's
    lookup table at the moment the trial is built, so re-measuring a
    line changes every trial that uses it without editing anything here.
    Storing milliseconds would silently freeze an old calibration into
    the task definition.
    """
    label: str
    liquid: str
    cue: CueSet
    volume_ul: float = 3.0
    reward_contingency_pct: float = 100.0

    def validate(self) -> list[str]:
        p = []
        if not self.label:
            p.append("Trial type has no label.")
        if not self.liquid:
            p.append(f"Trial type {self.label!r} has no liquid.")
        if not (0.0 <= self.reward_contingency_pct <= 100.0):
            p.append(f"{self.label}: contingency must be 0-100.")
        if self.volume_ul <= 0:
            p.append(f"{self.label}: volume must be positive.")
        if self.cue.is_empty():
            p.append(f"{self.label}: no cue selected. Tick at least one "
                     f"modality, or the animal has nothing to learn from.")
        return p


@dataclass
class BlockSpec:
    """
    kind == "single": one liquid, only that spout extended.
    kind == "choice": two liquids, one per side, both extended.
    """
    label: str
    kind: str
    liquids: list[str]
    n_trials: int
    trial_type_labels: list[str]
    # Fired once at block onset. Any combination of modalities - a block
    # is not obliged to be an LED any more than a trial is obliged to be
    # a tone.
    cue: Optional[CueSet] = None

    def validate(self, known: set[str]) -> list[str]:
        p = []
        if self.kind not in ("single", "choice"):
            p.append(f"{self.label}: kind must be 'single' or 'choice'.")
        if self.kind == "single" and len(self.liquids) != 1:
            p.append(f"{self.label}: a single block needs exactly one liquid.")
        if self.kind == "choice" and len(self.liquids) != 2:
            p.append(f"{self.label}: a choice block needs exactly two liquids.")
        if self.n_trials <= 0:
            p.append(f"{self.label}: n_trials must be positive.")
        if not self.trial_type_labels:
            p.append(f"{self.label}: no trial types selected.")
        for t in self.trial_type_labels:
            if t not in known:
                p.append(f"{self.label}: unknown trial type {t!r}.")
        return p


@dataclass
class OperantDesign:
    """
    mode:
      "none"        reward on every trial that meets contingency (ratio 1)
      "fixed"       fixed ratio, constant all session
      "variable"    ratio drawn per trial, averaging mean_ratio
      "progressive" ratio_set worked through in order; the whole block
                    set runs at each level before the ratio advances
    """
    mode: str = "none"
    ratio: int = 1
    mean_ratio: int = 3
    ratio_set: list[int] = field(default_factory=lambda: [1, 2, 4, 8])

    def validate(self) -> list[str]:
        p = []
        if self.mode not in ("none", "fixed", "variable", "progressive"):
            p.append("Operant mode must be none/fixed/variable/progressive.")
        if self.mode == "fixed" and self.ratio < 1:
            p.append("Fixed ratio must be at least 1.")
        if self.mode == "variable" and self.mean_ratio < 1:
            p.append("Mean variable ratio must be at least 1.")
        if self.mode == "progressive":
            if not self.ratio_set:
                p.append("Progressive ratio set is empty.")
            elif any(r < 1 for r in self.ratio_set):
                p.append("Progressive ratios must all be at least 1.")
        return p

    @property
    def n_levels(self) -> int:
        return len(self.ratio_set) if self.mode == "progressive" else 1


@dataclass
class SessionConfig:
    trial_types: list[TrialType]
    blocks: list[BlockSpec]

    # (liquid, spout) -> solenoid number 1-4
    solenoid_map: dict = field(default_factory=dict)
    active_spouts: list[str] = field(default_factory=lambda: ["l", "r"])

    # Exponential ITI. iti_mean_s is the exponential SCALE measured from
    # iti_min_s, not the realized mean: truncation at iti_max_s pulls the
    # realized mean down. generate_session reports what you actually get.
    iti_mean_s: float = 8.0
    iti_min_s: float = 3.0
    iti_max_s: float = 30.0

    cue_reward_delay_ms: int = 1000
    omission_window_ms: int = 5000
    iti_retract_delay_ms: int = 1000
    quiet_gate_ms: int = 500

    # Pseudorandomization constraints
    max_repeat: int = 3          # max consecutive identical trial types / sides
    balance_window: int = 20     # sides balanced within every N trials

    # Whether a liquid moves between spouts across trials. ON is the
    # right default for a preference task: with a fixed mapping an
    # animal can solve it by always going to one side, and side
    # preference becomes indistinguishable from liquid preference.
    # Turn it off for training, or for a design that wants side fixed.
    randomize_sides: bool = True

    # Whether spouts retract at all within a trial. OFF means both stay
    # extended throughout, which suits a free-access or habituation
    # session. It also removes the mechanism that stops an animal
    # sampling both spouts on a choice trial, so choice data from a
    # no-retraction session means something different.
    use_retraction: bool = True

    # Purge and refill the spout dead space whenever the liquid at a
    # spout changes. Without it the first lick after a switch delivers
    # the OLD solution while the cue says otherwise.
    purge_on_liquid_change: bool = True
    purge_vac_ul: float = 54.0        # aspirated per purge, per spout
    purge_fill_ul: float = 4.0        # dispensed per fill pulse
    purge_pulses: int = 3
    purge_cycles: int = 2
    purge_parallel: bool = True       # all spouts at once; needs a pump each

    operant: OperantDesign = field(default_factory=OperantDesign)
    randomize_block_order: bool = True
    seed: Optional[int] = None

    # ---- validation ----

    def validate(self) -> list[str]:
        p: list[str] = []
        if not self.trial_types:
            p.append("No trial types defined.")
        if not self.blocks:
            p.append("No blocks defined.")

        labels = [t.label for t in self.trial_types]
        if len(labels) != len(set(labels)):
            p.append("Trial type labels must be unique.")
        for t in self.trial_types:
            p += t.validate()

        known = set(labels)
        for b in self.blocks:
            p += b.validate(known)

        p += self.operant.validate()

        if self.iti_min_s < 0:
            p.append("ITI minimum cannot be negative.")
        if self.iti_max_s <= self.iti_min_s:
            p.append("ITI maximum must exceed the minimum.")
        if self.iti_mean_s <= 0:
            p.append("ITI mean must be positive.")
        if self.omission_window_ms < self.cue_reward_delay_ms:
            p.append("Omission window must be at least the cue-reward delay, "
                     "or trials could be scored as omissions before reward "
                     "was ever possible.")
        if self.max_repeat < 1:
            p.append("max_repeat must be at least 1.")
        if self.purge_on_liquid_change:
            if self.purge_vac_ul <= 0:
                p.append("Purge volume must be positive.")
            if self.purge_cycles < 1:
                p.append("Purge cycles must be at least 1.")
        if self.balance_window < 2:
            p.append("balance_window must be at least 2.")

        by_label = {t.label: t for t in self.trial_types}

        # Every block must be able to supply its liquids on real spouts,
        # and every trial type it uses must match one of them.
        for b in self.blocks:
            for liq in b.liquids:
                sides = [s for s in self.active_spouts
                         if (liq, s) in self.solenoid_map]
                if not sides:
                    p.append(f"{b.label}: no active spout is mapped to "
                             f"deliver {liq!r}. Set the solenoid identities.")
            for t in b.trial_type_labels:
                if t in by_label and by_label[t].liquid not in b.liquids:
                    p.append(f"{b.label}: trial type {t!r} delivers "
                             f"{by_label[t].liquid!r}, which is not one of "
                             f"this block's liquids {b.liquids}.")
            if b.kind == "choice":
                for liq in b.liquids:
                    if not [t for t in b.trial_type_labels
                            if t in by_label and by_label[t].liquid == liq]:
                        p.append(f"{b.label}: choice block has no trial type "
                                 f"for {liq!r}.")

        # Progressive ratio has to be able to divide the blocks.
        n_lv = self.operant.n_levels
        if n_lv > 1:
            for b in self.blocks:
                if b.n_trials < n_lv:
                    p.append(f"{b.label}: {b.n_trials} trials cannot be split "
                             f"across {n_lv} ratio levels.")
        return p


# =====================================================================
# Planned output
# =====================================================================

@dataclass
class SpoutPlan:
    spout: str
    liquid: str
    trial_type: str
    solenoid: int
    volume_ul: float
    cue: CueSet
    rewarded: bool
    # Filled in by resolve_durations() from the calibration tables, so
    # the plan carries the volume as intent and the milliseconds as the
    # thing the hardware is actually told.
    dispense_ms: Optional[float] = None
    dispense_quality: str = "none"


@dataclass
class PlannedTrial:
    index: int                    # 0-based, session-wide
    block_index: int              # position in the executed block order
    block_label: str
    block_kind: str
    ratio_level: int              # index into the PR set; 0 otherwise
    ratio: int                    # licks required this trial
    choice: bool
    spouts: list[SpoutPlan]
    iti_s: float
    is_block_start: bool
    block_cue: Optional[CueSet]
    # True when the liquid at some spout differs from the previous
    # trial, so the dead space holds the wrong solution and has to be
    # purged before this trial can be cued.
    needs_purge: bool = False
    purge_spouts: list = field(default_factory=list)   # [(spout, solenoid)]

    @property
    def trial_types(self) -> list[str]:
        return [s.trial_type for s in self.spouts]

    @property
    def any_rewarded(self) -> bool:
        return any(s.rewarded for s in self.spouts)

    def liquid_by_spout(self) -> dict:
        return {s.spout: s.liquid for s in self.spouts}

    def hardware_sequence(self, cfg) -> list:
        """
        The order of physical events in this trial, as text.

        Built from the same fields the runner uses, so the display on the
        setup page cannot drift from what the rig will actually do.
        Returns [(t_ms_or_None, description), ...] where None means the
        step waits on the animal or on hardware rather than a clock.
        """
        seq = []
        if self.needs_purge and cfg.purge_on_liquid_change:
            for sp, sol in self.purge_spouts:
                seq.append((None, f"PURGE {sp.upper()}: retract, aspirate "
                                  f"{cfg.purge_vac_ul:g} \u00b5L, refill from "
                                  f"solenoid {sol} \u00d7{cfg.purge_pulses}, "
                                  f"{cfg.purge_cycles} cycles"))
        if self.is_block_start and self.block_cue is not None:
            seq.append((0, f"BLOCK CUE: {self.block_cue.describe()}"))

        if cfg.use_retraction:
            active = ", ".join(s.spout.upper() for s in self.spouts)
            seq.append((None, f"EXTEND {active} to drinking position, "
                              f"wait for arrival"))
        else:
            seq.append((None, "spouts already extended (retraction off)"))

        cues = " + ".join(f"{s.spout.upper()} {s.cue.describe()}"
                          for s in self.spouts)
        seq.append((0, f"CUE together: {cues}"))
        seq.append((0, f"lick counting starts, {self.ratio} lick"
                       f"{'s' if self.ratio != 1 else ''} required"))

        if self.choice:
            seq.append((None, "first lick decides the choice"))
            if cfg.use_retraction:
                seq.append((None, "unchosen spout retracts immediately"))
            else:
                seq.append((None, "unchosen spout stays out (retraction off)"))

        for s in self.spouts:
            ms = f"{s.dispense_ms:.0f} ms" if s.dispense_ms else "uncalibrated"
            verb = "REWARD" if s.rewarded else "no reward (contingency)"
            seq.append((cfg.cue_reward_delay_ms,
                        f"{verb} if {s.spout.upper()} chosen: "
                        f"{s.volume_ul:g} \u00b5L = {ms} on solenoid {s.solenoid}"))

        seq.append((cfg.omission_window_ms,
                    "no response by here scores an omission"))
        if cfg.use_retraction:
            seq.append((cfg.cue_reward_delay_ms + cfg.iti_retract_delay_ms,
                        "chosen spout retracts"))
        seq.append((None, f"ITI {self.iti_s:.1f} s"))
        seq.append((None, f"wait for {cfg.quiet_gate_ms} ms with no licking"))
        return seq


def jsonable(obj):
    """
    Make a config safe for json.dump.

    solenoid_map is keyed by (liquid, spout) tuples, which JSON cannot
    represent. They are rewritten as "liquid|spout" strings, and
    solenoid_map_from_json reverses it, so a saved session round-trips.
    """
    if isinstance(obj, dict):
        out = {}
        for k, v in obj.items():
            key = "|".join(str(x) for x in k) if isinstance(k, tuple) else str(k)
            out[key] = jsonable(v)
        return out
    if isinstance(obj, (list, tuple)):
        return [jsonable(x) for x in obj]
    return obj


def solenoid_map_from_json(d: dict) -> dict:
    out = {}
    for k, v in d.items():
        parts = k.split("|")
        if len(parts) == 2:
            out[(parts[0], parts[1])] = int(v)
    return out


@dataclass
class Session:
    trials: list[PlannedTrial]
    config: SessionConfig
    seed: int
    cue_registry: dict            # cue label -> numeric event id

    # ---- summaries ----

    @property
    def n_trials(self) -> int:
        return len(self.trials)

    @property
    def realized_iti_mean(self) -> float:
        if not self.trials:
            return 0.0
        return sum(t.iti_s for t in self.trials) / len(self.trials)

    def estimated_duration_s(self) -> float:
        """Rough. Assumes every trial runs to the omission window, which
        is the worst case; real sessions are shorter."""
        c = self.config
        per_trial = (c.cue_reward_delay_ms + c.iti_retract_delay_ms +
                     c.quiet_gate_ms) / 1000.0
        return sum(t.iti_s + per_trial for t in self.trials)

    def counts_by_trial_type(self) -> dict:
        out: dict = {}
        for t in self.trials:
            for s in t.spouts:
                out[s.trial_type] = out.get(s.trial_type, 0) + 1
        return out

    def counts_by_block(self) -> dict:
        out: dict = {}
        for t in self.trials:
            out[t.block_label] = out.get(t.block_label, 0) + 1
        return out

    def side_balance(self) -> dict:
        """How often each liquid appeared on each spout."""
        out: dict = {}
        for t in self.trials:
            for s in t.spouts:
                out.setdefault(s.liquid, {})
                out[s.liquid][s.spout] = out[s.liquid].get(s.spout, 0) + 1
        return out

    def reward_rate_by_trial_type(self) -> dict:
        tot: dict = {}
        rew: dict = {}
        for t in self.trials:
            for s in t.spouts:
                tot[s.trial_type] = tot.get(s.trial_type, 0) + 1
                if s.rewarded:
                    rew[s.trial_type] = rew.get(s.trial_type, 0) + 1
        return {k: 100.0 * rew.get(k, 0) / v for k, v in tot.items()}

    def block_order(self) -> list[tuple[int, str, int]]:
        """[(ratio_level, block_label, n_trials), ...] as executed."""
        out = []
        for t in self.trials:
            if t.is_block_start:
                out.append([t.ratio_level, t.block_label, 0])
            if out:
                out[-1][2] += 1
        return [tuple(x) for x in out]

    def to_json(self, indent: int = 2) -> str:
        return json.dumps({
            "seed": self.seed,
            "n_trials": self.n_trials,
            "cue_registry": self.cue_registry,
            "config": jsonable(asdict(self.config)),
            "trials": [jsonable(asdict(t)) for t in self.trials],
        }, indent=indent, default=str)


# =====================================================================
# Randomization primitives
# =====================================================================

def _even_split(total: int, k: int, rng: random.Random) -> list[int]:
    """
    Split total into k parts as evenly as possible. The remainder goes to
    a RANDOM subset rather than always the first few, so no trial type is
    systematically favoured across sessions.
    """
    if k <= 0:
        return []
    base = total // k
    rem = total - base * k
    counts = [base] * k
    for i in rng.sample(range(k), rem):
        counts[i] += 1
    return counts


def _constrained_shuffle(items: Sequence, max_repeat: int,
                         rng: random.Random, prev_tail: Optional[list] = None,
                         attempts: int = 200) -> list:
    """
    Shuffle so that no value appears more than max_repeat times in a row,
    counting any run carried in from prev_tail.

    Greedy with random choice among legal candidates, restarted on
    deadlock. Falls back to a plain shuffle only if the constraint is
    arithmetically impossible (e.g. 10 of one item, max_repeat 1, in a
    window of 12), in which case the caller is told by the return of
    _violations().
    """
    pool_template = list(items)
    for _ in range(attempts):
        pool = list(pool_template)
        rng.shuffle(pool)
        out: list = []
        tail = list(prev_tail or [])
        ok = True
        while pool:
            legal = []
            for i, v in enumerate(pool):
                run = 0
                for x in reversed(tail + out):
                    if x == v:
                        run += 1
                    else:
                        break
                if run < max_repeat:
                    legal.append(i)
            if not legal:
                ok = False
                break
            i = rng.choice(legal)
            out.append(pool.pop(i))
        if ok:
            return out
    rng.shuffle(pool_template)
    return pool_template


def _violations(seq: Sequence, max_repeat: int) -> int:
    """Count positions exceeding the run-length constraint."""
    n, run, prev = 0, 0, object()
    for v in seq:
        run = run + 1 if v == prev else 1
        prev = v
        if run > max_repeat:
            n += 1
    return n


def _repair_runs(seq: list, max_repeat: int, window: int,
                 rng: random.Random, rounds: int = 12) -> list:
    """
    Fix remaining run-length violations by swapping.

    The window-then-shuffle construction leaves a few violations when a
    window happens to be dominated by one item. Swaps are restricted to
    the violating position's own window and its immediate neighbours, so
    repairing run length cannot disturb the balance the windows were
    built to guarantee. A swap is kept only if it strictly reduces the
    total violation count, which makes the pass monotonic and
    guarantees termination.
    """
    seq = list(seq)
    n = len(seq)
    for _ in range(rounds):
        cur = _violations(seq, max_repeat)
        if cur == 0:
            break
        bad, run, prev = [], 0, object()
        for i, v in enumerate(seq):
            run = run + 1 if v == prev else 1
            prev = v
            if run > max_repeat:
                bad.append(i)

        improved = False
        for i in bad:
            w = i // window
            lo = max(0, (w - 1) * window)
            hi = min(n, (w + 2) * window)
            cands = [j for j in range(lo, hi) if seq[j] != seq[i]]
            rng.shuffle(cands)
            for j in cands:
                seq[i], seq[j] = seq[j], seq[i]
                new = _violations(seq, max_repeat)
                if new < cur:
                    cur = new
                    improved = True
                    break
                seq[i], seq[j] = seq[j], seq[i]
        if not improved:
            break
    return seq


def _balanced_sequence(counts: dict, total: int, window: int,
                       max_repeat: int, rng: random.Random,
                       carry_tail: Optional[list] = None) -> list:
    """
    Build a sequence with the given item counts, balanced within every
    `window` trials and obeying the run-length constraint.

    Balance is enforced structurally: the sequence is cut into windows,
    each given a proportional share of every item, and only then shuffled
    within the window. That is stronger than shuffling the whole
    sequence and hoping, which routinely leaves one item clumped into the
    first half of a session.
    """
    keys = list(counts.keys())
    remaining = dict(counts)
    out: list = []
    tail0 = list(carry_tail or [])   # run carried in from the previous block
    n_windows = max(1, math.ceil(total / window))

    for w in range(n_windows):
        left_windows = n_windows - w
        chunk: list = []
        for k in keys:
            take = int(round(remaining[k] / left_windows))
            take = min(take, remaining[k])
            chunk += [k] * take
            remaining[k] -= take
        # Fix rounding drift against the window size.
        target = min(window, total - len(out))
        while len(chunk) < target:
            avail = [k for k in keys if remaining[k] > 0]
            if not avail:
                break
            k = rng.choice(avail)
            chunk.append(k)
            remaining[k] -= 1
        while len(chunk) > target:
            k = chunk.pop(rng.randrange(len(chunk)))
            remaining[k] += 1
        prev = (tail0 + out)[-max_repeat:]
        out += _constrained_shuffle(chunk, max_repeat, rng, prev_tail=prev)

    leftovers = [k for k in keys for _ in range(remaining[k])]
    if leftovers:
        out += _constrained_shuffle(leftovers, max_repeat, rng,
                                    prev_tail=(tail0 + out)[-max_repeat:])
    out = out[:total]
    if _violations(tail0 + out, max_repeat):
        out = _repair_runs(out, max_repeat, window, rng)
    return out


def _truncated_exponential(rng: random.Random, scale: float,
                           lo: float, hi: float) -> float:
    """
    Exponential ITI truncated to [lo, hi], sampled by inverse CDF rather
    than by resampling until it fits. Resampling is slower and, more
    importantly, makes the realized distribution depend on how often it
    rejected, which is harder to describe in a methods section.

    `scale` is the exponential scale measured from lo. The realized mean
    is below lo + scale because of truncation at hi; Session reports it.
    """
    if scale <= 0:
        return lo
    span = hi - lo
    u = rng.random()
    fmax = 1.0 - math.exp(-span / scale)
    return lo - scale * math.log(1.0 - u * fmax)


def _exact_reward_flags(n: int, pct: float, rng: random.Random) -> list[bool]:
    """
    Reward flags for n trials at the given contingency.

    Uses an EXACT count rather than an independent coin flip per trial.
    A 50% contingency over 24 trials gives exactly 12, not 12 +/- 3.
    Independent flips would let a run of bad luck confound a block.
    """
    k = int(round(n * pct / 100.0))
    k = max(0, min(n, k))
    flags = [True] * k + [False] * (n - k)
    rng.shuffle(flags)
    return flags


# =====================================================================
# Generation
# =====================================================================

def _ratio_for(operant: OperantDesign, level: int,
               rng: random.Random) -> int:
    if operant.mode == "none":
        return 1
    if operant.mode == "fixed":
        return max(1, operant.ratio)
    if operant.mode == "variable":
        # Geometric about the mean: the standard VR schedule, in which
        # every lick has a constant probability of completing the ratio.
        m = max(1, operant.mean_ratio)
        return max(1, min(4 * m, rng.geometric(1.0 / m)
                          if hasattr(rng, "geometric")
                          else _geometric(rng, 1.0 / m)))
    return operant.ratio_set[level]


def _geometric(rng: random.Random, p: float) -> int:
    if p >= 1.0:
        return 1
    return int(math.floor(math.log(1.0 - rng.random()) / math.log(1.0 - p))) + 1


def _build_cue_registry(cfg: SessionConfig) -> dict:
    """
    Numeric ids for every distinct cue in the task, assigned in a stable
    order. The event log writes this as a legend so the numeric codes in
    the data are interpretable years later without the config file.
    """
    reg: dict = {}
    n = 1
    for t in sorted(cfg.trial_types, key=lambda x: x.label):
        reg[f"trial_cue:{t.label}"] = n
        n += 1
    for b in sorted(cfg.blocks, key=lambda x: x.label):
        if b.cue is not None:
            reg[f"block_cue:{b.label}"] = n
            n += 1
    for name in ("BLOCK_START", "BLOCK_VAC", "BLOCK_FILL",
                 "BLOCK_SPOUT_DONE", "BLOCK_RETURN", "BLOCK_END",
                 "STEP_ASP", "STEP_DIS", "STEP_DONE"):
        reg[f"event:{name}"] = n
        n += 1
    for name in ("LICK_ON", "LICK_OFF", "TRIAL_START", "TRIAL_CUE",
                 "TRIAL_CHOICE", "TRIAL_FR_MET", "TRIAL_REWARD",
                 "TRIAL_NOREWARD", "TRIAL_OMISSION", "TRIAL_ITI_END",
                 "TRIAL_END"):
        reg[f"event:{name}"] = n
        n += 1
    return reg


def generate_session(cfg: SessionConfig) -> Session:
    problems = cfg.validate()
    if problems:
        raise TaskConfigError("Configuration is not valid:\n  - " +
                              "\n  - ".join(problems))

    seed = cfg.seed if cfg.seed is not None else random.randrange(2 ** 31)
    rng = random.Random(seed)
    by_label = {t.label: t for t in cfg.trial_types}

    n_levels = cfg.operant.n_levels
    trials: list[PlannedTrial] = []
    block_pos = 0

    # The run-length constraint must survive block boundaries. Without
    # carrying the tail across, a block ending in three left trials
    # followed by one starting with three more gives the animal six in a
    # row - exactly the side bias the constraint exists to prevent.
    carry: dict = {"sides": [], "types": []}

    for level in range(n_levels):
        # Block order is redrawn at each ratio level, so the sequence at
        # FR2 is not the sequence at FR1. Otherwise ratio level and block
        # order would be perfectly confounded.
        order = list(range(len(cfg.blocks)))
        if cfg.randomize_block_order:
            rng.shuffle(order)

        for bi in order:
            block = cfg.blocks[bi]
            share = _even_split(block.n_trials, n_levels, rng)[level]
            if share <= 0:
                continue

            chunk = _generate_block_chunk(block, share, level, block_pos,
                                          cfg, by_label, rng, carry)
            trials += chunk
            block_pos += 1

    for i, t in enumerate(trials):
        t.index = i

    _mark_purges(trials, cfg)

    return Session(trials=trials, config=cfg, seed=seed,
                   cue_registry=_build_cue_registry(cfg))


def _mark_purges(trials, cfg) -> None:
    """
    Flag every trial where the liquid at a spout differs from what that
    spout last held.

    This is what makes a purge cheap: it fires on an actual change, not
    on every block boundary. In a single-liquid block both spouts are
    primed with the same solution, so moving the reward from left to
    right needs no purge at all - only the gating changes. In a choice
    block a side swap genuinely does move alcohol from one line to the
    other, and there the dead space is holding the wrong thing.
    """
    if not cfg.purge_on_liquid_change:
        return
    held: dict = {}          # spout -> liquid currently in the dead space

    for t in trials:
        wanted: dict = {sp.spout: sp.solenoid for sp in t.spouts}
        liquid_of: dict = {sp.spout: sp.liquid for sp in t.spouts}

        # At the start of a SINGLE-liquid block, prime every active spout
        # with that liquid even though only one is used per trial. It
        # costs one purge at the boundary and buys zero purges for the
        # rest of the block, because side switching then only changes
        # which gate opens, not what is in the line.
        if t.is_block_start and t.block_kind == "single" and t.spouts:
            liq = t.spouts[0].liquid
            for spout in cfg.active_spouts:
                if (liq, spout) in cfg.solenoid_map:
                    wanted.setdefault(spout, cfg.solenoid_map[(liq, spout)])
                    liquid_of.setdefault(spout, liq)

        changed = [(sp, sol) for sp, sol in sorted(wanted.items())
                   if held.get(sp) != liquid_of[sp]]
        for sp in wanted:
            held[sp] = liquid_of[sp]

        # The first trial has nothing in the lines yet. Priming there is
        # a manual step on the hardware page, not a mid-session purge.
        if changed and t.index > 0:
            t.needs_purge = True
            t.purge_spouts = changed


def resolve_durations(session, calibration) -> list:
    """
    Fill in open times from the calibration tables.

    Kept separate from generation so a session can be designed, saved
    and previewed on a machine that has never seen the rig, then have
    real durations attached once the tables exist. Returns a list of
    warnings; anything extrapolated is reported rather than used
    silently.
    """
    notes: list = []
    seen: set = set()
    for t in session.trials:
        for sp in t.spouts:
            ms, quality = calibration.ms_for(sp.solenoid, sp.volume_ul)
            sp.dispense_ms = ms
            sp.dispense_quality = quality
            key = (sp.solenoid, sp.volume_ul, quality)
            if key in seen or quality in ("exact", "interpolated"):
                continue
            seen.add(key)
            if quality == "none":
                notes.append(
                    f"Solenoid {sp.solenoid} has no calibration table, so "
                    f"{sp.volume_ul:g} \u00b5L cannot be delivered.")
            else:
                notes.append(
                    f"Solenoid {sp.solenoid}: {sp.volume_ul:g} \u00b5L is "
                    f"outside the measured range, so its open time is "
                    f"extrapolated. Measure a point near it.")
    return notes


def _generate_block_chunk(block: BlockSpec, n: int, level: int,
                          block_pos: int, cfg: SessionConfig,
                          by_label: dict, rng: random.Random,
                          carry: dict) -> list[PlannedTrial]:
    if block.kind == "single":
        out = _generate_single_block(block, n, level, block_pos, cfg,
                                     by_label, rng, carry)
    else:
        out = _generate_choice_block(block, n, level, block_pos, cfg,
                                     by_label, rng, carry)
    k = cfg.max_repeat
    carry["sides"] = [t.spouts[0].spout for t in out][-k:]
    carry["types"] = [t.spouts[0].trial_type for t in out][-k:]
    return out


def _sides_for(liquid: str, cfg: SessionConfig) -> list[str]:
    return [s for s in cfg.active_spouts if (liquid, s) in cfg.solenoid_map]


def _generate_single_block(block, n, level, block_pos, cfg, by_label, rng, carry):
    liquid = block.liquids[0]
    types = [t for t in block.trial_type_labels
             if by_label[t].liquid == liquid]

    counts = dict(zip(types, _even_split(n, len(types), rng)))
    seq = _balanced_sequence(counts, n, cfg.balance_window,
                             cfg.max_repeat, rng, carry.get("types"))

    sides = _sides_for(liquid, cfg)
    if cfg.randomize_sides and len(sides) > 1:
        side_seq = _balanced_sequence(
            dict(zip(sides, _even_split(n, len(sides), rng))),
            n, cfg.balance_window, cfg.max_repeat, rng, carry.get("sides"))
    else:
        # Fixed mapping: the liquid always appears on its first mapped
        # spout. Sound for training; for a preference measurement it lets
        # side bias masquerade as liquid preference.
        side_seq = [sides[0]] * n

    reward_flags = _reward_flags_by_type(seq, by_label, rng)

    out = []
    for i in range(n):
        tt = by_label[seq[i]]
        side = side_seq[i]
        out.append(PlannedTrial(
            index=-1, block_index=block_pos, block_label=block.label,
            block_kind=block.kind, ratio_level=level,
            ratio=_ratio_for(cfg.operant, level, rng), choice=False,
            spouts=[SpoutPlan(spout=side, liquid=liquid, trial_type=tt.label,
                              solenoid=cfg.solenoid_map[(liquid, side)],
                              volume_ul=tt.volume_ul, cue=tt.cue,
                              rewarded=reward_flags[i])],
            iti_s=_truncated_exponential(rng, cfg.iti_mean_s - cfg.iti_min_s,
                                         cfg.iti_min_s, cfg.iti_max_s),
            is_block_start=(i == 0), block_cue=block.cue if i == 0 else None))
    return out


def _generate_choice_block(block, n, level, block_pos, cfg, by_label, rng, carry):
    la, lb = block.liquids
    types_a = [t for t in block.trial_type_labels if by_label[t].liquid == la]
    types_b = [t for t in block.trial_type_labels if by_label[t].liquid == lb]

    # Every combination of an A-type with a B-type, used equally often.
    pairs = [(a, b) for a in types_a for b in types_b]
    counts = dict(zip(range(len(pairs)), _even_split(n, len(pairs), rng)))
    pair_seq = _balanced_sequence(counts, n, cfg.balance_window,
                                  cfg.max_repeat, rng)

    sides_a = _sides_for(la, cfg)
    sides_b = _sides_for(lb, cfg)
    # Orientation: which side liquid A occupies. Balanced and run-limited,
    # so the animal cannot solve the task by always going to one side.
    orient_opts = [s for s in sides_a if any(o != s for o in sides_b)]
    if not orient_opts:
        orient_opts = sides_a
    if cfg.randomize_sides and len(orient_opts) > 1:
        orient_seq = _balanced_sequence(
            dict(zip(orient_opts, _even_split(n, len(orient_opts), rng))),
            n, cfg.balance_window, cfg.max_repeat, rng, carry.get("sides"))
    else:
        orient_seq = [orient_opts[0]] * n

    seq_a = [pairs[p][0] for p in pair_seq]
    seq_b = [pairs[p][1] for p in pair_seq]
    flags_a = _reward_flags_by_type(seq_a, by_label, rng)
    flags_b = _reward_flags_by_type(seq_b, by_label, rng)

    out = []
    for i in range(n):
        ta = by_label[seq_a[i]]
        tb = by_label[seq_b[i]]
        side_a = orient_seq[i]
        others = [s for s in sides_b if s != side_a]
        side_b = others[0] if others else sides_b[0]
        ratio = _ratio_for(cfg.operant, level, rng)

        out.append(PlannedTrial(
            index=-1, block_index=block_pos, block_label=block.label,
            block_kind=block.kind, ratio_level=level, ratio=ratio,
            choice=True,
            spouts=[
                SpoutPlan(spout=side_a, liquid=la, trial_type=ta.label,
                          solenoid=cfg.solenoid_map[(la, side_a)],
                          volume_ul=ta.volume_ul, cue=ta.cue,
                          rewarded=flags_a[i]),
                SpoutPlan(spout=side_b, liquid=lb, trial_type=tb.label,
                          solenoid=cfg.solenoid_map[(lb, side_b)],
                          volume_ul=tb.volume_ul, cue=tb.cue,
                          rewarded=flags_b[i]),
            ],
            iti_s=_truncated_exponential(rng, cfg.iti_mean_s - cfg.iti_min_s,
                                         cfg.iti_min_s, cfg.iti_max_s),
            is_block_start=(i == 0), block_cue=block.cue if i == 0 else None))
    return out


def _reward_flags_by_type(seq: Sequence[str], by_label: dict,
                          rng: random.Random) -> list[bool]:
    """
    Assign reward flags so each trial type hits its contingency EXACTLY
    within this chunk, then scatter them back into sequence order.
    """
    idx_by_type: dict = {}
    for i, lbl in enumerate(seq):
        idx_by_type.setdefault(lbl, []).append(i)

    flags = [False] * len(seq)
    for lbl, idxs in idx_by_type.items():
        pct = by_label[lbl].reward_contingency_pct
        for i, f in zip(idxs, _exact_reward_flags(len(idxs), pct, rng)):
            flags[i] = f
    return flags


# =====================================================================
# Audit
# =====================================================================

def audit_session(sess: Session) -> dict:
    """
    Check that the generated session actually honours its constraints.
    Randomization code is easy to get subtly wrong and the failure is
    invisible in the data, so this is worth running every time.
    """
    cfg = sess.config
    report: dict = {"problems": [], "warnings": [], "stats": {}}

    types_seq = [t.spouts[0].trial_type for t in sess.trials]
    v = _violations(types_seq, cfg.max_repeat)
    if v:
        report["warnings"].append(
            f"{v} positions exceed max_repeat={cfg.max_repeat} for trial "
            f"type. This can be arithmetically unavoidable when one type "
            f"dominates a block.")

    side_seq = [t.spouts[0].spout for t in sess.trials]
    v = _violations(side_seq, cfg.max_repeat)
    if v:
        report["warnings"].append(
            f"{v} positions exceed max_repeat={cfg.max_repeat} for side.")

    for liquid, sides in sess.side_balance().items():
        if len(sides) > 1:
            lo, hi = min(sides.values()), max(sides.values())
            if hi > 0 and (hi - lo) / hi > 0.15:
                report["warnings"].append(
                    f"{liquid}: side imbalance {sides}. More than 15% apart.")

    for lbl, got in sess.reward_rate_by_trial_type().items():
        want = next(t.reward_contingency_pct
                    for t in cfg.trial_types if t.label == lbl)
        if abs(got - want) > 2.0:
            report["problems"].append(
                f"{lbl}: reward rate {got:.1f}% but contingency is {want}%.")

    if cfg.operant.mode == "progressive":
        per_level: dict = {}
        for t in sess.trials:
            per_level[t.ratio_level] = per_level.get(t.ratio_level, 0) + 1
        report["stats"]["trials_per_ratio_level"] = per_level
        lo, hi = min(per_level.values()), max(per_level.values())
        if hi - lo > len(cfg.blocks):
            report["problems"].append(
                f"Ratio levels are unevenly filled: {per_level}")

    report["stats"].update({
        "n_trials": sess.n_trials,
        "counts_by_trial_type": sess.counts_by_trial_type(),
        "counts_by_block": sess.counts_by_block(),
        "side_balance": sess.side_balance(),
        "realized_iti_mean_s": round(sess.realized_iti_mean, 2),
        "estimated_duration_min": round(sess.estimated_duration_s() / 60.0, 1),
    })
    return report
