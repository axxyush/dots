from __future__ import annotations

import json
import sqlite3
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any


@dataclass(frozen=True)
class MapRecord:
    map_id: str
    created_at: float
    layout_2d: dict[str, Any]
    metadata: dict[str, Any]
    tactile_pdf_url: str | None


class MapStore:
    def __init__(self, db_path: Path):
        self.db_path = db_path
        self._init()

    def _connect(self) -> sqlite3.Connection:
        con = sqlite3.connect(str(self.db_path))
        con.row_factory = sqlite3.Row
        return con

    def _init(self) -> None:
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        with self._connect() as con:
            con.execute(
                """
                CREATE TABLE IF NOT EXISTS maps (
                  map_id TEXT PRIMARY KEY,
                  created_at REAL NOT NULL,
                  layout_json TEXT NOT NULL,
                  metadata_json TEXT NOT NULL,
                  tactile_pdf_url TEXT
                )
                """
            )
            con.execute(
                """
                CREATE TABLE IF NOT EXISTS chats (
                  map_id TEXT NOT NULL,
                  session_id TEXT NOT NULL,
                  created_at REAL NOT NULL,
                  updated_at REAL NOT NULL,
                  messages_json TEXT NOT NULL,
                  PRIMARY KEY (map_id, session_id)
                )
                """
            )

    def put_map(
        self,
        *,
        map_id: str,
        layout_2d: dict[str, Any],
        metadata: dict[str, Any],
        tactile_pdf_url: str | None = None,
    ) -> None:
        now = time.time()
        with self._connect() as con:
            con.execute(
                """
                INSERT OR REPLACE INTO maps(map_id, created_at, layout_json, metadata_json, tactile_pdf_url)
                VALUES (?, ?, ?, ?, ?)
                """,
                (
                    map_id,
                    now,
                    json.dumps(layout_2d),
                    json.dumps(metadata),
                    tactile_pdf_url,
                ),
            )

    def get_map(self, map_id: str) -> MapRecord | None:
        with self._connect() as con:
            row = con.execute(
                "SELECT map_id, created_at, layout_json, metadata_json, tactile_pdf_url FROM maps WHERE map_id=?",
                (map_id,),
            ).fetchone()
        if not row:
            return None
        return MapRecord(
            map_id=str(row["map_id"]),
            created_at=float(row["created_at"]),
            layout_2d=json.loads(row["layout_json"]),
            metadata=json.loads(row["metadata_json"]),
            tactile_pdf_url=row["tactile_pdf_url"],
        )

    def get_chat_messages(self, *, map_id: str, session_id: str) -> list[dict[str, Any]]:
        with self._connect() as con:
            row = con.execute(
                "SELECT messages_json FROM chats WHERE map_id=? AND session_id=?",
                (map_id, session_id),
            ).fetchone()
        if not row:
            return []
        try:
            msgs = json.loads(row["messages_json"])
            return list(msgs) if isinstance(msgs, list) else []
        except Exception:
            return []

    def upsert_chat_messages(
        self,
        *,
        map_id: str,
        session_id: str,
        messages: list[dict[str, Any]],
    ) -> None:
        now = time.time()
        with self._connect() as con:
            con.execute(
                """
                INSERT INTO chats(map_id, session_id, created_at, updated_at, messages_json)
                VALUES (?, ?, ?, ?, ?)
                ON CONFLICT(map_id, session_id) DO UPDATE SET
                  updated_at=excluded.updated_at,
                  messages_json=excluded.messages_json
                """,
                (map_id, session_id, now, now, json.dumps(messages)),
            )

