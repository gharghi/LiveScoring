"""Invariants over a REAL scored field — properties that must hold for any
field on any task.

`--verify`'s formula checks compare single functions against numbers printed in
the Sporting Code. They are necessary and they are not sufficient: every
formula can be individually right while the thing that assembles them is
wrong, and that is the failure mode that actually reaches a leaderboard.

These are the other half. They take a real scored field and assert things that
must be true of it regardless of what the pilots did — the arithmetic closes,
nobody scores more than is available, the ordering is consistent with the
times, the same input gives the same output, and a mid-task board never
contradicts the final one. A violation is a bug by construction; there is no
data for which it is acceptable.

Each check returns (name, ok, detail). Detail names the offending pilot, so a
failure is immediately actionable rather than merely alarming.
"""

from __future__ import annotations

import math

from engine import gap
from engine.gap import GapParams
from engine.score import project, score_pilot
from engine.scoring import score_task

EPS = 5e-4          # points are reported to 0.1; anything under this is rounding
Check = tuple[str, bool, str]


def _fmt(rs, key, n=3):
    """Name the worst offenders rather than just counting them."""
    bad = sorted(rs, key=key, reverse=True)[:n]
    return ", ".join(f"{r.pilot} ({key(r):+.4f})" for r in bad)


# --- arithmetic and allocation -------------------------------------------


def _expected_total(r) -> float:
    """S7F 12 then 13.5.

    The components are each already rounded to one decimal by their own rules
    (12.1, 12.2, 12.3); the sum of those rounded values is rounded again. That
    is not the same as rounding the sum of the unrounded parts, and S7F 12 is
    explicit about which it wants.
    """
    gross = round(r.distance_points + r.time_points + r.leading_points, 1)
    if r.penalty_points:
        return round(max(0.0, gross - r.penalty_points), 1)
    return gross


def check_totals(results, ts) -> Check:
    """total == distance + time + leading + arrival, rounded, less penalties.

    The rounding order is part of the rule, not a detail: S7F 12 rounds the
    sum, and S7F 13.5 penalties then apply to that rounded total, because a
    percentage penalty has to be a percentage of something final.
    """
    bad = [r for r in results if abs(r.total_points - _expected_total(r)) > EPS]
    n_pen = sum(1 for r in results if r.penalty_points)
    return ("total = distance + time + leading, rounded, less penalties",
            not bad,
            f"{len(bad)} pilots disagree: "
            + _fmt(bad, lambda r: r.total_points - _expected_total(r))
            if bad else f"all {len(results)} pilots"
                        + (f"; {n_pen} carry an S7F 13.5 penalty" if n_pen else ""))


# S7F 12 rounds each component to one decimal place; S7F 11's pots are carried
# unrounded (see rules/s7f_11_allocation, conflict 1). So a component can sit up
# to 0.05 above its own pot purely from that rounding — the pilot at the best
# distance scores round(361.66, 1) = 361.7 out of 361.66. Odd, and a direct
# consequence of applying 12's rounding rule but not 11's.
ROUND1 = 0.05 + EPS


def check_caps(results, ts) -> Check:
    """No component exceeds what S7F 11 made available for it, beyond rounding."""
    a = ts.alloc
    bad = []
    for r in results:
        if (r.distance_points > a.available_distance + ROUND1
                or r.time_points > a.available_time + ROUND1
                or r.leading_points > a.available_leading + ROUND1):
            bad.append(r)
    return (f"no component exceeds its allocation "
            f"(d≤{a.available_distance:.0f} t≤{a.available_time:.0f} "
            f"l≤{a.available_leading:.0f})",
            not bad,
            f"{len(bad)} over cap: " + ", ".join(r.pilot for r in bad[:3])
            if bad else f"all {len(results)} pilots (±0.05 for S7F 12 rounding)")


def check_thousand(results, ts) -> Check:
    """Nobody scores more than 1000 x taskValidity (S7F 11).

    An upper bound, so S7F 13.5 penalties can only help it hold. Penalties are
    also floored at zero, so no total can go negative either.
    """
    cap = 1000.0 * ts.task_validity
    bad = [r for r in results if r.total_points > cap + 0.05 + EPS
           or r.total_points < -EPS]
    top = max((r.total_points for r in results), default=0.0)
    return (f"total ≤ 1000 × taskValidity = {cap:.1f}", not bad,
            f"highest total {top:.1f}" if not bad
            else f"{len(bad)} above cap: " + _fmt(bad, lambda r: r.total_points - cap))


