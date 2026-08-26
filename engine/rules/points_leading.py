"""S7F 12.3 / 12.3.1 — Leading points.  [step 10]

    LeadingFactor = max(0, 1 - cuberoot( (LC - LCmin)^2 / sqrt(LCmin) ))
    LeadingPoints = LeadingFactor x availableLeading

where LC is the pilot's leading coefficient and LCmin the smallest in the
field, so the leader takes the whole pot and everyone else falls away from it.
The LeadingFactor half is not in doubt. The LC half is.

================================================================================
  *** THIS IS THE ONE ELEMENT KNOWN TO BE WRONG. READ BEFORE TRUSTING IT. ***
================================================================================

Against the officially published result for the reference task this module is
out by a MEAN OF 18.1 POINTS PER PILOT, worst case 58, while every other
element is within 0.4. It also puts the wrong pilot at LCmin.

How that was established, so it can be rechecked: because
LeadingFactor = 1 - cuberoot((LC-LCmin)^2 / sqrt(LCmin)), the quantity
(1 - factor)^1.5 is an AFFINE function of LC. So inverting the published
leading points gives each pilot's official LC up to the unknown LCmin, and
whichever candidate formula is a straight line against (1 - factor)^1.5 is the
one the official used -- no need to know their LCmin. Over the 119 pilots with
an uncensored factor:

    candidate leading coefficient              r^2      LCmin pilot
    ------------------------------------------------------------------
    sum d*t*integral weight(done)   <- THIS   0.9523    1073  wrong
    integral d dt / SS                        0.9537    0157  right
    integral weight(done)*d dt / SS           0.9449    0157  right
    integral d^1.5 dt / SS^1.5                0.9920    0157  right
    integral d^2 dt / SS^2  (classic form)    0.9938    1073  wrong
    integral d^3 dt / SS^3                    0.8904    1073  wrong

The official's LCmin pilot must be 0157, who scored the entire pot. Two
conclusions and only two: the implemented form is the WORST plausible
candidate, and the right one is in the `integral d^k dt` family, unweighted,
with k near 2.

WHY IT HAS NOT BEEN REPLACED. Sweeping k continuously peaks at k = 1.8
(r^2 = 0.9975), not at a round number, which is the signature of a model that
is close but still misspecified -- and the fitted exponent is biased anyway,
because this engine's own d(t) values are 0.42% short (VERIFICATION.md §5.4).
Fitting a rule to one task's output and shipping it as verified is exactly the
mistake that produced the optimiser bug in §4.1. What is needed is the S7F
12.3.1 text, or a second task to fit against and a third to test on.

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

import math


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
    """LeadingFactor = max(0, 1 - cuberoot((LC - LCmin)^2 / sqrt(LCmin)))"""
    if lc_min <= 0:
        return 0.0
    x = (lc - lc_min) ** 2 / math.sqrt(lc_min)
    return max(0.0, 1.0 - x ** (1.0 / 3.0))


def leading_points(lc: float, lc_min: float, available_leading: float) -> float:
    """Step 10 in one call."""
    if lc <= 0 or lc_min <= 0:
        return 0.0
    return leading_factor(lc, lc_min) * available_leading
