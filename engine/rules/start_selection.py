"""Which SSS crossing is the pilot's start.  [algorithm E — S7F 8.1]

A pilot crosses the start cylinder many times. On a task whose SSS sits on the
takeoff cylinder, pilots thermalling near launch produce a MEDIAN OF 6 and up
to 36 crossings each before they leave. Exactly one of them is the start, and
choosing wrong is the most consequential single error the engine can make: a
pilot who did not start scores minimum distance no matter how far they flew, so
a false negative silently deletes a whole flight and a false positive invents
one.

--------------------------------------------------------------------------
THE RULE
--------------------------------------------------------------------------
S7F 8.1 allows RE-STARTING only in a Race with MULTIPLE start gates, and in a
Time Trial. Two cases follow.

  SINGLE-GATE RACE — re-starting does not apply. A pilot cannot take a later
  start, so their start is simply their LAST SSS crossing before they validated
  the next control zone. That reading is consistent with S7F 13.3, which
  defines an early start by the pilot's LAST SSS crossing.

  MULTIPLE GATES, OR NOT A RACE — every crossing after a gate is a candidate.
  Each is replayed in full, and the scored one is the start that produced the
  biggest distance; if several reached goal, the LAST start after which goal
  was reached.

CROSSINGS BEFORE THE FIRST GATE are not candidates at all. If a pilot has
crossings before the gate and none after, they are an early starter
(S7F 13.3 — see rules/early_start.py). If they have both, they started
normally and are not an early starter: only the last crossing matters.

--------------------------------------------------------------------------
THE DEGENERATE CASE
--------------------------------------------------------------------------
When the control zone AFTER the SSS lies inside it — a concentric start
cylinder and first turnpoint, which the reference task has — the next zone is
validated BEFORE any start crossing, so "last crossing before the next zone" is
meaningless and selects nothing. The fallback is the biggest-distance rule over
the EARLIEST crossings: a start is near the beginning of a flight, and a pilot
re-crossing the SSS hours later on the way home is not re-starting.

--------------------------------------------------------------------------
WHY THE CANDIDATE LIST IS CAPPED
--------------------------------------------------------------------------
Each candidate costs a full replay of the pilot's track. Evaluating all 36 of
a thermalling pilot's crossings turns a 4 ms pilot into a 200 ms one for no
change in result, because the rules do not ask for it. MAX_START_CANDIDATES
caps it at 8, taking the earliest.

--------------------------------------------------------------------------
DIRECTION IS IRRELEVANT — S7F 6.2.1
--------------------------------------------------------------------------
The SSS is validated by ANY crossing of its tolerance band. The declared
EXIT/ENTER is advisory and display-only; gating on it makes a task whose
declared direction is wrong score as "nobody started". `--verify` scores the
same synthetic flight against a task declared EXIT and the same task declared
ENTER and requires identical results.

VERIFIED: seven hand-built synthetic flights with a known right answer, since
real tracklogs only ever show what the pilots actually did — normal start,
never entered, early start, ENTER start, two-gate restart, single-gate
no-restart, and EXIT-vs-ENTER equivalence. See engine/selftest.py.
"""

from __future__ import annotations

MAX_START_CANDIDATES = 8


def is_multi_start(start_type: str, n_gates: int) -> bool:
    """S7F 8.1 — does re-starting apply to this task?"""
    return n_gates > 1 or not start_type.upper().startswith("RACE")


def select_candidates(candidates, multi_start: bool,
                      next_zone_validated_at: float | None,
                      max_candidates: int = MAX_START_CANDIDATES):
    """Narrow every post-gate SSS crossing to the ones worth replaying.

    `candidates` is [(fix_index, crossing_time)], in time order.
    Returns (narrowed_candidates, rule_applied) — the second is the sentence
    that goes into the per-pilot audit trail, so a protest gets the reasoning
    and not just the answer.
    """
    if not candidates:
        return [], "no crossing of the SSS tolerance zone after the gate"

    if multi_start:
        return candidates[:max_candidates], (
            "S7F 8.1 re-starting applies (multiple gates, or not a RACE) — "
            "every crossing is a candidate start; the scored one is the start "
            "that produced the biggest distance, or the LAST start after which "
            "goal was reached")

    if next_zone_validated_at is not None:
        before = [c for c in candidates if c[1] <= next_zone_validated_at]
        if before:
            return before[-1:], (
                "S7F 8.1 single gate — no re-starting; the scored start is the "
                "LAST SSS crossing before the next control zone was validated "
                "(consistent with S7F 13.3, which defines an early start by "
                "the pilot's last SSS crossing)")

    return candidates[:max_candidates], (
        "the zone after the SSS was validated before any start crossing "
        "(concentric cylinders) — falling back to the biggest-distance rule "
        "over the earliest crossings")


def better_start(run, best, t_cross: float) -> bool:
    """Is this candidate's replay better than the best so far? S7F 8.1.

    `run`/`best` each need `goal` (crossing time or None), `raw` (distance) and
    `start_cross`. Goal beats no-goal; among goal starts the LATEST wins;
    otherwise the biggest distance wins.
    """
    if best is None:
        return True
    if run["goal"] is not None:
        return best["goal"] is None or t_cross >= best["start_cross"]
    return best["goal"] is None and run["raw"] > best["raw"]
