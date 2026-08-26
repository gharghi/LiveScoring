"""S7F 7.1 — THE NINE ALGORITHMS, all in one file, for checking against the Code.

Everything the Sporting Code lists under 7.1 is here, under the Code's own
names, in the Code's own order. One file on purpose: these nine are a single
interlocking specification and reading them apart is how you miss that they
disagree.

    1  GeodesicToCartesian    7.1.1   lat/lon -> local Cartesian
    2  PathFinder             7.1.3   shortest path via control zones, Cartesian
    3  CartesianToGeodesic    7.1.1   local Cartesian -> lat/lon
    4  DirectGeodesic         7.1.2?  point at a given distance and bearing
    5  InverseGeodesic        7.1.2?  distance and bearing between two points
    6  EllipsoidDistance      7.1.5   distance only, cheaper than InverseGeodesic
    7  FindTaskAreaCentre     7.1.6   the projection anchor
    8  ProjectionCorrection   7.1.7   put path points back on their real boundaries
    9  RouteOptimizer         7.1.8   the driver: shortest path ON THE ELLIPSOID

================================================================================
  HOW MUCH OF THIS IS THE CODE, AND HOW MUCH IS ME
================================================================================

I have the Code's NAMES and its ONE-LINE PURPOSES. I do not have the Code's
text for any of the nine. So:

  * the names, the numbering and the stated purposes are the Code's;
  * every BODY below is a standard implementation of that stated purpose,
    written by me, NOT transcribed from the Code;
  * anywhere the Code could reasonably specify something different from the
    standard choice, there is a CHECK THIS marker.

Treat this file as a proposal to diff against 7.1, not as a transcription of
it. The markers are where the diffing effort is worth spending.

================================================================================
  WHAT THIS ALREADY TELLS US, WITHOUT THE TEXT
================================================================================

The list alone settles a question the engine had open, and settles it against
the current implementation:

  THE EARTH MODEL IS THE WGS84 ELLIPSOID, NOT A SPHERE.

Four of the nine say so outright — GeodesicToCartesian works "on the WGS84
ellipsoid", DirectGeodesic and InverseGeodesic are ellipsoid operations, and
EllipsoidDistance is named for it. The running engine uses the FAI sphere,
R = 6 371 000 m, in engine/rules/earth_model.py. Over this competition's
waypoints WGS84 geodesics total 0.18% MORE than sphere distances, which is
about half of the 0.42% by which the engine's optimised route falls short of
the published result.

The list also describes a PIPELINE the engine does not have. The engine
optimises the route in a projected plane and measures it in that plane. The
Code's RouteOptimizer (9) projects to Cartesian, runs PathFinder (2) there,
projects back, then applies ProjectionCorrection (8) so the path points sit on
their real control-zone boundaries on the ellipsoid, and only then measures
with EllipsoidDistance (6). ProjectionCorrection exists precisely because the
projection distorts, and skipping it means measuring a path whose points are
not quite on the cylinders they are supposed to touch.

So there are two distinct differences to close, and they compound:
the earth model, and the correct-then-measure step.

================================================================================
"""

from __future__ import annotations

import math
from dataclasses import dataclass

# --- WGS84, the defining constants ---------------------------------------
# CHECK THIS: that the Code uses WGS84 with these values and not, say, a
# different flattening. These are the standard defining parameters.

WGS84_A = 6378137.0                 # semi-major axis, metres (defining)
WGS84_F = 1.0 / 298.257223563       # flattening (defining)
WGS84_B = WGS84_A * (1.0 - WGS84_F)  # semi-minor axis, derived

_ITER_LIMIT = 200
_CONVERGENCE = 1e-12


# =========================================================================
#  4 & 5.  DirectGeodesic and InverseGeodesic
#          "to find a point on the WGS84 ellipsoid that is a given distance
#           and direction from a given point"
#          "to find distance and direction between two points on the WGS84
#           ellipsoid"
# =========================================================================
#
# Implemented with Vincenty's formulae, which is the usual choice and is
# accurate to about 0.5 mm on the ellipsoid.
#
# CHECK THIS: whether the Code names an algorithm. Vincenty fails to converge
# for near-antipodal points; Karney's method does not, and is what modern
# libraries use. Task-sized distances are nowhere near antipodal, so the
# difference is theoretical here, but if the Code specifies Karney this should
# say Karney.