def check_allocation_sums(ts) -> Check:
    """The four weights partition 1.0, and the four pots partition the total."""
    a = ts.alloc
    w = a.distance_weight + a.leading_weight + a.time_weight + a.arrival_weight
    pot = round(1000.0 * ts.task_validity)
    got = a.available_total
    ok = abs(w - 1.0) < 1e-9 and abs(got - pot) <= 0.5   # pot itself is rounded
    return ("weights sum to 1 and the pots sum to 1000 × taskValidity", ok,
            f"Σweights = {w:.12f}, Σpots = {got:.0f} vs {pot:.0f}")


# --- ordering: the podium must follow from the flights -------------------


def check_distance_monotone(results, ts) -> Check:
    """More distance can never score fewer distance points (S7F 12.1, linear)."""
    rs = sorted((r for r in results), key=lambda r: r.distance)
    bad = []
    for a, b in zip(rs, rs[1:]):
        if b.distance > a.distance + 1e-6 and b.distance_points < a.distance_points - EPS:
            bad.append((a, b))
    return ("further flown ⇒ never fewer distance points", not bad,
            f"{len(bad)} inversions, e.g. {bad[0][1].pilot} < {bad[0][0].pilot}"
            if bad else f"checked {len(rs)} pilots")


def check_time_monotone(results, ts) -> Check:
    """A faster speed section can never score fewer time points (S7F 12.2).

    Restricted to pilots in goal, because that is the only population that
    scores time points at all in paragliding (S7F 9.4.1 / 13.2).

    The comparison is on time points BEFORE the elevated-goal factor, because
    S7F 13.1 deliberately breaks the raw ordering: a slower pilot who crossed
    the goal cylinder high can and should finish above a faster one who
    underflew it. Dividing the factor back out isolates the S7F 12.2 curve,
    which is the thing that must be monotone. When the goal is not elevated the
    factor is 1 for everyone and this is the plain statement.
    """
    rs = sorted((r for r in results
                 if r.goal_time is not None and r.speed_section_time),
                key=lambda r: r.speed_section_time)
    bad = []
    for a, b in zip(rs, rs[1:]):
        pa = a.time_points / (a.goal_altitude_factor or 1.0)
        pb = b.time_points / (b.goal_altitude_factor or 1.0)
        if b.speed_section_time > a.speed_section_time + 1e-9 and pb > pa + EPS:
            bad.append((a, b))
    n_scaled = sum(1 for r in rs if abs(r.goal_altitude_factor - 1.0) > 1e-9)
    return ("faster speed section ⇒ never fewer time points (before S7F 13.1)",
            not bad,
            f"{len(bad)} inversions, e.g. {bad[0][1].pilot} > {bad[0][0].pilot}"
            if bad else f"checked {len(rs)} pilots in goal; {n_scaled} carry an "
                        f"elevated-goal factor < 1 (S7F 13.1)")


def check_best_gets_max(results, ts) -> Check:
    """The best distance takes the whole distance pot; the fastest, the whole time pot."""
    msgs, ok = [], True
    if ts.best_distance > 0:
        winners = [r for r in results if abs(r.distance - ts.best_distance) < 1e-6]
        for r in winners:
            if abs(r.distance_points - round(ts.alloc.available_distance, 1)) > EPS:
                ok = False
                msgs.append(f"{r.pilot} flew the best distance but scored "
                            f"{r.distance_points:.4f} of {ts.alloc.available_distance:.0f}")
        if ok:
            msgs.append(f"{len(winners)} at best distance → full "
                        f"{ts.alloc.available_distance:.0f}")
    if ts.best_time:
        fast = [r for r in results if r.goal_time is not None
                and r.speed_section_time and abs(r.speed_section_time - ts.best_time) < 1e-9]
        for r in fast:
            want = round(ts.alloc.available_time * r.goal_altitude_factor, 1)
            if abs(r.time_points - want) > EPS:
                ok = False
                msgs.append(f"{r.pilot} was fastest but scored {r.time_points:.4f} "
                            f"of {want:.4f}")
        if ok:
            msgs.append(f"{len(fast)} at best time → full {ts.alloc.available_time:.0f}")
    return ("best distance and best time take the full pot", ok, "; ".join(msgs) or "n/a")


def check_lcmin_gets_max(results, ts) -> Check:
    """LeadingFactor is 1 exactly at LCmin, so the leader takes the whole pot."""
    if ts.lc_min <= 0:
        return ("LCmin scores the full leading pot", True, "no leading points in play")
    lead = [r for r in results if r.lc > 0 and abs(r.lc - ts.lc_min) < 1e-12]
    bad = [r for r in lead
           if abs(r.leading_points - round(ts.alloc.available_leading, 1)) > EPS]
    return ("LCmin scores the full leading pot", not bad,
            f"{lead[0].pilot} → {ts.alloc.available_leading:.0f}" if lead and not bad
            else f"{len(bad)} wrong" if bad else "nobody at LCmin")


