# RFAE Campeonato Sport Task 4 debug notes

Fixture:

- Official index: `https://scoring.rfae.es/campeonato-sport/`
- Official task: `https://scoring.rfae.es/campeonato-sport/task4.html`
- Local replay directory: `rfae_downloads/run_1787836207`
- Task date: `2026-08-07`
- Source timezone: `Europe/Madrid`
- Track data: 50 IGC files, 988,903 fixes

## Official task distances

The official page reports:

- Speed Section distance: `71.33 km`
- Take-off to ESS distance: `75.37 km`
- Total task distance: `76.37 km`

The original generated `task.xctsk` used `FAI_SPHERE`, which made the task about
0.18% short:

- Total: `76.236 km`
- Speed section: `71.190 km`
- Take-off to ESS: `75.236 km`

Using only `WGS84` gets close but still prints one centi-kilometre high:

- Total: `76.382 km`
- Speed section: `71.338 km`
- Take-off to ESS: `75.382 km`

The official SVL 1.158 page lines up with the older relative tolerance
convention:

```text
outer = max(radius * 1.001, radius + 5 m)
measurementRadius = outer
```

That gives:

- Total: `76.3734256 km` → `76.37 km`
- Speed section: `71.3302658 km` → `71.33 km`
- Take-off to ESS: `75.3734020 km` → `75.37 km`

Conclusion: RFAE/SVL task exports must be converted to `WGS84` and must carry
task-local radius tolerance metadata. The engine default remains the current
flat `±5 m` tolerance for native inputs.

## Official GAP parameters

The official HTML and extracted metadata agree on these task parameters:

- Minimum distance: `5.0 km`
- Nominal distance: `40.0 km`
- Nominal time: `90 min`
- Leading-time ratio: `26.0%`
- Nominal launched pilots: `96.0%`
- Nominal pilots in goal: `30.0%`
- Progress curve: `HUMP_V2A`
- Real Leading Points: `false`
- Best progress coefficient: `1.41713`

A sweep over nominal distance and nominal time did not change this fixture's
point pots:

- Distance validity: `1.000000`
- Time validity: `1.000000`
- Task validity: `1.000000`
- Available distance: `508.9`
- Available time: `363.4`
- Available leading: `127.7`

Conclusion: nominal distance and nominal time are not the cause of the current
mismatch for this task. The task is fully valid either way.

## Fixes already applied

- Corrected the leading-factor denominator.
  - Wrong: `(LC - LCmin)^2 / sqrt(LCmin)`
  - Correct/source-backed: `(LC - LCmin)^2 / LCmin`
- Switched leading `maxTime` default to the field-wide value used by
  GlideComp:
  - `min(max(lastOutlanding, lastESS), taskDeadline)`
- Added a conservative landing cutoff so continued recording after landing does
  not credit retrieve-car movement.
- Added segment-crossing validation for telemetry gaps where both endpoints are
  outside a cylinder but the segment crosses the cylinder.
- Added scoring/landing metadata to stored rankings and OpenAPI output.
- Updated the RFAE replay converter to write `earthModel: WGS84`.
- Added task-local tolerance parsing:
  - `radiusTolerance`
  - `absoluteTolerance`
  - `measurementRadius`
- Updated the RFAE replay converter to write SVL-compatible tolerance fields.
- Added an exact S7F 9.3 remaining-route pass for landed-out distance:
  - `taskDistance - optimizeRemainingRoute(position, unreached zones, goal)`

## Current comparison after distance/time fixes

Command:

```bash
PYTHONPYCACHEPREFIX=/tmp/livescoring-pycache .venv/bin/python run.py \
  --task rfae_downloads/run_1787836207/task.xctsk \
  --igc rfae_downloads/run_1787836207/igc \
  --gate 11:00 --deadline 15:00 \
  --nominal-distance 40 --min-distance 5 --nominal-time 90 \
  --leading-time-ratio 26 --workers 1 \
  --compare /private/tmp/rfae_task4_official.tsv --tz 2 --no-color
```

Result summary:

- Matched local IGC rows: `50/56`
- Official-only pilots without local IGC: `7`, `19`, `31`, `45`, `52`, `59`
- Pilots in goal: exact (`17`)
- Available distance/time/leading points: exact (`508.9 / 363.4 / 127.7`)
- Best distance flown: engine `76,373 m`, official `76,370 m`, delta `+3 m`
- Scored distance: `50/50` within ±0.05 km, mean absolute delta `0.005 km`
- ESS/final time: exact for all 17 goal pilots
- Speed section elapsed: exact for all 17 goal pilots
- Average speed: within tolerance for all 17 goal pilots
- Remaining failures:
  - Distance points: `47/50` within ±0.1 pt; worst `-0.20 pt`
  - Leading points: `1/38` within ±0.1 pt
  - Total score: `12/50` within ±0.1 pt

Distance and time are now considered verified for this fixture. The remaining
distance-points noise is a small displayed-rounding/denominator issue, not a
route-distance miss.

## Distance root cause found

The original engine scored outlanding distance with a fixed remaining table:

