# Verification report

**Engine:** paragliding live scoring, FAI Sporting Code Section 7F, 2026 edition V1.0
**Data:** `Task.xctsk` + 129 IGC tracklogs, 1,429,368 fixes, task day 2026‑05‑26
**Reproduce everything in this document with one command:**

```bash
python3 -m tests                                                # 203, no data needed
./run.py --verify  --igc igcs --gate 11:30 --deadline 15:00     # 228 internal checks
./run.py --compare official/T2.3.tsv --tz 2 \
         --igc igcs --gate 11:30 --deadline 15:00               # vs the published result
```

The tests live in `tests/`, one module per Sporting Code section, driven by a
registry. `--verify` and `python3 -m tests` run the identical set; the second
exists so the suite can be run without the scorer, and one section at a time.

Those two commands are the report. This file explains what each checks, what
neither checks, and what they found.

Both `--gate` and `--deadline` are UTC and both are corrections to the task
file, which declares a start gate after its own deadline and a deadline in local
time. See §5.

**Headline:** every component now matches the officially published result except
the leading coefficient, which does not and is documented as an open item in
§5.3.

Each element is now a separate file under `engine/rules/` — seven geometry and
distance algorithms, then seventeen scoring rules — so each can be checked
against the Code on its own: `./run.py --rules` lists all 24 with their status. That split is the
direct consequence of §5 — when a published result disagreed, the useful
question turned out to be never "is the scoring right" but "which of the
fifteen calculations is wrong", and the code was not shaped to answer it.

---

## 1. What "correct" can and cannot mean here

There are four different claims that get muddled together under "the scoring is
correct", and they have very different strengths. This engine separates them,
and every check below is labelled with which one it is.

| Level | Claim | How it is established | Strength |
| --- | --- | --- | --- |
| **A** | A formula matches the one published in S7F | Compared against worked examples printed in the Sporting Code itself — Table 2, Figure 13, Figure 18 | Strong. Independent of this code. |
| **B** | Two implementations of the same thing agree | Reference vs optimised: regex IGC parser vs byte‑offset parser, `geo.zone_crossing` vs its inlined copy, one process vs eight | Strong for *optimisations*. Says nothing about whether the shared rule is right. |
| **C** | A result satisfies a property it must satisfy | Invariants over the real field: arithmetic closes, nothing exceeds its allocation, ordering follows the times, distance never goes backwards | Strong at catching assembly bugs. Cannot catch a rule that is uniformly wrong. |
| **D** | Independent algorithm reaches the same answer | Route optimiser cross‑checked against a shortest‑path DP that shares no code with it | Strong. This is what found the largest bug. |
| **E** | The whole result matches an officially published one | `--compare` against `official/T2.3.tsv`, pilot by pilot | Strongest available. Another implementation, another operator, real data. |

Level E is not a superset of the others. An official table is produced by
another program (FS or Airscore), run by a scorer, with manual adjustments and
penalties applied afterwards — a disagreement means one of us is wrong, and it
does not say which. What it *is* good at is finding the thing no internal check
can: a rule read wrongly but read wrongly consistently.

---

## 2. Result

```
✓ 227 of 228 checks pass       (the 1 failure is the deliberate sphere/ellipsoid divergence)
✗ 10 of 15 comparisons outside tolerance      ./run.py --compare official/T2.3.tsv --tz 2
```

**Level A is now available**, and it changes the headline. The officially
published result for this task (Bassano del Grappa 2026, Task T2.3) is in
`official/T2.3.tsv`, and `--compare` diffs against it pilot by pilot, matched on
the pilot ID that is also the IGC filename. §5 is that comparison.

The tolerances are deliberately tight — every pilot within 0.1 points, 1 second,
50 m — and a category fails if a single pilot misses, so the count is not the
story. What matches **exactly** is the population, all three point pots and the
implied LeadingTimeRatio. What remains traces to just four root causes (§5.3–5.5
and the ±1 s crossing time), one of which is substantial and is in exactly the
place §7 predicted.