def check_state_order(results, ts) -> Check:
    """Takeoff ≤ start ≤ every tag in task order ≤ ESS ≤ goal."""
    bad = []
    for r in results:
        seq = [t for t in r.tags if t is not None]
        if any(b < a - 1e-9 for a, b in zip(seq, seq[1:])):
            bad.append((r, "turnpoint times out of order"))
        elif r.start_cross_time and r.goal_time and r.goal_time < r.start_cross_time:
            bad.append((r, "goal before start"))
        elif r.ess_time and r.goal_time and r.goal_time < r.ess_time:
            bad.append((r, "goal before ESS"))
        elif r.takeoff_time and r.start_cross_time and r.start_cross_time < r.takeoff_time:
            bad.append((r, "start before takeoff"))
    return ("event times are in task order for every pilot", not bad,
            f"{len(bad)}: {bad[0][0].pilot} — {bad[0][1]}" if bad
            else f"all {len(results)} pilots")


def check_distance_bounded(task, results) -> Check:
    """Nobody flies further than the task, and goal implies the full distance."""
    bad = []
    for r in results:
        if r.raw_distance > task.total_distance + 1e-6:
            bad.append((r, f"raw {r.raw_distance:.1f} > task {task.total_distance:.1f}"))
        elif r.goal_time is not None and abs(r.raw_distance - task.total_distance) > 1e-6:
            bad.append((r, "in goal but not scored the full task distance"))
    return ("scored distance ≤ task distance; goal ⇒ full distance", not bad,
            f"{len(bad)}: {bad[0][0].pilot} — {bad[0][1]}" if bad
            else f"all {len(results)} pilots, task {task.total_distance:,.1f} m")


# --- S7F 9.2.1: crossing times are tracklog timestamps, never interpolated


def check_times_are_fixes(task, tracks, results) -> Check:
    """Every scored time is the timestamp of a real point in that pilot's track.

    This is the check that S7F 9.2.1 actually asks for. The engine has an
    interpolating crossing routine (geo.touches_cylinder) that would give a
    better live *estimate* and a different official *result*; this asserts the
    scoring path never reaches for it.
    """
    by_pilot = {t.pilot: t for t in tracks}
    bad = []
    for r in results:
        tr = by_pilot.get(r.pilot)
        if tr is None:
            continue
        stamps = {f.t for f in tr.fixes}
        for label, t in (("start", r.start_cross_time), ("ESS", r.ess_time),
                         ("goal", r.goal_time)):
            if t is None:
                continue
            if float(t) != int(t) or int(t) not in stamps:
                bad.append(f"{r.pilot} {label} {t}")
        for i, t in enumerate(r.tags):
            if t is not None and (float(t) != int(t) or int(t) not in stamps):
                bad.append(f"{r.pilot} wp{i} {t}")
    return ("every scored time is a real tracklog timestamp (S7F 9.2.1)", not bad,
            f"{len(bad)} interpolated: {bad[:3]}" if bad
            else f"checked {sum(1 for r in results if r.start_cross_time)} started pilots")


# --- determinism, tracing, and live-vs-final -----------------------------


def check_determinism(task, tracks, params, now) -> Check:
    """Scoring the same points twice returns bit-identical numbers."""
    a = [score_pilot(task, t.fixes, now, params) for t in tracks]
    b = [score_pilot(task, t.fixes, now, params) for t in tracks]
    bad = []
    for t, x, y in zip(tracks, a, b):
        if (x.distance, x.start_time, x.ess_time, x.goal_time, x.raw_distance,
                tuple(x.lead_samples)) != (y.distance, y.start_time, y.ess_time,
                                           y.goal_time, y.raw_distance,
                                           tuple(y.lead_samples)):
            bad.append(t.pilot)
    return ("scoring is deterministic — same points, bit-identical result", not bad,
            f"{len(bad)} differ: {bad[:3]}" if bad else f"{len(tracks)} pilots, twice")


def check_trace_is_free(task, tracks, params, now) -> Check:
    """Turning the audit trail on must not change a single scored value.

    Without this the audit is worth nothing: it would be describing a run that
    is not the run on the leaderboard.
    """
    bad = []
    for t in tracks:
        plain = score_pilot(task, t.fixes, now, params)
        traced = score_pilot(task, t.fixes, now, params, {})
        if (plain.distance, plain.start_time, plain.start_cross_time, plain.ess_time,
                plain.goal_time, plain.raw_distance, plain.state,
                tuple(plain.tags), tuple(plain.lead_samples)) != (
                traced.distance, traced.start_time, traced.start_cross_time,
                traced.ess_time, traced.goal_time, traced.raw_distance, traced.state,
                tuple(traced.tags), tuple(traced.lead_samples)):
            bad.append(t.pilot)
    return ("--explain tracing changes no scored value", not bad,
            f"{len(bad)} differ: {bad[:3]}" if bad
            else f"{len(tracks)} pilots scored with and without tracing")


