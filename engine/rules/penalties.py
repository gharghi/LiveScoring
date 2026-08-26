"""S7F 13.5 — Penalties.  [step 16, and the last thing applied]

Airspace infringements, dangerous flying, late tracklog submission, a missing
declaration: penalties are decided by the meet director and the jury, not
derived from the tracklog, so they are an INPUT to scoring rather than an
output of it. That is why this module reads a file and does not compute
anything from the flights.

It was added after comparing against a published result. The reference
competition's own table carries

    PEN  1380  Marek DMOCHOWSKI  -100% of own points  AIRSPACE

and this engine scored that pilot 289.7 while the official gave 0.0 — a 290
point difference that had nothing to do with any formula, only with a rule the
engine did not know existed. It was the single largest discrepancy in the whole
comparison.

THREE KINDS, matching how tables actually express them:

    percent_own    a percentage of the pilot's OWN total for this task
    percent_task   a percentage of the task's available points
                   (1000 x taskValidity), so it does not shrink for a pilot
                   who already scored badly
    points         a flat number of points

All are subtractive. A pilot's total is floored at zero — S7F does not create
negative task scores.

ORDER MATTERS AND IS NOT NEGOTIABLE. Penalties apply LAST, to the total after
distance, time and leading have been summed and rounded. A percentage penalty
has to be a percentage of something final, and applying one before the rounding
in S7F 12 would give a different answer.

THE FILE. JSON, keyed by task name, matching pilots by the ID that is also the
IGC filename, or by name:

    {
      "Task": [
        {"pilot": "1380", "percent_own": 100, "reason": "AIRSPACE"},
        {"pilot": "0042", "points": 50,       "reason": "late tracklog"},
        {"pilot": "R. Smith", "percent_task": 5, "reason": "dangerous flying"}
      ]
    }

Every penalty must carry a `reason`. A points deduction with no stated reason
is not something a pilot can contest, and this whole engine is built around
being able to answer a protest — a penalty is the part of a score most likely
to be protested and the only part not derivable from the tracklog.

Penalties appear in the per-pilot audit (`--explain`) as their own section with
the reason quoted, and are listed on the leaderboard footer, so a score that
differs from the arithmetic above it always says why.

VERIFIED: reproduces the AIRSPACE -100% in the reference published result.
"""

from __future__ import annotations

import json
import os
import re
from dataclasses import dataclass, field


@dataclass(slots=True)
class Penalty:
    pilot: str                      # ID (IGC filename stem) or pilot name
    reason: str
    percent_own: float = 0.0        # % of the pilot's own total
    percent_task: float = 0.0       # % of 1000 x taskValidity
    points: float = 0.0             # flat deduction
    applied: float = 0.0            # filled in when applied, for the audit

    def amount(self, own_total: float, task_points: float) -> float:
        """What this penalty actually subtracts, in points."""
        return (own_total * self.percent_own / 100.0
                + task_points * self.percent_task / 100.0
                + self.points)

    def describe(self) -> str:
        bits = []
        if self.percent_own:
            bits.append(f"-{self.percent_own:g}% of own points")
        if self.percent_task:
            bits.append(f"-{self.percent_task:g}% of task points")
        if self.points:
            bits.append(f"-{self.points:g} points")
        return f"{' and '.join(bits) or 'no deduction'} ({self.reason})"


_KEYS = {"pilot", "reason", "percent_own", "percent_task", "points"}


def load_penalties(path: str, task_name: str) -> list[Penalty]:
    """Read the penalties for one task. Missing file is not an error.

    Unknown keys are rejected rather than ignored: a typo in a penalty file
    silently doing nothing is worse than a config error, because nobody
    notices a penalty that failed to apply.
    """
    if not path or not os.path.exists(path):
        return []
    with open(path, encoding="utf-8") as fh:
        doc = json.load(fh)

    entries = doc.get(task_name)
    if entries is None:
        # Allow a bare list for a single-task file.
        entries = doc if isinstance(doc, list) else []
    out: list[Penalty] = []
    for i, e in enumerate(entries):
        if not isinstance(e, dict):
            raise ValueError(f"{path}: entry {i} is not an object")
        unknown = set(e) - _KEYS
        if unknown:
            raise ValueError(f"{path}: entry {i} has unknown keys {sorted(unknown)}")
        if not e.get("pilot"):
            raise ValueError(f"{path}: entry {i} has no 'pilot'")
        if not e.get("reason"):
            raise ValueError(
                f"{path}: entry {i} ({e['pilot']}) has no 'reason' — every "
                f"penalty must state one, so a pilot can contest it")
        out.append(Penalty(
            pilot=str(e["pilot"]), reason=str(e["reason"]),
            percent_own=float(e.get("percent_own", 0.0)),
            percent_task=float(e.get("percent_task", 0.0)),
            points=float(e.get("points", 0.0)),
        ))
    return out


def _ids_for(track) -> set[str]:
    """Every string a penalty may name this pilot by."""
    ids = set()
    for name in getattr(track, "source_files", ()) or ():
        base = name.rsplit("/", 1)[-1]
        ids.add(base)
        stem = base.rsplit(".", 1)[0]
        ids.add(stem)
        m = re.match(r"^(\d+)", stem)
        if m:
            ids.add(m.group(1))
            ids.add(str(int(m.group(1))))       # 0157 and 157
    return ids


def apply_penalties(results, tracks, penalties: list[Penalty],
                    task_points: float) -> list[str]:
    """Subtract penalties from totals, in place. Returns unmatched pilot keys.

    `task_points` is 1000 x taskValidity, for percent_task.

    An unmatched penalty is RETURNED, not ignored: a penalty naming a pilot who
    is not in the field is either a typo or a missing tracklog, and both need
    a human. The caller is expected to report them loudly.
    """
    if not penalties:
        return []

    by_pilot = {}
    for r in results:
        by_pilot.setdefault(r.pilot.strip().lower(), []).append(r)
    by_id = {}
    tr_by_pilot = {t.pilot: t for t in tracks}
    for r in results:
        t = tr_by_pilot.get(r.pilot)
        if t is None:
            continue
        for key in _ids_for(t):
            by_id.setdefault(key.lower(), []).append(r)

    unmatched = []
    for pen in penalties:
        key = pen.pilot.strip().lower()
        targets = by_id.get(key) or by_pilot.get(key)
        if not targets:
            # last resort: substring on the pilot name
            targets = [r for r in results if key in r.pilot.lower()]
        if len(targets) != 1:
            detail = "no match" if not targets else (
                "ambiguous: " + ", ".join(r.pilot for r in targets[:3])
            )
            unmatched.append(
                f"{pen.pilot} ({detail})")
            continue
        r = targets[0]
        amount = pen.amount(r.total_points, task_points)
        pen.applied = amount
        r.penalties.append(pen)
        r.penalty_points += amount
        r.total_points = round(max(0.0, r.total_points - amount), 1)
    return unmatched
