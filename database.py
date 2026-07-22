from __future__ import annotations

import json
import sqlite3
from pathlib import Path
from typing import Any


class Database:
    def __init__(self, path: str = "casesolve.sqlite3") -> None:
        self.connection = sqlite3.connect(Path(path), check_same_thread=False)
        self.connection.row_factory = sqlite3.Row
        self.connection.executescript(
            """
            CREATE TABLE IF NOT EXISTS settings (
                chat_id INTEGER PRIMARY KEY,
                owner_id INTEGER NOT NULL,
                court_chat_id INTEGER,
                court_title TEXT
            );
            CREATE TABLE IF NOT EXISTS cases (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                source_chat_id INTEGER NOT NULL,
                court_chat_id INTEGER,
                plaintiff_id INTEGER,
                defendant_id INTEGER,
                judge_id INTEGER,
                witnesses TEXT NOT NULL DEFAULT '[]',
                state TEXT NOT NULL DEFAULT 'pending',
                turn TEXT NOT NULL DEFAULT 'plaintiff',
                complainant_id INTEGER NOT NULL,
                created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
            );
            """
        )
        self.connection.commit()

    def settings(self, chat_id: int) -> sqlite3.Row | None:
        return self.connection.execute("SELECT * FROM settings WHERE chat_id=?", (chat_id,)).fetchone()

    def save_settings(self, chat_id: int, owner_id: int, court_chat_id: int | None = None, title: str = "") -> None:
        self.connection.execute(
            """
            INSERT INTO settings(chat_id,owner_id,court_chat_id,court_title) VALUES(?,?,?,?)
            ON CONFLICT(chat_id) DO UPDATE SET owner_id=excluded.owner_id,
            court_chat_id=COALESCE(excluded.court_chat_id, settings.court_chat_id),
            court_title=COALESCE(NULLIF(excluded.court_title,''), settings.court_title)
            """,
            (chat_id, owner_id, court_chat_id, title),
        )
        self.connection.commit()

    def create_case(self, values: dict[str, Any]) -> int:
        cursor = self.connection.execute(
            """
            INSERT INTO cases(source_chat_id,court_chat_id,plaintiff_id,defendant_id,judge_id,
            witnesses,state,turn,complainant_id) VALUES(?,?,?,?,?,?,?,?,?)
            """,
            (
                values["source_chat_id"], values.get("court_chat_id"), values.get("plaintiff_id"),
                values.get("defendant_id"), values.get("judge_id"), json.dumps(values.get("witnesses", [])),
                values.get("state", "pending"), values.get("turn", "plaintiff"), values["complainant_id"],
            ),
        )
        self.connection.commit()
        return int(cursor.lastrowid)

    def case(self, case_id: int) -> sqlite3.Row | None:
        return self.connection.execute("SELECT * FROM cases WHERE id=?", (case_id,)).fetchone()

    def update_case(self, case_id: int, **values: Any) -> None:
        if "witnesses" in values:
            values["witnesses"] = json.dumps(values["witnesses"])
        assignments = ", ".join(f"{key}=?" for key in values)
        self.connection.execute(f"UPDATE cases SET {assignments} WHERE id=?", (*values.values(), case_id))
        self.connection.commit()

    def active_case(self, chat_id: int) -> sqlite3.Row | None:
        return self.connection.execute(
            "SELECT * FROM cases WHERE court_chat_id=? AND state NOT IN ('finished','stopped') ORDER BY id DESC LIMIT 1",
            (chat_id,),
        ).fetchone()

    def delete_case(self, case_id: int) -> None:
        self.connection.execute("DELETE FROM cases WHERE id=?", (case_id,))
        self.connection.commit()