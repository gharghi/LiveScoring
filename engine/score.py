"""The scoring function — FAI Sporting Code Section 7F (2026 V1.0), paragliding.

There is exactly one of these, and both callers use it:

  * official scoring calls it once with the complete track;
  * live scoring calls it incrementally, and on any anomaly -- late data,
    backfill, a reinstated point -- calls it in full.

Keeping them the same function turns live/official divergence into a testable
assertion instead of something discovered during a protest.
"""

from __future__ import annotations

import math
import bisect
from dataclasses import dataclass, field

from .gap import GapParams
from .geo import in_zone, zone_crossing
from .rules import start_selection
from .rules.distance_flown import distance_to_goal as rules_distance_to_goal
from .rules.route import optimise_route, polish_route, route_length
from .igc import Fix
from .task import CompiledTask

WAITING, AIRBORNE, STARTED, ESS_DONE, GOAL_DONE = "WAITING", "AIRBORNE", "STARTED", "ESS", "GOAL"
NOT_STARTED = "NO START"   # launched, but never made a valid SSS crossing

TAKEOFF_RADIUS = 200.0     # m from first fix before we call it airborne

# S7F 12.1 scores distance only "up until the pilot landed or the task
# deadline was reached, whichever comes first." IGC/live trackers often keep
# recording after landing; without a cutoff the engine can score retrieve-car
# movement. This detector is deliberately conservative: it requires three
# minutes inside a small horizontal/vertical box after the pilot has actually
# started the task.
LANDING_WINDOW_S = 180
LANDING_RADIUS_M = 100.0
LANDING_ALTITUDE_RANGE_M = 80.0
LANDING_SCAN_STEP_S = 5
LANDING_AFTER_START_DELAY_S = 60
LANDING_DISTANCE_STABILIZE_S = 15

# Each candidate start costs a full replay of the pilot's track. A task whose
# SSS is re-crossed during the course can generate dozens; evaluating all of
# them turns a 4 ms pilot into a 200 ms one for no change in result.
MAX_START_CANDIDATES = start_selection.MAX_START_CANDIDATES

# The state-machine pass keeps a fast fixed-tail approximation for progress.
# That is good enough for live ordering, but it is not the official S7F 9.3
# landed-out distance when a remaining cylinder is large: the shortest remaining
# path must be re-optimised from the pilot's actual position. We run this exact
# pass after the timing decisions are known. Sampling keeps live scoring usable;
# every control-zone tag and the chosen distance fix are always included.
EXACT_PROGRESS_SAMPLE_S = 5
EXACT_ROUTE_INITIAL_ITERATIONS = 80
EXACT_ROUTE_POLISH_ITERATIONS = 12


