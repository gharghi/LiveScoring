"""FastAPI delivery service for live-score.v1 snapshots.

Run with: uvicorn api_app:app --host 0.0.0.0 --port 8000
"""

from __future__ import annotations

import asyncio
import json
import os
import sqlite3
import time
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Optional

from fastapi import FastAPI, HTTPException, Query

from scoring_service import Service

EVENTS_PATH = Path(os.getenv("LIVE_SCORING_EVENTS", "events.jsonl"))
DATABASE_PATH = Path(os.getenv("LIVE_SCORING_DB", "live-scoring.sqlite3"))
PUBLISH_SECONDS = float(os.getenv("LIVE_SCORING_PUBLISH_SECONDS", "1"))


class SnapshotDatabase:
    def __init__(self, path: Path) -> None:
        self.conn = sqlite3.connect(path, check_same_thread=False)
        self.conn.row_factory = sqlite3.Row
        self.conn.execute("""
            CREATE TABLE IF NOT EXISTS score_snapshots (
                id INTEGER PRIMARY KEY,
                competition_id TEXT NOT NULL,
                source_sequence INTEGER NOT NULL,
                calculated_at TEXT NOT NULL,
                persisted_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                provisional INTEGER NOT NULL,
                payload TEXT NOT NULL,
                UNIQUE(competition_id, source_sequence)
            )
        """)
        self.conn.execute("CREATE INDEX IF NOT EXISTS snapshots_latest ON score_snapshots (competition_id, id DESC)")
        self.conn.commit()

    def save(self, payload: dict) -> None:
        self.conn.execute("""
            INSERT INTO score_snapshots
                (competition_id, source_sequence, calculated_at, provisional, payload)
            VALUES (?, ?, ?, ?, ?)
            ON CONFLICT(competition_id, source_sequence) DO UPDATE SET
                calculated_at=excluded.calculated_at,
                provisional=excluded.provisional,
                payload=excluded.payload,
                persisted_at=CURRENT_TIMESTAMP
        """, (payload["competition_id"], payload["source_sequence"],
              payload["calculated_at"], int(payload["provisional"]),
              json.dumps(payload, separators=(",", ":"), ensure_ascii=False)))
        self.conn.commit()

    def latest(self, competition_id: Optional[str] = None) -> Optional[dict]:
        query = "SELECT payload FROM score_snapshots"
        args: tuple = ()
        if competition_id:
            query += " WHERE competition_id = ?"
            args = (competition_id,)
        row = self.conn.execute(query + " ORDER BY id DESC LIMIT 1", args).fetchone()
        return json.loads(row["payload"]) if row else None

    def history(self, competition_id: Optional[str], limit: int) -> list[dict]:
        query = "SELECT payload FROM score_snapshots"
        args: tuple = ()
        if competition_id:
            query += " WHERE competition_id = ?"
            args = (competition_id,)
        rows = self.conn.execute(query + " ORDER BY id DESC LIMIT ?", args + (limit,)).fetchall()
        return [json.loads(row["payload"]) for row in rows]


class LiveWorker:
    def __init__(self, database: SnapshotDatabase) -> None:
        self.database = database
        self.service = Service("/tmp/live-scoring-api", publish_every=10**9)
        self.running = True

    async def run(self) -> None:
        position = 0
        last_publish = 0.0
        while self.running:
            if EVENTS_PATH.exists():
                with EVENTS_PATH.open(encoding="utf-8") as events:
                    events.seek(position)
                    for line in events:
                        if line.strip():
                            self.service.apply(json.loads(line))
                    position = events.tell()
            now = time.monotonic()
            if now - last_publish >= PUBLISH_SECONDS:
                snapshot = self.service.publish()
                if snapshot:
                    self.database.save(snapshot)
                last_publish = now
            await asyncio.sleep(0.1)


database = SnapshotDatabase(DATABASE_PATH)
worker = LiveWorker(database)


@asynccontextmanager
async def lifespan(_app: FastAPI):
    task = asyncio.create_task(worker.run())
    yield
    worker.running = False
    await task


app = FastAPI(
    title="Live Scoring API",
    version="1.0.0",
    description=("Reads canonical `live-scoring.v1` events, persists one `live-score.v1` "
                 "result per second in SQLite, and serves frontend-ready snapshots."),
    lifespan=lifespan,
)


@app.get("/health", tags=["operations"])
def health() -> dict:
    return {"ok": True, "events_path": str(EVENTS_PATH), "database_path": str(DATABASE_PATH)}


@app.get("/api/v1/results/latest", tags=["results"])
def latest_result(competition_id: Optional[str] = None) -> dict:
    snapshot = database.latest(competition_id)
    if snapshot is None:
        raise HTTPException(404, "No score snapshot has been persisted yet")
    return snapshot


@app.get("/api/v1/results/history", tags=["results"])
def result_history(competition_id: Optional[str] = None,
                   limit: int = Query(60, ge=1, le=3600)) -> dict:
    return {"items": database.history(competition_id, limit)}


@app.get("/api/v1/results/latest/pilots", tags=["results"])
def latest_pilots(competition_id: Optional[str] = None) -> dict:
    snapshot = database.latest(competition_id)
    if snapshot is None:
        raise HTTPException(404, "No score snapshot has been persisted yet")
    return {"competition_id": snapshot["competition_id"], "source_sequence": snapshot["source_sequence"],
            "provisional": snapshot["provisional"], "results": snapshot["results"]}
