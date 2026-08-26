# Live Scoring Engine — proof of concept

Implements Phases 1–5 and 7 of [DESIGN.md](DESIGN.md): geometry, task
compilation with route optimisation, IGC replay, the pilot state machine, GAP
scoring, and the benchmark.

**Rules: FAI Sporting Code Section 7F, 2026 edition V1.0 — paragliding.**
Section 7F covers both disciplines and marks the differences by colour (blue =
hang-gliding, orange = paragliding). Every paragliding-specific rule is
implemented and flagged `[PG]` in `engine/gap.py`, with the hang-gliding
variant recorded alongside it so the difference is auditable.

**No dependencies.** Python 3.10+, standard library only. Nothing to install.

## Live pipeline: one canonical input and output

`run.py` remains the offline scorer. For a live integration, use the two
separate processes below. The **feed gateway** is the only component that
knows about Voolando, IGC files, or any other provider. It emits append-only
`live-scoring.v1` events. The **scoring service** accepts only that event
format and publishes `live-score.v1` snapshots.

Terminal 1 — start the continuous scoring consumer:

```bash
touch events.jsonl
./scoring_service.py --events events.jsonl --snapshots snapshots --watch
```

Terminal 2 — replay the supplied files as a live feed (300× real time):

```bash
./feed_gateway.py --task samples/Task.xctsk --comp competition.json \
  --igc samples/igc --out events.jsonl --competition-id cto-sport-2026 --speed 300
```

For an immediate, non-delayed import, omit `--speed`. The latest result is
written to `snapshots/latest.json`; each published source sequence also creates
an immutable numbered snapshot in `snapshots/`. The service batches 25 input
events per published snapshot by default; use `--publish-every 1` for every
event. Stop either process with
Ctrl-C. Start a new replay with a fresh `events.jsonl` and empty `snapshots/`
directory, or use `--append` only when continuing the same stream.

The canonical event and snapshot contracts, idempotency rules, replay behaviour
and future-provider boundary are specified in [ARCHITECTURE.md](ARCHITECTURE.md).
The corresponding diagram is [live-scoring-architecture.drawio](live-scoring-architecture.drawio).

## Frontend API and SQLite snapshots

The FastAPI delivery service tails the same canonical event log, recalculates
the current board, and persists a `live-score.v1` snapshot in SQLite every
second. Install its two runtime dependencies once:

```bash
python3 -m pip install -r requirements.txt
```

Then, with `events.jsonl` being produced by the feed gateway, run:

```bash
python3 -m uvicorn api_app:app --host 0.0.0.0 --port 8000
```

It writes `live-scoring.sqlite3` by default. Set `LIVE_SCORING_EVENTS`,
`LIVE_SCORING_DB`, or `LIVE_SCORING_PUBLISH_SECONDS` to change the event file,
database path, or one-second persistence interval. The frontend handoff is in
[FRONTEND_API.md](FRONTEND_API.md); interactive OpenAPI documentation is at
`http://localhost:8000/docs`.

```bash
./run.py                          # FINAL result — competition is over
./run.py --seconds 2400           # SIMULATION — 2400 s after the start gate
./run.py --at 12:15               # SIMULATION — at a wall-clock moment
./run.py --live --speed 300       # animated replay
./run.py --check                  # task/tracklog match + start-gate analysis
./run.py --gate 11:30             # override a wrong start gate
./run.py --deadline 17:00         # override a wrong goal deadline
./run.py --elevated-goal 300      # goal on high ground (S7F 13.1)
./run.py --explain "SURNAME"      # full audit trail for one pilot
./run.py --compare official/T2.3.tsv --tz 2    # diff vs a published result
./run.py --rules                  # every scoring rule, its file, its status
./run.py --penalties penalties.json            # S7F 13.5 (default if present)
./run.py --verify                 # formulas vs published S7F values
./run.py --verify --igc igcs      # ...plus invariants over the real field
./run.py --bench                  # engine throughput + cold publish time
./run.py --json out.json          # machine-readable result
./run.py --top 15 --no-color      # trim / pipe-friendly
./run.py --serial                 # one process instead of all cores
```

## Answering a protest — `--explain`

```bash
./run.py --explain "GHARGHI"                  # name, surname, or IGC filename
./run.py --explain "GHARGHI" --json audit.json
```

Prints the complete derivation of one pilot's score: every input by SHA-256,
tracklog integrity and every gap over 5 s, the takeoff fix, **every** crossing
of the SSS tolerance zone with the two distances that bracket it and the S7F 8.1
rule that picked one, every control zone with its validating fix and
`d(prev)`/`d(fix)`, the distance subtraction, the leading-coefficient inputs,
the field-wide half of the score labelled as such, and every points line as
formula → numbers → result with its S7F reference.

