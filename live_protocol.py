"""Versioned JSONL contract shared by feed gateways and scoring services."""

from __future__ import annotations

import datetime as dt
import json
import uuid

SCHEMA = "live-scoring.v1"
EVENT_TYPES = {"competition.upsert", "task.upsert", "pilot.upsert", "position",
               "competition.status"}


def iso_utc(epoch: int | float) -> str:
    return dt.datetime.fromtimestamp(epoch, dt.timezone.utc).isoformat().replace("+00:00", "Z")


def epoch_utc(value: str) -> int:
    return int(dt.datetime.fromisoformat(value.replace("Z", "+00:00")).timestamp())


def event(competition_id: str, sequence: int, kind: str, data: dict,
          observed_at: int | float | None = None, event_id: str | None = None) -> dict:
    if kind not in EVENT_TYPES:
        raise ValueError(f"unsupported event type {kind!r}")
    return {
        "schema": SCHEMA,
        "event_id": event_id or str(uuid.uuid4()),
        "competition_id": competition_id,
        "sequence": sequence,
        "observed_at": iso_utc(observed_at or 0),
        "type": kind,
        "data": data,
    }


def validate(message: dict) -> None:
    required = {"schema", "event_id", "competition_id", "sequence", "observed_at", "type", "data"}
    missing = required - message.keys()
    if missing or message.get("schema") != SCHEMA or message.get("type") not in EVENT_TYPES:
        raise ValueError(f"invalid canonical event: missing={sorted(missing)}, type={message.get('type')!r}")
    if not isinstance(message["data"], dict) or not isinstance(message["sequence"], int):
        raise ValueError("canonical event data must be an object and sequence must be an integer")


def write_line(stream, message: dict) -> None:
    validate(message)
    stream.write(json.dumps(message, separators=(",", ":"), ensure_ascii=False) + "\n")
    stream.flush()
