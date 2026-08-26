"""Cylinder geometry that is NOT part of S7F Section 9.

Section 9 itself — the tolerance zones (9.1.1), crossing detection and crossing
time (9.2.1), scored distance (9.3) and best time (9.4.1) — lives in one file,
engine/rules/s7f_09_control_zones.py, so it can be checked against the Code in
one sitting. Those names are re-exported here so existing callers keep working.

What is left in this file is the line/circle algebra underneath, plus the
interpolating crossing variant that scoring must never use.
"""

from __future__ import annotations

import math
from dataclasses import dataclass

from .s7f_09_control_zones import (ABSOLUTE_TOLERANCE,  # noqa: F401
                                   RADIUS_TOLERANCE, in_zone, inner_radius,
                                   measurement_radius, outer_radius,
                                   validates_zone, zone_crossing)

# --- the interpolating variant, which scoring must NOT use ----------------


@dataclass(frozen=True, slots=True)
class Crossing:
    """When a segment met a cylinder — with an INTERPOLATED time."""

    time: float       # fractional epoch seconds
    x: float
    y: float
    fraction: float   # 0..1 along the segment


def line_circle_roots(x0, y0, x1, y1, cx, cy, r):
    """Parametric roots of |p0 + s*d - c|^2 = r^2, solved analytically.

    Returns (s_lo, s_hi) unclipped, or None if the infinite line misses.
    """
    dx = x1 - x0
    dy = y1 - y0
    fx = x0 - cx
    fy = y0 - cy
    a = dx * dx + dy * dy
    if a < 1e-12:                       # zero-length segment (duplicate fix)
        return None
    b = 2.0 * (fx * dx + fy * dy)
    c = fx * fx + fy * fy - r * r
    disc = b * b - 4 * a * c
    if disc < 0.0:
        return None
    sq = math.sqrt(disc)
    return ((-b - sq) / (2 * a), (-b + sq) / (2 * a))


def first_contact(p0, p1, cx, cy, r) -> Crossing | None:
    """First contact of the segment with the disc, at an INTERPOLATED time.

    *** NOT USED BY SCORING, DELIBERATELY, AND ALSO A KNOWN GAP. ***

    Two separate things are true about this function.

    It INTERPOLATES, so it must never decide an official time: S7F 9.2.1 says
    the crossing time is the tracklog point's timestamp. It would make a better
    LIVE estimate and a wrong OFFICIAL result.

    But it also catches a case `zone_crossing()` genuinely misses: a segment
    that passes entirely THROUGH a small cylinder between two fixes, with both
    endpoints outside. A 400 m goal cylinder at 25 m/s is traversed in 32 s, so
    at 1 Hz this cannot happen — but with degraded live telemetry at 0.1 Hz it
    can, and then the pilot validated a turnpoint the engine did not see.

    That gap is open on purpose: closing it changes scored results, so it
    should be a decided change and not a side effect. VERIFICATION.md §7 lists
    it. The fix, when made, is to consult this function's GEOMETRY while still
    taking p1's timestamp as the time.
    """
    x0, y0, t0 = p0
    x1, y1, t1 = p1
    rr = line_circle_roots(x0, y0, x1, y1, cx, cy, r)
    if rr is None:
        return None
    s_lo, s_hi = rr
    if s_hi < 0.0 or s_lo > 1.0:        # disc lies off the segment
        return None
    s = max(s_lo, 0.0)                  # already inside at p0 -> contact at p0
    return Crossing(
        time=t0 + s * (t1 - t0),
        x=x0 + s * (x1 - x0),
        y=y0 + s * (y1 - y0),
        fraction=s,
    )
