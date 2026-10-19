import httpx
import pytest
import sys
from types import SimpleNamespace
from fastapi.testclient import TestClient

from app.anime import AnimeError, AnimeService, GACHIAKUTA, safe_anime_url
from app.assistant import validate_action
from app.commands import parse_command
from app.main import create_app
from desktop import AnimeWindowApi


@pytest.mark.parametrize("text,query,season,episode", [
    ("Гачакута 1 сезон 3 серия қос", "Гачакута", 1, 3),
    ("Аян, аниме Гачиакута 2 серия", "Гачиакута", None, 2),
    ("Гачиакута 1 сезоны 3 сериясын қос", "Гачиакута", 1, 3),
    ("Naruto season 1", "Naruto", 1, None),
    ("Gachiakuta", "Gachiakuta", None, None),
])
def test_anime_command_extracts_requested_episode(text, query, season, episode):
    result = parse_command(text)
    assert result["intent"] == "anime_search"
    assert result["payload"] == {"query": query, "season": season, "episode": episode}


def test_anime_mentioned_inside_diary_stays_a_diary_entry():
    assert parse_command("күнделікке жаз: бүгін аниме Гачиакута көрдім")["intent"] == "diary_add"


def test_anime_action_is_bounded():
    assert validate_action({"intent": "anime_search", "payload": {"query": "Naruto", "season": 1, "episode": 3}})["payload"]["episode"] == 3
    with pytest.raises(ValueError):
        validate_action({"intent": "anime_search", "payload": {"query": "Naruto", "episode": 0}})


@pytest.mark.parametrize("url", ["https://animego.work.evil.test/x", "http://animego.work/x", "https://user@animego.work/x", "javascript:alert(1)"])
def test_anime_url_rejects_untrusted_targets(url):
    assert safe_anime_url(url) is None


async def test_seed_fuzzy_search_works_without_network():
    service = AnimeService()
    assert (await service.search("Гачакута"))["results"][0]["url"] == GACHIAKUTA


async def test_site_search_and_episode_links_use_only_animego():
    calls = []

    def handle(request):
        calls.append(str(request.url))
        if "do=search" in str(request.url):
            return httpx.Response(200, text='''
                <a href="/77-other.html">Other</a>
                <a href="/370-gachiakuta-2025.html">Naruto</a>
                <a href="https://evil.test/370-fake.html">Naruto</a>
            ''')
        return httpx.Response(200, text='''
            <a href="/370-gachiakuta-2025.html?episode=2">1 сезон 2 серия</a>
            <a href="https://evil.test/ep3">1 сезон 3 серия</a>
            <a href="#">1 сезон 4 серия</a>
        ''')

    async with httpx.AsyncClient(transport=httpx.MockTransport(handle)) as client:
        service = AnimeService(client)
        results = await service.search("Narut")
        assert results["results"][0]["title"] == "Naruto"
        episodes = await service.episodes(GACHIAKUTA)
    assert len(calls) == 2
    assert [item["episode"] for item in episodes["episodes"]] == [2]
    assert episodes["episodes"][0]["url"].endswith("?episode=2")


def test_anime_api_uses_service_without_live_site(tmp_path):
    class FakeAnime:
        async def search(self, query):
            return {"results": [{"title": query, "url": GACHIAKUTA}]}

        async def episodes(self, url):
            if url != GACHIAKUTA:
                raise AnimeError("Жарамсыз сілтеме")
            return {"url": url, "episodes": []}

    with TestClient(create_app(tmp_path, anime=FakeAnime())) as client:
        assert client.get("/api/anime/search", params={"q": "Gachiakuta"}).json()["results"][0]["url"] == GACHIAKUTA
        assert client.get("/api/anime/episodes", params={"url": GACHIAKUTA}).status_code == 200
        assert client.get("/api/anime/episodes", params={"url": "https://evil.test"}).status_code == 502


def test_native_anime_window_only_opens_animego(monkeypatch):
    opened = []
    monkeypatch.setitem(sys.modules, "webview", SimpleNamespace(create_window=lambda *args, **kwargs: opened.append((args, kwargs))))
    api = AnimeWindowApi()
    with pytest.raises(ValueError):
        api.open_anime("https://evil.test/watch")
    assert not opened
    assert api.open_anime(GACHIAKUTA) is True
    assert opened[0][1]["url"] == GACHIAKUTA
