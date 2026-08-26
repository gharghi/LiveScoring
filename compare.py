"""Compare this engine against an officially published result, pilot by pilot.

This is the only check in the project that reaches outside it. Everything in
`--verify` is either the rulebook (formula checks) or the engine agreeing with
itself (invariants, reference-vs-optimised equalities). Neither can catch a
rule that is uniformly misread — and VERIFICATION.md §6 named this as the
highest-value verification work remaining, specifically for the leading
coefficient, which has no published worked example.

An official table is not ground truth in a strict sense: it is another
implementation (usually FS or Airscore), run by a scorer, possibly with manual
adjustments and penalties applied afterwards. A disagreement means one of us is
wrong, and the point of this tool is to say *which quantity* disagrees and by
how much, so the argument can be about a specific number instead of a total.

The reference file is TSV, `#` for comments, matched on the pilot ID, which is
also the IGC filename:

    rank  id  name  start  finish  time  height_m  speed_kmh  distance_km
          distP  leadP  spdP  lowP  score

`-` means the column is empty (a pilot who did not reach goal has no time).
Times are whatever the official table uses; `--tz` states the offset from UTC.
"""

from __future__ import annotations

import math
import re

import leaderboard as lb


class Row:
    __slots__ = ("rank", "pid", "name", "start", "finish", "time", "height",
                 "speed", "distance", "dist_p", "lead_p", "spd_p", "low_p", "score")


def _num(s):
    s = s.strip()
    if s in ("", "-", "(-)"):
        return None
    try:
        return float(s)
    except ValueError:
        return None


def _clock(s):
    """HH:MM:SS -> seconds since midnight, or None."""
    s = s.strip()
    if not re.fullmatch(r"\d{1,2}:\d{2}:\d{2}", s):
        return None
    h, m, sec = (int(x) for x in s.split(":"))
    return h * 3600 + m * 60 + sec


def load(path: str) -> list[Row]:
    rows = []
    with open(path, encoding="utf-8") as fh:
        for line in fh:
            if line.startswith("#") or not line.strip():
                continue
            f = line.rstrip("\n").split("\t")
            if len(f) < 14:
                continue
            r = Row()
            (r.rank, r.pid, r.name, r.start, r.finish, r.time, r.height,
             r.speed, r.distance, r.dist_p, r.lead_p, r.spd_p, r.low_p,
             r.score) = (x.strip() for x in f[:14])
            rows.append(r)
    return rows


def _pid_of(track) -> str:
    """The official ID is the IGC filename: 0157.igc -> 0157."""
    for name in track.source_files:
        m = re.match(r"^(\d+)", name.rsplit("/", 1)[-1])
        if m:
            return m.group(1)
    return ""


def _stat(name, diffs, unit, tol, fmt="{:+.2f}"):
    """One line: how many pilots differ, and by how much at worst."""
    if not diffs:
        return None, 0
    bad = [d for d in diffs if abs(d[1]) > tol]
    worst = max(diffs, key=lambda d: abs(d[1]))
    mean = sum(abs(d[1]) for d in diffs) / len(diffs)
    ok = not bad
    line = (f"{len(diffs) - len(bad)}/{len(diffs)} within ±{tol}{unit}   "
            f"mean |Δ| {mean:.3f}{unit}   worst " + fmt.format(worst[1])
            + f"{unit} ({worst[0]})")
    return (name, ok, line), len(bad)


