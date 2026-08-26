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
from dataclasses import dataclass, field

from .gap import GapParams
from .geo import in_zone, zone_crossing
from .rules import start_selection
from .rules.distance_flown import distance_flown
from .rules.distance_flown import distance_to_goal as rules_distance_to_goal
from .igc import Fix
from .task import CompiledTask

WAITING, AIRBORNE, STARTED, ESS_DONE, GOAL_DONE = "WAITING", "AIRBORNE", "STARTED", "ESS", "GOAL"
NOT_STARTED = "NO START"   # launched, but never made a valid SSS crossing

TAKEOFF_RADIUS = 200.0     # m from first fix before we call it airborne

# Each candidate start costs a full replay of the pilot's track. A task whose
# SSS is re-crossed during the course can generate dozens; evaluating all of
# them turns a 4 ms pilot into a 200 ms one for no change in result.
MAX_START_CANDIDATES = start_selection.MAX_START_CANDIDATES


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

    # Audit anchors. These are indices into the pilot's own point list, so a
    # protest can be answered by naming the fix that decided the number rather
    # than by repeating the number (DESIGN.md §17). Filling them costs one
    # integer assignment on a path that already runs, so they are always
    # present, not only when tracing.
    dist_fix_index: int = -1        # the fix that set the scored distance
    start_fix_index: int = -1       # the fix whose timestamp is the start
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


def _first_validation(task, fixes, wp_index: int, now: float) -> float | None:
    """When the pilot first entered a control zone, ignoring task order."""
    w = task.waypoints[wp_index]
    wx, wy, w_out = w.x, w.y, w.outer
    hypot = math.hypot
    first = True
    d0 = 0.0
    for f in fixes:
        if f.t > now:
            break
        d1 = hypot(f.x - wx, f.y - wy)
        # Same reduction as the other two loops: a transition across either
        # boundary, or being inside the outer one, holds exactly when either
        # end of the segment is within the outer boundary.
        if not first and (d1 <= w_out or d0 <= w_out):
            return float(f.t)
        d0 = d1
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
            # either direction (S7F 9.2.1); being inside the outer boundary at
            # this fix validates too, which together reduce to "either end of
            # the segment is within the outer boundary".
            inside = d1 <= w_out
            if inside or d0 <= w_out:
                # Label only — the validation above is what scores. A crossing
                # is a transition across either boundary; being inside the outer
                # boundary with no transition means the boundary event itself
                # was never seen, which in practice is a telemetry gap over the
                # cylinder. Both validate the zone at the same timestamp.
                transition = (((d0 < w_in) != (d1 < w_in))
                              or ((d0 <= w_out) != (d1 <= w_out)))
                how = ("boundary crossing" if transition
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


def score_pilot(task: CompiledTask, fixes: list[Fix], now: float,
                params: GapParams, trace: dict | None = None) -> PilotResult:
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
            # geo.zone_crossing inlined — see the note in _run_from_start. Note
            # this is the strict TRANSITION test, with no "already inside"
            # fallback: a pilot orbiting inside the SSS must not generate a
            # candidate start on every fix.
            d1 = hypot(fx - sx, fy - sy)
            if ((d0 < s_in) != (d1 < s_in)) or ((d0 <= s_out) != (d1 <= s_out)):
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
