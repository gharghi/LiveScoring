#!/usr/bin/env python3
"""Continuous canonical-event consumer that publishes live-score.v1 snapshots."""

from __future__ import annotations

import argparse
import json
import os
import tempfile
import time

from engine import comp as compcfg
from engine.igc import Fix, Track
from engine.score import project, score_pilot
from engine.scoring import score_task
from engine.task import parse_xctsk
from live_protocol import iso_utc, validate


class Service:
    def __init__(self, snapshot_dir: str, publish_every: int = 25) -> None:
        if publish_every < 1:
            raise ValueError("publish_every must be positive")
        self.snapshot_dir = snapshot_dir
        self.publish_every = publish_every
        self.tmp = tempfile.TemporaryDirectory(prefix="live-scoring-")
        self.task = self.competition = self.params = None
        self.present = None
        self.tracks: dict[str, Track] = {}
        self.seen: set[str] = set()
        self.sequence = 0
        self.competition_id = ""
        self.now = 0
        self.status = "open"

    def apply(self, message: dict) -> None:
        validate(message)
        if message["event_id"] in self.seen:
            return
        if self.competition_id and message["competition_id"] != self.competition_id:
            raise ValueError("one scoring-service process consumes one competition stream")
        if message["sequence"] <= self.sequence:
            raise ValueError(f"out-of-order sequence {message['sequence']} after {self.sequence}")
        self.seen.add(message["event_id"])
        self.sequence, self.competition_id = message["sequence"], message["competition_id"]
        data, kind = message["data"], message["type"]
        if kind == "competition.upsert":
            path = os.path.join(self.tmp.name, "competition.json")
            with open(path, "w", encoding="utf-8") as f:
                json.dump(data["config"], f)
            self.competition = compcfg.load(path)
            self.params = self.competition.params
            self.present = self.competition.pilots_present
        elif kind == "task.upsert":
            path = os.path.join(self.tmp.name, "task.xctsk")
            with open(path, "w", encoding="utf-8") as f:
                json.dump(data["xctsk"], f)
            self.task = parse_xctsk(path, int(data["date_epoch"]))
            self.task.name = data.get("name", self.task.name)
            if self.competition:
                self.competition = self.competition.for_task(self.task.name)
                self.params, self.present = self.competition.params, self.competition.pilots_present
        elif kind == "pilot.upsert":
            pilot = data["pilot_id"]
            self.tracks.setdefault(pilot, Track(pilot=data.get("name", pilot), fixes=[], source_files=["canonical"]))
        elif kind == "position":
            pilot = data["pilot_id"]
            track = self.tracks.get(pilot)
            if track is None:
                raise ValueError(f"position for unknown pilot {pilot!r}")
            if self.task is None or self.params is None:
                raise ValueError("position arrived before task and competition setup")
            fix = Fix(int(data["timestamp"]), float(data["lat"]), float(data["lon"]),
                      int(data.get("alt_baro", 0)), int(data["alt_gps"]))
            project(self.task, [fix])
            if track.fixes and fix.t <= track.fixes[-1].t:
                raise ValueError(f"non-increasing timestamp for {pilot!r}")
            track.fixes.append(fix)
            self.now = max(self.now, fix.t)
        elif kind == "competition.status":
            self.status = data["status"]
        # Position ingestion is cheap; publishing requires a field-wide GAP
        # recompute. Batch it to keep a burst of telemetry from producing one
        # snapshot per fix. Final status always forces the last snapshot.
        if kind == "competition.status" or self.sequence % self.publish_every == 0:
            self.publish()

    def publish(self) -> dict | None:
        if self.task is None or self.params is None:
            return None
        results = []
        for track in self.tracks.values():
            result = score_pilot(self.task, track.fixes, self.now, self.params)
            result.pilot = track.pilot
            results.append(result)
        task_score = score_task(self.task, results, self.params, self.present)
        ranked = sorted(results, key=lambda r: r.rank_key)
        payload = {
            "schema": "live-score.v1", "competition_id": self.competition_id,
            "source_sequence": self.sequence, "task_version": self.task.task_hash,
            "calculated_at": iso_utc(self.now), "provisional": self.status != "final",
            "status": self.status,
            "task": {"name": self.task.name, "total_distance_m": self.task.total_distance,
                     "speed_distance_m": self.task.speed_distance},
            "validity": {"launch": task_score.launch_validity, "distance": task_score.distance_validity,
                         "time": task_score.time_validity, "task": task_score.task_validity},
            "results": [{"rank": n, "pilot": r.pilot, "state": r.state,
                         "distance_m": r.distance, "start": r.start_time, "ess": r.ess_time,
                         "goal": r.goal_time, "total_points": r.total_points}
                        for n, r in enumerate(ranked, 1)],
        }
        os.makedirs(self.snapshot_dir, exist_ok=True)
        version = os.path.join(self.snapshot_dir, f"{self.sequence:012d}.json")
        with open(version, "w", encoding="utf-8") as f:
            json.dump(payload, f, indent=2)
        with open(os.path.join(self.snapshot_dir, "latest.json"), "w", encoding="utf-8") as f:
            json.dump(payload, f, indent=2)
        return payload


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--events", default="events.jsonl")
    ap.add_argument("--snapshots", default="snapshots")
    ap.add_argument("--watch", action="store_true", help="tail the event file forever")
    ap.add_argument("--poll", type=float, default=0.25, help="seconds between tail polls")
    ap.add_argument("--publish-every", type=int, default=25,
                    help="publish after this many input events (default: 25)")
    args = ap.parse_args()
    service = Service(args.snapshots, args.publish_every)
    with open(args.events, encoding="utf-8") as stream:
        while True:
            line = stream.readline()
            if line:
                service.apply(json.loads(line))
                continue
            if not args.watch:
                break
            time.sleep(args.poll)
    # Offline input may end between publish boundaries; persist its final state.
    service.publish()
    print(f"published sequence {service.sequence} to {args.snapshots}/latest.json")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