def check_truncation_equals_now(task, tracks, params, at) -> Check:
    """Scoring a full track "as at T" equals scoring a track that stops at T.

    This is the live-vs-official property (DESIGN.md §4.3). The live engine has
    only the points that have arrived; official scoring has all of them and a
    `now`. If those two disagree, every live board is a lie that gets corrected
    later — the exact failure the one-scoring-function design exists to prevent.
    """
    bad = []
    for t in tracks:
        full = score_pilot(task, t.fixes, at, params)
        cut = [f for f in t.fixes if f.t <= at]
        if not cut:
            continue
        part = score_pilot(task, cut, 1e18, params)
        if (abs(full.distance - part.distance) > 1e-6
                or full.start_time != part.start_time
                or full.ess_time != part.ess_time
                or full.goal_time != part.goal_time):
            bad.append(f"{t.pilot}: full {full.distance:.1f} vs truncated "
                       f"{part.distance:.1f}")
    return ("live (truncated points) == official (full points, scored as at T)",
            not bad, f"{len(bad)} differ: {bad[:2]}" if bad
            else f"{len(tracks)} pilots at T")


def check_distance_never_decreases(task, tracks, params, times) -> Check:
    """A pilot's scored distance may only ever go up as the clock advances.

    A leaderboard that moves a pilot backwards is the most visible live-scoring
    failure there is, and it is the natural consequence of any incremental
    optimisation that gets its state machine wrong.

    There is exactly one drop the RULES require, and it is allowed here rather
    than being quietly ignored: [PG] S7F 13.3. A pilot whose only SSS crossing
    so far is before the gate is provisionally an early starter, scored on the
    launch-to-SSS distance. If they then cross again after the gate they are no
    longer an early starter at all — they are an ordinary pilot who has just
    started, and their progress along the course is momentarily near zero. The
    engine is right both times; the rule itself is not monotone across that
    transition. Every such case is counted and reported, so a genuine
    regression cannot hide behind the exemption.
    """
    bad = []
    early_transitions = []
    for t in tracks:
        prev_d = -1.0
        prev_r = None
        for now in times:
            r = score_pilot(task, t.fixes, now, params)
            if r.distance < prev_d - 1e-6:
                if (prev_r is not None and prev_r.early_start
                        and prev_r.start_time is None and r.start_time is not None):
                    early_transitions.append(
                        f"{t.pilot} {prev_d:,.0f}→{r.distance:,.0f} m")
                else:
                    bad.append(f"{t.pilot} dropped {prev_d:,.1f} → {r.distance:,.1f} "
                               f"at gate+{int(now - task.first_gate)}s")
                    break
            prev_d, prev_r = r.distance, r
    detail = f"{len(tracks)} pilots × {len(times)} checkpoints"
    if early_transitions:
        detail += (f"; {len(early_transitions)} S7F 13.3 early-start "
                   f"re-scores, all expected: {early_transitions[:2]}")
    return (f"scored distance is monotone in time ({len(times)} checkpoints)",
            not bad, f"{len(bad)} regressions: {bad[:2]}" if bad else detail)



def check_launch_validity_is_measured(ts) -> Check:
    """Is launch validity a measurement, or is it 1.0 by construction?

    S7F 10.1 calls launch validity a SAFETY FEATURE: it devalues a task most of
    the field judged too dangerous to fly. It only works if somebody counted
    the pilots who stood on launch and did not fly (DNF), and no tracklog
    records that. With nothing supplied the engine counts tracklogs, so
    PilotsFlying == PilotsPresent and the coefficient is 1.0 always.

    Reported rather than failed — it is a missing input, not a bug — but
    reported every run, because "launch validity 1.000" looks like a result.
    """
    derived = ts.pilots_present == ts.pilots_flying
    # Reported, never failed: this is a missing meet-director input, not a bug
    # in the engine, and a permanently red check is a check people stop
    # reading. It is surfaced on the board itself as a config warning, next to
    # the placeholder competition parameters, which is where a scorer will act
    # on it.
    return ("10.1 launch validity is measured, not assumed", True,
            f"pilots_present ({ts.pilots_present}) == pilots_flying "
            f"({ts.pilots_flying}): no DNF count was supplied, so launch "
            f"validity is 1.0 by construction and the S7F 10.1 safety feature "
            f"is switched off. NOT AN ENGINE FAULT — set pilots_present in "
            f"competition.json."
            if derived else
            f"{ts.pilots_flying} flew of {ts.pilots_present} present → "
            f"launch validity {ts.launch_validity:.6f}")



