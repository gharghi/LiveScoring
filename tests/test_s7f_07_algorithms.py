"""S7F 7.1 — the nine algorithms.

Moved out of engine/rules/ so the rules files hold rules and nothing
else. Run with `python3 -m tests` or `./run.py --verify`.
"""

from __future__ import annotations

import math

from engine.rules.s7f_71_algorithms import (CartesianToGeodesic,
                                            FindTaskAreaCentre,
                                            GeodesicLine,
                                            OptimisedRoute,
                                            PathFinder,
                                            RouteOptimizer,
                                            DirectGeodesic,
                                            EllipsoidDistance,
                                            GeodesicToCartesian,
                                            InverseGeodesic, WGS84_A)


def run() -> list[tuple[str, bool, str]]:
    """Published reference values for the geodesics, and round-trip identities.

    Run with `./run.py --rules --check-71`. These check the IMPLEMENTATIONS
    against known-good numbers; they cannot check them against the Code, which
    is what the CHECK THIS markers are for.
    """
    out = []

    def dms(d, m, sec):
        return d + m / 60.0 + sec / 3600.0

    # --- analytic references: no third party involved -----------------
    # One degree of longitude on the equator is exactly a * pi/180, by the
    # definition of the semi-major axis. Nothing to look up and nothing to
    # get wrong.
    eq = EllipsoidDistance(0.0, 0.0, 0.0, 1.0)
    exact = WGS84_A * math.pi / 180.0
    out.append(("equator, 1 deg of longitude == a * pi/180",
                abs(eq - exact) < 1e-6,
                f"{eq:.6f} m vs analytic {exact:.6f} m  (Δ {eq - exact:+.2e})"))

    # --- published WGS84 values ---------------------------------------
    md = EllipsoidDistance(0.0, 0.0, 1.0, 0.0)
    out.append(("meridian arc, 1 deg from the equator", abs(md - 110574.3886) < 5e-4,
                f"{md:.4f} m, published 110574.3886 m"))
    qm = EllipsoidDistance(0.0, 0.0, 90.0, 0.0)
    out.append(("quarter meridian, equator to pole", abs(qm - 10001965.729) < 5e-3,
                f"{qm:.3f} m, published 10001965.729 m"))

    # --- Vincenty's own published test line ---------------------------
    # Flinders Peak -> Buninyong. Coordinates converted from the DMS in the
    # paper; getting these wrong by 4e-6 deg moves the answer by 0.2 m, which
    # is a good reminder of what precision this arithmetic works at.
    fp = (-dms(37, 57, 3.72030), dms(144, 25, 29.52440))
    bu = (-dms(37, 39, 10.15610), dms(143, 55, 35.38390))
    g = InverseGeodesic(fp[0], fp[1], bu[0], bu[1])
    out.append(("InverseGeodesic vs Vincenty's published test line",
                abs(g.distance - 54972.271) < 1e-3,
                f"{g.distance:.4f} m, published 54972.271 m"))
    out.append(("  ...and its initial azimuth",
                abs(g.azimuth1 - dms(306, 52, 5.37)) < 1e-5,
                f"{g.azimuth1:.6f} deg, published {dms(306, 52, 5.37):.6f} deg"))
    # CONVENTION: azimuth2 here is the FORWARD azimuth at point 2, i.e. the
    # direction you are still travelling on arrival. Vincenty's table prints
    # the reverse azimuth, which is this minus 180.
    out.append(("  ...and its final azimuth (forward convention)",
                abs((g.azimuth2 - 180.0) - dms(127, 10, 25.07)) < 1e-5,
                f"{g.azimuth2:.6f} deg forward = {g.azimuth2 - 180.0:.6f} deg "
                f"reverse, published {dms(127, 10, 25.07):.6f} deg reverse"))

    # --- identities ---------------------------------------------------
    lat2, lon2, _ = DirectGeodesic(fp[0], fp[1], g.azimuth1, g.distance)
    err = EllipsoidDistance(lat2, lon2, bu[0], bu[1])
    out.append(("DirectGeodesic inverts InverseGeodesic", err < 1e-6,
                f"round trip closes to {err:.3e} m"))

    centre = (45.80, 11.75)
    worst = 0.0
    for dlat, dlon in ((0.0, 0.0), (0.1, 0.1), (-0.2, 0.15), (0.35, -0.3)):
        p = (centre[0] + dlat, centre[1] + dlon)
        x, y = GeodesicToCartesian(p[0], p[1], centre)
        back = CartesianToGeodesic(x, y, centre)
        worst = max(worst, EllipsoidDistance(p[0], p[1], back[0], back[1]))
    out.append(("GeodesicToCartesian / CartesianToGeodesic round trip",
                worst < 1e-6, f"worst closure {worst:.3e} m over a 0.35 deg box"))

    # Distance from the anchor is exact by construction in an azimuthal
    # equidistant projection. That is true ONLY at the anchor, which is
    # precisely why ProjectionCorrection (8) exists.
    p = (centre[0] + 0.3, centre[1] + 0.25)
    x, y = GeodesicToCartesian(p[0], p[1], centre)
    out.append(("projected distance from the anchor is the geodesic distance",
                abs(math.hypot(x, y)
                    - EllipsoidDistance(centre[0], centre[1], p[0], p[1])) < 1e-9,
                f"{math.hypot(x, y):.6f} m"))

    # ...and how wrong the projection gets AWAY from the anchor, which is the
    # size of the error ProjectionCorrection has to undo.
    a = (centre[0] + 0.25, centre[1] + 0.25)
    b = (centre[0] + 0.30, centre[1] + 0.30)
    ax, ay = GeodesicToCartesian(a[0], a[1], centre)
    bx, by = GeodesicToCartesian(b[0], b[1], centre)
    flat = math.hypot(bx - ax, by - ay)
    true = EllipsoidDistance(a[0], a[1], b[0], b[1])
    out.append(("projection distortion 30 km off the anchor (informational)",
                True,
                f"chord {flat:.4f} m vs geodesic {true:.4f} m  "
                f"→ {(flat - true) / true * 100:+.4f}%"))

    out.append(("InverseGeodesic on coincident points",
                InverseGeodesic(45.0, 11.0, 45.0, 11.0).distance == 0.0, "0 m"))


    # --- 7 FindTaskAreaCentre ------------------------------------------
    pts = [(45.0, 11.0), (46.0, 12.0), (45.5, 11.5)]
    c = FindTaskAreaCentre(pts)
    out.append(("7.1 (7) the task area centre is the mean of the turnpoints",
                abs(c[0] - 45.5) < 1e-12 and abs(c[1] - 11.5) < 1e-12,
                f"{c} — CHECK THIS: bounding box, radius-weighted and smallest "
                f"enclosing circle are all readable alternatives"))
    out.append(("7.1 (7) a single point is its own centre",
                FindTaskAreaCentre([(45.0, 11.0)]) == (45.0, 11.0), "identity"))
    try:
        FindTaskAreaCentre([])
        ok = False
    except ValueError:
        ok = True
    out.append(("7.1 (7) no points is an error, not a silent (0, 0)", ok,
                "an empty task must not project onto the Gulf of Guinea"))

    # --- 2 PathFinder ---------------------------------------------------
    zones = [(0.0, 0.0, 0.0), (5000.0, 5000.0, 1000.0), (10000.0, 0.0, 0.0)]
    px, py = PathFinder(zones, 0)
    r = math.hypot(px[1] - 5000.0, py[1] - 5000.0)
    out.append(("7.1 (2) PathFinder is Cartesian and puts the point on the rim",
                abs(r - 1000.0) < 1e-6,
                f"{r:,.3f} m from the centre of a 1,000 m cylinder — 'in "
                f"Cartesian geometry', per the Code"))

    # --- 9 RouteOptimizer, end to end -----------------------------------
    task = [(45.80, 11.70, 400.0), (45.85, 11.80, 2000.0),
            (45.78, 11.90, 1000.0), (45.82, 11.72, 400.0)]
    ro = RouteOptimizer(task, 0)
    out.append(("7.1 (9) RouteOptimizer returns points, legs and a distance",
                isinstance(ro, OptimisedRoute) and len(ro.points) == len(task)
                and abs(sum(ro.legs) - ro.distance) < 1e-9,
                f"{ro.distance/1000:.4f} km over {len(task)} zones; legs sum to "
                f"the total"))
    # every boundary point must sit on its real WGS84 boundary after 7.1.7
    worst = 0.0
    for i, (lat, lon, rad) in enumerate(task):
        if rad <= 0 or i in (0, len(task) - 1):
            continue
        d = EllipsoidDistance(lat, lon, ro.points[i][0], ro.points[i][1])
        worst = max(worst, abs(d - rad) if d > rad - 1.0 else 0.0)
    out.append(("7.1 (8) ProjectionCorrection lands the points ON the boundary",
                worst < 1e-3,
                f"worst deviation from the nominal radius {worst:.3e} m, "
                f"measured on the ellipsoid"))
    # the whole point of 7.1.7: the answer stops depending on the anchor
    import engine.rules.s7f_71_algorithms as M
    orig = M.FindTaskAreaCentre
    try:
        M.FindTaskAreaCentre = lambda pts: (46.5, 12.5)
        far = RouteOptimizer(task, 0).distance
    finally:
        M.FindTaskAreaCentre = orig
    out.append(("7.1 (8) the route is independent of the projection anchor",
                abs(far - ro.distance) < 0.5,
                f"anchor at the task centre → {ro.distance:,.3f} m; anchor 80 km "
                f"away → {far:,.3f} m. This is what 7.1.7 exists to guarantee"))

    out.append(("7.1 (5) InverseGeodesic returns a named result",
                isinstance(InverseGeodesic(45.0, 11.0, 46.0, 12.0), GeodesicLine),
                "distance, azimuth1, azimuth2"))
    return out