def compare(task, results, ts, tracks, rows, tz_hours: float,
            day: int, top_diffs: int = 12) -> tuple[list, int]:
    """Returns ([(label, ok, detail)], n_failed)."""
    by_pid = {r.pid: r for r in rows}
    tr_by_pilot = {t.pilot: t for t in tracks}

    matched = []
    unmatched_engine = []
    for res in results:
        t = tr_by_pilot.get(res.pilot)
        pid = _pid_of(t) if t else ""
        row = by_pid.get(pid)
        if row is None:
            unmatched_engine.append(res.pilot)
        else:
            matched.append((res, row, pid))
    unmatched_official = [r.pid for r in rows
                          if r.pid not in {p for _, _, p in matched}]

    out = []
    failed = 0

    out.append((f"matched {len(matched)}/{len(rows)} rows by pilot ID",
                not unmatched_engine and not unmatched_official,
                (f"engine-only: {unmatched_engine[:4]}   " if unmatched_engine else "")
                + (f"official-only: {unmatched_official[:4]}" if unmatched_official
                   else "all rows matched")))
    if unmatched_engine or unmatched_official:
        failed += 1

    # --- population ------------------------------------------------------
    off_goal = sum(1 for r in rows if _clock(r.finish) is not None)
    ok = off_goal == ts.pilots_goal
    out.append(("pilots in goal", ok,
                f"engine {ts.pilots_goal}   official {off_goal}"))
    failed += 0 if ok else 1

    # --- the three pots --------------------------------------------------
    # Every pot is a single number for the whole task, so a mismatch here
    # moves every pilot at once and is worth separating from per-pilot error.
    off_dist_pot = max((_num(r.dist_p) or 0) for r in rows)
    off_time_pot = max((_num(r.spd_p) or 0) for r in rows)
    off_lead_pot = max((_num(r.lead_p) or 0) for r in rows)
    for label, mine, theirs in (
            ("available distance (S7F 11)", ts.alloc.available_distance, off_dist_pot),
            ("available time (S7F 11)", ts.alloc.available_time, off_time_pot),
            ("available leading (S7F 11)", ts.alloc.available_leading, off_lead_pot)):
        ok = abs(mine - theirs) <= 0.5
        out.append((label, ok, f"engine {mine:,.1f}   official {theirs:,.1f}   "
                               f"Δ {mine - theirs:+.1f}"))
        failed += 0 if ok else 1

    off_total_pot = off_dist_pot + off_time_pot + off_lead_pot
    if off_total_pot > 0:
        # LeadingTimeRatio is invariant under whatever DistanceWeight came out
        # at: it is just leading/(leading+time). So the official pots pin it
        # exactly, whatever their goal ratio was, which makes this the cleanest
        # way to read a task parameter back out of a published result.
        ltr = off_lead_pot / (off_lead_pot + off_time_pot)
        mine = (ts.alloc.leading_weight / (1 - ts.alloc.distance_weight)
                if ts.alloc.distance_weight < 1 else 0.0)
        ok = abs(ltr - mine) < 0.0005
        out.append(("implied LeadingTimeRatio (S7F 11)", ok,
                    f"official pots imply {ltr:.4%}   engine uses {mine:.4%}   "
                    f"(pots sum to {off_total_pot:.1f})"))
        failed += 0 if ok else 1

    # --- best distance ---------------------------------------------------
    off_best = max((_num(r.distance) or 0) for r in rows) * 1000.0
    ok = abs(ts.best_distance - off_best) <= 50.0
    out.append(("best distance flown", ok,
                f"engine {ts.best_distance:,.0f} m   official {off_best:,.0f} m   "
                f"Δ {ts.best_distance - off_best:+,.0f} m "
                f"({(ts.best_distance - off_best) / off_best * 100:+.2f}%)"))
    failed += 0 if ok else 1

    # --- per-pilot quantities -------------------------------------------
    d_dist, d_ess, d_time, d_speed, d_dp, d_tp, d_lp, d_tot = ([] for _ in range(8))
    tz = tz_hours * 3600.0
    for res, row, pid in matched:
        who = f"{pid} {row.name}"
        od = _num(row.distance)
        if od is not None:
            d_dist.append((who, res.distance / 1000.0 - od))
        of = _clock(row.finish)
        if of is not None and res.ess_time is not None:
            # `of` is in the official file's local time; the engine is in UTC.
            d_ess.append((who, (res.ess_time - day) - (of - tz)))
        ot = _clock(row.time)
        if ot is not None and res.speed_section_time:
            d_time.append((who, res.speed_section_time - ot))
        os_ = _num(row.speed)
        if os_ is not None and res.speed:
            d_speed.append((who, res.speed - os_))
        # The official table shows time points BEFORE the S7F 13.1 elevated-goal
        # reduction and puts the reduction in its own "Low P" column, which the
        # total then adds. The engine folds the factor into time points, so the
        # comparable quantity is spdP + lowP.
        spd, low = _num(row.spd_p), _num(row.low_p)
        net_time = None if spd is None else spd + (low or 0.0)
        for lst, mine, theirs in (
                (d_dp, res.distance_points, _num(row.dist_p)),
                (d_tp, res.time_points, net_time),
                (d_lp, res.leading_points, _num(row.lead_p)),
                (d_tot, res.total_points, _num(row.score))):
            if theirs is not None:
                lst.append((who, mine - theirs))

    for name, diffs, unit, tol, fmt in (
            ("scored distance", d_dist, " km", 0.05, "{:+.3f}"),
            ("ESS / finish time", d_ess, " s", 1.0, "{:+.0f}"),
            ("speed section elapsed", d_time, " s", 1.0, "{:+.0f}"),
            ("average speed", d_speed, " km/h", 0.05, "{:+.2f}"),
            ("distance points", d_dp, " pt", 0.1, "{:+.2f}"),
            ("time points (spdP+lowP)", d_tp, " pt", 0.1, "{:+.2f}"),
            ("leading points", d_lp, " pt", 0.1, "{:+.2f}"),
            ("TOTAL score", d_tot, " pt", 0.1, "{:+.2f}")):
        line, nbad = _stat(name, diffs, unit, tol, fmt)
        if line:
            out.append(line)
            failed += 1 if nbad else 0

    return out, failed


