#!/usr/bin/env python3
"""Live scoring engine -- proof of concept.

Reads an XCTrack .xctsk task and a zip of IGC tracklogs, replays them through
the engine, and renders the leaderboard.

  ./run.py                                 final leaderboard
  ./run.py --seconds 3600                  the board as it stood 1 h after the gate
  ./run.py --at 12:15                      the board at a wall-clock moment
  ./run.py --live --speed 300              animated replay
  ./run.py --check                         does this task match this data?
  ./run.py --bench                         engine throughput
  ./run.py --json out.json                 machine-readable result

Everything under engine/ is stdlib-only and knows nothing about IGC zips,
terminals or the clock. It is handed points and a `now`, and returns results.
"""

from __future__ import annotations

import argparse
import dataclasses
import datetime
import json
import math
import os
import sys
import time

import leaderboard as lb
from engine import comp as compcfg
from engine import parallel
from engine import rules
from engine.igc import load_tracks, parse_igc
from engine.score import project, score_pilot
from engine.scoring import score_task
from engine.task import parse_xctsk

DEFAULT_TASK = "Task.xctsk"
DEFAULT_IGC = "igcs"                      # directory, zip, or single .igc
DEFAULT_COMP = "competition.json"


def parse_clock(s: str, day: int) -> int:
    parts = [int(x) for x in s.rstrip("Z").split(":")]
    while len(parts) < 3:
        parts.append(0)
    return day + parts[0] * 3600 + parts[1] * 60 + parts[2]


OUTLIER_KM = 200.0


def _drop_outliers(tracks, results=None, dists=None) -> None:
    """Remove tracklogs that are not at this task's site at all. In place.

    A folder of IGCs collected by hand routinely picks up a file from another
    competition. Left in, it counts towards PilotsFlying and drags distance
    validity down for everyone. Excluded loudly, never silently.

    `dists` is supplied by the parallel path, which measured the distance in
    the worker because the fixes never reach this process.
    """
    if dists is None:
        dists = [min(math.hypot(f.x, f.y) for f in tr.fixes) / 1000.0
                 for tr in tracks]
    else:
        dists = [d / 1000.0 for d in dists]
    bad = [i for i, d in enumerate(dists) if d > OUTLIER_KM]
    if not bad:
        return
    for i in bad:
        print(lb.paint(f"  ⚠ excluded {tracks[i].pilot}: tracklog is "
                       f"{dists[i]:,.0f} km from this task", lb.ORANGE))
    keep = [i for i in range(len(tracks)) if i not in set(bad)]
    tracks[:] = [tracks[i] for i in keep]
    if results is not None:
        results[:] = [results[i] for i in keep]


def score_all(task, tracks, now, params, present=None):
    """State-machine every pilot, then apply GAP over the whole field."""
    out = []
    for tr in tracks:
        r = score_pilot(task, tr.fixes, now, params)
        r.pilot = tr.pilot
        out.append(r)
    ts = score_task(task, out, params, present)
    return out, ts