@dataclass(slots=True)
class PilotResult:
    pilot: str = ""
    state: str = WAITING

    takeoff_time: float | None = None
    start_time: float | None = None          # effective clock (gate, for RACE)
    start_cross_time: float | None = None    # actual SSS zone crossing
    ess_time: float | None = None
    goal_time: float | None = None
    goal_alt: float | None = None      # altitude at the goal crossing, m AMSL

    tags: list[float | None] = field(default_factory=list)
    next_wp: int = 0
    tp_count: int = 0

    distance: float = 0.0          # S7F 9.3 scored distance, metres
    raw_distance: float = 0.0      # before the minimum-distance floor
    early_start: bool = False      # [PG] S7F 13.3
    goal_missed_deadline: bool = False

    # leading-coefficient input: (taskTime s, minToESS km) where minToESS fell
    lead_samples: list[tuple[float, float]] = field(default_factory=list)
    # ...reduced to the two numbers the field-wide pass actually needs, so a
    # worker process can drop the samples instead of shipping ~5,000 tuples per
    # pilot back to the parent (engine/parallel.py). Filled by score_task, or
    # by the worker before it discards the samples.
    lead_area: float | None = None
    lead_min_to_ess: float = 0.0
    last_task_time: float = 0.0
    max_time: float = 0.0      # S7F 12.3.1, per pilot
    lc: float = 0.0

    speed_section_time: float | None = None   # s, start -> ESS
    speed: float | None = None                # km/h

    # GAP points, filled by scoring.score_task()
    distance_points: float = 0.0
    time_points: float = 0.0
    leading_points: float = 0.0
    goal_altitude_factor: float = 1.0   # S7F 13.1
    total_points: float = 0.0

    # S7F 13.5. Decided by the meet director, not derived from the tracklog,
    # and applied last -- after the total above is summed and rounded. Kept as
    # the Penalty objects rather than just the number so --explain can quote
    # the reason, which is the part a pilot will contest.
    penalties: list = field(default_factory=list)
    penalty_points: float = 0.0

    last_t: int = 0
    last_lat: float = 0.0
    last_lon: float = 0.0
    last_alt: int = 0
    fixes_used: int = 0
    landing_time: float | None = None

    # Audit anchors. These are indices into the pilot's own point list, so a
    # protest can be answered by naming the fix that decided the number rather
    # than by repeating the number (DESIGN.md §17). Filling them costs one
    # integer assignment on a path that already runs, so they are always
    # present, not only when tracing.
    dist_fix_index: int = -1        # the fix that set the scored distance
    start_fix_index: int = -1       # the fix whose timestamp is the start
    landing_fix_index: int = -1     # first fix of the stationary landing window
    tag_fix_index: list[int] = field(default_factory=list)

    @property
    def launched(self) -> bool:
        return self.takeoff_time is not None

    @property
    def did_not_start(self) -> bool:
        """Launched but never made a valid start crossing.

        Distinct from 'has not started yet' only in the eye of the caller: the
        engine cannot know whether the gate is still open, so the display
        decides when this becomes DNS (see leaderboard._status).
        """
        return self.launched and self.start_time is None

    @property
    def rank_key(self) -> tuple:
        return (-self.total_points, -self.distance, self.pilot)

    @property
    def progress_key(self) -> tuple:
        """Ranking before points exist (live, mid-task)."""
        if self.goal_time is not None:
            return (0, self.goal_time, 0.0)
        if self.ess_time is not None:
            return (1, self.ess_time, 0.0)
        return (2, 0.0, -self.distance)


def project(task: CompiledTask, fixes: list[Fix]) -> None:
    """Fill task-local coordinates. Done once, on arrival, not per recompute."""
    xy = task.proj.xy
    for f in fixes:
        f.x, f.y = xy(f.lat, f.lon)


def remaining_to_goal(task: CompiledTask, f: Fix, next_wp: int) -> float:
    """Distance still to fly, via the next un-tagged control zone. [REFERENCE]

    The hot loop in _run_from_start() does NOT call this — it folds the same
    arithmetic inline and reuses the distance the crossing test already
    computed, which is most of what took the per-fix cost from 1.20 us to
    0.52 us. This is kept as the readable statement of the rule, and
    engine/invariants.check_distance_to_goal_reference asserts the two agree
    over every fix of the real field.

    The formula itself is documented in engine/rules/distance_flown.py.
    """
    if next_wp > task.goal_index:
        return 0.0
    w = task.waypoints[next_wp]
    return rules_distance_to_goal(f.x, f.y, w.x, w.y, w.measure,
                                  task.remaining[next_wp])


def _exact_remaining_route(task: CompiledTask, f: Fix, next_wp: int,
                           stop_wp: int,
                           cache: dict[tuple[int, int],
                                       tuple[list[float], list[float]]]) -> float:
    """Optimised remaining route from this fix through unreached zones.

    S7F 9.3 distance for a landed-out pilot is based on the shortest remaining
    route from the pilot's actual position, not on the task's pre-optimised
    waypoint tags. GlideComp documents the same rule in `optimizeRemainingRoute`:
    position + unreached control zones + goal are solved as their own route.

    `cache` keeps one polished route shape per waypoint span. For the next fix
    in the same span we reuse that shape, replace only the anchor with the new
    fix, and run a short polish instead of the full DP seed search.
    """
    if next_wp > stop_wp:
        return 0.0

    key = (next_wp, stop_wp)
    pts = [(f.x, f.y, 0.0)] + [
        (w.x, w.y, w.measure) for w in task.waypoints[next_wp:stop_wp + 1]
    ]
    cached = cache.get(key)
    if cached is None:
        px, py = optimise_route(
            pts, 0, iterations=EXACT_ROUTE_INITIAL_ITERATIONS)
    else:
        px, py = cached
        px = [f.x] + px[1:]
        py = [f.y] + py[1:]
        px, py = polish_route(
            pts, px, py, 0, iterations=EXACT_ROUTE_POLISH_ITERATIONS,
            eps=1e-3)
    cache[key] = (px, py)
    return route_length(px, py, 0)


