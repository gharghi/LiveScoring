#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import os
import tempfile
import time
import traceback
from datetime import datetime, timezone

import psycopg

from engine.geo import haversine
from engine.igc import Fix
from engine.rules.params import GapParams
from engine.score import project, score_pilot
from engine.scoring import score_task
from engine.task import parse_xctsk


def _json(value):
    if isinstance(value, str):
        return json.loads(value)
    return value


def db_connect():
    return psycopg.connect(
        dbname=os.environ["POSTGRES_DB"],
        user=os.environ.get("POSTGRES_USER", "livescoring"),
        password=os.environ.get("POSTGRES_PASSWORD", ""),
        host=os.environ.get("POSTGRES_HOST", "127.0.0.1"),
        port=os.environ.get("POSTGRES_PORT", "5432"),
        autocommit=True,
    )


def scoreable_tasks(conn, task_filter=None, force=False):
    params = []
    filters = [
        "t.settings ? 'xctsk'",
        "t.settings ? 'date_epoch'",
        "COALESCE(stats.point_count, 0) > 0",
    ]
    if task_filter:
        filters.append("(t.id::text = %s OR t.external_manga_id = %s)")
        params.extend([task_filter, task_filter])
    if not force:
        filters.append("""
            (
                snapshot.task_id IS NULL
                OR snapshot.processed_epoch IS DISTINCT FROM stats.max_epoch
                OR snapshot.point_count IS DISTINCT FROM stats.point_count
                OR snapshot.status <> 'ok'
            )
        """)

    sql = f"""
        SELECT t.id::text, t.external_manga_id, t.settings,
               c.id::text, c.settings,
               stats.point_count, stats.max_epoch
        FROM live_api_task t
        JOIN live_api_competition c ON c.id = t.competition_id
        LEFT JOIN LATERAL (
            SELECT COUNT(*)::integer AS point_count,
                   EXTRACT(EPOCH FROM MAX(timestamp))::bigint AS max_epoch
            FROM live_api_trackingpoint p
            WHERE p.task_id = t.id
        ) stats ON true
        LEFT JOIN live_api_taskresultsnapshot snapshot ON snapshot.task_id = t.id
        WHERE {' AND '.join(filters)}
        ORDER BY stats.max_epoch NULLS LAST, t.created_at
    """
    with conn.cursor() as cur:
        cur.execute(sql, params)
        return cur.fetchall()


def compile_task(task_settings):
    settings = _json(task_settings)
    with tempfile.NamedTemporaryFile(mode="w", suffix=".xctsk") as fh:
        json.dump(settings["xctsk"], fh)
        fh.flush()
        return parse_xctsk(fh.name, int(settings.get("date_epoch", 0)))


def gap_params(comp_settings):
    cfg = _json(comp_settings)
    return GapParams(
        nominal_distance=float(cfg.get("nominal_distance_km", 60)) * 1000,
        minimum_distance=float(cfg.get("minimum_distance_km", 5)) * 1000,
        nominal_time=float(cfg.get("nominal_time_min", 90)) * 60,
        leading_time_ratio=float(cfg.get("leading_time_ratio", 0.26)),
        ess_no_goal_time_factor=float(cfg.get("ess_no_goal_time_factor", 0.0)),
    )


def pilots_present(comp_settings):
    cfg = _json(comp_settings)
    if cfg.get("pilots_present") is not None:
        return cfg["pilots_present"]
    pilots = cfg.get("pilots")
    if isinstance(pilots, list):
        return len(pilots)
    return None


def load_work(conn, task_id):
    sql = """
        SELECT pilot_id, epoch, latitude, longitude, altitude_baro, altitude_gps
        FROM (
            SELECT DISTINCT ON (pilot_id, timestamp)
                pilot_id,
                EXTRACT(EPOCH FROM timestamp)::bigint AS epoch,
                latitude,
                longitude,
                COALESCE(altitude_baro, 0)::integer AS altitude_baro,
                COALESCE(altitude_gps, 0)::integer AS altitude_gps,
                timestamp
            FROM live_api_trackingpoint
            WHERE task_id = %s::uuid
            ORDER BY pilot_id, timestamp, id DESC
        ) points
        ORDER BY pilot_id, timestamp
    """
    work = []
    current_pilot = None
    current_fixes = None
    point_count = 0
    max_epoch = None
    with conn.cursor() as cur:
        cur.execute(sql, [task_id])
        while True:
            chunk = cur.fetchmany(20000)
            if not chunk:
                break
            for pilot, epoch, lat, lon, baro, gps in chunk:
                if pilot != current_pilot:
                    if current_pilot is not None:
                        work.append((current_pilot, current_fixes))
                    current_pilot = pilot
                    current_fixes = []
                current_fixes.append(Fix(int(epoch), lat, lon, int(baro or 0), int(gps or 0)))
                point_count += 1
                max_epoch = max(max_epoch or int(epoch), int(epoch))
    if current_pilot is not None:
        work.append((current_pilot, current_fixes))
    return work, point_count, max_epoch


