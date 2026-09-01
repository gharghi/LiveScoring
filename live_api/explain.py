"""Protest answers: why a competition, a task or a pilot scored what it did.

A pilot at the scoring desk does not want the number, they already have it.
They want to know which rule produced it, over which population, from which
inputs, and what the arithmetic was.  ``engine/audit.py`` already assembles
exactly that for one pilot straight out of the scoring pass; this module feeds
it from the live database and adds the two wider scopes around it.

Nothing here recomputes a formula of its own.  Every number is either read
back from the stored snapshot or produced by re-running the engine, so this
module cannot disagree with the scorer -- if it ever did, that is a bug in the
engine and the ``replay_agrees`` flag on a pilot explanation says so.

The pilot scope re-scores the whole field, because half of a protest ("my
points dropped and I did not move") is about the field, not the pilot: goal
ratio, best distance, best time and LCmin all move when somebody else lands.
That makes this endpoint slow and deliberately so -- it is a desk tool, not a
live path.
"""
from __future__ import annotations

import dataclasses

from engine.audit import audit_pilot, field_summary, points_derivation
from engine.igc import Fix
from engine.score import project, score_pilot
from engine.scoring import score_task

from .models import TrackingPoint

# Bulky, and meaningless to a reader: the per-fix leading samples are thousands
# of tuples whose reduction (lead_area, lead_min_to_ess) is already reported.
_HEAVY_RESULT_FIELDS = {"lead_samples", "tags"}


def _compile(task):
    """Compile the task and the GAP parameters exactly as the scorer does."""
    from score_worker import compile_task, gap_params, pilots_present
    settings = task.settings or {}
    if not settings.get("xctsk"):
        raise ValueError("task has no xctsk geometry; nothing to explain yet")
    compiled = compile_task(settings)
    params = gap_params(task.competition.settings or {})
    present = pilots_present(task.competition.settings or {})
    return compiled, params, present


def _load_tracks(task):
    """Every pilot's fixes for this task, deduplicated per (pilot, second).

    Uses the ORM rather than the worker's raw SQL so it behaves the same on
    both database backends; the explain path handles one task at a time and
    does not need the worker's streaming.
    """
    rows = (TrackingPoint.objects
            .filter(task=task)
            .order_by("pilot_id", "timestamp", "-id")
            .values_list("pilot_id", "timestamp", "latitude", "longitude",
                         "altitude_baro", "altitude_gps"))
    tracks: dict[str, list[Fix]] = {}
    seen: set[tuple[str, int]] = set()
    for pilot_id, ts, lat, lon, baro, gps in rows.iterator(chunk_size=20000):
        epoch = int(ts.timestamp())
        if (pilot_id, epoch) in seen:
            continue
        seen.add((pilot_id, epoch))
        tracks.setdefault(pilot_id, []).append(
            Fix(epoch, lat, lon, int(baro or 0), int(gps or 0)))
    return tracks


class _Track:
    """The shape engine.audit expects of a track: fixes plus their provenance."""

    def __init__(self, pilot_id, fixes):
        self.fixes = fixes
        self.source_files = [f"db:tracking_points/{pilot_id}"]


def _score_field(task):
    """Re-run the whole field, returning everything an explanation can need."""
    compiled, params, present = _compile(task)
    tracks = _load_tracks(task)
    if not tracks:
        raise ValueError("no tracking points stored for this task yet")
    now = max(fixes[-1].t for fixes in tracks.values() if fixes)
    results = []
    for pilot_id, fixes in sorted(tracks.items()):
        project(compiled, fixes)
        r = score_pilot(compiled, fixes, now, params)
        r.pilot = pilot_id
        results.append(r)
    ts = score_task(compiled, results, params,
                    pilots_present=present if present is not None else len(results))
    ranked = sorted(results, key=lambda r: r.rank_key)
    ranks = {r.pilot: i for i, r in enumerate(ranked, 1)}
    return {"task": compiled, "params": params, "results": results, "ts": ts,
            "now": now, "tracks": tracks, "ranks": ranks}


def _result_json(r):
    """A PilotResult as JSON, minus the fields that are bulk rather than evidence."""
    out = {}
    for f in dataclasses.fields(r):
        if f.name in _HEAVY_RESULT_FIELDS:
            continue
        v = getattr(r, f.name)
        out[f.name] = [dataclasses.asdict(x) if dataclasses.is_dataclass(x) else x
                       for x in v] if isinstance(v, list) else v
    return out


