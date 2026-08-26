#!/usr/bin/env python3
"""Decode an IGC file line by line and show every field.

A verification tool: it decodes each record independently of the engine and
prints the raw character slices next to the values they produce, so the parse
can be checked by eye against the IGC specification rather than trusted.

  ./decode_igc.py <file.igc> [--lines N] [--from HH:MM:SS] [--all]
"""

from __future__ import annotations

import argparse
import datetime
import sys

import leaderboard as lb
from engine.igc import parse_igc


def slices(line: str) -> dict:
    """The IGC B-record field layout, by character position."""
    return {
        "type":    (0, 1, line[0:1]),
        "time":    (1, 7, line[1:7]),
        "lat_deg": (7, 9, line[7:9]),
        "lat_min": (9, 14, line[9:14]),
        "lat_hem": (14, 15, line[14:15]),
        "lon_deg": (15, 18, line[15:18]),
        "lon_min": (18, 23, line[18:23]),
        "lon_hem": (23, 24, line[23:24]),
        "valid":   (24, 25, line[24:25]),
        "alt_baro": (25, 30, line[25:30]),
        "alt_gps":  (30, 35, line[30:35]),
    }


def main() -> int:
    ap = argparse.ArgumentParser(description="Decode an IGC file record by record")
    ap.add_argument("file")
    ap.add_argument("--lines", type=int, default=12, help="how many B records to show")
    ap.add_argument("--from", dest="start", metavar="HH:MM:SS", help="start at this fix time")
    ap.add_argument("--all", action="store_true", help="decode every record (no listing)")
    ap.add_argument("--no-color", action="store_true")
    args = ap.parse_args()
    lb.init_color(False if args.no_color else None)

    raw = open(args.file, "rb").read()
    lines = raw.decode("utf-8", "replace").splitlines()

    # --- headers -------------------------------------------------------
    print(lb.paint(f"\n  {args.file}", lb.BOLD + lb.WHITE))
    print("  " + lb.paint("─" * 96, lb.GREY))
    for ln in lines:
        if ln[:1] == "H":
            key = ln[2:5]
            print("  " + lb.paint(f"{ln[:5]:<6}", lb.BLUE) + lb.paint(ln[5:], lb.GREY))
        elif ln[:1] in ("A", "G", "L", "C", "I"):
            print("  " + lb.paint(f"{ln[:1]:<6}", lb.CYAN) + lb.paint(ln[1:60], lb.DIM + lb.GREY))

    # --- the engine's own parse, for comparison -------------------------
    pilot, day, fixes = parse_igc(raw, args.file)
    b_lines = [ln for ln in lines if ln[:1] == "B"]
    print()
    n_void = sum(1 for ln in b_lines if ln[24:25] != "A")
    print("  " + lb.paint("pilot ", lb.GREY) + lb.paint(pilot, lb.WHITE)
          + lb.paint("   date ", lb.GREY)
          + lb.paint(datetime.datetime.fromtimestamp(day, datetime.timezone.utc).strftime("%Y-%m-%d"), lb.WHITE)
          + lb.paint("   B records ", lb.GREY) + lb.paint(f"{len(b_lines):,}", lb.WHITE)
          + lb.paint("   decoded ", lb.GREY)
          + lb.paint(f"{len(fixes):,}",
                     lb.GREEN if len(fixes) == len(b_lines) - n_void else lb.RED))

    # --- field layout ---------------------------------------------------
    sample = b_lines[0]
    print()
    print("  " + lb.paint("FIELD LAYOUT", lb.BOLD + lb.WHITE)
          + lb.paint("   (IGC B record: B HHMMSS DDMMmmmN DDDMMmmmE V PPPPP GGGGG)", lb.GREY))
    print("  " + lb.paint("─" * 96, lb.GREY))
    print("  " + lb.paint(sample, lb.WHITE))
    for name, (a, b, val) in slices(sample).items():
        print("  " + lb.paint(" " * a + "^" * (b - a), lb.ORANGE)
              + lb.paint(" " * (36 - b) + f"[{a}:{b}] ", lb.GREY)
              + lb.paint(f"{name:<9}", lb.CYAN) + lb.paint(f"= {val!r}", lb.WHITE))

    # --- record-by-record -----------------------------------------------
    idx = {f.t: f for f in fixes}
    start = 0
    if args.start:
        h, m, sec = (int(x) for x in args.start.split(":"))
        want = day + h * 3600 + m * 60 + sec
        for i, ln in enumerate(b_lines):
            t = day + int(ln[1:3]) * 3600 + int(ln[3:5]) * 60 + int(ln[5:7])
            if t >= want:
                start = i
                break

    show = b_lines[start:] if args.all else b_lines[start:start + args.lines]
    print()
    print("  " + lb.paint("DECODED RECORDS", lb.BOLD + lb.WHITE))
    print("  " + lb.paint("─" * 110, lb.GREY))
    print("  " + lb.paint(f"{'#':>6} {'RAW B RECORD':<36} {'UTC':>9} "
                          f"{'LATITUDE':>12} {'LONGITUDE':>12} {'FIX':>4} {'BARO':>7} {'GPS':>7}",
                          lb.GREY + lb.BOLD))
    for i, ln in enumerate(show, start + 1):
        s = slices(ln)
        t = day + int(s["time"][2][0:2]) * 3600 + int(s["time"][2][2:4]) * 60 + int(s["time"][2][4:6])
        lat = int(s["lat_deg"][2]) + int(s["lat_min"][2]) / 60000.0
        if s["lat_hem"][2] == "S":
            lat = -lat
        lon = int(s["lon_deg"][2]) + int(s["lon_min"][2]) / 60000.0
        if s["lon_hem"][2] == "W":
            lon = -lon
        f = idx.get(t)
        # An 'A' record is a valid 3D fix; 'V' means the receiver had no fix and
        # the coordinates are meaningless (typically 0N 0E). The engine drops
        # them, so absence here is correct, not a mismatch.
        rejected = s["valid"][2] != "A"
        ok = (f is None) if rejected else (
            f is not None and abs(f.lat - lat) < 1e-9 and abs(f.lon - lon) < 1e-9)
        mark = lb.ORANGE if rejected else (lb.GREEN if ok else lb.RED)
        clock = datetime.datetime.fromtimestamp(t, datetime.timezone.utc).strftime("%H:%M:%S")
        print("  " + lb.paint(f"{i:>6} ", lb.GREY) + lb.paint(f"{ln:<36}", lb.DIM + lb.GREY)
              + lb.paint(f"{clock:>9} ", lb.WHITE)
              + lb.paint(f"{lat:>12.6f} {lon:>12.6f} ", mark)
              + lb.paint(f"{s['valid'][2]:>4} ", lb.ORANGE if rejected else lb.CYAN)
              + lb.paint(f"{int(s['alt_baro'][2]):>7} {int(s['alt_gps'][2]):>7}", lb.GREY))

    # --- whole-file consistency check -----------------------------------
    bad = 0
    void = 0
    for ln in b_lines:
        s = slices(ln)
        try:
            t = day + int(s["time"][2][0:2]) * 3600 + int(s["time"][2][2:4]) * 60 + int(s["time"][2][4:6])
            lat = int(s["lat_deg"][2]) + int(s["lat_min"][2]) / 60000.0
            lon = int(s["lon_deg"][2]) + int(s["lon_min"][2]) / 60000.0
        except ValueError:
            bad += 1
            continue
        f = idx.get(t)
        if s["valid"][2] != "A":
            void += 1
            if f is not None:
                bad += 1          # engine kept a fix it should have dropped
            continue
        if f is None or abs(f.lat - lat) > 1e-9 or abs(f.lon - lon) > 1e-9:
            bad += 1
    print()
    if bad:
        print("  " + lb.paint(f"✗ {bad} of {len(b_lines)} records disagree with the engine's parse",
                              lb.RED + lb.BOLD))
        return 1
    good = len(b_lines) - void
    print("  " + lb.paint(f"✓ all {good:,} valid B records decode identically to the engine's parse",
                          lb.GREEN + lb.BOLD))
    if void:
        print("  " + lb.paint(f"  {void} record(s) marked 'V' (no GPS fix) correctly dropped — "
                              f"their coordinates are 0N 0E", lb.ORANGE))
    lats = [f.lat for f in fixes]
    lons = [f.lon for f in fixes]
    alts = [f.alt_gps for f in fixes]
    print("  " + lb.paint(f"  bounds  lat {min(lats):.5f}..{max(lats):.5f}   "
                          f"lon {min(lons):.5f}..{max(lons):.5f}   "
                          f"gps alt {min(alts)}..{max(alts)} m", lb.GREY))
    return 0


if __name__ == "__main__":
    sys.exit(main())
