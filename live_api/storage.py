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
            -- Dedup within batch: keep latest version of each (pilot_id, epoch)
            SELECT DISTINCT ON (pilot_id, epoch)
                pilot_id, epoch, lat, lon, alt_gps, alt_baro, event_id,
                source, fingerprint, raw
            FROM incoming
            ORDER BY pilot_id, epoch, ord DESC
        ),
        upserted AS (
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
            ON CONFLICT (task_id, pilot_id, timestamp) DO UPDATE SET
                event_id = COALESCE(EXCLUDED.event_id, live_api_trackingpoint.event_id),
                source = COALESCE(EXCLUDED.source, live_api_trackingpoint.source),
                altitude_gps = COALESCE(EXCLUDED.altitude_gps, live_api_trackingpoint.altitude_gps),
                altitude_baro = COALESCE(EXCLUDED.altitude_baro, live_api_trackingpoint.altitude_baro),
                raw = live_api_trackingpoint.raw || EXCLUDED.raw,
                received_at = now()
            RETURNING EXTRACT(EPOCH FROM timestamp)::bigint AS epoch, xmax = 0 AS was_insert
        ),
        upserted_stats AS (
            SELECT
                COUNT(*)::integer AS total_rows,
                SUM(CASE WHEN was_insert THEN 1 ELSE 0 END)::integer AS inserted_new,
                MAX(epoch)::bigint AS latest_epoch
            FROM upserted
        ),
        state_upsert AS (
            INSERT INTO live_api_taskingestionstate (
                task_id, competition_id, latest_epoch, point_count, dirty, updated_at
            )
            SELECT %s::uuid, %s::uuid, latest_epoch, inserted_new, (inserted_new > 0), now()
            FROM upserted_stats
            WHERE inserted_new > 0
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
        SELECT inserted_new, total_rows FROM upserted_stats
    """
    with connection.cursor() as cursor:
        cursor.execute(sql, [
            json.dumps(rows, separators=(",", ":")),
            str(task.competition_id),
            str(task.id),
            str(task.id),
            str(task.competition_id),
        ])
        result = cursor.fetchone()
        inserted = int(result[0]) if result else 0
    return {
        "accepted": inserted,
        "duplicates": received_count - inserted,
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


def _scored_pilot_payload(row):
    """Rebuild a ranking entry from a stored pilot score snapshot.

    ``row`` is (pilot_id, rank, state, score, distance_m, speed_kmh, ess, goal,
    distance_points, time_points, leading_points, lc, position) — the columns
    score_worker writes in ``save_success``.
    """
    (pilot_id, rank, state, score, distance_m, speed_kmh, ess, goal,
     distance_points, time_points, leading_points, lc, position) = row
    return {
        "pilot_id": pilot_id,
        "rank": rank,
        "state": state,
        "score": score,
        "distance_m": distance_m,
        "speed_kmh": speed_kmh,
        "ess": ess,
        "goal": goal,
        # The GAP breakdown behind `score`, as promised by the OpenAPI schema.
        "scoring": {
            "distance_points": distance_points,
            "time_points": time_points,
            "leading_points": leading_points,
            "total_points": score,
            "lc": lc,
        },
        "position": _json_value(position) or {},
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
                       ess, goal, distance_points, time_points, leading_points,
                       lc, position
                FROM live_api_pilotscoresnapshot
                WHERE task_id = %s::uuid
                ORDER BY rank, pilot_id
            """, [str(task.id)])
            pilots = [_scored_pilot_payload(row) for row in cursor.fetchall()]
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
        pilots = [_scored_pilot_payload((
            row.pilot_id, row.rank, row.state, row.score, row.distance_m,
            row.speed_kmh, row.ess, row.goal, row.distance_points,
            row.time_points, row.leading_points, row.lc, row.position,
        )) for row in task.pilot_score_snapshots.order_by("rank", "pilot_id")]
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


# ============================================================================
# ARCHIVAL FUNCTIONS
# ============================================================================

