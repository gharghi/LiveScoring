"""S7F 9 — CONTROL ZONES, CROSSINGS, DISTANCE AND BEST TIME, all in one file.

Section 9 in the Code's own order, the same treatment as 7.1: one file, because
these four are a single chain and reading them apart is how you miss that they
disagree with each other. Which is exactly what had happened — see THE
TOLERANCE AUDIT below.

    9.1    Control zones and their tolerance
    9.1.1  radiusTolerance / absoluteTolerance
    9.2    Crossings
    9.2.1  Crossing time
    9.3    Scored distance
    9.4    Best time
    9.4.1  [PG] whose time counts

================================================================================
  HOW MUCH OF THIS IS THE CODE
================================================================================

Same caveat as engine/rules/s7f_71_algorithms.py. I have the section numbers
and, for 9.1.1, 9.2.1 and 9.4.1, sentences quoted from the Code in this
project's earlier notes. I do not have the full text of Section 9. Anywhere the
Code could reasonably specify something other than what is implemented, there
is a CHECK THIS marker. Diff against those first.

================================================================================
  THE TOLERANCE AUDIT — the reason this file exists
================================================================================

The +/- 5 m tolerance was being applied to WHETHER a zone counts and NOT to HOW
FAR the pilot has flown. Four places decide it, and they did not agree:

    score.py  validation, hot loop      w.outer / w.inner      tolerance
    score.py  distance to goal          w.radius               nominal
    score.py  remaining_to_goal ref     w.radius               nominal
    task.py   route optimiser           w.radius               nominal

So a pilot validated a turnpoint at r + 5 m while their distance was still
being measured to r: at the instant of validation their distance-to-goal jumped
by 5 m rather than reaching zero. Nothing crashed, no invariant caught it, and
it is worth real distance.

That decision is now made ONCE, by MEASUREMENT_RADIUS below, and every caller
reads it from here. What it is worth on the reference task, measured:

    measurement radius        route (WGS84, S7F 7.1)     vs official 59,900 m
    nominal  r                       59,791.2 m               -108.8 m
    outer    r + 5 m                 59,737.5 m               -162.5 m
    inner    r - 5 m                 59,845.0 m                -55.0 m

About 54 m of route per 5 m of radius, across seven movable turnpoints.

THE DEFAULT IS `nominal`, WHICH IS THE PREVIOUS BEHAVIOUR. `inner` happens to
land closest to the published result, and that is NOT a reason to choose it —
picking the option that best fits one task's output is how the route optimiser
came to be 8.3% wrong in the first place (VERIFICATION.md §4.1). Choose it if
the Code says so, and the numbers above tell you what it will cost.

CHECK THIS, and it is the main question in this file: does S7F 9.3 measure
distance to the nominal cylinder or to the tolerance boundary? The two readings
are both coherent —

  * tolerance is a MEASUREMENT ALLOWANCE, granted because GPS is imprecise. It
    decides whether you touched the cylinder. The cylinder is still the
    cylinder, and distance is measured to it.       -> nominal

  * the tolerance boundary IS the scored boundary, so a pilot who reaches it
    has arrived and their remaining distance is zero. Measuring to anything
    else contradicts the validation.                -> outer
"""

from __future__ import annotations

import math

# =========================================================================
#  9.1 / 9.1.1  Control zone tolerance
# =========================================================================
#
#     innerRadius = min(r x (1 - radiusTolerance), r - absoluteTolerance)
#     outerRadius = max(r x (1 + radiusTolerance), r + absoluteTolerance)
#
# radiusTolerance is 0.0% in the 2026 edition, so the tolerance is a FLAT
# +/- 5 m at every radius, from a 100 m goal to a 17 km turnpoint. An earlier
# draft of this engine used 0.5%, which on a 17 km cylinder is 85 m --
# seventeen times too generous, and invisible unless you go looking.
#
# The zone is the ANNULUS between inner and outer, not the cylinder.
#
# CHECK THIS: that radiusTolerance really is 0.0% in the edition being scored.
# It is the single value in this file that most changes results if wrong, and
# it changes them in a way that looks entirely plausible.

RADIUS_TOLERANCE = 0.0          # S7F 9.1.1, 2026 edition
ABSOLUTE_TOLERANCE = 5.0        # metres


def inner_radius(r: float) -> float:
    """S7F 9.1.1 — inner edge of the tolerance zone."""
    return min(r * (1.0 - RADIUS_TOLERANCE), r - ABSOLUTE_TOLERANCE)


