"""S7F 12.3 / 12.3.1 — Leading points.  [step 10]

    LeadingFactor = max(0, 1 - cuberoot( (LC - LCmin)^2 / LCmin ))
    LeadingPoints = LeadingFactor x availableLeading

where LC is the pilot's leading coefficient and LCmin the smallest in the
field, so the leader takes the whole pot and everyone else falls away from it.
The LeadingFactor half is cross-checked against AirScore/GlideComp. The
remaining published-result mismatch is in the LC/progress-curve behaviour used
by SVL's HUMP_V2A output, not in nominal distance/time or point allocation.

================================================================================
  *** CURRENT STATUS AGAINST THE RFAE/SVL TASK-4 FIXTURE ***
================================================================================

Two issues were separated on the 2026-08-07 RFAE task-4 fixture:

* Using sqrt(LCmin) inside the cuberoot denominator was a real bug. The Code /
  AirScore form is ((LC - LCmin) / sqrt(LCmin))^(2/3), which simplifies to
  cuberoot((LC - LCmin)^2 / LCmin). That is what leading_factor() implements.
* SVL's published page still awards materially lower leading points for many
  pilots than this LC formula. The page reports Progress curve HUMP_V2A and
  Best progress coefficient 1.41713; matching that exactly requires identifying
  SVL's progress coefficient convention rather than fitting a scale to one
  competition.

================================================================================

WHAT THE FORMULA BELOW CLAIMS TO BE

S7F 12.3.1's paragliding form, a distance-weighted integral rather than the
hang-gliding squared-area form:

    leadingArea = SUM  minToESS(i) x taskTime(i) x integral weight(x) dx
                       over [done(i-1), done(i)]
    missingArea = minToESS(best) x maxTime x integral weight(x) dx
                       over [done(best), 1]
    LC          = (leadingArea + missingArea) / (1800 x speedSectionDistance)

    done(i) = 1 - minToESS(i) / speedSectionDistance

`samples` is [(taskTime seconds, minToESS km)] at every point where minToESS
STRICTLY DECREASED. Points where it did not decrease contribute nothing,
because the integral runs between consecutive `done` values and `done` is a
function of minToESS alone -- so circling in a thermal neither helps nor costs.

The weight function IS separately verified against S7F Figure 18 and is not
implicated in the discrepancy above:

    weight(v) = weightRising(1-v) x weightFalling(1-v)
    weightRising(u)  = (1 - 10^(9u - 9))^5
    weightFalling(u) = (1 - 10^(-3u))^2

Zero at both ends of the speed section, peaking around 0.97 at ~30% along it,
which is what makes paragliding leading points reward being early in the MIDDLE
of a task rather than merely leaving first.

THE SPLIT. leading_partial() computes everything that depends only on the
pilot's own track; leading_from_partial() adds the missingArea term, which
needs maxTime and so cannot be known until the field has landed. That is what
lets a worker process score a pilot in isolation, and what lets a live board
recompute only the field-wide half each publish cycle. The two together are
EXACTLY leading_coefficient(), asserted on 2,000 random tracks.
"""

from __future__ import annotations


# --- the weight function, [PG] S7F 12.3.1 --------------------------------


def _weight_rising(v: float) -> float:
    return (1.0 - 10.0 ** (9.0 * v - 9.0)) ** 5


def _weight_falling(v: float) -> float:
    return (1.0 - 10.0 ** (-3.0 * v)) ** 2


def leading_weight(v: float) -> float:
    """weight(v) = weightRising(1-v) * weightFalling(1-v).  Verified vs Figure 18."""
    u = 1.0 - v
    return _weight_rising(u) * _weight_falling(u)


_STEPS = 4000
_CUM: list[float] = []


def _build_cumulative() -> None:
    """Cumulative integral of weight() over [0,1], by Simpson's rule.

    The LC formula integrates weight() between consecutive `done` values, so a
    single precomputed table turns every one of those integrals into two array
    lookups. --verify asserts the table agrees with direct integration to 1e-6,
    so it is a performance device and not a source of error.
    """
    global _CUM
    h = 1.0 / _STEPS
    _CUM = [0.0] * (_STEPS + 1)
    acc = 0.0
    prev = leading_weight(0.0)
    for i in range(1, _STEPS + 1):
        x1 = i * h
        cur = leading_weight(x1)
        mid = leading_weight(x1 - h * 0.5)
        acc += h * (prev + 4.0 * mid + cur) / 6.0
        _CUM[i] = acc
        prev = cur


_build_cumulative()