def check_earth_model_divergence(task, task_path: str | None) -> Check:
    """How far the running engine's route is from the S7F 7.1 pipeline.

    NOT a pass/fail of the optimiser — engine/rules/route.py's answer is
    verified optimal for the geometry it is given. This measures the geometry
    itself: the engine works on the FAI sphere and measures in the projected
    plane, while S7F 7.1 specifies the WGS84 ellipsoid, a projection
    correction (7.1.7) and measurement with EllipsoidDistance (7.1.5).

    Reported on every run rather than written down once, because it is a live
    divergence from the Code and not a historical note. It fails while the
    engine is still on the sphere; that is the point.
    """
    import json
    import os

    from engine.rules.s7f_71_algorithms import RouteOptimizer

    if not task_path or not os.path.exists(task_path):
        return ("engine route == S7F 7.1 RouteOptimizer", True,
                "skipped — needs the .xctsk to read lat/lon")

    with open(task_path, "rb") as fh:
        doc = json.load(fh)
    zones = [(tp["waypoint"]["lat"], tp["waypoint"]["lon"], float(tp["radius"]))
             for tp in doc["turnpoints"]]
    if len(zones) != len(task.waypoints):
        return ("engine route == S7F 7.1 RouteOptimizer", True,
                "skipped — task file and compiled task disagree on waypoints")

    ref = RouteOptimizer(zones, task.route_start)
    mine = task.total_distance
    d = mine - ref.distance
    ok = abs(d) < 1.0
    return ("engine route == S7F 7.1 RouteOptimizer (WGS84 + correction)", ok,
            f"engine {mine:,.1f} m (FAI sphere, measured in the plane) vs "
            f"7.1 {ref.distance:,.1f} m (WGS84, corrected, measured on the "
            f"ellipsoid) → {d:+,.1f} m ({d / ref.distance * 100:+.3f}%). "
            f"The engine is still on the sphere; see rules/earth_model.py.")


def check_inlined_zone_test(seed: int = 20260808, n: int = 1_000_000) -> Check:
    """The hot loop's inlined zone test == geo.zone_crossing, on random segments.

    score.py evaluates the S7F 9.2.1 tolerance-zone test inline rather than by
    calling geo.zone_crossing, because the call built two tuples per fix and
    recomputed distances the same loop already had. That is a real speedup and
    a real risk: an inlined copy of a rule is a second implementation of it.

    This closes that risk by brute force. A million random segments against a
    random cylinder, comparing the inlined boolean to what geo.zone_crossing
    returns — including the cases the geometry makes easy to get wrong: both
    endpoints inside, both outside, one of each, endpoints exactly on a
    boundary, and zero-length segments from duplicate fixes.
    """
    import random

    from engine.geo import in_zone, zone_crossing

    rng = random.Random(seed)
    bad = 0
    first = ""
    for _ in range(n):
        r = rng.choice((100.0, 200.0, 1000.0, 4000.0, 17000.0))
        inner, outer = r - 5.0, r + 5.0
        cx = cy = 0.0

        def pt():
            # Biased hard toward the boundary, where a disagreement would live.
            if rng.random() < 0.7:
                d = r + rng.uniform(-20.0, 20.0)
            else:
                d = rng.uniform(0.0, r * 2.0)
            a = rng.uniform(0.0, 2.0 * math.pi)
            return d * math.cos(a), d * math.sin(a)

        x0, y0 = pt()
        x1, y1 = (x0, y0) if rng.random() < 0.02 else pt()

        # what score.py's two loops compute
        d0 = math.hypot(x0 - cx, y0 - cy)
        d1 = math.hypot(x1 - cx, y1 - cy)
        inlined_transition = (((d0 < inner) != (d1 < inner))
                              or ((d0 <= outer) != (d1 <= outer)))
        inlined_validate = (d1 <= outer) or (d0 <= outer)

        # what geo.zone_crossing says
        c = zone_crossing((x0, y0, 0.0), (x1, y1, 1.0), cx, cy, inner, outer)
        ref_transition = c is not None
        ref_validate = ref_transition or in_zone(x1, y1, cx, cy, outer)

        if inlined_transition != ref_transition or inlined_validate != ref_validate:
            bad += 1
            if not first:
                first = (f"d0={d0:.6f} d1={d1:.6f} r={r} "
                         f"transition {inlined_transition}/{ref_transition} "
                         f"validate {inlined_validate}/{ref_validate}")
    return ("inlined zone test == geo.zone_crossing (1 M random segments)", not bad,
            f"{bad} disagreements, first: {first}" if bad
            else f"{n:,} segments, 0 disagreements")