@dataclass(frozen=True, slots=True)
class GeodesicLine:
    """The result of InverseGeodesic: how to get from point 1 to point 2."""

    distance: float          # metres along the geodesic
    azimuth1: float          # initial bearing at point 1, degrees from north
    azimuth2: float          # final bearing at point 2, degrees from north


def InverseGeodesic(lat1: float, lon1: float,
                    lat2: float, lon2: float) -> GeodesicLine:
    """S7F 7.1 (5) — distance and direction between two WGS84 points.

    Vincenty inverse. Returns metres and degrees.
    """
    a, f, b = WGS84_A, WGS84_F, WGS84_B
    if lat1 == lat2 and lon1 == lon2:
        return GeodesicLine(0.0, 0.0, 0.0)

    L = math.radians(lon2 - lon1)
    U1 = math.atan((1 - f) * math.tan(math.radians(lat1)))
    U2 = math.atan((1 - f) * math.tan(math.radians(lat2)))
    sinU1, cosU1 = math.sin(U1), math.cos(U1)
    sinU2, cosU2 = math.sin(U2), math.cos(U2)

    lam = L
    sin_sigma = cos_sigma = sigma = cos_sq_alpha = cos2_sigma_m = 0.0
    sin_lam = cos_lam = 0.0
    for _ in range(_ITER_LIMIT):
        sin_lam, cos_lam = math.sin(lam), math.cos(lam)
        sin_sigma = math.hypot(cosU2 * sin_lam,
                               cosU1 * sinU2 - sinU1 * cosU2 * cos_lam)
        if sin_sigma == 0.0:
            return GeodesicLine(0.0, 0.0, 0.0)      # coincident
        cos_sigma = sinU1 * sinU2 + cosU1 * cosU2 * cos_lam
        sigma = math.atan2(sin_sigma, cos_sigma)
        sin_alpha = cosU1 * cosU2 * sin_lam / sin_sigma
        cos_sq_alpha = 1.0 - sin_alpha * sin_alpha
        cos2_sigma_m = (cos_sigma - 2.0 * sinU1 * sinU2 / cos_sq_alpha
                        if cos_sq_alpha != 0.0 else 0.0)   # equatorial line
        C = f / 16.0 * cos_sq_alpha * (4.0 + f * (4.0 - 3.0 * cos_sq_alpha))
        lam_prev = lam
        lam = L + (1.0 - C) * f * sin_alpha * (
            sigma + C * sin_sigma * (
                cos2_sigma_m + C * cos_sigma * (-1.0 + 2.0 * cos2_sigma_m ** 2)))
        if abs(lam - lam_prev) < _CONVERGENCE:
            break
    else:                                            # pragma: no cover
        raise ValueError("InverseGeodesic did not converge (near-antipodal)")

    u_sq = cos_sq_alpha * (a * a - b * b) / (b * b)
    A = 1.0 + u_sq / 16384.0 * (
        4096.0 + u_sq * (-768.0 + u_sq * (320.0 - 175.0 * u_sq)))
    B = u_sq / 1024.0 * (256.0 + u_sq * (-128.0 + u_sq * (74.0 - 47.0 * u_sq)))
    d_sigma = B * sin_sigma * (
        cos2_sigma_m + B / 4.0 * (
            cos_sigma * (-1.0 + 2.0 * cos2_sigma_m ** 2)
            - B / 6.0 * cos2_sigma_m * (-3.0 + 4.0 * sin_sigma ** 2)
            * (-3.0 + 4.0 * cos2_sigma_m ** 2)))

    s = b * A * (sigma - d_sigma)
    az1 = math.atan2(cosU2 * sin_lam, cosU1 * sinU2 - sinU1 * cosU2 * cos_lam)
    az2 = math.atan2(cosU1 * sin_lam, -sinU1 * cosU2 + cosU1 * sinU2 * cos_lam)
    return GeodesicLine(s, math.degrees(az1) % 360.0, math.degrees(az2) % 360.0)


