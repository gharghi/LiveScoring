"""S7F 10 — TASK VALIDITY, all in one file.

    TaskValidity = LaunchValidity * DistanceValidity * TimeValidity

A value between 0 and 1 measuring "how suitable a competition task is to
evaluate pilots' skills", calculated after the task has been flown. It scales
the entire 1000-point pot, so it is the single number with the widest reach in
the whole system: get it wrong and every pilot moves together, which is
precisely the error a leaderboard cannot show you.

    10.1  Launch validity     did the field think the day was flyable?
    10.2  Distance validity   was the task long enough to make decisions?
    10.3  Time validity       was the speed section long enough to matter?

Each is a cubic through a normalised ratio, each ratio is capped at 1, and each
answers a different way the day can fail to be a test of skill.

Unlike engine/rules/s7f_71_algorithms.py and s7f_09_control_zones.py, the
Sporting Code text for this section WAS available when it was written, so the
formulas below are transcribed rather than inferred. What follows the formulas
is where transcription still left something to decide.

================================================================================
  THREE DIFFERENCES BETWEEN THE PUBLISHED TEXT AND THIS ENGINE
================================================================================

1.  LAUNCH VALIDITY IS NOT CLAMPED IN THE PUBLISHED FORMULA, AND EXCEEDS 1.

    The text gives LaunchValidity = 0.028 LVR + 2.917 LVR^2 - 1.944 LVR^3 with
    min(1, ...) applied to LVR only. At LVR = 1 that evaluates to 1.001, not 1.

    The asymmetry is hard to read as an OCR loss: 10.3 spells out
    max(0, min(1, ...)) around its cubic and 10.1 does not, in the same
    section, so whatever dropped the clamp from one would have dropped it from
    the other.

    This engine CLAMPS, because the published result for the reference task has
    the three pots summing to exactly 1000.0, which requires TaskValidity = 1
    and therefore a clamped LaunchValidity. Unclamped they would sum to 1001.0.

    CHECK THIS. If the Code really has no clamp, a full field can score 1001
    points and this engine is 0.1% low on every fully-attended task.

2.  10.3 SAYS "ESS". 9.4.1 SAYS "GOAL". THEY ARE DIFFERENT POPULATIONS.

    10.3: "If no pilot finishes the speed section, then time validity is not
    based on time but on distance" — finishing the speed section is reaching
    ESS. But [PG] 9.4.1 restricts BestTime to pilots who reached GOAL.

    So for a task where ESS and goal are different cylinders, and somebody
    crosses ESS but nobody reaches goal, the two readings disagree: 10.3 would
    use that pilot's time, 9.4.1 would say there is no BestTime and fall back
    to distance.

    `time_validity()` below takes the ESS-based time, following 10.3's own
    wording for its own rule. On the reference task ESS and goal are the same
    cylinder so the two are identical and nothing moves; the distinction is
    made explicit so it cannot be lost.

    CHECK THIS: whether 10.3's "BestTime" is the same variable as 9.4.1's.

3.  "PILOTS PRESENT" CANNOT BE DERIVED FROM TRACKLOGS, AND SILENTLY DEFAULTS.

    The text is precise: 'Pilots present' includes all pilots not marked as
    'Absent' (ABS) — those who took off AND those present but did not fly
    (DNF). It adds that DNF must be assigned carefully, distinguishing pilots
    who declined the conditions from pilots away sick.

    That distinction exists nowhere in any tracklog. With nothing supplied, this
    engine counts the tracklogs it was given, which makes PilotsFlying ==
    PilotsPresent, LVR = 1/0.96 capped to 1, and LaunchValidity exactly 1.0 by
    construction — for every task, always.

    That is not a measurement. It is the rule switched off. Launch validity is
    the Code's stated SAFETY FEATURE: it devalues a task that most of the field
    judged too dangerous to fly, and it only works if somebody counts the
    pilots who stood on launch and chose not to. Set `pilots_present` in
    competition.json.
"""

from __future__ import annotations

from .params import GapParams

# =========================================================================
#  10.1  Launch validity
# =========================================================================
#
#     NominalLaunch = 96%
#
#     LVR = min(1, NumberOfPilotsFlying / (NumberOfPilotsPresent * NominalLaunch))
#
#     LaunchValidity = 0.028 * LVR + 2.917 * LVR^2 - 1.944 * LVR^3
#
# "If 96% or more of the pilots present at take-off launch, Launch Validity is
# 1. It decreases as the percentage of launching pilots drops below this
# threshold. This mechanism serves as a safety feature. If a significant number
# of pilots choose not to launch due to unfavourable or dangerous conditions,
# the points awarded to those who do launch are reduced."
#
# The 96% is why the curve reaches 1 before every pilot has launched: the last
# 4% is assumed to be gear failure and illness rather than a verdict on the day.
#
#     LVR 0.00 -> 0.000      LVR 0.75 -> 0.842
#     LVR 0.25 -> 0.159      LVR 0.90 -> 0.971
#     LVR 0.50 -> 0.500      LVR 1.00 -> 1.001, clamped to 1  (see note 1)


def launch_validity(pilots_flying: int, pilots_present: int,
                    p: GapParams) -> float:
    """S7F 10.1.

    `pilots_present` per the Code: everyone not marked ABS, i.e. those who took
    off plus those present who did not fly (DNF). See note 3 at the top — this
    number cannot come from tracklogs.
    """
    if pilots_present <= 0:
        return 0.0
    lvr = min(1.0, pilots_flying / (pilots_present * p.nominal_launch))
    lv = 0.028 * lvr + 2.917 * lvr ** 2 - 1.944 * lvr ** 3
    return max(0.0, min(1.0, lv))       # clamp: see note 1 at the top


