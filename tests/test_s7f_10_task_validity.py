"""S7F 10 — task validity."""

from __future__ import annotations

from dataclasses import dataclass

from engine.rules.params import GapParams
from engine.rules.s7f_10_task_validity import (best_time_to_ess,
                                               distance_validity,
                                               launch_validity, task_validity,
                                               time_validity)


@dataclass
class _P:
    speed_section_time: float | None
    ess_time: float | None


def _p():
    return GapParams(nominal_distance=60000, minimum_distance=5000,
                     nominal_time=5400)


def run() -> list[tuple[str, bool, str]]:
    out: list[tuple[str, bool, str]] = []
    p = _p()

    # --- 10.1 -------------------------------------------------------------
    out.append(("10.1 validity is 1 at 96% launched and above",
                all(abs(launch_validity(n, 100, p) - 1.0) < 1e-12
                    for n in (96, 97, 100)),
                "NominalLaunch is 96%: the last 4% is assumed to be gear "
                "failure and illness, not a verdict on the day"))
    vals = [launch_validity(n, 100, p) for n in range(0, 97, 4)]
    out.append(("10.1 validity rises monotonically to the threshold",
                all(b > a - 1e-12 for a, b in zip(vals, vals[1:]))
                and vals[0] == 0.0,
                f"0/100 → 0.000000, 48/100 → {launch_validity(48, 100, p):.6f}, "
                f"92/100 → {launch_validity(92, 100, p):.6f}"))
    out.append(("10.1 the published cubic, sampled", all(
        abs(launch_validity(n, 100, p)
            - min(1.0, 0.028 * (n / 96) + 2.917 * (n / 96) ** 2
                  - 1.944 * (n / 96) ** 3)) < 1e-12
        for n in (24, 48, 72, 90)),
        "0.028 LVR + 2.917 LVR² − 1.944 LVR³"))
    out.append(("10.1 the clamp: the raw cubic exceeds 1 at LVR = 1",
                abs((0.028 + 2.917 - 1.944) - 1.001) < 1e-12
                and launch_validity(100, 100, p) == 1.0,
                "raw 1.001, clamped to 1.0. The published formula shows NO "
                "clamp here while 10.3 shows one — see the note in the module"))
    out.append(("10.1 zero pilots present is handled",
                launch_validity(0, 0, p) == 0.0, "not a ZeroDivisionError"))

    # --- 10.2 -------------------------------------------------------------
    out.append(("10.2 validity is 1 with the field at nominal distance",
                abs(distance_validity([60000.0] * 10, 60000.0, p) - 1.0) < 1e-12,
                "Area = ((0.3+1)(60−5))/2 = 35.75 km; each pilot is 55 km over "
                "the minimum"))
    out.append(("10.2 distance below the minimum contributes nothing",
                distance_validity([5000.0] * 10, 5000.0, p) == 0.0
                and distance_validity([1000.0] * 10, 5000.0, p) == 0.0,
                "a pilot who lands on the hill neither helps nor hurts"))
    half = 5000.0 + 0.5 * ((0.3 + 1.0) * (60000.0 - 5000.0)) / 2.0
    out.append(("10.2 half of NominalDistArea each gives 0.5",
                abs(distance_validity([half] * 10, half, p) - 0.5) < 1e-9,
                f"10 pilots at {half/1000:.3f} km → 0.500000"))
    same = [20000.0] * 10
    below, above = (distance_validity(same, 20000.0, p),
                    distance_validity(same, 90000.0, p))
    out.append(("10.2 a best distance beyond nominal raises the bar",
                above < below < 1.0,
                f"identical field, best 20 km → {below:.6f}; best 90 km → "
                f"{above:.6f}  (NominalDistArea 35.75 → 40.25 km)"))
    out.append(("10.2 an empty field has no distance validity",
                distance_validity([], 0.0, p) == 0.0, "0.0"))

    # --- 10.3 -------------------------------------------------------------
    out.append(("10.3 validity is 1 once best time reaches nominal",
                time_validity(5400, 0, p) == 1.0
                and time_validity(99999, 0, p) == 1.0,
                "a LONGER best time gives HIGHER validity, capped at nominal — "
                "the formula asks 'was this long enough to be a test'"))
    for frac in (0.25, 0.5, 0.75):
        want = -0.271 + 2.912 * frac - 2.098 * frac ** 2 + 0.457 * frac ** 3
        out.append((f"10.3 the published cubic at {frac:.0%} of nominal",
                    abs(time_validity(5400 * frac, 0, p) - max(0.0, want)) < 1e-12,
                    f"{time_validity(5400 * frac, 0, p):.6f}"))
    out.append(("10.3 the max(0,…) clamp is load-bearing at short times",
                time_validity(1, 0, p) == 0.0,
                "the cubic is NEGATIVE below TVR ≈ 0.1002; at TVR → 0 it is "
                "−0.271"))
    by_time = time_validity(0.5 * 5400, 0.0, p)
    by_dist = time_validity(None, 0.5 * 60000, p)
    out.append(("10.3 the no-ESS fallback substitutes distance for time exactly",
                abs(by_time - by_dist) < 1e-12,
                f"TVR 0.5 from time → {by_time:.6f}; from distance → "
                f"{by_dist:.6f}"))

    field = [_P(5000.0, 90.0), _P(6000.0, 100.0), _P(None, None)]
    out.append(("10.3 best_time_to_ess takes the fastest ESS crossing",
                best_time_to_ess(field) == 5000.0,
                "10.3 says ESS where 9.4.1 says GOAL — different populations "
                "whenever ESS and goal are different cylinders"))
    out.append(("10.3 no ESS means no time, sending 10.3 to distance",
                best_time_to_ess([_P(None, None)]) is None, "None"))

    # --- 10 ---------------------------------------------------------------
    out.append(("10 task validity is the product of the three",
                abs(task_validity(0.9, 0.8, 0.7) - 0.504) < 1e-12,
                "0.9 × 0.8 × 0.7 = 0.504"))
    out.append(("10 any coefficient at zero zeroes the task",
                task_validity(0.0, 1.0, 1.0) == 0.0
                and task_validity(1.0, 1.0, 0.0) == 0.0,
                "a task nobody launched into, or that was over instantly, is "
                "worth nothing"))
    return out
