#!/usr/bin/env python3
"""Find which task file the tracklogs actually belong to — and what changed.

Tasks get shortened on the hill. The published .xctsk is what was planned;
what the pilots flew is often that task with turnpoints removed and the start
moved. Scoring the planned task against the flown tracks produces "nobody
started", which looks like an engine fault and is not one.

For each candidate task this reports:
  * how much of the field reached each control zone,
  * how many pilots completed the task exactly as written,
  * the longest sub-route of that task the field DID fly, and which
    waypoints were dropped to get there.

  ./match_task.py *.xctsk [--igc igcs]
"""

from __future__ import annotations

import argparse
import itertools
import math
import sys

import leaderboard as lb
from engine.geo import zone_crossing
from engine.igc import load_tracks
from engine.score import project
from engine.task import parse_xctsk

OUTLIER_KM = 200.0


def first_entry(fixes, w):
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


def analyse(task, tracks):
    uniq, seen = [], set()
    for w in task.waypoints:
        key = (round(w.lat, 5), round(w.lon, 5), w.raw_radius)
        if key in seen:
            continue
        seen.add(key)
        uniq.append(w)

    entries = {}
    for w in uniq:
        entries[w.index] = {tr.pilot: t for tr in tracks
                            if (t := first_entry(tr.fixes, w)) is not None}

    scored = [w for w in uniq if w.index >= task.start_index]

    def completers(route):
        n = 0
        for tr in tracks:
            prev_t, ok = -1.0, True
            for w in route:
                t = entries[w.index].get(tr.pilot)
                if t is None or t < prev_t:
                    ok = False
                    break
                prev_t = t
            if ok:
                n += 1
        return n

    full = completers(scored)

    # longest sub-route of this task the field actually flew, keeping the goal
    best, best_key = None, (-1, -1)
    body = scored[:-1]
    goal = scored[-1]
    if len(body) <= 14:
        for r in range(len(body), -1, -1):
            for combo in itertools.combinations(body, r):
                route = list(combo) + [goal]
                c = completers(route)
                if c < max(3, round(0.05 * len(tracks))):
                    continue
                key = (len(route), c)
                if key > best_key:
                    best_key, best = key, route
    return uniq, entries, full, best, best_key


def main() -> int:
    ap = argparse.ArgumentParser(description="Match task files against tracklogs")
    ap.add_argument("tasks", nargs="+")
    ap.add_argument("--igc", default="igcs")
    ap.add_argument("--no-color", action="store_true")
    args = ap.parse_args()
    lb.init_color(False if args.no_color else None)

    tracks, day = load_tracks(args.igc)
    print(lb.paint(f"\n  {len(tracks)} pilots from {args.igc}", lb.BOLD + lb.WHITE))

    verdicts = []
    for tpath in args.tasks:
        try:
            task = parse_xctsk(tpath, day)
        except Exception as e:
            print(lb.paint(f"  {tpath}: cannot parse — {e}", lb.RED))
            continue

        # outlier tracks: not at this task's site at all
        cx, cy = 0.0, 0.0
        keep, out = [], []
        for tr in tracks:
            project(task, tr.fixes)
            d = min(math.hypot(f.x - cx, f.y - cy) for f in tr.fixes) / 1000.0
            (out if d > OUTLIER_KM else keep).append((tr, d))

        good = [tr for tr, _ in keep]
        uniq, entries, full, best, best_key = analyse(task, good)

        print()
        print("  " + lb.paint("─" * 92, lb.GREY))
        print("  " + lb.paint(tpath, lb.BOLD + lb.WHITE)
              + lb.paint(f"   optimised {task.total_distance/1000:.1f} km", lb.GREY))
        if out:
            print("  " + lb.paint(f"⚠ {len(out)} track(s) more than {OUTLIER_KM:.0f} km from this "
                                  f"task — excluded: "
                                  f"{', '.join(t.pilot for t, _ in out[:3])}", lb.ORANGE))
        n = len(good)
        for w in uniq:
            e = entries[w.index]
            frac = len(e) / n if n else 0
            col = lb.GREEN if frac >= 0.5 else (lb.YELLOW if frac > 0.05 else lb.RED)
            kind = w.kind if w.kind != "TURNPOINT" else ""
            print("  " + lb.paint(f"   {w.name:<6} {w.raw_radius:>6.0f}m {kind:<8}", lb.GREY)
                  + lb.paint(f"{len(e):>3}/{n} reached", col))

        if full > 0:
            verdict = lb.paint(f"✓ {full}/{n} pilots completed this task as written", lb.GREEN + lb.BOLD)
        elif best:
            dropped = [w.name for w in uniq
                       if w.index >= task.start_index and w not in best]
            verdict = (lb.paint("✗ nobody completed it as written", lb.RED + lb.BOLD)
                       + lb.paint(f"\n     but {best_key[1]}/{n} flew it with "
                                  f"{', '.join(dropped)} removed", lb.YELLOW)
                       + lb.paint(f"\n     → looks like this task SHORTENED on the hill: "
                                  f"{' › '.join(w.name for w in best)}", lb.CYAN))
        else:
            verdict = lb.paint("✗ no relation to these tracklogs", lb.RED + lb.BOLD)
        print("  " + verdict)
        verdicts.append((full, best_key[1] if best else 0, tpath))

    verdicts.sort(reverse=True)
    if verdicts:
        print()
        print("  " + lb.paint("BEST MATCH", lb.BOLD + lb.WHITE) + "  "
              + lb.paint(verdicts[0][2], lb.CYAN))
    return 0


if __name__ == "__main__":
    sys.exit(main())
