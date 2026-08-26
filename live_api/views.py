import json
import os
import secrets
import tempfile
from datetime import datetime, timezone

from django.db import transaction
from django.http import JsonResponse
from django.shortcuts import render
from django.views.decorators.csrf import csrf_exempt

from .models import ApiApplication, Competition, Task, TrackingPoint


def score_competition(comp):
    """Replay the stored points through the existing deterministic scorer.

    A task is scoreable when its settings include the original ``xctsk`` JSON
    and ``date_epoch``. Duplicate timestamps are retained in PostgreSQL but
    collapsed to the latest received fix for deterministic replay.
    """
    task_row = comp.tasks.order_by("-version").first()
    if not task_row or not isinstance(task_row.settings.get("xctsk"), dict):
        return None
    from engine.igc import Fix
    from engine.score import project, score_pilot
    from engine.scoring import score_task
    from engine.rules.params import GapParams
    from engine.task import parse_xctsk
    points = list(comp.tracking_points.order_by("pilot_id", "timestamp", "id"))
    if not points:
        return None
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
    by_pilot = {}
    for p in points:
        by_pilot.setdefault(p.pilot_id, {})[p.timestamp] = p
    results = []
    for pilot, rows in by_pilot.items():
        fixes = [Fix(int(p.timestamp.timestamp()), p.latitude, p.longitude,
                     int(p.altitude_baro or 0), int(p.altitude_gps or 0))
                 for p in rows.values()]
        fixes.sort(key=lambda f: f.t)
        project(task, fixes)
        result = score_pilot(task, fixes, fixes[-1].t, params)
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
        "TrackingPoint": {"type": "object", "required": ["pilot_id", "timestamp", "lat", "lon", "alt"], "properties": {
            "pilot_id": {"type": "string"}, "timestamp": {"type": "integer", "format": "int64", "description": "Unix epoch seconds of GPS fix"},
            "lat": {"type": "number", "minimum": -90, "maximum": 90}, "lon": {"type": "number", "minimum": -180, "maximum": 180}, "alt": {"type": "number"}, "event_id": {"type": "string"}}},
        "Classification": {"type": "object", "properties": {"computed_at_epoch": {"type": "integer", "format": "int64"}, "ranking": {"type": "array", "items": {"type": "object", "properties": {"pilot_id": {"type": "string"}, "category_id": {"type": "string"}, "rank": {"type": "integer"}, "score": {"type": "number"}}}}}},
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
        "/mangas/{manga_id}/points": {"post": {"tags": ["Live tracking"], "security": [{"ApiKeyAuth": []}], "summary": "Push points and receive classification (legacy manga name)", "description": "Send every ~15 seconds. Deduplication key is (pilot_id,timestamp). Late, out-of-order, duplicate and missing points are accepted. cutoff_epoch is a cache watermark and is echoed as received_cutoff_epoch.", "parameters": [{"in": "path", "name": "manga_id", "required": True, "schema": {"type": "string"}}], "requestBody": {"required": True, "content": {"application/json": {"schema": points_body}}}, "responses": {"200": {"description": "Ack and classification", "content": {"application/json": {"schema": {"$ref": "#/components/schemas/Classification"}}}}, "400": {"description": "Invalid points"}, "404": {"description": "Unknown task"}}}},
        "/events/{event_id}/tasks/sync": {"post": {"tags": ["Task sync"], "security": [{"ApiKeyAuth": []}], "summary": "Upsert a stable task/day configuration", "parameters": [{"in": "path", "name": "event_id", "required": True, "schema": {"type": "string"}}], "requestBody": {"required": True, "content": {"application/json": {"schema": task_body}}}, "responses": {"200": {"description": "Task accepted"}, "400": {"description": "Validation errors"}, "404": {"description": "Unknown event"}}}},
        "/tasks/{task_id}/points": {"post": {"tags": ["Live tracking"], "security": [{"ApiKeyAuth": []}], "summary": "Push task tracking points and calculate immediately", "parameters": [{"in": "path", "name": "task_id", "required": True, "schema": {"type": "string"}}], "requestBody": {"required": True, "content": {"application/json": {"schema": points_body}}}, "responses": {"200": {"description": "Ingestion acknowledgement and latest classification"}, "404": {"description": "Unknown task"}}}},
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
    accepted = duplicates = 0
    with transaction.atomic():
        for point in points:
            try:
                pilot = str(point["pilot_id"])
                timestamp = _epoch_datetime(point["timestamp"])
                lat, lon = float(point["lat"]), float(point["lon"])
                if not (-90 <= lat <= 90 and -180 <= lon <= 180):
                    raise ValueError
            except (KeyError, TypeError, ValueError, OverflowError):
                return error("each point requires pilot_id, epoch timestamp, lat and lon")
            # Integration identity is (pilot_id, GPS timestamp), independent of
            # arrival batch, coordinates, cutoff watermark, or event_id.
            if TrackingPoint.objects.filter(competition=task.competition, pilot_id=pilot, timestamp=timestamp).exists():
                duplicates += 1
                continue
            fp = TrackingPoint.make_fingerprint(pilot, timestamp, lat, lon)
            TrackingPoint.objects.create(competition=task.competition, pilot_id=pilot,
                event_id=str(point.get("event_id", "")), timestamp=timestamp, latitude=lat, longitude=lon,
                altitude_gps=point.get("alt"), source="volandoo", fingerprint=fp, raw=point)
            accepted += 1
    try:
        classification = _live_classification(task.competition, task)
    except Exception as exc:
        classification = {"task_score": None, "pilots": [], "scoring_error": str(exc)}
    cutoff = data.get("cutoff_epoch")
    return JsonResponse({"task_id": manga_id, "manga_id": manga_id, "received_cutoff_epoch": cutoff, "status": "ok",
        "accepted": accepted, "duplicates": duplicates,
        "classification": {"computed_at_epoch": cutoff or int(datetime.now(timezone.utc).timestamp()),
                            "ranking": classification["pilots"], "task_score": classification["task_score"]}})


# English terminology for new integrations. The manga routes remain available
# as compatibility aliases for already deployed Volandoo clients.
def task_sync(request, event_id):
    return manga_sync(request, event_id)


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
    scored = score_competition(comp)
    if scored:
        task_obj, task_score, pilot_results = scored
        pilots = []
        for rank, result in enumerate(sorted(pilot_results, key=lambda r: r.rank_key), 1):
            pilots.append({"pilot_id": result.pilot, "rank": rank, "state": result.state,
                "score": result.total_points, "distance_m": result.distance,
                "speed_kmh": result.speed, "goal": result.goal_time is not None,
                "ess": result.ess_time is not None})
        return {"task_score": {"launch_validity": task_score.launch_validity,
            "distance_validity": task_score.distance_validity, "time_validity": task_score.time_validity,
            "task_validity": task_score.task_validity}, "pilots": pilots}
    latest = {}
    for row in comp.tracking_points.order_by("pilot_id", "-timestamp"):
        latest.setdefault(row.pilot_id, row)
    return {"task_score": None, "pilots": [{"pilot_id": p, "rank": i, "state": "TRACKING",
        "score": 0, "distance_m": 0} for i, p in enumerate(sorted(latest), 1)]}


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
            TrackingPoint.objects.create(competition=comp, pilot_id=pilot, event_id=str(point.get("event_id", "")), timestamp=timestamp,
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