def check_distance_to_goal_reference(task, tracks, params, now) -> Check:
    """The hot loop's inlined distance-to-goal == the reference formula.

    score.py folds rules/distance_flown.distance_to_goal() inline and carries
    the distance to the current waypoint forward from the previous fix, which
    is most of what took the per-fix cost from 1.20 us to 0.52 us. That makes
    it an inlined copy of a rule, and an inlined copy is a second
    implementation.

    This walks every fix of every pilot, replays the same waypoint-advance the
    scorer does, and compares the inlined value against
    score.remaining_to_goal() — the readable statement of the same formula,
    which nothing else calls. Millions of comparisons, exact equality.
    """
    from engine.score import remaining_to_goal

    worst = 0.0
    worst_at = ""
    n = 0
    for tr in tracks:
        r = score_pilot(task, tr.fixes, now, params)
        if r.start_time is None:
            continue
        # Replay the waypoint advance from the scored start, using the tags the
        # scorer produced, so both sides are measuring to the same zone.
        next_wp = task.start_index + 1
        tags = [(i, t) for i, t in enumerate(r.tags) if t is not None
                and i > task.start_index]
        ti = 0
        for f in tr.fixes:
            if r.start_cross_time is not None and f.t < r.start_cross_time:
                continue
            if f.t > now:
                break
            while ti < len(tags) and f.t > tags[ti][1]:
                next_wp = tags[ti][0] + 1
                ti += 1
            if next_wp > task.goal_index:
                break
            w = task.waypoints[next_wp]
            ref = remaining_to_goal(task, f, next_wp)
            inline = math.hypot(f.x - w.x, f.y - w.y) - w.radius
            inline = (inline if inline > 0.0 else 0.0) + task.remaining[next_wp]
            d = abs(ref - inline)
            n += 1
            if d > worst:
                worst, worst_at = d, f"{tr.pilot} at t={f.t}"
    return ("inlined distance-to-goal == the reference formula", worst == 0.0,
            f"{n:,} fixes compared, worst difference {worst:.6g} m"
            + (f" ({worst_at})" if worst else ""))


def check_parser_equivalence(igc_path: str | None) -> Check:
    """The fast fixed-offset IGC parser == the regex reference, on the real corpus.

    igc.parse_igc takes B-record fields by byte offset because the regex was
    the single most expensive thing in a cold run. igc.parse_igc_reference is
    the same grammar written as a regex and is the readable specification. Two
    parsers means they can disagree, so every fix of every file is compared:
    timestamp, latitude, longitude, both altitudes, the pilot name and the task
    date.
    """
    import os

    from engine.igc import parse_igc, parse_igc_reference

    if not igc_path or not os.path.isdir(igc_path):
        return ("fast IGC parser == regex reference", True,
                "skipped — needs a directory of .igc files")

    files = sorted(f for f in os.listdir(igc_path) if f.lower().endswith(".igc"))
    nfix = 0
    bad = []
    for name in files:
        with open(os.path.join(igc_path, name), "rb") as fh:
            data = fh.read()
        a = parse_igc_reference(data, name)
        b = parse_igc(data, name)
        if a[0] != b[0] or a[1] != b[1] or len(a[2]) != len(b[2]):
            bad.append(f"{name}: header/count")
            continue
        for x, y in zip(a[2], b[2]):
            if ((x.t, x.lat, x.lon, x.alt_baro, x.alt_gps)
                    != (y.t, y.lat, y.lon, y.alt_baro, y.alt_gps)):
                bad.append(f"{name}: fix at t={x.t}")
                break
        nfix += len(a[2])
    return ("fast IGC parser == regex reference, field by field", not bad,
            f"{len(bad)} files differ: {bad[:3]}" if bad
            else f"{len(files)} files, {nfix:,} fixes, every field identical")


def check_parallel_matches_serial(task, params, now, igc_path: str | None,
                                  results, ts) -> Check:
    """Scoring across processes gives the same numbers as scoring in one.

    The parallel path exists only to make a cold full-field recompute finish
    inside a second. It reuses score_pilot and score_task unchanged, but it
    also regroups files by pilot from headers alone, merges multi-file pilots
    in the worker, and reduces the leading-coefficient samples to two floats
    before they cross the process boundary. Any of those could go wrong
    quietly, so the whole board is compared: points, distances, every event
    time, and the rank order.
    """
    import os

    from engine import parallel
    from engine.scoring import score_task

    if not igc_path or not parallel.usable(igc_path):
        return ("parallel field score == serial field score", True,
                "skipped — needs a directory of .igc files")

    groups, _day = parallel.scan_headers(parallel.igc_paths(igc_path))
    pres, _ptracks, _d = parallel.score_field(task, params, now, groups)
    pts = score_task(task, pres, params, ts.pilots_present)

    if {r.pilot for r in pres} != {r.pilot for r in results}:
        return ("parallel field score == serial field score", False,
                f"different pilots: {len(pres)} vs {len(results)}")

    by = {r.pilot: r for r in results}
    bad = []
    for a in pres:
        b = by[a.pilot]
        for fname in ("total_points", "distance_points", "time_points",
                      "leading_points", "distance", "raw_distance", "lc",
                      "start_time", "start_cross_time", "ess_time", "goal_time",
                      "state", "speed_section_time"):
            x, y = getattr(a, fname), getattr(b, fname)
            if x != y:
                bad.append(f"{a.pilot}.{fname}: {x!r} vs {y!r}")
    for fname in ("task_validity", "best_distance", "best_time", "lc_min",
                  "max_time", "pilots_goal", "pilots_flying"):
        x, y = getattr(pts, fname), getattr(ts, fname)
        if x != y:
            bad.append(f"task.{fname}: {x!r} vs {y!r}")

    ra = [r.pilot for r in sorted(pres, key=lambda r: r.rank_key)]
    rb = [r.pilot for r in sorted(results, key=lambda r: r.rank_key)]
    if ra != rb:
        bad.append("rank order differs")

    return ("parallel field score == serial field score, bit for bit", not bad,
            f"{len(bad)} differences: {bad[:3]}" if bad
            else f"{len(pres)} pilots on {os.cpu_count()} processes, identical "
                 f"board and identical rank order")


