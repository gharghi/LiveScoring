"""Task-level GAP scoring — the competition-wide pass.

Per-pilot state comes from score.score_pilot(); everything here needs the whole
field: validity, points allocation, best distance, best time, LCmin.

This split matters for the live engine. Per-pilot state is incremental and
cheap; the task-level pass is O(pilots) and runs once per publish cycle, not
per GPS fix. It is also the part that is genuinely *provisional* — every value
here moves as pilots land (DESIGN.md §11.3).
"""

from __future__ import annotations

from dataclasses import dataclass, field

from . import rules
from .rules import Allocation, GapParams
from .score import PilotResult
from .task import CompiledTask


@dataclass(slots=True)
class TaskScore:
    launch_validity: float = 0.0
    distance_validity: float = 0.0
    time_validity: float = 0.0
    task_validity: float = 0.0
    stopped_validity: float | None = None

    alloc: Allocation = field(default_factory=Allocation)

    pilots_present: int = 0
    pilots_flying: int = 0
    pilots_ess: int = 0
    pilots_goal: int = 0

    best_distance: float = 0.0
    best_time: float | None = None
    lc_min: float = 0.0
    max_time: float = 0.0


def score_task(task: CompiledTask, results: list[PilotResult], params: GapParams,
               pilots_present: int | None = None) -> TaskScore:
    """Apply GAP 2026 paragliding to an already state-machined field.

    Mutates each PilotResult's points fields and returns the task-level totals.
    """
    ts = TaskScore()
    flying = [r for r in results if r.takeoff_time is not None or r.distance > 0]
    ts.pilots_flying = len(flying)
    ts.pilots_present = pilots_present if pilots_present is not None else len(results)
    ts.pilots_ess = sum(1 for r in flying if r.ess_time is not None)
    ts.pilots_goal = sum(1 for r in flying if r.goal_time is not None)

    if not flying:
        return ts

    ts.best_distance = max(r.distance for r in flying)

    # S7F 9.4 / 9.4.1 — whose time counts. [PG] goal only; [HG] ESS is enough.
    # engine/rules/s7f_09_control_zones.py.
    ts.best_time = rules.best_time(flying, paragliding=True)

    # --- task validity (S7F 10) — engine/rules/s7f_10_task_validity.py ---
    # 10.3 keys its fallback on ESS ("if no pilot finishes the speed section"),
    # while [PG] 9.4.1 restricts BestTime to pilots who reached GOAL. Those are
    # different populations whenever ESS and goal are different cylinders, so
    # validity is fed the ESS-based time and time POINTS keep the goal-based
    # one. Identical on any task where ESS is the goal cylinder.
    ts.launch_validity = rules.launch_validity(
        ts.pilots_flying, ts.pilots_present, params)
    ts.distance_validity = rules.distance_validity(
        [r.distance for r in flying], ts.best_distance, params)
    ts.time_validity = rules.time_validity(
        rules.best_time_to_ess(flying), ts.best_distance, params)
    ts.task_validity = rules.task_validity(
        ts.launch_validity, ts.distance_validity, ts.time_validity)

    # --- points allocation (S7F 11) — rules/s7f_11_allocation.py ---
    ts.alloc = rules.allocate(ts.task_validity, ts.pilots_goal, ts.pilots_flying, params)

    # --- leading coefficient (S7F 12.3.1) ---
    # maxTime = min(max(lastOutlanding, lastESS), taskDeadline), as task time.
    # rules/s7f_12_pilot_points.max_time_for() keeps this selectable because
    # AirScore and GlideComp differ in their historical landout handling, but
    # the default follows GlideComp's field-wide interpretation.
    last_out = max((r.last_task_time for r in flying if r.start_time), default=0.0)
    last_ess = max((r.ess_time - task.first_gate for r in flying if r.ess_time), default=0.0)
    ts.max_time = max(last_out, last_ess)
    if task.goal_deadline:
        deadline_t = task.goal_deadline - task.first_gate
        ts.max_time = min(ts.max_time, deadline_t)
        last_ess = min(last_ess, deadline_t)

    sd_km = task.speed_distance / 1000.0
    started = [r for r in flying if r.start_time is not None]
    progress_curve = getattr(task, "progress_curve", "WEIGHTED").upper()
    for r in started:
        # The per-pilot half of the LC may already have been computed — by a
        # worker process, or by an earlier publish cycle whose maxTime has since
        # moved. Only the field-wide half is redone here.
        if r.lead_area is None:
            if progress_curve == "HUMP_V2A":
                r.lead_area, r.lead_min_to_ess = rules.leading_partial_hump_v2a(
                    r.lead_samples, sd_km)
            else:
                r.lead_area, r.lead_min_to_ess = rules.leading_partial(
                    r.lead_samples, sd_km)
        r.max_time = rules.max_time_for(r.last_task_time, last_ess, ts.max_time)
        r.lc = rules.s7f_12_pilot_points.leading_coefficient(
            r.lead_area, r.lead_min_to_ess, sd_km, r.max_time,
            progress_curve=progress_curve,
            last_task_time=r.last_task_time)
    lc_population = (
        [r for r in started if r.ess_time is not None]
        if progress_curve == "HUMP_V2A" and ts.pilots_ess > 0
        else started
    )
    lcs = [r.lc for r in lc_population if r.lc > 0]
    ts.lc_min = min(lcs) if lcs else 0.0

    # --- pilot points (S7F 12), stage by stage -------------------------
    # Each line below is one file under engine/rules/. Kept in pipeline order
    # and one call per rule, so that a disagreement with a published result can
    # be traced to a single module rather than to "the scoring".
    for r in results:
        # step 8 — S7F 12.1  distance points
        r.distance_points = rules.distance_points(
            r.distance, ts.best_distance, ts.alloc.available_distance)

        # step 12 — S7F 13.1  underflying an elevated goal, scales time only
        if (task.goal_elevated and r.goal_time is not None
                and r.goal_alt is not None):
            r.goal_altitude_factor = rules.goal_altitude_factor(
                r.goal_alt, task.waypoints[task.goal_index].alt,
                task.goal_elevation)
        else:
            r.goal_altitude_factor = 1.0

        # step 13 — S7F 13.2  ESS but not goal ([PG] zero time points)
        gate = rules.ess_no_goal_factor(
            in_goal=r.goal_time is not None,
            reached_ess=r.ess_time is not None,
            configured_factor=params.ess_no_goal_time_factor)

        # step 9 — S7F 12.2  time points, with 13.1 and 13.2 folded in
        if ts.best_time and r.speed_section_time:
            r.time_points = rules.time_points(
                r.speed_section_time, ts.best_time, ts.alloc.available_time,
                altitude_factor=r.goal_altitude_factor, ess_no_goal_factor=gate)
        else:
            r.time_points = 0.0

        # step 10 — S7F 12.3  leading points
        r.leading_points = rules.leading_points(
            r.lc, ts.lc_min, ts.alloc.available_leading)

        # step 11 — S7F 11 / 12.4  arrival points ([PG] never any)
        arrival = ts.alloc.available_arrival

        # S7F 12 — each component is already rounded to one decimal by its own
        # rule; this rounds the sum of those rounded values, which is not the
        # same as rounding the sum of the unrounded ones.
        r.total_points = rules.task_score(r.distance_points, r.time_points,
                                          r.leading_points, arrival)

    # step 16 — S7F 13.5  penalties, LAST, on the rounded total.
    # Applied by the caller via rules.apply_penalties(), which needs the tracks
    # to match pilot IDs and must report anything it could not match.
    return ts
