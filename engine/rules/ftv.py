"""S7F 16 — FTV, Formula Total Validity.  [step 17]  NOT IMPLEMENTED.

FTV is a COMPETITION-level rule, not a task-level one, which is why its absence
does not affect any single task's result and why it sits at the end of the
pipeline.

It lets a pilot drop their worst tasks: instead of summing every task score,
the overall result counts each pilot's best tasks up to a validity threshold
(commonly 100% of the total validity of all tasks, minus a percentage). The
effect is that a pilot who missed one day through no fault of their own is not
eliminated from the competition by it.

Implementing it needs the full set of task results for the competition, which
this engine has never been handed -- it scores one task at a time. That is the
real prerequisite, not the formula.

NOT IMPLEMENTED. A single task's leaderboard is unaffected; an overall standing
computed by naively summing task scores would be wrong wherever the meet uses
FTV, and this engine does not compute overall standings at all.
"""

from __future__ import annotations


def ftv_scores(*_args, **_kwargs):
    """S7F 16 — NOT IMPLEMENTED."""
    raise NotImplementedError(
        "S7F 16 FTV is a competition-level rule and is not implemented "
        "(engine/rules/ftv.py)")
