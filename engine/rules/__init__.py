"""One file per scoring rule, in the order the rules are applied.

The point of this package is reviewability. GAP is not one calculation, it is
about fifteen, chained; when a published result disagrees the useful question
is never "is the scoring right" but "which of the fifteen is wrong". Every
element below is a separate module holding one pure function, its Sporting Code
reference, the paragliding/hang-gliding difference where there is one, and an
honest note on how (or whether) it has been verified.

Read `./run.py --rules` for the same list with current status, or open the
files: they are meant to be checked against the Code side by side.

THE PIPELINE

    geometry, before any scoring at all            (ALGORITHMS, below)
      A1  earth model, projection, distance
      A2  S7F 9.1.1  tolerance zones
      A3  S7F 9.2.1  crossing detection and crossing time
      A4            segment through a cylinder      KNOWN GAP
      A5            route optimisation
      A6            distance still to fly
      A7  S7F 8.1   start selection

    per pilot, from their own points only          (engine/score.py)
      takeoff -> start -> control zones -> distance flown -> speed section
      -> leading-coefficient samples

    once the whole field is known                  (engine/scoring.py)
      1  S7F 5     competition parameters
      2  S7F 9.3   scored distance          (minimum-distance floor)
      3  S7F 10.1  launch validity      \\
      4  S7F 10.2  distance validity     >- multiply -> task validity
      5  S7F 10.3  time validity        /
      6  S7F 10.4  stopped-task validity     NOT IMPLEMENTED
      7  S7F 11    points allocation         -> the three pots
      8  S7F 12.1  distance points
      9  S7F 12.2  time points
     10  S7F 12.3  leading points
     11  S7F 12.4  arrival points            none, in paragliding
     12  S7F 13.1  elevated goal             scales time points
     13  S7F 13.2  ESS but not goal          zero time points, in paragliding
     14  S7F 13.3  early start               launch->SSS distance only
     15  S7F 13.4  stopped task              NOT IMPLEMENTED
     16  S7F 13.5  penalties                 subtracted from the total
     17  S7F 16    FTV                       NOT IMPLEMENTED (competition-level)

Steps 1-14 are pure functions of the field. 16 is applied last, to the rounded
total, because a percentage penalty has to be a percentage of something final.
"""

from __future__ import annotations

from dataclasses import dataclass

# --- the rule registry ----------------------------------------------------

IMPLEMENTED = "implemented"
MISSING = "NOT IMPLEMENTED"
NA_PG = "n/a for paragliding"


@dataclass(frozen=True, slots=True)
class Rule:
    """What one element is, where it lives, and how far to trust it."""

    step: int
    ref: str            # Sporting Code section, or "algorithm" for geometry
    title: str
    module: str         # file to open
    func: str           # the one public function in it
    status: str
    verified: str       # honest note: what actually checks this
    pg: str = ""        # paragliding-specific behaviour, where it differs
    group: str = "scoring"


# --- A. geometry and distance -------------------------------------------
# The algorithms every scored distance is built on. These are not points
# formulas, they are what the points formulas measure, and getting one wrong is
# uniform and invisible -- which is how the route optimiser was 8.3% long
# through two separate bugs. Section references are given where the Code is
# explicit (9.1.1, 9.2.1); the earth model and the optimisation algorithm are
# marked UNMAPPED because I do not have the Sporting Code text that specifies
# them and will not guess a section number.