def classify(task, task_score, results):
    rows = []
    for rank, result in enumerate(sorted(results, key=lambda r: r.rank_key), 1):
        next_wp = task.waypoints[result.next_wp] if result.next_wp < len(task.waypoints) else None
        distance_to_next = None
        if next_wp and result.last_lat and result.last_lon:
            distance_to_next = haversine(result.last_lat, result.last_lon, next_wp.lat, next_wp.lon)
        progress = None
        if task.total_distance:
            progress = min(100.0, max(0.0, result.distance / task.total_distance * 100.0))
        rows.append({
            "pilot_id": result.pilot,
            "rank": rank,
            "state": result.state,
            "score": result.total_points,
            "distance_m": result.distance,
            "speed_kmh": result.speed,
            "ess": result.ess_time is not None,
            "goal": result.goal_time is not None,
            "position": {
                "lat": result.last_lat,
                "lon": result.last_lon,
                "alt_m": result.last_alt,
                "next_waypoint_index": next_wp.index if next_wp else None,
                "next_waypoint": next_wp.name if next_wp else None,
                "distance_to_next_m": distance_to_next,
                "distance_to_goal_m": max(0.0, task.total_distance - result.distance),
                "progress_percent": progress,
            },
        })
    summary = {
        "launch_validity": task_score.launch_validity,
        "distance_validity": task_score.distance_validity,
        "time_validity": task_score.time_validity,
        "task_validity": task_score.task_validity,
        "pilots_present": task_score.pilots_present,
        "pilots_flying": task_score.pilots_flying,
        "pilots_ess": task_score.pilots_ess,
        "pilots_goal": task_score.pilots_goal,
        "best_distance": task_score.best_distance,
        "best_time": task_score.best_time,
    }
    return summary, rows


def save_success(conn, task_id, competition_id, processed_epoch, point_count, task_score, pilots, timings):
    pilots_json = json.dumps(pilots, separators=(",", ":"))
    with conn.transaction():
        with conn.cursor() as cur:
            cur.execute("""
                INSERT INTO live_api_taskresultsnapshot (
                    task_id, competition_id, computed_at, processed_epoch,
                    point_count, status, task_score, timings, error
                )
                VALUES (%s::uuid, %s::uuid, now(), %s, %s, 'ok', %s::jsonb, %s::jsonb, '')
                ON CONFLICT (task_id) DO UPDATE SET
                    competition_id = EXCLUDED.competition_id,
                    computed_at = now(),
                    processed_epoch = EXCLUDED.processed_epoch,
                    point_count = EXCLUDED.point_count,
                    status = 'ok',
                    task_score = EXCLUDED.task_score,
                    timings = EXCLUDED.timings,
                    error = ''
            """, [
                task_id,
                competition_id,
                processed_epoch,
                point_count,
                json.dumps(task_score, separators=(",", ":")),
                json.dumps(timings, separators=(",", ":")),
            ])
            cur.execute("DELETE FROM live_api_pilotscoresnapshot WHERE task_id = %s::uuid", [task_id])
            if pilots:
                cur.execute("""
                    INSERT INTO live_api_pilotscoresnapshot (
                        task_id, pilot_id, rank, state, score, distance_m,
                        speed_kmh, ess, goal, position, updated_at
                    )
                    SELECT %s::uuid, pilot_id, rank, state, score, distance_m,
                           speed_kmh, ess, goal, position, now()
                    FROM jsonb_to_recordset(%s::jsonb) AS x(
                        pilot_id text,
                        rank integer,
                        state text,
                        score double precision,
                        distance_m double precision,
                        speed_kmh double precision,
                        ess boolean,
                        goal boolean,
                        position jsonb
                    )
                """, [task_id, pilots_json])