def start_analysis(task, tracks, day) -> list[str]:
    """What the field actually did at the SSS, versus what the task declares.

    A start gate and a crossing direction are two small fields that decide
    whether anybody scores at all, and both are routinely wrong in exported
    task files. The field's own behaviour settles it: a mass start produces a
    tight cluster of crossings in ONE direction within a minute or two, and
    nothing else in a flight looks like that.
    """
    from collections import Counter
    from engine.geo import zone_crossing

    sw = task.waypoints[task.start_index]
    ent, ext = [], []
    for tr in tracks:
        prev, e, x = None, None, None
        for f in tr.fixes:
            if prev is not None:
                c = zone_crossing((prev.x, prev.y, float(prev.t)), (f.x, f.y, float(f.t)),
                                  sw.x, sw.y, sw.inner, sw.outer)
                if c:
                    if not c[1] and e is None:
                        e = c[0]
                    if c[1] and e is not None and x is None:
                        x = c[0]
            prev = f
        if e:
            ent.append(e)
        if x:
            ext.append(x)

    out = []

    def peak(times):
        if not times:
            return None, 0, 0
        c = Counter(int(t - day) // 60 for t in times)
        m, n = c.most_common(1)[0]
        return m, n, len(times)

    e_min, e_n, e_tot = peak(ent)
    x_min, x_n, x_tot = peak(ext)
    hm = lambda m: f"{m//60:02d}:{m%60:02d}"

    out.append(f"SSS {sw.name} r{sw.raw_radius:.0f}m — declared "
               f"{task.start_direction} gate {lb.hhmmss(task.first_gate)}Z")
    if e_tot:
        out.append(f"  ENTER crossings: {e_tot} pilots, busiest minute {hm(e_min)}Z "
                   f"with {e_n} ({e_n/max(1,len(tracks))*100:.0f}% of the field)")
    if x_tot:
        out.append(f"  EXIT  crossings: {x_tot} pilots, busiest minute {hm(x_min)}Z "
                   f"with {x_n} ({x_n/max(1,len(tracks))*100:.0f}% of the field)")

    # The declared direction is advisory (S7F 6.2.1) and is not scored, so the
    # only thing worth checking here is the gate: a mass start shows up as a
    # tight cluster of crossings in one minute, whichever way they cross.
    best_min, best_n = ((e_min, e_n) if e_n >= x_n else (x_min, x_n))
    if best_n >= 0.5 * len(tracks):
        declared = int(task.first_gate - day) // 60
        if abs(best_min - declared) > 2:
            out.append(f"  ⚠ gate looks like {hm(best_min)}Z, not "
                       f"{lb.hhmmss(task.first_gate)}Z "
                       f"({best_n} of {len(tracks)} pilots crossed in that minute)")
            out.append(f"    try: --gate {hm(best_min)}")
        else:
            out.append(f"  ✓ declared gate matches the field: {best_n} of "
                       f"{len(tracks)} pilots crossed within a minute of it")
    out.append("  note: enter/exit is advisory only and is not scored (S7F 6.2.1)")
    return out


def cmd_check(task, tracks, day) -> int:
    """Does this task file describe the flight the tracklogs contain?

    A real operational check: wrong radius, wrong waypoint or the wrong task
    file entirely are all things that happen on a competition morning, and all
    of them look like "the engine is broken" if nothing tests for them.
    """
    print(lb.header(task, "task/data consistency", day))
    print()
    n = len(tracks)
    print("  " + lb.paint(f"{'#':>3} {'WAYPOINT':10} {'KIND':10} {'RADIUS':>8}  {'REACHED':>9}   {'MEDIAN CLOSEST':>14}", lb.GREY + lb.BOLD))
    print("  " + lb.paint("─" * 62, lb.GREY))

    bad = []
    for w in task.waypoints:
        dists = []
        hits = 0
        for tr in tracks:
            d = min(math.hypot(f.x - w.x, f.y - w.y) for f in tr.fixes)
            dists.append(d)
            if d <= w.radius:
                hits += 1
        dists.sort()
        med = dists[len(dists) // 2]
        frac = hits / n
        col = lb.GREEN if frac >= 0.5 else (lb.YELLOW if frac > 0.05 else lb.RED)
        if w.kind in ("SSS", "TAKEOFF") and frac < 0.5:
            bad.append(w)
        elif frac <= 0.05:
            bad.append(w)
        print(
            "  "
            + lb.paint(f"{w.index:>3} {w.name:10} {w.kind:10} {w.raw_radius:>7.0f}m  ", lb.GREY)
            + lb.paint(f"{hits:>3}/{n} {frac*100:>4.0f}%", col)
            + lb.paint(f"   {med/1000:>11.2f} km", lb.GREY)
        )

    print()
    print("  " + lb.paint("START ANALYSIS", lb.BOLD + lb.WHITE))
    print("  " + lb.paint("─" * 62, lb.GREY))
    for line in start_analysis(task, tracks, day):
        col = lb.RED if line.strip().startswith("⚠") else (
            lb.GREEN if line.strip().startswith("✓") else lb.GREY)
        print("  " + lb.paint(line, col))

    print()
    if bad:
        print("  " + lb.paint("✗ TASK AND TRACKLOGS DO NOT MATCH", lb.RED + lb.BOLD))
        print("  " + lb.paint(f"  {len(bad)} waypoint(s) essentially untouched: "
                              + ", ".join(w.name for w in bad), lb.GREY))
        print("  " + lb.paint("  These tracklogs are from a different task.", lb.GREY))
        return 1
    print("  " + lb.paint("✓ task and tracklogs are consistent", lb.GREEN + lb.BOLD))
    return 0


def cmd_rules() -> int:
    """Print the scoring pipeline: every rule, its file, and its status.

    The point of the list is the third column. "Implemented" is not the same
    claim as "verified", and neither is the same as "matches a published
    result" -- so each row says which of those it has earned.
    """
    from engine import rules as R

    colour = {R.IMPLEMENTED: lb.GREEN, R.MISSING: lb.RED, R.NA_PG: lb.GREY,
              "SUSPECT": lb.YELLOW + lb.BOLD, "KNOWN GAP": lb.YELLOW + lb.BOLD,
              "assumption": lb.YELLOW, "none [PG]": lb.GREY,
              "WRONG": lb.RED + lb.BOLD, "REFERENCE": lb.CYAN,
              "OPEN QUESTION": lb.YELLOW + lb.BOLD,
              "NOT WIRED IN": lb.YELLOW}

    def table(rows, prefix):
        print("   " + lb.paint(f"{'#':<5}{'S7F':<11}{'RULE':<32}{'STATUS':<21}FILE",
                               lb.GREY))
        for r in rows:
            print("   " + lb.paint(f"{prefix}{r.step:<4}", lb.GREY)
                  + lb.paint(f"{r.ref:<11}", lb.CYAN)
                  + lb.paint(f"{r.title[:31]:<32}", lb.WHITE)
                  + lb.paint(f"{r.status:<21}", colour.get(r.status, lb.WHITE))
                  + lb.paint(r.module, lb.DIM + lb.GREY))

    print()
    print("  " + lb.paint("SCORING PIPELINE — FAI Sporting Code Section 7F, "
                          "2026 V1.0, paragliding", lb.BOLD + lb.WHITE))
    print("  " + lb.paint("─" * 96, lb.GREY))
    print()
    print("  " + lb.paint("A. GEOMETRY AND DISTANCE — what the points formulas "
                          "measure", lb.BOLD + lb.WHITE))
    print("  " + lb.paint("Not points rules, but every scored distance is built "
                          "on them, so an error here is", lb.DIM + lb.GREY))
    print("  " + lb.paint("uniform and invisible. 'unmapped' means I do not have "
                          "the Code text that specifies", lb.DIM + lb.GREY))
    print("  " + lb.paint("that algorithm and will not guess a section number.",
                          lb.DIM + lb.GREY))
    table(R.ALGORITHMS, "A")
    print()
    print("  " + lb.paint("B. SCORING — once the whole field is known",
                          lb.BOLD + lb.WHITE))
    print("  " + lb.paint("Per-pilot state (takeoff, start, control zones, "
                          "distance, speed section) comes first,", lb.DIM + lb.GREY))
    print("  " + lb.paint("from each pilot's own points alone: engine/score.py.",
                          lb.DIM + lb.GREY))
    table(R.STAGES, "")
    print()
    print("  " + lb.paint("VERIFICATION STATUS, element by element",
                          lb.BOLD + lb.WHITE))
    print("  " + lb.paint("─" * 96, lb.GREY))
    for r in R.ALL:
        head = lb.paint(f"{r.ref} {r.title}", lb.BOLD + lb.WHITE)
        print("   " + head + "   " + lb.paint(r.status, colour.get(r.status, lb.WHITE)))
        for line in _wrap_text(r.verified, 88):
            print("      " + lb.paint(line, lb.GREY))
        if r.pg:
            for line in _wrap_text("[PG] " + r.pg, 88):
                print("      " + lb.paint(line, lb.DIM + lb.CYAN))
        print()
    bad = [r for r in R.ALL
           if r.status in (R.MISSING, "SUSPECT", "KNOWN GAP", "WRONG",
                           "OPEN QUESTION", "NOT WIRED IN")]
    # sanity: every row must be findable by its own reference
    assert all(R.by_ref(r.ref) is not None for r in R.ALL)
    print("  " + lb.paint(f"{len(R.ALL) - len(bad)} of {len(R.ALL)} elements "
                          f"implemented and checked; {len(bad)} not:",
                          lb.BOLD + lb.WHITE))
    for r in bad:
        print("    " + lb.paint(f"{r.ref:<9}{r.title:<28}{r.status}",
                                colour.get(r.status, lb.WHITE)))
    print()
    print("  " + lb.paint("Open each file next to the Code — that is what they are "
                          "written for.", lb.DIM + lb.GREY))
    print("  " + lb.paint("VERIFICATION.md has the evidence behind every status "
                          "above.", lb.DIM + lb.GREY))
    return 0


def _wrap_text(s: str, w: int) -> list[str]:
    out, cur = [], ""
    for word in s.split():
        if len(cur) + len(word) + 1 > w:
            out.append(cur)
            cur = word
        else:
            cur = f"{cur} {word}".strip()
    if cur:
        out.append(cur)
    return out


def cmd_verify(task=None, tracks=None, results=None, ts=None, params=None,
               now=None, igc_path=None, task_path=None) -> int:
    """Run the whole test suite. The tests live in tests/, not here.

    They used to live in this function, which meant the only way to run them
    was to run the scorer. They are now one module per Sporting Code section
    under tests/, driven by the registry in tests/__init__.py, and
    `python3 -m tests` runs the identical set. This is a second way in, not a
    second suite.

    With no tracklogs the field-invariant suite is skipped and everything else
    still runs, so `--verify` is useful without any data at all.
    """
    import tests as T

    ctx = {}
    if task is not None and results:
        ctx = dict(task=task, tracks=tracks, results=results, ts=ts,
                   params=params, now=now, igc_path=igc_path,
                   task_path=task_path)

    print(lb.paint("\n  FAI SPORTING CODE S7F 2026 V1.0 · PARAGLIDING — "
                   "TEST SUITE", lb.BOLD + lb.WHITE))
    print("  " + lb.paint("─" * 92, lb.GREY))
    print("  " + lb.paint("Every check prints its detail whether it passes or "
                          "not: a green tick tells you", lb.DIM + lb.GREY))
    print("  " + lb.paint("nothing, and several numbers that turned out to be "
                          "wrong sat in a passing", lb.DIM + lb.GREY))
    print("  " + lb.paint("check's detail line for a long time before anyone "
                          "read them.", lb.DIM + lb.GREY))

    total = bad = 0
    for suite, checks in T.run_all(**ctx):
        nbad = sum(1 for c in checks if not c[1])
        total += len(checks)
        bad += nbad
        print()
        print("  " + lb.paint(f"{suite.ref:<10}{suite.title}", lb.BOLD + lb.WHITE)
              + lb.paint(f"   {len(checks) - nbad}/{len(checks)}",
                         lb.GREEN if not nbad else lb.RED))
        print("  " + lb.paint("─" * 92, lb.GREY))
        for name, ok, detail in checks:
            mark = lb.paint("✓", lb.GREEN) if ok else lb.paint("✗", lb.RED)
            print(f"  {mark} " + lb.paint(name, lb.WHITE if ok else lb.RED))
            if detail:
                print("      " + lb.paint(detail, lb.DIM + lb.GREY))

    if not ctx:
        print()
        print("  " + lb.paint("The field-invariant suite was skipped. To include "
                              "it:", lb.YELLOW))
        print("  " + lb.paint("    ./run.py --verify --igc igcs --gate HH:MM",
                              lb.GREY))
    print()
    if bad:
        print("  " + lb.paint(f"✗ {bad} of {total} checks FAILED", lb.RED + lb.BOLD))
        _print_gaps()
        return 1
    print("  " + lb.paint(f"✓ all {total} checks pass", lb.GREEN + lb.BOLD))
    _print_gaps()
    return 0


def _print_gaps() -> None:
    """What passing the suite does NOT mean, read off the rule registry.

    Generated rather than written out, because a hand-maintained "not covered"
    line goes stale the moment a rule is implemented — this one listed
    penalties as missing for exactly as long as it took to notice.
    """
    from engine import rules as R

    gaps = [r for r in R.ALL
            if r.status in (R.MISSING, "SUSPECT", "KNOWN GAP", "WRONG",
                            "OPEN QUESTION", "NOT WIRED IN")]
    if not gaps:
        return
    print("  " + lb.paint("Passing does not cover:", lb.DIM + lb.GREY))
    for r in gaps:
        print("    " + lb.paint(f"{r.ref:<11}", lb.CYAN)
              + lb.paint(f"{r.title[:38]:<40}", lb.DIM + lb.GREY)
              + lb.paint(r.status, lb.RED if r.status == R.MISSING
                         else lb.YELLOW + lb.BOLD))
    print("  " + lb.paint("./run.py --rules  for why, file by file.",
                          lb.DIM + lb.GREY))


def cmd_bench(task, tracks, params, igc_path=None) -> int:
    nfix = sum(len(t.fixes) for t in tracks)
    runs = []
    for _ in range(5):
        t0 = time.perf_counter()
        score_all(task, tracks, 1e18, params)
        runs.append(time.perf_counter() - t0)
    runs.sort()
    best, med = runs[0], runs[len(runs) // 2]

    print(lb.paint("\n  ENGINE THROUGHPUT", lb.BOLD + lb.WHITE))
    print("  " + lb.paint("─" * 62, lb.GREY))
    rows = [
        ("pilots", f"{len(tracks)}"),
        ("fixes", f"{nfix:,}"),
        ("full recompute, all pilots", f"{med*1000:.0f} ms  (best {best*1000:.0f} ms)"),
        ("throughput", f"{nfix/med:,.0f} fixes/sec"),
        ("per fix", f"{med/nfix*1e6:.2f} µs"),
        ("per-pilot recompute (mean)", f"{med/len(tracks)*1000:.0f} ms"),
    ]
    for k, v in rows:
        print("  " + lb.paint(f"{k:<32}", lb.GREY) + lb.paint(v, lb.WHITE))

    per_fix = med / nfix
    live = 150 * per_fix
    print()
    print("  " + lb.paint("PROJECTED AT DESIGN SCALE (150 pilots, 1 Hz)", lb.BOLD + lb.WHITE))
    print("  " + lb.paint("─" * 62, lb.GREY))
    print("  " + lb.paint(f"{'incremental load':<32}", lb.GREY)
          + lb.paint(f"{live*1000:.2f} ms per second of wall clock  =  {live*100:.2f}% of one core", lb.GREEN + lb.BOLD))
    longest = max(len(t.fixes) for t in tracks)
    print("  " + lb.paint(f"{'worst-case backfill recompute':<32}", lb.GREY)
          + lb.paint(f"{longest*per_fix*1000:.0f} ms  ({longest:,}-fix track)", lb.WHITE))
    print("  " + lb.paint(f"{'point storage, 150 pilots × 4 h':<32}", lb.GREY)
          + lb.paint(f"{150*4*3600*32/1e6:.0f} MB", lb.WHITE))
    # --- the number a scorer actually waits on ---------------------------
    # Everything above times the scoring loop with the points already parsed
    # and projected, which is the live case. The cold case -- last tracklog
    # uploaded, nothing in memory, publish the final board -- also has to read
    # and parse 40 MB of IGC, and that is what the second below measures.
    if igc_path and parallel.usable(igc_path):
        import subprocess
        print()
        print("  " + lb.paint("COLD FULL-FIELD PUBLISH — process start to printed board",
                              lb.BOLD + lb.WHITE))
        print("  " + lb.paint("─" * 62, lb.GREY))
        argv0 = [sys.executable, os.path.abspath(__file__), "--igc", igc_path,
                 "--no-color", "--top", "1"]
        gate = int(task.first_gate % 86400)
        argv0 += ["--gate", f"{gate//3600:02d}:{gate%3600//60:02d}"]
        for label, extra in (("all cores", []), ("single process", ["--serial"])):
            runs = []
            for _ in range(3):
                t0 = time.perf_counter()
                subprocess.run(argv0 + extra, stdout=subprocess.DEVNULL,
                               stderr=subprocess.DEVNULL, check=False)
                runs.append(time.perf_counter() - t0)
            runs.sort()
            col = lb.GREEN + lb.BOLD if runs[0] < 1.0 else lb.WHITE
            print("  " + lb.paint(f"{label:<32}", lb.GREY)
                  + lb.paint(f"{runs[0]*1000:.0f} ms  (median {runs[1]*1000:.0f} ms)", col))
        print("  " + lb.paint(f"{'cores':<32}", lb.GREY)
              + lb.paint(f"{os.cpu_count()}", lb.WHITE))

    print()
    print("  " + lb.paint("The engine is not the bottleneck. Fan-out is (DESIGN.md §15).", lb.DIM + lb.GREY))
    return 0


def cmd_explain(args, task, tracks, results, ts, params, now, competition) -> int:
    """Full audit trail for one pilot.

    Scoring has already run over the WHOLE field at this point, and it has to
    have: a pilot's points are a function of everyone else's flights, so an
    audit that scored one pilot in isolation would show different numbers from
    the leaderboard and be worse than useless (DESIGN.md §11.4).
    """
    import explain
    from engine import audit as au

    ranked = sorted(results, key=lambda r: r.rank_key)
    by_name = {r.pilot: r for r in results}
    tracks_by_name = {t.pilot: t for t in tracks}

    want = args.explain.strip()
    match = None
    if want in by_name:
        match = want
    else:
        low = want.lower()
        hits = [n for n in by_name if low in n.lower()]
        if not hits:
            # allow naming the IGC file instead of the pilot
            hits = [t.pilot for t in tracks
                    if any(low in f.lower() for f in t.source_files)]
        if len(hits) == 1:
            match = hits[0]
        elif len(hits) > 1:
            print(lb.paint(f"  '{want}' matches {len(hits)} pilots:", lb.YELLOW))
            for n in sorted(hits)[:20]:
                print("    " + lb.paint(n, lb.WHITE))
            return 2
    if match is None:
        print(lb.paint(f"  no pilot matching '{want}'", lb.RED), file=sys.stderr)
        print(lb.paint(f"  {len(results)} pilots loaded; try a surname, or an IGC "
                       f"filename", lb.GREY), file=sys.stderr)
        return 2

    r = by_name[match]
    track = tracks_by_name[match]

    # The parallel path leaves the fixes in the worker that scored them, so the
    # audit has to read this one tracklog back. That is not a workaround: an
    # audit that replays from the FILE, rather than from whatever happens to be
    # in memory, is a stronger claim — it demonstrates the score is reproducible
    # from the stored points alone. Cheap, too: one file, not 129.
    if not track.fixes and track.source_files:
        base = args.igc if os.path.isdir(args.igc) else os.path.dirname(args.igc)
        fixes = []
        for name in track.source_files:
            with open(os.path.join(base, name), "rb") as fh:
                fixes.extend(parse_igc(fh.read(), name)[2])
        fixes.sort(key=lambda f: f.t)
        deduped, last = [], -1
        for f in fixes:
            if f.t != last:
                deduped.append(f)
                last = f.t
        track.fixes = deduped
        project(task, track.fixes)

    rec = au.audit_pilot(
        task, track, r, ts, params, now,
        task_path=args.task, comp_path=args.comp,
        igc_dir=args.igc if os.path.isdir(args.igc) else "",
        rank=ranked.index(r) + 1, field_size=len(results),
    )
    print(explain.render(rec, task, args.explain_fixes))

    if args.json:
        with open(args.json, "w") as fh:
            json.dump(_audit_json(rec, task), fh, indent=2, default=str)
        print("  " + lb.paint(f"audit record written to {args.json}", lb.GREY))
    return 0 if rec["replay_agrees"] else 1


def _audit_json(rec: dict, task) -> dict:
    """The same record, with the dataclasses flattened for JSON."""
    out = dict(rec)
    out["result"] = dataclasses.asdict(rec["result"])
    out["task"] = {
        "name": task.name, "hash": task.task_hash,
        "start_type": task.start_type, "gates": task.gates,
        "deadline": task.goal_deadline,
        "total_distance_m": task.total_distance,
        "speed_distance_m": task.speed_distance,
        "centre_distance_m": task.centre_distance,
        "waypoints": [
            {"index": w.index, "name": w.name, "kind": w.kind,
             "lat": w.lat, "lon": w.lon, "radius_m": w.raw_radius,
             "inner_m": w.inner, "outer_m": w.outer, "alt_m": w.alt}
            for w in task.waypoints
        ],
    }
    return out


def render(task, results, ts, now, day, comp, top, reconstructed, competition, final) -> str:
    return "\n".join([
        lb.header(task, comp, day, reconstructed),
        "",
        lb.gapline(ts, competition, final),
        "",
        lb.clockline(task, results, now, final),
        "",
        lb.table(task, results, now, top, final),
        "",
        lb.legend(),
    ])


def main() -> int:
    ap = argparse.ArgumentParser(description="Paragliding live scoring engine (POC)")
    ap.add_argument("--task", default=DEFAULT_TASK)
    ap.add_argument("--igc", default=DEFAULT_IGC)
    ap.add_argument("--seconds", type=float, metavar="N",
                    help="score only the first N seconds after the start gate")
    ap.add_argument("--at", metavar="HH:MM[:SS]", help="score as at this UTC time")
    ap.add_argument("--live", action="store_true", help="animated replay")
    ap.add_argument("--speed", type=float, default=200.0, help="replay speed for --live")
    ap.add_argument("--step", type=float, default=30.0, help="seconds of task time per frame")
    ap.add_argument("--top", type=int, default=0, help="show only the top N")
    ap.add_argument("--check", action="store_true", help="verify task matches tracklogs")
    ap.add_argument("--bench", action="store_true", help="engine throughput")
    ap.add_argument("--workers", type=int, default=0, metavar="N",
                    help="processes for the cold full-field score "
                         "(0 = one per core, 1 = serial)")
    ap.add_argument("--serial", action="store_true",
                    help="force the single-process path (same result, slower)")
    ap.add_argument("--explain", metavar="PILOT",
                    help="full audit trail for one pilot (name, substring, or IGC file)")
    ap.add_argument("--explain-fixes", type=int, default=0, metavar="N",
                    help="with --explain, dump N raw fixes around each event")
    ap.add_argument("--penalties", default="penalties.json", metavar="FILE",
                    help="S7F 13.5 penalties (default penalties.json if present)")
    ap.add_argument("--compare", metavar="FILE",
                    help="diff this result against an official published one (TSV)")
    ap.add_argument("--tz", type=float, default=0.0, metavar="H",
                    help="hours the --compare file's times are ahead of UTC")
    ap.add_argument("--rules", action="store_true",
                    help="list every scoring rule, its file, and its status")
    ap.add_argument("--verify", action="store_true",
                    help="check GAP implementation against published S7F values")
    ap.add_argument("--json", metavar="FILE", help="write results as JSON")
    ap.add_argument("--no-color", action="store_true")
    g = ap.add_argument_group("competition parameters (S7F 5) — normally from --comp")
    g.add_argument("--comp", default=DEFAULT_COMP, metavar="FILE",
                   help=f"competition config (default {DEFAULT_COMP})")
    g.add_argument("--nominal-distance", type=float, metavar="KM", help="override config")
    g.add_argument("--min-distance", type=float, metavar="KM", help="override config")
    g.add_argument("--nominal-time", type=float, metavar="MIN", help="override config")
    g.add_argument("--leading-time-ratio", type=float, metavar="PCT", help="override config")
    g.add_argument("--present", type=int, metavar="N", help="override config")
    g.add_argument("--gate", metavar="HH:MM[:SS]",
                   help="override the task's start gate (UTC)")
    g.add_argument("--deadline", metavar="HH:MM[:SS]",
                   help="override the task's goal deadline (UTC)")
    g.add_argument("--elevated-goal", type=float, nargs="?", const=300.0, metavar="M",
                   help="goal is elevated (S7F 13.1); optional elevation band, default 300 m")
    args = ap.parse_args()

    lb.init_color(False if args.no_color else None)

    # --verify with no tracklogs is the fast path: formulas and synthetic
    # flights only, no I/O. With tracklogs it additionally asserts the
    # invariants over that real field, which is the part that catches an
    # assembly bug rather than a formula bug.
    if args.rules:
        return cmd_rules()

    if args.verify and not os.path.exists(args.igc):
        return cmd_verify()

    # --- competition configuration -------------------------------------
    try:
        if os.path.exists(args.comp):
            competition = compcfg.load(args.comp)
        else:
            competition = compcfg.default_competition()
            competition.source = f"<defaults — {args.comp} not found>"
    except (ValueError, json.JSONDecodeError) as e:
        print(lb.paint(f"config error: {e}", lb.RED), file=sys.stderr)
        return 2

    t0 = time.perf_counter()
    if not os.path.exists(args.igc):
        print(lb.paint(f"tracklogs not found: {args.igc}", lb.RED), file=sys.stderr)
        print(lb.paint("  pass --igc <directory|zip|file>", lb.GREY), file=sys.stderr)
        return 2

    def _secs(v):
        if not v:
            return None
        parts = [int(x) for x in v.rstrip("Z").split(":")]
        while len(parts) < 3:
            parts.append(0)
        return parts[0] * 3600 + parts[1] * 60 + parts[2]

    # One publish of a finished competition -- the case a scorer actually
    # waits on -- goes across every core. Everything else keeps the fixes
    # resident in this process because it needs them: --live re-scores the
    # same points hundreds of times, --check and --verify inspect them, and
    # --bench times the scoring loop in isolation.
    parallel_ok = (parallel.usable(args.igc) and not args.serial
                   and args.workers != 1 and not (args.live or args.check
                                                  or args.bench or args.verify))

    if parallel_ok:
        paths = parallel.igc_paths(args.igc)
        if not paths:
            print("no tracklogs found", file=sys.stderr)
            return 2
        groups, day = parallel.scan_headers(paths)
    else:
        tracks, day = load_tracks(args.igc)

    task = parse_xctsk(args.task, day, _secs(args.gate), _secs(args.deadline),
                       elevated_goal=args.elevated_goal)
    for w in task.warnings:
        print(lb.paint(f"  ⚠ task file: {w}", lb.RED))
    if task.warnings and not args.gate:
        print(lb.paint("    override with --gate HH:MM (see ./run.py --help)", lb.GREY))
    if not parallel_ok:
        for tr in tracks:
            project(task, tr.fixes)
    load_ms = (time.perf_counter() - t0) * 1000

    if not parallel_ok and not tracks:
        print("no tracklogs found", file=sys.stderr)
        return 2

    # per-task overrides (S7F 11 sets LeadingTimeRatio per task), then CLI
    competition = competition.for_task(task.name)
    overrides = {}
    if args.nominal_distance is not None:
        overrides["nominal_distance"] = args.nominal_distance * 1000.0
    if args.min_distance is not None:
        overrides["minimum_distance"] = args.min_distance * 1000.0
    if args.nominal_time is not None:
        overrides["nominal_time"] = args.nominal_time * 60.0
    if args.leading_time_ratio is not None:
        overrides["leading_time_ratio"] = args.leading_time_ratio / 100.0
    if overrides:
        competition.params = dataclasses.replace(competition.params, **overrides)
        cli_set = {"nominal_distance": "nominal_distance_km",
                   "minimum_distance": "minimum_distance_km",
                   "nominal_time": "nominal_time_min"}
        competition._declared_placeholders = [
            k for k in competition._declared_placeholders
            if k not in {cli_set[o] for o in overrides if o in cli_set}]
    # S7F 13.1 is a per-task property that is not in the .xctsk, so it comes
    # from the competition config -- but the task is compiled before the config
    # is narrowed to this task, so apply it here. The CLI flag still wins.
    if args.elevated_goal is None and competition.elevated_goal_m:
        task.goal_elevated = True
        task.goal_elevation = min(1000.0, float(competition.elevated_goal_m))

    params = competition.params
    for w in competition.warnings:
        print(lb.paint(f"  ⚠ config: {w}", lb.YELLOW))
    present = args.present if args.present is not None else competition.pilots_present

    comp = competition.name
    reconstructed = "RECONSTRUCT" in args.task.upper()

    if not parallel_ok:
        _drop_outliers(tracks)
        data_end = max(t.fixes[-1].t for t in tracks)
        if args.check:
            return cmd_check(task, tracks, day)
        if args.bench:
            return cmd_bench(task, tracks, params, args.igc)
        if args.verify:
            now_v = max(data_end, task.goal_deadline or 0)
            res_v, ts_v = score_all(task, tracks, now_v, params, present)
            return cmd_verify(task, tracks, res_v, ts_v, params, now_v,
                              args.igc, args.task)

    # --- the replay clock ------------------------------------------------
    # No time flag  -> the competition is over: score everything.  FINAL.
    # --seconds/--at -> simulate that instant mid-task.              PROVISIONAL.

    if args.at:
        now = parse_clock(args.at, day)
    elif args.seconds is not None:
        now = task.first_gate + args.seconds
    else:
        # FINAL. `now` is only ever used as "stop at the first fix after this",
        # so any value at or past the last fix gives the identical result --
        # which is what lets the parallel path score before it knows where the
        # data ends. The displayed clock is recovered from the results below.
        now = float("inf") if parallel_ok else max(data_end,
                                                   task.goal_deadline or 0)
    simulating = args.at is not None or args.seconds is not None
    final = not simulating

    if args.live:
        start = task.first_gate
        try:
            print("\033[?25l", end="")
            t_wall = time.perf_counter()
            clock = start
            while clock <= data_end:
                results, ts = score_all(task, tracks, clock, params, present)
                sys.stdout.write("\033[H\033[J" + render(
                    task, results, ts, clock, day, comp, args.top, reconstructed,
                    competition, False) + "\n")
                sys.stdout.flush()
                clock += args.step
                t_wall += args.step / args.speed
                d = t_wall - time.perf_counter()
                if d > 0:
                    time.sleep(d)
            results, ts = score_all(task, tracks, data_end, params, present)
            sys.stdout.write("\033[H\033[J" + render(
                task, results, ts, data_end, day, comp, args.top, reconstructed,
                competition, True) + "\n")
        except KeyboardInterrupt:
            pass
        finally:
            print("\033[?25h", end="")
        return 0

    t1 = time.perf_counter()
    if parallel_ok:
        workers = args.workers or None
        results, tracks, dists = parallel.score_field(task, params, now, groups,
                                                      workers)
        if not results:
            print("no tracklogs found", file=sys.stderr)
            return 2
        _drop_outliers(tracks, results, dists)
        data_end = max(r.last_t for r in results)
        if final:
            now = max(data_end, task.goal_deadline or 0)
        ts = score_task(task, results, params, present)
    else:
        results, ts = score_all(task, tracks, now, params, present)

    # S7F 13.5 — step 16, and deliberately the last thing to touch a total.
    # Left out of score_task() because it needs the tracks (to match pilot IDs)
    # and because an unmatched penalty has to reach a human rather than be
    # swallowed by a pure function.
    pens = rules.load_penalties(args.penalties, task.name)
    if pens:
        unmatched = rules.apply_penalties(results, tracks, pens,
                                          1000.0 * ts.task_validity)
        for pen in pens:
            if pen.applied:
                who = next((r.pilot for r in results if pen in r.penalties), pen.pilot)
                print("  " + lb.paint(f"⚠ penalty  {who}: {pen.describe()}  "
                                      f"= −{pen.applied:.1f} pt  (S7F 13.5)",
                                      lb.ORANGE))
        for u in unmatched:
            print("  " + lb.paint(f"✗ penalty names a pilot not in the field: {u}",
                                  lb.RED + lb.BOLD))
    score_ms = (time.perf_counter() - t1) * 1000

    if args.compare:
        import compare as cmp_mod
        rows = cmp_mod.load(args.compare)
        text, failed = cmp_mod.render(task, results, ts, tracks, rows,
                                      args.tz, day, args.compare)
        print(text)
        return 1 if failed else 0

    if args.explain:
        return cmd_explain(args, task, tracks, results, ts, params, now, competition)

    print(render(task, results, ts, now, day, comp, args.top, reconstructed,
                 competition, final))
    nfix = sum(r.fixes_used for r in results)
    print("  " + lb.paint(
        f"{len(tracks)} pilots · {nfix:,} fixes scored in {score_ms:.0f} ms "
        f"({nfix/max(score_ms,1e-9)*1000:,.0f} fixes/sec) · load {load_ms:.0f} ms",
        lb.DIM + lb.GREY))

    if not any(r.start_time for r in results):
        print()
        print("  " + lb.paint("⚠ no pilot made a valid start — run  ./run.py --check", lb.YELLOW + lb.BOLD))

    if args.json:
        payload = {
            "rules": "FAI Sporting Code S7F 2026 V1.0, paragliding",
            "task": {"name": task.name, "hash": task.task_hash,
                     "optimised_km": task.total_distance / 1000,
                     "speed_section_km": task.speed_distance / 1000,
                     "centre_km": task.centre_distance / 1000},
            "competition": {"name": competition.name, "class": competition.glider_class,
                            "discipline": competition.discipline,
                            "config": competition.source,
                            "placeholders": competition.placeholders()},
            "final": final,
            "parameters": {"nominal_distance_km": params.nominal_distance / 1000,
                           "minimum_distance_km": params.minimum_distance / 1000,
                           "nominal_time_min": params.nominal_time / 60,
                           "leading_time_ratio": params.leading_time_ratio},
            "validity": {"launch": ts.launch_validity, "distance": ts.distance_validity,
                         "time": ts.time_validity, "task": ts.task_validity},
            "allocation": {"goal_ratio": ts.alloc.goal_ratio,
                           "available_distance": ts.alloc.available_distance,
                           "available_time": ts.alloc.available_time,
                           "available_leading": ts.alloc.available_leading},
            "now": now,
            "results": [
                {"rank": i, "pilot": r.pilot, "state": r.state,
                 "distance_km": round(r.distance / 1000, 3),
                 "turnpoints": r.tp_count,
                 "start": r.start_time, "ess": r.ess_time, "goal": r.goal_time,
                 "speed_kmh": r.speed, "lc": r.lc,
                 "distance_points": round(r.distance_points, 1),
                 "time_points": round(r.time_points, 1),
                 "leading_points": round(r.leading_points, 1),
                 "total_points": r.total_points,
                 "early_start": r.early_start, "last_fix": r.last_t}
                for i, r in enumerate(sorted(results, key=lambda x: x.rank_key), 1)
            ],
        }
        with open(args.json, "w") as f:
            json.dump(payload, f, indent=2)
        print("  " + lb.paint(f"wrote {args.json}", lb.GREY))
    return 0


if __name__ == "__main__":
    sys.exit(main())
