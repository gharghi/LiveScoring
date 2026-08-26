"""CIVL GAP scoring — an index, not an implementation.

Every formula that used to live here now has its own file under engine/rules/,
one per Sporting Code element, each with the rule text, the paragliding /
hang-gliding difference, and an honest note on how far it has been verified.
That split exists so the rules can be checked against the Code one at a time —
which is how the errors in VERIFICATION.md §4 and §5 were eventually found.

    engine/rules/params.py             S7F 5      competition parameters
    engine/rules/s7f_09_control_zones.py  S7F 9   zones, crossings, distance,
                                                  best time
    engine/rules/s7f_10_task_validity.py  S7F 10  launch/distance/time validity
    engine/rules/s7f_11_allocation.py  S7F 11     points allocation
                                                  (and 12.4 arrival: none [PG])
    engine/rules/s7f_12_pilot_points.py   S7F 12  distance/time/leading points
    engine/rules/points_leading.py        S7F 12.3.1  the leading coefficient
    engine/rules/s7f_13_special_cases.py  S7F 13  elevated goal, ESS-no-goal,
                                                  early start, stopped tasks
    engine/rules/penalties.py          S7F 13.5   penalties
    engine/rules/ftv.py                S7F 16     FTV               NOT DONE

`./run.py --rules` prints the same list with current status.

This module re-exports them under their old names so existing callers keep
working. New code should import from engine.rules.
"""

from __future__ import annotations

from .rules import (Allocation, GapParams, Penalty, allocate,  # noqa: F401
                    apply_penalties, arrival_weight, available_points,
                    distance_points,
                    distance_validity, early_start_distance,
                    ess_no_goal_factor, goal_altitude_factor, launch_validity,
                    leading_coefficient, leading_factor, leading_from_partial,
                    leading_partial, leading_points, leading_weight,
                    load_penalties, scored_distance, speed_fraction,
                    time_points, time_validity, weight_integral)

__all__ = [
    "GapParams", "Allocation", "allocate", "Penalty", "apply_penalties",
    "load_penalties", "scored_distance", "early_start_distance",
    "launch_validity", "distance_validity", "time_validity",
    "distance_points", "time_points", "speed_fraction",
    "arrival_weight", "available_points",
    "leading_points", "leading_coefficient", "leading_factor",
    "leading_partial", "leading_from_partial", "leading_weight",
    "weight_integral", "goal_altitude_factor",
    "ess_no_goal_factor",
]
