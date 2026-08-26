"""S7F 12 — PILOT POINTS, all in one file.

    TaskScore_p = round(DistancePoints_p + TimePoints_p
                        + LeadingPoints_p + ArrivalPoints_p, 1)

    12.1  distance points
    12.2  time points
    12.3  leading points
    12.3.1  the leading coefficient
    12.4  arrival points   — [PG] none, see rules/s7f_11_allocation.arrival_weight

ROUNDING. Section 12 says each component is "rounded to one decimal place" AND
that TaskScore is round(sum, 1). So the components are rounded FIRST and the
already-rounded values are then summed. That is not the same as rounding the
sum of the unrounded parts, and it is what the published result does: Malecki's
361.7 + 168.0 + 470.3 - 1.5 = 998.5 exactly.

================================================================================
  12.3.1 SETTLED THE FORMULA AND LEFT A FACTOR OF ~2 UNEXPLAINED
================================================================================

The leading coefficient was this engine's one element flagged as wrong. With
12.3.1 in hand, the structure is CONFIRMED CORRECT — the formula implemented
here is the formula in the Code, term for term. What remains is a scale.

  * Spearman rank correlation between this engine's LC ordering and the
    official leading points: 0.9915 over 129 pilots. The order is right.

  * But the SPREAD is too narrow. LeadingFactor is extremely sensitive near
    LCmin, so a compressed LC range compresses the points: this engine gives
    the last finisher ~62 leading points where the official gives 6.6.

  * Fitting a single multiplier k on LC against the official result gives
    k = 2.05 by regression (r^2 0.974) and k = 2.35 by minimising the points
    error, which takes the mean error from 18.1 pt/pilot to 2.7.

  * Corroboration from the Code itself: Figure 16 plots LeadingFactor for
    LCmin = 1.0, 1.25, 1.5, 1.75, 2.0. This engine's LCmin on the reference
    task is 0.468 — below the entire plotted range. At k = 2 it is 0.94, at the
    bottom of it.

Two candidate homes for the factor, neither confirmable from the text quoted:

    a) the 1800 in the denominator being 900        -> exactly x2.000
    b) weight() normalised to unit area             -> x1.485
       (integral of weight over [0,1] = 0.673355, computed two ways)

(a) matches the regression almost exactly. (b) does not.

NO FACTOR IS APPLIED. Fitting a constant to one task's published output and
shipping it as correct is the mistake that left the route optimiser 8.3% wrong
through two separate bugs, and it would be worse here because it would look
like agreement. What is needed is the sentence in 12.3.1 that defines the
denominator, or a second competition to test a hypothesis against.

Set LC_SCALE below to experiment. It defaults to 1.0.

================================================================================
  maxTime
================================================================================

12.3.1: "The times used for this calculation are given in seconds from the
first start gate time (as defined for the task), to the time when the last
pilot reached ESS. For pilots who land out after the last pilot reached ESS,
the calculation keeps going until they land."

So maxTime is the LAST ESS TIME, extended per pilot to their own landing if
they came down later. The engine previously used a single field-wide
max(last outlanding, last ESS) for everybody, which inflates missingArea for
every pilot who landed before the slowest one. MAX_TIME_RULE selects; the
default now follows the Code.
"""

from __future__ import annotations

import math

from .points_leading import (leading_factor, leading_from_partial,  # noqa: F401
                             leading_partial, leading_weight, weight_integral)

# --- policies -------------------------------------------------------------

LC_SCALE = 1.0              # see the note above. 1.0 = the Code as written.
MAX_TIME_RULE = "code"      # "code" = per-pilot max(lastESS, own landing)
                            # "field" = the old field-wide max(lastOut, lastESS)


def round1(x: float) -> float:
    """Round to one decimal place. S7F 12 does this to every component."""
    return round(x, 1)


