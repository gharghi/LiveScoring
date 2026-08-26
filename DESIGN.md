# Paragliding Live Scoring Engine

## Technical Design Document

**Status:** Proposed
**Version:** 2.1
**Supersedes:** design.txt v1.0
**Scale target:** 150 pilots per task, 1 Hz telemetry
**Rules:** FAI Sporting Code Section 7F, 2026 edition V1.0 — **paragliding**

**Primary goal:** Produce a live leaderboard that is *correct* and *self-healing* under lossy, delayed, out-of-order tracker telemetry — and that provably agrees with official scoring when fed the same data.

---

## 0. What changed from v1.0, and why

v1.0 was architected around throughput and sub-second latency. Measured against the real workload, that was the wrong axis.

**Sizing, from the actual competition data in this repo** (`Cto-espana-Cat-Sport_2026-08-08`, 48 IGC files, 42 pilots, 176,533 fixes):

| Measurement | Value |
| --- | --- |
| Fix rate (onboard logger) | 1 Hz, every file |
| Peak concurrent pilots | 40 (at 11:30Z) |
| Median track duration | 52 min (max 153 min) |
| Design target | 150 pilots |
| **Peak event rate at target** | **150 events/sec** |

At 150 events/sec, a hot path of one dict lookup plus ~10 float operations costs roughly **0.5% of one CPU core**. The entire task's point history for 150 pilots over 4 hours fits in **under 100 MB of RAM**. Throughput is not a risk, and no amount of architecture will make it one.

What *is* a risk is correctness under bad input. The changes in v2.0 follow from that:

1. **Late/backfilled data now rewinds and recomputes the affected pilot.** v1.0 discarded it, which silently and permanently corrupts the leaderboard. This is now the central mechanism of the design, not an edge case. (§4, §7)
2. **One scoring function serves both live and official scoring.** They are no longer two engines that "may share models". Live is the same function called incrementally; official is the same function called once. Divergence becomes a testable assertion instead of a hope. (§7)
3. **Optimized route distance is promoted to a first-class component.** On the supplied Task 06, center-to-center distance is **91.42 km** but the optimized route is **46.36 km** — 49.3% shorter, because of the 17 km G23 cylinder. A leaderboard built on center-to-center distance is not approximately right; it is wrong by half. (§8)
4. **Horizontal scaling, task partitioning and multi-engine coordination are removed.** One process handles the entire competition. (§5)
5. **The engine never reads a wall clock.** Determinism in v1.0 was claimed but not achievable, because tracking-loss and gate expiry were time-dependent. (§16)
6. **Golden-file validation against real competitions is the primary correctness gate.** (§19)
7. **WebSocket fan-out is costed and designed.** It is now the largest compute cost in the system — larger than scoring by two orders of magnitude. (§15)

The parts of v1.0 that were right and are retained: engine as a dependency-free Python package, geometry as an isolated tested module, replay as a first-class feature, immutable GPS storage with derived state, async batch persistence, provisional-vs-official separation, versioned scoring rules, p99 over averages.

### 0.1 What changed in v2.1, and why

v2.0 said the optimized route was the highest-risk component in the system (§8.1). It was, and it was wrong. Three defects were found by building the verification described in §19 and running it against a real 129-pilot field; all three are fixed, and the way they were found changed how §19 is specified.

1. **The route optimizer was 8.3% long.** It placed each turnpoint on its cylinder in the direction of the *midpoint of its neighbours*, which is the correct minimizer only when the two legs are equal. The condition on a circle is the reflection law. Correcting that, and then escaping a local minimum with a multi-start discretized shortest-path seed, took the task from 58,435 m to **53,966 m**. Every distance-based quantity moved.

2. **A local check confirmed the bug.** A single-point perturbation search — the obvious way to test an optimizer — *passed* on the 2.0 km-too-long route, because coordinate descent stops precisely where no single point can improve, and the check moved one point at a time too. It shared the bug's assumption. Only an independent shortest-path DP over discretized cylinders, which moves every point at once, could see it. §19 now requires that **every check be independent of the mechanism it checks**, and names this as the reason.

3. **`launch_to_sss` exceeded the straight line** between the takeoff and SSS cylinders — 6,510 m across a 6,122 m gap — because it measured to the SSS point the *task route* uses rather than the nearest point of the start cylinder. It is a scored quantity for [PG] S7F 13.3 early starters.

Two capabilities were added, both of which follow from §17 (live vs official, and correction in the UI) rather than from anything new:

4. **A per-pilot audit trail (`--explain`).** §17 said the system must be able to explain a correction. It could not: it could only restate the answer. `score_pilot()` now takes an optional trace argument and records *why* it decided what it decided — every SSS crossing it considered and the S7F 8.1 rule that selected one, every control zone with the two distances that bracket the crossing, the fix that fixed the scored distance, and every points line as formula and substituted numbers. Because the trace comes out of the scoring pass itself there is no second implementation to drift; the invariant suite asserts that tracing changes no scored value.

5. **Cold full-field recompute now runs across every core.** The live path was never at risk — 0.56 µs per fix, 0.01% of one core at design scale. The number a scorer actually waits on is the cold one: last tracklog uploaded, nothing parsed, publish the final board. That was 3.8 s and is now **0.73 s** for 129 pilots and 1.4 M fixes. Pilots are independent, so this is a `fork` pool over the same `score_pilot` and `score_task` — no rule lives in it. The suite asserts the parallel board is identical to the serial one, pilot by pilot and in rank order.

6. **Each scoring rule is now its own file** (`engine/rules/`, one per Sporting Code section, with `./run.py --rules` as the index). GAP is not one calculation, it is seventeen, chained. When the published result for the reference task disagreed, every diagnostic question turned out to be "which of the seventeen", and a single `gap.py` holding all of them was the wrong shape to answer it. Each file now carries one pure function, the rule as text, the paragliding/hang-gliding difference, and an honest per-rule verification status — `implemented`, `SUSPECT` or `NOT IMPLEMENTED` — which `--verify` reads back rather than restating in prose that goes stale.

7. **S7F 13.5 penalties exist.** They were on the not-implemented list, and the reference result carries an airspace disqualification worth 290 points. A penalty is decided by the meet director and cannot be derived from a tracklog, so it is an INPUT (`penalties.json`) rather than a computation — which is precisely why no internal check could have found it, and why §19's levels needed a level E that reaches outside the codebase at all.

The optimizations that made §19's checks necessary rather than merely nice — an inlined copy of the S7F 9.2.1 zone test in the hot loops, a byte-offset IGC parser beside the regex one, and a leading coefficient split into per-pilot and field-wide halves — are each pinned by an exact equality check against the implementation they replace. See VERIFICATION.md §5.

---

## 1. The problem that actually defines the architecture

The IGC files in this repository are **onboard logger** files. They are essentially perfect: 1 Hz, 176,476 of 176,533 intervals exactly 1 second, longest gap under 60 seconds across all 48 files.

Live telemetry is nothing like this. The same flight, delivered live, is:

- delayed by 10–60 s in normal conditions,
- **absent for 10–15 minutes** when the pilot is in a valley or behind terrain,
- then **delivered all at once** as a burst of 600–900 backfilled points,
- interleaved with other pilots' current positions,
- occasionally duplicated, reordered, or containing impossible jumps.

So the design must hold two facts at once:

> **The live stream is a lossy, delayed, out-of-order approximation of a track that will later be known perfectly.**

This gives the system its defining requirement. Not speed — **convergence**:

> When a pilot's backfill arrives, the leaderboard must correct itself to exactly the state it would have had if that data had arrived on time.

