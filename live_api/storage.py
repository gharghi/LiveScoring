import json
from datetime import datetime, timezone

from django.db import connection, transaction

from .models import TaskIngestionState, TrackingPoint


def _json_value(value):
    if isinstance(value, str):
        try:
            return json.loads(value)
        except json.JSONDecodeError:
            return value
    return value


def _epoch_datetime(value):
    if isinstance(value, (int, float)):
        return datetime.fromtimestamp(value, tz=timezone.utc)
    return datetime.fromisoformat(str(value).replace("Z", "+00:00"))


def normalize_points(points):
    rows = []
    processed_epochs = []
    for idx, point in enumerate(points):
        try:
            pilot = str(point["pilot_id"])
            epoch_value = point.get("epoch", point.get("timestamp"))
            timestamp = _epoch_datetime(epoch_value).astimezone(timezone.utc)
            epoch = int(timestamp.timestamp())
            lat = float(point["lat"])
            lon = float(point["lon"])
            if not (-90 <= lat <= 90 and -180 <= lon <= 180):
                raise ValueError
        except (KeyError, TypeError, ValueError, OverflowError):
            raise ValueError("each point requires pilot_id, epoch (or timestamp), lat and lon")

        alt_gps = point.get("alt_gps", point.get("alt"))
        alt_baro = point.get("alt_baro")
        fingerprint = TrackingPoint.make_fingerprint(pilot, timestamp, lat, lon)
        rows.append({
            "ord": idx,
            "pilot_id": pilot,
            "epoch": epoch,
            "lat": lat,
            "lon": lon,
            "alt_gps": alt_gps,
            "alt_baro": alt_baro,
            "event_id": str(point.get("event_id", "")),
            "source": str(point.get("source", "volandoo")),
            "fingerprint": fingerprint,
            "raw": point,
        })
        processed_epochs.append(epoch)
    return rows, processed_epochs


def insert_task_points(task, points):
    normalized, processed_epochs = normalize_points(points)
    if connection.vendor == "postgresql":
        return _insert_task_points_postgres(task, normalized, len(points), processed_epochs)
    return _insert_task_points_orm(task, normalized, len(points), processed_epochs)


def _insert_task_points_postgres(task, rows, received_count, processed_epochs):
    if not rows:
        return {"accepted": 0, "duplicates": 0, "processed_epoch": None}

    sql = """
        WITH incoming AS (
            SELECT *
            FROM jsonb_to_recordset(%s::jsonb) AS x(
                ord integer,
                pilot_id text,
                epoch bigint,
                lat double precision,
                lon double precision,
                alt_gps double precision,
                alt_baro double precision,
                event_id text,
                source text,
                fingerprint text,
                raw jsonb
            )
        ),
        batch_latest AS (
            SELECT DISTINCT ON (pilot_id, epoch)
                pilot_id, epoch, lat, lon, alt_gps, alt_baro, event_id,
                source, fingerprint, raw
            FROM incoming
            ORDER BY pilot_id, epoch, ord DESC
        ),
        inserted AS (
            INSERT INTO live_api_trackingpoint (
                competition_id, task_id, pilot_id, event_id, timestamp,
                latitude, longitude, altitude_gps, altitude_baro, source,
                fingerprint, raw, received_at
            )
            SELECT
                %s::uuid, %s::uuid, b.pilot_id, COALESCE(b.event_id, ''),
                to_timestamp(b.epoch), b.lat, b.lon, b.alt_gps, b.alt_baro,
                COALESCE(b.source, ''), b.fingerprint, b.raw, now()
            FROM batch_latest b
            WHERE NOT EXISTS (
                SELECT 1
                FROM live_api_trackingpoint p
                WHERE p.task_id = %s::uuid
                  AND p.pilot_id = b.pilot_id
                  AND p.timestamp = to_timestamp(b.epoch)
            )
            RETURNING EXTRACT(EPOCH FROM timestamp)::bigint AS epoch
        ),
        inserted_stats AS (
            SELECT COUNT(*)::integer AS accepted, MAX(epoch)::bigint AS latest_epoch
            FROM inserted
        ),
        state_upsert AS (
            INSERT INTO live_api_taskingestionstate (
                task_id, competition_id, latest_epoch, point_count, dirty, updated_at
            )
            SELECT %s::uuid, %s::uuid, latest_epoch, accepted, (accepted > 0), now()
            FROM inserted_stats
            WHERE accepted > 0
            ON CONFLICT (task_id) DO UPDATE SET
                competition_id = EXCLUDED.competition_id,
                latest_epoch = GREATEST(
                    COALESCE(live_api_taskingestionstate.latest_epoch, EXCLUDED.latest_epoch),
                    EXCLUDED.latest_epoch
                ),
                point_count = live_api_taskingestionstate.point_count + EXCLUDED.point_count,
                dirty = true,
                updated_at = now()
            RETURNING 1
        )
        SELECT COUNT(*) FROM inserted
    """
    with connection.cursor() as cursor:
        cursor.execute(sql, [
            json.dumps(rows, separators=(",", ":")),
            str(task.competition_id),
            str(task.id),
            str(task.id),
            str(task.id),
            str(task.competition_id),
        ])
        accepted = int(cursor.fetchone()[0])
    return {
        "accepted": accepted,
        "duplicates": received_count - accepted,
        "processed_epoch": max(processed_epochs, default=None),
    }


