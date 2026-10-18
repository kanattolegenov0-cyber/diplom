"""Frequent-playlist requests stay separate from YouTube title searches."""

import json

import httpx
import pytest

from app.assistant import AssistantService, validate_action
from app.commands import parse_command


class Preferences:
    def __init__(self, key=None):
        self.key = key

    def get(self, name, default=None):
        return default

    def secret(self, name):
        return self.key


@pytest.mark.parametrize("text", [
    "плейлистімді қос", "Аян, менің плейлистімді қос", "плейлистті ойнат",
    "жиі тыңдайтын әндерімді қос", "жиі тыңдаған өлеңдерімді қосып бер",
    "жиі тыңдалған әндерді қой", "включи мой плейлист", "запусти плейлист",
    "включи часто прослушиваемые песни", "проиграй мои часто слушаемые треки",
])
async def test_play_playlist_works_offline_without_song_search(text):
    result = await AssistantService(Preferences()).resolve(text)
    assert result["intent"] == "music_playlist"
    assert result["payload"] == {"action": "play"}
    assert result["engine"] == "rules"


@pytest.mark.parametrize("text", [
    "плейлистім", "плейлистімді көрсет", "менің плейлистімді аш",
    "жиі тыңдайтын әндерім", "жиі тыңдаған әндерімді көрсет",
    "покажи мой плейлист", "открой мой плейлист", "плейлист",
])
def test_show_playlist_is_not_browser_navigation(text):
    result = parse_command(text)
    assert result["intent"] == "music_playlist"
    assert result["payload"] == {"action": "show"}


@pytest.mark.parametrize("text", [
    "Новый плейлист", "Создай плейлист", "Плейлистімді өзгерт",
    "Жиі тыңдайтын әндер", "Часто прослушиваемые песни",
    "включи новый плейлист", "музыка қос менің жаңа плейлистім",
])
async def test_unsupported_playlist_requests_do_not_become_bare_song_search(text):
    result = await AssistantService(Preferences()).resolve(text)
    assert result["intent"] != "music_search"


def test_explicit_song_request_and_diary_content_remain_intact():
    assert parse_command("включи песню Димаш SOS")["payload"] == {"query": "Димаш SOS"}
    assert parse_command("күнделікке жаз Плейлистімді қос деген ой келді") == {
        "intent": "diary_add", "message": "Күнделікке жазылды.",
        "payload": {"content": "Плейлистімді қос деген ой келді"},
    }


@pytest.mark.parametrize("action", ["play", "show"])
def test_playlist_action_schema(action):
    value = {"intent": "music_playlist", "payload": {"action": action}}
    assert validate_action(value) == value


@pytest.mark.parametrize("payload", [
    {}, {"action": "delete"}, {"action": "play", "tracks": ["invented"]},
    {"action": "show", "path": "/some/file"}, {"action": True},
])
def test_playlist_schema_rejects_unsupported_actions_and_properties(payload):
    with pytest.raises(ValueError):
        validate_action({"intent": "music_playlist", "payload": payload})


async def test_online_model_can_plan_frequent_playlist_without_track_data(monkeypatch):
    original_client = httpx.AsyncClient
    requests = []

    def respond(request):
        requests.append(json.loads(request.content))
        plan = {"message": "Жиі тыңдайтын әндеріңізді қосуға дайынмын.", "actions": [
            {"intent": "music_playlist", "payload": {"action": "play"}},
        ]}
        return httpx.Response(200, json={"choices": [{"finish_reason": "stop", "message": {
            "content": json.dumps(plan, ensure_ascii=False),
        }}]})

    monkeypatch.setattr("app.assistant.httpx.AsyncClient", lambda **kwargs:
                        original_client(**kwargs, transport=httpx.MockTransport(respond)))
    result = await AssistantService(Preferences("test-key")).resolve("Әдетте тыңдайтын музыкамды жіберсең")
    assert result["intent"] == "plan"
    assert result["payload"]["actions"] == [{"intent": "music_playlist", "payload": {"action": "play"}}]
    system_prompt = requests[0]["messages"][0]["content"]
    assert 'music_playlist {action:"play"|"show"}' in system_prompt
    assert "never invent songs, listening history" in system_prompt