def _apply_exact_progress(task: CompiledTask, fixes: list[Fix], now: float,
                          params: GapParams, r: PilotResult,
                          trace: dict | None = None) -> PilotResult:
    """Replace approximate progress with exact S7F 9.3 remaining-route progress.

    This intentionally does not select starts, validate sectors, or decide ESS /
    goal times. Those decisions stay in the tested replay state machine above.
    This pass only re-measures progress from already-valid state transitions.
    """
    if not fixes or r.start_fix_index < 0:
        return r

    if r.landing_time is not None:
        # Landing is inferred by looking FORWARD over a stationary window. The
        # first fix in that window is the safe cutoff for validating later task
        # sectors, but published scorers commonly use the stabilized landing
        # cluster for outlanding distance; using only the first GPS point can
        # lose tens of metres to touchdown jitter. Cap the distance scan at a
        # short stabilization interval, not at later retrieve-car movement.
        state_cutoff = min(float(now), float(r.landing_time))
        distance_cutoff = min(
            float(now), float(r.landing_time + LANDING_DISTANCE_STABILIZE_S))
    else:
        state_cutoff = distance_cutoff = min(float(now), float(r.last_t or now))
    if task.goal_deadline is not None:
        state_cutoff = min(state_cutoff, float(task.goal_deadline))
        distance_cutoff = min(distance_cutoff, float(task.goal_deadline))
    cutoff = max(state_cutoff, distance_cutoff)

    start = r.start_fix_index
    if start >= len(fixes):
        return r

    tag_fix = r.tag_fix_index or [-1] * len(task.waypoints)
    tag_fixes = {i for i in tag_fix if i >= start}
    if r.dist_fix_index >= start:
        tag_fixes.add(r.dist_fix_index)
    if r.landing_fix_index >= start:
        tag_fixes.add(r.landing_fix_index)

    distance_goal = r.goal_time is None
    lead_until = float(r.ess_time if r.ess_time is not None else state_cutoff)
    do_leading = lead_until >= fixes[start].t

    next_wp = task.start_index + 1
    min_remaining = 0.0 if r.goal_time is not None else float("inf")
    min_fix = r.dist_fix_index
    min_to_ess = task.speed_distance / 1000.0
    lead_samples: list[tuple[float, float]] = []
    cache: dict[tuple[int, int], tuple[list[float], list[float]]] = {}
    last_eval_t = -10**18

    for i in range(start, len(fixes)):
        f = fixes[i]
        ft = float(f.t)
        if ft > cutoff:
            break

        while (next_wp <= task.goal_index
               and next_wp < len(tag_fix)
               and tag_fix[next_wp] >= 0
               and tag_fix[next_wp] <= i):
            next_wp += 1

        mandatory = i in tag_fixes or i == start
        sampled = ft - last_eval_t >= EXACT_PROGRESS_SAMPLE_S
        if not mandatory and not sampled:
            continue
        last_eval_t = ft

        if distance_goal and ft <= distance_cutoff:
            rem_goal = _exact_remaining_route(
                task, f, next_wp, task.goal_index, cache)
            if rem_goal < min_remaining:
                min_remaining = rem_goal
                min_fix = i

        if do_leading and ft <= lead_until:
            if next_wp > task.ess_index:
                d_ess = 0.0
            else:
                d_ess = _exact_remaining_route(
                    task, f, next_wp, task.ess_index, cache) * 0.001
            if d_ess < min_to_ess:
                min_to_ess = d_ess
                lead_samples.append((ft - task.first_gate, d_ess))

    if distance_goal and min_remaining < float("inf"):
        raw = max(0.0, task.total_distance - min_remaining)
        r.raw_distance = raw
        r.distance = max(params.minimum_distance, raw)
        r.dist_fix_index = min_fix
        if trace is not None:
            trace["min_remaining"] = min_remaining
            trace["exact_progress"] = {
                "sample_s": EXACT_PROGRESS_SAMPLE_S,
                "fix": min_fix,
                "remaining": min_remaining,
                "formula": "taskDistance - optimizeRemainingRoute(position, unreached zones, goal)",
            }

    r.lead_samples = lead_samples
    r.lead_area = None
    r.lead_min_to_ess = 0.0
    return r