The evidence comes out of `score_pilot()` itself via an optional trace argument,
so there is no second implementation that could describe a different calculation
from the one that produced the number — `--verify` asserts that tracing changes
no scored value. The audit also re-reads the pilot's IGC and replays it, and says
so in red and exits non-zero if the replay disagrees with the leaderboard.

## Checking the engine — `--verify`

The tests live in **`tests/`**, one module per Sporting Code section, and run
two ways — `./run.py --verify` and `python3 -m tests`. Same registry, same
checks, so there is one suite and two doors into it.

```bash
python3 -m tests                    # 203 checks, no tracklogs needed
python3 -m tests 12 13              # just Sections 12 and 13
python3 -m tests --list             # what suites exist
python3 -m tests --igc igcs --gate 11:30 --deadline 15:00   # + field invariants
```

203 of the checks need no data at all: formulas against worked examples
published in S7F itself (Table 2, Figure 16, Figure 18), synthetic flights with
a known right answer, and unit tests for every geometry primitive. Whenever
tracklogs are supplied, 25 more run over that real field — the arithmetic
closes, nothing exceeds its S7F 11 allocation, every scored time is a real
tracklog timestamp (S7F 9.2.1 turned into a test), scoring is deterministic, a
mid-task board agrees with the final one, distance never goes backwards, and the
parallel path matches the serial one bit for bit. **228 in total.**

Every check prints its detail whether it passes or not. A green tick tells you
nothing; "111 pilots in goal, LCmin 0.4676" tells you what the engine did. More
than one number that turned out to be wrong sat in a passing check's detail line
for a long time before anyone read it.

## Checking against a published result — `--compare`

```bash
./run.py --gate 11:30 --deadline 15:00 --compare official/T2.3.tsv --tz 2
```

Diffs the engine against an officially published result, pilot by pilot, matched
on the pilot ID that is also the IGC filename. It separates the task-wide numbers
(pilots in goal, the three point pots, best distance) from the per-pilot ones
(distance, finish time, speed, each points component), because a pot that is
wrong moves the whole field at once and should not be reported 129 times.

`official/T2.3.tsv` is the published result for Bassano del Grappa 2026 Task
T2.3, the competition this repository's tracklogs come from. This is the only
check that reaches outside the codebase, and it is the one that has found the
most: see VERIFICATION.md §5.

**[VERIFICATION.md](VERIFICATION.md)** is the report: what each level of checking
can and cannot establish, the eight defects found so far — one worth 8.3 % of
every distance in the competition, one that made every scored distance 5.9 km
short — and, at length, what is still *not* verified. The leading coefficient
does not reproduce the official result and is documented as open.

**Two modes.** With no time flag the competition is treated as finished and
every fix is scored — the header reads `FINAL`. Pass `--seconds N` (seconds
since the first start gate) or `--at HH:MM` to simulate a moment mid-task —
the header reads `SIMULATION` and the board is marked `PROVISIONAL`.

## Inputs

| Flag | Default | Accepts |
| --- | --- | --- |
| `--igc` | `igcs` | a directory, a `.zip`, or a single `.igc` |
| `--task` | `TASK 06 - AGER.xctsk` | XCTrack `.xctsk` |
| `--comp` | `competition.json` | competition configuration |

## Competition configuration

Scoring parameters are **not** in the code and **not** in the `.xctsk` — they
live in `competition.json`, because they change per competition and per task.

```json
{
  "name": "Cto. de Espana Cat. Sport 2026",
  "discipline": "paragliding",
  "placeholders": ["nominal_distance_km", "minimum_distance_km", "nominal_time_min"],
  "nominal_distance_km": 60.0,
  "minimum_distance_km": 5.0,
  "nominal_time_min": 90.0,
  "pilots_present": null,
  "leading_time_ratio": 0.26,
  "ess_no_goal_time_factor": 0.0,
  "nominal_goal": 0.30,
  "nominal_launch": 0.96,
  "tasks": {}
}
```

The three values under S7F 5 — nominal distance, minimum distance, nominal
time — **must** be set by the meet director before the first task. Every task
validity number is a function of them. The shipped file contains placeholders,
and lists them in `"placeholders"`; the engine prints a warning naming them on
every run until you delete them from that list. Remove a name once the value
is real.

Turnpoint types in a `.xctsk` are optional and often missing. The engine
infers them by convention: the **first** point is the takeoff when it precedes
the SSS, the **last** point is always goal, and an absent ESS means the speed
section ends at goal.