# --- the task compiler, which everything else is built on ----------------


def check_projection(task) -> Check:
    """Projected distance must agree with haversine over the task envelope.

    Every scored distance is a flat 2-D hypot in a local projection. If the
    projection is wrong, every distance is wrong by the same invisible amount.
    """
    from engine.geo import haversine
    worst = 0.0
    worst_pair = ""
    ws = task.waypoints
    for i, a in enumerate(ws):
        for b in ws[i + 1:]:
            flat = math.hypot(a.x - b.x, a.y - b.y)
            true = haversine(a.lat, a.lon, b.lat, b.lon)
            e = abs(flat - true)
            if e > worst:
                worst, worst_pair = e, f"{a.name}→{b.name}"
    ok = worst < 0.5
    return ("projection agrees with haversine to < 0.5 m across the task", ok,
            f"worst error {worst:.3f} m on {worst_pair} "
            f"(FAI tolerance is ±5 m, S7F 9.1.1)")


def check_optimiser(task) -> Check:
    """The optimised route is optimal — checked two ways, neither of them its own.

    DESIGN.md §8.1 calls the optimiser the highest-risk component in the
    system: on this task it is the difference between 80.80 km centre-to-centre
    and 53.97 km actually scored, and every distance-based number depends on
    it. So it gets two independent checks, because one is demonstrably not
    enough:

      1. **Local.** Perturb each optimised point around its own cylinder — rim
         and interior — and confirm nothing shorter exists one point at a time.

      2. **Global.** Solve the whole route again with a completely different
         algorithm: sample every cylinder and run an exact shortest-path DP
         over the layers. The DP result is a feasible route, so it is an upper
         bound on the optimum; if it beats the engine, the engine is wrong.

    Check 1 alone is not sufficient and this is not hypothetical: an earlier
    coordinate-descent optimiser produced a route 2.0 km too long that passed
    check 1 cleanly, because no SINGLE point could improve it. Check 2 caught
    it. The DP here deliberately uses a different resolution from the seeds
    inside optimise(), so it is a cross-check and not a restatement.
    """
    import math as _m
    xs, ys = task.opt_x, task.opt_y
    n = len(xs)
    s = task.route_start

    def total(px, py):
        return sum(_m.hypot(px[i + 1] - px[i], py[i + 1] - py[i])
                   for i in range(s, n - 1))

    base = total(xs, ys)

    # --- 1. local: one point at a time ---
    local_gain, local_where = 0.0, ""
    for i in range(s, n):
        w = task.waypoints[i]
        for k in range(360):
            a = k * _m.pi / 180.0
            for frac in (1.0, 0.98, 0.9, 0.5, 0.0):
                px, py = list(xs), list(ys)
                px[i] = w.x + _m.cos(a) * w.radius * frac
                py[i] = w.y + _m.sin(a) * w.radius * frac
                gain = base - total(px, py)
                if gain > local_gain:
                    local_gain, local_where = gain, w.name

    # --- 2. global: an independent shortest-path DP ---
    from engine.rules.route import shortest_route_dp
    pts = [(w.x, w.y, w.radius) for w in task.waypoints]
    gx, gy = shortest_route_dp(pts, s, 128, (1.0, 0.75, 0.5, 0.25))
    dp_len = total(gx, gy)
    global_gain = base - dp_len

    ok = local_gain <= 1.0 and global_gain <= 5.0
    return ("optimised route is optimal (perturbation + independent DP search)", ok,
            f"engine {base/1000:.4f} km · best single-point move "
            f"{local_gain:+.3f} m{' at ' + local_where if local_where else ''} · "
            f"independent DP {dp_len/1000:.4f} km ({global_gain:+.1f} m vs engine)")