def _insert_task_points_orm(task, rows, received_count, processed_epochs):
    seen = set()
    parsed = []
    for row in rows:
        key = (row["pilot_id"], row["epoch"])
        if key in seen:
            continue
        seen.add(key)
        parsed.append(row)

    existing = set()
    if parsed:
        pilots = {row["pilot_id"] for row in parsed}
        timestamps = [
            datetime.fromtimestamp(row["epoch"], tz=timezone.utc)
            for row in parsed
        ]
        existing = set(TrackingPoint.objects.filter(
            task=task, pilot_id__in=pilots, timestamp__in=timestamps
        ).values_list("pilot_id", "timestamp"))

    new_rows = []
    for row in parsed:
        timestamp = datetime.fromtimestamp(row["epoch"], tz=timezone.utc)
        if (row["pilot_id"], timestamp) in existing:
            continue
        new_rows.append(TrackingPoint(
            competition=task.competition,
            task=task,
            pilot_id=row["pilot_id"],
            event_id=row["event_id"],
            timestamp=timestamp,
            latitude=row["lat"],
            longitude=row["lon"],
            altitude_gps=row["alt_gps"],
            altitude_baro=row["alt_baro"],
            source=row["source"],
            fingerprint=row["fingerprint"],
            raw=row["raw"],
        ))
    with transaction.atomic():
        if new_rows:
            TrackingPoint.objects.bulk_create(new_rows, batch_size=1000)
            state, _created = TaskIngestionState.objects.get_or_create(
                task=task, defaults={"competition": task.competition}
            )
            state.competition = task.competition
            state.latest_epoch = max(processed_epochs, default=state.latest_epoch)
            state.point_count += len(new_rows)
            state.dirty = True
            state.save(update_fields=["competition", "latest_epoch", "point_count", "dirty", "updated_at"])
    return {
        "accepted": len(new_rows),
        "duplicates": received_count - len(new_rows),
        "processed_epoch": max(processed_epochs, default=None),
    }


def latest_task_classification(task):
    if connection.vendor == "postgresql":
        return _latest_task_classification_postgres(task)
    return _latest_task_classification_orm(task)


