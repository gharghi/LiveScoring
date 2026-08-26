"""S7F 9 — control zones, crossings, scored distance, best time."""

from __future__ import annotations

from dataclasses import dataclass

import engine.rules.s7f_09_control_zones as S9
from engine.rules.s7f_09_control_zones import (ABSOLUTE_TOLERANCE,
                                               RADIUS_TOLERANCE, best_time,
                                               counts_for_best_time, in_zone,
                                               inner_radius, measurement_radius,
                                               outer_radius, scored_distance,
                                               validates_zone, zone_crossing)


@dataclass
class _P:
    speed_section_time: float | None
    goal_time: float | None
    ess_time: float | None


def run() -> list[tuple[str, bool, str]]:
    out: list[tuple[str, bool, str]] = []

    # --- 9.1.1  tolerance -------------------------------------------------
    out.append(("9.1.1 radiusTolerance is 0.0% in the 2026 edition",
                RADIUS_TOLERANCE == 0.0 and ABSOLUTE_TOLERANCE == 5.0,
                "so the tolerance is a FLAT ±5 m at every radius. An earlier "
                "draft used 0.5%, which on a 17 km cylinder is 85 m"))
    bad = [r for r in (100.0, 200.0, 1000.0, 4000.0, 7500.0, 17000.0)
           if inner_radius(r) != r - 5.0 or outer_radius(r) != r + 5.0]
    out.append(("9.1.1 every radius gets exactly ±5 m", not bad,
                f"failed at {bad}" if bad else
                "100 m → 95/105 … 17 km → 16,995/17,005"))
    out.append(("9.1.1 the zone is an annulus, inner < outer",
                all(inner_radius(r) < outer_radius(r)
                    for r in (100.0, 17000.0)), "inner < outer at both extremes"))

    # --- 9.3  the measurement radius policy -------------------------------
    was = S9.MEASUREMENT_RADIUS
    try:
        vals = {}
        for mode in ("nominal", "outer", "inner"):
            S9.MEASUREMENT_RADIUS = mode
            vals[mode] = measurement_radius(1000.0)
        ok = vals == {"nominal": 1000.0, "outer": 1005.0, "inner": 995.0}
        out.append(("9.3 the measurement radius policy selects all three", ok,
                    f"{vals} — VALIDATION always uses the tolerance zone; this "
                    f"is what DISTANCE is measured to, and the two used not to "
                    f"agree"))
        S9.MEASUREMENT_RADIUS = "nonsense"
        try:
            measurement_radius(1000.0)
            ok2 = False
        except ValueError:
            ok2 = True
        out.append(("9.3 an unknown measurement radius is rejected", ok2,
                    "a typo must not silently fall through to nominal"))
    finally:
        S9.MEASUREMENT_RADIUS = was

    # --- 9.2 / 9.2.1  crossings -------------------------------------------
    inner, outer = 995.0, 1005.0
    c = zone_crossing((0.0, 0.0, 10.0), (2000.0, 0.0, 11.0), 0.0, 0.0, inner, outer)
    out.append(("9.2.1 the crossing time is the tracklog point's timestamp",
                c is not None and c[0] == 11.0,
                f"fixes at 10 s and 11 s → crossing at {c[0]} s, NOT an "
                f"interpolated 10.5"))
    out.append(("9.2 a crossing is detected in both directions",
                zone_crossing((0.0, 0.0, 0.0), (2000.0, 0.0, 1.0), 0.0, 0.0,
                              inner, outer) is not None
                and zone_crossing((2000.0, 0.0, 0.0), (0.0, 0.0, 1.0), 0.0, 0.0,
                                  inner, outer) is not None,
                "S7F 6.2.1: 'the direction in which such a crossing occurs is "
                "irrelevant'"))
    out.append(("9.2 the outward flag is diagnostic only",
                zone_crossing((0.0, 0.0, 0.0), (2000.0, 0.0, 1.0), 0.0, 0.0,
                              inner, outer)[1] is True
                and zone_crossing((2000.0, 0.0, 0.0), (0.0, 0.0, 1.0), 0.0, 0.0,
                                  inner, outer)[1] is False,
                "reported, but must not affect validation"))
    out.append(("9.2 sitting still inside the zone is not a crossing",
                zone_crossing((100.0, 0.0, 0.0), (110.0, 0.0, 1.0), 0.0, 0.0,
                              inner, outer) is None,
                "no transition → None. This is what stops a pilot orbiting "
                "inside the SSS generating a start on every fix"))
    out.append(("9.2 staying entirely outside is not a crossing",
                zone_crossing((5000.0, 0.0, 0.0), (6000.0, 0.0, 1.0), 0.0, 0.0,
                              inner, outer) is None, "None"))

    out.append(("9.2 in_zone tests the outer boundary",
                in_zone(1004.0, 0.0, 0.0, 0.0, outer)
                and not in_zone(1006.0, 0.0, 0.0, 0.0, outer),
                "1,004 m inside a 1,005 m boundary; 1,006 m outside"))

    # validates_zone is the reduction the hot loop uses
    out.append(("9.2 validates_zone == 'either end within the outer boundary'",
                validates_zone(2000.0, 500.0, outer)
                and validates_zone(500.0, 2000.0, outer)
                and validates_zone(500.0, 700.0, outer)
                and not validates_zone(2000.0, 3000.0, outer),
                "the equivalence proved exhaustively against zone_crossing by "
                "tests/test_field_invariants.check_inlined_zone_test"))

    # --- 9.3  scored distance --------------------------------------------
    out.append(("9.3 the minimum-distance floor applies to anyone airborne",
                scored_distance(1000.0, 5000.0) == 5000.0,
                "1 km flown, 5 km minimum → 5 km"))
    out.append(("9.3 a pilot beyond the minimum keeps their distance",
                scored_distance(42000.0, 5000.0) == 42000.0, "42 km"))
    out.append(("9.3 a pilot who never launched scores nothing",
                scored_distance(42000.0, 5000.0, airborne=False) == 0.0,
                "not even the minimum"))
    out.append(("9.3 a negative route distance clamps to the floor",
                scored_distance(-100.0, 5000.0) == 5000.0, "5 km"))

    # --- 9.4 / 9.4.1  best time ------------------------------------------
    out.append(("9.4.1 [PG] only a pilot in GOAL contributes a time",
                counts_for_best_time(True, True, paragliding=True)
                and not counts_for_best_time(False, True, paragliding=True),
                "reaching ESS is not enough for paragliding"))
    out.append(("9.4.1 [HG] reaching ESS is enough",
                counts_for_best_time(False, True, paragliding=False),
                "the discipline difference, and it moves the whole board"))

    field = [_P(6000.0, 100.0, 100.0),      # in goal, slower
             _P(5000.0, None, 90.0),        # ESS only, FASTER
             _P(7000.0, 120.0, 120.0)]      # in goal, slowest
    out.append(("9.4 [PG] the fastest ESS-only pilot does NOT set best time",
                best_time(field, paragliding=True) == 6000.0,
                "the 5,000 s ESS-only flight is ignored; best time is the "
                "6,000 s pilot who actually finished"))
    out.append(("9.4 [HG] the same field gives a different best time",
                best_time(field, paragliding=False) == 5000.0,
                "5,000 s — a time nobody in goal achieved"))
    out.append(("9.4 no finisher means there is no best time",
                best_time([_P(5000.0, None, 90.0)], paragliding=True) is None,
                "None, which sends S7F 10.3 to its distance fallback"))
    out.append(("9.4 an empty field has no best time",
                best_time([], paragliding=True) is None, "None"))
    return out