# =========================================================================
#  12.1  Distance points
# =========================================================================
#
#     distance_p = max(MinimumDistance,
#                      taskDistance - min over the pilot's track points of
#                                     shortestDistanceToGoal(trackPoint))
#
#     DistancePoints_p = distance_p / bestDistance * AvailableDistancePoints
#
# "The distance considered for each pilot to calculate distance points is that
#  pilot's best distance along the course line, up until the pilot landed or
#  the task deadline was reached, whichever comes first."
#
# THE MINIMUM OVER THE WHOLE TRACK, not the value at landing — a pilot who
# reaches a point and drifts back keeps what they earned. That is also what
# makes the live leaderboard monotone in time.
#
# "The available distance points are assigned to each pilot LINEARLY, based on
#  the pilot's distance flown in relation to the best distance flown in the
#  task." Confirms [PG] S7F 12.1.1: no difficulty calculation. A
# difficulty-adjusted implementation would be wrong here, not merely different.


def distance_points(distance: float, best_distance: float,
                    available_distance: float) -> float:
    """S7F 12.1, rounded to one decimal place."""
    if best_distance <= 0:
        return 0.0
    return round1(min(distance / best_distance, 1.0) * available_distance)


# =========================================================================
#  12.2  Time points
# =========================================================================
#
#     SpeedPoints_p = max(0, 1 - ((Time_p - bestTime) / sqrt(bestTime))^(5/6))
#     TimePoints_p  = SpeedPoints_p * AvailableTimePoints
#
# All times in HOURS. "Slow pilots will get zero points for speed if their time
# to complete the speed section is equal to or longer than the fastest time
# plus the square root of the fastest time."
#
# 12.2.1 settles a question 10.3 left open: "The best time is defined as the
# time of the fastest pilot over the speed section WHO ALSO REACHED THE GOAL."
# One bestTime, goal-restricted, used by both 12.2 and 10.3.
#
# Published Table 2, which --verify checks against rather than against a
# snapshot of this code:
#
#     fastest   80% points   50% points   0 points
#     1:00      1:08:42      1:26:07      2:00:00
#     2:00      2:12:18      2:36:56      3:24:51
#     3:00      3:15:04      3:45:14      4:43:55
#
# S7F 13.1 (elevated goal) and 13.2 (ESS but not goal) both scale the result;
# they live in their own files and are passed in here as factors.


def speed_fraction(pilot_time: float, best_time: float | None) -> float:
    """S7F 12.2 — the fraction of the time pot. Times in SECONDS."""
    if best_time is None or best_time <= 0:
        return 0.0
    bt = best_time / 3600.0
    pt = pilot_time / 3600.0
    if pt <= bt:
        return 1.0
    x = (pt - bt) / math.sqrt(bt)
    return max(0.0, 1.0 - x ** (5.0 / 6.0))


def time_points(pilot_time: float, best_time: float | None,
                available_time: float, altitude_factor: float = 1.0,
                ess_no_goal_factor: float = 1.0) -> float:
    """S7F 12.2 with the 13.1 and 13.2 modifiers, rounded to one decimal."""
    return round1(speed_fraction(pilot_time, best_time) * available_time
                  * altitude_factor * ess_no_goal_factor)


# =========================================================================
#  12.3  Leading points
# =========================================================================
#
#     LCmin           = min over all pilots who flew of LC_p
#     LeadingFactor_p = max(0, 1 - cuberoot((LC_p - LCmin)^2 / sqrt(LCmin)))
#     LeadingPoints_p = LeadingFactor_p * AvailableLeadingPoints
#
# "Leading points are awarded to encourage pilots to start early and to reward
#  the risk involved in flying in the leading group. Pilots will get leading
#  points even if they landed before goal or the end of speed section."
#
# The pilot at LCmin always takes the whole pot, since the factor is exactly 1
# there. That identity is what lets LeadingTimeRatio be read back out of a
# published result (see rules/s7f_11_allocation.Allocation).
#
# CHECK THIS: the denominator inside the cube root. The transcription of 12.3
# available here is garbled around it — sqrt(LCmin) is what this engine uses
# and what the surrounding text is consistent with, but LCmin^3 and a bare
# LCmin are both readable in the source. Tested against the official result,
# sqrt(LCmin) and cuberoot-of-LCmin are close and LCmin^3 is much worse; none
# is decisive while the LC scale question above is open.


def leading_points(lc: float, lc_min: float, available_leading: float) -> float:
    """S7F 12.3, rounded to one decimal place."""
    if lc <= 0 or lc_min <= 0:
        return 0.0
    return round1(leading_factor(lc, lc_min) * available_leading)