A pilot who tags TP4 inside a coverage hole must appear at TP4 the moment their backfill lands — including a correct, retroactive turnpoint timestamp — not stay frozen at TP3 until official scoring. v1.0's rule ("the late packet normally does not alter the current state") makes that impossible.

---

## 2. Goals

### 2.1 Functional

1. Ingest and normalize positions from multiple tracker providers.
2. Persist raw normalized positions immutably.
3. Maintain live per-pilot task state for every pilot.
4. Detect task events: takeoff, start (including re-starts), turnpoint, ESS, goal, tracking loss/recovery, landing, task stop.
5. Compute provisional live GAP scoring: distance, speed, leading, arrival.
6. Publish live state, events and leaderboard to clients.
7. **Converge to the correct state after backfill, within one processing cycle of its arrival.**
8. Replay any task deterministically from stored data.
9. Produce official results from the same code path as live results.

### 2.2 Correctness (the real targets)

| Property | Target |
| --- | --- |
| Live result vs. full recompute of same points | **bit-identical**, asserted continuously |
| Live result vs. official result on complete data | **bit-identical** |
| Convergence lag after backfill arrives | < 1 processing cycle (≤ 1 s) |
| Turnpoint/start/ESS timestamp accuracy | interpolated to sub-second between fixes |
| Replay of identical input | identical output, always |

### 2.3 Performance (secondary, and already satisfied)

**Measured**, on the reference implementation in `engine/`, over the 42 real tracks in this repository (176,393 fixes, CPython 3.12, one core of an M-series laptop):

| Metric | Target | **Measured** |
| --- | --- | --- |
| Single position, full hot path | < 100 µs | **0.54 µs** |
| 150 pilots, one second of input | < 20 ms | **0.08 ms** |
| Full recompute of one 4 h pilot track | < 1 s | **8 ms** |
| Engine CPU, steady state | < 5% of one core | **0.008%** |
| Point storage, 150 pilots × 4 h | < 500 MB | **69 MB** |

The per-fix figure is measured on pilots who progressed through at least two turnpoints, so it reflects the expensive path (start evaluation, cylinder crossing, distance, leading integral) rather than the cheap pre-start one.

These are 50–100× better than the estimates in an earlier draft. The conclusion is unchanged but much stronger: **the engine's cost is not a design constraint at any scale this competition will ever see.** Reproduce with `./run.py --bench`.

Latency is reported as p99. Note that end-to-end latency is dominated by tracker provider delay (10–60 s) and is outside the system's control; v1.0's "< 1 second end-to-end" requirement is withdrawn as unachievable and replaced by the convergence guarantee above.

---

## 3. Non-goals

- Replacing tracker providers.
- Horizontal scaling across machines. One process is sufficient at 150 pilots and will remain so at 1,000.
- Implementing every CIVL scoring system at once. GAP 2025 first.
- PostGIS on the live path (PostGIS remains for analysis and visualization).
- Sub-second end-to-end delivery. Not achievable; not the product.
- **Live airspace infringement detection** — explicitly out of scope for v1, noted here because its absence in v1.0 read as an oversight. If added later it needs a precompiled R-tree built at task load; the architecture supports it.

---

## 4. Core architectural principles

### 4.1 The point list is the state

Each pilot has one append-only, time-ordered list of positions in RAM. Everything else — waypoint progress, distance, score, events — is **derived** from that list and can be thrown away and rebuilt at any moment.

```text
PilotTrack.points  ──────►  score_pilot()  ──────►  PilotResult
   (source of truth)          (pure function)        (disposable)
```

Memory cost at target scale: 150 pilots × 4 h × 1 Hz × 32 bytes = **69 MB**. There is no reason to be clever about this.

### 4.2 Recomputation is cheap, so use it

A full recompute of one pilot's entire task is **8 ms** for a 4-hour track (measured, §2.3). Backfill events happen perhaps 2–5 times per pilot per task. Even at 150 pilots × 5 recomputes = 750 recomputes spread over 3 hours, that is 6 seconds of CPU in total — a rounding error.

This buys an enormous simplification: **there is no snapshot/rewind machinery, no incremental undo, no state versioning.** The exception path is one line — throw the derived state away and recompute from the point list.

### 4.3 One scoring function, two callers

```python
def score_pilot(task: CompiledTask, points: Sequence[Position], now: Timestamp) -> PilotResult
```

- **Official scoring** calls it once with the complete track.
- **Live scoring** calls it incrementally, and on any anomaly calls it in full.

The incremental path is a *cache* of this function, and the invariant is:

> `incremental_result == score_pilot(task, all_points_so_far, now)` — always.

This is asserted in tests on every real track, and sampled cheaply in production. v1.0's two-engine split made this class of bug undetectable until a protest.

### 4.4 The engine touches nothing

No database, no network, no clock, no logging framework, no serialization on the hot path. `engine/` imports only the standard library and `math`. It is testable and replayable with no infrastructure running.

### 4.5 Immutable input, versioned rules

Positions are immutable. Task definitions are snapshotted and hashed at compile time — a mid-competition task edit creates a new version rather than silently rewriting the meaning of already-recorded events. Scoring rules are versioned (`CIVL_GAP_2025`) and the version is stored on the task.

---

## 5. Architecture

```text
                      TRACKER PROVIDERS
        ┌───────────┬────────────┬───────────┬──────────┐
    Flymaster   LiveTrack24   XCTrack    Airtribune   Other
        └───────────┴────────────┴───────────┴──────────┘
                              │
                              ▼
                  ┌───────────────────────┐
                  │      INGESTION        │
                  │  provider adapters    │
                  │  auth / validate      │
                  │  normalize            │
                  └───────────┬───────────┘
                              │ normalized positions
                              ▼
                  ┌───────────────────────┐
                  │   Redis Stream        │   ← the durable ingest log
                  │   gps:comp:{id}       │      (also: crash recovery,
                  └───────────┬───────────┘       replay, decoupling)
                              │
              ┌───────────────┴───────────────┐
              ▼                               ▼
   ┌─────────────────────┐         ┌─────────────────────┐
   │  ARCHIVER           │         │   LIVE ENGINE       │
   │  consumer group     │         │   consumer group    │
   │  COPY batches       │         │                     │
   │  → flight_points    │         │  point lists (RAM)  │
   └──────────┬──────────┘         │  compiled geometry  │
              │                    │  state machines     │
              ▼                    │  GAP live scoring   │
        ┌──────────┐               └──────────┬──────────┘
        │ Postgres │                          │
        │ +PostGIS │◄────── async batch ──────┤
        └────┬─────┘        (250 ms)          │
             │                                ▼
             │                    ┌─────────────────────┐
             │                    │  Redis: snapshot    │
             │                    │  task:{id}:snapshot │
             │                    │  refreshed @ 1 Hz   │
             │                    └──────────┬──────────┘
             │                               │
             │                               ▼
             │                    ┌─────────────────────┐
             │                    │   FAN-OUT SERVICE   │
             │                    │   WebSocket, deltas │
             │                    └──────────┬──────────┘
             ▼                               ▼
     Official Scoring                  Live Dashboard
     (same score_pilot)                (marked PROVISIONAL)
```

### 5.1 Why these processes and no others

**Three processes:** ingestion (web-facing, scales with providers), engine (single, stateful, one per competition), fan-out (scales with spectators). The engine is deliberately isolated from both traffic sources that can spike.