* 203 checks needing no data: formulas against published S7F values, synthetic
  flights with a known answer, and unit tests for every rule and geometry
  primitive
* 25 invariants over the real 129‑pilot field (levels B, C, D)

Wall time for the whole suite: **≈ 17 s**, of which most is the million‑segment
geometry comparison and re‑scoring the field several times over.

---

## 3. What the suite checks

### Level A — against published S7F values (68 checks)

| S7F | What is checked |
| --- | --- |
| 9.1.1 | Tolerance zone is a flat ±5 m at every radius from 100 m to 17 km (`radiusTolerance` is 0.0 % in the 2026 edition) |
| 10.1 | LaunchValidity at 0 %, 48 %, 96 %, 100 % launched — the curve, not just its endpoints |
| 10.2 | DistanceValidity at 0, ½ and full Area, arithmetic worked from the published definition |
| 10.3 | TimeValidity at 25 %, 50 %, 75 % and 100 % of nominal time |
| 11 | Allocation weights at 0 %, 10 %, 25 %, 50 %, 75 %, 90 % and 100 % in goal — distance, leading and time, all three |
| 11 [PG] | With nobody in goal, leading absorbs the **whole** non‑distance share (hang‑gliding would take only LeadingTimeRatio of it) |
| 12.1.1 [PG] | Distance points are linear — sampled at five points, so a difficulty‑adjusted implementation fails |
| 12.2 | SpeedFraction against Table 2: 1:00 best → 80 % at 1:08:42, 50 % at 1:26:07, 0 % at 2:00:00, and the 2:00 and 3:00 rows |
| 12.3 | LeadingFactor is exactly 1 at LCmin and clamps at 0 |
| 12.3.1 | Leading weight curve against Figure 18 (0 %, 10 %, 30 %, 100 %); the cumulative‑integral lookup table agrees with direct Simpson integration to 1e‑6 |
| 12.4 [PG] | Arrival weight is zero at every goal ratio |
| 13.1 | GoalAltitudeFactor curve: 0.8 at and below goal altitude, exactly 1.0 at +elevation, 0.975 halfway |
| 8.1, 9.2.1, 13.3 | Seven hand‑built synthetic flights with a known right answer — normal start, never entered, early start, ENTER start, two‑gate restart, single‑gate no‑restart, and EXIT‑vs‑ENTER giving identical results (6.2.1) |

### Levels B/C/D — over the real 129‑pilot field (21 checks)

```
✓ inlined zone test == geo.zone_crossing (1 M random segments)          [B]
✓ fast IGC parser == regex reference, field by field                     [B]
✓ parallel field score == serial field score, bit for bit                [B]
✓ every tolerance zone is radius ±5 m exactly                            [A]
✓ projection agrees with haversine to < 0.5 m across the task            [D]
✓ optimised route is optimal (perturbation + independent DP search)      [D]
✓ weights sum to 1 and the pots sum to 1000 × taskValidity               [C]
✓ total = distance + time + leading, rounded to 0.1                      [C]
✓ no component exceeds its allocation                                    [C]
✓ total ≤ 1000 × taskValidity                                            [C]
✓ further flown ⇒ never fewer distance points                            [C]
✓ faster speed section ⇒ never fewer time points                         [C]
✓ best distance and best time take the full pot                          [C]
✓ LCmin scores the full leading pot                                      [C]
✓ event times are in task order for every pilot                          [C]
✓ scored distance ≤ task distance; goal ⇒ full distance                  [C]
✓ every scored time is a real tracklog timestamp (S7F 9.2.1)             [C]
✓ scoring is deterministic — same points, bit-identical result           [C]
✓ --explain tracing changes no scored value                              [B]
✓ live (truncated points) == official (full points, scored as at T)      [C]
✓ scored distance is monotone in time (7 checkpoints)                    [C]
```

Three of these deserve a note.

**"every scored time is a real tracklog timestamp"** is S7F 9.2.1 turned into a
test. The Code says the crossing time *is* the timestamp of the tracklog point,
not an interpolated value. The engine contains an interpolating crossing routine
(`geo.touches_cylinder`) that would give a better live estimate and a different
official result; this asserts the scoring path never reaches for it, on every
crossing of every pilot.

