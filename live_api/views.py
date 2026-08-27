import json
import multiprocessing as mp
import os
import secrets
import tempfile
import time
from datetime import datetime, timezone

from django.db import transaction
from django.http import JsonResponse
from django.shortcuts import render
from django.views.decorators.csrf import csrf_exempt

from .models import ApiApplication, Competition, Task, TrackingPoint


# Set only in short-lived scoring workers.  Pilot scoring is pure and does not
# touch Django or PostgreSQL, so forked workers can safely share the compiled
# task and parameters copy-on-write.
_SCORE_TASK = None
_SCORE_PARAMS = None
_SCORE_NOW = 0.0


def _init_score_worker(task, params, now):
    global _SCORE_TASK, _SCORE_PARAMS, _SCORE_NOW
    _SCORE_TASK, _SCORE_PARAMS, _SCORE_NOW = task, params, now


def _score_pilot_worker(item):
    pilot, fixes = item
    from engine.score import project, score_pilot
    project(_SCORE_TASK, fixes)
    result = score_pilot(_SCORE_TASK, fixes, _SCORE_NOW, _SCORE_PARAMS)
    result.pilot = pilot
    return result


def score_competition(comp, task_row=None):
    """Replay the stored points through the existing deterministic scorer.

    A task is scoreable when its settings include the original ``xctsk`` JSON
    and ``date_epoch``. Duplicate timestamps are retained in PostgreSQL but
    collapsed to the latest received fix for deterministic replay.
    """
    task_row = task_row or comp.tasks.order_by("-version").first()
    if not task_row or not isinstance(task_row.settings.get("xctsk"), dict):
        return None
    from engine.igc import Fix
    from engine.score import project, score_pilot
    from engine.scoring import score_task
    from engine.rules.params import GapParams
    from engine.task import parse_xctsk
    with tempfile.NamedTemporaryFile(mode="w", suffix=".xctsk") as fh:
        json.dump(task_row.settings["xctsk"], fh)
        fh.flush()
        task = parse_xctsk(fh.name, int(task_row.settings.get("date_epoch", 0)))
    cfg = comp.settings
    params = GapParams(
        nominal_distance=float(cfg.get("nominal_distance_km", 60)) * 1000,
        minimum_distance=float(cfg.get("minimum_distance_km", 5)) * 1000,
        nominal_time=float(cfg.get("nominal_time_min", 90)) * 60,
        leading_time_ratio=float(cfg.get("leading_time_ratio", 0.26)),
        ess_no_goal_time_factor=float(cfg.get("ess_no_goal_time_factor", 0.0)),
    )
    from django.db import connection

    work = []

    # Fetch and build the scorer input in one pass.  Going through Django model
    # rows or even ORM values_list() is several seconds slower on large replay
    # tasks because every timestamp becomes a Python datetime before we turn it
    # straight back into an epoch.  The ORDER BY keeps duplicate timestamps
    # adjacent; the pending-fix logic preserves the previous "latest row wins"
    # behavior without building a per-pilot dict and sorting it again.
    sql = """
        SELECT pilot_id, EXTRACT(EPOCH FROM timestamp)::bigint, latitude,
               longitude, COALESCE(altitude_baro, 0)::int,
               COALESCE(altitude_gps, 0)::int
        FROM live_api_trackingpoint
        WHERE task_id = %s
        ORDER BY pilot_id, timestamp, id
    """
    current_pilot = None
    current_fixes = None
    pending_epoch = None
    pending_fix = None
    with connection.cursor() as cursor:
        cursor.execute(sql, [str(task_row.id)])
        while True:
            rows = cursor.fetchmany(20000)
            if not rows:
                break
            for pilot, epoch, lat, lon, baro, gps in rows:
                if pilot != current_pilot:
                    if pending_fix is not None:
                        current_fixes.append(pending_fix)
                    if current_pilot is not None:
                        work.append((current_pilot, current_fixes))
                    current_pilot = pilot
                    current_fixes = []
                    pending_epoch = None
                    pending_fix = None

                fix = Fix(int(epoch), lat, lon, int(baro or 0), int(gps or 0))
                if pending_epoch is None:
                    pending_epoch = fix.t
                    pending_fix = fix
                elif fix.t == pending_epoch:
                    pending_fix = fix
                else:
                    current_fixes.append(pending_fix)
                    pending_epoch = fix.t
                    pending_fix = fix

    if pending_fix is not None:
        current_fixes.append(pending_fix)
    if current_pilot is not None:
        work.append((current_pilot, current_fixes))
    if not work:
        return None

    # score_pilot is CPU-bound Python, so threads cannot use the extra cores.
    # Use a small fork pool per request.  The default is two workers because
    # production runs two Gunicorn workers (2 x 2 = 4 cores); set
    # LIVE_SCORING_SCORER_WORKERS=1 to disable parallel scoring safely.
    scorer_workers = max(1, int(os.environ.get("LIVE_SCORING_SCORER_WORKERS", "1")))
    now = max((fixes[-1].t for _, fixes in work if fixes), default=0)
    if scorer_workers > 1 and len(work) > 1:
        scorer_workers = min(scorer_workers, len(work))
        try:
            context = mp.get_context("fork")
        except ValueError:
            context = mp.get_context()
        with context.Pool(scorer_workers, initializer=_init_score_worker,
                          initargs=(task, params, now)) as pool:
            results = pool.map(_score_pilot_worker, work)
    else:
        results = []
        for pilot, fixes in work:
            project(task, fixes)
            result = score_pilot(task, fixes, now, params)
            result.pilot = pilot
            results.append(result)
    task_score = score_task(task, results, params, cfg.get("pilots_present"))
    return task, task_score, results