def _segment_reaches_radius(ax: float, ay: float, bx: float, by: float,
                            cx: float, cy: float, radius: float) -> bool:
    """Whether segment AB intersects or touches the circle/disc around C.

    The common endpoint-inside cases are handled by callers that already have
    d0/d1 for other work. This catches the remaining telemetry-gap case: both
    fixes are outside the cylinder, but the straight segment between them
    passes through it.
    """
    dx = bx - ax
    dy = by - ay
    l2 = dx * dx + dy * dy
    if l2 <= 1e-18:
        return False
    u = ((cx - ax) * dx + (cy - ay) * dy) / l2
    if u <= 0.0 or u >= 1.0:
        return False
    qx = ax + u * dx
    qy = ay + u * dy
    return (qx - cx) * (qx - cx) + (qy - cy) * (qy - cy) <= radius * radius


def _first_validation(task, fixes, wp_index: int, now: float) -> float | None:
    """When the pilot first entered a control zone, ignoring task order."""
    w = task.waypoints[wp_index]
    wx, wy, w_out = w.x, w.y, w.outer
    hypot = math.hypot
    first = True
    prev = None
    d0 = 0.0
    for f in fixes:
        if f.t > now:
            break
        d1 = hypot(f.x - wx, f.y - wy)
        # Validate when either endpoint is inside the tolerance zone, or when
        # a telemetry gap jumps across the zone between two outside endpoints.
        if (not first
                and (d1 <= w_out or d0 <= w_out
                     or _segment_reaches_radius(prev.x, prev.y, f.x, f.y,
                                                wx, wy, w_out))):
            return float(f.t)
        d0 = d1
        prev = f
        first = False
    return None