`"discipline"` switches the whole rule set: `paragliding` or `hang-gliding`
select the leading-coefficient formula, arrival points, the difficulty
calculation, the ESS-but-not-goal factor and the LeadingTimeRatio default.
`"tasks"` holds per-task overrides, since S7F 11 sets LeadingTimeRatio per
task:

```json
"tasks": { "TASK 06 - AGER": { "leading_time_ratio": 0.20 } }
```

Bad configs are rejected rather than silently ignored — unknown keys, an
unrecognised discipline, or a LeadingTimeRatio outside 0–26%.

Individual values can still be overridden for a one-off run with
`--nominal-distance`, `--min-distance`, `--nominal-time`,
`--leading-time-ratio` and `--present`.

Defaults are the files in this directory; override with `--task` and `--igc`.

## Status of the supplied data

`TASK 06 - AGER.xctsk` **does not match** the tracklogs in
`Cto-espana-Cat-Sport_2026-08-08_igcs.zip`. `./run.py --check` shows 0/42
pilots entering the B048 start cylinder and 1/42 entering the 17 km G23
cylinder, while the fleet clearly flies `D05 → B108 → B110 → …`. The correct
task file is needed to validate scoring.

`reconstruct_task.py` infers the flown route from the tracklogs so the engine
and display can be demonstrated end to end. Its output is labelled
**RECONSTRUCTED** everywhere and is a diagnostic, never a scoring input.

```bash
python3 reconstruct_task.py
./run.py --task TASK-RECONSTRUCTED.xctsk
```

## Layout

```
engine/          stdlib only — no db, no redis, no clock, no framework
  geo.py         projection, FAI tolerance-zone crossings
  task.py        .xctsk parsing, FAI tolerance, route optimiser
  igc.py         IGC parsing, dir/zip loading, multi-session merge
  score.py       state machine + score_pilot()  ← the one scoring function
  scoring.py     the task-level pipeline: calls the rules below in order
  rules/         ONE FILE PER RULE / ALGORITHM  ← start here to review
    # A. geometry and distance — what the points formulas measure
    s7f_71_algorithms.py S7F 7.1    ALL NINE ALGORITHMS IN ONE FILE, under the
                                    Code's own names, for diffing against 7.1
    earth_model.py       —          FAI sphere, projection, haversine  ← WRONG,
                                    7.1 specifies the WGS84 ellipsoid
    cylinder.py          S7F 9.1.1  tolerance zones
                         S7F 9.2.1  crossing detection and crossing time
    route.py             —          shortest route through the cylinders
    distance_flown.py    —          distance still to fly / distance flown
    start_selection.py   S7F 8.1    which SSS crossing is the start
    # B. scoring — once the whole field is known
    params.py            S7F 5      competition parameters
    distance.py          S7F 9.3    scored distance
    validity_launch.py   S7F 10.1   launch validity
    validity_distance.py S7F 10.2   distance validity
    validity_time.py     S7F 10.3   time validity
    allocation.py        S7F 11     points allocation
    points_distance.py   S7F 12.1   distance points
    points_time.py       S7F 12.2   time points
    points_leading.py    S7F 12.3   leading points   ← SUSPECT, see the file
    points_arrival.py    S7F 12.4   arrival points   (none, in paragliding)
    elevated_goal.py     S7F 13.1   underflying an elevated goal
    ess_no_goal.py       S7F 13.2   ESS but not goal
    early_start.py       S7F 13.3   early start
    penalties.py         S7F 13.5   penalties
    stopped.py           S7F 10.4 / 13.4   NOT IMPLEMENTED
    ftv.py               S7F 16            NOT IMPLEMENTED
  gap.py         an index re-exporting rules/ under the old names
  geo.py         likewise, for the geometry
  comp.py        competition configuration
  audit.py       per-pilot audit record  ← --explain
  parallel.py    the same scoring, across every core
tests/           THE TEST SUITE — one module per section, runs standalone
  __init__.py          the registry: what suites exist
  __main__.py          python3 -m tests
  test_s7f_07_algorithms.py     S7F 7.1
  test_s7f_09_control_zones.py  S7F 9
  test_s7f_10_task_validity.py  S7F 10
  test_s7f_11_allocation.py     S7F 11
  test_s7f_12_pilot_points.py   S7F 12
  test_s7f_13_special_cases.py  S7F 13
  test_geometry.py              earth model, cylinders, route, distance
  test_penalties.py             the penalty file: parsing and matching
  test_start_selection.py       S7F 8.1, incl. synthetic flights
  test_registry.py              the rule registry and the stubs
  test_field_invariants.py      properties over a real scored field
competition.json competition parameters  ← edit this, not the code
penalties.json   S7F 13.5 penalties, per task
official/        published results to compare against
samples/         a SECOND competition's tracklogs and task
leaderboard.py   terminal rendering
explain.py       renders an audit record
compare.py       diffs against a published result
run.py           CLI
VERIFICATION.md  what is checked, what is not, and what was found
reconstruct_task.py   diagnostic
```