def DirectGeodesic(lat1: float, lon1: float, azimuth1: float,
                   distance: float) -> tuple[float, float, float]:
    """S7F 7.1 (4) — the point `distance` metres from (lat1, lon1) on bearing
    `azimuth1` degrees. Returns (lat2, lon2, azimuth2).

    Vincenty direct.
    """
    a, f, b = WGS84_A, WGS84_F, WGS84_B
    if distance == 0.0:
        return lat1, lon1, azimuth1

    alpha1 = math.radians(azimuth1)
    sin_a1, cos_a1 = math.sin(alpha1), math.cos(alpha1)
    tanU1 = (1 - f) * math.tan(math.radians(lat1))
    cosU1 = 1.0 / math.sqrt(1.0 + tanU1 * tanU1)
    sinU1 = tanU1 * cosU1
    sigma1 = math.atan2(tanU1, cos_a1)
    sin_alpha = cosU1 * sin_a1
    cos_sq_alpha = 1.0 - sin_alpha * sin_alpha
    u_sq = cos_sq_alpha * (a * a - b * b) / (b * b)
    A = 1.0 + u_sq / 16384.0 * (
        4096.0 + u_sq * (-768.0 + u_sq * (320.0 - 175.0 * u_sq)))
    B = u_sq / 1024.0 * (256.0 + u_sq * (-128.0 + u_sq * (74.0 - 47.0 * u_sq)))

    sigma = distance / (b * A)
    sin_sigma = cos_sigma = cos2_sigma_m = 0.0
    for _ in range(_ITER_LIMIT):
        cos2_sigma_m = math.cos(2.0 * sigma1 + sigma)
        sin_sigma, cos_sigma = math.sin(sigma), math.cos(sigma)
        d_sigma = B * sin_sigma * (
            cos2_sigma_m + B / 4.0 * (
                cos_sigma * (-1.0 + 2.0 * cos2_sigma_m ** 2)
                - B / 6.0 * cos2_sigma_m * (-3.0 + 4.0 * sin_sigma ** 2)
                * (-3.0 + 4.0 * cos2_sigma_m ** 2)))
        sigma_prev = sigma
        sigma = distance / (b * A) + d_sigma
        if abs(sigma - sigma_prev) < _CONVERGENCE:
            break

    tmp = sinU1 * sin_sigma - cosU1 * cos_sigma * cos_a1
    lat2 = math.atan2(
        sinU1 * cos_sigma + cosU1 * sin_sigma * cos_a1,
        (1 - f) * math.hypot(sin_alpha, tmp))
    lam = math.atan2(sin_sigma * sin_a1,
                     cosU1 * cos_sigma - sinU1 * sin_sigma * cos_a1)
    C = f / 16.0 * cos_sq_alpha * (4.0 + f * (4.0 - 3.0 * cos_sq_alpha))
    L = lam - (1.0 - C) * f * sin_alpha * (
        sigma + C * sin_sigma * (
            cos2_sigma_m + C * cos_sigma * (-1.0 + 2.0 * cos2_sigma_m ** 2)))
    az2 = math.atan2(sin_alpha, -tmp)
    return (math.degrees(lat2), lon1 + math.degrees(L),
            math.degrees(az2) % 360.0)


# =========================================================================
#  6.  EllipsoidDistance          7.1.5
#      "an optimized version of InverseGeodesic, which only delivers
#       distance, but with significantly less computational effort"
# =========================================================================
#
# CHECK THIS, CAREFULLY. The Code says "optimized ... significantly less
# computational effort", which means it specifies a CHEAPER APPROXIMATION —
# most likely Andoyer-Lambert, or a fixed small number of Vincenty iterations.
# Those agree with the full inverse to a few metres over hundreds of km, but
# they are not identical, and this is the function that measures every scored
# distance.
#
# What is below is the FULL inverse with the bearings discarded. That is
# correct but not "optimized", so it is faithful to the RESULT and not to the
# METHOD. If the Code names a specific approximation, replace this body — and
# expect the scored distances to move slightly when you do.


