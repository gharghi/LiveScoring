"""S7F 8.1 — start selection, on synthetic flights with a known answer.

The start is the single most consequential decision the engine makes: a pilot
who did not start scores minimum distance no matter how far they flew, so a
false negative silently deletes a whole flight and a false positive invents
one. Real tracklogs cannot test it, because they only ever show whatever the
pilots actually did. These are hand-built flights with a known right answer.

Covers S7F 8.1 (re-starting), 9.2.1 (zone crossings) and 13.3 (early start).
"""

from __future__ import annotations

import calendar
import json
import os
import tempfile

from engine.gap import GapParams
from engine.igc import Fix
from engine.rules.start_selection import (better_start,
                                          is_multi_start,
                                          select_candidates)
from engine.score import project, score_pilot
from engine.task import parse_xctsk

DAY = calendar.timegm((2026, 8, 8, 0, 0, 0, 0, 0, 0))
GATE = 12 * 3600
LAUNCH = (42.0, 1.0)
TP1 = (42.10, 1.0)
GOAL = (42.20, 1.0)

PARAMS = GapParams(nominal_distance=20000, minimum_distance=1000, nominal_time=3600)


def _task(direction="EXIT", gates=("12:00:00Z",), start_at=LAUNCH):
    doc = {
        "version": 1, "taskType": "CLASSIC", "earthModel": "WGS84",
        "turnpoints": [
            {"type": "TAKEOFF", "radius": 400,
             "waypoint": {"name": "TO", "lat": LAUNCH[0], "lon": LAUNCH[1], "altSmoothed": 0}},
            {"type": "SSS", "radius": 1000,
             "waypoint": {"name": "SSS", "lat": start_at[0], "lon": start_at[1], "altSmoothed": 0}},
            {"radius": 400,
             "waypoint": {"name": "TP1", "lat": TP1[0], "lon": TP1[1], "altSmoothed": 0}},
            {"type": "ESS", "radius": 1000,
             "waypoint": {"name": "ESS", "lat": GOAL[0], "lon": GOAL[1], "altSmoothed": 0}},
            {"radius": 400,
             "waypoint": {"name": "GOAL", "lat": GOAL[0], "lon": GOAL[1], "altSmoothed": 0}},
        ],
        "sss": {"type": "RACE", "direction": direction, "timeGates": list(gates)},
        "goal": {"type": "CYLINDER", "deadline": "18:00:00Z"},
    }
    fd, path = tempfile.mkstemp(suffix=".xctsk")
    with os.fdopen(fd, "w") as fh:
        json.dump(doc, fh)
    try:
        return parse_xctsk(path, DAY)
    finally:
        os.unlink(path)


def _leg(fixes, a, b, t0, t1, step=5):
    """Straight line from a to b, one fix every `step` seconds."""
    span = max(1, t1 - t0)
    t = t0
    while t <= t1:
        f = (t - t0) / span
        fixes.append(Fix(t=DAY + t,
                         lat=a[0] + (b[0] - a[0]) * f,
                         lon=a[1] + (b[1] - a[1]) * f,
                         alt_baro=1500, alt_gps=1500))
        t += step
    return fixes


def _hold(fixes, at, t0, t1, step=10):
    return _leg(fixes, at, at, t0, t1, step)


# 0.010 deg lat ~= 1.11 km, so 42.010 is just outside a 1000 m SSS cylinder.
OUT_N = (42.012, 1.0)      # north of the SSS, outside it
FAR_E = (42.0, 1.08)       # ~6.6 km east of the SSS, never inside


def case_normal_exit():
    """Sits inside the SSS, leaves after the gate, completes the task."""
    t = _task()
    fx = []
    _hold(fx, LAUNCH, GATE - 600, GATE + 60)
    _leg(fx, LAUNCH, GOAL, GATE + 60, GATE + 3600)
    project(t, fx)
    r = score_pilot(t, fx, 1e18, PARAMS)
    return t, r


def case_never_entered():
    """Flies the whole task well east of the SSS: cannot start."""
    t = _task()
    fx = []
    _hold(fx, FAR_E, GATE - 600, GATE + 60)
    _leg(fx, FAR_E, (GOAL[0], FAR_E[1]), GATE + 60, GATE + 3600)
    project(t, fx)
    return t, score_pilot(t, fx, 1e18, PARAMS)


def case_early_start():
    """Leaves the SSS before the gate and never comes back (S7F 13.3)."""
    t = _task()
    fx = []
    _hold(fx, LAUNCH, GATE - 1200, GATE - 600)
    _leg(fx, LAUNCH, GOAL, GATE - 600, GATE + 2400)
    project(t, fx)
    return t, score_pilot(t, fx, 1e18, PARAMS)