def _run_from_start(task, fixes, i0, start_cross_t, now, alt_gps=True, trace=None):
    """Play the task forward from one candidate start. Returns a partial result.

    Factored out because S7F 8.1 requires evaluating *every* valid start and
    keeping the best one — the state machine genuinely has to run more than
    once. Recompute-from-scratch makes that affordable (see DESIGN.md §7).

    `trace`, when given, is a list that receives one evidence record per
    decision the state machine makes. It is the *same* pass that produces the
    score, so an audit cannot disagree with the result it is auditing — there
    is no second implementation to drift (DESIGN.md §17). The hot path pays one
    `is not None` test per validated zone, not per fix.
    """
    goal_i = task.goal_index
    ess_i = task.ess_index
    deadline = task.goal_deadline
    ess_remaining = task.remaining[ess_i]

    tags = [None] * len(task.waypoints)
    tag_fix = [-1] * len(task.waypoints)
    next_wp = task.start_index + 1
    min_remaining = task.total_distance
    min_fix = i0
    ess_time = goal_time = None
    goal_alt = None
    missed_deadline = False
    samples: list[tuple[float, float]] = []
    min_to_ess = task.speed_distance
    last_task_time = 0.0

    # Hoisted out of the loop: `first_gate` is a property, and resolving it per
    # fix cost 1.6 M attribute lookups over a 129-pilot field.
    first_gate = task.first_gate
    hypot = math.hypot
    wps = task.waypoints
    remaining = task.remaining
    n_fix = len(fixes)
    no_deadline = deadline is None

    # The zone test below is geo.zone_crossing inlined, for two reasons: it was
    # called once per fix and built two 3-tuples each time, and it recomputed
    # the distance to the current waypoint that _remaining() needed anyway.
    # Carrying `d0` forward from the previous iteration removes a third.
    # Together that is five square roots per fix down to one.
    #
    # The inlined form is *equivalent*, not merely similar, and engine/
    # invariants.check_inlined_zone_test proves it against geo.zone_crossing on
    # a million random segments. Read geo.zone_crossing for what the rule is;
    # this is only how it is evaluated.
    prev = fixes[i0]
    w = wps[next_wp] if next_wp <= goal_i else None
    if w is not None:
        wx, wy, w_in, w_out, w_r = w.x, w.y, w.inner, w.outer, w.measure
        rem_base = remaining[next_wp]
        d0 = hypot(prev.x - wx, prev.y - wy)
    else:
        wx = wy = w_in = w_out = w_r = rem_base = d0 = 0.0

    for i in range(i0 + 1, n_fix):
        f = fixes[i]
        ft = f.t
        if ft > now:
            break
        if not no_deadline and ft > deadline:
            break
        fx, fy = f.x, f.y

        if w is not None:
            d1 = hypot(fx - wx, fy - wy)
            # A crossing is a transition across either tolerance boundary in
            # either direction (S7F 9.2.1). Being inside the outer boundary at
            # either endpoint validates too. Finally, a telemetry gap can have
            # both endpoints outside while the segment between them passes
            # through the cylinder; that validates at the later fix timestamp,
            # preserving the engine's no-interpolated-scoring-time rule.
            inside = d1 <= w_out
            segment_only = (
                not inside and d0 > w_out
                and _segment_reaches_radius(prev.x, prev.y, fx, fy, wx, wy, w_out)
            )
            if inside or d0 <= w_out or segment_only:
                # Label only — the validation above is what scores. A crossing
                # is a transition across either boundary; being inside the outer
                # boundary with no transition means the boundary event itself
                # was never seen, which in practice is a telemetry gap over the
                # cylinder. Both validate the zone at the same timestamp.
                transition = (((d0 < w_in) != (d1 < w_in))
                              or ((d0 <= w_out) != (d1 <= w_out)))
                how = ("boundary crossing" if transition
                       else "segment crossing" if segment_only
                       else "inside zone, no boundary event")
                outward = d1 > d0
                t_cross = float(ft)
                if next_wp == goal_i and not no_deadline and t_cross > deadline:
                    missed_deadline = True
                    if trace is not None:
                        trace.append({
                            "kind": "goal_after_deadline", "wp": next_wp,
                            "t": t_cross, "deadline": deadline, "fix": i,
                        })
                else:
                    tags[next_wp] = t_cross
                    tag_fix[next_wp] = i
                    if trace is not None:
                        trace.append({
                            "kind": "validated", "wp": next_wp, "t": t_cross,
                            "fix": i, "how": how, "outward": outward,
                            "d_prev": d0, "d_fix": d1,
                            "alt_gps": f.alt_gps, "alt_baro": f.alt_baro,
                            "lat": f.lat, "lon": f.lon,
                        })
                    if next_wp == ess_i:
                        ess_time = t_cross
                    if next_wp == goal_i:
                        goal_time = t_cross
                        # S7F 9.2.1: the crossing altitude is the tracklog
                        # point's altitude, same rule as the crossing time.
                        goal_alt = float(f.alt_gps if alt_gps else f.alt_baro)
                    next_wp += 1
                    # Retarget onto the new control zone. Only ever runs once
                    # per waypoint, so the extra sqrt here is free.
                    if next_wp <= goal_i:
                        w = wps[next_wp]
                        wx, wy = w.x, w.y
                        w_in, w_out, w_r = w.inner, w.outer, w.measure
                        rem_base = remaining[next_wp]
                        d1 = hypot(fx - wx, fy - wy)
                    else:
                        w = None

            if w is not None:
                edge = d1 - w_r
                rem = (edge + rem_base) if edge > 0.0 else rem_base
                d0 = d1
            else:
                rem = 0.0
        else:
            rem = 0.0

        if rem < min_remaining:
            min_remaining = rem
            min_fix = i

        # leading coefficient input: monotonically decreasing distance to ESS
        if ess_time is None:
            d_ess = rem - ess_remaining
            d_ess = d_ess * 0.001 if d_ess > 0.0 else 0.0
            if d_ess < min_to_ess:
                min_to_ess = d_ess
                samples.append((float(ft) - first_gate, d_ess))
        last_task_time = float(ft) - first_gate

        prev = f
        if goal_time is not None:
            break

    raw = task.total_distance - min_remaining
    if goal_time is not None:
        raw = task.total_distance
    return {
        "tags": tags, "next_wp": next_wp, "raw": max(0.0, raw),
        "ess": ess_time, "goal": goal_time, "goal_alt": goal_alt,
        "missed": missed_deadline,
        "samples": samples, "start_cross": start_cross_t,
        "last_task_time": last_task_time,
        "tag_fix": tag_fix, "min_fix": min_fix, "min_remaining": min_remaining,
    }


