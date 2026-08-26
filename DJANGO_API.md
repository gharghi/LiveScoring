# Django/PostgreSQL API

The production API is Django-backed and uses PostgreSQL. All protected
requests send `X-API-Key: <key>` (Bearer tokens are also accepted).

## 1. Create an application key

```http
POST /api/v1/api-keys
Content-Type: application/json

{"name":"my-tracker"}
```

The response contains `id` and `api_key`. Store the key immediately; the
plaintext is returned only once. Set `LS_ADMIN_KEY` on the server to require
an admin header for key creation.

## 2. Create a competition

```http
POST /api/v1/competitions
X-API-Key: ls_...
Content-Type: application/json

{
  "name":"Summer Task 1",
  "status":"draft",
  "settings":{
    "discipline":"paragliding",
    "nominal_distance_km":60,
    "minimum_distance_km":5,
    "nominal_time_min":90,
    "pilots_present":120,
    "leading_time_ratio":0.26
  }
}
```

The response returns the competition UUID. Settings are stored as JSON so all
competition-specific rule settings can be retained without losing unknown
provider fields.

## 3. Configure a task

```http
POST /api/v1/competitions/{competition_id}/tasks
X-API-Key: ls_...
Content-Type: application/json

{"name":"Task 1", "settings":{"xctsk":{}, "gate":"11:00:00Z", "goal_deadline":"15:00:00Z"}}
```

Each task update receives a new task UUID and monotonically increasing version.

## 4. Send tracking data

Send one point or a batch. Batches are limited to 5,000 points per request.
The server accepts partial uploads, missing intervals, late points, points out
of order, and duplicates. Every received row is retained for audit. The
response reports duplicate fingerprints but does not discard the duplicate.

```http
POST /api/v1/competitions/{competition_id}/tracking
X-API-Key: ls_...
Content-Type: application/json

{"points":[
  {"pilot_id":"pilot-42","event_id":"device-42-100",
   "timestamp":"2026-08-26T12:00:00Z","lat":42.0463,"lon":0.7460,
   "alt_gps":1560,"source":"device-a"}
]}
```

`event_id` is retained as a source identifier. Duplicate detection uses the
stable tuple `(pilot_id, timestamp, latitude, longitude)`, allowing retries
without rejecting the request.

## 5. Read current data

- `GET /api/v1/competitions/{id}` — competition settings and task versions.
- `GET /api/v1/competitions/{id}/results` — latest point and received count per pilot.
- `GET /health` — service health.
- `GET /openapi.json` — machine-readable endpoint summary.

The current ingestion results endpoint is deliberately independent of transport
batch boundaries. A scoring worker can consume the same PostgreSQL rows later,
replay them in timestamp order, and produce final GAP results without changing
the ingestion contract.
