"""Earth model and distance.  [algorithm A]

Everything downstream — cylinder radii, route optimisation, scored distance —
is measured with these three functions, so an error here is invisible and
uniform, which is the worst kind.

    EARTH_R = 6 371 000 m      the FAI sphere

WHICH EARTH MODEL IS NOT SETTLED. This engine uses the FAI sphere throughout.
A .xctsk may declare `"earthModel": "WGS84"` instead, and the reference task
file declares nothing at all. The difference is not academic: over the
reference task's waypoints, WGS84 geodesics total 0.18% MORE than sphere
distances — 157 m on 87 km — which is about half of the 0.42% by which this
engine's optimised route falls short of the published result
(VERIFICATION.md §5.4). If the Code specifies an earth model, this is the file
to change, and `haversine()` is the only thing that needs replacing.

THREE FUNCTIONS, THREE JOBS

  haversine()   great-circle distance between two lat/lon on the FAI sphere.
                Used at task-compile time and as the reference the projection
                is validated against. Never in the hot path.

  Projection    a LOCAL AZIMUTHAL EQUIDISTANT projection anchored at the task
                centre. Distance from the anchor is exact by construction, and
                distance between any two nearby points is accurate to a couple
                of centimetres across a competition-sized task. Every position
                is projected ONCE, on arrival; the engine never re-projects
                during a recompute, which is what makes recompute cheap.

  dist()        plain 2-D hypot in projected metres. This is what actually
                measures every scored distance.

WHY NOT EQUIRECTANGULAR, THE OBVIOUS CHOICE. Measured over the reference task's
27 km envelope, azimuthal equidistant is 0.02 m wrong at worst and plain
equirectangular is 54 m wrong. 54 m is ten times the FAI tolerance on a 400 m
goal cylinder, so it is not usable. Cheap to get wrong, cheap to get right.

VERIFIED: --verify asserts the projection agrees with haversine to better than
0.5 m across every pair of task waypoints (worst 0.004 m on the reference
task), against a ±5 m tolerance. That validates the projection AGAINST the
sphere; it says nothing about whether the sphere is the right model.
"""

from __future__ import annotations

import math

EARTH_R = 6371000.0          # FAI sphere
WGS84_A = 6378137.0
WGS84_F = 1 / 298.257223563


