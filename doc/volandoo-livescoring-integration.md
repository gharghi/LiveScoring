# Volandoo ↔ Live Scoring Integration Guide

**Status:** Draft for joint review between Volandoo and the Live Scoring application developer.
**Purpose:** Define the data contract and workflow between the Volandoo server (which receives raw tracker data from pilots) and an external/internal Live Scoring application (which computes and returns live rankings).

---

## 1. Scope

This document covers:

- How event and stage ("manga") configuration is pushed from Volandoo to the Live Scoring app.
- How raw GPS tracker points flow from Volandoo to the Live Scoring app in near real time.
- How the Live Scoring app returns computed classifications back to Volandoo for public display.
- Synchronization, recovery, and data-integrity rules for a link that may run across two separate servers.

It does **not** define the scoring formula itself (owned by the Live Scoring developer) or the final transport/security layer (open item, see §9).

---

## 2. Actors & Responsibilities

| Actor | Responsibility |
|---|---|
| **Trackers** | Send raw GPS fixes (lat/lon/alt + timestamp) per pilot, roughly 1 point/second, over the existing tracker network. |
| **Volandoo Server** | Receives tracker data, detects takeoff/landing per pilot, buffers points, pushes config and point batches to Live Scoring, publishes results on the public event page. |
| **Live Scoring App** | Receives event/manga config, ingests point batches, deduplicates/orders them, computes classification per the event's formula, returns results to Volandoo. |
| **Event Organizer (Volandoo UI)** | Configures the event (formula, categories, pilots) and each day's manga, and triggers the sync actions. |

---

## 3. Glossary / Identifiers

| Term | Definition |
|---|---|
| `event_id` | Created the first time the organizer configures the event in the Volandoo UI and presses **Sync/Update**. Stable for the lifetime of the event. |
| `manga_id` | Created the first time a manga (competition round/day) is set up. Stable per day — **does not change** when the manga's parameters are later edited (e.g., start time changes). |
| `cutoff_epoch` | A Unix timestamp marking "Volandoo has sent all points up to this moment." Used purely to manage Volandoo's local cache — **it is not an identifier of the manga**, just a rolling watermark for the point-push loop. (This replaces the earlier "stage_id" wording — same idea, renamed to avoid confusion with `manga_id`.) |
| `point.timestamp` | The GPS fix's own timestamp (per pilot, per second). This is the true unique key for a point, independent of `cutoff_epoch`. |

---

## 4. High-Level Flow

1. **Event Sync** — organizer configures the event and syncs it once (or re-syncs on changes).
2. **Manga Sync** — organizer creates/updates the day's manga; can be re-sent any time parameters change, `manga_id` stays the same.
3. **Live Tracking Loop** — while the manga is active, Volandoo pushes buffered points every ~15s; Live Scoring acknowledges and returns computed rankings.
4. **Recovery** — either side can query sync state to resume cleanly after a restart or dropped message.

See diagrams `livescoring-architecture-flow.mermaid` and `livescoring-tracking-loop.mermaid` for the visual version of phases 1–4.

---

## 5. Phase 1 — Event Configuration Sync

**Trigger:** Organizer presses "Sync/Update" on the event's Live Scoring tab in Volandoo.

**Request:** `POST /events/sync`

```json
{
  "schema_version": "1.0",
  "event_id": "evt_2026_xc_open",
  "event_name": "XC Open 2026",
  "sent_at": "2026-08-26T09:00:00Z",
  "formula": {
    "type": "GAP",
    "parameters": {
      "min_distance_km": 5,
      "goal_bonus": 120
    }
  },
  "categories": [
    {
      "category_id": "cat_open",
      "name": "Open",
      "formula_override": null
    },
    {
      "category_id": "cat_sport",
      "name": "Sport",
      "formula_override": {
        "type": "GAP",
        "parameters": { "min_distance_km": 3, "goal_bonus": 90 }
      }
    }
  ],
  "pilots": [
    { "pilot_id": "plt_001", "name": "Jane Doe", "category_id": "cat_open", "tracker_id": "trk_1234" },
    { "pilot_id": "plt_002", "name": "John Smith", "category_id": "cat_sport", "tracker_id": "trk_5678" }
  ]
}
```

**Response:**