def get_task_tracking_points(task, pilot_id=None, start_time=None, end_time=None):
    """
    Get tracking points from active or archived table.
    Automatically checks active table first, falls back to archive if not found.

    Args:
        task: Task object
        pilot_id: Optional pilot ID filter
        start_time: Optional start datetime filter
        end_time: Optional end datetime filter

    Returns:
        QuerySet of TrackingPoint or TrackingPointArchive
    """
    from .models import TrackingPoint, TrackingPointArchive

    # Try active table first (faster)
    active_points = TrackingPoint.objects.filter(task=task)
    if pilot_id:
        active_points = active_points.filter(pilot_id=pilot_id)
    if start_time:
        active_points = active_points.filter(timestamp__gte=start_time)
    if end_time:
        active_points = active_points.filter(timestamp__lte=end_time)

    if active_points.exists():
        return active_points.order_by('timestamp')

    # Fall back to archive if not in active table
    archive_points = TrackingPointArchive.objects.filter(task=task)
    if pilot_id:
        archive_points = archive_points.filter(pilot_id=pilot_id)
    if start_time:
        archive_points = archive_points.filter(timestamp__gte=start_time)
    if end_time:
        archive_points = archive_points.filter(timestamp__lte=end_time)

    return archive_points.order_by('timestamp')


def archive_task_points(task, source='orm'):
    """
    Archive tracking points from a finished task to the archive table.

    Args:
        task: Task object to archive
        source: 'orm' (Django ORM) or 'sql' (direct SQL, faster for large batches)

    Returns:
        dict with 'archived_count', 'deleted_count', 'status'
    """
    from .models import TrackingPoint, TrackingPointArchive

    if connection.vendor == "postgresql" and source == 'sql':
        return _archive_task_points_postgres(task)

    # ORM fallback
    points = TrackingPoint.objects.filter(task=task)
    point_count = points.count()

    if point_count == 0:
        return {"archived_count": 0, "deleted_count": 0, "status": "no_points"}

    # Convert to archive objects
    archive_points = [
        TrackingPointArchive(
            id=p.id,
            competition_id=p.competition_id,
            task_id=p.task_id,
            pilot_id=p.pilot_id,
            event_id=p.event_id,
            timestamp=p.timestamp,
            latitude=p.latitude,
            longitude=p.longitude,
            altitude_gps=p.altitude_gps,
            altitude_baro=p.altitude_baro,
            source=p.source,
            fingerprint=p.fingerprint,
            raw=p.raw,
            received_at=p.received_at,
        )
        for p in points.iterator(chunk_size=5000)
    ]

    # Bulk insert to archive
    TrackingPointArchive.objects.bulk_create(
        archive_points,
        ignore_conflicts=True,
        batch_size=5000
    )

    # Delete from main table
    points.delete()

    return {
        "archived_count": point_count,
        "deleted_count": point_count,
        "status": "success"
    }


def _archive_task_points_postgres(task):
    """Fast PostgreSQL version using direct SQL for archival."""
    sql = """
        WITH to_archive AS (
            SELECT * FROM live_api_trackingpoint WHERE task_id = %s::uuid
        ),
        inserted AS (
            INSERT INTO live_api_trackingpoint_archive (
                id, competition_id, task_id, pilot_id, event_id, timestamp,
                latitude, longitude, altitude_gps, altitude_baro, source,
                fingerprint, raw, received_at
            )
            SELECT
                id, competition_id, task_id, pilot_id, event_id, timestamp,
                latitude, longitude, altitude_gps, altitude_baro, source,
                fingerprint, raw, received_at
            FROM to_archive
            ON CONFLICT (id) DO NOTHING
            RETURNING id
        ),
        deleted AS (
            DELETE FROM live_api_trackingpoint
            WHERE task_id = %s::uuid
            RETURNING id
        )
        SELECT (SELECT COUNT(*) FROM inserted) as archived, (SELECT COUNT(*) FROM deleted) as deleted
    """

    with connection.cursor() as cursor:
        cursor.execute(sql, [str(task.id), str(task.id)])
        result = cursor.fetchone()
        archived = int(result[0]) if result else 0
        deleted = int(result[1]) if result else 0

    return {
        "archived_count": archived,
        "deleted_count": deleted,
        "status": "success" if deleted > 0 else "no_points"
    }