def EllipsoidDistance(lat1: float, lon1: float,
                      lat2: float, lon2: float) -> float:
    """S7F 7.1 (6) — geodesic distance in metres. See the CHECK THIS above."""
    return InverseGeodesic(lat1, lon1, lat2, lon2).distance


# =========================================================================
#  7.  FindTaskAreaCentre         7.1.6
#      "to find the centre of a task's area, as required by WGS84ToCartesian
#       and CartesianToWGS84"
# =========================================================================
#
# The projection anchor. An azimuthal projection is exact at its anchor and
# degrades with distance from it, so where the anchor goes decides how much
# distortion ProjectionCorrection then has to undo.
#
# CHECK THIS: which centre. The obvious candidates give different answers:
#   (a) arithmetic mean of the turnpoint lat/lon        <- implemented below,
#                                                          and what the running
#                                                          engine already uses
#   (b) centre of the bounding box of the turnpoints
#   (c) centroid weighted by cylinder radius
#   (d) centre of the smallest enclosing circle
# On a compact task these differ by a few km and the effect is small; on a
# task with one distant turnpoint they diverge, and so does the distortion.


def FindTaskAreaCentre(points) -> tuple[float, float]:
    """S7F 7.1 (7) — the projection anchor for a task. `points` is [(lat, lon)].

    Arithmetic mean of the turnpoint coordinates. See the CHECK THIS above.
    """
    if not points:
        raise ValueError("FindTaskAreaCentre: no points")
    return (sum(p[0] for p in points) / len(points),
            sum(p[1] for p in points) / len(points))


# =========================================================================
#  1 & 3.  GeodesicToCartesian and CartesianToGeodesic        7.1.1
#          "to calculate the Cartesian coordinates of a point on the WGS84
#           ellipsoid" / "to calculate the WGS84 coordinates from the
#           Cartesian coordinates of a point"
# =========================================================================
#
# A LOCAL projection, anchored at FindTaskAreaCentre — that dependency is
# stated in the Code's own description of (7), and it is what rules out the
# other reading of "Cartesian", namely global ECEF X/Y/Z. A global frame would
# need no task centre.
#
# So this is an azimuthal equidistant projection built directly on the
# geodesics: take distance and bearing from the centre with InverseGeodesic,
# and lay them down as (x, y). Distance from the CENTRE is then exact by
# construction, which is the property that makes cylinders centred near the
# task centre come out round, and it is also exactly why ProjectionCorrection
# is needed for the ones that are not.
#
#     x = distance * sin(azimuth)      east
#     y = distance * cos(azimuth)      north
#
# CHECK THIS: the sign and axis convention (x=east/y=north here), and whether
# the Code's projection is azimuthal equidistant at all rather than, say, a
# local tangent plane or a transverse Mercator. The correction step in (8)
# strongly implies a projection with real distortion away from the anchor,
# which fits azimuthal equidistant.


def GeodesicToCartesian(lat: float, lon: float,
                        centre: tuple[float, float]) -> tuple[float, float]:
    """S7F 7.1 (1) — WGS84 lat/lon -> local Cartesian (x east, y north), metres."""
    g = InverseGeodesic(centre[0], centre[1], lat, lon)
    a = math.radians(g.azimuth1)
    return (g.distance * math.sin(a), g.distance * math.cos(a))


def CartesianToGeodesic(x: float, y: float,
                        centre: tuple[float, float]) -> tuple[float, float]:
    """S7F 7.1 (3) — local Cartesian -> WGS84 lat/lon. Inverse of (1)."""
    d = math.hypot(x, y)
    if d == 0.0:
        return centre
    az = math.degrees(math.atan2(x, y)) % 360.0
    lat, lon, _ = DirectGeodesic(centre[0], centre[1], az, d)
    return (lat, lon)


