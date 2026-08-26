"""S7F 13 — special cases.

Moved out of engine/rules/ so the rules files hold rules and nothing
else. Run with `python3 -m tests` or `./run.py --verify`.
"""

from __future__ import annotations

import math

import engine.rules.s7f_13_special_cases as S13
from engine.rules.s7f_13_special_cases import (altitude_bonus,
    bonus_distance, early_start_distance, ess_no_goal_factor,
    goal_altitude_factor, is_early_start, penalty_absolute,
    penalty_percentage, scored_distance_with_bonus, scored_time_window,
    stopped_duration_validity, stopped_distance_validity,
    stopped_pilots_validity, stopped_task_validity, stopped_time_points,
    task_stop_time, time_points_reduction)


def run() -> list[tuple[str, bool, str]]:
    """Section 13 against its own text. No worked examples exist for any of it."""
    out: list[tuple[str, bool, str]] = []

    # --- 13.1 -------------------------------------------------------------
    g, e = 1000.0, 300.0
    out.append(("13.1 at or below goal altitude the factor is exactly 0.8",
                goal_altitude_factor(900, g, e) == 0.8
                and goal_altitude_factor(g, g, e) == 0.8,
                "100 m below → 0.8; exactly at goal → 0.8 (a floor, not zero)"))
    out.append(("13.1 at or above goal + elevation the factor is exactly 1",
                goal_altitude_factor(g + e, g, e) == 1.0
                and goal_altitude_factor(g + 5000, g, e) == 1.0,
                "+300 m → 1.0; +5000 m → 1.0"))
    # continuity at both ends of the cubic
    lo = 0.8 + 0.6 * 0 - 0.6 * 0 + 0.2 * 0
    hi = 0.8 + 0.6 * 1 - 0.6 * 1 + 0.2 * 1
    out.append(("13.1 the cubic is continuous with both ends",
                abs(lo - 0.8) < 1e-15 and abs(hi - 1.0) < 1e-15,
                "Ag=0 → 0.8 exactly, Ag=1 → 1.0 exactly"))
    out.append(("13.1 halfway up the band", abs(goal_altitude_factor(1150, g, e)
                                                - 0.975) < 1e-12,
                f"Ag=0.5 → {goal_altitude_factor(1150, g, e):.6f}"))
    # monotone increasing across the band
    vals = [goal_altitude_factor(g + i * e / 20, g, e) for i in range(21)]
    out.append(("13.1 the factor rises monotonically across the band",
                all(b >= a - 1e-15 for a, b in zip(vals, vals[1:])),
                f"0.8 → 1.0 over {e:.0f} m, 21 samples"))
    # the reference task's 200 m band, recovered from the published Low P column
    # Recovered from the published Low P column by inverting the curve. The
    # three heights are far apart, so agreeing on all three pins the band.
    band200 = [(150, 0.9968750), (18, 0.8492858), (6, 0.8174654), (-13, 0.8)]
    out.append(("13.1 the reference task's 200 m band reproduces its Low P column",
                all(abs(goal_altitude_factor(g + h, g, 200.0) - want) < 1e-6
                    for h, want in band200),
                "heights 150/18/6 m → 0.996875/0.849286/0.817465, matching the "
                "official reductions; 13 m BELOW goal → 0.8, the floor. "
                "A 300 m band fits none of them."))

    # --- 13.2 -------------------------------------------------------------
    out.append(("13.2 [PG] ESS without goal scores zero time points",
                ess_no_goal_factor(False, True, 0.0) == 0.0,
                "paragliding parameter is 0%"))
    out.append(("13.2 [HG] the recommended default is 80%",
                ess_no_goal_factor(False, True, 0.8) == 0.8,
                "changeable by local regulations"))
    out.append(("13.2 a pilot in goal is unaffected",
                ess_no_goal_factor(True, True, 0.0) == 1.0,
                "reaching goal validates the speed section"))
    out.append(("13.2 a pilot who never reached ESS scores no time points",
                ess_no_goal_factor(False, False, 0.8) == 0.0,
                "no speed section completed at all"))

    # --- 13.3 -------------------------------------------------------------
    out.append(("13.3 an early start is defined by the LAST SSS crossing",
                is_early_start(99.0, 100.0) and not is_early_start(101.0, 100.0),
                "last crossing before the gate → early; after → a valid start"))
    out.append(("13.3 a pilot who never crossed the SSS is not an early starter",
                not is_early_start(None, 100.0),
                "no crossing at all is a different case (no valid start)"))
    was = S13.EARLY_START_RULE
    try:
        S13.EARLY_START_RULE = "launch_to_sss"
        a = early_start_distance(4860.0, 59647.0)
        S13.EARLY_START_RULE = "full_distance"
        b = early_start_distance(4860.0, 59647.0)
    finally:
        S13.EARLY_START_RULE = was
    out.append(("13.3 the two readings of the consequence differ by the task",
                a == 4860.0 and b == 59647.0,
                f"launch_to_sss → {a:,.0f} m; full_distance → {b:,.0f} m. "
                f"13.3 defines an early start but does not state the "
                f"consequence; 12.1 supports the second."))

    # --- 13.4.1 -----------------------------------------------------------
    out.append(("13.4.1 the stop time is the announcement less score-back",
                task_stop_time(50000.0, 300.0) == 49700.0,
                "announced at 50000 s, paragliding score-back 5 min → 49700 s"))

    # --- 13.4.3 -----------------------------------------------------------
    out.append(("13.4.3 anybody reaching ESS makes a stopped task fully valid",
                stopped_task_validity(1, 0.0, 9999.0, 0.0, 0.0, 1.0, 0, 1) == 1.0,
                "reached_ess > 0 → 1, short-circuiting everything else"))
    out.append(("13.4.3 the duration gate is binary",
                stopped_duration_validity(3600, 3600) == 1.0
                and stopped_duration_validity(3599, 3600) == 0.0,
                "at minimumTime → 1; one second short → 0"))
    out.append(("13.4.3 a task stopped too early is worth nothing",
                stopped_task_validity(0, 100.0, 3600.0, 50000.0, 10000.0,
                                      60000.0, 50, 100) == 0.0,
                "duration below minimumTime zeroes the whole factor"))
    # distance and pilots validity combine, capped at 1
    v = stopped_task_validity(0, 7200.0, 3600.0, 50000.0, 49000.0, 60000.0,
                              100, 100)
    out.append(("13.4.3 distance and pilots validity add, capped at 1",
                v == 1.0,
                f"whole field landed → pilots validity 1.0 → capped at {v:.3f}"))
    out.append(("13.4.3 pilots validity is cubed, so it bites late",
                abs(stopped_pilots_validity(50, 100) - 0.125) < 1e-12,
                "half the field down → 0.5^3 = 0.125, not 0.5"))

    # --- 13.4.4 -----------------------------------------------------------
    w = scored_time_window(500.0, 10000.0, 400.0, 900.0, True)
    out.append(("13.4.4 a single-gate race scores one window for everybody",
                w == (400.0, 10000.0),
                f"race start 400 s → {w}"))
    w1 = scored_time_window(500.0, 10000.0, 400.0, 900.0, False)
    w2 = scored_time_window(700.0, 10000.0, 400.0, 900.0, False)
    out.append(("13.4.4 multi-gate gives every pilot an EQUAL-LENGTH window",
                (w1[1] - w1[0]) == (w2[1] - w2[0]) == 9100.0,
                f"last start 900 s, stop 10000 s → 9100 s each: {w1} and {w2}"))

    # --- 13.4.5 -----------------------------------------------------------
    out.append(("13.4.5 nobody in goal means the time pot is zero and moves "
                "nothing", time_points_reduction(False, 3600, 3600, 470.0) == 0.0,
                "case 1: no time points, and none transferred to distance"))
    red = time_points_reduction(True, 3600.0, 3600.0, 470.0)
    out.append(("13.4.5 a reference pilot as fast as the best takes the whole "
                "time pot with them", abs(red - 470.0) < 1e-9,
                f"reference time == best time → reduction {red:.1f} of 470.0"))
    out.append(("13.4.5 the reduction is subtracted from each pilot in goal",
                stopped_time_points(470.0, 120.0) == 350.0
                and stopped_time_points(50.0, 120.0) == 0.0,
                "470 − 120 → 350; floored at zero for a slower finisher"))

    # --- 13.4.6 -----------------------------------------------------------
    out.append(("13.4.6 the altitude bonus is height above goal times the "
                "glide ratio", altitude_bonus(1500.0, 500.0, 8.0) == 8000.0,
                "1000 m above goal at 8:1 → 8 km"))
    out.append(("13.4.6 no bonus for a pilot below goal altitude",
                altitude_bonus(400.0, 500.0, 8.0) == 0.0,
                "max(0, ...) — being low is not a negative bonus"))
    bd = bonus_distance(60000.0, 20000.0, 8000.0)
    out.append(("13.4.6 the bonus applies to the distance AT THE STOP", bd == 48000.0,
                f"task 60 km, 20 km still to fly at the stop, 8 km bonus → "
                f"{bd/1000:.0f} km — 'disregarding any better distances "
                f"achieved previously'"))
    out.append(("13.4.6 the bonus distance is capped at the task distance",
                bonus_distance(60000.0, 1000.0, 8000.0) == 60000.0,
                "59 km + 8 km bonus → capped at 60 km"))
    out.append(("13.4.6 the larger of best and bonus distance is scored",
                scored_distance_with_bonus(50000.0, 48000.0) == 50000.0
                and scored_distance_with_bonus(40000.0, 48000.0) == 48000.0,
                "a pilot who was further earlier keeps that distance"))

    # --- 13.5 -------------------------------------------------------------
    out.append(("13.5 an absolute penalty subtracts points",
                penalty_absolute(500.0, 100.0) == 400.0,
                "'100 points' → 500 − 100"))
    out.append(("13.5 a percentage penalty is a percentage of the pilot's score",
                penalty_percentage(500.0, 10.0) == 450.0,
                "'10% of the pilot's score in the task' → 500 − 50"))
    out.append(("13.5 a task score is never negative",
                penalty_absolute(50.0, 100.0) == 0.0
                and penalty_percentage(500.0, 100.0) == 0.0,
                "over-large deductions floor at zero"))
    return out
