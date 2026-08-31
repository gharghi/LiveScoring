"""Task parsing and compilation.

Everything expensive happens once, here. The per-position hot path is left
with two square roots and a subtraction (see score.py).

The route optimiser is the highest-risk component in the whole system. On the
supplied Task 06, centre-to-centre distance is 91.42 km while the optimised
route is 46.36 km -- a 97% error -- because of the 17 km G23 cylinder. Every
distance-based scoring quantity depends on getting this right.
"""

from __future__ import annotations

import hashlib
import json
import math
from dataclasses import dataclass, field

from .geo import Projection, dist, haversine
from .rules.cylinder import ABSOLUTE_TOLERANCE, RADIUS_TOLERANCE  # noqa: F401
from .rules.route import leg_lengths, optimise_route, remaining_table
from .rules.s7f_09_control_zones import MEASUREMENT_RADIUS

TAKEOFF, SSS, TURNPOINT, ESS, GOAL = "TAKEOFF", "SSS", "TURNPOINT", "ESS", "GOAL"


@dataclass(frozen=True, slots=True)
class Waypoint:
    index: int
    name: str
    kind: str
    lat: float
    lon: float
    radius: float          # nominal radius, metres
    raw_radius: float
    # Three radii, three jobs, and conflating them is a scoring error:
    #   raw_radius / radius   what the task file declares
    #   inner / outer         S7F 9.1.1 tolerance zone -- VALIDATION only
    #   measure               S7F 9.3 -- what DISTANCE is measured to,
    #                         chosen once by rules.s7f_09_control_zones
    alt: float = 0.0       # published waypoint altitude, m AMSL
    inner: float = 0.0     # FAI tolerance zone, S7F 9.1.1
    outer: float = 0.0
    measure: float = 0.0   # S7F 9.3 measurement radius
    x: float = 0.0
    y: float = 0.0


@dataclass(slots=True)
class CompiledTask:
    name: str
    task_hash: str
    earth_model: str
    waypoints: list[Waypoint]
    proj: Projection

    start_index: int
    ess_index: int
    goal_index: int

    start_type: str            # RACE | ELAPSED-TIME
    start_direction: str       # EXIT | ENTER
    gates: list[int]           # epoch seconds
    goal_deadline: int | None  # epoch seconds
    goal_type: str
    goal_elevated: bool = False    # S7F 13.1
    goal_elevation: float = 300.0  # m, default 300, max 1000
    radius_tolerance: float = RADIUS_TOLERANCE
    absolute_tolerance: float = ABSOLUTE_TOLERANCE
    measurement_radius_policy: str = MEASUREMENT_RADIUS
    progress_curve: str = "WEIGHTED"

    # Where the SCORED route begins. Waypoint 0 -- the takeoff -- unless the
    # task has no point before the SSS, in which case it is the SSS. Distinct
    # from start_index, which is where the CLOCK begins (S7F 8.1).
    route_start: int = 0

    opt_x: list[float] = field(default_factory=list)   # optimised point per wp
    opt_y: list[float] = field(default_factory=list)
    remaining: list[float] = field(default_factory=list)  # opt distance wp -> goal
    total_distance: float = 0.0        # SSS -> goal, optimised
    launch_to_sss: float = 0.0         # [PG] early-start score, S7F 13.3
    speed_distance: float = 0.0        # SSS -> ESS, optimised
    centre_distance: float = 0.0       # SSS -> goal, centre to centre
    warnings: list = field(default_factory=list)

    @property
    def first_gate(self) -> int:
        return self.gates[0]


# S7F 9.1.1 tolerance zones live in engine/rules/cylinder.py, re-exported here
# because parse_xctsk() below is where every waypoint gets its zone.


def _kind(tp: dict, i: int, n: int, has_takeoff: bool, sss_i: int | None) -> str:
    """Classify a turnpoint.

    Types in a .xctsk are optional and frequently absent. The conventions are
    fixed, so infer them rather than mis-scoring a task that simply omits a
    label: the FIRST point is the takeoff when it precedes the SSS, and the
    LAST point is always goal.
    """
    t = tp.get("type")
    if t == "TAKEOFF":
        return TAKEOFF
    if t == "SSS":
        return SSS
    if t == "ESS":
        return ESS
    if i == n - 1:
        return GOAL
    if i == 0 and not has_takeoff and (sss_i is None or sss_i > 0):
        return TAKEOFF
    return TURNPOINT


