"""Geometry: earth model, cylinders, route optimisation, distance flown.

These are not points formulas — they are what the points formulas measure, so
an error here is uniform and invisible. The route optimiser alone has been
wrong twice (VERIFICATION.md §4.1) and neither bug was visible in a score.
"""

from __future__ import annotations

import math

from engine.rules.cylinder import Crossing, first_contact, line_circle_roots
from engine.rules.distance_flown import distance_flown, distance_to_goal
from engine.rules.earth_model import EARTH_R, Projection, dist, haversine
from engine.rules.route import (best_point_in_disc, leg_lengths,
                                optimise_route, polish_route, remaining_table,
                                route_length, shortest_route_dp)


def run() -> list[tuple[str, bool, str]]:
    out: list[tuple[str, bool, str]] = []

    # --- earth model ------------------------------------------------------
    # The FAI sphere is an ASSUMPTION: S7F 7.1 specifies WGS84. These check the
    # sphere implementation is self-consistent, not that the sphere is right.
    eq = haversine(0.0, 0.0, 0.0, 1.0)
    out.append(("sphere: 1 deg of longitude on the equator",
                abs(eq - EARTH_R * math.pi / 180.0) < 1e-6,
                f"{eq:,.3f} m on R = {EARTH_R:,.0f} m "
                f"(S7F 7.1 says WGS84, which gives 111,319.491 m)"))
    out.append(("haversine is symmetric and zero on itself",
                haversine(45.0, 11.0, 45.0, 11.0) == 0.0
                and abs(haversine(45.0, 11.0, 46.0, 12.0)
                        - haversine(46.0, 12.0, 45.0, 11.0)) < 1e-9,
                "d(a,a) = 0 and d(a,b) = d(b,a)"))
    out.append(("planar distance is a plain hypot",
                dist(0.0, 0.0, 3.0, 4.0) == 5.0, "3-4-5"))

    # The projection is exact AT its anchor and must stay close nearby --
    # that is the whole reason for anchoring it on the task.
    p = Projection(45.80, 11.75)
    out.append(("projection puts its anchor at the origin",
                p.xy(45.80, 11.75) == (0.0, 0.0), "anchor → (0, 0)"))
    worst = 0.0
    for dlat in (-0.2, -0.05, 0.0, 0.05, 0.2):
        for dlon in (-0.2, -0.05, 0.0, 0.05, 0.2):
            a = (45.80 + dlat, 11.75 + dlon)
            x, y = p.xy(*a)
            back = p.latlon(x, y)
            worst = max(worst, haversine(a[0], a[1], back[0], back[1]))
    out.append(("projection round-trips to under a millimetre", worst < 1e-3,
                f"worst closure {worst:.3e} m over a 0.4 deg box"))
    # ...and agrees with haversine over a task-sized envelope
    worst = 0.0
    for dlat, dlon in ((0.0, 0.3), (0.25, 0.0), (0.2, -0.25), (-0.3, 0.15)):
        a, b = (45.80, 11.75), (45.80 + dlat, 11.75 + dlon)
        flat = dist(*p.xy(*a), *p.xy(*b))
        worst = max(worst, abs(flat - haversine(a[0], a[1], b[0], b[1])))
    out.append(("projected distance agrees with haversine to < 0.5 m",
                worst < 0.5,
                f"worst {worst:.4f} m; the FAI tolerance is ±5 m (S7F 9.1.1)"))

    # --- line/circle algebra ---------------------------------------------
    out.append(("a line through the centre has roots at ±r",
                line_circle_roots(-10.0, 0.0, 10.0, 0.0, 0.0, 0.0, 5.0)
                == (0.25, 0.75),
                "segment -10→10 across r=5 → s = 0.25 and 0.75"))
    out.append(("a line that misses has no roots",
                line_circle_roots(-10.0, 9.0, 10.0, 9.0, 0.0, 0.0, 5.0) is None,
                "passing 9 m from the centre of a 5 m circle"))
    out.append(("a zero-length segment has no roots",
                line_circle_roots(1.0, 1.0, 1.0, 1.0, 0.0, 0.0, 5.0) is None,
                "a duplicate fix must not divide by zero"))

    # first_contact interpolates, which is why scoring must not use it
    c = first_contact((-10.0, 0.0, 0.0), (10.0, 0.0, 10.0), 0.0, 0.0, 5.0)
    out.append(("first_contact interpolates — which S7F 9.2.1 forbids for scoring",
                isinstance(c, Crossing) and abs(c.time - 2.5) < 1e-12,
                f"contact at t = {c.time} between fixes at 0 and 10 s; the "
                f"scored time would have to be 10"))
    out.append(("first_contact catches a segment that jumps a small cylinder",
                first_contact((-10.0, 0.0, 0.0), (10.0, 0.0, 1.0),
                              0.0, 0.0, 1.0) is not None,
                "both endpoints outside, path straight through — the KNOWN GAP "
                "in zone_crossing (VERIFICATION.md §7)"))

    # --- route optimisation ----------------------------------------------
    # A cylinder the direct line already crosses costs nothing.
    pts = [(0.0, 0.0, 0.0), (5000.0, 100.0, 2000.0), (10000.0, 0.0, 0.0)]
    px, py = optimise_route(pts, 0)
    out.append(("a cylinder on the direct line adds no distance",
                abs(route_length(px, py, 0) - 10000.0) < 1e-6,
                f"{route_length(px, py, 0):,.3f} m for a 10 km straight line "
                f"through a 2 km cylinder 100 m off it"))
    # ...and one off it costs exactly the detour, with the point on the rim.
    pts = [(0.0, 0.0, 0.0), (5000.0, 5000.0, 1000.0), (10000.0, 0.0, 0.0)]
    px, py = optimise_route(pts, 0)
    r = math.hypot(px[1] - 5000.0, py[1] - 5000.0)
    out.append(("a cylinder off the line puts the point on its rim",
                abs(r - 1000.0) < 1e-6,
                f"point sits {r:,.3f} m from the centre of a 1,000 m cylinder"))
    # symmetry: the answer must not depend on which way round the task runs
    fwd = route_length(*optimise_route(pts, 0), 0)
    rev = route_length(*optimise_route(list(reversed(pts)), 0), 0)
    out.append(("the optimised route is the same in either direction",
                abs(fwd - rev) < 1e-6, f"{fwd:,.4f} m vs {rev:,.4f} m"))

    # the exact single-point minimiser, against the reflection law
    ax, ay, bx, by, cx, cy, rr = 0.0, 0.0, 10000.0, 0.0, 5000.0, 3000.0, 1000.0
    qx, qy = best_point_in_disc(ax, ay, bx, by, cx, cy, rr)
    n1 = math.hypot(qx - ax, qy - ay)
    n2 = math.hypot(qx - bx, qy - by)
    ux = (qx - ax) / n1 + (qx - bx) / n2
    uy = (qy - ay) / n1 + (qy - by) / n2
    nx, ny = (qx - cx) / rr, (qy - cy) / rr
    cross = abs(ux * ny - uy * nx)
    out.append(("the rim point satisfies the reflection law", cross < 1e-6,
                f"|(P-A)/|P-A| + (P-B)/|P-B|  ×  (P-c)| = {cross:.3e} — the "
                f"condition the old midpoint heuristic got wrong"))
    out.append(("a neighbour already inside the disc is the answer",
                best_point_in_disc(5100.0, 3050.0, 0.0, 0.0, cx, cy, rr,
                                   two_legs=False) == (5100.0, 3050.0),
                "one-leg case: no reason to move to the rim"))

    # the DP seed cannot be worse than a feasible route
    pts = [(0.0, 0.0, 0.0), (4000.0, 4000.0, 1500.0), (9000.0, -2000.0, 2500.0),
           (14000.0, 1000.0, 0.0)]
    dpx, dpy = shortest_route_dp(pts, 0, 64, (1.0, 0.5))
    ppx, ppy = polish_route(pts, dpx, dpy, 0)
    full = optimise_route(pts, 0)
    out.append(("polishing never lengthens the DP seed",
                route_length(ppx, ppy, 0) <= route_length(dpx, dpy, 0) + 1e-9,
                f"DP {route_length(dpx, dpy, 0):,.1f} m → polished "
                f"{route_length(ppx, ppy, 0):,.1f} m"))
    out.append(("the multi-start driver beats or matches either seed alone",
                route_length(*full, 0) <= route_length(ppx, ppy, 0) + 1e-6,
                f"driver {route_length(*full, 0):,.1f} m"))

    # --- the per-waypoint tables -----------------------------------------
    px = [0.0, 1000.0, 3000.0, 6000.0]
    py = [0.0, 0.0, 0.0, 0.0]
    legs = leg_lengths(px, py, 0)
    rem = remaining_table(legs)
    out.append(("leg lengths and the remaining table agree",
                legs[:3] == [1000.0, 2000.0, 3000.0] and rem[0] == 6000.0
                and rem[3] == 0.0,
                f"legs {legs[:3]}, remaining {rem}"))
    out.append(("legs before the route start are excluded",
                leg_lengths(px, py, 1)[0] == 0.0
                and remaining_table(leg_lengths(px, py, 1))[1] == 5000.0,
                "starting at index 1 → the first leg is off-route"))

    # --- distance to goal / distance flown --------------------------------
    out.append(("distance to goal is measured to the cylinder EDGE",
                distance_to_goal(0.0, 0.0, 8000.0, 0.0, 7500.0, 1000.0)
                == 1500.0,
                "8,000 m from the centre of a 7,500 m cylinder with 1,000 m "
                "beyond it → 500 + 1,000"))
    out.append(("a pilot already inside the zone has no edge distance",
                distance_to_goal(0.0, 0.0, 100.0, 0.0, 7500.0, 1000.0)
                == 1000.0, "inside → clamped at zero, not negative"))
    out.append(("distance flown is the task less the best remaining",
                distance_flown(60000.0, 12000.0) == 48000.0
                and distance_flown(60000.0, 12000.0, reached_goal=True)
                == 60000.0,
                "48 km flown; a pilot in goal gets the full task distance"))
    out.append(("distance flown is never negative",
                distance_flown(60000.0, 99000.0) == 0.0,
                "a remaining distance larger than the task clamps at zero"))
    return out
