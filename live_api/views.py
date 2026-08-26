import json
import os
import secrets
from datetime import datetime, timezone

from django.db import transaction
from django.http import JsonResponse
from django.views.decorators.csrf import csrf_exempt

from .models import ApiApplication, Competition, Task, TrackingPoint


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
    return JsonResponse({"openapi": "3.0.3", "info": {"title": "LiveScoring API", "version": "2.0.0"},
        "servers": [{"url": "https://ls.buildmycabin.com"}], "paths": {
            "/api/v1/api-keys": {"post": {"summary": "Create a persistent application API key"}},
            "/api/v1/competitions": {"post": {"summary": "Create a competition with all settings"}},
            "/api/v1/competitions/{id}/tasks": {"post": {"summary": "Configure a task"}},
            "/api/v1/competitions/{id}/tracking": {"post": {"summary": "Ingest partial, duplicate or out-of-order tracking"}},
            "/api/v1/competitions/{id}/results": {"get": {"summary": "Get latest pilot positions"}},
        }})


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
    return JsonResponse({"competition_id": str(comp.id), "status": comp.status, "pilots": [
        {"pilot_id": pilot, "points_received": counts[pilot], "last_timestamp": latest[pilot].timestamp.isoformat(),
         "lat": latest[pilot].latitude, "lon": latest[pilot].longitude, "alt_gps": latest[pilot].altitude_gps}
        for pilot in sorted(latest)
    ]})
