from fastapi.testclient import TestClient
import pytest

from app.main import create_app


def listening(**updates):
    return {"video_id": "dQw4w9WgXcQ", "title": "Ән", "channel": "Орындаушы",
            "session_id": "session-123456", "listened_seconds": 30, **updates}


def test_playback_threshold_retries_replay_and_restart(tmp_path):
    with TestClient(create_app(tmp_path)) as client:
        assert client.get("/api/music/playlist").json() == {"tracks": []}
        assert client.post("/api/music/listens", json=listening(listened_seconds=29)).json()["counted"] is False
        assert client.get("/api/music/playlist").json()["tracks"] == []
        assert client.post("/api/music/listens", json=listening()).json()["counted"] is True
        client.post("/api/music/listens", json=listening())
        client.post("/api/music/listens", json=listening(listened_seconds=45))
        track = client.get("/api/music/playlist").json()["tracks"][0]
        assert track["play_count"] == 1 and track["total_listen_seconds"] == 45
        client.post("/api/music/listens", json=listening(session_id="session-replay"))
    with TestClient(create_app(tmp_path)) as client:
        track = client.get("/api/music/playlist").json()["tracks"][0]
        assert track["play_count"] == 2 and track["total_listen_seconds"] == 75


@pytest.mark.parametrize("updates", [
    {"listened_seconds": True}, {"listened_seconds": -1}, {"listened_seconds": 86401},
    {"listened_seconds": "30"}, {"video_id": "bad"}, {"session_id": "bad"}, {"title": " "},
])
def test_invalid_playback_is_not_recorded(tmp_path, updates):
    with TestClient(create_app(tmp_path)) as client:
        assert client.post("/api/music/listens", json=listening(**updates)).status_code == 422
        assert client.get("/api/music/playlist").json()["tracks"] == []