# =========================================================================
#  10.2  Distance validity
# =========================================================================
#
#     NominalGoal = 30%
#
#     SumOfFlownDistancesOverMinimum
#         = SUM over pilots p of  max(0, PilotDistance_p - MinimumDistance)
#
#     NominalDistArea
#         = ( (NominalGoal + 1) * (NominalDistance - MinimumDistance)
#             + max(0, NominalGoal * (bestDistance - NominalDistance)) ) / 2
#
#     DVR = SumOfFlownDistancesOverMinimum
#           / (NumberOfPilotsFlying * NominalDistArea)
#
#     DistanceValidity = min(1, DVR)
#
# "If the task distance is quite short in relation to nominal distance, the day
# is probably not a good measure of pilot skill because there would not be many
# decisions to make."
#
# Two behaviours worth having in mind when a number looks wrong:
#
#   * Distance BELOW MinimumDistance contributes nothing to the numerator. A
#     pilot who lands on the hill neither helps nor hurts validity beyond
#     counting in the denominator's NumberOfPilotsFlying.
#
#   * The second term of NominalDistArea only fires when somebody beat
#     NominalDistance, and it RAISES the bar. The Code: "If a task is longer
#     than nominal distance, the day will not be devalued because of distance
#     validity, even if the nominal goal parameter value is not achieved, as
#     long as a fair percentage of pilots fly a good distance."
#
# The Code also warns about the case this formula cannot see: "a task that is
# shorter than nominal distance [can have] a distance validity of almost 1 ...
# when a large percentage of the pilots fly a large percentage of the course
# but, in this case, you still have a practical devaluation because there will
# be little spreading between pilots' scores." Nothing to implement — but it is
# why a validity of 1 is not by itself evidence of a good task.


def distance_validity(flown: list[float], best_distance: float,
                      p: GapParams) -> float:
    """S7F 10.2. `flown` is every FLYING pilot's scored distance, in metres."""
    n = len(flown)
    if n == 0:
        return 0.0
    over_minimum = sum(max(0.0, d - p.minimum_distance) for d in flown)
    area = (
        (p.nominal_goal + 1.0) * (p.nominal_distance - p.minimum_distance)
        + max(0.0, p.nominal_goal * (best_distance - p.nominal_distance))
    ) / 2.0
    if area <= 0:
        return 0.0
    return min(1.0, over_minimum / (n * area))


# =========================================================================
#  10.3  Time validity
# =========================================================================
#
#     If one pilot reached ESS:   TVR = min(1, BestTime / NominalTime)
#     If no pilot reached ESS:    TVR = min(1, BestDistance / NominalDistance)
#
#     TimeValidity = max(0, min(1,
#         -0.271 + 2.912 * TVR - 2.098 * TVR^2 + 0.457 * TVR^3))
#
# The direction reads backwards at first: a LONGER best time gives HIGHER
# validity, capped at 1 once it reaches nominal time. The formula is asking
# "was this task long enough to be a real test", not "were the pilots quick".
# The Code: "If the fastest time is quite short, the day is probably not a good
# measure of pilot skill because there would not be many decisions to make and,
# because of this, luck can distort scores as there will be little possibility
# to recover any accidental loss of time."
#
# BOTH CLAMPS MATTER HERE, unlike in 10.1. The cubic is NEGATIVE below
# TVR ~ 0.1002 — at TVR = 0 it is -0.271 — so max(0, ...) is doing real work on
# a very short task, and it is spelled out in the text.
#
#     TVR 0.00 -> -0.271, clamped to 0      TVR 0.50 -> 0.718
#     TVR 0.10 -> -0.000, clamped to 0      TVR 0.75 -> 0.926
#     TVR 0.25 ->  0.333                    TVR 1.00 -> 1.000
#
# On the fallback: note that it substitutes a DISTANCE ratio into a formula
# whose input is normally a TIME ratio. The Code is explicit that this is
# intended — "the distance of the pilot who flies the furthest in relation to
# nominal distance is then used to calculate the time validity the same way as
# if it was the time".


def time_validity(best_time_to_ess: float | None, best_distance: float,
                  p: GapParams) -> float:
    """S7F 10.3.

    `best_time_to_ess` is the fastest time to complete the SPEED SECTION, in
    seconds, or None if nobody finished it. See note 2 at the top: 10.3 says
    ESS where [PG] 9.4.1 says goal, and this parameter follows 10.3.
    """
    if best_time_to_ess is not None and best_time_to_ess > 0:
        tvr = min(1.0, best_time_to_ess / p.nominal_time)
    elif p.nominal_distance:
        tvr = min(1.0, best_distance / p.nominal_distance)
    else:
        tvr = 0.0
    tv = -0.271 + 2.912 * tvr - 2.098 * tvr ** 2 + 0.457 * tvr ** 3
    return max(0.0, min(1.0, tv))


# =========================================================================
#  10  Task validity
# =========================================================================


def task_validity(launch: float, distance: float, time: float) -> float:
    """S7F 10 — TaskValidity = LaunchValidity * DistanceValidity * TimeValidity.

    A bare multiply, given a name because it is the number that scales the
    whole 1000-point pot and because writing it out is the only place the three
    coefficients are visible together.
    """
    return launch * distance * time


def best_time_to_ess(results) -> float | None:
    """The fastest speed-section time among pilots who reached ESS. [10.3]

    Distinct from rules.s7f_09_control_zones.best_time(), which applies
    [PG] 9.4.1 and counts only pilots who reached GOAL. Identical whenever ESS
    and goal are the same cylinder, which is the common case and is true of the
    reference task. See note 2 at the top of this file.
    """
    times = [r.speed_section_time for r in results
             if r.ess_time is not None and r.speed_section_time]
    return min(times) if times else None