## Reviewing the rules — `--rules`

```bash
./run.py --rules
```

Every scoring element, in pipeline order, with its Sporting Code reference, the
file it lives in, and its status — `implemented`, `SUSPECT`, or
`NOT IMPLEMENTED` — followed by an honest note on what actually verifies each
one. "Implemented" is not the same claim as "verified", and neither is the same
as "matches a published result", so each row says which it has earned.

`engine/rules/` is one file per rule for exactly this reason: when a published
result disagrees, the useful question is never "is the scoring right" but
"which of the fifteen calculations is wrong". Each file holds one pure function,
the rule as text, the paragliding/hang-gliding difference where there is one,
and the traps that particular rule has already caused. They are meant to be read
next to the Code.

### S7F 7.1 — the nine algorithms

`engine/rules/s7f_71_algorithms.py` holds all nine of S7F 7.1 in **one file**,
under the Code's own names and in the Code's own order, so the whole
specification can be diffed against the Code in one sitting:

| | | |
|---|---|---|
| 1 | `GeodesicToCartesian` | 7.1.1 |
| 2 | `PathFinder` | 7.1.3 |
| 3 | `CartesianToGeodesic` | 7.1.1 |
| 4 | `DirectGeodesic` | — |
| 5 | `InverseGeodesic` | — |
| 6 | `EllipsoidDistance` | 7.1.5 |
| 7 | `FindTaskAreaCentre` | 7.1.6 |
| 8 | `ProjectionCorrection` | 7.1.7 |
| 9 | `RouteOptimizer` | 7.1.8 |

The names, numbering and stated purposes are the Code's. **The bodies are not
transcribed from the Code** — they are standard implementations of the stated
purposes, and every place the Code could reasonably specify something else
carries a `CHECK THIS` marker. Diff against those markers first.

The geodesics are verified against Vincenty's own published test line (0.1 mm),
and against the analytic equator degree, the meridian arc and the quarter
meridian. `--verify` runs all of it.

`--rules` covers **25 elements in two groups**: seven geometry and distance
algorithms (A1–A7) and seventeen scoring rules (1–17). 19 are implemented and
checked. The six that are not:

| | | |
|---|---|---|
| A1 | Earth model | **wrong** — S7F 7.1 specifies the WGS84 ellipsoid; the engine uses the FAI sphere. Worth 144 m of route on this task |
| A4 | Segment through a cylinder | **known gap** — a segment jumping clean over a small cylinder is not detected |
| 10 | S7F 12.3 leading points | **wrong** — does not reproduce the official result, VERIFICATION.md §5.3 |
| 6, 15 | S7F 10.4 / 13.4 stopped tasks | not implemented |
| 17 | S7F 16 FTV | not implemented (competition-level) |

Two of those — A1 and A4 — are in the geometry group, which is where the
remaining distance gap against the official result lives. `--verify` measures
that gap on every run rather than recording it in a document:

```
✗ engine route == S7F 7.1 RouteOptimizer (WGS84 + correction)
    engine 59,647.0 m (FAI sphere, measured in the plane) vs
    7.1    59,791.2 m (WGS84, corrected, measured on the ellipsoid)
    → -144.2 m (-0.241%)
```

Against the official 59,900 m that would take the shortfall from −0.42 % to
−0.18 %. Switching has not been done, because it is a scoring change resting on
7.1 text I have not seen.

`engine/` is importable and fully testable with nothing else running. That is
the property that makes replay, the degradation harness (DESIGN.md §19.1) and
official scoring all reuse the same code.

## Measured

129 pilots, 1,429,368 fixes, CPython 3.12, 8 cores:

| | |
| --- | --- |
| **Cold full-field publish — process start to printed board** | **0.73 s** |
| Same, single process (`--serial`) | 3.79 s |
| Scoring loop only, points already in memory | 796 ms |
| Per fix, full hot path | **0.56 µs** |
| 150 pilots at 1 Hz, incremental | **0.01% of one core** |
| Worst-case single-pilot recompute (18,637-fix track) | **10 ms** |

The 10 ms figure is what makes DESIGN.md §7's central decision — recompute a
pilot from scratch whenever late data arrives — obviously correct rather than
merely defensible.