ALGORITHMS: tuple[Rule, ...] = (
    Rule(0, "S7F 7.1", "The nine algorithms (reference)",
         "engine/rules/s7f_71_algorithms.py", "RouteOptimizer", "REFERENCE",
         "all nine of S7F 7.1 in one file, under the Code's own names, for "
         "diffing against the Code. Geodesics verified against Vincenty's "
         "published test line to 0.1 mm and against the analytic equator, "
         "meridian and quarter-meridian. NOT YET USED FOR SCORING — see the "
         "next row.", group="geometry"),
    Rule(1, "S7F 7.1", "Earth model — SPHERE vs ELLIPSOID",
         "engine/rules/earth_model.py", "haversine, Projection, dist", "WRONG",
         "S7F 7.1 specifies the WGS84 ELLIPSOID — four of its nine algorithms "
         "say so. The running engine uses the FAI sphere, R = 6 371 km. On "
         "this task that costs 144 m of route: the 7.1 pipeline gives 59,791 m "
         "against the engine's 59,647 m and an official 59,900 m, so switching "
         "would close 57% of the remaining distance gap.", group="geometry"),
    Rule(2, "S7F 9.1.1", "Tolerance zones", "engine/rules/cylinder.py",
         "inner_radius, outer_radius", IMPLEMENTED,
         "flat +/- 5 m at every radius, since radiusTolerance is 0.0% in the "
         "2026 edition. Checked at 100 m, 1 km and 17 km.", group="geometry"),
    Rule(3, "S7F 9.2.1", "Crossing detection and time",
         "engine/rules/cylinder.py", "zone_crossing, validates_zone",
         IMPLEMENTED,
         "compared against the copy inlined in the hot loop over 1,000,000 "
         "random segments; and --verify asserts every scored time in the field "
         "is a real tracklog timestamp, never interpolated.",
         "direction is irrelevant (S7F 6.2.1) -- any crossing validates.",
         group="geometry"),
    Rule(4, "gap", "Segment through a cylinder",
         "engine/rules/cylinder.py", "first_contact", "KNOWN GAP",
         "a segment whose two endpoints are both outside a small cylinder but "
         "which passes through it is NOT detected. Unreachable at 1 Hz; "
         "reachable with live telemetry at 0.1 Hz. first_contact() has the "
         "geometry but is deliberately unused, because closing this changes "
         "scored results and should be a decision.", group="geometry"),
    Rule(5, "S7F 7.1.3", "Route optimisation (PathFinder)",
         "engine/rules/route.py", "optimise_route", IMPLEMENTED,
         "S7F 7.1 calls this PathFinder and states it is Cartesian, which this "
         "is. Verified two independent ways — perturbation, and a shortest-path "
         "DP at a resolution the optimiser does not use. What is MISSING around "
         "it is 7.1.7 ProjectionCorrection and measuring the corrected path "
         "with 7.1.5 EllipsoidDistance; the engine measures in the plane.",
         group="geometry"),
    Rule(6, "—", "Distance still to fly",
         "engine/rules/distance_flown.py", "distance_to_goal, distance_flown",
         IMPLEMENTED,
         "the hot loop inlines this; --verify compares the two over every fix "
         "of the real field and requires exact equality.", group="geometry"),
    Rule(7, "S7F 8.1", "Start selection", "engine/rules/start_selection.py",
         "select_candidates", IMPLEMENTED,
         "seven hand-built synthetic flights with a known right answer, since "
         "real tracklogs only show what the pilots did. Covers re-starting, "
         "single-gate, the concentric fallback and EXIT-vs-ENTER equivalence.",
         "re-starting applies only to multi-gate races and time trials.",
         group="geometry"),
)