**"live == official"** scores each pilot twice — once from the full track with a
`now` of T, once from a track physically truncated at T — and requires identical
results. This is the property that stops a live board from being a number that
gets quietly corrected afterwards.

**"scored distance is monotone in time"** re‑scores every pilot at seven points
through the task and requires their distance to never fall. A board that moves a
pilot backwards is the most visible live‑scoring failure there is.

**"faster speed section ⇒ never fewer time points"** compares time points
*before* the S7F 13.1 elevated‑goal factor, because 13.1 deliberately breaks the
raw ordering: on this task 40 of 111 finishers carry a factor below 1, and a
slower pilot who crossed goal high should and does finish above a faster one who
underflew it. The check states the 12.2 curve, which must be monotone, and not
the display order, which need not be.

---

## 4. What the suite found

Three real defects, all found by the checks above rather than by reading the
code, and all fixed. Two of them changed the scores.

### 4.1 The route optimiser was 8.3 % long — every distance‑based score was wrong

**Found by:** level D, the independent shortest‑path DP.
**Impact:** task distance 58,435 m → **53,966 m**. Every pilot's distance,
speed, distance points and leading coefficient moved.

`optimise()` placed each turnpoint on the rim of its cylinder in the direction of
the *midpoint of its neighbours*. That is the correct minimiser only when the two
legs happen to be equal length; the actual condition on a circle is the
reflection law. Correcting that got 58,435 → 55,965 m.

That was still 2.0 km long, and — importantly — **the perturbation check passed
on it**. Coordinate descent moves one point at a time, so it stopped at an
arrangement no single point could improve, and a single‑point perturbation search
agrees with it by construction. Only the DP, which considers every combination of
sample points across all cylinders at once, could see the shorter route.

The optimiser now runs three seeds (all points centred, plus two DP solutions at
different resolutions), polishes each by coordinate descent with an exact
per‑point 1‑D minimiser, and keeps the shortest. It takes ~50 ms, once, per task.
The invariant now runs both the perturbation search *and* an independent DP at a
resolution the optimiser does not use.

> This is the lesson of the whole exercise: a check that shares an assumption
> with the code it is checking will confirm the bug. The perturbation test was
> not weak because it was badly written — it was weak because it and the
> optimiser were both "one point at a time".

### 4.2 `launch_to_sss` exceeded the straight-line distance

**Found by:** level C, distance monotonicity.
**Impact:** [PG] S7F 13.3 early starters only. 6,510 m → **3,022 m**.

The early‑start distance was measured from the takeoff cylinder to *the SSS point
the task route uses* — which sits on the far side of the start cylinder, pointing
at the first turnpoint. On this task that produced 6,510 m between two cylinders
whose centres are 6,122 m apart: a shortest path longer than the straight line,
which cannot be a distance anyone flew. It is now the optimised
cylinder‑to‑cylinder distance, centre‑to‑centre less both radii.

### 4.3 Two pilots' scored distance went backwards mid‑task

**Found by:** level C, distance monotonicity.
**Impact:** live display only; final scores unaffected.

A pilot whose only SSS crossing so far was before the gate was provisionally
credited the launch‑to‑SSS distance under S7F 13.3. Forty‑two seconds later they
crossed again, validly, and were re‑scored on actual course progress — which at
that instant is near zero. The engine was right both times; the rule is simply
not monotone across that transition.

Fixing 4.2 removed the symptom on this data (3,022 m is below the 5 km minimum
distance, so the floor absorbs it). The invariant now allows *that specific
transition* — early‑start credit → valid start — while still failing any other
regression, and reports every occurrence rather than silently tolerating it.

---

## 5. What the official result found

`--compare` against the published Bassano T2.3 table found **six** more things,
four of them fixed. It is by a wide margin the most productive check in this
document, which is the argument for getting a second one.

The reproducing command, and what each flag is for:

