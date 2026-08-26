"""IGC file parsing.

IGC files are onboard logger recordings: complete, 1 Hz, effectively gapless.
They are ground truth. Live telemetry is a degraded version of exactly this,
which is what makes them the reference side of the degradation harness.
"""

from __future__ import annotations

import calendar
import re
from dataclasses import dataclass

B_RE = re.compile(
    rb"^B"
    rb"(\d{2})(\d{2})(\d{2})"          # HHMMSS
    rb"(\d{2})(\d{5})([NS])"           # lat  DD MM.mmm
    rb"(\d{3})(\d{5})([EW])"           # lon DDD MM.mmm
    rb"([AV])"                         # fix validity
    rb"(\d{5}|-\d{4})(\d{5}|-\d{4})"   # pressure alt, gps alt
)


@dataclass(slots=True)
class Fix:
    """One GPS position. Mirrors the Position model in the design doc.

    x/y are the task-local projected coordinates. In the live system these are
    filled once by ingestion when the position arrives; here they are filled by
    project() at load time. Either way the engine never re-projects during a
    recompute, which is what keeps recompute cheap.
    """

    t: int            # epoch seconds UTC
    lat: float
    lon: float
    alt_baro: int
    alt_gps: int
    x: float = 0.0
    y: float = 0.0


@dataclass(slots=True)
class Track:
    pilot: str
    fixes: list[Fix]
    source_files: list[str]


def _date_epoch(line: bytes) -> int | None:
    m = re.search(rb"HFDTE(?:DATE:)?(\d{2})(\d{2})(\d{2})", line)
    if not m:
        return None
    dd, mm, yy = (int(x) for x in m.groups())
    year = 2000 + yy
    return calendar.timegm((year, mm, dd, 0, 0, 0, 0, 0, 0))


def parse_igc_reference(data: bytes, filename: str = "") -> tuple[str, int, list[Fix]]:
    """The B-record grammar, written out as a regex. THIS IS THE SPECIFICATION.

    parse_igc() below is a hand-sliced version of exactly this, roughly 1.4x
    faster, and that speed is only worth having if the two cannot disagree.
    engine/invariants.check_parser_equivalence runs both over the real corpus
    and compares every field of every fix. Change this one first; the fast one
    is an optimisation of it, not an alternative to it.
    """
    pilot = ""
    day = None
    fixes: list[Fix] = []
    prev_secs = -1
    day_offset = 0

    for line in data.split(b"\n"):
        line = line.rstrip(b"\r")
        if not line:
            continue
        head = line[:5]
        if head.startswith(b"HFDTE"):
            day = _date_epoch(line) or day
            continue
        if head.startswith(b"HFPLT"):
            _, _, v = line.partition(b":")
            try:
                pilot = v.decode("utf-8").strip()
            except UnicodeDecodeError:
                pilot = v.decode("latin-1").strip()
            continue
        if line[0:1] != b"B":
            continue
        m = B_RE.match(line)
        if not m:
            continue
        hh, mi, ss, latd, latm, ns, lond, lonm, ew, valid, baro, gps = m.groups()
        if valid == b"V":                       # no GPS fix
            continue
        secs = int(hh) * 3600 + int(mi) * 60 + int(ss)
        if secs < prev_secs - 43200:            # midnight rollover
            day_offset += 86400
        prev_secs = secs
        lat = int(latd) + int(latm) / 60000.0
        if ns == b"S":
            lat = -lat
        lon = int(lond) + int(lonm) / 60000.0
        if ew == b"W":
            lon = -lon
        fixes.append(
            Fix(
                t=(day or 0) + day_offset + secs,
                lat=lat,
                lon=lon,
                alt_baro=int(baro),
                alt_gps=int(gps),
            )
        )

    if not pilot:
        pilot = filename.rsplit("/", 1)[-1].split(".")[0].replace("_", " ")
    return pilot, day or 0, fixes