def haversine(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    """Great-circle distance in metres on the FAI sphere."""
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dp = p2 - p1
    dl = math.radians(lon2 - lon1)
    a = math.sin(dp / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return 2 * EARTH_R * math.asin(math.sqrt(a))


def dist(ax: float, ay: float, bx: float, by: float) -> float:
    """Planar distance between two projected points, in metres."""
    dx = bx - ax
    dy = by - ay
    return math.sqrt(dx * dx + dy * dy)


class Projection:
    """Local azimuthal equidistant projection, anchored at the task centre."""

    __slots__ = ("lat0", "lon0", "earth_model", "_p0", "_l0", "_sin0", "_cos0",
                 "_wgs_origin", "_wgs_east", "_wgs_north")

    def __init__(self, lat0: float, lon0: float, earth_model: str = "FAI_SPHERE") -> None:
        self.lat0 = lat0
        self.lon0 = lon0
        self.earth_model = earth_model.upper()
        if self.earth_model not in {"FAI_SPHERE", "WGS84"}:
            raise ValueError(f"unsupported earth model: {earth_model!r}")
        self._p0 = math.radians(lat0)
        self._l0 = math.radians(lon0)
        self._sin0 = math.sin(self._p0)
        self._cos0 = math.cos(self._p0)
        self._wgs_origin = self._wgs_ecef(self._p0, self._l0)
        self._wgs_east = (-math.sin(self._l0), math.cos(self._l0), 0.0)
        self._wgs_north = (-self._sin0 * math.cos(self._l0),
                           -self._sin0 * math.sin(self._l0), self._cos0)

    @staticmethod
    def _wgs_ecef(lat: float, lon: float) -> tuple[float, float, float]:
        e2 = WGS84_F * (2.0 - WGS84_F)
        sin_lat = math.sin(lat)
        n = WGS84_A / math.sqrt(1.0 - e2 * sin_lat * sin_lat)
        return (n * math.cos(lat) * math.cos(lon),
                n * math.cos(lat) * math.sin(lon),
                n * (1.0 - e2) * sin_lat)

    def _wgs_inverse(self, lat: float, lon: float) -> tuple[float, float]:
        """Vincenty's inverse solution: (geodesic metres, initial bearing)."""
        a, f = WGS84_A, WGS84_F
        b = a * (1.0 - f)
        u1 = math.atan((1.0 - f) * math.tan(self._p0))
        u2 = math.atan((1.0 - f) * math.tan(lat))
        sin_u1, cos_u1 = math.sin(u1), math.cos(u1)
        sin_u2, cos_u2 = math.sin(u2), math.cos(u2)
        lam = lon - self._l0
        for _ in range(100):
            sin_lam, cos_lam = math.sin(lam), math.cos(lam)
            sin_sigma = math.hypot(cos_u2 * sin_lam,
                                   cos_u1 * sin_u2 - sin_u1 * cos_u2 * cos_lam)
            if sin_sigma < 1e-15:
                return 0.0, 0.0
            cos_sigma = sin_u1 * sin_u2 + cos_u1 * cos_u2 * cos_lam
            sigma = math.atan2(sin_sigma, cos_sigma)
            sin_alpha = cos_u1 * cos_u2 * sin_lam / sin_sigma
            cos2_alpha = 1.0 - sin_alpha * sin_alpha
            cos2_sigma_m = (cos_sigma - 2.0 * sin_u1 * sin_u2 / cos2_alpha
                            if cos2_alpha > 1e-15 else 0.0)
            c = f / 16.0 * cos2_alpha * (4.0 + f * (4.0 - 3.0 * cos2_alpha))
            nxt = (lon - self._l0) + (1.0 - c) * f * sin_alpha * (
                sigma + c * sin_sigma * (cos2_sigma_m + c * cos_sigma *
                (-1.0 + 2.0 * cos2_sigma_m * cos2_sigma_m)))
            if abs(nxt - lam) < 1e-12:
                lam = nxt
                break
            lam = nxt
        u2sq = cos2_alpha * (a * a - b * b) / (b * b)
        aa = 1.0 + u2sq / 16384.0 * (4096.0 + u2sq * (-768.0 + u2sq * (320.0 - 175.0 * u2sq)))
        bb = u2sq / 1024.0 * (256.0 + u2sq * (-128.0 + u2sq * (74.0 - 47.0 * u2sq)))
        delta = bb * sin_sigma * (cos2_sigma_m + bb / 4.0 * (
            cos_sigma * (-1.0 + 2.0 * cos2_sigma_m * cos2_sigma_m) - bb / 6.0 *
            cos2_sigma_m * (-3.0 + 4.0 * sin_sigma * sin_sigma) *
            (-3.0 + 4.0 * cos2_sigma_m * cos2_sigma_m)))
        distance = b * aa * (sigma - delta)
        bearing = math.atan2(cos_u2 * math.sin(lam),
                             cos_u1 * sin_u2 - sin_u1 * cos_u2 * math.cos(lam))
        return distance, bearing

    def xy(self, lat: float, lon: float) -> tuple[float, float]:
        p = math.radians(lat)
        dl = math.radians(lon) - self._l0
        if self.earth_model == "WGS84":
            d, bearing = self._wgs_inverse(p, math.radians(lon))
            return d * math.sin(bearing), d * math.cos(bearing)
        sin_p = math.sin(p)
        cos_p = math.cos(p)
        cos_dl = math.cos(dl)
        cos_c = self._sin0 * sin_p + self._cos0 * cos_p * cos_dl
        if cos_c > 1.0:
            cos_c = 1.0
        elif cos_c < -1.0:
            cos_c = -1.0
        c = math.acos(cos_c)
        k = 1.0 if c < 1e-12 else c / math.sin(c)
        return (
            EARTH_R * k * cos_p * math.sin(dl),
            EARTH_R * k * (self._cos0 * sin_p - self._sin0 * cos_p * cos_dl),
        )

    def latlon(self, x: float, y: float) -> tuple[float, float]:
        if self.earth_model == "WGS84":
            ox, oy, oz = self._wgs_origin
            ex, ey, ez = self._wgs_east
            nx, ny, nz = self._wgs_north
            px, py, pz = ox + x * ex + y * nx, oy + x * ey + y * ny, oz + x * ez + y * nz
            lon = math.atan2(py, px)
            p = math.hypot(px, py)
            e2 = WGS84_F * (2.0 - WGS84_F)
            lat = math.atan2(pz, p * (1.0 - e2))
            for _ in range(8):
                n = WGS84_A / math.sqrt(1.0 - e2 * math.sin(lat) ** 2)
                lat = math.atan2(pz + e2 * n * math.sin(lat), p)
            return (math.degrees(lat), math.degrees(lon))
        c = math.sqrt(x * x + y * y) / EARTH_R
        if c < 1e-12:
            return (self.lat0, self.lon0)
        sin_c = math.sin(c)
        cos_c = math.cos(c)
        lat = math.asin(cos_c * self._sin0 + y * sin_c * self._cos0 / (c * EARTH_R))
        lon = self._l0 + math.atan2(
            x * sin_c, c * EARTH_R * self._cos0 * cos_c - y * self._sin0 * sin_c
        )
        return (math.degrees(lat), math.degrees(lon))