```bash
./run.py --gate 11:30 --deadline 15:00 --compare official/T2.3.tsv --tz 2
```

`--gate 11:30` and `--deadline 15:00` are both **UTC**, and both are corrections
to the task file: it declares a gate of 23:00:00Z, which is after its own
deadline, and a deadline of 17:00:00 which is local time (UTC+2), not Z. The
competition ran 13:30–17:00 local.

### 5.1 Scored distance was measured from the wrong place

**Impact:** every pilot. Best distance 53,966 m → **59,647 m**.

The engine measured the scored route from the SSS. Every published result
measures it from the **first turnpoint** — here the D18 takeoff cylinder —
because a pilot who lands before reaching the start has still flown the
launch‑to‑SSS leg and is scored for it. The speed section is the separate
SSS→ESS span, and only the clock uses it.

Before the fix, non‑goal pilots' distances were short by a near‑constant
5.9 km, which is what made the cause obvious: a constant offset is a definition
error, not a geometry error. `CompiledTask` now carries `route_start` (where the
scored route begins) separately from `start_index` (where the clock begins).

### 5.2 Two task parameters exist in no input file

Neither is in the `.xctsk`, and both change scores. Both were read back out of
the published result and are now in `competition.json` under `tasks`:

* **`elevated_goal_m: 200`** — S7F 13.1 was not applied at all. The official
  table's `Low P` column *is* the 13.1 reduction, and inverting the published
  curve gives an elevation band of 200 m rather than the 300 m default. Three
  pilots at very different heights agree: 150 m → 0.9968, 18 m → 0.8493,
  6 m → 0.8175, all consistent with 200 and none with 300. A pilot 13 m *below*
  goal gets exactly 0.8, the floor. 40 of 111 finishers are affected.

* **`leading_time_ratio: 0.2632`** — the official pots are distance 361.7,
  leading 168.0, time 470.3. LeadingTimeRatio is `leading / (leading + time)`,
  which is invariant under whatever DistanceWeight came out at, so those three
  numbers pin it at **26.32 %**. S7F 11 gives 26 % for paragliding, and no goal
  ratio reconciles the two: at 26 % the leading pot would be 166.0, which is
  less than the 168.0 the winner actually scored, and a pilot cannot score more
  than the pot. **This needs confirming with the meet director**; the engine now
  warns whenever it is above 26 % rather than refusing to load, which is what
  the old `0..0.26` validation did.

With those two set, `available distance`, `available time`, `available leading`
and the implied LeadingTimeRatio all match the official exactly, and time points
match for 108 of 129 pilots within 0.1.

### 5.3 The leading coefficient does not reproduce the official result

**This is the open item.** Mean error **18.1 points per pilot**, worst 58.
Every other component is now within ~0.4 points.

`gap.leading_coefficient` implements S7F 12.3.1's paragliding weighted form as
`Σ minToESS·taskTime·∫weight(done)`. Inverting the official leading points gives
each pilot's official LC up to an unknown LCmin — and because
`LeadingFactor = 1 − ∛((LC−LCmin)²/√LCmin)`, the quantity `(1−factor)^1.5` is an
*affine* function of LC. So whichever candidate formula is a straight line
against `(1−factor)^1.5` is the one the official used, whatever its LCmin was.
Over the 119 pilots with an uncensored factor:

| Candidate leading coefficient | r² vs official | LCmin pilot |
| --- | --- | --- |
| `Σ d·t·∫weight(done)` — **what this engine does** | 0.9523 | 1073 ✗ |
| `∫ d dt / SS` | 0.9537 | 0157 ✓ |
| `∫ weight(done)·d dt / SS` | 0.9449 | 0157 ✓ |
| `∫ d^1.5 dt / SS^1.5` | 0.9920 | 0157 ✓ |
| **`∫ d² dt / SS²`** (the classic squared‑area form) | **0.9938** | 1073 ✗ |
| `∫ d³ dt / SS³` | 0.8904 | 1073 ✗ |