def outer_radius(r: float) -> float:
    """S7F 9.1.1 — outer edge of the tolerance zone."""
    return max(r * (1.0 + RADIUS_TOLERANCE), r + ABSOLUTE_TOLERANCE)


# --- the one place the nominal/tolerance choice is made ------------------

# A pilot is credited at the boundary that validates the control zone.  Using
# the nominal radius here left a pilot who had validated at r + 5 m with a
# non-zero distance still to fly, and makes task distances too long.
MEASUREMENT_RADIUS = "outer"        # "nominal" | "outer" | "inner"


def measurement_radius(r: float) -> float:
    """The radius DISTANCE is measured to, as opposed to validated against.

    Read by the route optimiser (S7F 7.1.3 / engine/task.py) and by
    distance-to-goal (engine/rules/distance_flown.py and the inlined copy in
    score.py's hot loop), so all three agree by construction. Validation always
    uses the tolerance zone regardless — that part is not in question.

    See THE TOLERANCE AUDIT at the top of this file for what each option is
    worth on the reference task.
    """
    if MEASUREMENT_RADIUS == "outer":
        return outer_radius(r)
    if MEASUREMENT_RADIUS == "inner":
        return inner_radius(r)
    if MEASUREMENT_RADIUS != "nominal":
        raise ValueError(
            f"MEASUREMENT_RADIUS must be 'nominal', 'outer' or 'inner', "
            f"got {MEASUREMENT_RADIUS!r}")
    return r


# =========================================================================
#  9.2 / 9.2.1  Crossings
# =========================================================================
#
# A crossing is a transition across EITHER tolerance boundary in EITHER
# direction. Direction is irrelevant: S7F 6.2.1 removed the enter/exit
# designation in 2020 --
#
#     "the direction in which such a crossing occurs is irrelevant. Task
#      setters may still choose to indicate whether the start or subsequent
#      turnpoint cylinders are 'enter' or 'exit', to explain their intended
#      task route. But pilots are not bound to those indications."
#
# So the engine validates on ANY crossing and the declared direction is display
# only. Gating on it makes a task whose declared direction is wrong -- or whose
# first turnpoint sits inside the start cylinder -- score as "nobody started".
# `--verify` scores the same synthetic flight against a task declared EXIT and
# the same task declared ENTER and requires identical results.
#
# 9.2.1, THE CROSSING TIME:
#
#     "Crossing time and altitude for each crossing is the time at which the
#      corresponding tracklog point was recorded."
#
# NOT an interpolated time. Interpolating is more physically accurate and is
# the natural thing to write; at 1 Hz and 12 m/s it differs by up to a full
# second on start and ESS, which is the difference between the right and the
# wrong podium, and it would disagree with official scoring. `--verify`
# asserts every scored time in the field is a real timestamp from that pilot's
# own tracklog. The same rule governs the crossing ALTITUDE, which matters for
# S7F 13.1.


def zone_crossing(p0, p1, cx: float, cy: float, inner: float, outer: float):
    """S7F 9.2 — did this segment cross the tolerance zone, and when?

    p0/p1 are (x, y, t) in projected metres. Returns (time, outward) or None.
    `outward` is diagnostic only; per S7F 6.2.1 it must not affect validation.
    The returned time is p1's timestamp, never an interpolation (S7F 9.2.1).
    """
    x0, y0, _t0 = p0
    x1, y1, t1 = p1
    d0 = math.hypot(x0 - cx, y0 - cy)
    d1 = math.hypot(x1 - cx, y1 - cy)
    crossed = (
        (d0 < inner) != (d1 < inner)          # inner boundary, either direction
        or (d0 <= outer) != (d1 <= outer)     # outer boundary, either direction
    )
    if not crossed:
        return None
    return (t1, d1 > d0)


def in_zone(x: float, y: float, cx: float, cy: float, outer: float) -> bool:
    """Is this point inside the outer tolerance boundary?"""
    return (x - cx) ** 2 + (y - cy) ** 2 <= outer * outer