STAGES: tuple[Rule, ...] = (
    Rule(1, "S7F 5", "Competition parameters", "engine/rules/params.py",
         "GapParams", IMPLEMENTED,
         "values come from competition.json; the engine warns while they are "
         "placeholders. Not a formula, but every validity depends on it."),
    Rule(2, "S7F 9.1-9.4", "Control zones, crossings, distance, best time",
         "engine/rules/s7f_09_control_zones.py",
         "measurement_radius, scored_distance, best_time", "OPEN QUESTION",
         "all of Section 9 in one file. 9.1.1 tolerance checked at 100 m, 1 km "
         "and 17 km; 9.2.1 crossing times asserted to be real tracklog "
         "timestamps for the whole field; 9.3 matches the published result for "
         "113/129 pilots; 9.4.1 now a named rule instead of an unnamed "
         "condition. OPEN: does 9.3 measure distance to the nominal cylinder "
         "or to the tolerance boundary? The engine validated at r+5 m and "
         "measured to r, in four call sites that did not agree. One policy "
         "decides it now (MEASUREMENT_RADIUS); worth ~54 m of route per 5 m."),
    Rule(3, "S7F 10", "Task validity (10.1, 10.2, 10.3)",
         "engine/rules/s7f_10_task_validity.py",
         "launch_validity, distance_validity, time_validity, task_validity",
         "OPEN QUESTION",
         "all of Section 10 in one file, transcribed from the Code text. 10.2 "
         "and 10.3 match the official published result exactly. OPEN: (a) the "
         "published 10.1 cubic has NO clamp and yields 1.001 at LVR=1, this "
         "engine clamps because the official pots sum to exactly 1000; (b) "
         "10.3 says ESS where 9.4.1 says GOAL — different populations whenever "
         "ESS and goal are different cylinders; (c) PilotsPresent cannot come "
         "from tracklogs, so launch validity is 1.0 by construction until the "
         "meet director supplies it, which switches the Code's safety feature "
         "off."),
    Rule(6, "S7F 10.4", "Stopped-task validity",
         "engine/rules/s7f_13_special_cases.py", "stopped_task_validity",
         "NOT WIRED IN",
         "the formula is implemented and tested against the Code text, but "
         "nothing calls it: a stopped task needs an announcement time no input "
         "carries, and there is no stopped task in the reference data. Scoring "
         "one as though it ran to completion is the dangerous failure, so the "
         "engine reports 13.4 as absent instead."),
    Rule(7, "S7F 11", "Points allocation", "engine/rules/s7f_11_allocation.py",
         "allocate", "OPEN QUESTION",
         "transcribed from the Code text; 13 tests derived from that text, all "
         "passing, and all three pots reproduce the official published result "
         "exactly. TWO CONFLICTS with the Code: (a) Section 11 writes "
         "round(...,0) — WHOLE points — but the official shows 361.7/168.0/"
         "470.3 to one decimal, a difference of up to 0.5 pt per component per "
         "pilot; (b) Section 11 caps LeadingTimeRatio at 26%, but the official "
         "pots imply 26.32%, and no goal ratio reconciles it.",
         "leading takes the WHOLE non-distance share when nobody reaches goal "
         "— a discontinuity, not a limit: one finisher moves the leading pot "
         "by 70 points. Arrival is always zero."),
    Rule(8, "S7F 12", "Pilot points (12.1, 12.2, 12.3)",
         "engine/rules/s7f_12_pilot_points.py", "task_score", "OPEN QUESTION",
         "transcribed from the Code text; 14 tests from that text, including "
         "all nine cells of the published Table 2. 12.1 and 12.2 match the "
         "official result. OPEN, and it is 12.3.1: the LC FORMULA IS NOW "
         "CONFIRMED CORRECT term for term, and the ordering it produces "
         "matches the official at Spearman 0.9915 — but the SCALE is out by a "
         "factor near 2, which compresses the leading points. Figure 16 plots "
         "LCmin from 1.0 to 2.0; this engine gets 0.468. No factor is applied.",
         "distance points are purely linear (12.1.1 excludes paragliding from "
         "the difficulty calculation); bestTime is goal-restricted (12.2.1)."),
    Rule(12, "S7F 13", "Special cases (13.1–13.4)",
         "engine/rules/s7f_13_special_cases.py", "goal_altitude_factor",
         "OPEN QUESTION",
         "all of Section 13 in one file, transcribed from the Code; 32 tests "
         "from that text. 13.1 CONFIRMED against the published Low P column "
         "(the 200 m band inverts consistently at three very different "
         "heights). 13.4 STOPPED TASKS ARE NOW IMPLEMENTED — validity, scored "
         "time window, the ESS time-points transfer and the altitude bonus — "
         "but NOT WIRED INTO THE PIPELINE and never run on a real stopped "
         "task. OPEN: 13.3 defines an early start but does not state the "
         "consequence; this engine caps them at launch-to-SSS while 12.1 read "
         "literally gives them full distance, a difference of the whole task. "
         "13.2 has never been exercised (ESS is the goal cylinder here).",
         "13.2 costs paragliders 100% of time points, hang-gliders 20%."),
    Rule(16, "S7F 13.5", "Penalties", "engine/rules/penalties.py",
         "apply_penalties", IMPLEMENTED,
         "reproduces the AIRSPACE -100% on pilot 1380 in the reference result. "
         "Added after the official comparison showed the engine ignoring it."),
    Rule(17, "S7F 16", "FTV (competition-level)", "engine/rules/ftv.py",
         "ftv_scores", MISSING,
         "competition-level, not task-level; does not affect a single task's "
         "result."),
)