def body(request):
    try:
        return json.loads(request.body or b"{}")
    except json.JSONDecodeError:
        return None


def auth(request):
    raw = request.headers.get("X-API-Key", "") or request.headers.get("Authorization", "").removeprefix("Bearer ")
    if not raw:
        return None
    return ApiApplication.objects.filter(key_hash=ApiApplication.digest(raw), active=True).first()


def error(message, status=400):
    return JsonResponse({"error": message}, status=status)


def health(request):
    return JsonResponse({"ok": True, "service": "livescoring-django"})


def openapi(request):
    schemas = {
        "Error": {"type": "object", "properties": {"error": {"type": "string"}}},
        "Pilot": {"type": "object", "required": ["pilot_id"], "properties": {
            "pilot_id": {"type": "string"}, "name": {"type": "string"},
            "category_id": {"type": "string"}, "tracker_id": {"type": "string"},
            "competition_number": {"type": "string"}, "country": {"type": "string"},
            "glider": {"type": "string"}, "metadata": {"type": "object"}}},
        "TrackingPoint": {"type": "object", "required": ["pilot_id", "epoch", "lat", "lon", "alt"], "properties": {
            "pilot_id": {"type": "string"}, "epoch": {"type": "integer", "format": "int64", "description": "Unix epoch seconds; sender signature and GPS fix time."}, "timestamp": {"type": "integer", "format": "int64", "description": "Backward-compatible alias for epoch."},
            "lat": {"type": "number", "minimum": -90, "maximum": 90}, "lon": {"type": "number", "minimum": -180, "maximum": 180}, "alt": {"type": "number"}, "event_id": {"type": "string"}}},
        "Classification": {"type": "object", "properties": {"computed_at_epoch": {"type": "integer", "format": "int64"}, "ranking": {"type": "array", "items": {"type": "object", "properties": {"pilot_id": {"type": "string"}, "category_id": {"type": "string"}, "rank": {"type": "integer"}, "state": {"type": "string"}, "score": {"type": "number"}, "distance_m": {"type": "number"}, "speed_kmh": {"type": "number", "nullable": True}, "ess": {"type": "boolean"}, "goal": {"type": "boolean"}, "position": {"type": "object", "properties": {"lat": {"type": "number"}, "lon": {"type": "number"}, "alt_m": {"type": "number", "nullable": True}, "next_waypoint_index": {"type": "integer", "nullable": True}, "next_waypoint": {"type": "string", "nullable": True}, "distance_to_next_m": {"type": "number", "nullable": True}, "distance_to_goal_m": {"type": "number", "nullable": True}, "progress_percent": {"type": "number", "nullable": True}}}}}}}},
    }
    event_body = {"type": "object", "required": ["schema_version", "event_id", "event_name", "sent_at", "formula", "categories", "pilots"], "properties": {
        "schema_version": {"type": "string"}, "event_id": {"type": "string"}, "event_name": {"type": "string"}, "sent_at": {"type": "string", "format": "date-time"},
        "formula": {"type": "object", "properties": {"type": {"type": "string", "example": "GAP"}, "parameters": {"type": "object", "additionalProperties": True}}},
        "categories": {"type": "array", "items": {"type": "object", "required": ["category_id", "name"], "properties": {"category_id": {"type": "string"}, "name": {"type": "string"}, "formula_override": {"type": "object", "nullable": True}}}},
        "pilots": {"type": "array", "items": {"$ref": "#/components/schemas/Pilot"}}}}
    manga_body = {"type": "object", "required": ["schema_version", "event_id", "manga_id", "manga_date", "scheduled_start_time", "status", "pilots", "sent_at"], "properties": {
        "schema_version": {"type": "string"}, "event_id": {"type": "string"}, "manga_id": {"type": "string"}, "manga_date": {"type": "string", "format": "date"}, "scheduled_start_time": {"type": "string", "format": "date-time"}, "status": {"type": "string"}, "pilots": {"type": "array", "items": {"type": "string"}}, "sent_at": {"type": "string", "format": "date-time"}}}
    task_body = {"type": "object", "required": ["schema_version", "event_id", "task_id", "task_date", "scheduled_start_time", "status", "pilots", "sent_at"], "properties": {
        "schema_version": {"type": "string"}, "event_id": {"type": "string"}, "task_id": {"type": "string"}, "task_date": {"type": "string", "format": "date"}, "scheduled_start_time": {"type": "string", "format": "date-time"}, "status": {"type": "string"}, "pilots": {"type": "array", "items": {"type": "string"}}, "sent_at": {"type": "string", "format": "date-time"}, "date_epoch": {"type": "integer", "format": "int64"}, "xctsk": {"type": "object"}}}
    points_body = {"type": "object", "required": ["schema_version", "event_id", "manga_id", "cutoff_epoch", "points"], "properties": {
        "schema_version": {"type": "string"}, "event_id": {"type": "string"}, "manga_id": {"type": "string"}, "cutoff_epoch": {"type": "integer", "format": "int64"}, "points": {"type": "array", "maxItems": 5000, "items": {"$ref": "#/components/schemas/TrackingPoint"}}}}
    paths = {
        "/events/sync": {"post": {"tags": ["Event sync"], "security": [{"ApiKeyAuth": []}], "summary": "Upsert event, formula, categories and pilot roster", "requestBody": {"required": True, "content": {"application/json": {"schema": event_body}}}, "responses": {"200": {"description": "{event_id,status,errors}"}, "400": {"description": "Validation errors"}}}},
        "/events/{event_id}/mangas/sync": {"post": {"tags": ["Manga sync"], "security": [{"ApiKeyAuth": []}], "summary": "Upsert a stable manga/day configuration", "parameters": [{"in": "path", "name": "event_id", "required": True, "schema": {"type": "string"}}], "requestBody": {"required": True, "content": {"application/json": {"schema": manga_body}}}, "responses": {"200": {"description": "{event_id,manga_id,status,errors}"}, "400": {"description": "Validation errors"}, "404": {"description": "Unknown event"}}}},
        "/mangas/{manga_id}/points": {"post": {"tags": ["Live tracking"], "security": [{"ApiKeyAuth": []}], "summary": "Push points and receive classification (legacy manga name)", "description": "Send every ~15 seconds. Deduplication key is (pilot_id,epoch). Late, out-of-order, duplicate and missing points are accepted. cutoff_epoch is a cache watermark and is echoed as received_cutoff_epoch. processed_epoch confirms the highest epoch successfully handled.", "parameters": [{"in": "path", "name": "manga_id", "required": True, "schema": {"type": "string"}}], "requestBody": {"required": True, "content": {"application/json": {"schema": points_body}}}, "responses": {"200": {"description": "Ack and classification"}, "400": {"description": "Invalid points"}, "404": {"description": "Unknown task"}}}},
        "/events/{event_id}/tasks/sync": {"post": {"tags": ["Task sync"], "security": [{"ApiKeyAuth": []}], "summary": "Upsert a stable task/day configuration", "parameters": [{"in": "path", "name": "event_id", "required": True, "schema": {"type": "string"}}], "requestBody": {"required": True, "content": {"application/json": {"schema": task_body}}}, "responses": {"200": {"description": "Task accepted"}, "400": {"description": "Validation errors"}, "404": {"description": "Unknown event"}}}},
        "/tasks/{task_id}/points": {"post": {"tags": ["Live tracking"], "security": [{"ApiKeyAuth": []}], "summary": "Push task tracking points and calculate immediately", "description": "Each point must include epoch (Unix seconds). The response returns processed_epoch, the highest point epoch successfully accepted or recognized as a duplicate.", "parameters": [{"in": "path", "name": "task_id", "required": True, "schema": {"type": "string"}}], "requestBody": {"required": True, "content": {"application/json": {"schema": points_body}}}, "responses": {"200": {"description": "Ingestion acknowledgement and latest classification, including received_cutoff_epoch and processed_epoch"}, "404": {"description": "Unknown task"}}}},
        "/tasks/{task_id}/results": {"get": {"tags": ["Results"], "security": [{"ApiKeyAuth": []}], "summary": "Get the latest calculated task results", "parameters": [{"in": "path", "name": "task_id", "required": True, "schema": {"type": "string"}}], "responses": {"200": {"description": "Current ranking and per-pilot scoring fields", "content": {"application/json": {"schema": {"$ref": "#/components/schemas/Classification"}}}}, "404": {"description": "Unknown task"}}}},
        "/events/{event_id}/results": {"get": {"tags": ["Results"], "security": [{"ApiKeyAuth": []}], "summary": "Get the latest task results for an event", "parameters": [{"in": "path", "name": "event_id", "required": True, "schema": {"type": "string"}}], "responses": {"200": {"description": "Current ranking"}, "404": {"description": "Unknown event"}}}},
    }
    return JsonResponse({"openapi": "3.0.3", "info": {"title": "LiveScoring Integration API", "version": "3.0.0", "description": "Volandoo event/manga synchronization and live tracking contract."}, "servers": [{"url": "https://ls.buildmycabin.com"}], "components": {"securitySchemes": {"ApiKeyAuth": {"type": "apiKey", "in": "header", "name": "X-API-Key"}}, "schemas": schemas}, "paths": paths})


