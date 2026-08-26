"""Per-pilot audit trail — the answer to "prove it".

A pilot who protests does not want the score. They already have the score.
They want to know *which fix in their own tracklog* the engine looked at, what
it measured there, which rule it applied, and what the arithmetic was. This
module produces exactly that, for one pilot, as a structured record that
renders to a terminal or to JSON.

Three properties make it evidence rather than commentary:

  1. **Same pass.** The evidence comes out of `score_pilot()` itself, via its
     `trace` argument — not from a second walk of the track. There is no
     shadow implementation that can drift from the scoring one, so the audit
     physically cannot describe a calculation different from the one that
     produced the number. `--verify` asserts that tracing changes no value.

  2. **Recomputable.** Every input is hashed: the task file, the pilot's IGC
     file(s), the competition configuration and the engine source. Two people
     running the same command on the same inputs get byte-identical output.
     Anything else means an input changed, and the hashes say which.

  3. **Arithmetic, not assertions.** Every points line shows its inputs, its
     formula and its S7F reference, so the reader can redo it on paper.

The field-level half (validity, weights, best distance, best time, LCmin) is
identical for every pilot and is what actually moves during a competition, so
it is reported separately and labelled with the population it was computed
over (DESIGN.md §11.4).
"""

from __future__ import annotations

import hashlib
import math
import os
from dataclasses import dataclass, field

from . import gap
from .gap import GapParams
from .score import PilotResult, score_pilot
from .scoring import TaskScore
from .task import CompiledTask


def sha256_file(path: str) -> str:
    try:
        with open(path, "rb") as fh:
            return hashlib.sha256(fh.read()).hexdigest()
    except OSError:
        return "<unreadable>"


def engine_hash() -> str:
    """One hash over every engine source file, so the audit names its own code.

    Sorted by name so the value does not depend on directory order.
    """
    here = os.path.dirname(os.path.abspath(__file__))
    h = hashlib.sha256()
    for name in sorted(os.listdir(here)):
        if name.endswith(".py"):
            with open(os.path.join(here, name), "rb") as fh:
                h.update(name.encode())
                h.update(fh.read())
    return h.hexdigest()


def _hhmmss(t: float | None) -> str:
    if t is None:
        return "—"
    t = int(t) % 86400
    return f"{t // 3600:02d}:{t % 3600 // 60:02d}:{t % 60:02d}"


def _dur(sec: float | None) -> str:
    if sec is None:
        return "—"
    sec = int(sec)
    return f"{sec // 3600:d}:{sec % 3600 // 60:02d}:{sec % 60:02d}"


# --- track integrity ------------------------------------------------------


def track_integrity(fixes) -> dict:
    """What the point list itself looks like, before any scoring.

    A protest often turns out to be a data problem, not a scoring problem: a
    logger that dropped out over the turnpoint, a clock that jumped, two
    sessions merged. Those are visible here and nowhere else, so they are
    reported first.
    """
    n = len(fixes)
    if n == 0:
        return {"fixes": 0}
    gaps = []
    prev = fixes[0].t
    biggest = 0
    biggest_at = None
    total_gap = 0
    for f in fixes[1:]:
        d = f.t - prev
        if d > 1:
            total_gap += d - 1
            if d > biggest:
                biggest, biggest_at = d, prev
        if d > 5:
            gaps.append((prev, d))
        prev = f.t
    span = fixes[-1].t - fixes[0].t
    return {
        "fixes": n,
        "first_t": fixes[0].t,
        "last_t": fixes[-1].t,
        "span_s": span,
        "nominal_rate_hz": (n - 1) / span if span else 0.0,
        "biggest_gap_s": biggest,
        "biggest_gap_at": biggest_at,
        "seconds_missing": total_gap,
        "gaps_over_5s": gaps[:20],
        "n_gaps_over_5s": len(gaps),
        "alt_gps_range": (min(f.alt_gps for f in fixes), max(f.alt_gps for f in fixes)),
        "alt_baro_range": (min(f.alt_baro for f in fixes), max(f.alt_baro for f in fixes)),
    }


# --- the audit record -----------------------------------------------------


