#!/usr/bin/env python3
"""File/replay adapter: files in, canonical live-scoring.v1 JSONL out."""

from __future__ import annotations

import argparse
import json
import os
import time

from engine.igc import load_tracks
from live_protocol import event, write_line


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--task", required=True, help="XCTrack .xctsk task")
    ap.add_argument("--comp", required=True, help="competition JSON")
    ap.add_argument("--igc", required=True, help="IGC file, directory, or ZIP")
    ap.add_argument("--out", default="events.jsonl", help="canonical JSONL event log")
    ap.add_argument("--competition-id", default="demo", help="stable stream ID")
    ap.add_argument("--speed", type=float, default=0.0,
                    help="replay multiplier; 0 emits immediately, 300 is 300× real time")
    ap.add_argument("--append", action="store_true", help="append instead of starting a new event log")
    args = ap.parse_args()
    if args.speed < 0:
        ap.error("--speed must be zero or positive")

    task_doc = json.load(open(args.task, encoding="utf-8"))
    comp_doc = json.load(open(args.comp, encoding="utf-8"))
    tracks, day = load_tracks(args.igc)
    if not tracks:
        ap.error(f"no IGC fixes found in {args.igc}")

    mode = "a" if args.append else "w"
    seq = 0
    if args.append and os.path.exists(args.out):
        with open(args.out, encoding="utf-8") as previous:
            for line in previous:
                if line.strip():
                    prior = json.loads(line)
                    if prior.get("competition_id") != args.competition_id:
                        ap.error("--append competition ID does not match the existing event log")
                    seq = max(seq, int(prior["sequence"]))
    with open(args.out, mode, encoding="utf-8") as out:
        def emit(kind, data, observed_at=None):
            nonlocal seq
            seq += 1
            write_line(out, event(args.competition_id, seq, kind, data, observed_at))

        emit("competition.upsert", {"config": comp_doc}, day)
        emit("task.upsert", {"name": os.path.basename(args.task).removesuffix(".xctsk"),
                               "date_epoch": day, "xctsk": task_doc}, day)
        for track in tracks:
            emit("pilot.upsert", {"pilot_id": track.pilot, "name": track.pilot}, day)

        fixes = sorted((fix.t, track.pilot, fix) for track in tracks for fix in track.fixes)
        previous = fixes[0][0]
        for timestamp, pilot, fix in fixes:
            if args.speed:
                time.sleep(max(0.0, timestamp - previous) / args.speed)
            emit("position", {"pilot_id": pilot, "timestamp": timestamp,
                              "lat": fix.lat, "lon": fix.lon,
                              "alt_gps": fix.alt_gps, "alt_baro": fix.alt_baro}, timestamp)
            previous = timestamp
        emit("competition.status", {"status": "final"}, fixes[-1][0])
    print(f"wrote {seq} canonical events to {args.out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