def _latest_task_classification_postgres(task):
    with connection.cursor() as cursor:
        cursor.execute("""
            SELECT EXTRACT(EPOCH FROM computed_at)::bigint, processed_epoch,
                   point_count, status, task_score, timings, error
            FROM live_api_taskresultsnapshot
            WHERE task_id = %s::uuid
        """, [str(task.id)])
        snapshot = cursor.fetchone()

        if snapshot:
            computed_at, scored_epoch, point_count, status, task_score, timings, error = snapshot
            cursor.execute("""
                SELECT pilot_id, rank, state, score, distance_m, speed_kmh,
                       ess, goal, position
                FROM live_api_pilotscoresnapshot
                WHERE task_id = %s::uuid
                ORDER BY rank, pilot_id
            """, [str(task.id)])
            pilots = [{
                "pilot_id": row[0],
                "rank": row[1],
                "state": row[2],
                "score": row[3],
                "distance_m": row[4],
                "speed_kmh": row[5],
                "ess": row[6],
                "goal": row[7],
                "position": _json_value(row[8]),
            } for row in cursor.fetchall()]
            return {
                "computed_at_epoch": computed_at,
                "processed_epoch": scored_epoch,
                "point_count": point_count,
                "status": status,
                "task_score": _json_value(task_score),
                "timings": _json_value(timings),
                "error": error,
                "pilots": pilots,
            }

        cursor.execute("""
            WITH latest AS (
                SELECT DISTINCT ON (pilot_id)
                    pilot_id, EXTRACT(EPOCH FROM timestamp)::bigint AS epoch,
                    latitude, longitude, altitude_gps
                FROM live_api_trackingpoint
                WHERE task_id = %s::uuid
                ORDER BY pilot_id, timestamp DESC, id DESC
            ),
            counts AS (
                SELECT pilot_id, COUNT(*) AS point_count
                FROM live_api_trackingpoint
                WHERE task_id = %s::uuid
                GROUP BY pilot_id
            )
            SELECT latest.pilot_id, latest.epoch, latest.latitude,
                   latest.longitude, latest.altitude_gps, counts.point_count
            FROM latest
            JOIN counts ON counts.pilot_id = latest.pilot_id
            ORDER BY latest.pilot_id
        """, [str(task.id), str(task.id)])
        pilots = []
        for rank, row in enumerate(cursor.fetchall(), 1):
            pilot, epoch, lat, lon, alt, point_count = row
            pilots.append({
                "pilot_id": pilot,
                "rank": rank,
                "state": "TRACKING",
                "score": 0,
                "distance_m": 0,
                "speed_kmh": None,
                "ess": False,
                "goal": False,
                "points_received": point_count,
                "last_epoch": epoch,
                "position": {
                    "lat": lat,
                    "lon": lon,
                    "alt_m": alt,
                    "next_waypoint_index": None,
                    "next_waypoint": None,
                    "distance_to_next_m": None,
                    "distance_to_goal_m": None,
                    "progress_percent": None,
                },
            })
        return {
            "computed_at_epoch": None,
            "processed_epoch": None,
            "point_count": sum(p["points_received"] for p in pilots),
            "status": "pending",
            "task_score": None,
            "timings": None,
            "error": None,
            "pilots": pilots,
        }


def _latest_task_classification_orm(task):
    snapshot = getattr(task, "score_snapshot", None)
    if snapshot:
        pilots = [{
            "pilot_id": row.pilot_id,
            "rank": row.rank,
            "state": row.state,
            "score": row.score,
            "distance_m": row.distance_m,
            "speed_kmh": row.speed_kmh,
            "ess": row.ess,
            "goal": row.goal,
            "position": row.position,
        } for row in task.pilot_score_snapshots.order_by("rank", "pilot_id")]
        return {
            "computed_at_epoch": int(snapshot.computed_at.timestamp()),
            "processed_epoch": snapshot.processed_epoch,
            "point_count": snapshot.point_count,
            "status": snapshot.status,
            "task_score": snapshot.task_score,
            "timings": snapshot.timings,
            "error": snapshot.error,
            "pilots": pilots,
        }

    latest = {}
    counts = {}
    for row in task.tracking_points.order_by("pilot_id", "-timestamp", "-id"):
        counts[row.pilot_id] = counts.get(row.pilot_id, 0) + 1
        latest.setdefault(row.pilot_id, row)
    pilots = []
    for rank, (pilot, row) in enumerate(sorted(latest.items()), 1):
        pilots.append({
            "pilot_id": pilot,
            "rank": rank,
            "state": "TRACKING",
            "score": 0,
            "distance_m": 0,
            "speed_kmh": None,
            "ess": False,
            "goal": False,
            "points_received": counts[pilot],
            "last_epoch": int(row.timestamp.timestamp()),
            "position": {
                "lat": row.latitude,
                "lon": row.longitude,
                "alt_m": row.altitude_gps,
                "next_waypoint_index": None,
                "next_waypoint": None,
                "distance_to_next_m": None,
                "distance_to_goal_m": None,
                "progress_percent": None,
            },
        })
    return {
        "computed_at_epoch": None,
        "processed_epoch": None,
        "point_count": sum(counts.values()),
        "status": "pending",
        "task_score": None,
        "timings": None,
        "error": None,
        "pilots": pilots,
    }