def _geometry(compiled):
    return {
        "total_distance_m": compiled.total_distance,
        "speed_section_distance_m": compiled.speed_distance,
        "launch_to_sss_m": compiled.launch_to_sss,
        "waypoints": [{
            "index": w.index, "name": w.name, "kind": w.kind,
            "lat": w.lat, "lon": w.lon,
            "radius_m": w.radius,
            "measurement_radius_m": w.measure,
            "tolerance_inner_m": w.inner, "tolerance_outer_m": w.outer,
            "optimised_remaining_m": compiled.remaining[w.index]
            if w.index < len(compiled.remaining) else None,
        } for w in compiled.waypoints],
    }


def explain_competition(comp):
    """Why the competition is configured the way it is, and what it contains."""
    cfg = comp.settings or {}
    tasks = []
    for t in comp.tasks.all().order_by("created_at"):
        snap = getattr(t, "score_snapshot", None)
        tasks.append({
            "task_id": t.external_manga_id,
            "name": t.name,
            "status": (t.settings or {}).get("status"),
            "date": (t.settings or {}).get("task_date"),
            "scored": bool(snap),
            "scoring_status": getattr(snap, "status", None),
            "point_count": getattr(snap, "point_count", None),
            "task_score": getattr(snap, "task_score", None),
        })
    return {
        "scope": "competition",
        "event_id": comp.external_event_id,
        "name": comp.name,
        "rules": "FAI Sporting Code Section 7F, 2026 edition V1.0 — paragliding",
        "formula": cfg.get("formula"),
        "scoring_parameters": {k: v for k, v in cfg.items()
                               if k not in {"pilots", "categories", "formula"}},
        "categories": cfg.get("categories", []),
        "pilots_registered": len(cfg.get("pilots", []) or []),
        "tasks": tasks,
        "narrative": [
            f"Event '{comp.name}' ({comp.external_event_id}) is scored under GAP as "
            f"defined by FAI Sporting Code Section 7F, 2026 edition, paragliding.",
            f"{len(cfg.get('pilots', []) or [])} pilots are registered and "
            f"{len(tasks)} task(s) have been synchronised.",
            "Per-task validity, available points and the winner's coefficients are "
            "reported by the task scope; ask for ?task_id=<id> to see them.",
        ],
    }


def explain_task(task):
    """Why the task allocated the points it did, over which population."""
    field = _score_field(task)
    compiled, ts, params = field["task"], field["ts"], field["params"]
    summary = field_summary(compiled, ts, params)
    a = ts.alloc
    return {
        "scope": "task",
        "task_id": task.external_manga_id,
        "event_id": task.competition.external_event_id,
        "rules": "FAI Sporting Code Section 7F, 2026 edition V1.0 — paragliding",
        "geometry": _geometry(compiled),
        "field": summary,
        "derivation": [
            {"part": "launchValidity", "ref": "S7F 10.1",
             "formula": "from pilots flying against nominal launch",
             "substituted": f"{ts.pilots_flying} flying of {ts.pilots_present} present, "
                            f"nominalLaunch {params.nominal_launch}",
             "value": ts.launch_validity},
            {"part": "distanceValidity", "ref": "S7F 10.2",
             "formula": "from the field's distances against nominal distance",
             "substituted": f"bestDistance {ts.best_distance:,.1f} m, "
                            f"nominalDistance {params.nominal_distance:,.0f} m",
             "value": ts.distance_validity},
            {"part": "timeValidity", "ref": "S7F 10.3",
             "formula": "from best time against nominal time",
             "substituted": f"bestTime {ts.best_time or 0:,.0f} s, "
                            f"nominalTime {params.nominal_time:,.0f} s",
             "value": ts.time_validity},
            {"part": "taskValidity", "ref": "S7F 10",
             "formula": "launchValidity × distanceValidity × timeValidity",
             "substituted": f"{ts.launch_validity:.4f} × {ts.distance_validity:.4f} "
                            f"× {ts.time_validity:.4f}",
             "value": ts.task_validity},
            {"part": "goalRatio", "ref": "S7F 12",
             "formula": "pilotsInGoal / pilotsFlying",
             "substituted": f"{ts.pilots_goal} / {ts.pilots_flying}",
             "value": a.goal_ratio},
            {"part": "availableDistance", "ref": "S7F 12",
             "formula": "1000 × taskValidity × distanceWeight",
             "substituted": f"1000 × {ts.task_validity:.4f} × {a.distance_weight:.6f}",
             "value": a.available_distance},
            {"part": "availableTime", "ref": "S7F 12",
             "formula": "1000 × taskValidity × timeWeight",
             "substituted": f"1000 × {ts.task_validity:.4f} × {a.time_weight:.6f}",
             "value": a.available_time},
            {"part": "availableLeading", "ref": "S7F 12",
             "formula": "1000 × taskValidity × leadingWeight",
             "substituted": f"1000 × {ts.task_validity:.4f} × {a.leading_weight:.6f}",
             "value": a.available_leading},
        ],
        "narrative": [
            f"The task is {compiled.total_distance/1000:.2f} km total with a "
            f"{compiled.speed_distance/1000:.2f} km speed section.",
            f"{ts.pilots_flying} pilots flew, {ts.pilots_ess} reached ESS and "
            f"{ts.pilots_goal} reached goal, giving a goal ratio of {a.goal_ratio:.4f}.",
            f"Task validity is {ts.task_validity:.4f}, so {a.available_total:,.1f} points "
            f"were available: {a.available_distance:,.1f} distance, "
            f"{a.available_time:,.1f} time, {a.available_leading:,.1f} leading.",
            f"The reference marks every pilot is measured against are "
            f"bestDistance {ts.best_distance:,.1f} m, bestTime {ts.best_time or 0:,.0f} s "
            f"and LCmin {ts.lc_min:.5f}.",
            "If your points changed while you did not move, one of those four moved: "
            "they are field-wide and recomputed every time somebody else lands.",
        ],
    }