```text
remaining = distance_to_next_cylinder_edge + precomputed_remaining[next_wp]
flown = task_distance - min(remaining)
```

That approximation is fast, but it is not always the official GAP/SVL model.
For a land-out position, the correct model is:

```text
remaining = optimized route from the pilot's actual fix through all un-reached
            control zones to goal
flown = task_distance - remaining
```

GlideComp explicitly documents and implements this as `optimizeRemainingRoute`.
That matters on Task 4 because B02090 is a very large 24 km cylinder. The
optimal remaining route depends on the pilot's actual position, not only on the
next waypoint index.

Prototype result:

- Pilot 4:
  - Current approximation: `29.595 km`
  - Exact remaining-route recomputation: `27.068 km`
  - Official: `27.10 km`

This confirms the distance bug for pilot 4.

After implementing this exact pass, all 50 matched pilots are within ±0.05 km
of the official distance. The pass keeps the hot state machine as an
approximation, then re-measures progress after the timing/sector decisions are
known.

## Leading-points status

The remaining mismatch is leading coefficient/progress-curve handling:

- Available leading pot is exact: `127.7`
- Official `Best progress coefficient`: `1.41713`
- Engine weighted S7F 2026 LCmin: `0.60160`
- Official full-leading pilot: `50` Daniel Gonzalez Rizo
- Engine weighted S7F 2026 LCmin pilot: `5` Alejandro Martin Acuña

Existing-system checks:

- GlideComp's current 2026 implementation uses the modern weighted integral
  form and the same final leading-points curve.
- AirScore's `Gap.pm` uses the same final leading-points curve:
  `1 - ((LC - LCmin) / sqrt(LCmin))^(2/3)`.
- AirScore also has a historical branch that takes `LCmin` only from pilots who
  reached ESS when at least one pilot reached ESS.
- Flare Timing exposes both area families:
  - `Leading1Area`: `∫ d dt`, normalized by `1800 * SS`
  - `Leading2Area`: `∫ d² dt`, normalized by `1800 * SS²`

SVL Task 4 reports `Progress curve = HUMP_V2A` and `Real Leading Points =
false`. Probing against the official table shows:

- A HUMP-style point-weighted progress area, using exact distance-to-ESS
  samples, reproduces all goal-pilot leading points to rounding.
- The same formula gives LCmin around `1.418`, matching the official
  `1.41713`, and selects pilot `50` as LCmin.
- Landed-out pilots still need SVL-specific handling. Obvious tail variants
  (`field max time`, `last ESS`, `own landing`, `remaining field time`) do not
  reproduce every landout row.

Therefore the remaining issue should not be solved by a fitted scale factor.
The next change should be a named/configurable `HUMP_V2A` leading mode with the
verified goal-pilot behavior, followed by a focused landout-tail implementation
backed by SVL/Flare/AirScore behavior.

## Current comparison after HUMP_V2A implementation

The engine now has an opt-in task field:

```json
"progressCurve": "HUMP_V2A"
```

Latest Task 4 comparison:

- Task distances remain matched:
  - Speed section: `71.33 km`
  - Take-off to ESS: `75.37 km`
  - Total task: `76.37 km`
- Pilots in goal: exact (`17/17`)
- Available distance/time/leading points: exact (`508.9 / 363.4 / 127.7`)
- Best distance: engine `76,373 m`, official `76,370 m`, delta `+3 m`
- Scored distance: `50/50` within ±0.05 km
- ESS/final time: `17/17` exact
- Speed section elapsed: `17/17` exact
- Average speed: `17/17` within tolerance
- Time points: exact
- Distance points: `47/50` within ±0.1 pt, worst `-0.20 pt`
- Leading points: `16/38` within ±0.1 pt, mean absolute delta `5.932 pt`
- Total score: `20/50` within ±0.1 pt, mean absolute delta `4.514 pt`

The HUMP_V2A midpoint progress area fixes the leading-coefficient family and
matches goal-pilot leading to display rounding. The remaining error is confined
to landed-out leading coefficients. The tested source-backed tails were:

- no landout tail
- own last-task-time tail
- last-ESS tail
- field max-time tail
- task-deadline tail
- AirScore-style replacement of task-close tail with last-arrival tail

None reproduces all SVL landout rows. The current implementation keeps the
closest identified source-backed behavior as opt-in and documents it as
unfinished for SVL landouts.

## Next debug steps

1. Add a task/competition parameter for `progressCurve`, defaulting to the
   current S7F weighted mode for native inputs.
2. Implement `HUMP_V2A` as a separate leading-coefficient mode, not by changing
   the modern weighted formula globally.
3. Preserve the verified exact S7F 9.3 distance/time behavior.
4. Re-run Task 4 and require:
   - official task distances unchanged: `71.33 / 75.37 / 76.37 km`
   - scored distance still `50/50` within ±0.05 km
   - ESS/final time still `17/17` exact
   - goal-pilot leading remains exact
5. Resolve the landed-out HUMP tail from source behavior or another clean SVL
   fixture before treating the total score as fully matched.
