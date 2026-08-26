#!/usr/bin/env python3
"""Infer the flown task from tracklogs, when the supplied task file does not match.

This is a diagnostic, NOT a scoring input. Anything it produces is labelled
RECONSTRUCTED and must not be treated as official.

Method
------
1. Take the competition's waypoint catalogue from the supplied .xctsk.
2. For every pilot, find the *first entry* into each cylinder (entry time, not
   closest approach -- closest approach is unreliable when waypoints sit a few
   km apart, because a pilot thermalling nearby produces a spurious ordering).
3. Order candidate waypoints by median first-entry time.
4. Search ordered subsets for the route completed, in order, by the most
   pilots; longer routes win ties.

Step 4 is what separates a real turnpoint from one merely transited. On the
supplied data, B110 and G23 are entered by 10 and 1 pilots respectively while
flying between B108 and D03 -- they look like turnpoints under any
count-based threshold, and only the subsequence search rejects them.
"""

from __future__ import annotations

import itertools
import json
import math
import statistics
import sys

from engine.geo import zone_crossing
from engine.igc import load_tracks
from engine.score import project
from engine.task import parse_xctsk

MIN_REACH = 0.08


def first_entry(fixes, w) -> float | None:
    prev = None
    for f in fixes:
        if prev is not None:
            if math.hypot(f.x - w.x, f.y - w.y) <= w.outer:
                return float(f.t)
            c = zone_crossing((prev.x, prev.y, float(prev.t)), (f.x, f.y, float(f.t)),
                              w.x, w.y, w.inner, w.outer)
            if c is not None and not c[1]:
                return c[0]
        prev = f
    return None


def last_exit(fixes, w, before) -> float | None:
    prev, out = None, None
    for f in fixes:
        if before is not None and f.t > before:
            break
        if prev is not None:
            was = math.hypot(prev.x - w.x, prev.y - w.y) <= w.outer
            now = math.hypot(f.x - w.x, f.y - w.y) <= w.outer
            if was and not now:
                out = float(f.t)
        prev = f
    return out