def save_error(conn, task_id, competition_id, point_count, processed_epoch, error, timings):
    with conn.transaction():
        with conn.cursor() as cur:
            cur.execute("""
                INSERT INTO live_api_taskresultsnapshot (
                    task_id, competition_id, computed_at, processed_epoch,
                    point_count, status, task_score, timings, error
                )
                VALUES (%s::uuid, %s::uuid, now(), %s, %s, 'error', '{}'::jsonb, %s::jsonb, %s)
                ON CONFLICT (task_id) DO UPDATE SET
                    competition_id = EXCLUDED.competition_id,
                    computed_at = now(),
                    processed_epoch = EXCLUDED.processed_epoch,
                    point_count = EXCLUDED.point_count,
                    status = 'error',
                    task_score = '{}'::jsonb,
                    timings = EXCLUDED.timings,
                    error = EXCLUDED.error
            """, [task_id, competition_id, processed_epoch, point_count, json.dumps(timings), error])


def score_one(conn, record):
    task_id, external_id, task_settings, competition_id, comp_settings, _stored_count, _stored_epoch = record
    started = time.perf_counter()
    timings = {}
    try:
        t0 = time.perf_counter()
        task = compile_task(task_settings)
        params = gap_params(comp_settings)
        present = pilots_present(comp_settings)
        timings["task_compile_ms"] = round((time.perf_counter() - t0) * 1000, 2)

        t0 = time.perf_counter()
        work, point_count, processed_epoch = load_work(conn, task_id)
        timings["db_fetch_build_ms"] = round((time.perf_counter() - t0) * 1000, 2)
        if not work:
            return {"task": external_id or task_id, "status": "empty", "point_count": 0}

        now = max((fixes[-1].t for _, fixes in work if fixes), default=0)
        t0 = time.perf_counter()
        results = []
        for pilot, fixes in work:
            project(task, fixes)
            result = score_pilot(task, fixes, now, params)
            result.pilot = pilot
            results.append(result)
        task_score = score_task(task, results, params, present)
        timings["engine_ms"] = round((time.perf_counter() - t0) * 1000, 2)

        summary, pilots = classify(task, task_score, results)
        timings["total_before_save_ms"] = round((time.perf_counter() - started) * 1000, 2)
        t0 = time.perf_counter()
        save_success(conn, task_id, competition_id, processed_epoch, point_count, summary, pilots, timings)
        timings["save_ms"] = round((time.perf_counter() - t0) * 1000, 2)
        timings["total_ms"] = round((time.perf_counter() - started) * 1000, 2)
        return {
            "task": external_id or task_id,
            "status": "ok",
            "points": point_count,
            "pilots": len(work),
            "processed_epoch": processed_epoch,
            "timings": timings,
        }
    except Exception:
        timings["total_ms"] = round((time.perf_counter() - started) * 1000, 2)
        error = traceback.format_exc(limit=20)
        save_error(conn, task_id, competition_id, _stored_count or 0, _stored_epoch, error, timings)
        return {"task": external_id or task_id, "status": "error", "error": error.splitlines()[-1], "timings": timings}


def run_once(conn, task_filter=None, force=False):
    tasks = scoreable_tasks(conn, task_filter=task_filter, force=force)
    results = [score_one(conn, task) for task in tasks]
    return results


def main():
    parser = argparse.ArgumentParser(description="Score LiveScoring tasks outside the Django request path.")
    parser.add_argument("--task", help="Task UUID or external task_id to score.")
    parser.add_argument("--once", action="store_true", help="Run one scoring pass and exit.")
    parser.add_argument("--force", action="store_true", help="Score even when no new points arrived.")
    parser.add_argument("--interval", type=float, default=1.0, help="Seconds between polling cycles.")
    args = parser.parse_args()

    with db_connect() as conn:
        while True:
            results = run_once(conn, task_filter=args.task, force=args.force)
            for result in results:
                print(json.dumps({
                    "at": datetime.now(timezone.utc).isoformat(),
                    **result,
                }), flush=True)
            if args.once:
                break
            time.sleep(args.interval)


if __name__ == "__main__":
    main()
