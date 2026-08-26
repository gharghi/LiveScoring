"""Terminal rendering for the live leaderboard.

Deliberately outside engine/ -- the engine has no idea this exists.

The display carries the three ways a live result is provisional (DESIGN.md 17):
incomplete data (fix age), provisional validity, and the approximated route.
A rank correction after backfill is the system working, so it is shown as an
event, not hidden.
"""

from __future__ import annotations

import datetime
import os
import shutil

from engine.score import PilotResult
from engine.task import CompiledTask

RESET = "\033[0m"
BOLD = "\033[1m"
DIM = "\033[2m"


def _c(n: int) -> str:
    return f"\033[38;5;{n}m"


GREEN, CYAN, YELLOW, RED, GREY, BLUE, WHITE, ORANGE = (
    _c(78), _c(80), _c(179), _c(167), _c(243), _c(69), _c(252), _c(215),
)

_USE_COLOR = True


def init_color(force: bool | None = None) -> None:
    global _USE_COLOR
    if force is not None:
        _USE_COLOR = force
    else:
        _USE_COLOR = os.isatty(1) and os.environ.get("TERM") != "dumb"


def paint(s: str, *codes: str) -> str:
    if not _USE_COLOR or not codes:
        return s
    return "".join(codes) + s + RESET


def _vis(s: str) -> int:
    """Visible width, ignoring ANSI escapes."""
    out, i = 0, 0
    while i < len(s):
        if s[i] == "\033":
            while i < len(s) and s[i] != "m":
                i += 1
            i += 1
        else:
            out += 1
            i += 1
    return out


def pad(s: str, w: int, right: bool = False) -> str:
    d = w - _vis(s)
    if d <= 0:
        return s
    return (" " * d + s) if right else (s + " " * d)


def hhmmss(t: float | None) -> str:
    if t is None:
        return "–"
    return datetime.datetime.fromtimestamp(t, datetime.timezone.utc).strftime("%H:%M:%S")


def hhmm(t: float | None) -> str:
    if t is None:
        return "–"
    return datetime.datetime.fromtimestamp(t, datetime.timezone.utc).strftime("%H:%M")


def dur(sec: float) -> str:
    sec = int(max(0, sec))
    return f"{sec // 3600}:{sec % 3600 // 60:02d}:{sec % 60:02d}"


def _status(r: PilotResult, now: float, task: CompiledTask,
            final: bool = False) -> tuple[str, str]:
    """(label, colour) for a pilot's current condition.

    A pilot who launched but never made a valid start is NOT the same as one
    who started and landed out, even though both end up stationary on the
    ground with a stale fix. Collapsing them into "LANDED" hides the single
    most important fact about a task: whether anybody started at all.
    """
    age = now - r.last_t
    if r.state == "GOAL":
        return "GOAL", GREEN + BOLD
    if r.state == "ESS":
        return "ESS", CYAN
    if r.goal_missed_deadline:
        return "DEADLINE", RED

    if r.did_not_start:
        if r.early_start:
            return "EARLY", RED
        if now < task.first_gate:
            return "WAITING", DIM + GREY
        if final or age > 900:
            return "NO START", RED          # launched, never started
        return "PRE-START", ORANGE          # airborne, gate open, not yet out
    if not r.launched:
        return "WAITING", DIM + GREY

    if age > 900:
        return "LANDED", GREY
    if age > 120:
        return "NO SIG", ORANGE
    return f"TP{r.tp_count}", WHITE


def _bar(r: PilotResult, task: CompiledTask) -> str:
    """One glyph per scored waypoint: start, turnpoints, ESS, goal."""
    out = []
    for i in range(task.start_index, task.goal_index + 1):
        hit = r.tags[i] is not None
        if i == task.goal_index:
            g, col = ("▣" if hit else "▢"), (GREEN if hit else GREY)
        elif i == task.ess_index:
            g, col = ("◆" if hit else "◇"), (CYAN if hit else GREY)
        elif i == task.start_index:
            g, col = ("▸" if hit else "▹"), (BLUE if hit else GREY)
        else:
            g, col = ("●" if hit else "○"), (WHITE if hit else GREY)
        out.append(paint(g, col))
    return "".join(out)


