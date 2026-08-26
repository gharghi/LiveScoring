# Frontend API handoff

Run the API with `python3 -m uvicorn api_app:app --host 0.0.0.0 --port 8000`. Interactive
OpenAPI documentation is available at `http://localhost:8000/docs`; the raw
contract is `http://localhost:8000/openapi.json`.

## Endpoints

| Method | Endpoint | Frontend use |
| --- | --- | --- |
| `GET` | `/health` | deployment/readiness check |
| `GET` | `/api/v1/results/latest` | initial page load and polling target |
| `GET` | `/api/v1/results/latest/pilots` | lightweight leaderboard refresh |
| `GET` | `/api/v1/results/history?limit=60` | timeline, chart or replay scrubber |

All result endpoints accept an optional `competition_id` query parameter. The
API returns JSON and uses `404` before the first canonical input has generated
a persisted snapshot.

## Polling model

Poll `/api/v1/results/latest` every 1–2 seconds. Compare `source_sequence` to
the value already rendered; if unchanged, do not redraw. `provisional` is true
until the feed emits `competition.status: final`; show this status prominently.

## Snapshot fields

`live-score.v1` contains:

```json
{
  "schema": "live-score.v1",
  "competition_id": "cto-sport-2026",
  "source_sequence": 1042,
  "calculated_at": "2026-08-07T13:24:02Z",
  "provisional": true,
  "status": "open",
  "task": { "name": "Task", "total_distance_m": 76381, "speed_distance_m": 71338 },
  "validity": { "launch": 1, "distance": 0.9, "time": 0.8, "task": 0.72 },
  "results": [{ "rank": 1, "pilot": "…", "state": "GOAL", "distance_m": 76381,
                "start": 0, "ess": 0, "goal": 0, "total_points": 997.4 }]
}
```

Times are Unix seconds in pilot rows; `calculated_at` is an ISO-8601 UTC string.
Distances are metres. Sort order in `results` is already the official ranking.