def check_measurement_radius(task, tracks, params, now) -> Check:
    """How much distance is left at the instant a zone is validated. [S7F 9.1/9.3]

    This measures the seam between the two halves of Section 9. A pilot
    VALIDATES a control zone on reaching its tolerance boundary (9.1.1/9.2),
    and their distance is MEASURED to whichever radius
    rules.s7f_09_control_zones.MEASUREMENT_RADIUS names (9.3). If those are the
    same radius the residual is zero; if they are not, every pilot's distance
    jumps by the difference at every turnpoint.

    Reported rather than failed, because which radius 9.3 measures to is a
    question for the Code, not for a test. What is NOT acceptable is the four
    call sites disagreeing with each other, which is what this catches: they
    all read the same policy now, so the residual is one number for the whole
    field instead of one per code path.
    """
    from engine.rules.s7f_09_control_zones import MEASUREMENT_RADIUS

    residuals = []
    for tr in tracks:
        r = score_pilot(task, tr.fixes, now, params)
        if r.start_time is None:
            continue
        for i, fix_i in enumerate(r.tag_fix_index or []):
            if fix_i < 0 or i <= task.start_index or i >= len(task.waypoints):
                continue
            w = task.waypoints[i]
            f = tr.fixes[fix_i]
            d = math.hypot(f.x - w.x, f.y - w.y)
            residuals.append(max(0.0, d - w.measure))
    if not residuals:
        return (f"distance left at validation (MEASUREMENT_RADIUS="
                f"{MEASUREMENT_RADIUS})", True, "no validated zones")
    mean = sum(residuals) / len(residuals)
    return (f"distance left at validation (MEASUREMENT_RADIUS={MEASUREMENT_RADIUS})",
            True,
            f"{len(residuals):,} zone validations · mean {mean:.2f} m left to "
            f"fly at the validating fix, max {max(residuals):.2f} m "
            f"(zero iff 9.3 measures to the same radius 9.1.1 validates on)")


def check_tolerance(task) -> Check:
    """Every zone is the nominal radius ±5 m exactly (S7F 9.1.1, tolerance 0.0%)."""
    from engine.rules.s7f_09_control_zones import MEASUREMENT_RADIUS, measurement_radius

    bad = [w.name for w in task.waypoints
           if abs(w.outer - (w.raw_radius + 5.0)) > 1e-9
           or abs(w.inner - (w.raw_radius - 5.0)) > 1e-9
           or abs(w.measure - measurement_radius(w.raw_radius)) > 1e-9]
    return ("every tolerance zone is radius ±5 m exactly (S7F 9.1.1)", not bad,
            f"{bad}" if bad else f"{len(task.waypoints)} zones, "
            f"radii {min(w.raw_radius for w in task.waypoints):,.0f}–"
            f"{max(w.raw_radius for w in task.waypoints):,.0f} m; "
            f"distance measured to '{MEASUREMENT_RADIUS}' (S7F 9.3)")


# --- driver ---------------------------------------------------------------


def run(task=None, tracks=None, results=None, ts=None, params=None,
        now=None, igc_path: str | None = None,
        task_path: str | None = None, **_ignored) -> list[Check]:
    """Everything above, over one real scored field."""
    out: list[Check] = []
    out.append(check_launch_validity_is_measured(ts))
    out.append(check_earth_model_divergence(task, task_path))
    out.append(check_inlined_zone_test())
    out.append(check_distance_to_goal_reference(task, tracks, params, now))
    out.append(check_parser_equivalence(igc_path))
    out.append(check_tolerance(task))
    out.append(check_measurement_radius(task, tracks, params, now))
    out.append(check_projection(task))
    out.append(check_optimiser(task))
    out.append(check_allocation_sums(ts))
    out.append(check_totals(results, ts))
    out.append(check_caps(results, ts))
    out.append(check_thousand(results, ts))
    out.append(check_distance_monotone(results, ts))
    out.append(check_time_monotone(results, ts))
    out.append(check_best_gets_max(results, ts))
    out.append(check_lcmin_gets_max(results, ts))
    out.append(check_state_order(results, ts))
    out.append(check_distance_bounded(task, results))
    out.append(check_times_are_fixes(task, tracks, results))
    out.append(check_determinism(task, tracks, params, now))
    out.append(check_trace_is_free(task, tracks, params, now))

    # A moment mid-task, and a ladder of moments, are where live and official
    # can diverge. Pick one that is genuinely mid-race.
    mid = task.first_gate + 3600
    out.append(check_truncation_equals_now(task, tracks, params, mid))
    ladder = [task.first_gate + s for s in (0, 900, 1800, 3600, 5400, 7200, 10800)]
    out.append(check_distance_never_decreases(task, tracks, params, ladder))
    out.append(check_parallel_matches_serial(task, params, now, igc_path,
                                             results, ts))
    return out
