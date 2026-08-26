"""Terminal rendering of a per-pilot audit record (engine/audit.py).

Kept out of engine/ for the same reason leaderboard.py is: the engine must not
know what a terminal is. This module turns the record into something a meet
director can read aloud to a pilot standing at the scoring desk.

The order is the order the engine actually decided things in — track, takeoff,
start, zones, distance, speed, leading, points — because a protest is nearly
always about one of those steps and the reader needs to find it fast.
"""

from __future__ import annotations

import math

import leaderboard as lb
from engine import audit as au


def _hhmmss(t):
    return au._hhmmss(t)


def _rule(text: str) -> str:
    return lb.paint(text, lb.DIM + lb.GREY)


def _h(n: str, title: str) -> str:
    return "\n" + lb.paint(f"  {n}. {title}", lb.BOLD + lb.WHITE) + "\n  " + \
        lb.paint("─" * 100, lb.GREY)


def _kv(k: str, v: str, colour: str = "") -> str:
    return "     " + lb.paint(f"{k:<26}", lb.GREY) + lb.paint(str(v), colour or lb.WHITE)


def render(rec: dict, task, show_fixes: int = 0) -> str:
    """The whole audit as text. `show_fixes` dumps N raw fixes around each event."""
    r = rec["result"]
    tr = rec["trace"]
    fld = rec["field"]
    out: list[str] = []
    P = out.append

    # ---- heading ---------------------------------------------------------
    rank = f"rank {rec['rank']} of {rec['field_size']}" if rec["rank"] else ""
    P("")
    P("  " + lb.paint(f"PILOT AUDIT — {rec['pilot']}", lb.BOLD + lb.WHITE)
      + ("   " + lb.paint(rank, lb.CYAN) if rank else ""))
    P("  " + lb.paint(rec["rules"], lb.DIM + lb.GREY))
    if not rec["replay_agrees"]:
        P("  " + lb.paint("✗ REPLAY DISAGREES WITH THE LEADERBOARD — this is an engine "
                          "fault, not a scoring decision:", lb.RED + lb.BOLD))
        for k, (a, b) in rec["replay_mismatches"].items():
            P("      " + lb.paint(f"{k}: replay {a!r} vs board {b!r}", lb.RED))
    else:
        P("  " + lb.paint("✓ independently replayed from the stored points — "
                          "identical to the leaderboard", lb.GREEN))

    # ---- 0. inputs -------------------------------------------------------
    inp = rec["inputs"]
    P(_h("0", "INPUTS — everything this score depends on, by content hash"))
    P(_rule("     Re-running the command below on these four hashes reproduces this "
            "page byte for byte."))
    P(_kv("task file", f"{inp['task_file']}"))
    P(_kv("  sha256", inp["task_sha256"]))
    P(_kv("competition config", f"{inp['comp_file']}"))
    P(_kv("  sha256", inp["comp_sha256"]))
    for name in inp["igc_files"]:
        P(_kv("tracklog", name))
        P(_kv("  sha256", inp["igc_sha256"].get(name, "—")))
    P(_kv("engine source", inp["engine_sha256"]))
    P(_kv("scored as at", f"{_hhmmss(rec['now'])}Z"
          if rec["now"] < 1e17 else "FINAL — every fix scored"))

    # ---- 1. track integrity ---------------------------------------------
    t = rec["track"]
    P(_h("1", "TRACKLOG — what the engine was given, before any scoring"))
    if not t.get("fixes"):
        P("     " + lb.paint("no fixes at all", lb.RED))
        return "\n".join(out)
    P(_kv("fixes", f"{t['fixes']:,}"))
    P(_kv("covering", f"{_hhmmss(t['first_t'])}Z → {_hhmmss(t['last_t'])}Z "
                      f"({au._dur(t['span_s'])})"))
    P(_kv("mean rate", f"{t['nominal_rate_hz']:.3f} Hz"))
    gap_c = lb.YELLOW if t["biggest_gap_s"] > 30 else lb.WHITE
    P(_kv("largest gap", f"{t['biggest_gap_s']} s at {_hhmmss(t['biggest_gap_at'])}Z", gap_c))
    P(_kv("gaps over 5 s", f"{t['n_gaps_over_5s']}  "
                           f"({t['seconds_missing']:,} s of the flight not recorded)",
          gap_c if t["n_gaps_over_5s"] else lb.WHITE))
    P(_kv("GPS altitude range", f"{t['alt_gps_range'][0]:,} … {t['alt_gps_range'][1]:,} m"))
    P(_kv("baro altitude range", f"{t['alt_baro_range'][0]:,} … {t['alt_baro_range'][1]:,} m"))
    if t["n_gaps_over_5s"]:
        P(_rule("     A gap can hide a cylinder crossing. Every gap over 5 s is listed "
                "so a claimed crossing can be checked against one:"))
        for at, d in t["gaps_over_5s"]:
            P("       " + lb.paint(f"{_hhmmss(at)}Z  +{d:>4d} s", lb.YELLOW))

    # ---- 2. takeoff ------------------------------------------------------
    P(_h("2", "TAKEOFF"))
    tk = tr.get("takeoff")
    if tk:
        P(_kv("detected at", f"fix #{tk['fix']:,}   {_hhmmss(tk['t'])}Z"))
        P(_kv("position", f"{tk['lat']:.5f}, {tk['lon']:.5f}   {tk['alt_gps']:,} m GPS"))
        P(_kv("distance from fix #0", f"{tk['from_launch']:.0f} m  "
                                      f"(threshold {tk['radius']:.0f} m)"))
        P(_rule("     Engine convention, not a rule: the first fix more than 200 m from "
                "the first recorded position. Used only to decide who counts as flying "
                "for launch validity (S7F 10.1)."))
    else:
        P("     " + lb.paint("never moved more than 200 m from the first fix — "
                             "not counted as launched", lb.YELLOW))

    # ---- 3. start --------------------------------------------------------
    sw = task.waypoints[task.start_index]
    P(_h("3", f"START — SSS {sw.name}, radius {sw.raw_radius:,.0f} m"))
    P(_kv("tolerance zone", f"{sw.inner:,.1f} … {sw.outer:,.1f} m from centre "
                            f"(flat ±5 m, S7F 9.1.1)"))
    P(_kv("start type", f"{task.start_type}   "
          f"gate(s) {', '.join(_hhmmss(g) + 'Z' for g in task.gates)}"))
    P(_kv("declared direction", f"{task.start_direction}"))
    P(_rule("     S7F 6.2.1: the enter/exit designation was removed in 2020. "
            "'The direction in which such a crossing occurs is irrelevant.'"))
    P(_rule("     The engine therefore validates the SSS on ANY crossing of the "
            "tolerance band, and the declared direction above is displayed only."))
    P("")

    xs = tr.get("sss_crossings", [])
    scored = tr.get("scored_start")
    scored_fix = scored["fix"] if scored else -1
    if not xs:
        P("     " + lb.paint("no crossing of the SSS tolerance zone anywhere in this "
                             "tracklog", lb.RED))
    else:
        P("     " + lb.paint(f"every crossing of the SSS tolerance zone in this "
                             f"tracklog ({len(xs)} found):", lb.WHITE))
        P("       " + lb.paint(f"{'#':>3} {'FIX':>9} {'TIME':>10} "
                               f"{'d(prev fix)':>12} {'d(this fix)':>12} "
                               f"{'DIR':<8} {'VS GATE':<12}", lb.GREY))
        shown = xs if len(xs) <= 40 else xs[:20] + [None] + xs[-20:]
        n = 0
        for c in shown:
            if c is None:
                P("       " + lb.paint(f"    … {len(xs) - 40} more crossings elided "
                                       f"(--json for all)", lb.DIM + lb.GREY))
                n += len(xs) - 40
                continue
            n += 1
            mark = "  ← SCORED START" if c["fix"] == scored_fix else ""
            dt = c["t"] - task.first_gate
            vs = f"{dt:+.0f}s" if abs(dt) < 86400 else "—"
            col = lb.GREEN + lb.BOLD if mark else (
                lb.GREY if not c["after_gate"] else lb.WHITE)
            P("       " + lb.paint(
                f"{n:>3} {c['fix']:>9,} {_hhmmss(c['t']):>10} "
                f"{c['d_prev']:>12,.1f} {c['d_fix']:>12,.1f} "
                f"{'outward' if c['outward'] else 'inward':<8} "
                f"{vs:<12}", col) + lb.paint(mark, lb.GREEN + lb.BOLD))

    sr = tr.get("start_rule", {})
    if sr:
        P("")
        P(_kv("crossings after gate", f"{sr.get('n_after_gate', 0)} of "
                                      f"{sr.get('n_crossings', 0)}"))
        if sr.get("early"):
            P("     " + lb.paint("⚠ at least one crossing is BEFORE the gate — "
                                 "S7F 13.3 early start", lb.YELLOW))
        if sr.get("next_zone"):
            P(_kv("next control zone", f"{sr['next_zone']} first validated "
                  f"{_hhmmss(sr['next_zone_first_validated'])}Z"
                  if sr.get("next_zone_first_validated") else
                  f"{sr['next_zone']} never validated"))
        P("     " + lb.paint("rule applied:", lb.GREY))
        for line in _wrap(sr.get("rule", "—"), 92):
            P("       " + lb.paint(line, lb.WHITE))

    cands = tr.get("candidates", [])
    if len(cands) > 1:
        P("")
        P("     " + lb.paint(f"{len(cands)} candidate starts were each replayed in "
                             f"full (S7F 8.1):", lb.WHITE))
        for c in cands:
            mark = "  ← kept" if c["fix"] == scored_fix else ""
            P("       " + lb.paint(
                f"fix #{c['fix']:<9,} {_hhmmss(c['t'])}Z  →  distance "
                f"{c['raw']/1000:>8,.2f} km   goal "
                f"{_hhmmss(c['goal']) if c['goal'] else 'no':<9}", lb.WHITE)
              + lb.paint(mark, lb.GREEN + lb.BOLD))

    if r.start_time is not None:
        P("")
        P(_kv("SCORED CROSSING", f"{_hhmmss(r.start_cross_time)}Z", lb.GREEN + lb.BOLD))
        P(_kv("START CLOCK", f"{_hhmmss(r.start_time)}Z", lb.GREEN + lb.BOLD))
        if task.start_type.upper().startswith("RACE"):
            P(_rule("     RACE to goal: the speed section is timed from the GATE, not "
                    "from the crossing. Leaving late costs time; it does not move the "
                    "clock (S7F 8.1)."))
    else:
        P("")
        P("     " + lb.paint("NO VALID START", lb.RED + lb.BOLD))
        if r.early_start:
            P(_rule("     [PG] S7F 13.3: an early start scores the launch-to-SSS "
                    "distance only. Hang-gliding instead applies a time penalty."))

    # ---- 4. control zones -----------------------------------------------
    P(_h("4", "CONTROL ZONES — every turnpoint, and the fix that validated it"))
    P("       " + lb.paint(f"{'#':>2} {'NAME':<8} {'KIND':<10} {'RADIUS':>9} "
                           f"{'ZONE (m from centre)':>22} {'VALIDATED':>10} "
                           f"{'FIX':>9} {'d(prev)':>10} {'d(fix)':>10}", lb.GREY))
    zones = {z["wp"]: z for z in (tr.get("zones") or []) if z["kind"] == "validated"}
    for w in task.waypoints:
        i = w.index
        z = zones.get(i)
        tag = r.tags[i] if i < len(r.tags) else None
        if i == task.start_index and r.start_cross_time is not None:
            tag = r.start_cross_time
        kind = w.kind
        if i == task.ess_index and i == task.goal_index:
            kind = "ESS+GOAL"
        if tag is None and i > task.start_index:
            col, mark, fixs, dp, df = lb.RED, "not validated", "—", "—", "—"
            P("       " + lb.paint(
                f"{i:>2} {w.name:<8} {kind:<10} {w.raw_radius:>8,.0f}m "
                f"{w.inner:>10,.1f} … {w.outer:<9,.1f} {mark:>10} "
                f"{fixs:>9} {dp:>10} {df:>10}", col))
        else:
            fixs = f"{z['fix']:,}" if z else "—"
            dp = f"{z['d_prev']:,.1f}" if z else "—"
            df = f"{z['d_fix']:,.1f}" if z else "—"
            P("       " + lb.paint(
                f"{i:>2} {w.name:<8} {kind:<10} {w.raw_radius:>8,.0f}m "
                f"{w.inner:>10,.1f} … {w.outer:<9,.1f} "
                f"{(_hhmmss(tag) if tag else '—'):>10} "
                f"{fixs:>9} {dp:>10} {df:>10}", lb.WHITE))
    P(_rule("     S7F 9.2.1: the crossing time is the timestamp of the tracklog point at "
            "which the crossing was detected — NOT an interpolated time. d(prev) and "
            "d(fix) are the two distances that bracket the boundary."))
    if any(z.get("how", "").startswith("inside") for z in zones.values()):
        P(_rule("     'inside zone, no boundary event' means the pilot was already "
                "within the zone at that fix without a boundary transition being seen — "
                "normally a telemetry gap over the cylinder."))
    for z in (tr.get("zones") or []):
        if z["kind"] == "goal_after_deadline":
            P("     " + lb.paint(f"⚠ goal cylinder reached at {_hhmmss(z['t'])}Z, AFTER "
                                 f"the deadline {_hhmmss(z['deadline'])}Z — not scored "
                                 f"as goal", lb.RED))

    # ---- 5. distance -----------------------------------------------------
    P(_h("5", "SCORED DISTANCE (S7F 9.3)"))
    P(_kv("optimised task distance", f"{task.total_distance:,.1f} m  "
                                     f"({task.total_distance/1000:.2f} km)"))
    P(_rule("     Optimised = the shortest legal route through the cylinders, not "
            "centre-to-centre. Centre-to-centre for this task is "
            f"{task.centre_distance/1000:.2f} km."))
    if r.goal_time is not None:
        P(_kv("goal reached", f"{_hhmmss(r.goal_time)}Z → full task distance",
              lb.GREEN))
    elif r.start_time is None:
        P(_kv("no valid start", f"raw {r.raw_distance:,.1f} m", lb.YELLOW))
    else:
        mr = tr.get("min_remaining", 0.0)
        nw = r.next_wp
        wname = task.waypoints[nw].name if nw < len(task.waypoints) else "—"
        P(_kv("closest approach", f"fix #{r.dist_fix_index:,}  "
                                  f"toward {wname}"))
        P(_kv("distance still to fly", f"{mr:,.1f} m"))
        P(_kv("→ raw distance", f"{task.total_distance:,.1f} − {mr:,.1f} = "
                                f"{r.raw_distance:,.1f} m"))
    if r.distance > r.raw_distance:
        P(_kv("minimum distance floor", f"{r.raw_distance:,.1f} → {r.distance:,.1f} m  "
                                        f"(S7F 5.2)", lb.YELLOW))
    P(_kv("SCORED DISTANCE", f"{r.distance:,.1f} m  ({r.distance/1000:.2f} km)",
          lb.GREEN + lb.BOLD))

    # ---- 6. speed section ------------------------------------------------
    P(_h("6", "SPEED SECTION"))
    P(_kv("speed section distance", f"{task.speed_distance:,.1f} m  (SSS → ESS, optimised)"))
    if r.speed_section_time:
        P(_kv("clock start", f"{_hhmmss(r.start_time)}Z"))
        P(_kv("ESS", f"{_hhmmss(r.ess_time)}Z"))
        P(_kv("elapsed", f"{au._dur(r.speed_section_time)}  "
                         f"({r.speed_section_time:,.0f} s)"))
        P(_kv("average speed", f"{r.speed:.2f} km/h", lb.GREEN))
    else:
        P("     " + lb.paint("speed section not completed — no time points "
                             "(S7F 9.4.1 [PG]: only pilots who reach GOAL have a "
                             "time that counts)", lb.YELLOW))

    # ---- 7. leading coefficient ------------------------------------------
    P(_h("7", "LEADING COEFFICIENT (S7F 12.3.1, paragliding weighted form)"))
    if r.lc > 0:
        s = rec["replay"].lead_samples or r.lead_samples
        P(_kv("samples", f"{len(s):,} points where distance-to-ESS strictly decreased"))
        P(_rule("     Only decreases contribute: the integral runs between consecutive "
                "'done' values, and 'done' is a function of distance-to-ESS alone. "
                "Circling in a thermal adds nothing and costs nothing."))
        P(_kv("speed section", f"{task.speed_distance/1000:.3f} km"))
        P(_kv("maxTime (field-wide)", f"{fld['max_time']:,.0f} s"))
        P(_kv("leadingArea (Σ)", f"{rec['replay'].lead_area:,.3f}"))
        P(_kv("minToESS at last sample", f"{rec['replay'].lead_min_to_ess:.6f} km"))
        P(_rule("     LC = (leadingArea + minToESS × maxTime × ∫weight over the "
                "remaining 'done') / (1800 × speedSectionDistance)"))
        P(_kv("LC", f"{r.lc:.6f}", lb.GREEN))
        P(_kv("LCmin (best in field)", f"{fld['lc_min']:.6f}"))
        if s:
            P("     " + lb.paint("first and last samples (taskTime s, distance-to-ESS km):",
                                 lb.GREY))
            head = s[:5]
            tail = s[-5:] if len(s) > 10 else []
            for tt, dd in head:
                P("       " + lb.paint(f"{tt:>9,.0f} s   {dd:>9.3f} km", lb.WHITE))
            if tail:
                P("       " + lb.paint(f"    … {len(s)-10:,} more", lb.DIM + lb.GREY))
                for tt, dd in tail:
                    P("       " + lb.paint(f"{tt:>9,.0f} s   {dd:>9.3f} km", lb.WHITE))
    else:
        P("     " + lb.paint("no leading coefficient — no valid start", lb.YELLOW))

    # ---- 8. field --------------------------------------------------------
    P(_h("8", "FIELD — the half of the score that is not about this pilot"))
    P(_rule("     These numbers are identical for every pilot and move as the field "
            "lands. 'My points changed and I did not move' is almost always this."))
    P(_kv("pilots present / flying", f"{fld['pilots_present']} / {fld['pilots_flying']}"))
    P(_kv("in goal", f"{fld['pilots_goal']}   → goal ratio {fld['goal_ratio']:.6f}"))
    P("")
    P(_kv("nominal distance", f"{fld['nominal_distance']/1000:,.1f} km   (S7F 5.1)"))
    P(_kv("minimum distance", f"{fld['minimum_distance']/1000:,.1f} km   (S7F 5.2)"))
    P(_kv("nominal time", f"{fld['nominal_time']/60:,.0f} min   (S7F 5.3)"))
    P("")
    P(_kv("launch validity", f"{fld['launch_validity']:.6f}   (S7F 10.1)"))
    P(_kv("distance validity", f"{fld['distance_validity']:.6f}   (S7F 10.2)"))
    P(_kv("time validity", f"{fld['time_validity']:.6f}   (S7F 10.3)"))
    P(_kv("→ TASK VALIDITY", f"{fld['launch_validity']:.6f} × "
                             f"{fld['distance_validity']:.6f} × "
                             f"{fld['time_validity']:.6f} = "
                             f"{fld['task_validity']:.6f}", lb.CYAN + lb.BOLD))
    P("")
    P(_kv("distance weight", f"{fld['distance_weight']:.6f}   → available "
                             f"{fld['available_distance']:>6,.0f}"))
    P(_kv("leading weight", f"{fld['leading_weight']:.6f}   → available "
                            f"{fld['available_leading']:>6,.0f}"))
    P(_kv("time weight", f"{fld['time_weight']:.6f}   → available "
                         f"{fld['available_time']:>6,.0f}"))
    P(_kv("arrival weight", f"{fld['arrival_weight']:.6f}   → available "
                            f"{0:>6,.0f}   [PG] S7F 12.4: none, ever"))
    P(_kv("→ TOTAL AVAILABLE", f"{fld['available_total']:,.0f}", lb.CYAN + lb.BOLD))
    P("")
    P(_kv("best distance", f"{fld['best_distance']:,.1f} m"))
    P(_kv("best time", f"{au._dur(fld['best_time'])}" if fld["best_time"]
          else "nobody reached goal"))
    P(_rule("     [PG] S7F 9.4.1: a time counts towards best time only if the pilot "
            "reached GOAL. Hang-gliding accepts ESS."))

    # ---- 9. points -------------------------------------------------------
    P(_h("9", "POINTS — every line, with its formula and its numbers"))
    for d in rec["points"]:
        if d["part"] == "TOTAL":
            P("  " + lb.paint("─" * 100, lb.GREY))
        P("     " + lb.paint(f"{d['part']:<16}", lb.BOLD + lb.WHITE)
          + lb.paint(f"{d['ref']}", lb.CYAN))
        P("     " + lb.paint(f"{'':<16}{d['formula']}", lb.GREY))
        P("     " + lb.paint(f"{'':<16}{d['substituted']}", lb.WHITE))
        val = f"{d['value']:,.4f}" if isinstance(d["value"], float) else str(d["value"])
        line = "     " + lb.paint(f"{'':<16}= ", lb.GREY) + lb.paint(
            val, lb.GREEN + lb.BOLD if d["part"] == "TOTAL" else lb.GREEN)
        if d["engine"] is not None and abs(d["engine"] - d["value"]) > 5e-5:
            line += lb.paint(f"   ✗ engine says {d['engine']:,.4f} — MISMATCH",
                             lb.RED + lb.BOLD)
        P(line)
        P("")

    # ---- 10. reproduce ---------------------------------------------------
    P(_h("10", "REPRODUCE"))
    P(_rule("     Anyone with these files can rerun this exact page. If a hash in "
            "section 0 differs, the inputs differ — not the engine."))
    P("     " + lb.paint(f'./run.py --explain "{rec["pilot"]}"', lb.WHITE))
    P("     " + lb.paint(f'./run.py --explain "{rec["pilot"]}" --json audit.json'
                         f'    # same record, machine-readable', lb.WHITE))
    P("")
    return "\n".join(out)


def _wrap(s: str, w: int) -> list[str]:
    words = s.split()
    lines, cur = [], ""
    for word in words:
        if len(cur) + len(word) + 1 > w:
            lines.append(cur)
            cur = word
        else:
            cur = f"{cur} {word}".strip()
    if cur:
        lines.append(cur)
    return lines