def swagger_docs(request):
    return render(request, "swagger-ui.html")


def _integration_auth(request):
    app = auth(request)
    if not app:
        return None, error("valid X-API-Key or Bearer token required", 401)
    return app, None


@csrf_exempt
def event_sync(request):
    """Upsert the stable Volandoo event configuration."""
    if request.method != "POST":
        return error("method not allowed", 405)
    app, response = _integration_auth(request)
    if response:
        return response
    data = body(request)
    if not data or not data.get("event_id") or not data.get("event_name"):
        return JsonResponse({"event_id": data.get("event_id") if data else None, "status": "error",
                             "errors": [{"field": "event_id/event_name", "message": "Both fields are required"}]}, status=400)
    categories = data.get("categories", [])
    category_ids = {c.get("category_id") for c in categories}
    errors = []
    for i, pilot in enumerate(data.get("pilots", [])):
        if not pilot.get("pilot_id"):
            errors.append({"field": f"pilots[{i}].pilot_id", "message": "pilot_id is required"})
        if pilot.get("category_id") and pilot["category_id"] not in category_ids:
            errors.append({"field": f"pilots[{i}].category_id", "message": f"Unknown category_id '{pilot['category_id']}'"})
    if errors:
        return JsonResponse({"event_id": data["event_id"], "status": "error", "errors": errors}, status=400)
    formula = data.get("formula", {})
    settings = {"formula": formula, "categories": categories, "pilots": data.get("pilots", []),
                "schema_version": data.get("schema_version", "1.0"), "sent_at": data.get("sent_at")}
    comp, created = Competition.objects.update_or_create(
        external_event_id=str(data["event_id"]), defaults={"owner": app, "name": data["event_name"],
        "settings": settings, "status": "open"})
    return JsonResponse({"event_id": str(data["event_id"]), "status": "ok", "errors": [],
                         "competition_id": str(comp.id), "created": created})