def _score_pilot_core(task: CompiledTask, fixes: list[Fix], now: float,
                      params: GapParams,
                      trace: dict | None = None) -> PilotResult:
    """Derive a pilot's complete task state from their point list.

    Pure: no clock, no I/O, no globals. Given the same points, the same `now`
    and the same parameters, it returns the same result every time.

    Pass `trace` — an empty dict — to have the function also record why it
    decided what it decided: every SSS crossing it found, which one it scored
    and under which rule, every control zone validation with the two fixes that
    bracket it, and the fix that fixed the scored distance. See engine/audit.py.
    Tracing does not change any scored value; ``--verify`` asserts that.
    """
    r = PilotResult(tags=[None] * len(task.waypoints))
    if not fixes:
        return r

    sw = task.waypoints[task.start_index]
    is_race = task.start_type.upper().startswith("RACE")
    first_gate = task.first_gate

    launch_x, launch_y = fixes[0].x, fixes[0].y
    used = 0
    prev = None
    candidates: list[tuple[int, float]] = []   # (index, crossing time)
    early = False

    hypot = math.hypot
    sx, sy, s_in, s_out = sw.x, sw.y, sw.inner, sw.outer
    to_r2 = TAKEOFF_RADIUS * TAKEOFF_RADIUS
    airborne = False
    d0 = hypot(fixes[0].x - sx, fixes[0].y - sy)

    for i, f in enumerate(fixes):
        ft = f.t
        if ft > now:
            break
        used = i + 1
        fx, fy = f.x, f.y
        if not airborne:
            # Squared comparison: the actual distance is only needed on the one
            # fix that crosses the threshold.
            dx, dy = fx - launch_x, fy - launch_y
            if dx * dx + dy * dy > to_r2:
                airborne = True
                r.takeoff_time = ft
                if trace is not None:
                    trace["takeoff"] = {
                        "fix": i, "t": ft, "lat": f.lat, "lon": f.lon,
                        "alt_gps": f.alt_gps,
                        "from_launch": hypot(dx, dy),
                        "radius": TAKEOFF_RADIUS,
                    }
        if prev is not None:
            # S7F 6.2.1: "the designation of 'enter' or 'exit' cylinder has been
            # removed... The direction in which such a crossing occurs is
            # irrelevant. Task setters may still choose to indicate whether the
            # start or subsequent turnpoint cylinders are 'enter' or 'exit', to
            # explain their intended task route. But pilots are not bound to
            # those indications."
            #
            # So the SSS is validated by ANY crossing of its tolerance band.
            # task.start_direction is retained for display only; scoring must
            # not consult it. Gating the start on direction makes tasks whose
            # declared direction is wrong -- or whose first turnpoint sits
            # inside the start cylinder -- score as "nobody started".
            # geo.zone_crossing inlined — see the note in _run_from_start.
            # This is still a strict event test, with no "already inside"
            # fallback: a pilot orbiting inside the SSS must not generate a
            # candidate start on every fix. The segment-only case handles
            # telemetry gaps whose endpoints are both outside but whose segment
            # crosses the cylinder.
            d1 = hypot(fx - sx, fy - sy)
            transition = (((d0 < s_in) != (d1 < s_in))
                          or ((d0 <= s_out) != (d1 <= s_out)))
            segment_only = (
                not transition and d0 > s_out and d1 > s_out
                and _segment_reaches_radius(prev.x, prev.y, fx, fy, sx, sy, s_out)
            )
            if transition or segment_only:
                t_cross = float(ft)
                after_gate = t_cross >= first_gate
                if after_gate:
                    candidates.append((i, t_cross))
                else:
                    early = True          # [PG] S7F 13.3
                if trace is not None:
                    trace.setdefault("sss_crossings", []).append({
                        "fix": i, "t": t_cross, "outward": d1 > d0,
                        "after_gate": after_gate,
                        "segment_only": segment_only,
                        "d_prev": d0, "d_fix": d1,
                        "lat": f.lat, "lon": f.lon,
                        "alt_gps": f.alt_gps, "alt_baro": f.alt_baro,
                    })
            d0 = d1
        prev = f

    last = fixes[used - 1] if used else fixes[0]
    r.last_t, r.last_lat, r.last_lon, r.last_alt = last.t, last.lat, last.lon, last.alt_gps
    r.fixes_used = used

    # S7F 8.1: re-starting exists only in Races with MULTIPLE start gates and
    # in Time Trials. With a single gate a pilot cannot take a later start, so
    # their start is simply their last SSS crossing before they validate the
    # next control zone — consistent with S7F 13.3, which defines an early
    # start by the pilot's *last* SSS crossing.
    #
    # This is not just a rules detail: on a task whose SSS sits on the takeoff
    # cylinder, pilots thermalling near launch produce a median of 6 and up to
    # 36 exit crossings each. Evaluating every one of them as a candidate start
    # costs 7x the scoring time for a result the rules do not ask for.
    multi_start = start_selection.is_multi_start(task.start_type, len(task.gates))
    if trace is not None:
        trace["start_rule"] = {
            "start_type": task.start_type, "gates": list(task.gates),
            "multi_start": multi_start,
            "n_crossings": len(trace.get("sss_crossings", [])),
            "n_after_gate": len(candidates),
            "early": early,
        }
    # S7F 8.1 — which of these crossings is the start. The rule, the
    # degenerate concentric-cylinder fallback and the candidate cap all live in
    # engine/rules/start_selection.py so they can be checked on their own; this
    # only supplies the one thing that needs the tracklog, namely when the zone
    # after the SSS was first validated.
    if candidates:
        t_next = (None if multi_start else
                  _first_validation(task, fixes, task.start_index + 1, now))
        candidates, rule_text = start_selection.select_candidates(
            candidates, multi_start, t_next)
        if trace is not None:
            trace["start_rule"].update({
                "next_zone": task.waypoints[task.start_index + 1].name,
                "next_zone_first_validated": t_next,
                "rule": rule_text,
            })

    # Score the start that produced the biggest distance; if several reached
    # goal, the LAST start after which goal was reached.
    # S7F 8.1 — score the start that produced the biggest distance; if several
    # reached goal, the LAST start after which goal was reached. The comparison
    # itself is engine/rules/start_selection.better_start, so the rule is
    # stated once and tested on its own.
    best = None
    best_idx = -1
    for idx, t_cross in candidates:
        sub = [] if trace is not None else None
        run = _run_from_start(task, fixes, idx, t_cross, now, params.altitude_gps, sub)
        run["fix"] = idx
        run["trace"] = sub
        if trace is not None:
            trace.setdefault("candidates", []).append({
                "fix": idx, "t": t_cross, "raw": run["raw"],
                "goal": run["goal"], "ess": run["ess"],
            })
        if start_selection.better_start(run, best, t_cross):
            best, best_idx = run, idx

    if best is None:
        # Never made a valid start.
        r.early_start = early
        if trace is not None:
            trace["scored_start"] = None
        if early:
            # [PG] S7F 13.3: scored only for launch -> SSS distance.
            r.raw_distance = max(0.0, task.launch_to_sss)
        r.distance = max(params.minimum_distance, r.raw_distance) if r.takeoff_time else 0.0
        r.state = AIRBORNE if r.takeoff_time else WAITING
        return r

    r.tags = best["tags"]
    r.tags[task.start_index] = best["start_cross"]
    r.tag_fix_index = best["tag_fix"]
    r.tag_fix_index[task.start_index] = best_idx
    r.start_fix_index = best_idx
    r.dist_fix_index = best["min_fix"]
    if trace is not None:
        trace["scored_start"] = {"fix": best_idx, "t": best["start_cross"]}
        trace["zones"] = best["trace"]
        trace["min_remaining"] = best["min_remaining"]
    r.next_wp = best["next_wp"]
    r.ess_time = best["ess"]
    r.goal_time = best["goal"]
    r.goal_alt = best["goal_alt"]
    r.goal_missed_deadline = best["missed"]
    r.lead_samples = best["samples"]
    r.last_task_time = best["last_task_time"]
    r.start_cross_time = best["start_cross"]
    r.start_time = (
        max((g for g in task.gates if g <= best["start_cross"]), default=first_gate)
        if is_race else best["start_cross"]
    )
    r.raw_distance = best["raw"]
    r.distance = max(params.minimum_distance, best["raw"])
    r.tp_count = sum(1 for i in range(task.start_index + 1, task.goal_index)
                     if r.tags[i] is not None)

    if r.goal_time is not None:
        r.state = GOAL_DONE
    elif r.ess_time is not None:
        r.state = ESS_DONE
    else:
        r.state = STARTED

    if r.ess_time is not None and r.start_time is not None:
        r.speed_section_time = r.ess_time - r.start_time
        if r.speed_section_time > 0:
            r.speed = task.speed_distance / r.speed_section_time * 3.6
    return r


