import json

from fastapi.testclient import TestClient
import httpx
import pytest

from app.main import create_app
from app.ollama import MAX_RESPONSE_BYTES, OllamaService, valid_model_name
from app.preferences import Preferences, SettingsError


@pytest.mark.parametrize("name", ["qwen2.5:3b", "qwen2.5", "user/model:Q4_K_M", "library/qwen2.5:1.5b"])
def test_valid_local_model_names(name):
    assert valid_model_name(name)


@pytest.mark.parametrize("name", [
    None, [], {}, True, "", "../model", "foo/../bar", "http://localhost/model",
    "evil.example:1234/model", "qwen;echo", "qwen\n", "qwen 2.5", "qwen:cloud",
    "gpt-oss:120b-cloud", "MODEL-CLOUD", "qwen@sha256:bad", "x" * 121,
])
def test_invalid_or_cloud_names_are_rejected_without_network(name):
    assert not valid_model_name(name)


@pytest.mark.asyncio
async def test_discovery_is_local_proxy_free_and_does_not_send_keys(tmp_path, monkeypatch):
    preferences = Preferences(tmp_path)
    preferences.update({"llm_api_key": "fake-openai-secret"})
    monkeypatch.setenv("HTTP_PROXY", "http://proxy.invalid:1")
    monkeypatch.setenv("HTTPS_PROXY", "http://proxy.invalid:1")
    calls, options = [], []

    def handle(request):
        calls.append(request)
        return httpx.Response(200, json={"models": [
            {"name": "qwen2.5:3b"}, {"model": "qwen2.5:1.5b"},
            {"name": "qwen2.5:3b"}, {"name": "other:cloud"},
            {"name": "renamed-local", "remote_host": "remote.invalid"},
            {"name": "renamed-alias", "remote_model": "remote-model"},
            {"name": "http://bad/model"}, {}, None,
        ]})

    original = httpx.AsyncClient

    def client(**kwargs):
        options.append(kwargs)
        return original(**kwargs)

    monkeypatch.setattr("app.ollama.httpx.AsyncClient", client)
    status = await OllamaService(preferences, transport=httpx.MockTransport(handle)).status()
    assert status["available"] and status["model_ready"]
    assert status["models"] == ["qwen2.5:1.5b", "qwen2.5:3b"]
    assert len(calls) == 1 and str(calls[0].url) == "http://127.0.0.1:11434/api/tags"
    assert "authorization" not in calls[0].headers
    assert "fake-openai-secret" not in json.dumps(status)
    assert options[0]["trust_env"] is False and options[0]["follow_redirects"] is False


@pytest.mark.asyncio
@pytest.mark.parametrize("model,names,ready", [
    ("qwen2.5", ["qwen2.5:latest"], True),
    ("qwen2.5:3b", ["qwen2.5:1.5b"], False),
    ("qwen2.5:3b", [], False),
])
async def test_selected_model_readiness_is_truthful(tmp_path, model, names, ready):
    preferences = Preferences(tmp_path)
    preferences.update({"ollama_model": model})
    transport = httpx.MockTransport(lambda request: httpx.Response(200, json={"models": [{"name": name} for name in names]}))
    status = await OllamaService(preferences, transport=transport).status()
    assert status["available"] and status["model_ready"] is ready
    assert ("error" in status) is not ready


@pytest.mark.asyncio
@pytest.mark.parametrize("response", [
    httpx.Response(302, headers={"location": "https://outside.invalid"}),
    httpx.Response(500, text="secret stack trace"), httpx.Response(200, text="invalid"),
    httpx.Response(200, json=[]), httpx.Response(200, json={"models": "invalid"}),
    httpx.Response(200, content=b"x" * (MAX_RESPONSE_BYTES + 1)),
])
async def test_bad_discovery_is_bounded_and_sanitized(tmp_path, response):
    calls = []

    def handle(request):
        calls.append(request)
        return response

    status = await OllamaService(Preferences(tmp_path), transport=httpx.MockTransport(handle)).status()
    assert not status["available"] and not status["model_ready"]
    assert status["models"] == [] and status["error"]
    assert "secret stack trace" not in status["error"]
    assert len(calls) == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("exception", [httpx.ConnectError, httpx.ReadTimeout])
async def test_missing_or_busy_server_is_actionable(tmp_path, exception):
    def handle(request):
        raise exception("private server details", request=request)

    status = await OllamaService(Preferences(tmp_path), transport=httpx.MockTransport(handle)).status()
    assert not status["available"] and "Ollama" in status["error"]
    assert "private server details" not in status["error"]


def test_legacy_settings_gain_ollama_without_losing_data_or_keys(tmp_path):
    preferences = Preferences(tmp_path)
    preferences.update({"llm_api_key": "fake-legacy-secret", "tts_voice": "kk-KZ-AigulNeural"})
    saved = json.loads(preferences.path.read_text())
    saved["settings"].pop("llm_provider")
    saved["settings"].pop("ollama_model")
    preferences.path.write_text(json.dumps(saved))
    restored = Preferences(tmp_path)
    assert restored.get("llm_provider") == "ollama"
    assert restored.get("ollama_model") == "qwen2.5:3b"
    assert restored.secret("llm_api_key") == "fake-legacy-secret"
    assert restored.get("tts_voice") == "kk-KZ-AigulNeural"


@pytest.mark.parametrize("updates", [
    {"llm_provider": "external"}, {"ollama_model": "../../model"},
    {"ollama_model": "gpt-oss:120b-cloud"}, {"ollama_model": ""},
])
def test_bad_provider_settings_do_not_change_saved_preferences(tmp_path, updates):
    preferences = Preferences(tmp_path)
    preferences.update({"ollama_model": "qwen2.5:1.5b"})
    before = preferences.path.read_bytes()
    with pytest.raises(SettingsError):
        preferences.update(updates)
    assert preferences.path.read_bytes() == before


def test_ollama_status_and_provider_settings_api(tmp_path):
    preferences = Preferences(tmp_path)
    transport = httpx.MockTransport(lambda request: httpx.Response(200, json={"models": [{"name": "qwen2.5:3b"}]}))
    service = OllamaService(preferences, transport=transport)
    with TestClient(create_app(tmp_path, ollama=service)) as client:
        status = client.get("/api/ollama/status")
        assert status.status_code == 200 and status.json()["model_ready"]
        settings = client.get("/api/settings").json()
        assert settings["llm_provider"] == "ollama" and not settings["llm_configured"]
        response = client.post("/api/settings", json={"llm_provider": "ollama", "ollama_model": "qwen2.5:1.5b"})
        assert response.status_code == 200 and response.json()["ollama_model"] == "qwen2.5:1.5b"
        assert client.post("/api/settings", json={"llm_provider": "bad"}).status_code == 422
        assert client.get("/api/ollama/status", headers={"Origin": "https://outside.invalid"}).status_code == 403
