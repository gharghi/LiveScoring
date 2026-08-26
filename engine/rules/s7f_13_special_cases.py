"""S7F 13 — SPECIAL CASES, all in one file.

    13.1  Underflying an elevated goal
    13.2  ESS but not goal
    13.3  Early start
    13.4  Stopped tasks          (13.4.1, 13.4.3, 13.4.4, 13.4.5, 13.4.6)
    13.5  Penalties

The rare cases, which is exactly why they are worth writing out: they are the
ones nobody has a worked example of, the ones no reference competition
exercises, and the ones that decide a result when they finally happen.

STATUS, honestly. 13.1, 13.2 and 13.5 are implemented and 13.1 is confirmed
against a published result. 13.4 is implemented HERE, from the Code text, but
is NOT WIRED INTO THE SCORING PIPELINE and has never run on a real stopped
task — there is no stopped task in the reference data. 13.3's definition is
confirmed; its consequence is not (see below).

Not covered by any of this: nothing in Section 13 has a worked example in the
Code, so every number below is checked against the Code's own words or against
an internal identity, never against another implementation.
"""

from __future__ import annotations

import math  # noqa: F401  (used by stopped_distance_validity)

# =========================================================================
#  13.1  Underflying Elevated Goal
# =========================================================================
#
#     goal.altitude   the published goal altitude, metres AMSL
#     elevation       the drop to the lower goal limit; 300 m by default,
#                     up to 1000 m per task
#     crossing        the pilot's altitude when crossing goal
#
#     crossing <= goal.altitude                -> GoalAltitudeFactor = 0.8
#     crossing >= goal.altitude + elevation    -> GoalAltitudeFactor = 1
#     otherwise, with  Ag = (crossing - goal.altitude) / elevation
#         GoalAltitudeFactor = 0.8 + 0.6 Ag - 0.6 Ag^2 + 0.2 Ag^3
#
#     TimePoints.final = TimePoints * GoalAltitudeFactor
#
# TIME POINTS ONLY. Distance and leading are untouched.
#
# The curve is continuous at both ends: Ag = 0 gives exactly 0.8 and Ag = 1
# gives exactly 1.0. Note the FLOOR — crossing a kilometre below goal still
# keeps 0.8 of the time points, not zero.
#
# The crossing ALTITUDE is governed by S7F 9.2.1 exactly as the crossing time
# is: it is the altitude recorded at that tracklog point, not an interpolation.
#
# `elevation` is per task and is NOT in the .xctsk, so it comes from
# competition.json:  "tasks": {"<name>": {"elevated_goal_m": 200}}
#
# CONFIRMED against the published result for the reference task: inverting the
# curve on three pilots at very different crossing heights (150 m, 18 m, 6 m)
# all gives elevation = 200, and none gives 300. A pilot 13 m BELOW goal gets
# exactly 0.8, the floor. 40 of 111 finishers are affected.


def goal_altitude_factor(crossing_alt: float, goal_alt: float,
                         elevation: float = 300.0) -> float:
    """S7F 13.1 — the factor applied to TIME POINTS only."""
    if elevation <= 0:
        return 1.0
    if crossing_alt <= goal_alt:
        return 0.8
    if crossing_alt >= goal_alt + elevation:
        return 1.0
    ag = (crossing_alt - goal_alt) / elevation
    return 0.8 + 0.6 * ag - 0.6 * ag ** 2 + 0.2 * ag ** 3


# =========================================================================
#  13.2  ESS but not goal
# =========================================================================
#
# "Reaching goal is seen as 'validating' one's speed section performance. A
#  pilot who does not reach goal after reaching ESS will lose a portion of his
#  time points... He will also score FULL DISTANCE POINTS for the distance
#  covered and his FULL LEADING POINTS."
#
#     [PG]  0%   — no time points at all
#     [HG]  80%  — recommended default, changeable by local regulations
#
# "The timepoint penalty for not reaching goal is seen as a safety measure,
#  since it encourages pilots to plan their final glide to ESS with enough
#  altitude to safely reach goal." And for paragliding specifically, 0% "as
#  this discourages high-speed final glides low to the ground."
#
# So in paragliding, crossing ESS and landing short of the goal cylinder costs
# the ENTIRE time component — on a task where time is worth 470 of 1000 points
# that is close to half the available score. Distance and leading are
# explicitly untouched, which is easy to get wrong in the other direction.
#
# NEVER EXERCISED. On the reference task ESS and goal are the same cylinder, so
# the case cannot arise. This rule has not been checked against any real
# result.


def ess_no_goal_factor(in_goal: bool, reached_ess: bool,
                       configured_factor: float = 0.0) -> float:
    """S7F 13.2 — the factor applied to time points. 1.0 for a pilot in goal."""
    if in_goal:
        return 1.0
    if reached_ess:
        return configured_factor
    return 0.0