def audit_pilot(task: CompiledTask, track, result: PilotResult, ts: TaskScore,
                params: GapParams, now: float, *, task_path: str = "",
                comp_path: str = "", igc_dir: str = "",
                rank: int | None = None, field_size: int | None = None) -> dict:
    """Re-run this one pilot with tracing on, and assemble the whole record.

    Re-running is deliberate. It costs a few milliseconds, it proves the result
    is reproducible from the stored points alone, and the record then carries
    an explicit equality check between the traced re-run and the result that is
    actually on the leaderboard.
    """
    trace: dict = {}
    replay = score_pilot(task, track.fixes, now, params, trace)

    # The leading-coefficient samples do not survive the process boundary on
    # the parallel path — the worker reduces them to (leadingArea, minToESS)
    # and drops the rest, because 5,000 tuples per pilot cost more to ship than
    # the parallelism saved. So compare the reduction, which is what scoring
    # actually consumed, rather than the sample count, which is bookkeeping.
    from .gap import leading_partial
    replay.lead_area, replay.lead_min_to_ess = leading_partial(
        replay.lead_samples, task.speed_distance / 1000.0)

    # The audit is worthless if it describes a different run than the board.
    # Check the scored quantities, not the whole object: `tags` and the audit
    # anchors are lists and compare fine, but floats are compared exactly on
    # purpose — the same code on the same input must be bit-identical.
    agree = {
        "start_time": (replay.start_time, result.start_time),
        "start_cross_time": (replay.start_cross_time, result.start_cross_time),
        "ess_time": (replay.ess_time, result.ess_time),
        "goal_time": (replay.goal_time, result.goal_time),
        "distance": (replay.distance, result.distance),
        "raw_distance": (replay.raw_distance, result.raw_distance),
        "state": (replay.state, result.state),
        "leading area": (replay.lead_area, result.lead_area),
        "leading minToESS": (replay.lead_min_to_ess, result.lead_min_to_ess),
    }
    if result.lead_area is None:            # pilot never started; nothing to compare
        agree.pop("leading area")
        agree.pop("leading minToESS")
    mismatches = {k: v for k, v in agree.items() if v[0] != v[1]}

    return {
        "pilot": result.pilot,
        "rank": rank,
        "field_size": field_size,
        "rules": "FAI Sporting Code Section 7F, 2026 edition V1.0 — paragliding",
        "now": now,
        "inputs": {
            "task_file": task_path,
            "task_sha256": sha256_file(task_path) if task_path else "",
            "task_hash_short": task.task_hash,
            "igc_files": list(track.source_files),
            "igc_sha256": {n: sha256_file(os.path.join(igc_dir, n))
                           for n in track.source_files} if igc_dir else {},
            "comp_file": comp_path,
            "comp_sha256": sha256_file(comp_path) if comp_path else "",
            "engine_sha256": engine_hash(),
        },
        "replay_agrees": not mismatches,
        "replay_mismatches": mismatches,
        "track": track_integrity(track.fixes),
        "trace": trace,
        "result": result,
        # Section 7 reads its samples from here: on the parallel path they exist
        # only in the replay, and the replay is the run this page describes.
        "replay": replay,
        "field": field_summary(task, ts, params),
        "points": points_derivation(task, result, ts, params),
    }


def field_summary(task: CompiledTask, ts: TaskScore, params: GapParams) -> dict:
    """The half of the score that is not about this pilot at all.

    Spelled out because it is the half pilots dispute without realising it:
    "my points dropped and I did not move" is almost always the field moving —
    another pilot reaching goal changes the goal ratio, and with it everyone's
    available points (DESIGN.md §11.4).
    """
    p = params
    a = ts.alloc
    return {
        "pilots_present": ts.pilots_present,
        "pilots_flying": ts.pilots_flying,
        "pilots_goal": ts.pilots_goal,
        "pilots_ess": ts.pilots_ess,
        "nominal_distance": p.nominal_distance,
        "minimum_distance": p.minimum_distance,
        "nominal_time": p.nominal_time,
        "nominal_goal": p.nominal_goal,
        "nominal_launch": p.nominal_launch,
        "leading_time_ratio": p.leading_time_ratio,
        "launch_validity": ts.launch_validity,
        "distance_validity": ts.distance_validity,
        "time_validity": ts.time_validity,
        "task_validity": ts.task_validity,
        "goal_ratio": a.goal_ratio,
        "distance_weight": a.distance_weight,
        "leading_weight": a.leading_weight,
        "time_weight": a.time_weight,
        "arrival_weight": a.arrival_weight,
        "available_distance": a.available_distance,
        "available_time": a.available_time,
        "available_leading": a.available_leading,
        "available_total": a.available_total,
        "best_distance": ts.best_distance,
        "best_time": ts.best_time,
        "lc_min": ts.lc_min,
        "max_time": ts.max_time,
        "speed_distance": task.speed_distance,
        "total_distance": task.total_distance,
    }