def case_enter_start():
    """ENTER start: outside at the gate, flies in, then completes."""
    t = _task(direction="ENTER")
    fx = []
    _hold(fx, OUT_N, GATE - 600, GATE + 60)
    _leg(fx, OUT_N, LAUNCH, GATE + 60, GATE + 300)
    _leg(fx, LAUNCH, GOAL, GATE + 300, GATE + 3600)
    project(t, fx)
    return t, score_pilot(t, fx, 1e18, PARAMS)


def case_restart_multi_gate():
    """Two gates. Starts on the first, returns, restarts on the second (S7F 8.1).

    Goal is reached after the second start, so the second start is the one
    that must be scored -- 'the last start after which they reached goal'.
    """
    t = _task(gates=("12:00:00Z", "12:30:00Z"))
    fx = []
    _hold(fx, LAUNCH, GATE - 600, GATE + 60)
    _leg(fx, LAUNCH, TP1, GATE + 60, GATE + 900)          # out, tags TP1
    _leg(fx, TP1, LAUNCH, GATE + 900, GATE + 1740)        # back inside the SSS
    _hold(fx, LAUNCH, GATE + 1740, GATE + 1860)           # waits for gate 2
    _leg(fx, LAUNCH, GOAL, GATE + 1860, GATE + 5000)      # restarts, goes to goal
    project(t, fx)
    return t, score_pilot(t, fx, 1e18, PARAMS)


def case_single_gate_no_restart():
    """One gate: re-starting does not apply (S7F 8.1).

    The pilot leaves, comes back, leaves again -- all before validating TP1.
    The scored start is the LAST crossing before TP1, and the start clock is
    still the single gate.
    """
    t = _task()
    fx = []
    _hold(fx, LAUNCH, GATE - 600, GATE + 60)
    _leg(fx, LAUNCH, OUT_N, GATE + 60, GATE + 300)
    _leg(fx, OUT_N, LAUNCH, GATE + 300, GATE + 540)
    _hold(fx, LAUNCH, GATE + 540, GATE + 900)
    _leg(fx, LAUNCH, GOAL, GATE + 900, GATE + 4500)
    project(t, fx)
    return t, score_pilot(t, fx, 1e18, PARAMS)


def case_direction_ignored():
    """S7F 6.2.1: the declared direction must not affect scoring.

    The same flight is scored against a task declared EXIT and the same task
    declared ENTER. Since 2020 the enter/exit designation is advisory only
    ("pilots are not bound to those indications"), so both must agree.
    """
    fx = []
    _hold(fx, OUT_N, GATE - 600, GATE + 60)
    _leg(fx, OUT_N, LAUNCH, GATE + 60, GATE + 300)
    _leg(fx, LAUNCH, GOAL, GATE + 300, GATE + 3600)

    out = []
    for direction in ("EXIT", "ENTER"):
        t = _task(direction=direction)
        f2 = [Fix(t=f.t, lat=f.lat, lon=f.lon, alt_baro=f.alt_baro, alt_gps=f.alt_gps)
              for f in fx]
        project(t, f2)
        out.append(score_pilot(t, f2, 1e18, PARAMS))
    return out


def run() -> list[tuple[str, bool, str]]:
    """Seven hand-built flights, plus the selection rule on its own.

    Real tracklogs can only ever show what the pilots actually did, so the
    start rule — the single most consequential decision the engine makes —
    has to be tested against flights whose right answer is known in advance.
    A false negative silently deletes a whole flight; a false positive
    invents one.
    """
    out = _rule_checks()

    t, r = case_normal_exit()
    out.append(("exit start, inside then out after gate → STARTED",
                r.start_time == DAY + GATE and r.goal_time is not None,
                f"start={r.start_time and r.start_time - DAY - GATE:+.0f}s vs gate, "
                f"goal={'yes' if r.goal_time else 'NO'}"))

    t, r = case_never_entered()
    out.append(("never inside SSS → NOT started",
                r.start_time is None and r.did_not_start and not r.early_start,
                f"start={r.start_time}, dns={r.did_not_start}, early={r.early_start}"))

    t, r = case_early_start()
    out.append(("left SSS before gate → early start, NOT started (S7F 13.3)",
                r.start_time is None and r.early_start,
                f"start={r.start_time}, early={r.early_start}"))

    t, r = case_enter_start()
    out.append(("ENTER start, outside then in → STARTED",
                r.start_time == DAY + GATE and r.goal_time is not None,
                f"start={r.start_time and r.start_time - DAY - GATE:+.0f}s vs gate, "
                f"goal={'yes' if r.goal_time else 'NO'}"))

    t, r = case_restart_multi_gate()
    out.append(("two gates, restart → scored on the LATER gate (S7F 8.1)",
                r.start_time == DAY + GATE + 1800 and r.goal_time is not None,
                f"start=gate+{r.start_time and r.start_time - DAY - GATE:.0f}s "
                f"(want +1800), goal={'yes' if r.goal_time else 'NO'}"))

    a, b = case_direction_ignored()
    out.append(("declared EXIT vs ENTER → identical result (S7F 6.2.1)",
                a.start_time == b.start_time and a.goal_time == b.goal_time
                and a.start_time is not None and a.goal_time is not None,
                f"EXIT: start={a.start_time and a.start_time - DAY - GATE:+.0f}s "
                f"goal={'y' if a.goal_time else 'n'}  |  "
                f"ENTER: start={b.start_time and b.start_time - DAY - GATE:+.0f}s "
                f"goal={'y' if b.goal_time else 'n'}"))

    t, r = case_single_gate_no_restart()
    out.append(("one gate, no restart → last crossing before TP1 (S7F 8.1)",
                r.start_time == DAY + GATE
                and r.start_cross_time is not None
                and r.start_cross_time > DAY + GATE + 540
                and r.goal_time is not None,
                f"clock=gate+{r.start_time and r.start_time - DAY - GATE:.0f}s, "
                f"crossing=gate+{r.start_cross_time and r.start_cross_time - DAY - GATE:.0f}s "
                f"(want >540), goal={'yes' if r.goal_time else 'NO'}"))

    return out