@csrf_exempt
def manga_sync(request, event_id):
    if request.method != "POST":
        return error("method not allowed", 405)
    app, response = _integration_auth(request)
    if response:
        return response
    data = body(request) or {}
    try:
        comp = Competition.objects.get(external_event_id=event_id, owner=app)
    except Competition.DoesNotExist:
        return JsonResponse({"event_id": event_id, "status": "error", "errors": [{"field": "event_id", "message": "Unknown event_id"}]}, status=404)
    task_id = data.get("task_id", data.get("manga_id"))
    task_date = data.get("task_date", data.get("manga_date"))
    required = [("task_id", task_id), ("task_date", task_date), ("scheduled_start_time", data.get("scheduled_start_time")), ("status", data.get("status"))]
    errors = [{"field": f, "message": "This field is required"} for f, value in required if not value]
    if errors:
        return JsonResponse({"event_id": event_id, "status": "error", "errors": errors}, status=400)
    settings = {"task_date": task_date, "manga_date": task_date, "scheduled_start_time": data["scheduled_start_time"],
                "status": data["status"], "pilots": data.get("pilots", []), "sent_at": data.get("sent_at"),
                "schema_version": data.get("schema_version", "1.0")}
    # Accept the English task payload as well as the original manga wrapper.
    task_config = data.get("task", data.get("settings", {}))
    if isinstance(task_config, dict):
        settings.update(task_config)
    if data.get("xctsk") is not None:
        settings["xctsk"] = data["xctsk"]
    if data.get("date_epoch") is not None:
        settings["date_epoch"] = data["date_epoch"]
    task, created = Task.objects.update_or_create(external_manga_id=str(task_id), defaults={
        "competition": comp, "name": str(task_id), "settings": settings})
    if not created:
        task.version = task.version + 1
        task.save(update_fields=["competition", "settings", "version"])
    return JsonResponse({"event_id": event_id, "task_id": task_id, "manga_id": task_id, "status": "ok", "errors": [], "created": created})