def header(task: CompiledTask, comp: str, day: int, reconstructed: bool = False) -> str:
    w = min(shutil.get_terminal_size((110, 30)).columns, 132)
    date = datetime.datetime.fromtimestamp(day, datetime.timezone.utc).strftime("%Y-%m-%d")
    route = " › ".join(wp.name for wp in task.waypoints)
    gates = ", ".join(hhmmss(g) for g in task.gates)

    lines = [
        paint("╭" + "─" * (w - 2) + "╮", GREY),
    ]

    def row(s: str) -> str:
        return paint("│ ", GREY) + pad(s, w - 4) + paint(" │", GREY)

    title = paint(task.name, BOLD + WHITE) + paint(f"   {comp}   {date}", GREY)
    lines.append(row(title))
    if reconstructed:
        lines.append(row(paint("⚠ RECONSTRUCTED TASK — inferred from tracklogs, NOT the official task", RED + BOLD)))
    lines.append(row(paint(route, GREY)))
    lines.append(
        row(
            paint(f"{task.start_type} · gate {gates}", BLUE)
            + paint(f"  ({task.start_direction} advisory, not scored)", DIM + GREY)
            + paint("   deadline ", GREY)
            + paint(hhmmss(task.goal_deadline), BLUE)
        )
    )
    lines.append(
        row(
            paint(f"{len(task.waypoints)} turnpoints", GREY)
            + paint(f"   optimised {task.total_distance/1000:.2f} km", BOLD + WHITE)
            + paint(f"   speed section {task.speed_distance/1000:.2f} km", GREY)
            + paint(f"   (centres {task.centre_distance/1000:.2f} km)", DIM + GREY)
        )
    )
    lines.append(paint("╰" + "─" * (w - 2) + "╯", GREY))
    return "\n".join(lines)


def clockline(task: CompiledTask, results: list[PilotResult], now: float,
              final: bool = False) -> str:
    # Mutually exclusive buckets -- they must sum to the field, or the header
    # lies about how many pilots are in the air.
    n_goal = n_ess = n_fly = n_lost = n_air = n_down = n_dns = 0
    for r in results:
        age = now - r.last_t
        if r.state == "GOAL":
            n_goal += 1
        elif r.state == "ESS":
            n_ess += 1
        elif r.did_not_start:
            n_dns += 1
        elif age > 900:
            n_down += 1
        elif age > 120:
            n_lost += 1
        else:
            n_fly += 1
    n_started = sum(1 for r in results if r.start_time is not None)
    n_launched = sum(1 for r in results if r.launched)

    t_rel = now - task.first_gate
    rel = ("T+" + dur(t_rel)) if t_rel >= 0 else ("T−" + dur(-t_rel))

    started_col = GREEN if n_started else RED
    parts = [
        paint("CLOCK ", GREY) + paint(hhmmss(now) + "Z", BOLD + WHITE),
        paint(rel, BLUE),
        paint(f"launched {n_launched}", GREY),
        paint(f"STARTED {n_started}", started_col + BOLD),
        paint(f"no-start {n_dns}", RED) if n_dns else "",
        paint(f"goal {n_goal}", GREEN),
        paint(f"ess {n_ess}", CYAN),
        paint(f"flying {n_fly}", WHITE),
        paint(f"no-sig {n_lost}", ORANGE) if n_lost else "",
        paint(f"landed {n_down}", GREY) if n_down else "",
    ]
    line = "   ".join(p for p in parts if p)
    tag = (paint("TASK COMPLETE", GREEN + BOLD) if final
           else paint("PROVISIONAL", YELLOW + BOLD))
    return "  " + line + "   " + tag


COLS = [
    ("#", 4, True),
    ("PILOT", 24, False),
    ("STATUS", 9, False),
    ("PROGRESS", 10, False),
    ("START", 9, True),
    ("DIST", 9, True),
    ("SPEED", 7, True),
    ("ESS", 9, True),
    ("DIST", 7, True),
    ("TIME", 7, True),
    ("LEAD", 7, True),
    ("TOTAL", 8, True),
    ("AGE", 5, True),
]


