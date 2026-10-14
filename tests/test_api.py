import asyncio
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app.main import create_app
from app.music import MusicError, MusicService, video_id
from app.storage import Store


class FakeBrowser:
    def __init__(self):
        self.calls = []

    async def available(self):
        return True

    async def execute(self, *args):
        self.calls.append(args)
        return {"url": "https://example.com", "title": "Example", "screenshot": "cG5n", "message": "Сайт ашылды."}

    async def close(self):
        pass


@pytest.fixture
def client(tmp_path):
    with TestClient(create_app(tmp_path, browser=FakeBrowser())) as client:
        yield client


def test_dashboard_serves_real_assets(client):
    response = client.get("/")
    assert response.status_code == 200
    assert "AYAN" in response.text
    assert client.get("/static/app.js").status_code == 200
    assert client.get("/api/health").json()["status"] == "ok"


def test_diary_command_persists_and_deletes(client):
    result = client.post("/api/command", json={"text": "Күнделікке жаз: Бүгін жобамды бастадым"}).json()
    entry = result["data"]["entry"]
    assert result["intent"] == "diary_add"
    assert client.get("/api/diary").json()["entries"][0]["content"] == "Бүгін жобамды бастадым"
    assert client.delete(f"/api/diary/{entry['id']}").status_code == 200
    assert client.get("/api/diary").json()["entries"] == []
    assert client.delete(f"/api/diary/{entry['id']}").status_code == 404


def test_diary_survives_store_recreation(tmp_path):
    Store(tmp_path).add_entry("'quoted' ; DROP TABLE diary; --")
    assert Store(tmp_path).entries()[0]["content"] == "'quoted' ; DROP TABLE diary; --"


def test_reminder_command_completes(client):
    result = client.post("/api/command", json={"text": "10 минуттан кейін су ішуді еске сал"}).json()
    reminder = result["data"]["reminder"]
    assert reminder["completed"] is False
    assert reminder["title"] == "су ішуді"
    assert client.post(f"/api/reminders/{reminder['id']}/complete").status_code == 200
    assert client.get("/api/reminders").json()["reminders"][0]["completed"] is True


@pytest.mark.parametrize("endpoint,payload", [
    ("/api/diary", {"content": "   "}),
    ("/api/reminders", {"title": "x", "delay_seconds": -5}),
    ("/api/command", {"text": "громче", "volume": 105}),
])
def test_invalid_data_not_saved(client, endpoint, payload):
    assert client.post(endpoint, json=payload).status_code == 422


def test_music_controls_do_not_claim_server_playback(client):
    result = client.post("/api/command", json={"text": "дыбысты азайт", "volume": 60}).json()
    assert result["intent"] == "music_control"
    assert result["payload"] == {"action": "volume", "volume": 50}


def test_browser_click_requires_explicit_confirmation(tmp_path):
    browser = FakeBrowser()
    with TestClient(create_app(tmp_path, browser=browser)) as client:
        assert client.post("/api/browser/action", json={"action": "click", "target": "Send"}).status_code == 409
        assert browser.calls == []
        assert client.post("/api/browser/action", json={"action": "click", "target": "Read", "confirmed": True}).status_code == 200
        assert len(browser.calls) == 1


def test_browser_commands_only_parse_until_frontend_executes(client):
    result = client.post("/api/command", json={"text": "сайт аш example.com"}).json()
    assert result["intent"] == "browser_open"
    assert result["payload"]["url"] == "https://example.com"


def test_cross_origin_and_rebinding_requests_rejected(client):
    assert client.post("/api/diary", headers={"Origin": "https://other.example"}, json={"content": "x"}).status_code == 403
    assert client.get("/api/health", headers={"Host": "other.example"}).status_code == 400
    assert client.post("/api/diary", headers={"Origin": "http://testserver"}, json={"content": "local"}).status_code == 201


def test_native_mic_requires_desktop_mode(client, monkeypatch):
    monkeypatch.delenv("AYAN_DESKTOP_MODE", raising=False)
    assert client.post("/api/voice/listen", json={"language": "kk-KZ"}).status_code == 409
    monkeypatch.setenv("AYAN_DESKTOP_MODE", "1")
    monkeypatch.setattr("app.voice.listen", lambda language: "Музыка қос: Moldanazar")
    assert client.post("/api/voice/listen", json={"language": "kk-KZ"}).json()["text"] == "Музыка қос: Moldanazar"


def test_youtube_url_route_works_without_search_access(client):
    response = client.get("/api/music/search", params={"q": "https://youtu.be/dQw4w9WgXcQ"})
    assert response.status_code == 200
    assert response.json()["tracks"][0]["id"] == "dQw4w9WgXcQ"


@pytest.mark.parametrize("url", ["https://youtube.com.evil.example/watch?v=dQw4w9WgXcQ", "https://youtu.be/invalid", "not a url"])
def test_youtube_id_validation(url):
    assert video_id(url) is None


async def test_search_metadata_and_cache(monkeypatch):
    calls = []

    def search(query):
        calls.append(query)
        return {"tracks": [{"id": "dQw4w9WgXcQ", "title": "Song"}], "source": "youtube"}

    service = MusicService()
    monkeypatch.setattr(service, "_search", search)
    assert (await service.search("Song"))["tracks"]
    assert (await service.search("song"))["tracks"]
    assert calls == ["Song"]


async def test_search_failure_is_actionable(monkeypatch):
    service = MusicService()

    def failing(query):
        raise OSError("upstream failed")

    monkeypatch.setattr(service, "_search", failing)
    with pytest.raises(MusicError, match="сілтемесін"):
        await service.search("song")