def render(task, results, ts, tracks, rows, tz_hours, day, path,
           top_diffs: int = 15) -> str:
    checks, failed = compare(task, results, ts, tracks, rows, tz_hours, day)
    o = []
    P = o.append
    P("")
    P("  " + lb.paint("OFFICIAL RESULT COMPARISON", lb.BOLD + lb.WHITE))
    P("  " + lb.paint("─" * 96, lb.GREY))
    P("  " + lb.paint(f"reference: {path}", lb.GREY))
    P("  " + lb.paint(f"official times are UTC{tz_hours:+g}; the engine works in UTC",
                      lb.DIM + lb.GREY))
    P("")
    for name, ok, detail in checks:
        mark = lb.paint("✓", lb.GREEN) if ok else lb.paint("✗", lb.RED)
        P(f"  {mark} " + lb.paint(f"{name:<36}", lb.WHITE if ok else lb.RED)
          + lb.paint(detail, lb.DIM + lb.GREY))

    # --- the biggest per-pilot total differences -------------------------
    by_pid = {r.pid: r for r in rows}
    tr_by_pilot = {t.pilot: t for t in tracks}
    diffs = []
    for res in results:
        t = tr_by_pilot.get(res.pilot)
        pid = _pid_of(t) if t else ""
        row = by_pid.get(pid)
        if row is None:
            continue
        s = _num(row.score)
        if s is None:
            continue
        diffs.append((abs(res.total_points - s), pid, row, res))
    diffs.sort(reverse=True, key=lambda d: d[0])

    P("")
    P("  " + lb.paint(f"LARGEST TOTAL DIFFERENCES (top {top_diffs} of {len(diffs)})",
                      lb.BOLD + lb.WHITE))
    P("  " + lb.paint("─" * 96, lb.GREY))
    P("    " + lb.paint(f"{'ID':<6}{'PILOT':<20}"
                        f"{'DIST km':>18}{'DIST P':>16}{'TIME P':>16}"
                        f"{'LEAD P':>16}{'TOTAL':>18}", lb.GREY))
    for _, pid, row, res in diffs[:top_diffs]:
        def pair(mine, theirs, w=16, dp=1):
            if theirs is None:
                return lb.paint(f"{'—':>{w}}", lb.GREY)
            d = mine - theirs
            col = lb.WHITE if abs(d) < 0.05 else (lb.YELLOW if abs(d) < 2 else lb.RED)
            return lb.paint(f"{mine:.{dp}f}/{theirs:.{dp}f}".rjust(w), col)
        P("    " + lb.paint(f"{pid:<6}{row.name[:19]:<20}", lb.WHITE)
          + pair(res.distance / 1000.0, _num(row.distance), 18, 2)
          + pair(res.distance_points, _num(row.dist_p))
          + pair(res.time_points, _num(row.spd_p))
          + pair(res.leading_points, _num(row.lead_p))
          + pair(res.total_points, _num(row.score), 18))
    P("")
    P("  " + lb.paint("each cell is  engine/official", lb.DIM + lb.GREY))
    P("")
    if failed:
        P("  " + lb.paint(f"✗ {failed} of {len(checks)} comparisons outside tolerance",
                          lb.RED + lb.BOLD))
        P("  " + lb.paint(
            "Tolerances are deliberately tight — every pilot within 0.1 pt, 1 s, "
            "50 m. A category", lb.DIM + lb.GREY))
        P("  " + lb.paint(
            "fails if ONE pilot misses, so read the mean and the worst case, not "
            "the count.", lb.DIM + lb.GREY))
    else:
        P("  " + lb.paint(f"✓ all {len(checks)} comparisons within tolerance",
                          lb.GREEN + lb.BOLD))
    return "\n".join(o), failed