**Why Redis Streams, specifically.** It is not for throughput — 150 msg/s needs no help. It earns its place for three things: (a) it is a durable, replayable log, so an engine restart resumes from the last acknowledged ID rather than losing state; (b) it decouples ingestion from the engine so a slow engine cannot drop tracker packets; (c) consumer groups let the archiver and the engine consume the same stream independently. If those three needs disappeared, Postgres + `LISTEN/NOTIFY` would be sufficient.

**Why the dual write is gone.** v1.0 had ingestion writing to both Postgres and Redis with no atomicity — a classic dual-write inconsistency, and it put a synchronous DB write in the ingest path. Now the stream is the only write from ingestion, and Postgres is a materialization of it. Recovery and replay both get simpler.

**Why no horizontal scaling.** v1.0 §22 designed task partitioning and multi-engine coordination. At 150 pilots one process handles every task in the competition simultaneously with CPU to spare. Removing this removes distributed locking, fencing tokens, split-brain leaderboards and a class of deploy-time bugs. A single-instance lease (`SET engine:comp:{id} {token} NX EX 30`, refreshed) is retained purely to prevent two engines running during a deploy.

---

## 6. Normalized position model

```python
@dataclass(frozen=True, slots=True)
class Position:
    pilot_id: int
    timestamp: int          # epoch seconds UTC (int: exact, hashable, fast)
    latitude: float
    longitude: float
    altitude_gps: int | None
    altitude_baro: int | None
    source: str             # provider id
    sequence: int | None
    received_at: int        # epoch seconds, when we got it — NOT used in scoring
```

Notes:

- `timestamp` is the **fix time**, `received_at` is arrival time. The gap between them is the backfill signal, and it is metadata only — it must never influence a scoring decision, or replay stops being deterministic.
- Integer epoch seconds rather than `datetime`: exact comparison, no timezone ambiguity, no object allocation on the hot path. The 1 Hz data makes sub-second resolution unnecessary for input (crossing *times* are still interpolated to fractional seconds — §9).
- Both altitudes are carried. GAP stopped-task scoring needs baro; the supplied IGC files record both (`B...A0057200624` = press 572 m, GPS 624 m). Which one scores is a task setting.
- Provider-specific fields (battery, signal, accuracy) stay in the ingestion layer and are stored separately. The engine must not know that Flymaster exists.

### 6.1 Per-pilot track

```python
class PilotTrack:
    pilot_id: int
    points: list[Position]     # append-only, sorted by timestamp
    _result: PilotResult       # derived cache
    _dirty_from: int | None    # index needing recompute, None = clean
```

---

## 7. Handling late, duplicate and out-of-order data

This is the heart of the design.

### 7.1 Classification

Every arriving position is classified against the pilot's existing points:

| Case | Condition | Action |
| --- | --- | --- |
| **In-order** | `ts > last_ts` | append; incremental update (hot path, >99% of traffic) |
| **Duplicate** | identical `(ts, lat, lon)` already present | drop, count metric |
| **Conflicting duplicate** | same `ts`, different position | keep the one with the lower `sequence`, else first-received; flag for diagnostics |
| **Late / backfill** | `ts < last_ts`, not present | **insert in order, mark dirty, recompute pilot** |
| **Implausible** | fails validation (§18) | quarantine table, never enters the point list |

### 7.2 The reorder buffer

Positions are held for a short grace window before being handed to the engine, and sorted by `(pilot_id, timestamp)` within it.

```text
Redis Stream ──► reorder buffer (default 10 s) ──► engine
```

This absorbs ordinary provider jitter — packets that arrive 2–5 s out of order because of retry or multi-path delivery — without ever triggering a recompute. It costs 10 seconds of added display lag against a provider delay that is already 10–60 s, so it is invisible to users. It is configurable per provider.

It does **not** absorb the 10–15 minute backfill case. Nothing sensibly could. That case is handled by recompute.

### 7.3 Recompute on backfill

When a pilot's 900-point backfill burst lands:

```python
def on_backfill(track: PilotTrack, new_points: list[Position]) -> PilotResult:
    insort_all(track.points, new_points)          # merge, keep sorted
    result = score_pilot(task, track.points, now) # full recompute — 8 ms worst case
    events = diff_events(track._result, result)   # what changed?
    track._result = result
    return result, events
```

Three consequences worth stating explicitly:

**Retroactive events.** The pilot may now have tagged TP4 at 12:14:37 — twelve minutes in the past. The emitted event carries the *fix timestamp*, not the arrival time. Downstream consumers must handle events arriving out of chronological order; the WebSocket protocol (§15) does this by sending state snapshots rather than an event log the client must fold.

**Rank changes are normal.** A pilot can jump 20 places when their backfill lands. The UI must present this as a correction, not an anomaly — see §17.

**Burst amortization.** A 900-point burst is merged and scored **once**, not 900 times. Batching backfill per pilot per cycle is what keeps the cost at 8 ms instead of seconds.

### 7.4 Why not incremental rewind

The obvious alternative — snapshot pilot state every 60 s and replay forward from the last snapshot before the backfill window — saves at most 8 ms of CPU per event, at the cost of snapshot storage, snapshot invalidation, a second code path that must stay consistent with the first, and a whole class of subtle bugs where the snapshot and the recompute disagree. At 150 pilots that trade is clearly wrong. **Recompute from scratch.**

If this ever needs to scale to several thousand pilots, the escape hatch is to snapshot at turnpoint boundaries (where state is naturally checkpointed) rather than on a timer. That is a later problem.

---

## 8. Task compilation and optimized route

### 8.1 Why this is the highest-risk component

The supplied `TASK 06 - AGER.xctsk` demonstrates it precisely:

```text
  0  TAKEOFF    D05    r =  1,000 m
  1  SSS        B048   r =  1,000 m     RACE, EXIT, gate 10:30Z
  2  TURNPOINT  B110   r =  1,000 m
  3  TURNPOINT  G23    r = 17,000 m     ← 17 km radius
  4  TURNPOINT  B108   r =  2,000 m
  5  TURNPOINT  D03    r =  1,000 m
  6  TURNPOINT  B009   r =  1,000 m
  7  TURNPOINT  B005   r =  1,000 m
  8  ESS        G01    r =  1,000 m
  9  GOAL       G01    r =    400 m     cylinder, deadline 14:00Z
```

| Measure | Distance |
| --- | --- |
| Center-to-center, SSS → goal | 91.42 km |
| **Optimized route, SSS → goal** | **46.07 km** |
| Error if center-to-center is used | **+45.35 km (+98%)** |

(46.07 km is with FAI tolerance applied to each radius; 46.36 km without.)

The 17 km G23 cylinder means a pilot need only clip its edge. Every distance-based scoring quantity — distance points, distance to goal, leading coefficient, the ranking of everyone who did not reach goal — is derived from the optimized route. v1.0 represented this as `update_distance(state)`, a single line of pseudocode. It is the component most likely to be wrong, and it must be built and validated first.

### 8.2 Compilation

Optimization runs **once** when the task is compiled, not per position:

```python
@dataclass(frozen=True, slots=True)
class CompiledTask:
    task_id: int
    task_hash: str                      # hash of the definition; stamped on every event
    scoring_version: str                # "CIVL_GAP_2025"

    waypoints: tuple[CompiledWaypoint, ...]
    optimized_route: tuple[Point, ...]  # one optimal point per cylinder
    remaining_distance: tuple[float, ...]  # cumulative from each wp to goal
    total_distance: float               # 46,360 m for Task 06

    start: StartConfig                  # type, direction, gates, deadline
    ess_index: int
    goal: GoalConfig
    tolerance: float                    # FAI: max(0.5% of radius, 5 m)

    proj: LocalProjection                # local equirectangular, task-centered
```