def parse_xctsk(path: str, date_epoch: int, gate_override: int | None = None,
                deadline_override: int | None = None,
                direction_override: str | None = None,
                elevated_goal: float | None = None) -> CompiledTask:
    """Parse an XCTrack .xctsk file. date_epoch is midnight UTC of the task day.

    gate_override / deadline_override are seconds-since-midnight UTC, for task
    files whose declared times are wrong (see _sanity).
    """
    raw = open(path, "rb").read()
    doc = json.loads(raw)
    tps = doc["turnpoints"]
    n = len(tps)

    def _snake(name: str) -> str:
        return "".join("_" + c.lower() if c.isupper() else c for c in name).lstrip("_")

    def _fraction(name: str, default: float) -> float:
        raw_value = doc.get(name, doc.get(_snake(name), default))
        value = float(raw_value)
        # Accept both the S7F fraction form (0.001) and a UI percentage form
        # (0.1). Values above 1% are almost certainly percent notation.
        return value / 100.0 if abs(value) > 0.01 else value

    radius_tolerance = _fraction("radiusTolerance", RADIUS_TOLERANCE)
    absolute_tolerance = float(doc.get(
        "absoluteTolerance", doc.get("absolute_tolerance", ABSOLUTE_TOLERANCE)))
    measurement_policy = str(doc.get(
        "measurementRadius", doc.get("measurement_radius", MEASUREMENT_RADIUS)
    )).lower()
    progress_curve = str(doc.get(
        "progressCurve", doc.get("progress_curve", "WEIGHTED")
    )).upper()

    def _inner_radius(radius: float) -> float:
        return min(radius * (1.0 - radius_tolerance),
                   radius - absolute_tolerance)

    def _outer_radius(radius: float) -> float:
        return max(radius * (1.0 + radius_tolerance),
                   radius + absolute_tolerance)

    def _measurement_radius(radius: float) -> float:
        if measurement_policy == "outer":
            return _outer_radius(radius)
        if measurement_policy == "inner":
            return _inner_radius(radius)
        if measurement_policy != "nominal":
            raise ValueError(
                "measurementRadius must be 'nominal', 'outer' or 'inner', "
                f"got {measurement_policy!r}")
        return radius

    has_takeoff = any(tp.get("type") == "TAKEOFF" for tp in tps)
    sss_i = next((i for i, tp in enumerate(tps) if tp.get("type") == "SSS"), None)

    wps: list[Waypoint] = []
    for i, tp in enumerate(tps):
        w = tp["waypoint"]
        r = float(tp["radius"])
        wps.append(
            Waypoint(
                index=i,
                name=w["name"],
                kind=_kind(tp, i, n, has_takeoff, sss_i),
                alt=float(w.get("altSmoothed") or 0.0),
                lat=float(w["lat"]),
                lon=float(w["lon"]),
                radius=r,
                raw_radius=r,
                inner=_inner_radius(r),
                outer=_outer_radius(r),
                measure=_measurement_radius(r),
            )
        )

    lat0 = sum(w.lat for w in wps) / n
    lon0 = sum(w.lon for w in wps) / n
    earth_model = str(doc.get("earthModel", "FAI_SPHERE")).upper()
    proj = Projection(lat0, lon0, earth_model)
    wps = [
        Waypoint(w.index, w.name, w.kind, w.lat, w.lon, w.radius, w.raw_radius,
                 w.alt, w.inner, w.outer, w.measure, *proj.xy(w.lat, w.lon))
        for w in wps
    ]

    sss = doc.get("sss", {})

    def _gate(hhmmss: str) -> int:
        h, m, s = hhmmss.rstrip("Z").split(":")
        return date_epoch + int(h) * 3600 + int(m) * 60 + int(s)

    gates = sorted(_gate(g) for g in sss.get("timeGates", []))
    goal = doc.get("goal", {})
    deadline = _gate(goal["deadline"]) if goal.get("deadline") else None
    if gate_override is not None:
        gates = [date_epoch + gate_override]
    if deadline_override is not None:
        deadline = date_epoch + deadline_override

    start_i = next((w.index for w in wps if w.kind == SSS), 1)
    ess_i = next((w.index for w in wps if w.kind == ESS), n - 1)

    task = CompiledTask(
        name=path.rsplit("/", 1)[-1].removesuffix(".xctsk"),
        task_hash=hashlib.sha256(raw).hexdigest()[:16],
        earth_model=earth_model,
        waypoints=wps,
        proj=proj,
        start_index=start_i,
        ess_index=ess_i,
        goal_index=n - 1,
        start_type=sss.get("type", "RACE"),
        start_direction=(direction_override or sss.get("direction", "EXIT")).upper(),
        gates=gates or [date_epoch],
        goal_deadline=deadline,
        goal_type=goal.get("type", "CYLINDER"),
        goal_elevated=bool(elevated_goal) if elevated_goal is not None
        else bool(goal.get("elevated", False)),
        goal_elevation=min(1000.0, float(elevated_goal if elevated_goal else
                                         goal.get("elevation", 300.0) or 300.0)),
        radius_tolerance=radius_tolerance,
        absolute_tolerance=absolute_tolerance,
        measurement_radius_policy=measurement_policy,
        progress_curve=progress_curve,
        route_start=0,
    )
    optimise(task)
    task.warnings = _sanity(task)
    return task


