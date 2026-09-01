import json
import os
import secrets
import time

from django.http import JsonResponse
from django.shortcuts import render
from django.views.decorators.csrf import csrf_exempt

from .models import ApiApplication, Competition, Task
from .explain import explain_competition, explain_pilot, explain_task
from .storage import insert_task_points, latest_task_classification


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
        "Classification": {"type": "object", "properties": {
            "computed_at_epoch": {"type": "integer", "format": "int64", "nullable": True},
            "processed_epoch": {"type": "integer", "format": "int64", "nullable": True},
            "point_count": {"type": "integer"},
            "status": {"type": "string"},
            "task_score": {"type": "object", "nullable": True},
            "timings": {"type": "object", "nullable": True},
            "ranking": {"type": "array", "items": {"type": "object", "properties": {
                "pilot_id": {"type": "string"},
                "category_id": {"type": "string"},
                "rank": {"type": "integer"},
                "state": {"type": "string"},
                "score": {"type": "number"},
                "distance_m": {"type": "number"},
                "speed_kmh": {"type": "number", "nullable": True},
                "ess": {"type": "boolean"},
                "goal": {"type": "boolean"},
                "scoring": {"type": "object", "nullable": True, "properties": {
                    "distance_points": {"type": "number"},
                    "time_points": {"type": "number"},
                    "leading_points": {"type": "number"},
                    "total_points": {"type": "number"},
                    "lc": {"type": "number"},
                    "start_epoch": {"type": "number", "nullable": True},
                    "start_cross_epoch": {"type": "number", "nullable": True},
                    "ess_epoch": {"type": "number", "nullable": True},
                    "goal_epoch": {"type": "number", "nullable": True}}},
                "landing": {"type": "object", "nullable": True, "properties": {
                    "detected": {"type": "boolean"},
                    "epoch": {"type": "number", "nullable": True},
                    "fix_index": {"type": "integer", "nullable": True}}},
                "position": {"type": "object", "properties": {
                    "lat": {"type": "number"},
                    "lon": {"type": "number"},
                    "alt_m": {"type": "number", "nullable": True},
                    "next_waypoint_index": {"type": "integer", "nullable": True},
                    "next_waypoint": {"type": "string", "nullable": True},
                    "distance_to_next_m": {"type": "number", "nullable": True},
                    "distance_to_goal_m": {"type": "number", "nullable": True},
                    "progress_percent": {"type": "number", "nullable": True}}}}}}}},
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
        "/mangas/{manga_id}/points": {"post": {"tags": ["Live tracking"], "security": [{"ApiKeyAuth": []}], "summary": "Push points and receive latest stored classification (legacy manga name)", "description": "Send every ~15 seconds. Deduplication key is (pilot_id,epoch). Late, out-of-order, duplicate and missing points are accepted. Django stores the points and returns the latest scorer snapshot; scoring runs outside the request path, so classification can lag by the worker interval. processed_epoch confirms the highest point epoch accepted or recognized by ingestion. scored_epoch is the highest epoch in the returned classification snapshot.", "parameters": [{"in": "path", "name": "manga_id", "required": True, "schema": {"type": "string"}}], "requestBody": {"required": True, "content": {"application/json": {"schema": points_body}}}, "responses": {"200": {"description": "Ack and latest stored classification"}, "400": {"description": "Invalid points"}, "404": {"description": "Unknown task"}}}},
        "/events/{event_id}/tasks/sync": {"post": {"tags": ["Task sync"], "security": [{"ApiKeyAuth": []}], "summary": "Upsert a stable task/day configuration", "parameters": [{"in": "path", "name": "event_id", "required": True, "schema": {"type": "string"}}], "requestBody": {"required": True, "content": {"application/json": {"schema": task_body}}}, "responses": {"200": {"description": "Task accepted"}, "400": {"description": "Validation errors"}, "404": {"description": "Unknown event"}}}},
        "/tasks/{task_id}/points": {"post": {"tags": ["Live tracking"], "security": [{"ApiKeyAuth": []}], "summary": "Push task tracking points and read latest stored classification", "description": "Each point must include epoch (Unix seconds). Django only ingests and fetches stored results. The separate scoring worker updates the classification snapshot continuously.", "parameters": [{"in": "path", "name": "task_id", "required": True, "schema": {"type": "string"}}], "requestBody": {"required": True, "content": {"application/json": {"schema": points_body}}}, "responses": {"200": {"description": "Ingestion acknowledgement and latest stored classification, including received_cutoff_epoch, processed_epoch and scored_epoch"}, "404": {"description": "Unknown task"}}}},
        "/tasks/{task_id}/results": {"get": {"tags": ["Results"], "security": [{"ApiKeyAuth": []}], "summary": "Get the latest calculated task results", "parameters": [{"in": "path", "name": "task_id", "required": True, "schema": {"type": "string"}}], "responses": {"200": {"description": "Current ranking and per-pilot scoring fields", "content": {"application/json": {"schema": {"$ref": "#/components/schemas/Classification"}}}}, "404": {"description": "Unknown task"}}}},
        "/explain": {"get": {"tags": ["Explain"], "security": [{"ApiKeyAuth": []}], "summary": "Protest desk: why a competition, task or pilot scored what it did", "description": "Pass event_id for the competition scope, task_id for the task scope, or task_id together with pilot_id for one pilot. The task and pilot scopes re-run the engine over the whole field and are deliberately slow; every points line carries its S7F reference, its formula and the same formula with the actual numbers substituted.", "parameters": [{"in": "query", "name": "event_id", "required": False, "schema": {"type": "string"}}, {"in": "query", "name": "task_id", "required": False, "schema": {"type": "string"}}, {"in": "query", "name": "pilot_id", "required": False, "schema": {"type": "string"}}], "responses": {"200": {"description": "Explanation for the requested scope"}, "400": {"description": "No scope given, or pilot_id without task_id"}, "404": {"description": "Unknown event, task or pilot"}, "409": {"description": "Task has no geometry or no tracking points yet"}}}},
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
    try:
        ingestion = insert_task_points(task, points)
    except ValueError as exc:
        return error(str(exc))
    accepted = ingestion["accepted"]
    duplicates = ingestion["duplicates"]
    ingestion_ms = (time.perf_counter() - started_at) * 1000
    classification = latest_task_classification(task)
    cutoff = data.get("cutoff_epoch")
    processed_epoch = ingestion["processed_epoch"]
    return JsonResponse({"task_id": manga_id, "manga_id": manga_id, "received_cutoff_epoch": cutoff,
        "processed_epoch": processed_epoch, "status": "ok",
        "accepted": accepted, "duplicates": duplicates,
        "ingestion_ms": round(ingestion_ms, 2),
        "scored_epoch": classification.get("processed_epoch"),
        "scoring_status": classification.get("status", "pending"),
        "processing_ms": round((time.perf_counter() - started_at) * 1000, 2),
        "classification": {"computed_at_epoch": classification.get("computed_at_epoch"),
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
def explain(request):
    """Protest desk: why a competition, a task or a pilot scored what it did.

    GET /explain?event_id=E                      → competition scope
    GET /explain?task_id=T                       → task scope
    GET /explain?task_id=T&pilot_id=P            → pilot scope

    The task and pilot scopes re-run the engine over the whole field, because
    the half of a score a pilot usually disputes is field-wide. That is slow
    on a big task by design; this is a desk tool, not a live path.
    """
    if request.method != "GET":
        return error("method not allowed", 405)
    app, response = _integration_auth(request)
    if response:
        return response

    event_id = request.GET.get("event_id")
    task_id = request.GET.get("task_id")
    pilot_id = request.GET.get("pilot_id")
    if not event_id and not task_id:
        return error("pass event_id, or task_id (optionally with pilot_id)", 400)
    if pilot_id and not task_id:
        return error("pilot_id needs a task_id: a pilot is explained per task", 400)

    started = time.perf_counter()
    try:
        if task_id:
            try:
                task = Task.objects.select_related("competition").get(
                    external_manga_id=task_id, competition__owner=app)
            except Task.DoesNotExist:
                return error("task not found", 404)
            payload = (explain_pilot(task, pilot_id) if pilot_id
                       else explain_task(task))
        else:
            try:
                comp = Competition.objects.get(external_event_id=event_id, owner=app)
            except Competition.DoesNotExist:
                return error("event not found", 404)
            payload = explain_competition(comp)
    except LookupError as exc:
        return error(str(exc), 404)
    except ValueError as exc:
        return error(str(exc), 409)

    payload["elapsed_ms"] = round((time.perf_counter() - started) * 1000, 1)
    return JsonResponse(payload)


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
    classification = latest_task_classification(task)
    return JsonResponse({"task_id": task_id, "event_id": task.competition.external_event_id,
        **classification})


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
    classification = latest_task_classification(task) if task else {
        "computed_at_epoch": None, "processed_epoch": None, "point_count": 0,
        "status": "pending", "task_score": None, "timings": None, "error": None,
        "pilots": [],
    }
    return JsonResponse({"event_id": event_id, "task_id": task.external_manga_id if task else None,
        **classification})


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
    else:
        task = comp.tasks.order_by("-version").first()
    if task is None:
        return error("task_id is required until a task has been configured", 400)
    try:
        ingestion = insert_task_points(task, points)
    except ValueError as exc:
        return error(str(exc))
    classification = latest_task_classification(task)
    return JsonResponse({"accepted": ingestion["accepted"], "duplicates": ingestion["duplicates"],
                         "processed_epoch": ingestion["processed_epoch"],
                         "scored_epoch": classification.get("processed_epoch"),
                         "scoring_status": classification.get("status", "pending"),
                         "missing_data_accepted": True, "competition_id": str(comp.id),
                         "task_id": task.external_manga_id or str(task.id)}, status=202)


def results(request, competition_id):
    comp, response = owned(request, competition_id)
    if response: return response
    task = comp.tasks.order_by("-version").first()
    classification = latest_task_classification(task) if task else {
        "computed_at_epoch": None, "processed_epoch": None, "point_count": 0,
        "status": "pending", "task_score": None, "timings": None, "error": None,
        "pilots": [],
    }
    return JsonResponse({"competition_id": str(comp.id), "status": comp.status,
        "task_id": task.external_manga_id if task else None, **classification})