The live requirement was never in doubt at half a microsecond per fix. The row
that needed work is the first: the cold recompute a scorer waits on after the
last tracklog is uploaded, or after a task correction, or a crash. Pilots are
independent, so it runs across every core (`engine/parallel.py`), and `--verify`
asserts the parallel board is identical to the serial one, pilot by pilot and in
rank order.

`./run.py --bench` prints all of these and measures the cold publish by
launching the process three times rather than estimating it.

> If your working directory is on a network or synced filesystem, reading the
> tracklogs will dominate every number above — on the machine this was developed
> on the same 40 MB took 0.09 s from local disk and over 100 s from the project
> directory. Benchmark from local disk.

## Paragliding rules implemented

| S7F | Rule | Paragliding | Hang-gliding |
| --- | --- | --- | --- |
| 9.1.1 | Cylinder tolerance | flat ±5 m (`radiusTolerance` = 0.0%) | same |
| 9.2.1 | Crossing time | the tracklog point's timestamp, **not** interpolated | same |
| 6.2.1 | Enter/exit direction | **advisory only, never scored** — any crossing validates | same |
| 13.1 | Elevated goal | time points scaled by arrival altitude | same |
| 9.4.1 | Best time counts | only pilots who reached **goal** | ESS is enough |
| 12.1.1 | Difficulty calculation | **not applied** — distance points are linear | applied |
| 12.2 | Time points | `1 − ((t−best)/√best)^(5/6)` | same |
| 12.3.1 | Leading coefficient | weighted integral, `weight(v)` | squared-area form |
| 12.4 | Arrival points | **none, ever** | awarded |
| 11 | LeadingTimeRatio | 26% | 17.5% |
| 11 | Nobody in goal | leading takes the whole non-distance share | LeadingTimeRatio of it |
| 13.2 | ESS but not goal | **0%** of time points | 80% |
| 13.3 | Early start | scored launch→SSS distance only | "jump the gun" penalty |
| 8.1 | Re-starting | multi-gate races and time trials only | same |

`./run.py --verify` checks these against worked examples published in S7F
itself — Table 2, Figure 13, Figure 18 and the fixed parameters — rather than
against a snapshot of this implementation. The leading coefficient (12.3.1) is
the exception and the weak spot: its *weight function* is checked against Figure
18, but the integral that consumes it has no published worked example to check
against. See VERIFICATION.md §6.

## Known corrections

Three defects were found by the checks described above and fixed. Two changed
scores; VERIFICATION.md §4 has the detail.

* **The route optimiser was 8.3 % long.** Task distance 58,435 m → 53,966 m,
  moving every pilot's distance, speed, distance points and leading coefficient.
  It placed each turnpoint toward the midpoint of its neighbours, which is the
  correct minimiser only for equal-length legs. Worse, the obvious check — a
  single-point perturbation search — *passed* on the wrong answer, because it
  shared the "one point at a time" assumption with the bug. An independent
  shortest-path DP found it.
* **`launch_to_sss` exceeded the straight-line distance** between the two
  cylinders (6,510 m across a 6,122 m gap). Affects [PG] S7F 13.3 early
  starters. Now the optimised cylinder-to-cylinder distance, 3,022 m.
* **Two pilots' scored distance went backwards mid-task**, at the moment
  provisional early-start credit gave way to a valid start. Live display only;
  final scores unaffected.

## Penalties — S7F 13.5

Penalties are decided by the meet director and the jury; they are not derived
from any tracklog, so they are an input to scoring rather than an output of it.

```json
{ "Task": [ {"pilot": "1380", "percent_own": 100, "reason": "AIRSPACE"} ] }
```

`penalties.json` is read automatically if present. Pilots are matched by the ID
that is also the IGC filename, or by name. `percent_own`, `percent_task` (of
1000 × task validity) and flat `points` are all supported, all subtractive, and
the total is floored at zero. Penalties apply **last**, to the already-rounded
total, because a percentage penalty has to be a percentage of something final.

`reason` is required on every entry — a deduction a pilot cannot see the reason
for is one they cannot contest — and it appears in `--explain` as its own
section and on the leaderboard footer.

A penalty naming a pilot who is not in the field is reported loudly, never
silently skipped.

## Not yet implemented

Stopped tasks (S7F 10.4 and 13.4) and FTV (16) — `./run.py --rules` lists them,
and `engine/rules/stopped.py` and `ftv.py` describe what each would involve
rather than being absent. The incremental path and degradation harness
(Phase 6); ingestion, Redis, persistence, fan-out (Phases 8–10).
