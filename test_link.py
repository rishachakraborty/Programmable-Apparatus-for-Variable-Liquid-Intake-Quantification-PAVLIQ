"""
test_link.py — smoke test for arduino_link against real hardware.

Run with NO ANIMAL on the rig.

    python test_link.py                 autodetect the port
    python test_link.py /dev/cu.usbmodem1101
    python test_link.py COM3 --trial    also run live trials

Everything except --trial is non-destructive: no solenoid fires and no
servo moves until you are prompted.
"""

import sys
import time

from arduino_link import ArduinoLink, ArduinoError


def hr(title):
    print(f"\n{'=' * 62}\n{title}\n{'=' * 62}")


def main():
    args = [a for a in sys.argv[1:] if not a.startswith("--")]
    do_trial = "--trial" in sys.argv
    port = args[0] if args else None

    hr("CONNECT")
    print("Ports found:")
    for dev, desc in ArduinoLink.list_serial_ports():
        print(f"   {dev:28s} {desc}")

    link = ArduinoLink(port=port)
    print(f"\nConnecting{' to ' + port if port else ' (autodetect)'} ...")
    print("The Mega resets when the port opens, so this takes a moment.")
    fw = link.connect()
    print(f"Firmware: {fw}")

    # Every event is printed as it arrives, which also proves that
    # asynchronous events are not being swallowed by the command path.
    link.add_event_listener(
        lambda e: print(f"   [event] {e.t_ms:>8} ms  {e.type:<16} "
                        f"{e.ch:<2} {e.d1:>7} {e.d2:>7}"))

    hr("HOUSEKEEPING")
    print(f"ping           : {link.ping()}")
    print(f"clock offset   : {link.clock_offset:.4f} s "
          f"(+/- {link.clock_uncertainty * 1000:.1f} ms)")
    print("A large uncertainty means a congested USB bus. It affects only")
    print("wall-clock reporting; analysis runs on the Arduino clock.")

    hr("SOLENOID IDENTITIES")
    for s in link.solenoid_get_all():
        cal = f"{s.nl_per_ms} nL/ms" if s.nl_per_ms else "UNCALIBRATED"
        print(f"   {s.index}: {s.liquid or 'UNSET':<12} spout {s.spout:<5} {cal}")

    hr("SERVOS")
    for ch in ("l", "c", "r"):
        s = link.servo_read(ch)
        print(f"   {s.ch}: pos {s.current:>4} target {s.target:>4} "
              f"zero {s.zero_angle:>4} extend "
              f"{s.extend_angle if s.extend_set else '  --':>4} "
              f"limits {s.soft_min}-{s.soft_max} "
              f"{'attached' if s.attached else 'detached'} "
              f"{'' if s.pos_known else 'POS UNVERIFIED'}")

    hr("LICK SENSORS")
    for lk in link.lick_read_all():
        if lk.calibrated:
            snr = f"{lk.snr:.1f}x noise" if lk.snr else "n/a"
            print(f"   {lk.ch}: baseline {lk.baseline:7.1f} sd {lk.sd:5.2f} "
                  f"on {lk.on_delta:6.1f} off {lk.off_delta:6.1f} "
                  f"pol {lk.polarity:+d}  {snr}  count {lk.count}")
        else:
            print(f"   {lk.ch}: NOT CALIBRATED (raw {lk.last_raw})")

    hr("READINESS")
    rep = link.readiness_report(spouts=("l", "r"))
    if rep["problems"]:
        print("BLOCKING PROBLEMS:")
        for p in rep["problems"]:
            print(f"   x {p}")
    if rep["warnings"]:
        print("Warnings:")
        for w in rep["warnings"]:
            print(f"   ! {w}")
    if rep["ready"] and not rep["warnings"]:
        print("   All clear.")

    hr("CUES")
    input("Press Enter to flash the white LED and play a tone... ")
    link.led("w", 400, brightness=200)
    time.sleep(0.6)
    link.speaker("l", 400, 10000, click_train=True, click_hz=50)
    time.sleep(0.8)

    print("\nSynchronised onset: both speakers, one GO.")
    print("Two separate commands would be milliseconds apart; this is not.")
    input("Press Enter... ")
    link.disarm()
    link.arm(link.speaker_cmd("l", 800, 10000, click_train=True, click_hz=50))
    link.arm(link.speaker_cmd("r", 800, 5000, click_train=True, click_hz=200))
    link.go()
    time.sleep(1.2)

    hr("ERROR HANDLING")
    print("Deliberately sending a bad command; an exception here is a PASS.")
    try:
        link.servo_write("l", 999)
    except ArduinoError as e:
        print(f"   caught as expected: {e}")
    else:
        print("   PROBLEM: out-of-range angle was accepted.")

    if do_trial:
        hr("LIVE TRIAL")
        if not rep["ready"]:
            print("Skipping: readiness check did not pass.")
        else:
            print("Single-spout trial on LEFT, FR 3, reward on completion.")
            input("Press Enter, then touch the left spout three times... ")
            outcome = link.run_trial(
                trial_id=901, choice=False,
                cues=[link.speaker_cmd("l", 500, 10000,
                                       click_train=True, click_hz=50)],
                spouts=[{"ch": "l", "solenoid": 1, "dispense_ms": 50,
                         "ratio": 3, "rewarded": True}],
                cue_reward_ms=1000, omission_ms=6000,
                retract_delay_ms=800, iti_ms=2000,
                wait=True, timeout=30)
            from arduino_link import OUTCOME_NAMES
            print(f"\n   outcome: {OUTCOME_NAMES.get(outcome, outcome)}")

    hr("DONE")
    link.disconnect()
    print("Disconnected. STOPALL was sent first: gates closed, cues off.")


if __name__ == "__main__":
    main()