The optimizer is the standard iterative-projection method: place each point at its cylinder center, then repeatedly move each point to the position on its cylinder closest to the midpoint of its neighbours, until movement falls below 1 cm. It converges in a few hundred iterations for a 10-turnpoint task and takes milliseconds. It runs once per task, so its performance is irrelevant and its *accuracy* is everything.

### 8.3 The per-position calculation

With `remaining_distance` precomputed, distance-along-route for a pilot at the current position is:

```python
distance_flown = total_distance - (
    distance_to_next_cylinder_edge(pos, next_wp) + remaining_distance[next_wp_index]
)
```

Two square roots and a subtraction. This is what makes the hot path trivial while the geometry stays correct.

**Known approximation.** This uses the *task* optimum, not the optimum re-computed from the pilot's actual position. Real scorers re-optimize the remaining route from where the pilot actually is, which differs when a pilot is well off the optimal line. For the live leaderboard the fixed-route approximation is acceptable and is what most live systems use — but it **must not** be used for official scoring, where the remaining route is re-optimized per pilot. This is the one place where live and official legitimately differ, and it is stated here so it is a decision rather than a bug. The live number is marked provisional in the UI.

### 8.4 Projection

All geometry runs in a local **azimuthal equidistant** projection centered on the task. This turns every distance into a flat 2-D `hypot`, which is both faster and simpler to reason about than spherical trigonometry.

The choice of projection is not incidental, and an earlier draft of this document got it wrong. Measured against haversine over the real Task 06 envelope (27 km diagonal, 81 sample points, all pairs):

| Projection | Worst-case error |
| --- | --- |
| Equirectangular with fixed `cos(lat0)` | **53.89 m** |
| Azimuthal equidistant | **0.02 m** |

53.89 m is ten times the FAI tolerance on a 400 m goal cylinder — unusable. 0.02 m is free. Both are one function; there is no reason to take the cheap one.

Haversine is retained for compile-time work and for validating the projection in tests. The antimeridian and polar cases from v1.0's test list are not reachable at competition latitudes but are handled by rejecting task compilation outside the projection's validity envelope, rather than by pretending the flat approximation holds.

---

## 9. Geometry package

An isolated package with no dependencies beyond `math`. This is the natural place to drop to Rust/`pyo3` if it is ever needed — it will not be needed.

```python
# scalars
distance(a: Point, b: Point) -> float
bearing(a: Point, b: Point) -> float
point_on_bearing(a: Point, bearing: float, dist: float) -> Point

# cylinder tests
inside_cylinder(p: Point, c: Cylinder) -> bool
distance_to_edge(p: Point, c: Cylinder) -> float   # signed: negative inside

# crossings — return WHEN, not whether
enter_cylinder(prev: Fix, cur: Fix, c: Cylinder) -> Crossing | None
exit_cylinder(prev: Fix, cur: Fix, c: Cylinder) -> Crossing | None
cross_line(prev: Fix, cur: Fix, line: Line) -> Crossing | None
```

### 9.1 Crossings are tolerance-zone transitions, timed by tracklog point

**This section is a correction.** Earlier drafts argued that crossings must return an *interpolated* time, on the reasoning that at 1 Hz and 12 m/s a boolean costs up to a second of error on start and ESS. The physics is right; the rule disagrees. S7F 9.2.1:

> "Crossing time and altitude for each crossing is the time at which the corresponding tracklog point was recorded."

Official scoring uses the timestamp of the tracklog point at which the crossing is detected. Interpolating would produce a defensibly *better* number that disagrees with every other scorer — which is worse than useless. The engine uses the recorded point time.

S7F 9.2.1 also defines what a crossing *is*, and it is not "inside the cylinder":

> "A cylinder crossing is defined as crossing into or out of the turnpoint's tolerance zone, in any direction."

The zone is the annulus between `innerRadius` and `outerRadius`; a crossing is a transition across either boundary, in either direction.

```python
zone_crossing(prev, cur, cx, cy, inner, outer) -> (time, outward) | None
```

### 9.2 Tolerance is a flat ±5 m

**Also a correction.** Earlier drafts used `max(0.5% × radius, 5 m)`. S7F 9.1.1 for the 2026 edition sets:

```text
radiusTolerance   = 0.0%
absoluteTolerance = 5 m
innerRadius = min(r × (1 − radiusTolerance), r − absoluteTolerance)   = r − 5
outerRadius = max(r × (1 + radiusTolerance), r + absoluteTolerance)   = r + 5
```

The percentage term is **zero** in 2026, so tolerance is a flat ±5 m at every radius. On the 17 km G23 cylinder the 0.5% rule would have allowed 85 m — seventeen times too generous, and a plausible way to award a turnpoint nobody else awards.

### 9.3 Required tests

The v1.0 list, plus what it was missing:

- exact boundary; 1 cm inside; 1 cm outside
- high-speed crossing that fully traverses a small cylinder between two fixes (a 400 m goal cylinder at 25 m/s is entered and exited within 32 s — but a 5 s telemetry gap can hide it entirely; this must be detected as a chord intersection, not an endpoint test)
- entry and exit on the same segment
- tangential grazing (discriminant near zero)
- duplicate points, zero-length segments, identical timestamps
- **property-based** (Hypothesis): `distance(a,b) == distance(b,a)`; triangle inequality; a point at `distance_to_edge == 0` is on the boundary; interpolated crossing point is always within tolerance of the cylinder edge
- projection error vs. haversine bounded over the task envelope

---

## 10. Pilot state machine

```text
                    ┌─────────┐
                    │ WAITING │
                    └────┬────┘
                         │ altitude gain / speed threshold
                    ┌────▼─────┐
              ┌─────┤ AIRBORNE │
              │     └────┬─────┘
   re-start   │          │ valid start (§10.1)
   (later     │     ┌────▼────┐
    gate)     └─────┤ STARTED │
                    └────┬────┘
                         │ turnpoints, in order
                    ┌────▼────┐
                    │  TP n   │
                    └────┬────┘
                    ┌────▼────┐
                    │   ESS   │
                    └────┬────┘
                    ┌────▼────┐
                    │  GOAL   │
                    └─────────┘

  orthogonal:  tracking ∈ {ONLINE, LOST}      (never a task state)
  terminal:    LANDED_OUT, TASK_STOPPED, DEADLINE_EXPIRED
```

### 10.1 The start is not a single event

This is where v1.0's model breaks.

**Direction is not part of it — and getting this wrong cost real debugging time.** Earlier drafts of this document, and the first implementation, required the SSS crossing to match the task's declared `EXIT`/`ENTER`. That rule was withdrawn from the Sporting Code in 2020. S7F 6.2.1:

> "Note that the designation of 'enter' or 'exit' cylinder has been removed, to reduce a potential source of confusion and task setting errors... The direction in which such a crossing occurs is irrelevant. Task setters may still choose to indicate whether the start or subsequent turnpoint cylinders are 'enter' or 'exit', to explain their intended task route. **But pilots are not bound to those indications.**"

The 2020 change list puts it plainly: *"No more prescribed turnpoint direction (including start)."*

So the SSS is validated by **any** crossing of its tolerance band at or after a gate. `start_direction` is carried through the compiled task for display only, and scoring must never consult it.

This is the highest-cost thing in the engine to get wrong, because the failure is silent and misattributed: a task whose declared direction disagrees with how the field flew scores as *nobody started*, which looks exactly like a broken engine. On the Bassano task in this repository, honouring the declared `EXIT` put 3 pilots in goal; ignoring it, as the rules require, put 112.