# =========================================================================
#  2.  PathFinder                 7.1.3
#      "to calculate the path points that define the shortest path from
#       point A to point B via a set of control zones, in Cartesian geometry"
# =========================================================================
#
# Purely Cartesian, per the Code's own wording — no ellipsoid here. That is
# already implemented, and verified two independent ways, in
# engine/rules/route.py: multi-start coordinate descent with an exact
# per-point 1-D minimiser, seeded by a discretised shortest-path DP.
#
# It is imported rather than duplicated, so there is one route optimiser in
# this codebase and not two. Read route.py for the algorithm and for the two
# separate bugs it has already had.
#
# CHECK THIS: the Code may specify a particular method. What matters for
# agreement is the RESULT — the shortest path is the shortest path — and
# route.py's is verified globally optimal by an independent DP. A different
# method that finds the same optimum is fine; one that finds a different
# answer means one of the two is not optimal.


def PathFinder(zones, first: int = 0):
    """S7F 7.1 (2) — shortest path via control zones, in Cartesian geometry.

    `zones` is [(x, y, radius)] in metres; `first` is the index the path starts
    at. Returns (px, py): one path point per zone.
    """
    from .route import optimise_route
    return optimise_route(zones, first)


# =========================================================================
#  8.  ProjectionCorrection       7.1.7
#      "to ensure that the path points in WGS84 lie on their corresponding
#       control zone boundaries, regardless of any projection distortion that
#       may occur"
# =========================================================================
#
# THE STEP THE RUNNING ENGINE DOES NOT HAVE, and the reason the Code bothers
# with a separate ellipsoid distance at all.
#
# PathFinder puts each path point exactly on its cylinder IN THE PROJECTED
# PLANE. Project that point back to WGS84 and it is no longer exactly `radius`
# metres from the zone centre on the ellipsoid, because the projection distorts
# away from its anchor. Measuring the path in that state measures a route
# through points that are not on the cylinders.
#
# The correction: for each path point, take the true geodesic distance and
# bearing from its zone centre with InverseGeodesic, then move the point along
# that same bearing to exactly the zone radius with DirectGeodesic. Repeat
# until it stops moving — one pass is very nearly enough, since the correction
# is small and the bearing barely changes.
#
# Points that are meant to be INSIDE their cylinder rather than on the
# boundary — which happens when the direct line already passes through a large
# zone, so the detour is zero — must be left alone. Snapping those to the rim
# would invent distance. They are identified by being strictly inside in the
# projected plane.
#
# CHECK THIS: whether the Code corrects only boundary points or all of them,
# and whether it iterates. Also whether "corresponding control zone boundary"
# means the nominal radius or the S7F 9.1.1 tolerance radius — this uses the
# nominal, matching how the route is optimised.


def ProjectionCorrection(path_ll, zones_ll, radii, on_boundary,
                         iterations: int = 3, tol: float = 1e-4):
    """S7F 7.1 (8) — snap path points onto their real WGS84 zone boundaries.

    path_ll      [(lat, lon)]  one path point per zone, from CartesianToGeodesic
    zones_ll     [(lat, lon)]  the zone centres
    radii        [metres]      the zone radii
    on_boundary  [bool]        False for a point that is legitimately inside
                               its zone and must not be moved

    Returns the corrected [(lat, lon)].
    """
    out = list(path_ll)
    for _ in range(iterations):
        moved = 0.0
        for i, (p, c, r, snap) in enumerate(zip(out, zones_ll, radii, on_boundary)):
            if not snap or r <= 0.0:
                continue
            g = InverseGeodesic(c[0], c[1], p[0], p[1])
            err = g.distance - r
            if abs(err) < tol:
                continue
            lat, lon, _ = DirectGeodesic(c[0], c[1], g.azimuth1, r)
            out[i] = (lat, lon)
            moved = max(moved, abs(err))
        if moved < tol:
            break
    return out