def _rule_checks() -> list[tuple[str, bool, str]]:
    """engine/rules/start_selection.py on its own, without a tracklog.

    The synthetic flights below exercise the rule through the whole state
    machine; these pin the rule's own three branches directly, which is what
    tells you WHICH part broke when a flight fails.
    """
    out = []
    out.append(("8.1 re-starting applies to multi-gate races and time trials",
                is_multi_start("RACE", 2) and is_multi_start("ELAPSED-TIME", 1)
                and not is_multi_start("RACE", 1),
                "RACE 1 gate → no re-start; RACE 2 gates → yes; "
                "ELAPSED-TIME → yes"))

    c = [(10, 100.0), (20, 200.0), (30, 300.0)]
    got, rule = select_candidates(c, multi_start=False,
                                  next_zone_validated_at=250.0)
    out.append(("8.1 single gate keeps the LAST crossing before the next zone",
                got == [(20, 200.0)],
                f"crossings at 100/200/300 s, next zone validated at 250 s → "
                f"{got}"))

    got, _ = select_candidates(c, multi_start=True, next_zone_validated_at=250.0)
    out.append(("8.1 multi-gate keeps every crossing as a candidate",
                got == c, f"{len(got)} candidates kept"))

    got, rule = select_candidates(c, multi_start=False,
                                  next_zone_validated_at=50.0)
    out.append(("8.1 concentric cylinders fall back to the earliest crossings",
                got == c and "concentric" in rule,
                "next zone validated BEFORE any start crossing → "
                "biggest-distance rule over the earliest crossings"))

    many = [(i, float(i)) for i in range(40)]
    got, _ = select_candidates(many, multi_start=True,
                               next_zone_validated_at=None)
    out.append(("8.1 the candidate list is capped",
                len(got) == 8,
                f"40 crossings → {len(got)} replayed (a thermalling pilot can "
                f"produce 36; each costs a full replay)"))

    got, rule = select_candidates([], multi_start=False,
                                  next_zone_validated_at=None)
    out.append(("8.1 no crossing after the gate is not an error",
                got == [] and "no crossing" in rule, rule))

    # --- better_start: which replayed candidate wins ---------------------
    def R(goal, raw, cross):
        return {"goal": goal, "raw": raw, "start_cross": cross}

    out.append(("8.1 the first candidate always wins against nothing",
                better_start(R(None, 100.0, 10.0), None, 10.0),
                "best is None → take it"))
    out.append(("8.1 without goal, the biggest distance wins",
                better_start(R(None, 200.0, 20.0), R(None, 100.0, 10.0), 20.0)
                and not better_start(R(None, 50.0, 20.0), R(None, 100.0, 10.0),
                                     20.0),
                "200 km beats 100 km; 50 km does not"))
    out.append(("8.1 reaching goal beats not reaching goal, whatever the "
                "distance",
                better_start(R(500.0, 10.0, 20.0), R(None, 99999.0, 10.0), 20.0),
                "a goal start with a tiny raw distance still wins"))
    out.append(("8.1 among goal starts the LATEST one wins",
                better_start(R(500.0, 100.0, 30.0), R(500.0, 100.0, 10.0), 30.0)
                and not better_start(R(500.0, 100.0, 5.0),
                                     R(500.0, 100.0, 10.0), 5.0),
                "'the last start after which goal was reached'"))
    out.append(("8.1 a non-goal start never displaces a goal start",
                not better_start(R(None, 99999.0, 30.0), R(500.0, 10.0, 10.0),
                                 30.0),
                "even with far more distance"))
    return out
