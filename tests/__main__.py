"""python3 -m tests — run the suite on its own.

    python3 -m tests
    python3 -m tests 12 13
    python3 -m tests --list
    python3 -m tests --igc igcs --gate 11:30 --deadline 15:00

The last form adds the suites that need a real scored field. `./run.py --verify`
runs the identical registry, so there is one suite and two ways in.
"""

from __future__ import annotations

import argparse
import sys

import leaderboard as lb
from tests import SUITES, run_suite


def _field_context(args):
    """Load and score a real field, for the suites that need one."""
    from engine import comp as compcfg
    from engine.igc import load_tracks
    from engine.score import project
    from engine.scoring import score_task
    from engine.task import parse_xctsk

    def secs(v):
        if not v:
            return None
        parts = [int(x) for x in v.rstrip("Z").split(":")]
        while len(parts) < 3:
            parts.append(0)
        return parts[0] * 3600 + parts[1] * 60 + parts[2]

    tracks, day = load_tracks(args.igc)
    task = parse_xctsk(args.task, day, secs(args.gate), secs(args.deadline))
    competition = compcfg.load(args.comp).for_task(task.name)
    if competition.elevated_goal_m:
        task.goal_elevated = True
        task.goal_elevation = min(1000.0, float(competition.elevated_goal_m))
    for tr in tracks:
        project(task, tr.fixes)
    from run import score_all
    now = max(max(t.fixes[-1].t for t in tracks), task.goal_deadline or 0)
    results, ts = score_all(task, tracks, now, competition.params,
                            competition.pilots_present)
    return dict(task=task, tracks=tracks, results=results, ts=ts,
                params=competition.params, now=now, igc_path=args.igc,
                task_path=args.task)


def main() -> int:
    ap = argparse.ArgumentParser(prog="python3 -m tests",
                                 description="FAI S7F scoring test suite")
    ap.add_argument("keys", nargs="*", help="suites to run (default: all)")
    ap.add_argument("--list", action="store_true", help="list the suites")
    ap.add_argument("--igc", help="tracklogs, to enable the field suites")
    ap.add_argument("--task", default="Task.xctsk")
    ap.add_argument("--comp", default="competition.json")
    ap.add_argument("--gate", help="override the start gate, UTC")
    ap.add_argument("--deadline", help="override the goal deadline, UTC")
    ap.add_argument("--quiet", action="store_true",
                    help="only show failures and the summary")
    ap.add_argument("--no-color", action="store_true")
    args = ap.parse_args()
    lb.init_color(False if args.no_color else None)

    if args.list:
        print()
        for s in SUITES:
            need = "  (needs --igc)" if s.needs_field else ""
            print("  " + lb.paint(f"{s.key:<10}", lb.CYAN)
                  + lb.paint(f"{s.ref:<10}", lb.GREY)
                  + lb.paint(f"{s.title}{need}", lb.WHITE))
        print()
        return 0

    ctx = _field_context(args) if args.igc else {}
    if not args.igc:
        print("  " + lb.paint("no --igc: the field-invariant suite is skipped",
                              lb.DIM + lb.GREY))

    total = failed = 0
    for s in SUITES:
        if args.keys and s.key not in args.keys:
            continue
        if s.needs_field and not ctx:
            continue
        checks = run_suite(s, **ctx)
        bad = [c for c in checks if not c[1]]
        total += len(checks)
        failed += len(bad)
        head = lb.GREEN if not bad else lb.RED
        print()
        print("  " + lb.paint(f"{s.ref:<10}{s.title}", lb.BOLD + lb.WHITE)
              + lb.paint(f"   {len(checks) - len(bad)}/{len(checks)}", head))
        print("  " + lb.paint("─" * 92, lb.GREY))
        for name, ok, detail in checks:
            if ok and args.quiet:
                continue
            mark = lb.paint("✓", lb.GREEN) if ok else lb.paint("✗", lb.RED)
            print(f"  {mark} " + lb.paint(name, lb.WHITE if ok else lb.RED))
            if detail:
                print("      " + lb.paint(detail, lb.DIM + lb.GREY))

    print()
    if failed:
        print("  " + lb.paint(f"✗ {failed} of {total} checks FAILED",
                              lb.RED + lb.BOLD))
    else:
        print("  " + lb.paint(f"✓ all {total} checks pass", lb.GREEN + lb.BOLD))
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