The official's LCmin pilot must be 0157 (A. Malecki scored 168.0, the whole
pot). Two conclusions, and only two:

1. **The implemented form is the worst plausible candidate.** It is beaten by
   every area‑under‑the‑distance‑to‑ESS‑curve variant, and it puts the wrong
   pilot at LCmin. It is very likely wrong.
2. **The right formula is in the `∫ dᵏ dt` family**, unweighted, with k near 2.

What has deliberately **not** been done is to swap in the best‑fitting formula.
Sweeping k continuously peaks at k ≈ 1.8 (r² 0.9975), not at a round number,
which is the signature of a model that is close but misspecified — and the fitted
exponent is biased anyway, because this engine's `d(t)` values are themselves
0.4 % short (§5.4). Fitting a rule to one task's output and calling the result
verified is exactly the mistake §19.4.1 of DESIGN.md exists to prevent. What is
needed is the S7F 12.3.1 text, or a second task to fit against and a third to
test on.

### 5.4 The optimised route is 0.42 % short — half of it is the earth model

**UPDATE.** S7F 7.1's list of algorithms settled the first of the three
candidates below. Four of its nine name the WGS84 ellipsoid, so the earth model
is not an open question: **the engine's FAI sphere is wrong.** All nine are
implemented in `engine/rules/s7f_71_algorithms.py` for checking, and running
the Code's pipeline — project with `GeodesicToCartesian`, optimise with
`PathFinder`, correct with `ProjectionCorrection`, measure with
`EllipsoidDistance` — gives **59,791 m against the engine's 59,647 m and an
official 59,900 m**. That closes 57 % of the gap and leaves −0.18 %.

Two things fell out of implementing it. The pipeline **converges in one pass**
(one iteration and five give the same metre). And its answer is **independent
of `FindTaskAreaCentre`** — 0.1 m of spread across anchors as different as the
arithmetic mean, the bounding-box centre, the first waypoint and a point 55 km
north. That is `ProjectionCorrection` doing precisely what 7.1.7 says it is
for, and it rules the projection anchor out as an explanation for what remains.

`--verify` now measures the sphere-vs-ellipsoid divergence on every run and
fails while the engine is still on the sphere. Switching has not been done: it
is a scoring change resting on 7.1 text I have not seen.

The original analysis follows.



**Impact:** −253 m on a 59.9 km task; average speed off by 0.14 km/h; the
1‑second differences in finish times.

Engine 59,647 m against official 59,900 m, and the speed section 54,787 m
against ~55,110 m (recovered by intersecting `speed × time` across the field).
Three plausible contributors, not yet separated:

* **Earth model.** The engine uses the FAI sphere, R = 6,371 km. WGS84 geodesics
  over this task's waypoints are 0.18 % longer — about half the gap. The
  `.xctsk` here declares no `earthModel`.
* **The concentric start.** B50 appears twice, as a 3,000 m SSS and a 2,000 m
  turnpoint inside it. The engine puts the start point wherever in the 3,000 m
  disc shortens the route, which collapses that leg to zero; pinning it to the
  rim instead adds 1.0 km. Which is right depends on whether an EXIT start
  requires the optimised point to be on the boundary.
* **FS's own optimiser** may simply be above the true optimum, in which case the
  engine is right and the official is long.

The engine's route is a verified local *and* global optimum for the geometry as
the engine models it (§4.1), so this is a modelling question, not an optimiser
bug.

### 5.5 Penalties were not implemented at all

**Impact:** one pilot here, and 290 points — the single largest discrepancy in
the whole comparison. **Fixed.**

The official table carries `PEN 1380 Marek DMOCHOWSKI −100% of own points
AIRSPACE`. The engine scored him 289.7; the official gave 0.0. Nothing to do
with any formula: S7F 13.5 was on the "not implemented" list, and a penalty is
the one part of a score that cannot be derived from a tracklog, so no amount of
internal checking could ever have surfaced it.

