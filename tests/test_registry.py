"""The rule registry itself, and the not-implemented stubs.

Small, but the registry is what `--rules` and `--verify`'s coverage footer both
read, so a row that has drifted from reality is a lie told in two places.
"""

from __future__ import annotations

import os

import engine.rules as R


def run() -> list[tuple[str, bool, str]]:
    out: list[tuple[str, bool, str]] = []

    out.append(("registry: every element is reachable by its own reference",
                all(R.by_ref(r.ref) is not None for r in R.ALL),
                f"{len(R.ALL)} elements ({len(R.ALGORITHMS)} geometry + "
                f"{len(R.STAGES)} scoring)"))

    missing = [r.module for r in R.ALL if not os.path.exists(r.module)]
    out.append(("registry: every named file exists", not missing,
                f"missing: {missing}" if missing
                else "all file paths resolve"))

    bad = [r.ref for r in R.ALL if not r.ref or not r.title or not r.verified]
    out.append(("registry: every row states a reference, a title and how it "
                "is verified", not bad, f"incomplete: {bad}" if bad
                else "no row can claim a status without saying what backs it"))

    dup = [r.step for r in R.STAGES]
    out.append(("registry: scoring steps are numbered in pipeline order",
                dup == sorted(dup), f"steps {dup}"))

    known = {R.IMPLEMENTED, R.MISSING, R.NA_PG, "SUSPECT", "KNOWN GAP",
             "WRONG", "OPEN QUESTION", "REFERENCE", "assumption",
             "none [PG]", "NOT WIRED IN"}
    unknown = {r.status for r in R.ALL} - known
    out.append(("registry: every status is one the renderer can colour",
                not unknown, f"unknown: {unknown}" if unknown
                else f"{len(known)} recognised statuses"))

    open_items = [r.ref for r in R.ALL if r.status in
                  (R.MISSING, "SUSPECT", "KNOWN GAP", "WRONG", "OPEN QUESTION",
                   "NOT WIRED IN")]
    out.append(("registry: the open items are still open", bool(open_items),
                f"{len(open_items)}: {', '.join(open_items)} — this check exists "
                f"so closing one is a deliberate edit here, not a silent drift"))

    # --- the stubs must refuse loudly, not return a plausible number ------
    from engine.rules.ftv import ftv_scores
    try:
        ftv_scores()
        ok, why = False, "returned instead of raising"
    except NotImplementedError as e:
        ok, why = "16" in str(e) or "FTV" in str(e), str(e)[:70]
    out.append(("S7F 16 FTV refuses rather than returning a plausible number",
                ok, why))
    return out
