import json
import os
import sqlite3
from contextlib import closing
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


class CommandHistory:
    def __init__(self, path: Path):
        self.path = path

    def _connect(self) -> sqlite3.Connection:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        try:
            fd = os.open(self.path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
        except FileExistsError:
            pass
        else:
            os.close(fd)
        db = sqlite3.connect(self.path, timeout=1)
        try:
            db.execute("""
                CREATE TABLE IF NOT EXISTS command_history (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    timestamp TEXT NOT NULL,
                    command TEXT NOT NULL,
                    source TEXT NOT NULL,
                    points TEXT,
                    outcome TEXT NOT NULL
                )
            """)
            db.execute("""
                CREATE TABLE IF NOT EXISTS command_sequences (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    timestamp TEXT NOT NULL,
                    event_ids TEXT NOT NULL,
                    points TEXT NOT NULL
                )
            """)
        except BaseException:
            db.close()
            raise
        return db

    def start(
        self, command: str, source: dict[str, Any], points: list[tuple[float, float]] | None,
        started_at: datetime | None = None,
    ) -> int:
        with closing(self._connect()) as db, db:
            cursor = db.execute(
                "INSERT INTO command_history (timestamp, command, source, points, outcome) "
                "VALUES (?, ?, ?, ?, 'pending')",
                ((started_at or datetime.now(timezone.utc)).isoformat(), command, json.dumps(source),
                 json.dumps(points) if points is not None else None),
            )
            db.execute(
                "DELETE FROM command_history WHERE id NOT IN "
                "(SELECT id FROM command_history ORDER BY timestamp DESC, id DESC LIMIT 100)"
            )
            assert cursor.lastrowid is not None
            return cursor.lastrowid

    def finish(self, event_id: int, outcome: str) -> None:
        with closing(self._connect()) as db, db:
            db.execute("UPDATE command_history SET outcome = ? WHERE id = ?", (outcome, event_id))

    def read(self, limit: int = 100) -> list[dict[str, Any]]:
        with closing(self._connect()) as db:
            rows = db.execute(
                "SELECT id, timestamp, command, source, points, outcome "
                "FROM command_history ORDER BY timestamp DESC, id DESC LIMIT ?", (limit,)
            ).fetchall()
        return [
            {
                "id": event_id, "timestamp": timestamp, "command": command,
                "source": json.loads(source),
                "points": json.loads(points) if points is not None else None,
                "outcome": outcome,
            }
            for event_id, timestamp, command, source, points, outcome in rows
        ]

    def save_sequence(
        self, event_ids: list[int], points: list[dict[str, float]]
    ) -> dict[str, Any]:
        timestamp = datetime.now(timezone.utc).isoformat()
        with closing(self._connect()) as db, db:
            cursor = db.execute(
                "INSERT INTO command_sequences (timestamp, event_ids, points) VALUES (?, ?, ?)",
                (timestamp, json.dumps(event_ids), json.dumps(points)),
            )
            assert cursor.lastrowid is not None
            return {
                "id": cursor.lastrowid, "timestamp": timestamp,
                "event_ids": event_ids, "points": points,
            }

    @staticmethod
    def _sequence_from_row(row: tuple[Any, ...]) -> dict[str, Any]:
        sequence_id, timestamp, event_ids, points = row
        return {
            "id": sequence_id, "timestamp": timestamp,
            "event_ids": json.loads(event_ids), "points": json.loads(points),
        }

    def read_sequence(self, sequence_id: int) -> dict[str, Any] | None:
        with closing(self._connect()) as db:
            row = db.execute(
                "SELECT id, timestamp, event_ids, points FROM command_sequences WHERE id = ?",
                (sequence_id,),
            ).fetchone()
        return self._sequence_from_row(row) if row is not None else None

    def read_sequences(self) -> list[dict[str, Any]]:
        with closing(self._connect()) as db:
            rows = db.execute(
                "SELECT id, timestamp, event_ids, points FROM command_sequences ORDER BY id DESC"
            ).fetchall()
        return [self._sequence_from_row(row) for row in rows]
