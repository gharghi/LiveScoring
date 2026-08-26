"""S7F 11 — POINTS ALLOCATION, all in one file.

    "The available points for each task are 1000 * TaskValidity. These points
     are distributed between distance points, time points, leading points, and
     arrival points."

One number drives the whole split: what fraction of the pilots who LAUNCHED
reached goal before the deadline.

    goalRatio      = NumberOfPilotsInGoal / NumberOfPilotsFlying

    distanceWeight = 0.9 - 1.665 gr + 1.713 gr^2 - 0.587 gr^3

    goalRatio == 0:  leadingWeight = (1 - distanceWeight)
    goalRatio  > 0:  leadingWeight = (1 - distanceWeight) * LeadingTimeRatio

    arrivalWeight  = 0
    timeWeight     = 1 - distanceWeight - leadingWeight - arrivalWeight

    availablePointsDistance = round(1000 * TaskValidity * distanceWeight, 0)
    availablePointsTime     = round(1000 * TaskValidity * timeWeight,     0)
    availablePointsLeading  = round(1000 * TaskValidity * leadingWeight,  0)
    availablePointsArrival  = round(1000 * TaskValidity * arrivalWeight,  0)

The logic behind the cubic: nobody in goal means the task was a survival
exercise, so 90% of the points go to raw distance and speed is meaningless.
Everybody in goal means distance separates nobody, so its share falls to 36%
and time takes over.

    gr = 0.00 -> distance 0.900   leading 0.100   time 0.000
    gr = 0.50 -> distance 0.427   leading 0.149   time 0.424
    gr = 1.00 -> distance 0.361   leading 0.166   time 0.473

WORTH KNOWING WHEN A POT LOOKS WRONG: distanceWeight is remarkably FLAT above
gr ~ 0.8, moving only from 0.3618 to 0.3610 between 80% and 100% in goal. It is
very nearly impossible to explain a difference in the distance pot by a
disagreement about the goal count; look at TaskValidity instead.

THE gr == 0 BRANCH IS A REAL DISCONTINUITY, not a limit. With nobody in goal,
leading takes the WHOLE non-distance share (0.100) instead of
LeadingTimeRatio of it (0.026), and timeWeight collapses to zero. One pilot
reaching goal moves the leading pot from 100 to 26 points. That is intended —
with nobody in goal there is no time to score — but it means the allocation is
not continuous in gr, and a task with exactly one finisher is scored very
differently from one with none.

================================================================================
  TWO CONFLICTS BETWEEN THE PUBLISHED CODE TEXT AND THE PUBLISHED RESULT
================================================================================

1.  THE CODE ROUNDS THE POTS TO WHOLE POINTS. THE OFFICIAL RESULT DOES NOT.

    Section 11 writes all four as round(..., 0) — zero decimal places, so whole
    points. The published result for the reference task shows

        distance 361.7    leading 168.0    time 470.3

    to one decimal, and every pilot in goal scored exactly 361.7 distance
    points. If the pot had been rounded they would all have scored 362.0.

    Rounded, the same inputs give 362 / 168 / 470.

    So the two disagree by up to half a point on every pilot, which is larger
    than several of the differences elsewhere in this engine that have been
    worth chasing. ROUNDING below selects; the default is "none", because that
    is what reproduces the published result exactly.

    CHECK THIS. If the Code really rounds, this engine is out by up to 0.5
    points per component per pilot, and the published result is too.

2.  THE OFFICIAL RESULT IMPLIES A LeadingTimeRatio OUTSIDE THE LEGAL RANGE.

    Section 11: "The parameter LeadingTimeRatio is set for each task, with a
    value between 0 and 26%. The default LeadingTimeRatio in paragliding is
    26%."

    LeadingTimeRatio is recoverable from any published result without knowing
    anything else, because it is invariant under whatever distanceWeight came
    out at:

        LeadingTimeRatio = leadingPot / (leadingPot + timePot)

    For the reference task that is 168.0 / 638.3 = 26.32%, which is ABOVE the
    26% maximum the Code states.

    It cannot be reconciled by choosing a different goal ratio. At the 26%
    maximum the leading pot would be 165.97, but the winner actually scored
    168.0 leading points — and nobody can score more than the pot, because
    LeadingFactor is exactly 1 at LCmin.

    So either the published result was produced with an out-of-range parameter,
    or one of the premises above is wrong. The engine keeps 26.32% for this
    task (it is what reproduces the result) and warns whenever the configured
    value exceeds 26%.

    CHECK THIS with the meet director. It is worth 2 points on every pilot.

3.  "as well as the chosen goal form"

    The section's opening sentence says the distribution depends on the goal
    ratio "as well as the chosen goal form" — CYLINDER versus LINE. None of the
    formulas that follow reference the goal form, and arrivalWeight is
    unconditionally 0 for paragliding, so there is nothing here to implement.
    Recorded because an unimplemented clause is worth being able to point at.
"""