def _detect_landing_cutoff(fixes: list[Fix], after: float,
                           before: float) -> tuple[float, int] | None:
    """Return the first fix of a sustained stationary window, if one exists.

    This is not an aircraft-state classifier. It is a scoring guardrail for
    final/live feeds that continue after landing. A candidate is accepted only
    when every fix for LANDING_WINDOW_S seconds stays within LANDING_RADIUS_M
    of the window anchor and the GPS-altitude range stays below
    LANDING_ALTITUDE_RANGE_M.
    """
    if not fixes or before <= after:
        return None

    times = [f.t for f in fixes]
    start = bisect.bisect_left(times, int(after))
    end = bisect.bisect_right(times, int(before))
    if end - start < 2:
        return None

    radius2 = LANDING_RADIUS_M * LANDING_RADIUS_M
    i = start
    while i < end:
        f0 = fixes[i]
        j = bisect.bisect_left(times, f0.t + LANDING_WINDOW_S, i, end)
        if j >= end:
            return None

        min_alt = max_alt = f0.alt_gps
        x0, y0 = f0.x, f0.y
        stationary = True
        for k in range(i, j + 1):
            f = fixes[k]
            if f.alt_gps < min_alt:
                min_alt = f.alt_gps
            elif f.alt_gps > max_alt:
                max_alt = f.alt_gps
            if (max_alt - min_alt > LANDING_ALTITUDE_RANGE_M
                    or (f.x - x0) * (f.x - x0) + (f.y - y0) * (f.y - y0) > radius2):
                stationary = False
                break
        if stationary:
            return float(f0.t), i

        next_t = f0.t + LANDING_SCAN_STEP_S
        ni = bisect.bisect_left(times, next_t, i + 1, end)
        i = ni if ni > i else i + 1
    return None


