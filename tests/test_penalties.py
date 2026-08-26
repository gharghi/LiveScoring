"""S7F 13.5 — the penalty FILE: parsing, validation and pilot matching.

The arithmetic is tested in tests/test_s7f_13_special_cases.py. What is tested
here is everything around it, because a penalty is the one part of a score that
cannot be derived from a tracklog: it is typed in by a human, so the failure
modes are a typo in an ID, a missing reason, and a deduction that silently does
nothing. The last is the dangerous one — nobody notices a penalty that failed
to apply.
"""

from __future__ import annotations

import json
import os
import tempfile
from dataclasses import dataclass, field

from engine.rules.penalties import (Penalty, apply_penalties, load_penalties)


@dataclass
class _Track:
    pilot: str
    source_files: list = field(default_factory=list)


@dataclass
class _Result:
    pilot: str
    total_points: float
    penalties: list = field(default_factory=list)
    penalty_points: float = 0.0


def _write(doc) -> str:
    fd, path = tempfile.mkstemp(suffix=".json")
    with os.fdopen(fd, "w") as fh:
        json.dump(doc, fh)
    return path


def run() -> list[tuple[str, bool, str]]:
    out: list[tuple[str, bool, str]] = []

    # --- the three forms --------------------------------------------------
    p = Penalty("1380", "AIRSPACE", percent_own=100.0)
    out.append(("13.5 percent_own is a percentage of the pilot's own score",
                p.amount(289.7, 1000.0) == 289.7,
                f"100% of 289.7 → {p.amount(289.7, 1000.0)}"))
    p = Penalty("x", "r", percent_task=5.0)
    out.append(("13.5 percent_task is a percentage of the task pot",
                p.amount(289.7, 1000.0) == 50.0,
                "5% of a 1000-point task → 50, regardless of the pilot's score. "
                "NOT a form the Code defines; see the note in penalties.py"))
    p = Penalty("x", "r", points=50.0)
    out.append(("13.5 a flat deduction ignores both",
                p.amount(289.7, 1000.0) == 50.0, "50 points"))
    p = Penalty("x", "r", percent_own=10.0, points=25.0)
    out.append(("13.5 forms combine additively",
                abs(p.amount(500.0, 1000.0) - 75.0) < 1e-9,
                "10% of 500 plus 25 → 75"))

    # --- the file ---------------------------------------------------------
    path = _write({"Task": [{"pilot": "1380", "percent_own": 100,
                             "reason": "AIRSPACE"}]})
    got = load_penalties(path, "Task")
    out.append(("13.5 a penalty file loads and keys by task name",
                len(got) == 1 and got[0].pilot == "1380"
                and got[0].reason == "AIRSPACE",
                f"{len(got)} penalty for 'Task'"))
    out.append(("13.5 a task with no penalties loads as empty",
                load_penalties(path, "Some Other Task") == [],
                "an absent key is not an error"))
    out.append(("13.5 a missing file is not an error",
                load_penalties("/nonexistent/penalties.json", "Task") == [],
                "most competitions have none"))
    os.unlink(path)

    # --- validation: the failure modes that would otherwise be silent -----
    bad = _write({"Task": [{"pilot": "1380", "percent_own": 100}]})
    try:
        load_penalties(bad, "Task")
        ok, why = False, "accepted a penalty with no reason"
    except ValueError as e:
        ok, why = "reason" in str(e), str(e).split(":")[-1].strip()[:70]
    out.append(("13.5 a penalty with no reason is rejected", ok, why))
    os.unlink(bad)

    bad = _write({"Task": [{"pilot": "1380", "percentOwn": 100, "reason": "r"}]})
    try:
        load_penalties(bad, "Task")
        ok, why = False, "a typo'd key was silently ignored"
    except ValueError as e:
        ok, why = "unknown" in str(e), str(e).split(":")[-1].strip()[:70]
    out.append(("13.5 an unknown key is rejected, not ignored", ok, why))
    os.unlink(bad)

    bad = _write({"Task": [{"percent_own": 100, "reason": "r"}]})
    try:
        load_penalties(bad, "Task")
        ok = False
    except ValueError:
        ok = True
    out.append(("13.5 a penalty naming no pilot is rejected", ok,
                "'pilot' is required"))
    os.unlink(bad)

    # --- matching ---------------------------------------------------------
    tracks = [_Track("Marek DMOCHOWSKI", ["1380.igc"]),
              _Track("Andreas MALECKI", ["0157.igc"])]
    def fresh():
        return [_Result("Marek DMOCHOWSKI", 289.7),
                _Result("Andreas MALECKI", 992.0)]

    for key, label in (("1380", "the bare ID"), ("1380.igc", "the filename"),
                       ("Marek DMOCHOWSKI", "the full name"),
                       ("dmochowski", "a lowercase surname")):
        res = fresh()
        un = apply_penalties(res, tracks, [Penalty(key, "AIRSPACE",
                                                   percent_own=100.0)], 1000.0)
        hit = next(r for r in res if r.pilot == "Marek DMOCHOWSKI")
        out.append((f"13.5 a pilot can be named by {label}",
                    not un and hit.total_points == 0.0,
                    f"{key!r} → 289.7 − 289.7 = {hit.total_points}"))

    res = fresh()
    un = apply_penalties(res, tracks, [Penalty("9999", "typo", points=10.0)],
                         1000.0)
    out.append(("13.5 a penalty naming nobody in the field is REPORTED",
                len(un) == 1 and "no match" in un[0],
                f"returned {un} — a typo must reach a human, not vanish"))

    res = fresh()
    apply_penalties(res, tracks, [Penalty("1380", "over", points=9999.0)], 1000.0)
    hit = next(r for r in res if r.pilot == "Marek DMOCHOWSKI")
    out.append(("13.5 an over-large deduction floors the score at zero",
                hit.total_points == 0.0, "289.7 − 9999 → 0.0, not negative"))

    res = fresh()
    apply_penalties(res, tracks,
                    [Penalty("1380", "a", points=10.0),
                     Penalty("1380", "b", points=20.0)], 1000.0)
    hit = next(r for r in res if r.pilot == "Marek DMOCHOWSKI")
    out.append(("13.5 several penalties on one pilot all apply",
                abs(hit.total_points - 259.7) < 1e-9
                and len(hit.penalties) == 2
                and abs(hit.penalty_points - 30.0) < 1e-9,
                f"289.7 − 10 − 20 = {hit.total_points}"))

    res = fresh()
    apply_penalties(res, tracks, [], 1000.0)
    out.append(("13.5 no penalties leaves every score untouched",
                [r.total_points for r in res] == [289.7, 992.0],
                "the common case"))

    # --- the audit trail --------------------------------------------------
    p = Penalty("1380", "AIRSPACE", percent_own=100.0)
    out.append(("13.5 a penalty describes itself for the audit trail",
                "-100% of own points" in p.describe()
                and "AIRSPACE" in p.describe(),
                p.describe()))
    return out