from __future__ import annotations

from dataclasses import dataclass

from .params import GapParams

# --- the one place the rounding choice is made ---------------------------

ROUNDING = "none"          # "none" | "integer"  — see conflict 1 above

LEADING_TIME_RATIO_MAX = 0.26      # S7F 11, paragliding


def available_points(task_validity: float, weight: float) -> float:
    """S7F 11 — 1000 * TaskValidity * weight, with the rounding policy applied."""
    raw = 1000.0 * task_validity * weight
    if ROUNDING == "integer":
        return float(round(raw))
    if ROUNDING != "none":
        raise ValueError(f"ROUNDING must be 'none' or 'integer', got {ROUNDING!r}")
    return raw


# --- the four weights, one function each ---------------------------------


def goal_ratio(in_goal: int, flying: int) -> float:
    """S7F 11 — pilots in goal before the deadline, over pilots who launched."""
    return (in_goal / flying) if flying else 0.0


def distance_weight(gr: float) -> float:
    """S7F 11 — 0.9 - 1.665 gr + 1.713 gr^2 - 0.587 gr^3."""
    return 0.9 - 1.665 * gr + 1.713 * gr ** 2 - 0.587 * gr ** 3


def leading_weight(dw: float, leading_time_ratio: float, gr: float) -> float:
    """S7F 11 — the whole non-distance share when nobody is in goal.

    [PG] With gr == 0 leading absorbs everything left over. Hang-gliding
    applies LeadingTimeRatio in both branches and leaves the rest unawarded.
    """
    if gr == 0.0:
        return 1.0 - dw
    return (1.0 - dw) * leading_time_ratio


def arrival_weight(*_args, **_kwargs) -> float:
    """S7F 11 / 12.4 — [PG] always 0."""
    return 0.0


def time_weight(dw: float, lw: float, aw: float) -> float:
    """S7F 11 — whatever the other three leave."""
    return 1.0 - dw - lw - aw


# --- the result -----------------------------------------------------------


@dataclass(slots=True)
class Allocation:
    """The four weights and the four pots, for one task."""

    goal_ratio: float = 0.0
    distance_weight: float = 0.0
    leading_weight: float = 0.0
    arrival_weight: float = 0.0
    time_weight: float = 0.0
    available_distance: float = 0.0
    available_time: float = 0.0
    available_leading: float = 0.0
    available_arrival: float = 0.0

    @property
    def available_total(self) -> float:
        return (self.available_distance + self.available_time
                + self.available_leading + self.available_arrival)

    @property
    def implied_leading_time_ratio(self) -> float:
        """LeadingTimeRatio read back out of the pots.

        Invariant under distanceWeight, which makes it the one parameter
        recoverable from any published result without knowing anything else.
        See conflict 2 at the top of this file.
        """
        denom = self.available_leading + self.available_time
        return self.available_leading / denom if denom else 0.0


def allocate(task_validity: float, in_goal: int, flying: int,
             p: GapParams) -> Allocation:
    """S7F 11 — the whole of section 11. THE TASK SCORE FUNCTION.

    Given how valid the task was and how the field did, return the four weights
    and the four pots every pilot's points are then drawn from.

        task_validity  S7F 10, in [0, 1]
        in_goal        pilots who reached goal before the deadline
        flying         pilots who launched
        p.leading_time_ratio   S7F 11, per task, 0..26%

    Pure: no clock, no I/O, no globals except the two documented policies at
    the top of this file.
    """
    gr = goal_ratio(in_goal, flying)
    dw = distance_weight(gr)
    lw = leading_weight(dw, p.leading_time_ratio, gr)
    aw = arrival_weight()
    tw = time_weight(dw, lw, aw)

    return Allocation(
        goal_ratio=gr,
        distance_weight=dw, leading_weight=lw,
        arrival_weight=aw, time_weight=tw,
        available_distance=available_points(task_validity, dw),
        available_time=available_points(task_validity, tw),
        available_leading=available_points(task_validity, lw),
        available_arrival=available_points(task_validity, aw),
    )