def parse_igc(data: bytes, filename: str = "") -> tuple[str, int, list[Fix]]:
    """Returns (pilot_name, date_epoch, fixes). Malformed B records are skipped.

    A B record is fixed-width, so the fields are taken by offset instead of by
    regex — the regex was matching 1.4 M lines and cost more than the scoring
    did. Byte-identical to parse_igc_reference() above, which is the readable
    statement of the same grammar and is asserted equal to this on the whole
    corpus by --verify.

        B HHMMSS DDMMmmmN DDDMMmmmE A PPPPP GGGGG
        0 1....6 7.....14 15.....23 24 25..29 30..34
    """
    pilot = ""
    day = None
    fixes: list[Fix] = []
    append = fixes.append
    prev_secs = -1
    day_offset = 0

    for line in data.split(b"\n"):
        if line[:1] != b"B":
            if line[:5] == b"HFDTE":
                day = _date_epoch(line) or day
            elif line[:5] == b"HFPLT":
                _, _, v = line.partition(b":")
                try:
                    pilot = v.decode("utf-8").strip()
                except UnicodeDecodeError:
                    pilot = v.decode("latin-1").strip()
            continue
        if len(line) < 35 or line[24:25] == b"V":   # short line, or no GPS fix
            continue
        try:
            secs = int(line[1:3]) * 3600 + int(line[3:5]) * 60 + int(line[5:7])
            lat = int(line[7:9]) + int(line[9:14]) / 60000.0
            lon = int(line[15:18]) + int(line[18:23]) / 60000.0
            baro = int(line[25:30])
            gps = int(line[30:35])
        except ValueError:                          # malformed B record
            continue
        if line[14:15] == b"S":
            lat = -lat
        if line[23:24] == b"W":
            lon = -lon
        if secs < prev_secs - 43200:                # midnight rollover
            day_offset += 86400
        prev_secs = secs
        append(Fix(t=(day or 0) + day_offset + secs, lat=lat, lon=lon,
                   alt_baro=baro, alt_gps=gps))

    if not pilot:
        pilot = filename.rsplit("/", 1)[-1].split(".")[0].replace("_", " ")
    return pilot, day or 0, fixes


def _merge(entries: list[tuple[str, str, int, list[Fix]]]) -> tuple[list[Track], int]:
    """Merge per-file parses into one Track per pilot."""
    by_pilot: dict[str, Track] = {}
    day = 0
    for name, pilot, d, fixes in entries:
        if not fixes:
            continue
        day = day or d
        key = pilot.strip().lower()
        tr = by_pilot.get(key)
        if tr is None:
            by_pilot[key] = Track(pilot=pilot.strip(), fixes=list(fixes), source_files=[name])
        else:
            tr.fixes.extend(fixes)
            tr.source_files.append(name)

    tracks = []
    for tr in by_pilot.values():
        tr.fixes.sort(key=lambda f: f.t)
        deduped: list[Fix] = []
        last_t = -1
        for f in tr.fixes:
            if f.t != last_t:
                deduped.append(f)
                last_t = f.t
        tr.fixes = deduped
        tracks.append(tr)

    tracks.sort(key=lambda t: t.pilot)
    return tracks, day


def load_tracks(path: str) -> tuple[list[Track], int]:
    """Load IGC tracklogs from a .zip, a directory, or a single .igc file.

    Multiple logger sessions for one pilot are merged into a single sorted,
    deduplicated point list -- a pilot who restarted their instrument mid-flight
    produces several files but flew one flight.
    """
    import os
    import zipfile

    entries: list[tuple[str, str, int, list[Fix]]] = []

    if os.path.isdir(path):
        for root, _dirs, files in os.walk(path):
            for name in sorted(files):
                if not name.lower().endswith(".igc"):
                    continue
                full = os.path.join(root, name)
                with open(full, "rb") as fh:
                    pilot, d, fixes = parse_igc(fh.read(), name)
                entries.append((name, pilot, d, fixes))
    elif path.lower().endswith(".igc"):
        with open(path, "rb") as fh:
            pilot, d, fixes = parse_igc(fh.read(), path)
        entries.append((path, pilot, d, fixes))
    else:
        with zipfile.ZipFile(path) as z:
            for name in z.namelist():
                if not name.lower().endswith(".igc"):
                    continue
                pilot, d, fixes = parse_igc(z.read(name), name)
                entries.append((name, pilot, d, fixes))

    return _merge(entries)


# Kept for callers that specifically want a zip.
load_zip = load_tracks