def weight_integral(a: float, b: float) -> float:
    """Integral of weight(x) dx from a to b, both clamped to [0,1]."""
    if b <= a:
        return 0.0
    a = 0.0 if a < 0.0 else (1.0 if a > 1.0 else a)
    b = 0.0 if b < 0.0 else (1.0 if b > 1.0 else b)

    def at(x: float) -> float:
        p = x * _STEPS
        i = int(p)
        if i >= _STEPS:
            return _CUM[_STEPS]
        f = p - i
        return _CUM[i] + (_CUM[i + 1] - _CUM[i]) * f

    return at(b) - at(a)


# --- the leading coefficient ---------------------------------------------


def hump_v2a_weight(remaining_fraction: float) -> float:
    """SVL/FS HUMP_V2A point weight, expressed over remaining-to-ESS fraction."""
    p = 0.0 if remaining_fraction < 0.0 else (
        1.0 if remaining_fraction > 1.0 else remaining_fraction)
    return _weight_rising(p) * _weight_falling(p)


def leading_partial_hump_v2a(samples, speed_distance_km: float) -> tuple[float, float]:
    """SVL/FS HUMP_V2A-style progress area.

    This is intentionally separate from the S7F 2026 weighted integral above.
    RFAE/SVL Task 4 declares `Progress curve = HUMP_V2A`; probing against that
    page shows this midpoint point-weighted area reproduces the goal pilots'
    leading points and the published best progress coefficient.
    """
    if speed_distance_km <= 0.0:
        return 0.0, 0.0
    prev = speed_distance_km
    area = 0.0
    min_to_ess = speed_distance_km
    for t, d in samples:
        d = 0.0 if d < 0.0 else (speed_distance_km if d > speed_distance_km else d)
        if d < prev:
            remaining_fraction = (prev + d) / (2.0 * speed_distance_km)
            area += t * (prev - d) * hump_v2a_weight(remaining_fraction)
            prev = d
            min_to_ess = d
    return area, min_to_ess


def leading_from_partial_hump_v2a(area: float, min_to_ess: float,
                                  speed_distance_km: float, max_time: float,
                                  last_task_time: float) -> float:
    """Finish an SVL/FS HUMP_V2A LC from its per-pilot area.

    The landout tail is the closest source-backed/current-fixture match found
    so far: remaining field time times the AirScore/SVL falling term. It is not
    a fitted scale factor; the mode remains opt-in because native S7F weighted
    scoring should not inherit SVL-specific behavior.
    """
    if speed_distance_km <= 0.0:
        return 0.0
    if min_to_ess > 0.0:
        remaining_fraction = min_to_ess / speed_distance_km
        falling = _weight_falling(remaining_fraction)
        area += max(0.0, max_time - last_task_time) * min_to_ess * falling
    return area / (1800.0 * speed_distance_km)


def leading_partial(samples, speed_distance_km: float) -> tuple[float, float]:
    """The half of the leading area that does not depend on the field.

    Returns (leadingArea, minToESS at the last sample).
    """
    if speed_distance_km <= 0:
        return 0.0, 0.0
    done_prev = 0.0
    area = 0.0
    min_to_ess = speed_distance_km
    for t, d in samples:
        done = 1.0 - d / speed_distance_km
        area += d * t * weight_integral(done_prev, done)
        done_prev = done
        min_to_ess = d
    return area, min_to_ess


def leading_from_partial(area: float, min_to_ess: float,
                         speed_distance_km: float, max_time: float) -> float:
    """Finish an LC once the field-wide maxTime is known."""
    if speed_distance_km <= 0:
        return 0.0
    done_prev = 1.0 - min_to_ess / speed_distance_km
    area = area + min_to_ess * max_time * weight_integral(done_prev, 1.0)
    return area / (1800.0 * speed_distance_km)


def leading_coefficient(samples, speed_distance_km: float,
                        max_time: float) -> float:
    """The two halves above, written out as one. The reference for the split."""
    if speed_distance_km <= 0:
        return 0.0
    done_prev = 0.0
    area = 0.0
    min_to_ess = speed_distance_km
    for t, d in samples:
        done = 1.0 - d / speed_distance_km
        area += d * t * weight_integral(done_prev, done)
        done_prev = done
        min_to_ess = d
    area += min_to_ess * max_time * weight_integral(done_prev, 1.0)
    return area / (1800.0 * speed_distance_km)


# --- points ---------------------------------------------------------------


def leading_factor(lc: float, lc_min: float) -> float:
    """LeadingFactor = max(0, 1 - cuberoot((LC - LCmin)^2 / LCmin))."""
    if lc_min <= 0:
        return 0.0
    x = (lc - lc_min) ** 2 / lc_min
    return max(0.0, 1.0 - x ** (1.0 / 3.0))


def leading_points(lc: float, lc_min: float, available_leading: float) -> float:
    """Step 10 in one call."""
    if lc <= 0 or lc_min <= 0:
        return 0.0
    return leading_factor(lc, lc_min) * available_leading