`engine/rules/penalties.py` now reads a `penalties.json` keyed by task, matching
pilots by the ID that is also the IGC filename. It supports the three forms
tables actually use — percentage of own points, percentage of the task pot, and
flat points — applies them **last**, to the already-rounded total, and floors
the result at zero. Every entry must state a reason, which then appears in
`--explain` and on the leaderboard footer. A penalty naming a pilot who is not
in the field is reported rather than skipped.

With it applied, the worst total difference against the official falls from
+289.7 to +47.6, and what remains is entirely §5.3.

### 5.6 A goal reached after the deadline loses its distance

**Impact:** one pilot here, 0221 M. King: engine 55.6 km, official 59.9 km.

The engine stops the state machine at the goal deadline, so a pilot who crosses
goal late is scored on wherever they were when the clock ran out. The official
credits the full task distance — they did fly the whole course — while excluding
them from the goal count and awarding no time points. The official's goal count
is 111, not 112, which is what made the distance pot match exactly.

This is **not fixed**. It is a rules question about S7F 13's treatment of a late
goal arrival, it moves one pilot's score by 24 points, and it should be decided
rather than inferred.

---

## 6. Optimisations, and why they are safe

Four changes were made for speed. Each one creates a second implementation of
something, and each is therefore pinned by an equality check that runs in
`--verify`:

| Change | Speedup | Pinned by |
| --- | --- | --- |
| Inline the S7F 9.2.1 zone test into the two hot loops; carry the previous distance forward instead of recomputing it (5 square roots per fix → 1) | `score_pilot` 1.20 → 0.52 µs/fix | 1,000,000 random segments compared against `geo.zone_crossing`, biased hard toward the boundary |
| Take IGC B‑record fields by byte offset instead of by regex | parse 1.67 → 1.19 µs/fix | Every field of every one of 1,429,368 fixes compared against `parse_igc_reference`, the regex version, which remains the specification |
| Split the leading coefficient into a per‑pilot half and a field‑wide half | Removes ~600k tuples from the process boundary | `leading_partial` + `leading_from_partial` == `leading_coefficient`, exactly, on 2,000 random tracks |
| Score pilots across every core | 3.9 s → 0.73 s cold | Whole board compared pilot by pilot and field value by field value, plus rank order |

None of them touch a rule. `engine/gap.py`, which is where the rules live, is
unchanged except for the leading‑coefficient split, and that split is exact.

---

## 7. Not verified — read this part

An honest report is mostly this section.

**The leading coefficient is wrong.** This section used to say "if one part of
this engine is wrong, it is most likely this one". The official result now says
so outright — see §5.3. The formula in `gap.leading_coefficient` does not
reproduce the published leading points, and the evidence points at a different
family of formula rather than a tuning error. It is still in place, unchanged,
because guessing a replacement that fits one task is not verification. This is
the open item.

**S7F 13.3 wording.** Whether an early start should be credited the optimised
cylinder‑to‑cylinder launch→SSS distance (what the engine now does) or something
else depends on wording not available here. The quantity is at least now
geometrically coherent, which it was not before.

**Not implemented at all:** stopped tasks (S7F 10.4 and 13.4) and FTV (16).
`./run.py --rules` lists them with what each would involve, and `--verify`
prints the same list rather than a hand-maintained sentence — which is how
penalties came to be listed as missing for longer than they actually were.

**A segment that jumps clean over a small cylinder is not caught.** The scoring
path validates a zone when either end of a segment is within the tolerance
boundary. A segment whose two endpoints are both outside a small cylinder but
which passes through it is missed. `geo.touches_cylinder` exists to catch exactly
this and is documented as doing so, but `zone_crossing` — the function actually
used — never calls it. At 1 Hz with a 200 m goal cylinder this is unreachable;
with degraded live telemetry at 0.1 Hz it is not. **This is a known gap, not a
fixed one**, left alone deliberately because closing it changes scored results and
that should be a decided change, not a side effect of a performance pass.

**One task, one comparison.** §5 checks against a single published task. That is
enormously better than nothing — it caught a wrong task-distance definition, two
task parameters that were not in any input file, and the leading-coefficient
problem — but a formula that fits one task can still be wrong. A second task, and
ideally a second competition, is the next thing worth having.