def explain_pilot(task, pilot_id):
    """Why this pilot scored what they did, fix by fix and line by line."""
    field = _score_field(task)
    compiled, ts, params = field["task"], field["ts"], field["params"]
    result = next((r for r in field["results"] if r.pilot == str(pilot_id)), None)
    if result is None:
        known = sorted(r.pilot for r in field["results"])
        raise LookupError(f"pilot '{pilot_id}' has no tracking points on this task "
                          f"(known: {', '.join(known[:20])}{'…' if len(known) > 20 else ''})")

    track = _Track(result.pilot, field["tracks"][result.pilot])
    record = audit_pilot(compiled, track, result, ts, params, field["now"],
                         rank=field["ranks"].get(result.pilot),
                         field_size=len(field["results"]))
    points = record["points"]
    by_part = {p["part"]: p for p in points}

    narrative = [
        f"Pilot {result.pilot} is ranked {field['ranks'].get(result.pilot)} of "
        f"{len(field['results'])} with {result.total_points:,.1f} points, state {result.state}.",
        f"Scored distance {result.distance:,.1f} m of a possible "
        f"{compiled.total_distance:,.1f} m.",
    ]
    if "distance" in by_part:
        narrative.append(f"Distance points: {by_part['distance']['formula']} = "
                         f"{by_part['distance']['substituted']} → "
                         f"{by_part['distance']['value']:,.1f}.")
    if "leadingFactor" in by_part:
        narrative.append(f"Leading: {by_part['leadingFactor']['substituted']}; "
                         f"leading points = {by_part['leading']['substituted']} → "
                         f"{by_part['leading']['value']:,.1f}.")
    elif "leading" in by_part:
        narrative.append(f"Leading: {by_part['leading']['formula']} "
                         f"({by_part['leading']['substituted']}).")
    if "time" in by_part:
        narrative.append(f"Time: {by_part['time']['formula']} — "
                         f"{by_part['time']['substituted']} → "
                         f"{by_part['time']['value']:,.1f}.")
    if not record["replay_agrees"]:
        narrative.append("WARNING: re-running this pilot did not reproduce the stored "
                         "result. The mismatches are listed under replay_mismatches "
                         "and should be reported as an engine bug.")

    return {
        "scope": "pilot",
        "task_id": task.external_manga_id,
        "event_id": task.competition.external_event_id,
        "pilot_id": result.pilot,
        "rank": record["rank"],
        "field_size": record["field_size"],
        "rules": record["rules"],
        "scored_to_epoch": record["now"],
        "inputs": record["inputs"],
        "replay_agrees": record["replay_agrees"],
        "replay_mismatches": {k: {"replay": v[0], "stored": v[1]}
                              for k, v in record["replay_mismatches"].items()},
        "track": record["track"],
        "trace": record["trace"],
        "result": _result_json(record["result"]),
        "points": points,
        "field": record["field"],
        "geometry": _geometry(compiled),
        "narrative": narrative,
    }