def _epoch_datetime(value):
    if isinstance(value, (int, float)):
        return datetime.fromtimestamp(value, tz=timezone.utc)
    return datetime.fromisoformat(str(value).replace("Z", "+00:00"))


@csrf_exempt
def manga_points(request, manga_id):
    if request.method != "POST":
        return error("method not allowed", 405)
    app, response = _integration_auth(request)
    if response:
        return response
    started_at = time.perf_counter()
    try:
        task = Task.objects.select_related("competition").get(external_manga_id=manga_id, competition__owner=app)
    except Task.DoesNotExist:
        return error("task not found", 404)
    data = body(request) or {}
    points = data.get("points", [])
    if not isinstance(points, list) or len(points) > 5000:
        return error("points must be a list of at most 5000 items")
    if data.get("event_id") and data["event_id"] != task.competition.external_event_id:
        return error("event_id does not match manga", 400)
    # Parse the request once, then do one duplicate lookup and one bulk insert.
    # The previous per-point exists()+create() loop made a 1,000-point batch
    # perform roughly 2,000 database round trips before scoring even started.
    parsed = []
    keys = set()
    processed_epochs = []
    for point in points:
        try:
            pilot = str(point["pilot_id"])
            epoch = point.get("epoch", point.get("timestamp"))
            timestamp = _epoch_datetime(epoch)
            lat, lon = float(point["lat"]), float(point["lon"])
            if not (-90 <= lat <= 90 and -180 <= lon <= 180):
                raise ValueError
        except (KeyError, TypeError, ValueError, OverflowError):
            return error("each point requires pilot_id, epoch (or timestamp), lat and lon")
        timestamp = timestamp.astimezone(timezone.utc)
        key = (pilot, timestamp)
        processed_epochs.append(int(timestamp.timestamp()))
        if key in keys:
            continue
        keys.add(key)
        parsed.append((pilot, timestamp, lat, lon, point))

    existing = set()
    if parsed:
        pilots = {p[0] for p in parsed}
        timestamps = {p[1] for p in parsed}
        existing = set(TrackingPoint.objects.filter(
            task=task, pilot_id__in=pilots, timestamp__in=timestamps
        ).values_list("pilot_id", "timestamp"))
    new_rows = []
    for pilot, timestamp, lat, lon, point in parsed:
        if (pilot, timestamp) in existing:
            continue
        fp = TrackingPoint.make_fingerprint(pilot, timestamp, lat, lon)
        new_rows.append(TrackingPoint(
            competition=task.competition, task=task, pilot_id=pilot,
            event_id=str(point.get("event_id", "")), timestamp=timestamp,
            latitude=lat, longitude=lon, altitude_gps=point.get("alt"),
            source="volandoo", fingerprint=fp, raw=point))
    with transaction.atomic():
        if new_rows:
            TrackingPoint.objects.bulk_create(new_rows, batch_size=1000)
    accepted = len(new_rows)
    duplicates = len(points) - accepted
    ingestion_ms = (time.perf_counter() - started_at) * 1000
    scoring_started = time.perf_counter()
    try:
        classification = _live_classification(task.competition, task)
    except Exception as exc:
        classification = {"task_score": None, "pilots": [], "scoring_error": str(exc)}
    cutoff = data.get("cutoff_epoch")
    processed_epoch = max(processed_epochs, default=None)
    return JsonResponse({"task_id": manga_id, "manga_id": manga_id, "received_cutoff_epoch": cutoff,
        "processed_epoch": processed_epoch, "status": "ok",
        "accepted": accepted, "duplicates": duplicates,
        "ingestion_ms": round(ingestion_ms, 2),
        "scoring_ms": round((time.perf_counter() - scoring_started) * 1000, 2),
        "processing_ms": round((time.perf_counter() - started_at) * 1000, 2),
        "classification": {"computed_at_epoch": cutoff or int(datetime.now(timezone.utc).timestamp()),
                            "ranking": classification["pilots"], "task_score": classification["task_score"]}})