def _sanity(task: "CompiledTask") -> list[str]:
    """Internal consistency of the task definition itself.

    Task files are exported by many different tools and are not always
    coherent. A gate after the goal deadline makes every start invalid, which
    presents as "nobody started" and looks exactly like an engine fault.
    """
    out = []
    if task.goal_deadline is not None and task.first_gate >= task.goal_deadline:
        out.append(
            f"start gate ({_hhmmss(task.first_gate)}Z) is at or after the goal "
            f"deadline ({_hhmmss(task.goal_deadline)}Z) — no start can be valid"
        )
    if task.start_index + 1 <= task.goal_index:
        sw = task.waypoints[task.start_index]
        nw = task.waypoints[task.start_index + 1]
        gap = math.hypot(nw.x - sw.x, nw.y - sw.y)
        if gap + nw.radius <= sw.radius:
            out.append(
                f"the control zone after the SSS ({nw.name} r{nw.raw_radius:.0f}m) lies "
                f"entirely inside the SSS ({sw.name} r{sw.raw_radius:.0f}m)"
            )
    return out


def _hhmmss(t: int) -> str:
    t = int(t) % 86400
    return f"{t//3600:02d}:{t%3600//60:02d}:{t%60:02d}"


def optimise(task: CompiledTask) -> None:
    """Fill the task's optimised route and every distance derived from it.

    The algorithm is in engine/rules/route.py, which works on plain
    (x, y, radius) tuples and knows nothing about tasks. This function is only
    the adapter: it hands the waypoints over, then records the three distances
    the rest of the engine asks for.

    THREE DISTANCES, THREE DIFFERENT SPANS, and conflating any two of them is a
    scoring error:

      total_distance   route_start -> goal.  The SCORED route. Starts at the
                       first turnpoint (normally the takeoff), because a pilot
                       who lands before the start has still flown the
                       launch-to-SSS leg and is scored for it. Measuring this
                       from the SSS made every distance in the reference
                       competition 5.9 km short (VERIFICATION.md §5.1).

      speed_distance   SSS -> ESS.  The span the CLOCK applies to, and the only
                       one used for speed and the leading coefficient.

      launch_to_sss    route_start -> SSS.  [PG] S7F 13.3 scores an early
                       starter on this alone.
    """
    wps = task.waypoints
    # S7F 9.1.1 / 9.3 — DISTANCE is measured to whichever radius
    # rules.s7f_09_control_zones.MEASUREMENT_RADIUS names. Validation always
    # uses the tolerance zone; these are separate decisions and used not to
    # agree. One place decides now.
    # The TAKEOFF is the point you launch from, not a cylinder you clip: the
    # route starts AT it. Giving it radius 0 here is what says so. Measuring to
    # its rim instead shortened the launch-to-SSS leg by the takeoff radius —
    # 400 m on the samples task, where the expected 4.04 km is the
    # centre-to-centre 5.028 km less the SSS radius alone.
    pts = [(w.x, w.y, 0.0 if w.kind == TAKEOFF else w.measure) for w in wps]
    first = task.route_start

    px, py = optimise_route(pts, first)
    task.opt_x, task.opt_y = px, py

    legs = leg_lengths(px, py, first)
    remaining = remaining_table(legs)
    task.remaining = remaining

    s = task.start_index
    task.total_distance = remaining[first]
    task.speed_distance = remaining[s] - remaining[task.ess_index]
    task.launch_to_sss = remaining[first] - remaining[s] if s > first else 0.0

    task.centre_distance = sum(
        haversine(wps[i].lat, wps[i].lon, wps[i + 1].lat, wps[i + 1].lon)
        for i in range(s, len(wps) - 1)
    )
