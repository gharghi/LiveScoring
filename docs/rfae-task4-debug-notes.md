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

Using `WGS84` matches the official geometry:

- Total: `76.382 km`
- Speed section: `71.338 km`
- Take-off to ESS: `75.382 km`

Conclusion: RFAE/SVL task exports must be converted to `WGS84`, not
`FAI_SPHERE`.

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

## Current comparison after those fixes

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
- ESS/final time: exact for all 17 goal pilots
- Speed section elapsed: exact for all 17 goal pilots
- Average speed: within tolerance for all 17 goal pilots
- Remaining failures:
  - Scored distance: `41/50` within ±0.05 km
  - Distance points: `34/50` within ±0.1 pt
  - Leading points: `0/38` within ±0.1 pt
  - Total score: `3/50` within ±0.1 pt

Largest confirmed distance mismatches:

- Pilot 13 Fermin Montaner Morant:
  - Engine: `30.82 km`
  - Official: `35.70 km`
  - Official tooltip: `109 m from sector #4 [B02090] @ 13:53:26`
- Pilot 4 Alberto Restifo:
  - Engine: `29.59 km`
  - Official: `27.10 km`
  - Official tooltip: `1.3 km from sector #4 [B02090] @ 13:47:16`

## Distance root cause found

The engine currently scores outlanding distance with a fixed remaining table:

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

Pilot 13 still does not fully match:

- Current approximation: `30.818 km`
- Exact remaining-route recomputation while B02090 is still unvalidated:
  `33.981 km`
- Exact remaining-route recomputation if B02090 is credited:
  `34.049 km`
- Official: `35.70 km`

So pilot 13 needs one additional check: either SVL's `HUMP_V2A` best-progress
curve changes credited progress near the sector, or SVL is applying another
distance-progress adjustment that this engine does not yet implement.

## Leading-points status

The source-backed leading fixes above improved correctness, but official SVL
leading points still do not match:

- Available leading pot is exact: `127.7`
- Engine leading is systematically higher than official for many pilots.
- Example:
  - Pilot 22: engine `31.7`, official `15.4`

The remaining mismatch is likely linked to SVL's `HUMP_V2A` progress curve and
the exact route/distance-to-ESS curve used in the leading coefficient. It should
not be patched with an arbitrary scale factor; the next implementation step is
to add the exact remaining-route/progress calculation and rerun leading from
that data.

## Next debug steps

1. Implement exact remaining-route scoring for best-progress anchors instead of
   using the fixed remaining table for outlanding distance.
2. Keep the hot path efficient by recomputing the exact remaining route only
   when a fix becomes a new best-progress candidate, not on every fix.
3. Re-run the RFAE Task 4 comparison.
4. If pilot 13 still differs, reverse-engineer/apply SVL `HUMP_V2A` using the
   official `Best progress coefficient = 1.41713`.
5. Recompute leading coefficients from the corrected distance-to-ESS/progress
   curve and compare again.