# =========================================================================
#  12.3.1  The leading coefficient
# =========================================================================
#
#     bestTrackPoint_p = the track point with the shortest distance to ESS
#
#     minToESS(tp_0) = distanceOfSpeedSection
#     minToESS(tp_i) = min(minToESS(tp_{i-1}), distToESS(tp_i))       for i > 0
#
#     taskTime(tp)   = min(tp.time, taskDeadline) - firstTaskStartGate
#
#     done(t)        = 1 - minToESS(t) / distanceOfSpeedSection
#
#     leadingArea_p  = SUM over tp_i in trackPointsInSS_p of
#                        minToESS(tp_i) * taskTime(tp_i)
#                        * integral of weight(x) dx
#                          from done(tp_{i-1}) to done(tp_i)
#
#     missingArea_p  = minToESS(bestTrackPoint_p) * maxTime
#                        * integral of weight(x) dx
#                          from done(bestTrackPoint_p) to 1
#
#     LC_p = (leadingArea_p + missingArea_p) / (1800 * speedSectionDistance)
#
#     weight(v)        = weightRising(1-v) * weightFalling(1-v)
#     weightRising(v)  = (1 - 10^(9v-9))^5
#     weightFalling(v) = (1 - 10^(-3v))^2
#
# Distances in KILOMETRES, times in SECONDS.
#
# THE GRAPH NEVER GOES BACK. minToESS is a running minimum, so flying away from
# goal for a while costs nothing and gains nothing — the graph holds flat at
# the best distance already reached. This is also why summing only over the
# points where minToESS strictly DECREASED is exact rather than an
# approximation: where done does not change, the integral between consecutive
# done values is zero and the term contributes nothing.
#
# 12.3.2 explains what a gap in a tracklog costs: "Missing parts are calculated
# as if the dotted line was the actual track log, so LC becomes bigger, lowering
# the leading points for that pilot." A pilot with a telemetry gap is penalised,
# and a pilot who lands just short of goal "will be less penalised and could
# even get full leading points if he led for a long while".
#
# The implementation is in engine/rules/points_leading.py, split into
# leading_partial() (everything that depends only on the pilot's own track) and
# leading_from_partial() (the missingArea term, which needs maxTime). The two
# together are exactly leading_coefficient(); --verify asserts it on 2,000
# random tracks. The split is what lets a worker process score a pilot in
# isolation.


def max_time_for(pilot_last_task_time: float, last_ess_task_time: float,
                 field_max_task_time: float) -> float:
    """S7F 12.3.1 — the maxTime that goes into missingArea, for ONE pilot.

    "to the time when the last pilot reached ESS. For pilots who land out after
     the last pilot reached ESS, the calculation keeps going until they land."

    So: the last ESS time, extended to this pilot's own landing if they came
    down later. MAX_TIME_RULE == "field" restores the previous behaviour, a
    single field-wide maximum applied to everyone, which inflates missingArea
    for every pilot who landed before the slowest one.
    """
    if MAX_TIME_RULE == "field":
        return field_max_task_time
    if MAX_TIME_RULE != "code":
        raise ValueError(f"MAX_TIME_RULE must be 'code' or 'field', "
                         f"got {MAX_TIME_RULE!r}")
    return max(last_ess_task_time, pilot_last_task_time)


def leading_coefficient(lead_area: float, min_to_ess: float,
                        speed_distance_km: float, max_time: float) -> float:
    """S7F 12.3.1, with the LC_SCALE experiment applied. See the note at top."""
    lc = leading_from_partial(lead_area, min_to_ess, speed_distance_km, max_time)
    return lc * LC_SCALE


# =========================================================================
#  12  Task score
# =========================================================================


def task_score(distance_pts: float, time_pts: float, leading_pts: float,
               arrival_pts: float = 0.0) -> float:
    """S7F 12 — the pilot's score for the task.

    Each component is already rounded to one decimal (S7F 12.1, 12.2, 12.3);
    this rounds the sum. S7F 13.5 penalties are applied AFTER this, to the
    rounded total — see rules/penalties.py.
    """
    return round1(distance_pts + time_pts + leading_pts + arrival_pts)
