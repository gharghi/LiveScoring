"""S7F 12 — pilot points.

Moved out of engine/rules/ so the rules files hold rules and nothing
else. Run with `python3 -m tests` or `./run.py --verify`.
"""

from __future__ import annotations

import math

from engine.rules.points_leading import (leading_coefficient,
                                         leading_factor,
                                         leading_partial,
                                         leading_from_partial,
                                         leading_weight, weight_integral)
import engine.rules.s7f_12_pilot_points as S12
from engine.rules.s7f_12_pilot_points import (distance_points, round1,
                                              leading_points, max_time_for,
                                              speed_fraction, task_score,
                                              time_points)


def run() -> list[tuple[str, bool, str]]:
    """Section 12 against its own published text and Table 2."""
    out: list[tuple[str, bool, str]] = []

    def hms(h, m, s):
        return h * 3600 + m * 60 + s

    # --- 12.2.1 Table 2, all three rows -----------------------------------
    table = [
        ("1:00", hms(1, 0, 0), [(hms(1, 8, 42), 0.80), (hms(1, 26, 7), 0.50),
                                (hms(2, 0, 0), 0.00)]),
        ("2:00", hms(2, 0, 0), [(hms(2, 12, 18), 0.80), (hms(2, 36, 56), 0.50),
                                (hms(3, 24, 51), 0.00)]),
        ("3:00", hms(3, 0, 0), [(hms(3, 15, 4), 0.80), (hms(3, 45, 14), 0.50),
                                (hms(4, 43, 55), 0.00)]),
    ]
    bad = []
    for label, best, pts in table:
        for t, want in pts:
            got = speed_fraction(t, best)
            if abs(got - want) > 5e-3:
                bad.append(f"best {label} at {t}s: {got:.4f} vs {want}")
    out.append(("12.2.1 SpeedFraction matches all nine cells of Table 2",
                not bad, f"{len(bad)} wrong: {bad[:2]}" if bad
                else "3 best times × 80%/50%/0% points"))

    # "zero points if their time is equal to or longer than the fastest time
    #  plus the square root of the fastest time"
    best = hms(1, 0, 0)
    zero_at = best + math.sqrt(best / 3600.0) * 3600.0
    out.append(("12.2 zero at bestTime + sqrt(bestTime) hours",
                speed_fraction(zero_at, best) == 0.0
                and speed_fraction(zero_at - 60, best) > 0.0,
                f"best 1:00 → zero from {zero_at / 3600:.4f} h = 2:00:00"))
    out.append(("12.2 the fastest pilot takes the whole time pot",
                abs(speed_fraction(best, best) - 1.0) < 1e-15,
                "SpeedFraction(bestTime, bestTime) = 1"))

    # --- 12.1 linearity ---------------------------------------------------
    bad = [f for f in (0.0, 0.25, 0.5, 0.75, 1.0)
           if abs(distance_points(60000.0 * f, 60000.0, 400.0) - round1(f * 400.0))
           > 1e-9]
    out.append(("12.1 distance points are linear in distance", not bad,
                f"failed at {bad}" if bad else
                "0/25/50/75/100% of best distance → 0.0/100.0/200.0/300.0/400.0"))
    out.append(("12.1 the best distance takes the whole distance pot",
                distance_points(59647.0, 59647.0, 361.7) == 361.7,
                "ratio 1 → the full pot"))

    # --- 12.3 -------------------------------------------------------------
    out.append(("12.3 LCmin takes the whole leading pot",
                leading_points(1.234, 1.234, 168.0) == 168.0,
                "LeadingFactor is exactly 1 at LC == LCmin"))
    out.append(("12.3 leading points never go negative",
                leading_points(99.0, 0.5, 168.0) == 0.0,
                "max(0, ...) clamps a far-behind pilot to zero"))
    out.append(("12.3 a pilot who landed out still scores leading points",
                leading_points(1.3, 1.234, 168.0) > 0.0,
                f"LC 1.3 vs LCmin 1.234 → {leading_points(1.3, 1.234, 168.0)} pts "
                f"— 12.3: 'even if they landed before goal'"))

    # --- 12.3.1 -----------------------------------------------------------
    # done() is a pure function of minToESS, so points where minToESS did not
    # decrease contribute an integral over an empty range.
    out.append(("12.3.1 a non-decreasing track point contributes nothing",
                weight_integral(0.4, 0.4) == 0.0,
                "the graph 'never goes back' — flat stretches are free"))
    # the weight curve, against Figure 18
    bad = [v for v, want in ((0.0, 0.0), (0.10, 0.508), (0.30, 0.974), (1.0, 0.0))
           if abs(leading_weight(v) - want) > 0.02]
    out.append(("12.3.1 the weight curve matches Figure 18", not bad,
                f"failed at {bad}" if bad else
                "0% → 0.000, 10% → 0.508, 30% → 0.974, 100% → 0.000"))
    # the split used by the parallel path is exact
    import random
    rng = random.Random(20260826)
    worst = 0.0
    for _ in range(200):
        sd, t, d = 55.0, 0.0, 55.0
        s = []
        for _ in range(rng.randint(0, 200)):
            t += rng.uniform(1, 30)
            d = max(0.0, d - rng.uniform(0.001, 0.5))
            s.append((t, d))
        mt = rng.uniform(1000, 20000)
        a, m = leading_partial(s, sd)
        worst = max(worst, abs(leading_from_partial(a, m, sd, mt)
                               - leading_from_partial(*leading_partial(s, sd), sd, mt)))
    out.append(("12.3.1 leadingArea and missingArea split exactly", worst == 0.0,
                f"200 random tracks, worst difference {worst:.3e}"))

    # --- 12.3.1 maxTime ---------------------------------------------------
    out.append(("12.3.1 maxTime is lastESS, extended to a later landing",
                max_time_for(9000.0, 10689.0, 12600.0) == 10689.0
                and max_time_for(13000.0, 10689.0, 12600.0) == 13000.0,
                "landed before lastESS → 10689 s; landed after → own landing "
                "time, not the field maximum"))

    # --- 12 rounding ------------------------------------------------------
    # Components are rounded to 1 dp FIRST, then summed and rounded again.
    out.append(("12 the task score is the sum of already-rounded components",
                task_score(361.7, 470.3, 168.0, 0.0) == 1000.0,
                "361.7 + 470.3 + 168.0 = 1000.0"))
    out.append(("12 every component is rounded to one decimal",
                distance_points(1.0, 3.0, 100.0) == 33.3
                and time_points(3600, 3600, 470.33) == 470.3,
                "1/3 of 100 → 33.3;  full time pot 470.33 → 470.3"))

    # --- 12.3 LeadingFactor ----------------------------------------------
    out.append(("12.3 LeadingFactor is exactly 1 at LCmin",
                leading_factor(1.5, 1.5) == 1.0, "the leader takes the pot"))
    out.append(("12.3 LeadingFactor falls monotonically away from LCmin",
                all(leading_factor(1.0 + i / 20, 1.0)
                    >= leading_factor(1.0 + (i + 1) / 20, 1.0) - 1e-15
                    for i in range(20)),
                "20 samples from LC = LCmin outward"))
    out.append(("12.3 LeadingFactor clamps at zero, never negative",
                leading_factor(99.0, 1.0) == 0.0, "max(0, ...)"))
    out.append(("12.3 a zero or absent LCmin yields no leading points",
                leading_factor(1.0, 0.0) == 0.0,
                "no started pilot → no leading coefficient at all"))
    # the published shape: a higher LCmin is more forgiving at the same ratio
    out.append(("12.3 at the same LC/LCmin ratio a higher LCmin scores lower",
                leading_factor(2.0, 1.0) < leading_factor(1.25, 1.0) and
                leading_factor(4.0, 2.0) <= leading_factor(2.0, 1.0),
                "the family of curves in Figure 16 is ordered by LCmin"))

    # --- 12.3.1 leading_coefficient --------------------------------------
    SS = 50.0
    out.append(("12.3.1 a zero-length speed section yields LC 0",
                leading_coefficient([(0.0, 0.0)], 0.0, 100.0) == 0.0,
                "not a division by zero"))
    out.append(("12.3.1 a pilot who never moved carries the whole missingArea",
                leading_coefficient([], SS, 3600.0) > 0.0,
                f"no samples → LC {leading_coefficient([], SS, 3600.0):.4f}, "
                f"entirely missingArea"))
    early = leading_coefficient([(t, SS - t / 100.0) for t in range(1, 3600, 10)],
                                SS, 3600.0)
    late = leading_coefficient([(t, SS - (t - 1800) / 100.0)
                                for t in range(1800, 5400, 10)], SS, 5400.0)
    out.append(("12.3.1 leading early gives a SMALLER coefficient than leading "
                "late", early < late,
                f"same ground covered, started at 0 s → LC {early:.4f}; started "
                f"at 1800 s → LC {late:.4f}. Smaller is better"))
    # LC_SCALE is an experiment knob and must default to inert
    out.append(("12.3.1 LC_SCALE defaults to 1.0 — no fitted factor is applied",
                S12.LC_SCALE == 1.0,
                "a scale near 2 fits the published result, but fitting a "
                "constant to one task is not verification"))
    return out