# =========================================================================
#  13.3  Early start
# =========================================================================
#
# "An early start occurs if a pilot's last SSS control zone crossing occurred
#  before the first (or only) start gate time."
#
# That is the whole of 13.3 as transcribed: a DEFINITION, with no consequence
# attached.
#
# THE DEFINITION IS CONFIRMED and it is the important half, because it settles
# that the LAST crossing decides. A pilot who leaves early, returns and leaves
# again after the gate has made a valid start and is an ordinary pilot; only
# someone who left and never came back is an early starter. That selection is
# in engine/rules/start_selection.py.
#
# THE CONSEQUENCE IS NOT CONFIRMED, and this engine may be wrong about it.
#
# The engine scores an early starter on the launch-to-SSS distance alone. That
# came from an assumption made before the Code text was available, and nothing
# in 13.3 supports it. Reading 12.1 literally points the other way:
#
#     distance_p = max(MinimumDistance,
#                      taskDistance - min over the pilot's track points of
#                                     shortestDistanceToGoal(trackPoint))
#
# with no requirement of a valid start anywhere in it. On that reading an early
# starter who flew the whole course would score FULL DISTANCE POINTS, and lose
# only their time points and leading points — which they lose automatically,
# having no valid start and therefore no speed section and no leading
# coefficient.
#
# The difference is large. On the reference task, launch-to-SSS is 4,860 m
# against a minimum distance of 5,000 m, so the engine's reading gives an early
# starter the MINIMUM DISTANCE no matter how far they flew — potentially 5 km
# instead of 60.
#
# NO EARLY STARTER EXISTS IN THE REFERENCE DATA (0 of 129), so neither reading
# has been tested against a published result. EARLY_START_RULE below selects;
# the default is the engine's existing behaviour so that nothing changes
# silently, but "full_distance" is the reading the Code text supports.
#
# CHECK THIS. It is the largest unresolved consequence in Section 13.

EARLY_START_RULE = "launch_to_sss"      # "launch_to_sss" | "full_distance"


def is_early_start(last_sss_crossing_time: float | None,
                   first_gate_time: float) -> bool:
    """S7F 13.3 — the definition, and it is the LAST crossing that decides."""
    if last_sss_crossing_time is None:
        return False
    return last_sss_crossing_time < first_gate_time


def early_start_distance(launch_to_sss: float, course_distance: float) -> float:
    """The distance an early starter is scored on, before the S7F 5.2 floor.

    See the long note above: which of these two the Code intends is not settled
    by the text available, and they differ by the whole task.
    """
    if EARLY_START_RULE == "full_distance":
        return max(0.0, course_distance)
    if EARLY_START_RULE != "launch_to_sss":
        raise ValueError(f"EARLY_START_RULE must be 'launch_to_sss' or "
                         f"'full_distance', got {EARLY_START_RULE!r}")
    return max(0.0, launch_to_sss)


# =========================================================================
#  13.4  Stopped tasks
# =========================================================================
#
# IMPLEMENTED HERE, NOT WIRED IN. Every function below follows the Code text,
# and each is tested against that text — but nothing calls them from the
# scoring pipeline yet, because a stopped task needs an announcement time that
# no input to this engine currently carries, and because there is no stopped
# task in the reference data to check the wiring against. Scoring a stopped
# task as though it had run to completion is the dangerous failure, since the
# numbers look entirely reasonable; the engine reports 13.4 as absent instead.
#
# --- 13.4.1  Stop task time ---------------------------------------------
#
# "The time when a stop was announced for the first time is the 'task stop
#  announcement time'... a 'task stop' time is calculated, by 'scoring back',
#  or deducting a number of minutes from the announcement time. Pilots' flights
#  will only be scored up to this task stop time."
#
# Score-back is 5 minutes in paragliding, 15 in hang-gliding
# (competition.json: score_back_min). Without it, pilots racing towards a known
# stop are rewarded for taking risks in deteriorating conditions, which is
# precisely what the rule removes.


def task_stop_time(announcement_time: float, score_back_seconds: float) -> float:
    """S7F 13.4.1 — flights are scored only up to this."""
    return announcement_time - score_back_seconds


