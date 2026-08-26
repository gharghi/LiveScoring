"""S7F 11 — points allocation.

Moved out of engine/rules/ so the rules files hold rules and nothing
else. Run with `python3 -m tests` or `./run.py --verify`.
"""

from __future__ import annotations

import math

from engine.rules.params import GapParams
import engine.rules.s7f_11_allocation as M
from engine.rules.s7f_11_allocation import (LEADING_TIME_RATIO_MAX,
                                            Allocation, allocate,
                                            available_points)


def run() -> list[tuple[str, bool, str]]:
    """Section 11 against its own published text, not against this code.

    Every expectation below is either a sentence from Section 11 turned into a
    number, or the published cubic evaluated independently.
    """
    out: list[tuple[str, bool, str]] = []

    def p(ltr=0.26):
        return GapParams(nominal_distance=60000, minimum_distance=5000,
                         nominal_time=5400, leading_time_ratio=ltr)

    def cubic(gr):
        return 0.9 - 1.665 * gr + 1.713 * gr ** 2 - 0.587 * gr ** 3

    # --- the cubic, sampled across its range -----------------------------
    bad = [gr for gr in (0.0, 0.1, 0.25, 0.5, 0.75, 0.9, 1.0)
           if abs(allocate(1.0, int(gr * 1000), 1000, p()).distance_weight
                  - cubic(gr)) > 1e-12]
    out.append(("11 distanceWeight matches the published cubic at 7 points",
                not bad, f"failed at {bad}" if bad else
                "gr 0 → 0.900000, gr 0.5 → 0.427125, gr 1 → 0.361000"))

    # --- "Weight factors are always between 0 and 1" ---------------------
    bad = []
    for n in range(0, 1001, 7):
        a = allocate(1.0, n, 1000, p())
        for name, w in (("distance", a.distance_weight),
                        ("leading", a.leading_weight),
                        ("arrival", a.arrival_weight),
                        ("time", a.time_weight)):
            if not (-1e-12 <= w <= 1.0 + 1e-12):
                bad.append((n, name, w))
    out.append(("11 every weight factor stays within [0, 1]", not bad,
                f"{len(bad)} out of range, e.g. {bad[:2]}" if bad
                else "144 goal ratios × 4 weights"))

    # --- the four weights partition 1.0 ----------------------------------
    worst = 0.0
    for n in range(0, 1001, 7):
        a = allocate(1.0, n, 1000, p())
        worst = max(worst, abs(a.distance_weight + a.leading_weight
                               + a.arrival_weight + a.time_weight - 1.0))
    out.append(("11 the four weights sum to exactly 1", worst < 1e-12,
                f"worst deviation {worst:.3e}"))

    # --- "A weight factor of 0.5 for distance means 50% of the day's
    #      available overall points are available for distance points" -----
    a = allocate(1.0, 0, 100, p())
    out.append(("11 available points are 1000 × TaskValidity × weight",
                abs(a.available_distance - 1000.0 * a.distance_weight) < 1e-9,
                f"weight {a.distance_weight:.6f} → {a.available_distance:.4f} pts"))
    half = allocate(0.5, 0, 100, p())
    out.append(("11 task validity scales every pot linearly",
                abs(half.available_total - 0.5 * a.available_total) < 1e-9,
                f"validity 1.0 → {a.available_total:.1f}, "
                f"0.5 → {half.available_total:.1f}"))

    # --- the gr == 0 branch, and that it is a discontinuity ---------------
    zero = allocate(1.0, 0, 100, p())
    out.append(("11 [PG] with nobody in goal leading takes the WHOLE "
                "non-distance share", abs(zero.leading_weight - 0.1) < 1e-12
                and abs(zero.time_weight) < 1e-12,
                f"distance {zero.distance_weight:.3f}, leading "
                f"{zero.leading_weight:.3f}, time {zero.time_weight:.3f}"))
    one = allocate(1.0, 1, 100, p())
    jump = zero.available_leading - one.available_leading
    out.append(("11 the gr==0 branch is a discontinuity, not a limit", jump > 60.0,
                f"0 in goal → {zero.available_leading:.1f} leading pts; "
                f"1 of 100 → {one.available_leading:.1f}; one finisher moves "
                f"the pot by {jump:.1f}"))

    # --- arrival is never awarded ----------------------------------------
    bad = [n for n in range(0, 101, 5)
           if allocate(1.0, n, 100, p()).available_arrival != 0.0]
    out.append(("11 [PG] arrival weight is zero at every goal ratio", not bad,
                f"nonzero at {bad}" if bad else "21 goal ratios"))

    # --- LeadingTimeRatio drives the leading/time split ------------------
    bad = []
    for ltr in (0.0, 0.05, 0.13, 0.26):
        a = allocate(1.0, 50, 100, p(ltr))
        if abs(a.leading_weight - (1.0 - a.distance_weight) * ltr) > 1e-12:
            bad.append(ltr)
        if ltr > 0 and abs(a.implied_leading_time_ratio - ltr) > 1e-9:
            bad.append(("implied", ltr))
    out.append(("11 leadingWeight is LeadingTimeRatio of the non-distance "
                "share, and is recoverable from the pots", not bad,
                f"failed at {bad}" if bad else
                "0%, 5%, 13%, 26% — forward and inverted"))

    # --- LeadingTimeRatio 0 puts everything into time --------------------
    a = allocate(1.0, 50, 100, p(0.0))
    out.append(("11 LeadingTimeRatio 0 leaves no leading points",
                a.available_leading == 0.0
                and abs(a.available_time - 1000.0 * (1 - a.distance_weight)) < 1e-9,
                f"leading {a.available_leading:.1f}, time {a.available_time:.1f}"))

    # --- the legal range the Code states ---------------------------------
    out.append(("11 LeadingTimeRatio's stated maximum is 26%",
                LEADING_TIME_RATIO_MAX == 0.26,
                "0..26%, default 26% in paragliding (S7F 11)"))

    # --- nobody flying is not a division by zero -------------------------
    a = allocate(1.0, 0, 0, p())
    out.append(("11 an empty field is handled", a.goal_ratio == 0.0,
                f"0 flying → gr 0.0, distance weight {a.distance_weight:.3f}"))

    # --- the rounding policy actually switches ---------------------------
    was = M.ROUNDING
    try:
        M.ROUNDING = "integer"
        r = allocate(1.0, 111, 129, p(0.2632))
        M.ROUNDING = "none"
        n = allocate(1.0, 111, 129, p(0.2632))
        ok = (r.available_distance == 362.0 and abs(n.available_distance - 361.66) < 0.01)
        out.append(("11 round(...,0) vs unrounded, on the reference task", ok,
                    f"Code as written → {r.available_distance:.0f}/"
                    f"{r.available_leading:.0f}/{r.available_time:.0f}; "
                    f"unrounded → {n.available_distance:.1f}/"
                    f"{n.available_leading:.1f}/{n.available_time:.1f}; "
                    f"official → 361.7/168.0/470.3"))
    finally:
        M.ROUNDING = was

    # --- available_points, and the rounding policy on its own -------------
    # NOTE the tolerance: the pots are deliberately NOT rounded (see conflict 1
    # in the module), so 1000 × 0.3617 is 361.70000000000005 and an exact
    # comparison would fail. S7F 12 does the rounding, one level down.
    out.append(("11 available_points is 1000 × validity × weight",
                abs(available_points(1.0, 0.3617) - 361.7) < 1e-9
                and abs(available_points(0.5, 0.9) - 450.0) < 1e-9,
                f"1.0 × 0.3617 → {available_points(1.0, 0.3617)!r}, unrounded"))
    was = M.ROUNDING
    try:
        M.ROUNDING = "integer"
        r = available_points(1.0, 0.36166)
        M.ROUNDING = "nonsense"
        try:
            available_points(1.0, 0.5)
            rejected = False
        except ValueError:
            rejected = True
    finally:
        M.ROUNDING = was
    out.append(("11 round(...,0) gives whole points", r == 362.0,
                f"361.66 → {r:.0f}; the official shows 361.7"))
    out.append(("11 an unknown rounding policy is rejected", rejected,
                "a typo must not silently fall through to unrounded"))

    # --- Allocation ------------------------------------------------------
    a = allocate(1.0, 111, 129, p(0.2632))
    out.append(("11 available_total sums the four pots",
                abs(a.available_total - (a.available_distance + a.available_time
                                         + a.available_leading
                                         + a.available_arrival)) < 1e-12
                and abs(a.available_total - 1000.0) < 1e-9,
                f"{a.available_total:.4f}"))
    out.append(("11 LeadingTimeRatio is recoverable from the pots alone",
                abs(a.implied_leading_time_ratio - 0.2632) < 1e-9,
                f"leading/(leading+time) = {a.implied_leading_time_ratio:.6f} — "
                f"invariant under distanceWeight, which is how the reference "
                f"task's 26.32% was read out of its published result"))
    out.append(("11 an empty Allocation is all zeros",
                Allocation().available_total == 0.0
                and Allocation().implied_leading_time_ratio == 0.0,
                "no division by zero on a task nobody flew"))
    return out
