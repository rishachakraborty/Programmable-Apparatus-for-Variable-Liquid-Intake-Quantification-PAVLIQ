"""
demo_task.py — builds the actual two-choice alcohol/water task and
audits the generated session. No hardware needed.

    python demo_task.py
    python demo_task.py --seed 42 --json session.json
"""

import argparse

from calibration import CalibrationSet, LookupTable
from task_design import (
    SpeakerCue, LedCue, CueSet, TrialType, BlockSpec, OperantDesign,
    SessionConfig, generate_session, audit_session, resolve_durations,
)


def build_config(seed=None) -> SessionConfig:
    # Reward amount is encoded two ways at once: the CLICK RATE tells the
    # animal what is coming, the VOLUME delivers it. They must be kept
    # consistent or the cue is a lie. Open time is no longer written
    # here - it comes from the calibration table at build time.
    amounts = [(50, 1.0), (100, 3.0), (200, 6.0), (400, 10.0)]  # Hz, uL

    trial_types = []
    for click_hz, ul in amounts:
        trial_types.append(TrialType(
            label=f"alc_{click_hz}", liquid="alcohol",
            cue=CueSet(speaker=SpeakerCue(duration_ms=500, tone_hz=12000,
                                          click_train=True,
                                          click_hz=click_hz, volume=50)),
            volume_ul=ul, reward_contingency_pct=100.0))
        trial_types.append(TrialType(
            label=f"wat_{click_hz}", liquid="water",
            cue=CueSet(speaker=SpeakerCue(duration_ms=500, tone_hz=5000,
                                          click_train=True,
                                          click_hz=click_hz, volume=50)),
            volume_ul=ul, reward_contingency_pct=100.0))

    blocks = [
        BlockSpec(label="alcohol_only", kind="single", liquids=["alcohol"],
                  n_trials=100,
                  trial_type_labels=[f"alc_{h}" for h, _ in amounts],
                  cue=CueSet(led=LedCue(channel="w", duration_ms=2000,
                                        brightness=255))),
        BlockSpec(label="water_only", kind="single", liquids=["water"],
                  n_trials=100,
                  trial_type_labels=[f"wat_{h}" for h, _ in amounts],
                  cue=CueSet(led=LedCue(channel="b", duration_ms=2000,
                                        brightness=255))),
        BlockSpec(label="choice", kind="choice", liquids=["alcohol", "water"],
                  n_trials=100,
                  trial_type_labels=([f"alc_{h}" for h, _ in amounts] +
                                     [f"wat_{h}" for h, _ in amounts]),
                  cue=CueSet(led=LedCue(channel="g", duration_ms=2000,
                                        brightness=255))),
    ]

    return SessionConfig(
        trial_types=trial_types,
        blocks=blocks,
        # Solenoid 1 water/left, 2 alcohol/left, 3 water/right, 4 alcohol/right
        solenoid_map={("water", "l"): 1, ("alcohol", "l"): 2,
                      ("water", "r"): 3, ("alcohol", "r"): 4},
        active_spouts=["l", "r"],
        iti_mean_s=8.0, iti_min_s=3.0, iti_max_s=30.0,
        cue_reward_delay_ms=1000, omission_window_ms=5000,
        iti_retract_delay_ms=1000, quiet_gate_ms=500,
        max_repeat=3, balance_window=20,
        operant=OperantDesign(mode="progressive", ratio_set=[1, 2, 4, 8]),
        randomize_block_order=True, randomize_sides=True,
        use_retraction=True, purge_on_liquid_change=True, seed=seed)


def build_calibration() -> CalibrationSet:
    """Stand-in measurements. Replace with real gravimetric points."""
    cs = CalibrationSet(shared=True)
    cs.shared_table = LookupTable(label="shared")
    cs.shared_table.set_points([(1.0, 12), (3.0, 26), (6.0, 46), (10.0, 74)])
    return cs


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--seed", type=int, default=None)
    ap.add_argument("--json", type=str, default=None)
    args = ap.parse_args()

    cfg = build_config(seed=args.seed)
    problems = cfg.validate()
    if problems:
        print("Configuration invalid:")
        for p in problems:
            print("  -", p)
        return

    sess = generate_session(cfg)
    for note in resolve_durations(sess, build_calibration()):
        print("  calibration:", note)
    print(f"Seed {sess.seed} — store this; it reproduces the session exactly.")
    print(f"{sess.n_trials} trials\n")

    print("BLOCK ORDER AS EXECUTED")
    print("Block order is redrawn at each ratio level, so ratio and block")
    print("order are not confounded.\n")
    last = None
    for lvl, label, n in sess.block_order():
        if lvl != last:
            fr = cfg.operant.ratio_set[lvl]
            print(f"  --- FR {fr} ---")
            last = lvl
        print(f"      {label:<16} {n:>4} trials")

    rep = audit_session(sess)
    print("\nCOUNTS BY TRIAL TYPE")
    for k, v in sorted(rep["stats"]["counts_by_trial_type"].items()):
        print(f"   {k:<12} {v}")

    print("\nSIDE BALANCE")
    for liq, sides in rep["stats"]["side_balance"].items():
        print(f"   {liq:<10} {sides}")

    print(f"\nITI: requested scale {cfg.iti_mean_s}s from "
          f"{cfg.iti_min_s}-{cfg.iti_max_s}s")
    print(f"     realized mean {rep['stats']['realized_iti_mean_s']}s")
    print("     (truncation at the maximum pulls the mean down; this is")
    print("      the number to quote in a methods section)")
    print(f"\nEstimated worst-case duration: "
          f"{rep['stats']['estimated_duration_min']} min")

    print("\nAUDIT")
    if rep["problems"]:
        for p in rep["problems"]:
            print("   PROBLEM:", p)
    if rep["warnings"]:
        for w in rep["warnings"]:
            print("   warning:", w)
    if not rep["problems"] and not rep["warnings"]:
        print("   All constraints satisfied.")

    print("\nFIRST 12 TRIALS")
    for t in sess.trials[:12]:
        marker = "*" if t.is_block_start else " "
        spec = "  ".join(
            f"{s.spout.upper()}:{s.trial_type}"
            f"{'' if s.rewarded else '(no rwd)'}" for s in t.spouts)
        print(f" {marker}{t.index:>4} {t.block_label:<14} FR{t.ratio:<3} "
              f"ITI {t.iti_s:5.1f}s   {spec}")

    n_purge = sum(1 for t in sess.trials if t.needs_purge)
    print(f"\nPurges needed: {n_purge} of {sess.n_trials} trials "
          f"({100.0 * n_purge / sess.n_trials:.0f}%)")

    print("\nHARDWARE SEQUENCE, first choice trial")
    ex = next((t for t in sess.trials if t.choice), sess.trials[0])
    for t_ms, desc in ex.hardware_sequence(cfg):
        stamp = f"{t_ms:>6} ms" if t_ms is not None else "  event"
        print(f"   {stamp}  {desc}")

    if args.json:
        with open(args.json, "w") as f:
            f.write(sess.to_json())
        print(f"\nWrote {args.json}")


if __name__ == "__main__":
    main()