def _score_until_landing_if_needed(task: CompiledTask, fixes: list[Fix],
                                   now: float, params: GapParams,
                                   initial: PilotResult,
                                   trace: dict | None = None) -> PilotResult:
    if initial.start_cross_time is not None:
        after = initial.start_cross_time + LANDING_AFTER_START_DELAY_S
    elif initial.takeoff_time is not None:
        after = initial.takeoff_time + LANDING_AFTER_START_DELAY_S
    else:
        return initial

    # A goal pilot is still searched to `now`: the crossing itself may have come
    # from ground movement after landing (retrieve vehicles drive to goal), and
    # `last_t` would already be the bogus crossing. Scoring the whole track is
    # what lets the landing below be found at all.
    if initial.goal_time is not None:
        before = float(now)
    else:
        before = min(float(now), float(initial.last_t or now))
    landing = _detect_landing_cutoff(fixes, after, before)
    if landing is None:
        return initial

    landing_time, landing_index = landing
    if landing_time >= now:
        return initial

    # A pilot who genuinely flew to goal landed after crossing it; leave them
    # alone. Only a "goal" reached after a sustained landing is ground movement,
    # and that one gets re-scored with the clock cut at the landing.
    if initial.goal_time is not None and initial.goal_time <= landing_time:
        return initial

    # Replay the exact same state machine with the scoring clock cut at the
    # landing time. The first pass only decides whether a landing cutoff is
    # needed; all scored fields come from this final pass.
    final = _score_pilot_core(task, fixes, landing_time, params, trace)
    final.landing_time = landing_time
    final.landing_fix_index = landing_index
    if trace is not None:
        trace["landing"] = {
            "fix": landing_index,
            "t": landing_time,
            "window_s": LANDING_WINDOW_S,
            "radius_m": LANDING_RADIUS_M,
            "altitude_range_m": LANDING_ALTITUDE_RANGE_M,
        }
    return final


def score_pilot(task: CompiledTask, fixes: list[Fix], now: float,
                params: GapParams, trace: dict | None = None) -> PilotResult:
    """Derive a pilot's complete task state from their point list.

    This wraps the pure state-machine pass with the S7F landing cutoff. Goal
    pilots are returned from the first pass; non-goal pilots are replayed to the
    detected landing time when their track keeps recording afterwards.
    """
    if trace is None:
        initial = _score_pilot_core(task, fixes, now, params, None)
        final = _score_until_landing_if_needed(
            task, fixes, now, params, initial)
        return _apply_exact_progress(task, fixes, now, params, final, None)

    # Keep trace deterministic: first decide the cutoff without recording
    # events, then record only the final scored pass.
    initial = _score_pilot_core(task, fixes, now, params, None)
    final = _score_until_landing_if_needed(task, fixes, now, params, initial, trace)
    if final is initial:
        final = _score_pilot_core(task, fixes, now, params, trace)
    return _apply_exact_progress(task, fixes, now, params, final, trace)