# English terminology for new integrations. The manga routes remain available
# as compatibility aliases for already deployed Volandoo clients.
#
# csrf_exempt is required on the ALIASES too, not just on the targets: Django
# applies CSRF at the view the URLconf resolves to, so without it these two
# returned 403 to every non-browser client while /mangas/... worked, which
# reads exactly like an auth failure and is not one.
@csrf_exempt
def task_sync(request, event_id):
    return manga_sync(request, event_id)


@csrf_exempt
def task_points(request, task_id):
    return manga_points(request, manga_id=task_id)


@csrf_exempt
def task_results(request, task_id):
    if request.method != "GET":
        return error("method not allowed", 405)
    app, response = _integration_auth(request)
    if response:
        return response
    try:
        task = Task.objects.select_related("competition").get(external_manga_id=task_id, competition__owner=app)
    except Task.DoesNotExist:
        return error("task not found", 404)
    classification = _live_classification(task.competition, task)
    return JsonResponse({"task_id": task_id, "event_id": task.competition.external_event_id,
        "computed_at_epoch": int(datetime.now(timezone.utc).timestamp()), **classification})


@csrf_exempt
def event_results(request, event_id):
    if request.method != "GET":
        return error("method not allowed", 405)
    app, response = _integration_auth(request)
    if response:
        return response
    try:
        comp = Competition.objects.get(external_event_id=event_id, owner=app)
    except Competition.DoesNotExist:
        return error("event not found", 404)
    task = comp.tasks.order_by("-version").first()
    classification = _live_classification(comp, task)
    return JsonResponse({"event_id": event_id, "task_id": task.external_manga_id if task else None,
        "computed_at_epoch": int(datetime.now(timezone.utc).timestamp()), **classification})