ALL: tuple[Rule, ...] = ALGORITHMS + STAGES


def by_ref(ref: str) -> Rule | None:
    for r in ALL:
        if r.ref == ref:
            return r
    return None


# --- re-exports, so callers can say `from engine import rules` ------------
# Each name below is defined in exactly one module above. This package is the
# index; the modules are the content.

from . import (cylinder, distance_flown, earth_model, route,  # noqa: E402
               s7f_09_control_zones, s7f_10_task_validity,
               s7f_11_allocation, s7f_12_pilot_points,
               s7f_13_special_cases, s7f_71_algorithms, start_selection)
from .s7f_13_special_cases import (early_start_distance,  # noqa: E402
                                   ess_no_goal_factor,
                                   goal_altitude_factor,
                                   is_early_start)
from .s7f_12_pilot_points import (distance_points,  # noqa: E402
                                  leading_points, max_time_for,
                                  round1, speed_fraction,
                                  task_score, time_points)
from .s7f_11_allocation import (Allocation, allocate,  # noqa: E402
                                arrival_weight, available_points,
                                distance_weight, goal_ratio,
                                leading_weight, time_weight)
from .s7f_09_control_zones import (best_time,  # noqa: E402
                                   counts_for_best_time,
                                   measurement_radius,
                                   scored_distance)
from .params import GapParams                                      # noqa: E402
from .penalties import Penalty, apply_penalties, load_penalties    # noqa: E402
# NOTE: leading_points is deliberately NOT imported from here. S7F 12.3 rounds
# it to one decimal, so the authoritative one lives in s7f_12_pilot_points and
# importing this module's unrounded namesake afterwards would silently shadow
# it — which it did, until an invariant caught the LCmin pilot scoring
# 168.0110437 out of a 168.0 pot.
from .points_leading import (leading_coefficient,  # noqa: E402
                             leading_factor, leading_from_partial,
                             leading_from_partial_hump_v2a,
                             leading_partial, leading_partial_hump_v2a,
                             leading_weight, weight_integral)
from .s7f_10_task_validity import (best_time_to_ess,  # noqa: E402
                                   distance_validity, launch_validity,
                                   task_validity, time_validity)

__all__ = [
    "Rule", "STAGES", "ALGORITHMS", "ALL", "by_ref",
    "IMPLEMENTED", "MISSING", "NA_PG",
    "earth_model", "cylinder", "route", "distance_flown", "start_selection",
    "s7f_71_algorithms", "s7f_09_control_zones", "s7f_10_task_validity",
    "s7f_11_allocation", "s7f_12_pilot_points",
    "s7f_13_special_cases", "is_early_start", "goal_ratio",
    "task_score", "max_time_for", "round1", "distance_weight",
    "leading_weight", "time_weight", "arrival_weight", "available_points",
    "task_validity", "best_time_to_ess",
    "best_time", "counts_for_best_time", "measurement_radius",
    "GapParams", "Allocation", "allocate",
    "scored_distance", "early_start_distance",
    "launch_validity", "distance_validity", "time_validity",
    "distance_points", "time_points", "speed_fraction",
    "leading_points", "leading_coefficient", "leading_factor",
    "leading_partial", "leading_from_partial",
    "leading_partial_hump_v2a", "leading_from_partial_hump_v2a",
    "leading_weight", "weight_integral",
    "goal_altitude_factor", "ess_no_goal_factor",
    "Penalty", "apply_penalties", "load_penalties",
]