**The competition parameters are still placeholders.** `nominal_distance_km`,
`minimum_distance_km` and `nominal_time_min` in `competition.json` are the shipped
defaults, and every validity number in every result above is a function of them.
The engine prints a warning naming them on every run. Until the meet director
sets them, the task validity of 1.000 reported here is not a real number.

---

## 8. Per‑pilot audit — answering a protest

`--verify` establishes that the engine is right in general. It does nothing for a
pilot standing at the scoring desk saying "I was inside that cylinder".

```bash
./run.py --explain "GHARGHI"              # by name, surname, or IGC filename
./run.py --explain "GHARGHI" --json a.json
```

The audit prints, for that one pilot:

0. **Every input, by SHA‑256** — task file, competition config, their tracklog,
   and the engine source. Same hashes, same page, byte for byte.
1. **Tracklog integrity** — fix count, coverage, mean rate, and *every gap over
   5 seconds*, because a claimed crossing that falls in a gap is a data problem
   and not a scoring one.
2. **Takeoff** — which fix, and how far from the first recorded position.
3. **Start** — a table of *every* crossing of the SSS tolerance zone in the whole
   tracklog, with the two distances that bracket each one, which side of the gate
   it fell, which one was scored, and the S7F 8.1 rule that selected it. When
   several candidate starts exist, each is replayed in full and the distance it
   produced is shown.
4. **Control zones** — every turnpoint, its tolerance band, the validating fix
   number, and `d(prev)`/`d(fix)`: the two measured distances the crossing sits
   between. This is the section a cylinder protest is settled from.
5. **Distance** — the optimised task distance, the closest‑approach fix, and the
   subtraction.
6. **Speed section** — clock start, ESS, elapsed, average speed.
7. **Leading coefficient** — sample count, leadingArea, minToESS, maxTime, LC,
   LCmin, and the first and last samples.
8. **The field** — validity, weights, available points, best distance, best time.
   Labelled explicitly, because "my points changed and I did not move" is almost
   always this half moving, not the pilot's.
9. **Points** — every line as *formula*, *numbers substituted*, *result*, with
   its S7F reference, so it can be redone on paper.

Two things make it evidence rather than commentary:

* **It comes out of the scoring pass itself.** `score_pilot()` takes an optional
  trace argument and records why it decided what it decided. There is no second
  implementation that could describe a different calculation from the one that
  produced the number. `--verify` asserts that turning tracing on changes no
  scored value.
* **It re-derives the score from the file.** The audit re‑reads the pilot's IGC,
  replays it, and compares against what is on the leaderboard field by field. If
  they differ it says so in red at the top and exits non‑zero, instead of printing
  a plausible page.

---

## 9. Speed

Measured on 8 cores, CPython 3.12, 129 pilots, 1,429,368 fixes.

| | |
| --- | --- |
| **Cold full‑field publish — process start to printed board** | **0.73 s** |
| Same, single process (`--serial`) | 3.79 s |
| Scoring loop only, points already in memory | 796 ms |
| Per fix, full hot path | 0.56 µs |
| Live incremental load, 150 pilots at 1 Hz | 0.08 ms/s = **0.01 % of one core** |
| Worst‑case single‑pilot recompute (18,637‑fix track) | 10 ms |

`./run.py --bench` prints all of these, and re‑measures the cold publish by
actually launching the process three times rather than estimating it.

The live requirement was never in doubt — one fix costs half a microsecond. The
number that needed work is the first row: the cold recompute a scorer waits on
after the last tracklog is uploaded, or after a task correction. Pilots are
independent, so it goes across every core; the invariant in §5 is what makes that
safe to do.

> **Note on measuring this yourself:** if the working directory is on a network
> or synced filesystem, reading the tracklogs will dominate everything above. On
> the machine this was developed on, reading the same 40 MB took 0.09 s from
> local disk and over 100 s from the project directory. Benchmark from local
> disk, or the numbers mean nothing.