**Gates.** `timeGates` may list several. A pilot who starts on the 10:30 gate and then re-enters and re-exits after the 10:45 gate has taken the later gate — and under race rules the **last valid start counts**. The state machine therefore moves *backwards*: `TP2 → STARTED`, with `start_time` rewritten and all downstream time-based scoring invalidated.

This means `start_time` is mutable until the rules freeze it, which means live speed and leading points for that pilot are provisional in a stronger sense than the rest. Combined with backfill (§7), a re-start can surface twelve minutes after it happened. The recompute-from-scratch design handles this for free: the start rule is simply re-evaluated over the whole point list. An incremental state machine would need explicit undo.

**Start deadline / goal deadline.** Task 06 has `goal.deadline = 14:00Z`. A pilot crossing goal after the deadline scores as landed at ESS distance. This is a virtual-clock event (§16), not a wall-clock one.

### 10.2 Turnpoints stay incremental

v1.0's core insight is correct and retained: only the *next* turnpoint is evaluated on the hot path. `TP1..TP3` already tagged are never re-tested during incremental processing.

The qualifier v1.0 missed: this holds for the incremental path only. During a recompute the whole sequence is re-derived, which is exactly what makes a re-start or a backfilled turnpoint correct.

### 10.3 Tracking state is orthogonal

```python
task_state:     WAITING | AIRBORNE | STARTED | TP_n | ESS | GOAL | LANDED_OUT | ...
tracking_state: ONLINE | LOST
```

A pilot can be `STARTED` + `LOST` (flying through a valley — the common case here) or `LANDED_OUT` + `ONLINE`. Conflating them, as a single enum would, is what makes live dashboards show pilots as "landed" when they are merely behind a ridge.

`LOST` is declared when `now - last_fix_time > threshold` (default 120 s, configurable per provider — a Flymaster and a phone app have very different silence profiles). It is derived from the virtual clock (§16), and it is **display metadata only**: it must never affect a scoring quantity, because it depends on delivery timing rather than on what the pilot flew.

---

## 11. Live scoring (GAP)

### 11.1 Ranking

The leaderboard is a sorted view over `PilotResult`, keyed by:

```text
1. goal reached      → by goal time (earliest first)
2. ESS reached       → by ESS time
3. otherwise         → by distance along optimized route (greatest first)
4. tie-break         → by pilot id (deterministic, never by dict order)
```

At 150 pilots the leaderboard is re-sorted from scratch every publish cycle. That is 150 items at 1 Hz — measurably free, and it removes an entire class of incremental-sort bugs. No heap, no incremental rank maintenance.

### 11.2 Leading coefficient — the paragliding form

GAP leading points come from the area under each pilot's distance-to-ESS curve, integrated from the first start gate. It is genuinely stateful and is the one scoring quantity that cannot be reconstructed from a final position — which is why it must be designed in from Phase 3 rather than bolted on.

**Paragliding does not use the hang-gliding formula.** Earlier drafts of this document assumed a single shared `d^(2/3)` accumulator. S7F 12.3.1 gives two different formulas:

```text
hang-gliding:   leadingArea = Σ (minToESS(tp_i-1)² − minToESS(tp_i)²) · taskTime(tp_i)
                LC          = (leadingArea + missingArea) / (1800 · speedSectionDistance²)

paragliding:    leadingArea = Σ minToESS(tp_i) · taskTime(tp_i) · ∫ weight(x) dx
                                                    over [done(tp_i-1), done(tp_i)]
                LC          = (leadingArea + missingArea) / (1800 · speedSectionDistance)

                done(p)          = 1 − minToESS(p) / speedSectionDistance
                weight(v)        = weightRising(1−v) · weightFalling(1−v)
                weightRising(v)  = (1 − 10^(9v−9))^5
                weightFalling(v) = (1 − 10^(−3v))^2
```

The weight function is zero at both ends of the speed section and peaks near 0.97 at about 30% along it. That shape is the whole point: paragliding rewards leading *through the middle of the task*, not merely leaving the start first or arriving first. Using the hang-gliding form would produce a plausible-looking leaderboard that is wrong for every pilot.

Two implementation consequences:

- `minToESS` is monotonically non-increasing by definition, so the integral only accrues where it *strictly decreases*. Storing only those points keeps per-pilot LC state small and the recompute cheap.
- `∫ weight(x) dx` is precomputed once as a cumulative table, turning each per-point integral into two array lookups.

Under recompute (§7) the integral is re-run over the point list, so backfill produces the correct LC rather than one with a 15-minute hole in it.

### 11.3 Points allocation and the paragliding differences

S7F 11 splits `1000 × TaskValidity` between distance, time, leading and arrival by goal ratio. The paragliding branch differs from hang-gliding in three ways, all implemented:

```text
GoalRatio      = NumberOfPilotsInGoal / NumberOfPilotsFlying
DistanceWeight = 0.9 − 1.665·GR + 1.713·GR² − 0.587·GR³

[PG] GoalRatio = 0 :  LeadingWeight = 1 − DistanceWeight      ← the whole share
[PG] GoalRatio > 0 :  LeadingWeight = (1 − DistanceWeight) · LeadingTimeRatio
[PG] ArrivalWeight = 0                                        ← always
     TimeWeight    = 1 − DistanceWeight − LeadingWeight − ArrivalWeight
```

`LeadingTimeRatio` defaults to **26%** in paragliding (17.5% in hang-gliding).

Other paragliding-only rules that a hang-gliding implementation gets wrong:

| S7F | Paragliding | Hang-gliding |
| --- | --- | --- |
| 12.4 | **No arrival points, ever** | awarded by ESS position |
| 12.1.1 | **No difficulty calculation** — distance points purely linear | applied |
| 9.4.1 | Best time counts only for pilots who reached **goal** | ESS is enough |
| 13.2 | ESS but not goal → **0%** of time points | 80% |
| 13.3 | Early start → scored launch-to-SSS distance only | "jump the gun" penalty |
| 13.4.2 | No minimum duration for a stopped task | `min(1 h, NominalTime)` |

### 11.4 Provisional quantities

Live GAP points require competition-wide values that are not final until the task ends: all three validities, the goal ratio that drives allocation, best distance, best time, and `LCmin`. Live scoring recomputes these each cycle over the current pilot set and publishes them as provisional. They move — sometimes substantially — as pilots land.

In particular, **the first pilot to reach goal changes the points allocation for the entire field**: `GoalRatio` jumps off zero and the leading share collapses from the whole non-distance budget to 26% of it. The UI must state this (§17).

---

## 12. Ingestion

Per-provider adapters normalize into `Position`. Each adapter owns authentication, its own wire format, and its own quirks; nothing provider-specific escapes into the engine.

Resolution chain for an incoming packet:

```text
provider + external_tracker_id
        → tracker record
        → pilot
        → active task for that pilot's competition
        → Redis stream gps:comp:{id}
```

**Multiple trackers per pilot.** A pilot may run a Flymaster and a phone app simultaneously, producing two position streams for one pilot that disagree. This was unaddressed in v1.0. Policy: each tracker has a priority; positions from a lower-priority tracker are used only when the higher-priority tracker has been silent for longer than its threshold. Merging is done in ingestion, before the stream, so the engine still sees exactly one series per pilot. Both raw series are archived.

**Active task resolution.** A tracker is bound to a pilot for a competition, not a task. Ingestion consults an in-memory active-task registry (refreshed on task open/close) to stamp `task_id`. Positions outside any active task window are archived but not streamed.

