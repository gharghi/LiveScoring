"""How far along the route a pilot got.  [algorithm D]

Two lines of arithmetic, evaluated once per fix, and the source of every
distance on the leaderboard.

    distanceToGoal(fix) = max(0, |fix - nextCylinderCentre| - nextRadius)
                          + remaining[nextCylinder]

    distanceFlown       = taskDistance - min(distanceToGoal over the flight)

MEASURED TO THE EDGE, NOT THE CENTRE. A pilot 8.1 km from the centre of a
7.5 km cylinder is 600 m from validating it, not 8.1 km. The `- radius` term is
that, clamped at zero for a pilot already inside.

MIXED, DELIBERATELY. The first term is the pilot's real distance to the EDGE of
the next zone; the second is the pre-computed optimised distance onward from
that zone's OPTIMISED point. Those are not the same point, so the sum is an
approximation — the standard one, and what published scoring uses.

`nextCylinder` is the next UN-TAGGED control zone, so it advances as the pilot
validates turnpoints. Distance can therefore only be measured along the task in
order: a pilot who skips a turnpoint is measured to the one they skipped, not
to where they actually are.

THE MINIMUM OVER THE FLIGHT, not the value at landing. A pilot who reaches a
point and drifts back keeps the distance they earned. This is also what makes
the leaderboard monotone in time.

A pilot who reached GOAL is credited the full task distance regardless.

--------------------------------------------------------------------------
THIS FILE IS THE REFERENCE. THE HOT LOOP INLINES IT.
--------------------------------------------------------------------------
score.py does not call `distance_to_goal()`. It carries the distance to the
current waypoint forward from the previous fix — the same number the crossing
test already computed — and folds this arithmetic inline, which took the hot
path from five square roots per fix to one and roughly halved the scoring time.

That makes this an inlined copy of a rule, which is a second implementation and
therefore a risk. It is stated here in full so it can be read, and
`engine/invariants.check_distance_to_goal_reference` compares the two over the
whole real field. If you change one, change both, and let the check tell you.
"""

from __future__ import annotations

import math


def distance_to_goal(fx: float, fy: float, wx: float, wy: float,
                     radius: float, remaining_after: float) -> float:
    """Distance still to fly, via the next un-tagged control zone. [reference]

    `remaining_after` is remaining[nextCylinder] from
    rules.route.remaining_table().
    """
    edge = math.hypot(fx - wx, fy - wy) - radius
    if edge < 0.0:
        edge = 0.0
    return edge + remaining_after


def distance_flown(task_distance: float, min_remaining: float,
                   reached_goal: bool = False) -> float:
    """Distance along the route, before the S7F 5.2 minimum-distance floor.

    The floor is applied separately, by rules.distance.scored_distance(),
    because it is a scoring rule and this is geometry.
    """
    if reached_goal:
        return task_distance
    return max(0.0, task_distance - min_remaining)
