"""Small local SQLite store; diary content never leaves this database."""
from datetime import datetime, timedelta, timezone
from pathlib import Path
import math
import re
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
                CREATE TABLE IF NOT EXISTS tracks (
                    video_id TEXT PRIMARY KEY NOT NULL
                        CHECK(length(video_id) = 11 AND video_id NOT GLOB '*[^A-Za-z0-9_-]*'),
                    title TEXT NOT NULL CHECK(length(title) BETWEEN 1 AND 300),
                    channel TEXT NOT NULL CHECK(length(channel) <= 200),
                    play_count INTEGER NOT NULL DEFAULT 0 CHECK(play_count >= 0),
                    total_listen_seconds REAL NOT NULL DEFAULT 0 CHECK(total_listen_seconds >= 0),
                    last_played_at TEXT NOT NULL,
                    created_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS play_sessions (
                    session_id TEXT PRIMARY KEY NOT NULL
                        CHECK(length(session_id) BETWEEN 8 AND 100
                            AND session_id NOT GLOB '*[^A-Za-z0-9._-]*'),
                    video_id TEXT NOT NULL
                        CHECK(length(video_id) = 11 AND video_id NOT GLOB '*[^A-Za-z0-9_-]*'),
                    listened_seconds REAL NOT NULL CHECK(listened_seconds > 0 AND listened_seconds <= 86400),
                    counted INTEGER NOT NULL DEFAULT 0 CHECK(counted IN (0, 1)),
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS tracks_frequent
                    ON tracks(play_count DESC, last_played_at DESC, video_id ASC);
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

    @staticmethod
    def _track(row: sqlite3.Row | None) -> dict | None:
        if row is None:
            return None
        return {
            "id": row["video_id"], "title": row["title"], "channel": row["channel"],
            "play_count": row["play_count"], "total_listen_seconds": row["total_listen_seconds"],
            "last_played_at": row["last_played_at"],
            "thumbnail": f"https://i.ytimg.com/vi/{row['video_id']}/hqdefault.jpg",
        }

    def record_listen(self, video_id: str, title: str, channel: str,
                      session_id: str, listened_seconds: float) -> dict:
        """Record monotonic playback progress, counting one play per 30s session.

        The frontend measures actual playback, excluding buffering and pauses.
        Partial sessions are persisted to prevent reuse for a different video;
        they do not appear in the playlist or contribute to aggregate duration.
        """
        if not isinstance(video_id, str) or not re.fullmatch(r"[A-Za-z0-9_-]{11}", video_id):
            raise ValueError("YouTube әнінің ID-і 11 әріп, сан, «_» немесе «-» таңбасынан тұруы керек.")
        if not isinstance(session_id, str) or not re.fullmatch(r"[A-Za-z0-9._-]{8,100}", session_id):
            raise ValueError("Тыңдау сессиясының ID-і 8–100 әріп, сан, «.», «_» немесе «-» таңбасынан тұруы керек.")
        if not isinstance(title, str) or not 1 <= len(title.strip()) <= 300:
            raise ValueError("Ән атауы 1–300 таңба болуы керек.")
        if not isinstance(channel, str) or len(channel.strip()) > 200:
            raise ValueError("Арна атауы 200 таңбадан аспауы керек.")
        if (isinstance(listened_seconds, bool) or not isinstance(listened_seconds, (int, float))
                or not 0 < listened_seconds <= 86400 or not math.isfinite(listened_seconds)):
            raise ValueError("Тыңдау ұзақтығы 0-ден үлкен және 86400 секундтан аспауы керек.")
        title, channel = title.strip(), channel.strip()
        with self.connect() as connection:
            # Serialize the read/modify/write sequence across API worker threads.
            connection.execute("BEGIN IMMEDIATE")
            previous = connection.execute(
                "SELECT * FROM play_sessions WHERE session_id=?", (session_id,)
            ).fetchone()
            if previous is not None and previous["video_id"] != video_id:
                raise ValueError("Бұл тыңдау сессиясы басқа әнге тиесілі. Жаңа сессия бастаңыз.")
            old_seconds = previous["listened_seconds"] if previous is not None else 0
            was_counted = bool(previous["counted"]) if previous is not None else False
            seconds = max(old_seconds, listened_seconds)
            qualified = seconds >= 30
            if previous is None or seconds > old_seconds:
                now = timestamp()
                connection.execute("""
                    INSERT INTO play_sessions
                        (session_id, video_id, listened_seconds, counted, created_at, updated_at)
                    VALUES (?, ?, ?, ?, ?, ?)
                    ON CONFLICT(session_id) DO UPDATE SET
                        listened_seconds=excluded.listened_seconds,
                        counted=excluded.counted, updated_at=excluded.updated_at
                """, (session_id, video_id, seconds, int(qualified), now, now))
                if qualified:
                    increment = 0 if was_counted else 1
                    added_seconds = seconds - old_seconds if was_counted else seconds
                    connection.execute("""
                        INSERT INTO tracks
                            (video_id, title, channel, play_count, total_listen_seconds, last_played_at, created_at)
                        VALUES (?, ?, ?, ?, ?, ?, ?)
                        ON CONFLICT(video_id) DO UPDATE SET
                            title=excluded.title,
                            channel=CASE WHEN excluded.channel != '' THEN excluded.channel ELSE tracks.channel END,
                            play_count=tracks.play_count + excluded.play_count,
                            total_listen_seconds=tracks.total_listen_seconds + excluded.total_listen_seconds,
                            last_played_at=excluded.last_played_at
                    """, (video_id, title, channel, increment, added_seconds, now, now))
            row = connection.execute("SELECT * FROM tracks WHERE video_id=?", (video_id,)).fetchone() if qualified else None
            return {"counted": qualified, "track": self._track(row)}

    def frequent_tracks(self, limit: int = 30) -> list[dict]:
        """Return the local automatic playlist, ranked by plays then recent activity."""
        if isinstance(limit, bool) or not isinstance(limit, int) or not 1 <= limit <= 100:
            raise ValueError("Плейлист көлемі 1–100 ән аралығында болуы керек.")
        with self.connect() as connection:
            rows = connection.execute("""
                SELECT * FROM tracks WHERE play_count > 0
                ORDER BY play_count DESC, last_played_at DESC, video_id ASC LIMIT ?
            """, (limit,))
            return [self._track(row) for row in rows]
