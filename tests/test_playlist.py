"""A persistent automatic playlist counts playback, with idempotent sessions."""

from concurrent.futures import ThreadPoolExecutor
import sqlite3

import pytest

from app.storage import Store


VIDEO_A = "dQw4w9WgXcQ"
VIDEO_B = "jNQXAC9IVRw"
VIDEO_C = "aqz-KE-bpKQ"


@pytest.fixture
def store(tmp_path):
    return Store(tmp_path)


def listen(store, video=VIDEO_A, session="session-001", seconds=30, title="Ақпен бірге", channel="Moldanazar"):
    return store.record_listen(video, title, channel, session, seconds)


def test_playlist_starts_empty_and_partial_listens_do_not_add_tracks(store):
    assert store.frequent_tracks() == []
    assert listen(store, seconds=0.5) == {"counted": False, "track": None}
    assert listen(store, seconds=29.999) == {"counted": False, "track": None}
    assert store.frequent_tracks() == []
    result = listen(store, seconds=30)
    assert result["counted"] is True
    assert result["track"]["play_count"] == 1
    assert result["track"]["total_listen_seconds"] == 30
    assert store.frequent_tracks() == [result["track"]]


def test_retried_session_and_pause_resume_never_add_another_play(store):
    first = listen(store, seconds=35)
    assert listen(store, seconds=35) == first
    assert listen(store, seconds=20) == first
    resumed = listen(store, seconds=65)
    assert resumed["track"]["play_count"] == 1
    assert resumed["track"]["total_listen_seconds"] == 65
    assert listen(store, seconds=35) == resumed
    assert len(store.frequent_tracks()) == 1


def test_replaying_video_with_another_session_adds_one_play(store):
    listen(store, seconds=60)
    replay = listen(store, session="session-002", seconds=40)
    assert replay["track"]["play_count"] == 2
    assert replay["track"]["total_listen_seconds"] == 100
    listen(store, session="session-002", seconds=80)
    assert store.frequent_tracks()[0]["play_count"] == 2
    assert store.frequent_tracks()[0]["total_listen_seconds"] == 140


def test_partial_session_does_not_change_an_existing_track(store):
    track = listen(store, seconds=50)["track"]
    assert listen(store, session="partial-002", seconds=29) == {"counted": False, "track": None}
    assert store.frequent_tracks() == [track]


@pytest.mark.parametrize("seconds", [10, 45])
def test_session_cannot_be_reassigned_to_a_different_video(store, seconds):
    listen(store, seconds=seconds)
    with pytest.raises(ValueError, match="басқа әнге"):
        listen(store, video=VIDEO_B, seconds=60)
    assert all(track["id"] != VIDEO_B for track in store.frequent_tracks())
    result = listen(store, seconds=70)
    assert result["track"]["play_count"] == 1
    assert result["track"]["total_listen_seconds"] == 70


def test_playlist_orders_by_count_then_recency_and_stable_id(store, monkeypatch):
    times = iter([
        "2026-10-18T10:00:00+00:00", "2026-10-18T11:00:00+00:00",
        "2026-10-18T12:00:00+00:00", "2026-10-18T13:00:00+00:00",
    ])
    monkeypatch.setattr("app.storage.timestamp", lambda: next(times))
    listen(store, video=VIDEO_A)
    listen(store, video=VIDEO_A, session="session-002")
    listen(store, video=VIDEO_B, session="session-003")
    listen(store, video=VIDEO_C, session="session-004")
    assert [track["id"] for track in store.frequent_tracks()] == [VIDEO_A, VIDEO_C, VIDEO_B]
    assert [track["id"] for track in store.frequent_tracks(limit=2)] == [VIDEO_A, VIDEO_C]
    with store.connect() as connection:
        connection.execute("UPDATE tracks SET last_played_at=?", ("2026-10-18T00:00:00+00:00",))
    assert [track["id"] for track in store.frequent_tracks()] == [VIDEO_A, *sorted([VIDEO_B, VIDEO_C])]


def test_playlist_and_partial_session_survive_restart(tmp_path):
    first = Store(tmp_path)
    track = listen(first, seconds=50)["track"]
    listen(first, video=VIDEO_B, session="session-002", seconds=20)
    restarted = Store(tmp_path)
    assert restarted.frequent_tracks() == [track]
    assert listen(restarted, seconds=50)["track"]["play_count"] == 1
    assert listen(restarted, video=VIDEO_B, session="session-002", seconds=31)["track"]["play_count"] == 1
    assert listen(restarted, video=VIDEO_B, session="session-002", seconds=31)["track"]["total_listen_seconds"] == 31