---

## 13. Redis usage

```text
gps:comp:{comp_id}            Stream  — ingest log; consumer groups "engine", "archiver"
                                        MAXLEN ~ 2,000,000 (a full comp day, ~48 h retention)
engine:lease:{comp_id}        String  — single-instance lease, SET NX EX 30, refreshed at 10 s
task:{task_id}:snapshot       String  — full leaderboard + pilot states, msgpack, rewritten @ 1 Hz
task:{task_id}:updates        PubSub  — delta frames for the fan-out service
```

The snapshot is a **single key holding the whole task state**, not a key per pilot as v1.0 proposed. At 150 pilots the whole snapshot is roughly 40 KB; one `GET` serves a client joining mid-task, instead of 150 round trips. New WebSocket clients get the snapshot, then deltas.

The engine's own RAM remains authoritative. Redis holds a published copy.

---

## 14. PostgreSQL

Durable store and the source for official scoring. Never on the live path.

### 14.1 `flight_points`

```sql
CREATE TABLE flight_points (
    task_id       int         NOT NULL,
    pilot_id      int         NOT NULL,
    timestamp     timestamptz NOT NULL,
    latitude      double precision NOT NULL,
    longitude     double precision NOT NULL,
    altitude_gps  smallint,
    altitude_baro smallint,
    source        text        NOT NULL,
    sequence      int,
    received_at   timestamptz NOT NULL,
    PRIMARY KEY (task_id, pilot_id, timestamp)
) PARTITION BY LIST (task_id);
```

Two deliberate changes from v1.0:

**Partitioned from day one**, not "eventually". 150 pilots × 4 h × 1 Hz = 2.16 M rows per task; a 6-task competition is 13 M rows. Retrofitting partitioning onto a live multi-gigabyte table mid-season is precisely the work nobody has time for. Partition by task — task boundaries are the natural query, retention and drop unit.

**The surrogate `id` is gone.** `(task_id, pilot_id, timestamp)` is the natural key, it is the primary access pattern, and it enforces the duplicate constraint for free via `ON CONFLICT DO NOTHING`. A `bigserial` on 13 M rows buys an index that nothing queries.

Written by the archiver in `COPY` batches of ~1,000 rows or every 250 ms, whichever first.

### 14.2 Other tables

```text
competitions   id, name, location, timezone, start_date, end_date, status
tasks          id, competition_id, task_number, task_type, definition jsonb,
               task_hash, scoring_system, scoring_version,
               start_gates[], goal_deadline, stop_time, status
task_waypoints id, task_id, sequence, type, name, lat, lon, radius, geometry(PostGIS)
pilots         id, competition_id, competition_number, name, country, glider, class
trackers       id, pilot_id, provider, external_id, priority, status
pilot_task_state   pilot_id, task_id, ... (derived; batch-written every 250 ms)
task_events    id, task_id, pilot_id, type, fix_time, received_at, task_hash, payload jsonb
rejected_points    quarantine — see §18
```

`tasks.definition` stores the full task JSON (the `.xctsk` as supplied) and `task_hash` its hash. Every event carries the hash, so a mid-competition task edit can never silently reinterpret existing events.

`pilot_task_state` is derived and disposable — if it is lost, replay rebuilds it from `flight_points`.

---

## 15. WebSocket fan-out

Under-designed in v1.0, and now the largest compute cost in the system.

Naive fan-out is 150 pilots × 1 Hz × N spectators. At 2,000 spectators that is 300,000 pilot-updates/second of serialization — **four orders of magnitude more work than the scoring engine**. The engine is not the scaling problem; this is.

Design:

- **Fixed-rate frames, not per-event.** One delta frame per second per task, regardless of input rate. Serialized **once** and sent to every subscriber as the same bytes.
- **Deltas against the last frame.** Only pilots whose displayed state changed. A pilot in a coverage hole contributes nothing.
- **Binary encoding** (msgpack), with positions quantized to ~1 m (`int32` micro-degrees) and altitudes to metres. Roughly 12 bytes per pilot per frame: a full 150-pilot frame is ~2 KB, a typical delta a few hundred bytes.
- **Separate process**, subscribing to Redis pub/sub. A spectator surge cannot backpressure or crash the engine.
- **Snapshot on join**, then deltas — one Redis `GET` (§13), not a replay of the event log.
- **Optional viewport filtering** for map clients at high zoom. Not needed at 150 pilots; the hook exists.

Because backfill produces retroactive events (§7.3), the protocol sends **state**, not an event log the client folds. A client that has missed frames re-fetches the snapshot rather than trying to reconcile. This makes correction-after-backfill trivially correct on the client.

---

## 16. Determinism and the virtual clock

v1.0 claimed determinism (§4.3) while making tracking-loss, gate expiry and task-stop depend on `now`. Replaying the same input would produce different `TRACKING_LOST` events on every run, so replay could not be used to validate live behaviour — which is most of its value.

**Rule: nothing under `engine/` may call `time.time()` or `datetime.now()`.**

The engine has two inputs:

```python
engine.process(position: Position)   # data
engine.tick(now: int)                # time
```

In production, `tick` is driven by a 1 Hz timer. In replay, ticks are synthesized from the data timeline and can run at 1x, 100x or as fast as the CPU allows. Every time-dependent decision — tracking loss, gate opening, goal deadline, task stop — is evaluated inside `tick`, from the supplied value.

This is a small decision now and a rewrite later. It is also what makes §19's degradation harness possible at all.

---

## 17. Live vs. official, and correction in the UI

The live leaderboard is provisional in three distinct ways, and the UI should distinguish them rather than showing one undifferentiated disclaimer:

1. **Incomplete data** — a pilot is in a coverage hole and their shown position is stale. Show the fix age; grey the row past the tracking-loss threshold.
2. **Provisional validity** — GAP task/distance/time validity are computed over pilots still flying and will move as pilots land (§11.3).
3. **Approximated route** — live distance uses the fixed task optimum; S7F 9.3 defines scored distance as `taskDistance − min(RouteOptimizer(remaining route))` evaluated at every point, i.e. re-optimized per point (§8.3).
4. **Allocation shift on first goal** — every pilot's points change when the first pilot reaches goal (§11.4). This is the largest single discontinuity a spectator will see, and it is correct.

Rank corrections after backfill are **expected behaviour** and must be presented as such. A pilot jumping 20 places when their 15-minute gap fills in is the system working correctly. Animating the move and briefly marking the row ("data recovered") turns what would look like a glitch into visible evidence that the system is honest.

```text
LIVE — PROVISIONAL              task in progress
 1  Pilot 17    998 pts   ESS 13:14:02
 2  Pilot 31    992 pts   ESS 13:14:41
 3  Pilot 08    981 pts   46.2 km      ⟳ data recovered 12:47
 4  Pilot 22    944 pts   44.8 km      ⚠ no signal 6 min
```

---

## 18. Validation and security

Tracker endpoints authenticate per provider. Adapters validate identity, task association and plausibility.

Rejected outright, into `rejected_points`, never into the point list:

```text
latitude outside [-90, 90]           timestamp before task window - 1 h
longitude outside [-180, 180]        timestamp in the future (> now + 60 s)
altitude outside [-500, 12000] m     unknown tracker / unmapped pilot
implied ground speed > 120 km/h      duplicate (ts, pilot) with conflicting position
implied climb rate > 20 m/s          malformed / unparseable
```

Speed and climb checks compare against the pilot's previous *accepted* point. Two considerations:

