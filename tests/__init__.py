"""The test suite, one module per Sporting Code section.

Kept out of engine/ so the rules files stay rules files and so the suite can be
run on its own:

    python3 -m tests                    everything that needs no tracklogs
    python3 -m tests --igc igcs --gate 11:30 --deadline 15:00
                                        ...plus the checks that need a field
    python3 -m tests 12                 just Section 12
    python3 -m tests --list             what there is

`./run.py --verify` runs exactly the same suite through the same registry, so
there is one set of tests and two ways in.

WHAT A TEST IS HERE

Every check returns `(name, ok, detail)` and `detail` is printed whether it
passes or fails. That is deliberate: a passing check that says
"111 pilots in goal, LCmin 0.4676" tells a reader what the engine actually did,
where a bare green tick tells them nothing. Several of the numbers that turned
out to be wrong over this project's life were sitting in the detail line of a
passing check long before anyone noticed.

Checks are graded by what they can establish — see VERIFICATION.md §1:

    A  against a worked example published in the Sporting Code
    B  two implementations of the same thing agree (reference vs optimised)
    C  a property the result must satisfy, over a real field
    D  an independent algorithm reaches the same answer
    E  the whole result matches an officially published one   (--compare)
"""

from __future__ import annotations

import importlib
from dataclasses import dataclass, field

# (name, ok, detail)
Check = tuple[str, bool, str]


@dataclass(frozen=True, slots=True)
class Suite:
    """One test module."""

    key: str            # "12", "geometry", ...
    ref: str            # "S7F 12" or "—"
    title: str
    module: str         # dotted path under tests/
    needs_field: bool = False   # requires tracklogs + a scored field


SUITES: tuple[Suite, ...] = (
    Suite("7.1", "S7F 7.1", "The nine algorithms",
          "tests.test_s7f_07_algorithms"),
    Suite("9", "S7F 9", "Control zones, crossings, distance, best time",
          "tests.test_s7f_09_control_zones"),
    Suite("10", "S7F 10", "Task validity", "tests.test_s7f_10_task_validity"),
    Suite("11", "S7F 11", "Points allocation", "tests.test_s7f_11_allocation"),
    Suite("12", "S7F 12", "Pilot points", "tests.test_s7f_12_pilot_points"),
    Suite("13", "S7F 13", "Special cases", "tests.test_s7f_13_special_cases"),
    Suite("geometry", "—", "Earth model, cylinders, route, distance flown",
          "tests.test_geometry"),
    Suite("penalties", "S7F 13.5", "Penalty file handling and matching",
          "tests.test_penalties"),
    Suite("start", "S7F 8.1", "Start selection, on synthetic flights",
          "tests.test_start_selection"),
    Suite("registry", "—", "The rule registry and the stubs",
          "tests.test_registry"),
    Suite("field", "—", "Invariants over a real scored field",
          "tests.test_field_invariants", needs_field=True),
)


def by_key(key: str) -> Suite | None:
    for s in SUITES:
        if s.key == key or s.ref.replace("S7F ", "") == key:
            return s
    return None


def run_suite(s: Suite, **ctx) -> list[Check]:
    """Run one module. Field suites get the scored field via **ctx."""
    mod = importlib.import_module(s.module)
    return list(mod.run(**ctx) if s.needs_field else mod.run())


def run_all(keys=None, **ctx) -> list[tuple[Suite, list[Check]]]:
    """Run every suite, or only the named ones. Field suites need ctx."""
    out = []
    for s in SUITES:
        if keys and s.key not in keys:
            continue
        if s.needs_field and not ctx:
            continue
        out.append((s, run_suite(s, **ctx)))
    return out