def main() -> int:
    task_path = sys.argv[1] if len(sys.argv) > 1 else "TASK 06 - AGER.xctsk"
    igc_path = sys.argv[2] if len(sys.argv) > 2 else "igcs"
    out_path = sys.argv[3] if len(sys.argv) > 3 else "TASK-RECONSTRUCTED.xctsk"

    tracks, day = load_tracks(igc_path)
    task = parse_xctsk(task_path, day)
    for tr in tracks:
        project(task, tr.fixes)
    n = len(tracks)

    # Unique waypoints from the catalogue. A point can appear twice (G01 is
    # both ESS r=1000 and goal r=400); keep the TIGHTEST radius.
    #
    # This matters more than it looks. D03 sits 2.9 km from G01, so a pilot
    # heading north to D03 passes through G01's 1 km ESS ring on the way. Using
    # the ESS radius makes G01's first entry precede D03's, and every route
    # ending at G01 then scores zero completers -- which is exactly how the
    # first version of this script talked itself into a 3-turnpoint task.
    by_key: dict[tuple, object] = {}
    for w in task.waypoints:
        key = (round(w.lat, 5), round(w.lon, 5))
        prev = by_key.get(key)
        if prev is None or w.raw_radius < prev.raw_radius:
            by_key[key] = w
    uniq = sorted(by_key.values(), key=lambda w: w.index)

    entries = {w.name: {} for w in uniq}
    for w in uniq:
        for tr in tracks:
            t = first_entry(tr.fixes, w)
            if t is not None:
                entries[w.name][tr.pilot] = t

    hm = lambda s: f"{int(s - day)//3600:02d}:{int(s - day)%3600//60:02d}"
    print(f"waypoint catalogue vs {n} tracklogs\n")
    print(f"  {'WAYPOINT':<8} {'RADIUS':>8} {'ENTERED':>10} {'MEDIAN ENTRY':>13}")
    print("  " + "-" * 44)
    cands = []
    for w in uniq:
        e = entries[w.name]
        med = statistics.median(e.values()) if e else None
        print(f"  {w.name:<8} {w.raw_radius:>7.0f}m {len(e):>6}/{n} {(hm(med) if med else '–'):>13}")
        if len(e) / n >= MIN_REACH and med is not None:
            cands.append((med, w))
    cands.sort()

    takeoff = cands[0][1]
    mids = [w for _, w in cands[1:]]

    # --- subsequence search ------------------------------------------
    def completers(route) -> int:
        c = 0
        for tr in tracks:
            t_prev = -1.0
            ok = True
            for w in route:
                t = entries[w.name].get(tr.pilot)
                if t is None or t < t_prev:
                    ok = False
                    break
                t_prev = t
            if ok:
                c += 1
        return c

    # Objective: the LONGEST route that a credible number of pilots completed
    # in order. Maximising completers alone is wrong -- a one-turnpoint route
    # is always completed by more pilots, so the search collapses to the
    # trivial task. In a real competition goal is reached by a minority, so
    # the route is bounded below by a finisher count, not above.
    goal_w = task.waypoints[task.goal_index]
    goal_key = (round(goal_w.lat, 5), round(goal_w.lon, 5))
    goal_cand = next((w for w in mids
                      if (round(w.lat, 5), round(w.lon, 5)) == goal_key), None)
    if goal_cand is None:
        print("\n  goal waypoint never reached — cannot reconstruct")
        return 1
    body = [w for w in mids if w is not goal_cand]
    floor = max(3, round(0.05 * n))

    best, best_key = [], (-1, -1)
    for r in range(len(body), -1, -1):
        for combo in itertools.combinations(body, r):
            route = list(combo) + [goal_cand]
            c = completers(route)
            if c < floor:
                continue
            key = (len(route), c)
            if key > best_key:
                best_key, best = key, route
    if not best:
        print(f"\n  no route completed by at least {floor} pilots — cannot reconstruct")
        return 1
    route = best
    dropped = [w.name for w in mids if w not in route]

    print(f"\n  longest route completed in order by >= {floor} pilots: "
          f"{best_key[1]}/{n} completed it")
    if dropped:
        print(f"  dropped as transit (entered, but not on any consistent route): "
              f"{', '.join(dropped)}")

    mid = route[:-1]
    goal_w = route[-1]

    # The real start gate is unknowable from tracklogs. Use the EARLIEST
    # departure rather than the median: a gate set too late turns genuine
    # starters into early starts (S7F 13.3), which in paragliding scores them
    # for launch-to-SSS distance only and silently deletes their whole flight.
    # A gate set too early costs nothing but slightly understated speeds, and
    # understates them equally for everyone, so the ranking survives.
    gates = [t for t in (last_exit(tr.fixes, takeoff,
                                   entries[mid[0].name].get(tr.pilot) if mid else None)
                         for tr in tracks) if t]
    gate = int(min(gates)) if gates else task.first_gate
    gs = gate - day

    def wp(w, radius, kind=None):
        d = {"radius": int(radius),
             "waypoint": {"name": w.name, "lat": w.lat, "lon": w.lon, "altSmoothed": 0}}
        if kind:
            d["type"] = kind
        return d

    doc = {
        "version": 1, "taskType": "CLASSIC", "earthModel": "WGS84",
        "_reconstructed": True,
        "_note": "Inferred from tracklogs. NOT an official task definition.",
        "_completed_by": f"{best_key[0]}/{n} pilots",
        "turnpoints": (
            [wp(takeoff, takeoff.raw_radius, "TAKEOFF"),
             wp(takeoff, takeoff.raw_radius, "SSS")]
            + [wp(w, w.raw_radius) for w in mid]
            + [wp(goal_w, 1000, "ESS"), wp(goal_w, 400)]
        ),
        "sss": {"type": "RACE", "direction": "EXIT",
                "timeGates": [f"{gs//3600:02d}:{gs%3600//60:02d}:{gs%60:02d}Z"]},
        "goal": {"type": "CYLINDER", "deadline": "16:00:00Z"},
    }
    with open(out_path, "w") as f:
        json.dump(doc, f, indent=2)

    print(f"\n  route : {' → '.join(t['waypoint']['name'] for t in doc['turnpoints'])}")
    print(f"  gate  : {doc['sss']['timeGates'][0]} "
          f"(earliest departure from {takeoff.name} — see note in source)")
    print(f"\n  wrote {out_path}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
