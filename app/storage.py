"""Small local SQLite store; diary content never leaves this database."""
from datetime import datetime, timedelta, timezone
from pathlib import Path
import sqlite3


def timestamp() -> str:
    return datetime.now(timezone.utc).isoformat()


class Store:
    def __init__(self, directory: Path):
        directory.mkdir(parents=True, exist_ok=True)
        self.path = directory / "ayan.sqlite3"
        with self.connect() as connection:
            connection.executescript("""
                CREATE TABLE IF NOT EXISTS diary (
                    id INTEGER PRIMARY KEY, content TEXT NOT NULL, created_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS reminders (
                    id INTEGER PRIMARY KEY, title TEXT NOT NULL, due_at TEXT NOT NULL,
                    completed INTEGER NOT NULL DEFAULT 0
                );
            """)

    def connect(self):
        connection = sqlite3.connect(self.path, timeout=10)
        connection.row_factory = sqlite3.Row
        return connection

    def add_entry(self, content: str) -> dict:
        content = content.strip()
        if not 1 <= len(content) <= 10000:
            raise ValueError("Күнделік жазбасы 1–10000 таңба болуы керек.")
        with self.connect() as connection:
            cursor = connection.execute(
                "INSERT INTO diary (content, created_at) VALUES (?, ?)", (content, timestamp())
            )
            return dict(connection.execute("SELECT * FROM diary WHERE id=?", (cursor.lastrowid,)).fetchone())

    def entries(self) -> list[dict]:
        with self.connect() as connection:
            return [dict(row) for row in connection.execute("SELECT * FROM diary ORDER BY id DESC LIMIT 200")]

    def delete_entry(self, entry_id: int) -> bool:
        with self.connect() as connection:
            return connection.execute("DELETE FROM diary WHERE id=?", (entry_id,)).rowcount > 0

    def add_reminder(self, title: str, delay_seconds: int) -> dict:
        title = title.strip()
        if not 1 <= len(title) <= 500 or not 1 <= delay_seconds <= 2592000:
            raise ValueError("Еске салу мәтіні қажет; уақыт 1 секундтан 30 күнге дейін болуы керек.")
        due = (datetime.now(timezone.utc) + timedelta(seconds=delay_seconds)).isoformat()
        with self.connect() as connection:
            cursor = connection.execute("INSERT INTO reminders (title, due_at) VALUES (?, ?)", (title, due))
            row = dict(connection.execute("SELECT * FROM reminders WHERE id=?", (cursor.lastrowid,)).fetchone())
            row["completed"] = bool(row["completed"])
            return row

    def reminders(self) -> list[dict]:
        with self.connect() as connection:
            rows = [dict(row) for row in connection.execute(
                "SELECT * FROM reminders ORDER BY completed, due_at LIMIT 1000"
            )]
        for row in rows:
            row["completed"] = bool(row["completed"])
        return rows

    def complete_reminder(self, reminder_id: int) -> bool:
        with self.connect() as connection:
            return connection.execute("UPDATE reminders SET completed=1 WHERE id=?", (reminder_id,)).rowcount > 0
