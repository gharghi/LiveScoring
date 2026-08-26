# Live scoring architecture

Split the system into two independently deployable responsibilities:

1. **Feed gateway (converter)** accepts any source, validates it, normalises it
   to one versioned event format, and appends it to the canonical event stream.
2. **Scoring engine** accepts *only* that canonical stream. It holds the
   current event state, recomputes a deterministic leaderboard, and publishes
   versioned snapshots. It must not know whether a position came from Voolando,
   an IGC file, a simulator, or a future provider.

The supplied task, competition, and IGC files become just one adapter: the
file/replay adapter. They are not special inputs to the scoring engine.

## Canonical input — `live-scoring.v1`

Transport may be JSON Lines on disk for demos and a queue/topic in production;
the message body is identical in both cases. Every event has this envelope:

```json
{
  "schema": "live-scoring.v1",
  "event_id": "uuid-or-source-stable-id",
  "competition_id": "cto-espana-sport-2026",
  "sequence": 1042,
  "observed_at": "2026-08-07T13:24:02Z",
  "type": "position",
  "data": {}
}
```

`event_id` makes delivery idempotent; `sequence` gives a reproducible ordering
within a competition; `observed_at` is the source timestamp, not arrival time.
Persist the original source payload and the canonical event together so every
conversion and score can be audited.

Required event types:

| Type | `data` required by the scorer |
| --- | --- |
| `competition.upsert` | name, discipline, scoring parameters, pilots-present |
| `task.upsert` | task ID/version, XCTrack task document (or its normalised turnpoints, gates and goal) |
| `pilot.upsert` | stable `pilot_id`, display name, competition ID |
| `position` | `pilot_id`, UTC timestamp, latitude, longitude, GPS altitude; optional barometric altitude, source and accuracy |
| `competition.status` | `open`, `stopped`, `final`; optional stop/deadline decisions |

The scorer rejects a position until it has the referenced task, competition and
pilot. The gateway should route malformed, duplicate-conflicting, or
unidentified data to a dead-letter/audit stream rather than guessing.

## Canonical output — `live-score.v1`

Publish a complete snapshot after a debounced batch of input events and at a
fixed heartbeat while a task is live. A snapshot contains `competition_id`,
`task_version`, `source_sequence`, `calculated_at`, `provisional`, task-wide
validity/allocation values, and the ranked pilots with distance, state,
crossing times, score components and total. The existing `--json` payload is a
useful starting point, but it should gain `schema`, `source_sequence`, and
`task_version` before being treated as this contract.

Store snapshots separately from the append-only event log. The API serves the
latest snapshot; a WebSocket/SSE channel pushes its version to the frontend.

## Continuous flow

```text
source update → adapter → validate/dedupe → canonical event log
              → scoring consumer → deterministic score → snapshot store
              → REST + WebSocket/SSE → frontend
```

The scoring consumer should checkpoint the last processed sequence. On restart
it loads the latest checkpoint/snapshot and replays later canonical events. A
full replay from event 1 must produce the same output: that is the essential
live-scoring audit property.

## Adapters

- **Voolando adapter:** poll or receive webhooks, retain provider IDs and raw
  payloads, then emit canonical `position` events.
- **File/replay adapter:** read `Task.xctsk`, `competition.json` and IGC files;
  emit setup events first, then position events ordered by fix timestamp.
  `--speed` controls wall-clock delay only, never event timestamps. It can
  pause, seek, restart, throttle and inject latency/dropouts for demos.
- **Future adapters:** FLARM, Flymaster, REST polling, CSV, or other scoring
  providers. They can be developed and tested without changing scoring rules.

## Operational boundary

Keep the current `engine/` package as the scoring core. Add a thin service
wrapper around it later (`scoring-service`) and place all provider/file code in
`feed-gateway`. Neither service should call the other’s database directly:
the canonical event stream and snapshot contract are their boundary.

For a first demo, JSONL files are enough:

```text
events.jsonl  # append-only canonical input
snapshots/    # canonical output, latest.json plus historical versions
```

Production can replace those two implementations with a durable broker and
database without changing the messages or the scoring core.
