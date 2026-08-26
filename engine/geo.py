"""Geometry — an index, not an implementation.

The algorithms moved to engine/rules/, one file per concern, so each can be
checked against the Sporting Code on its own:

    engine/rules/earth_model.py   earth model, haversine, projection, planar
                                  distance
    engine/rules/cylinder.py      S7F 9.1.1 tolerance zones, S7F 9.2.1 crossing
                                  detection and crossing time, inside tests,
                                  and the interpolating variant that scoring
                                  must NOT use
    engine/rules/route.py         shortest route through the cylinders
    engine/rules/distance_flown.py  distance still to fly, distance flown

`./run.py --rules` lists them with status. This module re-exports under the old
names so existing callers keep working; new code should import from
engine.rules.
"""

from __future__ import annotations

from .rules.cylinder import (ABSOLUTE_TOLERANCE, RADIUS_TOLERANCE,  # noqa: F401
                             Crossing, in_zone, inner_radius,
                             line_circle_roots, outer_radius, validates_zone,
                             zone_crossing)
from .rules.cylinder import first_contact as touches_cylinder      # noqa: F401
from .rules.earth_model import EARTH_R, Projection, dist, haversine  # noqa: F401

_roots = line_circle_roots          # kept: the old private name

__all__ = [
    "EARTH_R", "Projection", "haversine", "dist",
    "inner_radius", "outer_radius", "in_zone", "validates_zone",
    "zone_crossing", "touches_cylinder", "Crossing", "line_circle_roots",
]