- After a 15-minute gap the implied speed across the gap is meaningless — the check is skipped when the interval exceeds 60 s, otherwise every backfill burst would be rejected at its boundary. This interacts directly with §7 and is easy to get wrong.
- A single wild outlier followed by good data would otherwise reject the good data too. The check therefore compares against a short median of recent points, not the single previous one.

Quarantined points are retained for diagnostics and can be manually reinstated, which then triggers a recompute for that pilot (§7.3) — the same mechanism as backfill.

---

## 19. Testing strategy

The competition data in this repository makes the central test possible, and it is the reason to build the harness first.

### 19.1 The degradation harness

The 48 IGC files are **complete ground truth**: 1 Hz, no meaningful gaps. Live telemetry is a degraded version of exactly this. So:

```text
  IGC file (perfect)
        │
        ├──────────────────────────────► score_pilot()  ──►  REFERENCE RESULT
        │                                                          │
        └──► degrade():                                            │
                drop 5% of fixes                                   │
                delay by 10–60 s                                   │
                blackout 10–15 min, then burst                     │
                reorder within ±5 s                                │
                duplicate 1%                                       │
             ──► synthetic live stream                             │
                     │                                             │
                     └──► LIVE ENGINE ──► LIVE RESULT  ═══════════╪══► MUST MATCH
                                                                   ▼
```

**The assertion:** after the last point is delivered, the live result equals the reference result exactly — every turnpoint time, the start time, ESS time, distance, leading coefficient, and rank.

This single test exercises backfill, reordering, duplicate handling, retroactive events, recompute correctness, and live/official agreement, against 42 real pilots flying a real task with a 17 km cylinder. It is worth more than the rest of the test suite combined.

Degradation is seeded, so every failure is reproducible.

### 19.2 Golden-file regression

Score the supplied task from the IGC files and diff against the published official results for `Cto-espana-Cat-Sport 2026-08-08 Task 06`. Extend to 10–20 historical tasks with published FS/Airscore results.

Agreement with real published results for real tasks is the only thing that actually demonstrates the scoring is right. Everything else demonstrates the code does what its author expected.

Tolerances: turnpoint times exact to the second; distances within 10 m; GAP points within 1.

### 19.3 Unit and property tests

Geometry as in §9.3. State machine transitions including the re-start and exit-start cases from §10.1, which no synthetic happy-path test will catch.

### 19.4 The invariant test

For every real track, at every prefix length, assert:

```python
incremental_result(points[:n]) == score_pilot(task, points[:n], t_n)
```

This is the §4.3 invariant made executable. It is the test that catches incremental-path drift, which is otherwise invisible until a protest.

Implemented as `./run.py --verify --igc <dir>`, together with the rest of the invariant set (`engine/invariants.py`): the arithmetic closes, no component exceeds its S7F 11 allocation, ordering follows the times, every scored time is a real tracklog timestamp (S7F 9.2.1 turned into a test), scoring is deterministic, a mid-task board agrees with the final one, scored distance never decreases as the clock advances, and the parallel path matches the serial one bit for bit.

### 19.4.1 A check must not share the mechanism it checks

This is the lesson of v2.1's optimizer bug (§0.1) and it is a requirement, not a style note.

The route optimizer was 2.0 km long. The test for it perturbed each optimized point around its own cylinder and confirmed nothing shorter existed. It passed — necessarily, because coordinate descent terminates exactly where no single point can improve, and the check also moved one point at a time. The check and the bug shared an assumption, so the check could only ever agree.

Every verification in this system is therefore labelled with what it can establish, and the labels are what make the suite honest:

| Level | Claim | Strength |
| --- | --- | --- |
| A | Matches a worked example published in S7F | Strong; independent of this code |
| B | Two implementations of the same thing agree | Strong for optimizations; silent if the shared rule is wrong |
| C | The result satisfies a property it must satisfy | Catches assembly bugs; blind to a uniformly wrong rule |
| D | An independent algorithm reaches the same answer | Strong; this is what found the optimizer bug |

The optimizer now carries a level-D check: an exact shortest-path DP over discretized cylinders, at a resolution the optimizer itself does not use, sharing no code with it.

The two places where level A is *not* available — the leading-coefficient integral (S7F 12.3.1) and the exact wording of the early-start rule (S7F 13.3) — are recorded as such in VERIFICATION.md §6 rather than left to look verified. Cross-checking a task against FS or Airscore is the highest-value verification work remaining.

### 19.5 Task/data consistency check

Before a task is scored — live or offline — verify that the task definition actually describes the flight the data contains: for each cylinder, what fraction of the field ever entered it, and the fleet's median closest approach.

This is not a test, it is an operational pre-flight check, and it earns its place because wrong radii, wrong waypoints and the wrong task file entirely all happen on a competition morning, and all of them present as "the scoring is broken". It found a real mismatch in the data supplied with this design (§23.2) in under a second.

A start cylinder with 0% reach, or any scored cylinder in low single digits, means the task and the data disagree. Fail loudly rather than publishing a leaderboard where nobody started.

Implemented as `./run.py --check`.

### 19.6 Performance

Benchmark at 150, 500 and 1,000 pilots — the last two only to confirm headroom, not because they are required. Report p50/p95/p99, RSS, and recompute cost at end-of-task track length. Run before building infrastructure around the engine (v1.0 had this right).

Two numbers, not one, and they have different owners:

* **Live incremental** — one fix, full hot path: **0.56 µs**, so 150 pilots at 1 Hz is 0.01% of one core. This was never at risk and no work is warranted on it.
* **Cold full-field publish** — process start to printed board, nothing parsed: **0.73 s** for 129 pilots and 1.4 M fixes across 8 cores (3.79 s single-process). This is the one a human waits on, after the last tracklog is uploaded or a task is corrected, and it is the one worth optimizing.

`./run.py --bench` reports both, and measures the second by launching the process rather than estimating it from the first.

Measure from local disk. On a network or synced filesystem, reading the tracklogs dominates everything: the same 40 MB took 0.09 s locally and over 100 s from a mediated directory during this work, which is 130× the entire scoring cost.

---

## 20. Project structure

```text
livescoring/
│
├── engine/                     # stdlib only — no db, no redis, no clock, no framework
│   ├── geometry/
│   │   ├── projection.py       # local equirectangular
│   │   ├── primitives.py       # distance, bearing
│   │   ├── cylinder.py         # containment, signed edge distance
│   │   ├── crossing.py         # analytic crossings returning interpolated time
│   │   └── tests/
│   │
│   ├── task/
│   │   ├── xctsk.py            # .xctsk parser
│   │   ├── models.py
│   │   ├── optimizer.py        # ◄ highest-risk component (§8)
│   │   └── compiler.py
│   │
│   ├── pilot/
│   │   ├── track.py            # PilotTrack, append + insert-in-order
│   │   ├── state.py
│   │   └── state_machine.py    # incl. exit start, gates, re-start
│   │
│   ├── scoring/
│   │   ├── score_pilot.py      # ◄ THE function (§4.3)
│   │   ├── incremental.py      # the cache over it
│   │   ├── leading.py          # LC accumulator
│   │   └── civl/gap_2025.py
│   │
│   ├── events/
│   ├── replay/
│   │   ├── runner.py
│   │   └── degrade.py          # ◄ the harness (§19.1)
│   └── live_engine.py          # process() + tick()
│
├── ingestion/
│   ├── providers/              # flymaster, livetrack24, xctrack, ...
│   ├── merge.py                # multi-tracker priority (§12)
│   ├── validate.py
│   └── normalizer.py
│
├── archiver/                   # stream → COPY → flight_points
├── persistence/
├── transport/                  # redis stream, reorder buffer
├── fanout/                     # websocket service (§15)
├── api/
└── tests/
    ├── unit/  integration/  golden/  performance/
    └── data/                   # IGC + xctsk fixtures
```