def points_derivation(task: CompiledTask, r: PilotResult, ts: TaskScore,
                      params: GapParams) -> list[dict]:
    """Every points line as (label, formula with numbers substituted, value).

    Recomputed here from the same public gap.* functions the scorer used, so a
    disagreement between `value` and the corresponding field on PilotResult is
    a real bug and is reported as one rather than hidden.
    """
    a = ts.alloc
    out: list[dict] = []

    # --- distance, S7F 12.1 (linear for paragliding, 12.1.1) ---
    ratio = min(r.distance / ts.best_distance, 1.0) if ts.best_distance > 0 else 0.0
    dp = gap.distance_points(r.distance, ts.best_distance,
                             a.available_distance)
    out.append({
        "part": "distance", "ref": "S7F 12.1 / 12.1.1 [PG: linear, no difficulty]",
        "formula": "min(flown / bestDistance, 1) × availableDistance",
        "substituted": f"min({r.distance:,.1f} / {ts.best_distance:,.1f}, 1) "
                       f"= {ratio:.6f}   ×  {a.available_distance:,.0f}",
        "value": dp, "engine": r.distance_points,
    })

    # --- time, S7F 12.2, gated by 9.4.1 / 13.2 ---
    if r.goal_time is not None and ts.best_time and r.speed_section_time:
        sf = gap.speed_fraction(r.speed_section_time, ts.best_time)
        bt = ts.best_time / 3600.0
        pt = r.speed_section_time / 3600.0
        x = (pt - bt) / math.sqrt(bt) if bt > 0 else 0.0
        tp = sf * a.available_time
        out.append({
            "part": "speedFraction", "ref": "S7F 12.2",
            "formula": "1 − ((Tpilot − Tbest) / √Tbest)^(5/6)   [hours]",
            "substituted": f"Tpilot {pt:.6f} h, Tbest {bt:.6f} h  →  "
                           f"x = {x:.6f}  →  1 − x^(5/6) = {sf:.6f}",
            "value": sf, "engine": None,
        })
        note = ""
        if task.goal_elevated and r.goal_alt is not None:
            gaf = r.goal_altitude_factor
            tp *= gaf
            note = f" × goalAltitudeFactor {gaf:.4f} (S7F 13.1)"
        out.append({
            "part": "time", "ref": "S7F 12.2",
            "formula": "speedFraction × availableTime" + note,
            "substituted": f"{sf:.6f} × {a.available_time:,.0f}"
                           + (f" × {r.goal_altitude_factor:.4f}" if note else ""),
            "value": tp, "engine": r.time_points,
        })
    elif r.ess_time is not None:
        out.append({
            "part": "time", "ref": "S7F 13.2 [PG]",
            "formula": "ESS reached but not goal → essNoGoalTimeFactor × ...",
            "substituted": f"factor = {params.ess_no_goal_time_factor:.2f} "
                           f"(paragliding: 0%. Hang-gliding would award 80%.)",
            "value": r.time_points, "engine": r.time_points,
        })
    else:
        out.append({
            "part": "time", "ref": "S7F 12.2",
            "formula": "no time points without reaching goal",
            "substituted": "goal not reached" if ts.best_time
                           else "nobody reached goal — availableTime is 0 anyway",
            "value": 0.0, "engine": r.time_points,
        })

    # --- leading, S7F 12.3 ---
    if r.lc > 0 and ts.lc_min > 0:
        lf = gap.leading_factor(r.lc, ts.lc_min)
        num = (r.lc - ts.lc_min) ** 2
        den = math.sqrt(ts.lc_min)
        lp = lf * a.available_leading
        out.append({
            "part": "leadingFactor", "ref": "S7F 12.3",
            "formula": "max(0, 1 − ∛((LC − LCmin)² / √LCmin))",
            "substituted": f"LC {r.lc:.6f}, LCmin {ts.lc_min:.6f}  →  "
                           f"({num:.6f} / {den:.6f})^(1/3) = {(num/den)**(1/3):.6f}"
                           f"  →  {lf:.6f}",
            "value": lf, "engine": None,
        })
        out.append({
            "part": "leading", "ref": "S7F 12.3.1 [PG weighted form]",
            "formula": "leadingFactor × availableLeading",
            "substituted": f"{lf:.6f} × {a.available_leading:,.0f}",
            "value": lp, "engine": r.leading_points,
        })
    else:
        out.append({
            "part": "leading", "ref": "S7F 12.3",
            "formula": "no leading points without a valid start",
            "substituted": f"LC = {r.lc:.6f}, LCmin = {ts.lc_min:.6f}",
            "value": 0.0, "engine": r.leading_points,
        })

    # --- arrival: never, in paragliding ---
    out.append({
        "part": "arrival", "ref": "S7F 12.4 [PG]",
        "formula": "paragliding awards no arrival points, ever",
        "substituted": "0", "value": 0.0, "engine": 0.0,
    })

    total = sum(o["value"] for o in out if o["part"] in
                ("distance", "time", "leading", "arrival"))
    gross = round(total, 1)
    out.append({
        "part": "SUBTOTAL" if r.penalties else "TOTAL", "ref": "S7F 12",
        "formula": "distance + time + leading + arrival, rounded to 0.1",
        "substituted": f"{r.distance_points:.4f} + {r.time_points:.4f} + "
                       f"{r.leading_points:.4f} + 0 = {total:.4f}",
        "value": gross,
        "engine": None if r.penalties else r.total_points,
    })

    # --- S7F 13.5, applied last, to the rounded total ---
    for pen in r.penalties:
        out.append({
            "part": "penalty", "ref": "S7F 13.5",
            "formula": pen.describe(),
            "substituted": (
                f"{pen.percent_own:g}% of {gross:.1f}" if pen.percent_own else
                f"{pen.percent_task:g}% of the task pot" if pen.percent_task else
                f"flat deduction"),
            "value": -pen.applied, "engine": None,
        })
    if r.penalties:
        out.append({
            "part": "TOTAL", "ref": "S7F 12 + 13.5",
            "formula": "subtotal less penalties, floored at zero",
            "substituted": f"{gross:.1f} − {r.penalty_points:.1f}",
            "value": round(max(0.0, gross - r.penalty_points), 1),
            "engine": r.total_points,
        })
    return out