def _live_classification(comp, task=None):
    scored = score_competition(comp, task)
    if scored:
        task_obj, task_score, pilot_results = scored
        pilots = []
        for rank, result in enumerate(sorted(pilot_results, key=lambda r: r.rank_key), 1):
            next_wp = task_obj.waypoints[result.next_wp] if result.next_wp < len(task_obj.waypoints) else None
            distance_to_next = None
            if next_wp and result.last_lat and result.last_lon:
                from engine.geo import haversine
                distance_to_next = haversine(result.last_lat, result.last_lon, next_wp.lat, next_wp.lon)
            progress = None
            if task_obj.total_distance:
                progress = min(100.0, max(0.0, result.distance / task_obj.total_distance * 100.0))
            pilots.append({"pilot_id": result.pilot, "rank": rank, "state": result.state,
                "score": result.total_points, "distance_m": result.distance,
                "speed_kmh": result.speed, "goal": result.goal_time is not None,
                "ess": result.ess_time is not None,
                "position": {"lat": result.last_lat, "lon": result.last_lon, "alt_m": result.last_alt,
                    "next_waypoint_index": next_wp.index if next_wp else None,
                    "next_waypoint": next_wp.name if next_wp else None,
                    "distance_to_next_m": distance_to_next,
                    "distance_to_goal_m": max(0.0, task_obj.total_distance - result.distance),
                    "progress_percent": progress}})
        return {"task_score": {"launch_validity": task_score.launch_validity,
            "distance_validity": task_score.distance_validity, "time_validity": task_score.time_validity,
            "task_validity": task_score.task_validity}, "pilots": pilots}
    latest = {}
    rows = comp.tracking_points.filter(task=task) if task else comp.tracking_points.none()
    for row in rows.order_by("pilot_id", "-timestamp"):
        latest.setdefault(row.pilot_id, row)
    return {"task_score": None, "pilots": [{"pilot_id": p, "rank": i, "state": "TRACKING",
        "score": 0, "distance_m": 0, "position": {"lat": row.latitude, "lon": row.longitude,
        "alt_m": row.altitude_gps, "next_waypoint": None, "distance_to_next_m": None,
        "distance_to_goal_m": None, "progress_percent": None}}
        for i, (p, row) in enumerate(sorted(latest.items()), 1)]}


@csrf_exempt
def api_keys(request):
    if request.method != "POST": return error("method not allowed", 405)
    admin_key = os.environ.get("LS_ADMIN_KEY")
    if admin_key and request.headers.get("X-Admin-Key") != admin_key: return error("admin key required", 401)
    data = body(request)
    if not data or not data.get("name"): return error("name is required")
    plain = "ls_" + secrets.token_urlsafe(32)
    app = ApiApplication.objects.create(name=str(data["name"]), key_prefix=plain[:12], key_hash=ApiApplication.digest(plain))
    return JsonResponse({"id": str(app.id), "name": app.name, "api_key": plain,
                         "warning": "Store this key now; it is never returned again."}, status=201)


@csrf_exempt
def competitions(request):
    if request.method != "POST": return error("method not allowed", 405)
    app = auth(request)
    if not app: return error("valid X-API-Key or Bearer token required", 401)
    data = body(request)
    if not data or not data.get("name"): return error("name is required")
    comp = Competition.objects.create(owner=app, name=data["name"], settings=data.get("settings", {}), status=data.get("status", "draft"))
    return JsonResponse({"id": str(comp.id), "name": comp.name, "status": comp.status}, status=201)


def owned(request, competition_id):
    app = auth(request)
    if not app: return None, error("valid X-API-Key or Bearer token required", 401)
    try: comp = Competition.objects.get(id=competition_id, owner=app)
    except (Competition.DoesNotExist, ValueError): return None, error("competition not found", 404)
    return comp, None


def competition_detail(request, competition_id):
    comp, response = owned(request, competition_id)
    if response: return response
    return JsonResponse({"id": str(comp.id), "name": comp.name, "status": comp.status, "settings": comp.settings,
        "tasks": [{"id": str(t.id), "name": t.name, "version": t.version} for t in comp.tasks.all()]})