`engine/` must remain importable and fully testable with nothing else installed and nothing running.

---

## 21. Implementation plan

Ordered so that the highest-risk work happens first and every later phase has real data to validate against.

**Phase 1 — Geometry + projection.** Primitives, cylinder tests, analytic crossings returning interpolated times. Full unit and property test suite. *Exit: §9.3 passes.*

**Phase 2 — Task parsing + route optimizer.** Parse `.xctsk`. Implement the optimizer. *Exit: Task 06 optimizes to 46.07 km ±10 m; verified independently against XCTrack's own figure.* This is the riskiest component; it is done second and validated against an external source.

**Phase 3 — IGC replay harness.** Parse IGC, feed tracks through a skeleton engine. *Exit: all 48 files parse; 176,533 fixes replay without error.* Deliberately moved ahead of the state machine — real input to develop against beats a simulator.

**Phase 4 — State machine + `score_pilot()`.** Full task progression: exit start, gates, re-start, turnpoints, ESS, goal, deadline. Pure function, no infrastructure. *Exit: plausible per-pilot results for all 42 pilots on Task 06.*

**Phase 5 — GAP 2026 scoring + leading coefficient.** *Exit: golden-file agreement with published official results for Task 06 (§19.2).* This is the milestone that proves the system works. Everything before it is scaffolding; everything after it is delivery.

**Phase 6 — Incremental path + degradation harness.** Add the incremental cache and the recompute-on-backfill path (§7). Build `degrade.py`. *Exit: §19.1 converges and §19.4 holds for all 42 pilots.*

**Phase 7 — Benchmark.** 150 / 500 / 1,000 pilots. *Exit: numbers in §2.3 confirmed, before any infrastructure is built around the engine.* (Phases 1–5 and this benchmark are implemented — see `README.md`.)

**Phase 8 — Ingestion + Redis transport + archiver.** Providers, validation, reorder buffer, stream, `COPY` batching. *The engine does not change in this phase* — if it does, the boundary was wrong.

**Phase 9 — Persistence of derived state + crash recovery.** Batch writes; restart-and-resume from the stream; verify recovered state matches uninterrupted state.

**Phase 10 — Fan-out + dashboard.** Snapshot/delta protocol, WebSocket service, provisional-state UI (§17).

**Phase 11 — Official scoring path.** Re-optimized per-pilot route (§8.3), full GAP validity, results publication.

The system is genuinely useful from Phase 5 (offline scoring of completed tasks from IGC) and live from Phase 10.

---

## 22. Key design decisions

| # | Decision | Rationale |
| --- | --- | --- |
| 1 | Point list is the source of truth; all state derived | Makes recompute trivial, so backfill is correct by construction |
| 2 | **Recompute the whole pilot on late data** | 0.4 s worst case at 150 pilots; removes all snapshot/rewind machinery |
| 3 | **One `score_pilot()` for live and official** | Makes live/official divergence a testable assertion, not a discovery |
| 4 | Optimized route computed once at compile time | 49% error on the real task if skipped; free per-position afterwards |
| 5 | Crossings return interpolated times, not booleans | 1 Hz × 12 m/s = up to 1 s of error on start and ESS |
| 6 | Start time is mutable; state machine can move backwards | Exit starts, multiple gates, last-valid-start rule |
| 7 | Engine never reads a wall clock; `process()` + `tick()` | Determinism was claimed but unachievable in v1.0 |
| 8 | Tracking state orthogonal to task state | Otherwise pilots behind a ridge display as landed |
| 9 | Redis Stream is the only write from ingestion | Removes v1.0's dual write; Postgres becomes a materialization |
| 10 | Single engine process; no partitioning | 0.5% of a core at target scale; removes distributed-systems failure modes |
| 11 | One snapshot key per task, not per pilot | One `GET` to join, not 150 |
| 12 | Fan-out is a separate process, fixed-rate binary deltas | It is 10,000× the engine's cost; must not be able to affect it |
| 13 | `flight_points` partitioned by task from day one | 13 M rows per competition; retrofitting is not an option mid-season |
| 14 | Reorder buffer absorbs jitter; recompute absorbs backfill | Two different problems, two different mechanisms |
| 15 | Golden-file agreement with published results is the gate | The only evidence that the scoring is actually right |
| 16 | Task definition hashed and versioned | A mid-competition edit must not reinterpret recorded events |
| 17 | Crossing time is the tracklog point time, not interpolated | S7F 9.2.1 — a better number that disagrees with every other scorer is worse than useless |
| 18 | Cylinder tolerance is flat ±5 m | S7F 9.1.1 — `radiusTolerance` is 0.0% in the 2026 edition |
| 19 | Paragliding leading coefficient is weighted, not the HG squared form | S7F 12.3.1 — a different formula, not a different constant |
| 20 | Every PG/HG divergence flagged `[PG]` in code with the HG variant beside it | Makes the discipline difference auditable instead of invisible |
| 21 | Enter/exit is advisory; any crossing validates a control zone | S7F 6.2.1 — prescribed direction was removed in 2020 |
| 22 | Task definitions are sanity-checked before scoring | A gate after the deadline, or a zone nested inside the SSS, presents as "nobody started" |
| 23 | Turnpoint types are inferred, not required | `.xctsk` types are optional: first point is takeoff, last is goal, absent ESS means ESS is goal |
| 24 | Elevated goal scales time points by arrival altitude | S7F 13.1 — a pilot can be inside the goal cylinder but below it |

---

## 23. Open questions

1. **Competition parameters are required input.** S7F 5 requires Nominal Distance, Minimum Distance and Nominal Time to be set *before the first task*. Every validity number is meaningless without them, and none are in the `.xctsk`. The engine currently assumes nominal distance = task distance and says so on screen; these must come from the competition setup.
2. **The supplied task file does not match the supplied tracklogs — blocking.** `TASK 06 - AGER.xctsk` describes `D05 → B048 → B110 → G23 → B108 → D03 → B009 → B005 → G01`. The 42 tracklogs in `Cto-espana-Cat-Sport_2026-08-08_igcs.zip` show **0/42 pilots entering the B048 start cylinder** and 1/42 entering G23 (a 17 km cylinder — essentially impossible to miss if it were on the route), while pilots clearly fly `D05 → B108 → B110 → …` — the reverse of the task's `B110 → G23 → B108`. These tracklogs are from a different task. The correct `.xctsk` for 2026-08-08 is needed before Phase 5 can be validated. Run `./run.py --check` to reproduce.

3. **Official results source for golden files.** Confirm published results for that task are available in FS/FSDB or Airscore format for the §19.2 diff.
4. **Which providers first?** Determines Phase 8 scope. The merge policy in §12 only matters if pilots really do run two trackers.
5. **Task files cannot be trusted as supplied.** Across three real task files in this repository: one had a start gate six hours after its own goal deadline, one declared a direction the entire field contradicted, and one described a different day entirely. The engine now sanity-checks the definition and compares the declared gate against the field's own crossing histogram (§19.5) — but a competition needs the *as-flown* task, which is not always what gets exported.

6. **Airspace.** Confirmed out of scope for v1 (§3) — worth re-confirming, since adding it later touches task compilation.
7. **Stopped tasks.** Score-back window and altitude bonus are specified in GAP but need a decision on whether v1 supports them or refuses to score a stopped task live.