```json
{
  "event_id": "evt_2026_xc_open",
  "status": "ok",
  "errors": []
}
```

or, on validation failure:

```json
{
  "event_id": "evt_2026_xc_open",
  "status": "error",
  "errors": [
    { "field": "pilots[3].category_id", "message": "Unknown category_id 'cat_hg'" }
  ]
}
```

> **Assumption to confirm:** the event carries one default formula, with an optional per-category override (needed for mixed-category events like Open/Sport with different GAP parameters). Please confirm with the Live Scoring developer whether per-category overrides are supported or whether one formula per event is the actual requirement.

**Re-sync behavior:** the organizer can resend this same payload at any time (e.g., adding a pilot mid-event); `event_id` never changes, and Live Scoring should treat this as an upsert of the full config, not a delta.

---

## 6. Phase 2 — Manga (Stage) Configuration Sync

**Trigger:** Organizer creates or edits a day's manga in Volandoo.

**Request:** `POST /events/{event_id}/mangas/sync`

```json
{
  "schema_version": "1.0",
  "event_id": "evt_2026_xc_open",
  "manga_id": "manga_2026_08_26",
  "manga_date": "2026-08-26",
  "scheduled_start_time": "2026-08-26T11:00:00Z",
  "status": "scheduled",
  "pilots": ["plt_001", "plt_002"],
  "sent_at": "2026-08-26T08:30:00Z"
}
```

**Response:** same `status` / `errors` pattern as Phase 1.

**Rules:**
- `manga_id` is stable per day; edits (start time changes, pilot list changes, cancellations) are re-sent under the same `manga_id`.
- Volandoo determines takeoff (from the scheduled start / first movement) and landing (per pilot) automatically. Live Scoring does **not** need to infer flight state — see §8.

---

## 7. Phase 3 — Live Tracking Loop

While a manga is active, Volandoo buffers incoming tracker points and flushes them every **15 seconds**.

### 7.1 Points push (Volandoo → Live Scoring)

`POST /mangas/{manga_id}/points`

```json
{
  "schema_version": "1.0",
  "event_id": "evt_2026_xc_open",
  "manga_id": "manga_2026_08_26",
  "cutoff_epoch": 1756201215,
  "points": [
    { "pilot_id": "plt_001", "timestamp": 1756201201, "lat": 41.123456, "lon": 2.123456, "alt": 850.5 },
    { "pilot_id": "plt_001", "timestamp": 1756201202, "lat": 41.123481, "lon": 2.123511, "alt": 852.1 },
    { "pilot_id": "plt_002", "timestamp": 1756201200, "lat": 41.130200, "lon": 2.118800, "alt": 910.0 }
  ]
}
```

- `cutoff_epoch`: "Volandoo has flushed everything it has up to this moment."
- `points[]`: may contain duplicates, out-of-order entries relative to a previous batch, and small gaps. See §8 for handling rules.

### 7.2 Response — Ack + classification (Live Scoring → Volandoo)

```json
{
  "manga_id": "manga_2026_08_26",
  "received_cutoff_epoch": 1756201215,
  "status": "ok",
  "classification": {
    "computed_at_epoch": 1756201215,
    "ranking": [
      { "pilot_id": "plt_001", "category_id": "cat_open", "rank": 1, "score": 812.4 },
      { "pilot_id": "plt_002", "category_id": "cat_sport", "rank": 1, "score": 640.0 }
    ]
  }
}
```

- `received_cutoff_epoch` echoes the request's `cutoff_epoch` — this is what tells Volandoo it can purge its cache up to that point.
- The exact shape of `classification` (fields beyond `score`/`rank`) is owned by the Live Scoring developer — pending confirmation (§9).
- If the calculation takes longer than the 15s window, `status` can be `"processing"` with the previous `computed_at_epoch`, and Volandoo will pick up the latest ranking on the next cycle.

### 7.3 Recommended addition — classification receipt ack

To keep both sides provably in sync (not just on points, but on published rankings too):

`POST /mangas/{manga_id}/classification/ack`

```json
{
  "manga_id": "manga_2026_08_26",
  "acknowledged_epoch": 1756201215
}
```

Sent by Volandoo once it has published the ranking on the public page. This is a **proposed enhancement**, not yet finalized — flagged for discussion in §9.

---

## 8. Data Handling Rules