# =========================================================================
#  9.  RouteOptimizer             7.1.8
#      "to calculate the path points that define the shortest path from point
#       A to point B via a set of control zones, on the WGS84 ellipsoid, as
#       well as the distance from A to B along this path"
# =========================================================================
#
# The driver, and the only one of the nine a caller needs. It is what makes the
# other eight a pipeline rather than a toolbox:
#
#     7  FindTaskAreaCentre     pick the anchor
#     1  GeodesicToCartesian    project every zone centre
#     2  PathFinder             optimise in the plane
#     3  CartesianToGeodesic    project the path points back
#     8  ProjectionCorrection   put them on their real boundaries
#     6  EllipsoidDistance      measure the corrected path, leg by leg
#
# Note the order: OPTIMISE in the plane, MEASURE on the ellipsoid. The engine
# currently does both in the plane, which is where its 0.42% shortfall against
# the published result comes from.
#
# CHECK THIS: whether the Code iterates the whole pipeline — correcting the
# points changes the path slightly, so in principle PathFinder could be re-run
# on the corrected geometry. `iterations` below allows it; 1 is the literal
# reading of the list order.


@dataclass(frozen=True, slots=True)
class OptimisedRoute:
    """What RouteOptimizer returns."""

    points: list          # [(lat, lon)] one path point per control zone
    legs: list            # [metres] geodesic length of each leg i -> i+1
    distance: float       # total metres from the first zone to the last
    centre: tuple         # the task area centre that was used


def RouteOptimizer(zones, first: int = 0, iterations: int = 1) -> OptimisedRoute:
    """S7F 7.1 (9) — shortest path via control zones, ON THE WGS84 ELLIPSOID.

    `zones` is [(lat, lon, radius_m)] in task order. `first` is the index the
    scored route starts at.
    """
    centre = FindTaskAreaCentre([(z[0], z[1]) for z in zones])

    xy = [GeodesicToCartesian(z[0], z[1], centre) for z in zones]
    radii = [z[2] for z in zones]

    # The ZONE CENTRES never move. What a further pass may adjust is the
    # EFFECTIVE RADIUS each zone gets in the plane: after correction, a point
    # that is exactly `radius` metres from the centre on the ellipsoid sits at
    # some slightly different distance in the projection, and using that as the
    # plane radius makes the plane geometry represent the ellipsoid locally.
    #
    # An earlier version of this loop fed the CORRECTED PATH POINTS back in as
    # the zone centres, which walked every zone outward and grew the route by
    # 1 km per pass. Kept as a comment because it looked entirely plausible.
    eff = list(radii)
    path_ll = None
    for _ in range(max(1, iterations)):
        cart = [(xy[i][0], xy[i][1], eff[i]) for i in range(len(zones))]
        px, py = PathFinder(cart, first)

        # A point strictly inside its zone in the plane is there because the
        # detour is zero; it is not a boundary point and must not be snapped.
        on_boundary = [
            eff[i] > 0.0
            and math.hypot(px[i] - xy[i][0], py[i] - xy[i][1]) >= eff[i] - 1e-6
            for i in range(len(zones))
        ]

        path_ll = [CartesianToGeodesic(px[i], py[i], centre)
                   for i in range(len(zones))]
        path_ll = ProjectionCorrection(
            path_ll, [(z[0], z[1]) for z in zones], radii, on_boundary)

        # Effective plane radius implied by the corrected point.
        for i in range(len(zones)):
            if not on_boundary[i]:
                continue
            cxp, cyp = GeodesicToCartesian(path_ll[i][0], path_ll[i][1], centre)
            eff[i] = math.hypot(cxp - xy[i][0], cyp - xy[i][1])

    legs = [0.0] * len(zones)
    for i in range(first, len(zones) - 1):
        legs[i] = EllipsoidDistance(path_ll[i][0], path_ll[i][1],
                                    path_ll[i + 1][0], path_ll[i + 1][1])
    return OptimisedRoute(points=path_ll, legs=legs,
                          distance=sum(legs), centre=centre)


# =========================================================================
#  Self-check
# =========================================================================
