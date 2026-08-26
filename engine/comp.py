"""Competition configuration.

Everything in S7F that a competition can change lives here, not in the code.
Three different kinds of value get mixed together in the Sporting Code, and
keeping them apart matters:

  * Set per competition, before the first task (S7F 5): nominal distance,
    minimum distance, nominal time. Every validity number is meaningless if
    these are wrong, and none of them are in the .xctsk file.
  * Set per task (S7F 11): LeadingTimeRatio, 0-26%.
  * Fixed by the Sporting Code but discipline-dependent: nominal goal 30%,
    nominal launch 96%, arrival points, the difficulty calculation, the
    leading-coefficient formula, the ESS-but-not-goal factor.

The loader records which values actually came from the file, so the UI can
flag placeholders instead of quietly presenting invented numbers as if they
were the competition's.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field, replace

from .gap import GapParams

PARAGLIDING = "paragliding"
HANG_GLIDING = "hang-gliding"

# S7F discipline defaults. Anything here can be overridden by the config file;
# these are what the Sporting Code specifies when nothing says otherwise.
DISCIPLINE_DEFAULTS = {
    PARAGLIDING: {
        "leading_time_ratio": 0.26,      # S7F 11
        "ess_no_goal_time_factor": 0.0,  # S7F 13.2
        "arrival_points": False,         # S7F 12.4 -- never awarded
        "difficulty": False,             # S7F 12.1.1 -- not applied
        "score_back_min": 5.0,           # S7F 13.4.1
        "min_task_duration_min": 0.0,    # S7F 13.4.2
    },
    HANG_GLIDING: {
        "leading_time_ratio": 0.175,
        "ess_no_goal_time_factor": 0.8,
        "arrival_points": True,
        "difficulty": True,
        "score_back_min": 15.0,
        "min_task_duration_min": 60.0,
    },
}

# Competition parameters (S7F 5) have no Sporting Code default -- they depend
# entirely on the site. These are placeholders so the engine runs; they are
# reported as placeholders everywhere they are used.
PLACEHOLDERS = {
    "nominal_distance_km": 60.0,
    "minimum_distance_km": 5.0,
    "nominal_time_min": 90.0,
}


@dataclass(slots=True)
class Competition:
    name: str = "Unnamed competition"
    discipline: str = PARAGLIDING
    glider_class: str = ""
    pilots_present: int | None = None

    params: GapParams = None            # type: ignore[assignment]
    source: str = "<defaults>"
    from_file: set[str] = field(default_factory=set)
    tasks: dict = field(default_factory=dict)
    _declared_placeholders: list[str] = field(default_factory=lambda: list(PLACEHOLDERS))
    elevated_goal_m: float | None = None   # S7F 13.1, per task
    warnings: list = field(default_factory=list)

    def placeholders(self) -> list[str]:
        """Parameters not yet set for real, and therefore not to be trusted.

        A value can be present in the config file and still be a placeholder --
        that is the normal state before the meet director supplies the real
        numbers. The config declares them explicitly in its "placeholders"
        list; anything absent from the file entirely is also a placeholder.
        """
        declared = set(self._declared_placeholders)
        missing = {k for k in PLACEHOLDERS if k not in self.from_file}
        return sorted(declared | missing)

    def for_task(self, task_name: str) -> "Competition":
        """Apply any per-task overrides (S7F 11 sets LeadingTimeRatio per task)."""
        over = self.tasks.get(task_name)
        if not over:
            return self
        p = self.params
        c = replace(self)
        c.params = replace(
            p,
            leading_time_ratio=float(over.get("leading_time_ratio", p.leading_time_ratio)),
            nominal_time=float(over["nominal_time_min"]) * 60.0
            if "nominal_time_min" in over else p.nominal_time,
        )
        c.from_file = set(self.from_file) | {f"task:{k}" for k in over}
        c.elevated_goal_m = over.get("elevated_goal_m", self.elevated_goal_m)
        c.warnings = list(self.warnings)
        ltr = c.params.leading_time_ratio
        if ltr > 0.26:
            c.warnings.append(
                f"{task_name}: leading_time_ratio is {ltr:.2%}, above the 26% "
                f"S7F 11 gives for paragliding — confirm with the meet director")
        return c


def default_competition(discipline: str = PARAGLIDING) -> Competition:
    d = DISCIPLINE_DEFAULTS[discipline]
    return Competition(
        discipline=discipline,
        params=GapParams(
            nominal_distance=PLACEHOLDERS["nominal_distance_km"] * 1000.0,
            minimum_distance=PLACEHOLDERS["minimum_distance_km"] * 1000.0,
            nominal_time=PLACEHOLDERS["nominal_time_min"] * 60.0,
            leading_time_ratio=d["leading_time_ratio"],
            ess_no_goal_time_factor=d["ess_no_goal_time_factor"],
            arrival_points=d["arrival_points"],
            difficulty=d["difficulty"],
        ),
    )


def load(path: str) -> Competition:
    """Read a competition config. Unknown keys are an error, not a silent no-op."""
    with open(path, "rb") as fh:
        doc = json.load(fh)

    known = {
        "name", "discipline", "class", "pilots_present",
        "nominal_distance_km", "minimum_distance_km", "nominal_time_min",
        "nominal_goal", "nominal_launch", "leading_time_ratio",
        "ess_no_goal_time_factor", "score_back_min", "min_task_duration_min",
        "tasks", "placeholders", "altitude_source",
    }
    unknown = {k for k in doc if not k.startswith("_")} - known
    if unknown:
        raise ValueError(
            f"{path}: unknown key(s) {sorted(unknown)}. Known keys: {sorted(known)}"
        )

    discipline = str(doc.get("discipline", PARAGLIDING)).lower()
    if discipline not in DISCIPLINE_DEFAULTS:
        raise ValueError(
            f"{path}: discipline must be one of {sorted(DISCIPLINE_DEFAULTS)}, got {discipline!r}"
        )

    c = default_competition(discipline)
    c.source = path
    c.name = doc.get("name", c.name)
    c.glider_class = doc.get("class", "")
    c.pilots_present = doc.get("pilots_present")
    c.tasks = doc.get("tasks", {}) or {}
    c.from_file = {k for k in doc if not k.startswith("_")}
    c._declared_placeholders = list(doc.get("placeholders", []) or [])

    d = DISCIPLINE_DEFAULTS[discipline]
    p = c.params
    c.params = GapParams(
        nominal_distance=float(doc.get("nominal_distance_km",
                                       PLACEHOLDERS["nominal_distance_km"])) * 1000.0,
        minimum_distance=float(doc.get("minimum_distance_km",
                                       PLACEHOLDERS["minimum_distance_km"])) * 1000.0,
        nominal_time=float(doc.get("nominal_time_min",
                                   PLACEHOLDERS["nominal_time_min"])) * 60.0,
        nominal_goal=float(doc.get("nominal_goal", p.nominal_goal)),
        nominal_launch=float(doc.get("nominal_launch", p.nominal_launch)),
        leading_time_ratio=float(doc.get("leading_time_ratio", d["leading_time_ratio"])),
        ess_no_goal_time_factor=float(doc.get("ess_no_goal_time_factor",
                                              d["ess_no_goal_time_factor"])),
        arrival_points=d["arrival_points"],
        difficulty=d["difficulty"],
        altitude_gps=str(doc.get("altitude_source", "gps")).lower() != "baro",
    )

    if str(doc.get("altitude_source", "gps")).lower() not in ("gps", "baro"):
        raise ValueError(f"{path}: altitude_source must be 'gps' or 'baro'")
    ltr = c.params.leading_time_ratio
    # S7F 11 puts the paragliding value at 26%. Published results do not always
    # agree: Bassano 2026 T2.3's own points allocation works out at 26.32%
    # (leading pot 168.0 of a 638.3 non-distance share), which a hard 0.26 cap
    # would have rejected outright. So the cap is above the rulebook value and
    # anything past it warns rather than refusing to score.
    if not 0.0 <= ltr <= 0.30:
        raise ValueError(f"{path}: leading_time_ratio must be 0..0.30, got {ltr}")
    if c.pilots_present is None:
        c.warnings.append(
            "pilots_present is not set, so launch validity (S7F 10.1) will be "
            "1.0 by construction. The Code counts everyone not marked ABS — "
            "those who took off PLUS those present who did not fly (DNF) — and "
            "no tracklog records that. Until it is set, the safety feature "
            "that devalues a task most of the field refused to fly is off.")
    if ltr > 0.26:
        c.warnings.append(
            f"leading_time_ratio is {ltr:.2%}, above the 26% S7F 11 gives for "
            f"paragliding — confirm it with the meet director")
    if c.params.nominal_distance <= c.params.minimum_distance:
        raise ValueError(f"{path}: nominal_distance_km must exceed minimum_distance_km")
    return c