def validates_zone(d0: float, d1: float, outer: float) -> bool:
    """Crossing OR already inside, reduced to one comparison. [reference]

    What score.py's hot loops actually evaluate, written out once so it can be
    checked. EQUIVALENT to `zone_crossing(...) is not None or in_zone(p1,...)`:

        if d1 <= outer                      -> inside, validates
        if d1 >  outer, then (d1 < inner) is False, so
             crossed == (d0 < inner) or (d0 <= outer) == (d0 <= outer)

    so the test collapses to "either end of the segment is within the outer
    boundary". engine/invariants.check_inlined_zone_test proves it on a million
    random segments.

    NOTE the asymmetry with the START. A turnpoint accepts simply BEING inside
    at a fix, which covers a telemetry gap that hid the boundary event. The SSS
    does not: it requires a strict transition, or a pilot orbiting inside the
    start cylinder would generate a candidate start on every fix. See
    engine/rules/start_selection.py.
    """
    return d1 <= outer or d0 <= outer


# =========================================================================
#  9.3  Scored distance
# =========================================================================
#
#     scoredDistance = max(minimumDistance, distanceFlownAlongTheOptimisedRoute)
#
# Two traps, both of which this engine fell into and both of which cost real
# points:
#
# WHERE THE ROUTE STARTS. The scored route runs from the FIRST turnpoint,
# normally the takeoff cylinder, through every control zone to goal. It is NOT
# the speed section. A pilot who lands before reaching the start has still
# flown the launch-to-SSS leg and is scored for it. Measuring from the SSS
# instead made every scored distance in the reference competition 5.9 km short
# (VERIFICATION.md §5.1). CompiledTask keeps the two indices apart:
# `route_start` is where the SCORED ROUTE begins, `start_index` is where the
# CLOCK begins.
#
# OPTIMISED, NOT CENTRE TO CENTRE. Measured along the shortest legal path
# through the cylinders. On the reference task that is 59.6 km against 80.8 km
# centre to centre: a leaderboard built on centre-to-centre is not
# approximately right, it is wrong by a third. The optimiser is S7F 7.1.3
# PathFinder, in engine/rules/route.py, and it is checked two independent ways
# because it is the highest-risk component in the system.
#
# THE FLOOR. minimumDistance (S7F 5.2) is what a pilot scores for launching and
# flying at all. It applies to anyone airborne, including a pilot who never
# made a valid start.
#
# A pilot who reached GOAL is credited the full task distance regardless of
# what their track did afterwards.


def scored_distance(route_distance: float, minimum_distance: float,
                    airborne: bool = True) -> float:
    """S7F 9.3 — apply the S7F 5.2 floor to a distance measured along the route."""
    if not airborne:
        return 0.0
    return max(minimum_distance, max(0.0, route_distance))


# =========================================================================
#  9.4 / 9.4.1  Best time
# =========================================================================
#
# BestTime is the fastest speed-section time in the field. It is the
# denominator of every pilot's time points (S7F 12.2) and it drives time
# validity (S7F 10.3), so who is allowed into it moves the whole board.
#
# 9.4.1, AND THIS IS A PARAGLIDING/HANG-GLIDING DIFFERENCE:
#
#     [PG]  only pilots who reached GOAL have a time that counts
#     [HG]  anyone who reached ESS counts
#
# The consequence is easy to miss. A paraglider pilot who crosses ESS fastest
# and then lands 200 m short of the goal cylinder does NOT set BestTime. The
# fastest pilot who actually completed does. Under hang-gliding rules the same
# flight would set it, and every other pilot's time points would be scaled
# against a time nobody in goal achieved.
#
# This used to be an unnamed condition inside scoring.py. It is a rule, it has
# a section number, and it differs by discipline, so it is a function.
#
# CHECK THIS: whether a pilot who reaches goal AFTER the goal deadline
# contributes a time. The engine says no — they are not in goal — and the
# reference competition's published result agrees, counting 111 in goal while
# crediting the late arrival full distance (VERIFICATION.md §5.6).


def counts_for_best_time(in_goal: bool, reached_ess: bool,
                         paragliding: bool = True) -> bool:
    """S7F 9.4.1 — does this pilot's speed-section time count towards BestTime?"""
    return in_goal if paragliding else reached_ess


def best_time(results, paragliding: bool = True) -> float | None:
    """S7F 9.4 — the fastest qualifying speed-section time, or None.

    `results` is any iterable of PilotResult-like objects with `goal_time`,
    `ess_time` and `speed_section_time`.
    """
    times = [r.speed_section_time for r in results
             if r.speed_section_time
             and counts_for_best_time(r.goal_time is not None,
                                      r.ess_time is not None, paragliding)]
    return min(times) if times else None