@csrf_exempt
def tasks(request, competition_id):
    if request.method != "POST": return error("method not allowed", 405)
    comp, response = owned(request, competition_id)
    if response: return response
    data = body(request)
    if not data or not data.get("name"): return error("name is required")
    previous = comp.tasks.order_by("-version").first()
    task = Task.objects.create(competition=comp, name=data["name"], version=(previous.version + 1 if previous else 1), settings=data.get("settings", {}))
    return JsonResponse({"id": str(task.id), "competition_id": str(comp.id), "name": task.name, "version": task.version}, status=201)


@csrf_exempt
def tracking(request, competition_id):
    if request.method != "POST": return error("method not allowed", 405)
    comp, response = owned(request, competition_id)
    if response: return response
    data = body(request)
    if not data: return error("JSON body required")
    points = data.get("points", [data]) if isinstance(data, dict) else data
    if not isinstance(points, list) or len(points) > 5000: return error("points must be a list of at most 5000 items")
    task = None
    requested_task = data.get("task_id") if isinstance(data, dict) else None
    if requested_task:
        task = comp.tasks.filter(external_manga_id=str(requested_task)).first()
        if task is None:
            try: task = comp.tasks.get(id=requested_task)
            except (Task.DoesNotExist, ValueError): return error("task not found", 404)
    accepted = duplicates = 0
    with transaction.atomic():
        for point in points:
            try:
                pilot = str(point["pilot_id"])
                timestamp = datetime.fromisoformat(str(point["timestamp"]).replace("Z", "+00:00"))
                if timestamp.tzinfo is None: timestamp = timestamp.replace(tzinfo=timezone.utc)
                lat, lon = float(point["lat"]), float(point["lon"])
                if not (-90 <= lat <= 90 and -180 <= lon <= 180): raise ValueError
            except (KeyError, TypeError, ValueError): return error("each point requires pilot_id, ISO timestamp, lat and lon")
            fp = TrackingPoint.make_fingerprint(pilot, timestamp, lat, lon)
            if TrackingPoint.objects.filter(competition=comp, fingerprint=fp).exists(): duplicates += 1
            TrackingPoint.objects.create(competition=comp, task=task, pilot_id=pilot, event_id=str(point.get("event_id", "")), timestamp=timestamp,
                latitude=lat, longitude=lon, altitude_gps=point.get("alt_gps"), altitude_baro=point.get("alt_baro"),
                source=str(point.get("source", "")), fingerprint=fp, raw=point)
            accepted += 1
    return JsonResponse({"accepted": accepted, "duplicates": duplicates, "missing_data_accepted": True,
                         "competition_id": str(comp.id)}, status=202)


def results(request, competition_id):
    comp, response = owned(request, competition_id)
    if response: return response
    rows = comp.tracking_points.order_by("pilot_id", "-timestamp")
    latest, counts = {}, {}
    for row in rows:
        counts[row.pilot_id] = counts.get(row.pilot_id, 0) + 1
        latest.setdefault(row.pilot_id, row)
    scored = score_competition(comp)
    score_by_pilot = {}
    task_score = None
    if scored:
        _task, task_score, scored_results = scored
        score_by_pilot = {r.pilot: r for r in scored_results}
    return JsonResponse({"competition_id": str(comp.id), "status": comp.status,
        "task_score": ({"launch_validity": task_score.launch_validity, "distance_validity": task_score.distance_validity,
                         "time_validity": task_score.time_validity, "task_validity": task_score.task_validity} if task_score else None), "pilots": [
        {"pilot_id": pilot, "points_received": counts[pilot], "last_timestamp": latest[pilot].timestamp.isoformat(),
         "lat": latest[pilot].latitude, "lon": latest[pilot].longitude, "alt_gps": latest[pilot].altitude_gps}
        | ({"state": score_by_pilot[pilot].state, "distance_m": score_by_pilot[pilot].distance,
            "total_points": score_by_pilot[pilot].total_points} if pilot in score_by_pilot else {})
        for pilot in sorted(latest)
    ]})