def test_migration_preserves_preexisting_diary_and_reminders(tmp_path):
    with sqlite3.connect(tmp_path / "ayan.sqlite3") as connection:
        connection.executescript("""
            CREATE TABLE diary (id INTEGER PRIMARY KEY, content TEXT NOT NULL, created_at TEXT NOT NULL);
            CREATE TABLE reminders (id INTEGER PRIMARY KEY, title TEXT NOT NULL,
                due_at TEXT NOT NULL, completed INTEGER NOT NULL DEFAULT 0);
            INSERT INTO diary VALUES (7, 'Ескі жазба', '2026-10-17T00:00:00+00:00');
            INSERT INTO reminders VALUES (9, 'Ескі еске салу', '2026-10-19T00:00:00+00:00', 0);
        """)
    migrated = Store(tmp_path)
    assert migrated.entries()[0]["content"] == "Ескі жазба"
    assert migrated.reminders()[0]["title"] == "Ескі еске салу"
    listen(migrated)
    migrated.add_entry("Жаңа жазба")
    assert migrated.complete_reminder(9) is True
    assert len(Store(tmp_path).entries()) == 2
    assert Store(tmp_path).reminders()[0]["completed"] is True
    assert len(Store(tmp_path).frequent_tracks()) == 1


def test_thumbnail_is_derived_only_from_valid_video_id(store):
    track = listen(store)["track"]
    assert set(track) == {"id", "title", "channel", "play_count", "total_listen_seconds", "last_played_at", "thumbnail"}
    assert track["thumbnail"] == f"https://i.ytimg.com/vi/{VIDEO_A}/hqdefault.jpg"


def test_metadata_is_bound_as_data_and_does_not_change_existing_tables(store):
    store.add_entry("Сақталған күнделік")
    title = "'; DROP TABLE diary; --"
    channel = '<img src="https://evil.example/">'
    track = listen(store, title=title, channel=channel)["track"]
    assert track["title"] == title
    assert track["channel"] == channel
    assert store.entries()[0]["content"] == "Сақталған күнделік"
    assert track["thumbnail"].startswith("https://i.ytimg.com/vi/")


def test_empty_channel_on_progress_does_not_erase_known_channel(store):
    listen(store)
    assert listen(store, seconds=40, channel="")["track"]["channel"] == "Moldanazar"


@pytest.mark.parametrize("field,value", [
    ("video_id", "https://youtu.be/dQw4w9WgXcQ"),
    ("video_id", "x" * 10), ("video_id", "x" * 12), ("video_id", "x' OR 1=1--"),
    ("video_id", "😺" * 11), ("video_id", None),
    ("session_id", "short"), ("session_id", "x" * 101),
    ("session_id", "' DROP TABLE tracks;--"), ("session_id", "https://example.com"),
    ("title", ""), ("title", "  "), ("title", "x" * 301), ("title", None),
    ("channel", "x" * 201), ("channel", None),
    ("listened_seconds", 0), ("listened_seconds", -1), ("listened_seconds", 86401),
    ("listened_seconds", 10 ** 1000),
    ("listened_seconds", float("nan")), ("listened_seconds", float("inf")),
    ("listened_seconds", True), ("listened_seconds", "30"), ("listened_seconds", None),
])
def test_invalid_input_has_no_persistent_effect(store, field, value):
    arguments = dict(video_id=VIDEO_A, title="Title", channel="Channel", session_id="session-001", listened_seconds=30)
    arguments[field] = value
    with pytest.raises(ValueError):
        store.record_listen(**arguments)
    assert store.frequent_tracks() == []
    with store.connect() as connection:
        assert connection.execute("SELECT count(*) FROM play_sessions").fetchone()[0] == 0


@pytest.mark.parametrize("limit", [0, -1, 101, True, 1.5, "30"])
def test_invalid_playlist_limits_are_rejected(store, limit):
    with pytest.raises(ValueError):
        store.frequent_tracks(limit)


def test_concurrent_same_session_progress_counts_once(store):
    with ThreadPoolExecutor(max_workers=8) as workers:
        list(workers.map(lambda seconds: listen(store, seconds=seconds), [30, 70, 40, 50, 70, 60, 35, 45]))
    track = store.frequent_tracks()[0]
    assert track["play_count"] == 1
    assert track["total_listen_seconds"] == 70


def test_concurrent_replays_of_same_track_are_not_lost(store):
    with ThreadPoolExecutor(max_workers=8) as workers:
        list(workers.map(lambda index: listen(store, session=f"session-{index:03d}", seconds=40), range(12)))
    track = store.frequent_tracks()[0]
    assert track["play_count"] == 12
    assert track["total_listen_seconds"] == 480