# --- 13.4.3  Stopped task validity ---------------------------------------
#
#     taskDuration = taskStopTime - max(∀p ∈ StartedPilots: startTime_p)
#
#     stoppedDurationValidity = taskDuration >= minimumTime ? 1 : 0
#
#     stoppedDistanceValidity =
#         sqrt( (bestDistFlown - avg(∀i: distFlown_i))
#               / (taskLengthToESS - bestDistFlown + 1) )
#
#     stoppedPilotsValidity =
#         (numPilotsLandedBeforeStopTime / numPilotsLaunched) ^ 3
#
#     StoppedTaskValidity =
#         numPilotsReachedESS > 0 ? 1
#                                 : stoppedDurationValidity
#                                   * min(1, stoppedDistanceValidity
#                                            + stoppedPilotsValidity)
#
#     dayQuality = LaunchValidity * DistanceValidity * TimeValidity
#                  * StoppedTaskValidity
#
# The shape of it: a stopped task is fully valid the moment anybody completed
# the speed section. Otherwise it has to earn validity two ways — by having run
# long enough to be a test at all (the duration gate, which is binary), and by
# either spreading the field out (distance validity) or having most of the
# field already down (pilots validity), whichever helps more, capped at 1.
#
# CHECK THIS, in two places the transcription is not certain:
#   * whether stoppedDistanceValidity is a square root, as read here
#   * whether stoppedPilotsValidity is the ratio CUBED, as read here, or a cube
#     root. Cubed makes it contribute almost nothing until most of the field is
#     down, which is the behaviour the surrounding text implies.


def stopped_duration_validity(task_duration: float, minimum_time: float) -> float:
    """S7F 13.4.3 — binary: did the task run long enough to be a test at all."""
    return 1.0 if task_duration >= minimum_time else 0.0


def stopped_distance_validity(best_distance: float, mean_distance: float,
                              task_length_to_ess: float) -> float:
    """S7F 13.4.3 — how far the field spread out before the stop."""
    denom = task_length_to_ess - best_distance + 1.0
    if denom <= 0:
        return 0.0
    return math.sqrt(max(0.0, (best_distance - mean_distance) / denom))


def stopped_pilots_validity(landed_before_stop: int, launched: int) -> float:
    """S7F 13.4.3 — the fraction of the field already down, cubed."""
    if launched <= 0:
        return 0.0
    return (landed_before_stop / launched) ** 3


def stopped_task_validity(reached_ess: int, task_duration: float,
                          minimum_time: float, best_distance: float,
                          mean_distance: float, task_length_to_ess: float,
                          landed_before_stop: int, launched: int) -> float:
    """S7F 13.4.3 — the extra validity factor, multiplied into TaskValidity."""
    if reached_ess > 0:
        return 1.0
    return stopped_duration_validity(task_duration, minimum_time) * min(
        1.0,
        stopped_distance_validity(best_distance, mean_distance, task_length_to_ess)
        + stopped_pilots_validity(landed_before_stop, launched))


# --- 13.4.4  Scored time window ------------------------------------------
#
# Race with a SINGLE start gate — one window for everybody:
#     scoreTimeWindow_p = (raceStartTime, taskStopTime)
#
# Multi-gate race, or a Time Trial — an equal-length window per pilot:
#     lastStartTime = the latest start gate or start clock taken by AT LEAST
#                     ONE competitor, and "shall not be redefined by individual
#                     pilots crossing the start line after that time"
#     scoreTime     = taskStopTime - lastStartTime
#     scoreTimeWindow_p = (startTime_p, startTime_p + scoreTime)
#
# "This means that if the last pilot started and then flew for, for example, 75
#  minutes until the task was stopped, all tracks are only scored for the first
#  75 minutes each pilot flew after taking their respective start."
#
# So an early starter in a multi-gate race does NOT get credit for the extra
# time they were in the air — everyone gets the same number of minutes.


def scored_time_window(start_time: float, task_stop_time_: float,
                       race_start_time: float, last_start_time: float,
                       single_gate_race: bool) -> tuple[float, float]:
    """S7F 13.4.4 — the (from, to) each pilot's flight is scored over."""
    if single_gate_race:
        return (race_start_time, task_stop_time_)
    return (start_time, start_time + (task_stop_time_ - last_start_time))


# --- 13.4.5  Time points for pilots at or after ESS ----------------------
#
# "No pilot shall receive any points from any flight segment after the task
#  stop time has been announced."
#
#   1. Nobody reached goal before the stop -> available Time Points are ZERO,
#      and none of them move to Distance Points.
#
#   2. At least one pilot reached goal:
#      a. If at least one pilot is BETWEEN ESS AND GOAL at the stop, the
#         reference pilot is whichever of them would have scored the most time
#         points had they reached goal — in a single-gate race that is the
#         earliest ESS crossing, otherwise the smallest start-to-ESS time.
#         timePointsReduction = the time points that pilot would have scored.
#      b. If nobody is between ESS and goal, timePointsReduction = the time
#         points a pilot would have scored had they reached ESS exactly at the
#         task stop time and flown on to goal.
#
#      Then: every pilot in goal has timePointsReduction SUBTRACTED from their
#      time points, and the same amount is ADDED to the available Distance
#      Points for the task.
#
# The logic: a pilot still between ESS and goal when the task stopped was
# denied the chance to finish, so the time advantage the finishers hold over
# them is removed from the time pot and given back to everybody as distance.
#
# CHECK THIS: whether the reduction is floored at zero per pilot, and whether
# the moved points are added to the pot BEFORE or AFTER distance points are
# computed. This implementation floors at zero and returns the amount for the
# caller to add to the pot before distance points are assigned.