def gapline(ts, competition, final: bool) -> str:
    """Task validity and points allocation.

    Placeholder competition parameters are called out explicitly: every
    validity number below is a function of them, so presenting them silently
    would make invented inputs look like the competition's own.
    """
    params = competition.params
    a = ts.alloc
    ph = set(competition.placeholders())

    def val(key, text):
        return paint(text, YELLOW) if key in ph else paint(text, GREY)

    banner = (paint(" FINAL ", BOLD + "\033[48;5;22m" + WHITE) if final
              else paint(" SIMULATION ", BOLD + "\033[48;5;94m" + WHITE))

    lines = [
        "  " + paint(f"GAP 2026 · {competition.discipline.upper()}", BOLD + WHITE)
        + "  " + banner
        + val("nominal_distance_km", f"   nom dist {params.nominal_distance/1000:.0f} km")
        + val("minimum_distance_km", f"   min dist {params.minimum_distance/1000:.0f} km")
        + val("nominal_time_min", f"   nom time {params.nominal_time/60:.0f} min")
        + paint(f"   LTR {params.leading_time_ratio*100:.1f}%", GREY),
        "  " + paint("VALIDITY", GREY)
        + paint(f"  launch {ts.launch_validity:.3f}", WHITE)
        + paint(f"  distance {ts.distance_validity:.3f}", WHITE)
        + paint(f"  time {ts.time_validity:.3f}", WHITE)
        + paint(f"  →  task {ts.task_validity:.3f}", BOLD + CYAN)
        + paint(f"   ({ts.pilots_goal}/{ts.pilots_flying} in goal)", GREY),
        "  " + paint("AVAILABLE", GREY)
        + paint(f"  distance {a.available_distance:.0f}", BLUE)
        + paint(f"  time {a.available_time:.0f}", GREEN)
        + paint(f"  leading {a.available_leading:.0f}", ORANGE)
        + paint(f"  →  total {a.available_total:.0f}", BOLD + WHITE)
        + paint("   (arrival: none in paragliding)", DIM + GREY)
        if not params.arrival_points else
        "  " + paint("AVAILABLE", GREY)
        + paint(f"  distance {a.available_distance:.0f}", BLUE)
        + paint(f"  time {a.available_time:.0f}", GREEN)
        + paint(f"  leading {a.available_leading:.0f}", ORANGE)
        + paint(f"  arrival {a.available_arrival:.0f}", CYAN)
        + paint(f"  →  total {a.available_total:.0f}", BOLD + WHITE),
    ]
    if ph:
        lines.append("  " + paint("⚠ PLACEHOLDER", YELLOW + BOLD)
                     + paint(f"  {', '.join(sorted(ph))} not set for this competition"
                             f" — every validity above depends on them", YELLOW))
    lines.append("  " + paint(f"config: {competition.source}", DIM + GREY))
    return "\n".join(lines)


def table(task: CompiledTask, results: list[PilotResult], now: float, top: int = 0,
          final: bool = False) -> str:
    rows = sorted(results, key=lambda r: (r.rank_key if any(x.total_points for x in results)
                                          else r.progress_key))
    if top:
        rows = rows[:top]


    head = "  " + " ".join(pad(paint(n, GREY + BOLD), w, right) for n, w, right in COLS)
    sep = "  " + paint("─" * (sum(w for _, w, _ in COLS) + len(COLS) - 1), GREY)
    out = [head, sep]

    for i, r in enumerate(rows, 1):
        label, col = _status(r, now, task, final)
        scored = r.total_points > 0 or r.distance > 0
        age = now - r.last_t
        dash = paint("–", GREY)

        rank = paint(str(i), BOLD + WHITE) if scored else dash
        name = paint(r.pilot[:24], WHITE if scored else GREY)
        if r.early_start:
            name = paint(r.pilot[:22] + " ⚑", RED)
        dist = paint(f"{r.distance/1000:.2f}", WHITE if scored else GREY) if scored else dash
        spd = paint(f"{r.speed:.1f}", CYAN) if r.speed else dash
        dp = paint(f"{r.distance_points:.1f}", BLUE) if r.distance_points else dash
        tp = paint(f"{r.time_points:.1f}", GREEN) if r.time_points else dash
        lp = paint(f"{r.leading_points:.1f}", ORANGE) if r.leading_points else dash
        tot = paint(f"{r.total_points:.1f}", BOLD + WHITE) if r.total_points else dash
        ages = (paint(f"{int(age)//60}m", ORANGE if age > 120 else GREY)
                if r.last_t and age < 100000 else dash)

        start_cell = (paint(hhmmss(r.start_time), BLUE) if r.start_time
                      else paint("no start", RED))
        cells = [
            rank, name, paint(label, col), _bar(r, task), start_cell, dist, spd,
            paint(hhmmss(r.ess_time), CYAN) if r.ess_time else dash,
            dp, tp, lp, tot, ages,
        ]
        out.append("  " + " ".join(pad(c, w, right) for c, (_, w, right) in zip(cells, COLS)))

    return "\n".join(out)


def legend() -> str:
    bits = [
        paint("▸", BLUE) + paint(" start", GREY),
        paint("●", WHITE) + paint(" turnpoint", GREY),
        paint("◆", CYAN) + paint(" ESS", GREY),
        paint("▣", GREEN) + paint(" goal", GREY),
        paint("⚑", RED) + paint(" early start (PG: launch–SSS only)", GREY),
        paint("DIST/TIME/LEAD", GREY) + paint(" = GAP points", DIM + GREY),
    ]
    return "  " + paint("   ".join(bits), GREY)
