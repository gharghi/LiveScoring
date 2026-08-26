"""S7F 5 — Competition parameters.  [step 1 of the pipeline]

Not a formula, but the input every task-validity formula is a function of. The
meet director sets these before the first task; they are not in the .xctsk and
they are not in the code. They live in competition.json.

  5.1  NominalDistance   a distance most pilots should be able to fly
  5.2  MinimumDistance   what a pilot scores for launching and flying at all
  5.3  NominalTime       how long the task should take the fastest pilot

Get these wrong and every validity number is wrong, silently and plausibly, so
the engine tracks which of them are still shipped placeholders and warns by
name on every run until the director replaces them.

The remaining fields are fixed by S7F but exposed anyway, because a rule change
should be a config edit and not a code change:

  10.1  NominalLaunch          0.96, fixed
  10.2  NominalGoal            0.30, fixed
  11    LeadingTimeRatio       26% for paragliding, 17.5% for hang-gliding,
                               and set PER TASK
  13.2  ESS-but-not-goal       0% for paragliding, 80% for hang-gliding
  12.4  ArrivalPoints          never, for paragliding
  12.1.1 Difficulty            never, for paragliding
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class GapParams:
    nominal_distance: float          # m   (S7F 5.1)
    minimum_distance: float          # m   (S7F 5.2)
    nominal_time: float              # s   (S7F 5.3)

    nominal_goal: float = 0.30       # S7F 10.2, fixed at 30%
    nominal_launch: float = 0.96     # S7F 10.1, fixed at 96%
    leading_time_ratio: float = 0.26         # [PG] S7F 11 (HG: 0.175)
    ess_no_goal_time_factor: float = 0.0     # [PG] S7F 13.2 (HG: 0.8)
    arrival_points: bool = False             # [PG] S7F 12.4 — never awarded
    difficulty: bool = False                 # [PG] S7F 12.1.1 — not applied
    altitude_gps: bool = True                # GPS altitude for goal crossings