def time_points_reduction(any_in_goal: bool, best_time: float | None,
                          reference_time: float | None,
                          available_time: float) -> float:
    """S7F 13.4.5 — how many time points move to the distance pot.

    `reference_time` is the speed-section time of the reference pilot under
    2a, or the notional time to the stop under 2b. None when case 1 applies.
    """
    from .s7f_12_pilot_points import speed_fraction

    if not any_in_goal or reference_time is None or best_time is None:
        return 0.0
    return max(0.0, speed_fraction(reference_time, best_time) * available_time)


def stopped_time_points(time_points: float, reduction: float) -> float:
    """S7F 13.4.5 — a pilot in goal loses the reduction, floored at zero."""
    return max(0.0, time_points - reduction)


# --- 13.4.6  Distance points with altitude bonus -------------------------
#
#     altitudeBonus_p = max(0, lastPoint_p.altitude - goalAltitude)
#                       * bonusGlideRatio
#
#     bonusDistance_p = min(taskDistance,
#                           taskDistance
#                           - shortestDistanceToGoal(lastPoint_p)
#                           + altitudeBonus_p)
#
#     ScoredDistance_p = max(distance_p, bonusDistance_p)
#
# "To compensate for altitude differences at the time when a task is stopped."
# A pilot who was high when the stop came would have glided further, so they
# are credited for it.
#
# THREE THINGS THAT ARE EASY TO GET WRONG, all stated in the Code:
#   * the bonus is added to the distance AT THE STOP, "disregarding any better
#     distances achieved previously" — not to the pilot's best distance;
#   * only then is it compared against the best distance, and the larger wins;
#   * "Time Point and Leading Point calculations remain unaffected by the Bonus
#     Distance". It feeds distance points only.
#
# Only pilots STILL FLYING at the task stop time get a bonus.


def altitude_bonus(last_altitude: float, goal_altitude: float,
                   bonus_glide_ratio: float) -> float:
    """S7F 13.4.6 — extra distance the pilot's remaining height was worth."""
    return max(0.0, last_altitude - goal_altitude) * bonus_glide_ratio


def bonus_distance(task_distance: float, distance_to_goal_at_stop: float,
                   altitude_bonus_: float) -> float:
    """S7F 13.4.6 — distance at the stop plus the altitude bonus, capped."""
    return min(task_distance,
               task_distance - distance_to_goal_at_stop + altitude_bonus_)


def scored_distance_with_bonus(best_distance: float,
                               bonus_distance_: float) -> float:
    """S7F 13.4.6 — the larger of the two. Distance points only."""
    return max(best_distance, bonus_distance_)


# =========================================================================
#  13.5  Penalties
# =========================================================================
#
# "These penalties are either expressed as an absolute number (e.g. '100
#  points') or as a percentage (e.g. '10% of the pilot's score in the task
#  where he performed the punishable action')."
#
#     finalScore_p = score_p - absolutePenalty
#     finalScore_p = score_p - score_p * percentagePenalty
#
# TWO SCOPES, and they are not interchangeable:
#
#   1. UNSPORTING BEHAVIOUR — deducted from the pilot's COMPETITION score, to
#      calculate their final competition score.
#   2. ALL PUNISHABLE ACTIONS — deducted from the pilot's TASK score, to
#      calculate their final task score.
#
# This engine scores ONE TASK, so it implements scope 2. Scope 1 needs the
# whole competition and is out of reach here for the same reason FTV (S7F 16)
# is — recorded so its absence is visible rather than assumed.
#
# The mechanism, the file format and the ID matching are in
# engine/rules/penalties.py, which was written after the published result
# showed this engine ignoring an airspace disqualification worth 290 points.
#
# NOTE: penalties.py also offers `percent_task`, a percentage of the task's
# available points rather than of the pilot's own score. That form is NOT in
# the Code text above. It is kept because published tables sometimes express a
# penalty that way, but it should not be used where a rule says "percentage".


def penalty_absolute(score: float, points: float) -> float:
    """S7F 13.5 — finalScore = score - absolutePenalty, floored at zero."""
    return max(0.0, score - points)


def penalty_percentage(score: float, percent: float) -> float:
    """S7F 13.5 — finalScore = score - score * percentage, floored at zero."""
    return max(0.0, score - score * percent / 100.0)
