"""Score the whole field across every core, for the cold-start case.

The live engine never needs this. Positions arrive one at a time, the per-fix
cost is 0.5 µs, and 150 pilots at 1 Hz is 0.01% of one core — a single thread
is already three orders of magnitude clear of the requirement.

The case that *is* slow is the cold one: 129 tracklogs, 1.4 million fixes,
nothing parsed yet. That happens on a scorer's laptop the moment the last
tracklog is uploaded, and after a crash, a config change, or a task correction
— exactly the moments when someone is waiting for the number. Serially it is
about 3 s. Pilots are embarrassingly parallel (a pilot's state depends only on
their own points), so it is 8× narrower than that on any modern machine.

Two properties keep this from becoming a second scoring implementation:

  * The worker calls the same `score_pilot` and the same `score_task` as the
    serial path. Nothing about the rules lives here — this file only decides
    *where* the loop runs.
  * `engine/invariants.check_parallel_matches_serial` asserts the two produce
    identical results on the real field, so a divergence is a test failure
    rather than a protest.

`fork` is used deliberately. The compiled task, the parameters and the imported
engine are inherited rather than pickled and re-derived, which is what keeps
worker startup at a few milliseconds instead of the ~100 ms `spawn` costs per
process. This module does no I/O, holds no locks and starts no threads before
forking, which is what makes fork safe here.
"""

from __future__ import annotations

import os

from .igc import Track, parse_igc
from .score import project, score_pilot

# Set in the parent before forking; inherited by every worker.
_TASK = None
_PARAMS = None
_NOW = 0.0


def _worker(group: tuple[str, list[str]]):
    """Parse, project and score one pilot. Returns (result, source_files, n_fixes).

    The pilot's fixes stay in the worker. Sending 1.4 M Fix objects back across
    a pipe costs more than the scoring saved, and nothing in the task-level
    pass needs them.
    """
    pilot_key, paths = group
    fixes = []
    pilot = ""
    for path in paths:
        with open(path, "rb") as fh:
            name, _day, fx = parse_igc(fh.read(), os.path.basename(path))
        pilot = pilot or name
        fixes.extend(fx)

    if len(paths) > 1:
        # Same merge rule as igc._merge: one pilot flew one flight, whatever
        # their instrument did.
        fixes.sort(key=lambda f: f.t)
        deduped = []
        last = -1
        for f in fixes:
            if f.t != last:
                deduped.append(f)
                last = f.t
        fixes = deduped

    project(_TASK, fixes)
    r = score_pilot(_TASK, fixes, _NOW, _PARAMS)
    r.pilot = pilot.strip()

    # Reduce the leading-coefficient samples to the two numbers score_task
    # needs before crossing the process boundary. A finisher accumulates around
    # 5,000 of them; pickling 129 pilots' worth cost more than the parallelism
    # saved. The remaining field-wide half (missingArea, which needs maxTime)
    # is done in the parent exactly as it is on the serial path.
    from .gap import leading_partial, leading_partial_hump_v2a
    if getattr(_TASK, "progress_curve", "WEIGHTED").upper() == "HUMP_V2A":
        r.lead_area, r.lead_min_to_ess = leading_partial_hump_v2a(
            r.lead_samples, _TASK.speed_distance / 1000.0)
    else:
        r.lead_area, r.lead_min_to_ess = leading_partial(
            r.lead_samples, _TASK.speed_distance / 1000.0)
    r.lead_samples = []

    # How far this tracklog is from the task, for the outlier filter in run.py.
    # Computed here because it needs the fixes, which do not leave the worker.
    from math import hypot
    far = min((hypot(f.x, f.y) for f in fixes), default=0.0)

    return r, [os.path.basename(p) for p in paths], len(fixes), far


def scan_headers(paths: list[str], probe: int = 8192):
    """Pilot name and task date from each file's header, without parsing fixes.

    Needed before forking, for two reasons: the task cannot be compiled until
    the date is known, and files have to be grouped by pilot so that a pilot
    who restarted their instrument is scored as one flight rather than two.
    Reads the first few KB of each file, not the whole thing.
    """
    from .igc import _date_epoch

    day = 0
    groups: dict[str, list[str]] = {}
    for path in paths:
        pilot = ""
        with open(path, "rb") as fh:
            head = fh.read(probe)
        for line in head.split(b"\n"):
            if line[:5] == b"HFDTE":
                day = day or (_date_epoch(line) or 0)
            elif line[:5] == b"HFPLT":
                _, _, v = line.partition(b":")
                try:
                    pilot = v.decode("utf-8").strip()
                except UnicodeDecodeError:
                    pilot = v.decode("latin-1").strip()
            elif line[:1] == b"B":
                break
        if not pilot:
            pilot = os.path.basename(path).split(".")[0].replace("_", " ")
        groups.setdefault(pilot.strip().lower(), []).append(path)
    return groups, day


def usable(path: str) -> bool:
    """The parallel path handles a directory of .igc files and nothing else.

    A zip has to be decompressed in one place anyway, and --live re-scores the
    same points hundreds of times so it wants them resident. Both fall back to
    the serial loader, which is the same code either way.
    """
    return os.path.isdir(path)


def igc_paths(directory: str) -> list[str]:
    out = []
    for root, _dirs, files in os.walk(directory):
        for name in sorted(files):
            if name.lower().endswith(".igc"):
                out.append(os.path.join(root, name))
    return out


def score_field(task, params, now: float, groups: dict[str, list[str]],
                workers: int | None = None):
    """Score every pilot across `workers` processes.

    Returns (results, tracks, distance_from_task) -- the last for run.py's
    outlier filter, which needs the fixes and so cannot run in the parent.

    `tracks` carry the pilot name, source files and fix count but NOT the
    fixes: nothing downstream of scoring needs them, and --explain re-reads the
    one tracklog it is asked about.
    """
    global _TASK, _PARAMS, _NOW
    _TASK, _PARAMS, _NOW = task, params, now

    items = sorted(groups.items())
    if workers is None:
        workers = min(len(items), os.cpu_count() or 1)

    if workers <= 1 or len(items) <= 1:
        out = [_worker(it) for it in items]
    else:
        import multiprocessing as mp
        try:
            ctx = mp.get_context("fork")
        except ValueError:                    # platform without fork
            ctx = mp.get_context("spawn")
        # Big chunks: each task is ~25 ms, so per-item IPC would dominate.
        chunk = max(1, len(items) // (workers * 4))
        with ctx.Pool(workers) as pool:
            out = pool.map(_worker, items, chunksize=chunk)

    results = []
    tracks = []
    dists = []
    for r, files, nfix, far in out:
        results.append(r)
        tracks.append(Track(pilot=r.pilot, fixes=[], source_files=files))
        r.fixes_used = r.fixes_used or nfix
        dists.append(far)
    order = sorted(range(len(results)), key=lambda i: results[i].pilot)
    return ([results[i] for i in order], [tracks[i] for i in order],
            [dists[i] for i in order])