These rules exist so that normal tracker/network imperfections never become integration bugs:

1. **Uniqueness key:** a point is uniquely identified by `(pilot_id, timestamp)` — not by arrival order or by `cutoff_epoch`. Live Scoring must dedupe on this key.
2. **Out-of-order delivery is expected.** Points for a given pilot may arrive with timestamps earlier than points already processed for that pilot. Live Scoring should reserve/accept insertion at the correct position rather than assuming strictly increasing order.
3. **Point loss tolerance:** occasional gaps in the ~1 point/second stream are expected due to tracker/network conditions. As long as loss stays under ~1% of expected points, it has no meaningful effect on scoring and requires no special handling (no interpolation, no error state).
4. **Idle / landed pilots:** a pilot with no incoming points is simply not flying (pre-takeoff, landed, or temporary signal loss). Volandoo does not send an explicit "landed" flag — Live Scoring should treat absence of new points as "no update," not as an error or a "frozen position" bug on the public map.
5. **Volandoo already filters flight state.** Takeoff/landing detection is handled entirely on the Volandoo side; once a pilot lands, Volandoo stops sending further points for that pilot in that manga. Live Scoring does not need takeoff/landing logic of its own.

---

## 9. Open Questions (for joint decision)

These are intentionally left open pending discussion between the Volandoo team and the Live Scoring developer:

1. **Validation error contract for config sync** (Phase 1 & 2): exact error schema, HTTP status codes, and whether partial validation (accept event, reject one bad pilot) is allowed or if it's all-or-nothing.
2. **Transport & security:** REST over HTTPS vs. WebSocket for the tracking loop; authentication method (API key, mTLS, IP allow-list); whether Volandoo and Live Scoring will ever run on the same host (simplifying auth) or always assume a network hop.
3. **Schema versioning policy:** this draft includes a `schema_version` field on every payload as a low-cost safeguard — confirm both sides will honor it and define a deprecation policy for breaking changes.
4. **Classification receipt ack (§7.3):** confirm whether this second ack round-trip is worth the added complexity, or whether the single points-ack is sufficient.
5. **Exact shape of the `classification` object:** fields beyond `rank`/`score` (e.g., distance, speed, GAP details) — to be defined by the Live Scoring developer once the formula output is finalized.

---

## 10. Recovery & Resynchronization

To handle app restarts, dropped connections, or lost acks without manual intervention:

- **Sync status query** (either direction): `GET /mangas/{manga_id}/sync-status`

```json
{
  "manga_id": "manga_2026_08_26",
  "last_epoch_sent_by_volandoo": 1756201215,
  "last_epoch_confirmed_by_livescoring": 1756201200,
  "last_epoch_calculated_by_livescoring": 1756201200
}
```

- If Live Scoring restarts and loses in-memory state, it can request a **full snapshot** of the active manga instead of waiting for the next incremental push:
  `GET /mangas/{manga_id}/snapshot` → returns all points from manga start to now, using the same `points[]` shape as §7.1.
- If Volandoo does not receive an ack for a pushed batch within a timeout, it should re-poll `sync-status` rather than blindly resending, since the batch may have arrived but the ack was lost (idempotency comes from the `(pilot_id, timestamp)` key, so resending is also safe if it happens anyway).

---

## 11. Summary of Endpoints

| Endpoint | Direction | Purpose |
|---|---|---|
| `POST /events/sync` | Volandoo → Live Scoring | Create/update event, formula, categories, pilots |
| `POST /events/{event_id}/mangas/sync` | Volandoo → Live Scoring | Create/update a day's manga |
| `POST /mangas/{manga_id}/points` | Volandoo → Live Scoring | Push buffered GPS points (~every 15s) |
| *(response to the above)* | Live Scoring → Volandoo | Ack cutoff + return computed ranking |
| `POST /mangas/{manga_id}/classification/ack` | Volandoo → Live Scoring | *(proposed)* confirm ranking was published |
| `GET /mangas/{manga_id}/sync-status` | either | Compare last known epochs for recovery |
| `GET /mangas/{manga_id}/snapshot` | Volandoo → Live Scoring | Full point replay after a restart |

---

*This document is intended as a working draft. Sections 9 and the exact transport/security decisions in §2/§10 should be confirmed jointly before implementation starts.*
