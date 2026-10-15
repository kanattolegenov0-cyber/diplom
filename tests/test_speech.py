"""Speech provider tests use mocked streams; no paid or external requests."""

import asyncio
import json
import ssl
import sys
from types import ModuleType, SimpleNamespace
from unittest.mock import AsyncMock, Mock

import httpx
import pytest

from app.speech import SpeechError, SpeechService, _edge_trust_context, _verified_ssl_context


class FakePreferences:
    def __init__(self, values=None, secrets=None):
        self.values = values or {}
        self.secrets = secrets or {}

    def get(self, name, default=None):
        return self.values.get(name, default)

    def secret(self, name):
        return self.secrets.get(name)


@pytest.fixture
def edge_mock(monkeypatch):
    calls = []
    state = {"chunks": [{"type": "WordBoundary"}, {"type": "audio", "data": b"ID3mp3"}]}

    class Communicate:
        def __init__(self, text, **kwargs):
            calls.append((text, kwargs))

        async def stream(self):
            for chunk in state["chunks"]:
                if isinstance(chunk, BaseException):
                    raise chunk
                yield chunk

    edge = ModuleType("edge_tts")
    edge.__path__ = []
    edge.Communicate = Communicate
    communicate = ModuleType("edge_tts.communicate")
    communicate._SSL_CTX = SimpleNamespace(load_default_certs=Mock(), load_verify_locations=Mock())
    edge.communicate = communicate
    monkeypatch.setitem(sys.modules, "edge_tts", edge)
    monkeypatch.setitem(sys.modules, "edge_tts.communicate", communicate)
    return state, calls, communicate._SSL_CTX


@pytest.mark.asyncio
async def test_default_kazakh_voice_stream(edge_mock):
    _, calls, trust = edge_mock
    audio = await SpeechService(FakePreferences()).synthesize("Сәлем!", "kk-KZ")
    assert audio == b"ID3mp3"
    assert calls[0][1]["voice"] == "kk-KZ-DauletNeural"
    assert calls[0][1]["connect_timeout"] == 10
    assert calls[0][1]["receive_timeout"] == 20
    trust.load_default_certs.assert_called_once()


@pytest.mark.asyncio
@pytest.mark.parametrize(("configured", "language", "expected"), [
    ("kk-KZ-AigulNeural", "kk-KZ", "kk-KZ-AigulNeural"),
    ("kk-KZ-AigulNeural", "ru-RU", "ru-RU-SvetlanaNeural"),
    ("ru-RU-DmitryNeural", "kk", "kk-KZ-DauletNeural"),
    ("ru-RU-SvetlanaNeural", "ru", "ru-RU-SvetlanaNeural"),
])
async def test_voice_whitelist_and_language_mapping(edge_mock, configured, language, expected):
    service = SpeechService(FakePreferences({"tts_voice": configured}))
    await service.synthesize("Hello", language)
    assert edge_mock[1][0][1]["voice"] == expected


@pytest.mark.asyncio
async def test_unknown_actor_name_is_not_pretended_voice(edge_mock):
    service = SpeechService(FakePreferences({"tts_voice": "AniLibria actor"}))
    with pytest.raises(SpeechError):
        await service.synthesize("Сәлем")
    assert not edge_mock[1]


@pytest.mark.asyncio
@pytest.mark.parametrize("text", ["", "   ", "x" * 2001, None])
async def test_text_limit_before_request(edge_mock, text):
    with pytest.raises(SpeechError):
        await SpeechService(FakePreferences()).synthesize(text)
    assert not edge_mock[1]


@pytest.mark.asyncio
async def test_unsupported_language_before_request(edge_mock):
    with pytest.raises(SpeechError):
        await SpeechService(FakePreferences()).synthesize("hello", "xx")
    assert not edge_mock[1]


@pytest.mark.asyncio
async def test_proxy_is_passed_without_changing_tls(edge_mock, monkeypatch):
    monkeypatch.setenv("HTTPS_PROXY", "http://proxy.example:8080")
    await SpeechService(FakePreferences()).synthesize("Сәлем")
    assert edge_mock[1][0][1]["proxy"] == "http://proxy.example:8080"


@pytest.mark.asyncio
@pytest.mark.parametrize("chunks", [
    [], [{"type": "audio", "data": "not bytes"}], [ssl.SSLError("certificate verify failed")],
])
async def test_empty_invalid_or_untrusted_audio_is_error(edge_mock, chunks):
    edge_mock[0]["chunks"] = chunks
    service = SpeechService(FakePreferences())
    with pytest.raises(SpeechError):
        await service.synthesize("Сәлем")
    assert not service._cache


@pytest.mark.asyncio
async def test_audio_cap_checked_during_stream(edge_mock, monkeypatch):
    monkeypatch.setattr("app.speech.MAX_AUDIO_BYTES", 4)
    edge_mock[0]["chunks"] = [{"type": "audio", "data": b"123"}, {"type": "audio", "data": b"45"}]
    service = SpeechService(FakePreferences())
    with pytest.raises(SpeechError, match="үлкен"):
        await service.synthesize("Сәлем")
    assert not service._cache


@pytest.mark.asyncio
async def test_cancellation_is_preserved(edge_mock):
    edge_mock[0]["chunks"] = [asyncio.CancelledError()]
    service = SpeechService(FakePreferences())
    with pytest.raises(asyncio.CancelledError):
        await service.synthesize("Сәлем")
    assert not service._cache


@pytest.mark.asyncio
async def test_timeout_is_user_facing():
    service = SpeechService(FakePreferences())
    service._edge = AsyncMock(side_effect=asyncio.TimeoutError())
    with pytest.raises(SpeechError, match="уақытында"):
        await service.synthesize("Сәлем")


@pytest.mark.asyncio
async def test_audio_cache_is_bounded_and_expires(monkeypatch):
    clock = [1000]
    monkeypatch.setattr("app.speech.time", SimpleNamespace(monotonic=lambda: clock[0]))
    service = SpeechService(FakePreferences())
    service._edge = AsyncMock(return_value=b"ID3mp3")
    await service.synthesize("same")
    await service.synthesize("same")
    assert service._edge.await_count == 1
    for index in range(17):
        await service.synthesize(f"text {index}")
    assert len(service._cache) == 16
    await service.synthesize("same")
    assert service._edge.await_count == 19
    clock[0] += 301
    await service.synthesize("same")
    assert service._edge.await_count == 20


def eleven_preferences(**values):
    return FakePreferences(
        {"tts_provider": "elevenlabs", "elevenlabs_voice_id": "A" * 20, **values},
        {"elevenlabs_api_key": "test-only-secret"},
    )


@pytest.fixture
def eleven_http(monkeypatch):
    state = {
        "models": [{"model_id": "eleven_v3", "can_do_text_to_speech": True,
                    "languages": [{"language_id": "kk"}, {"language_id": "ru"}]}],
        "status": 200, "model_status": 200, "audio": b"ID3eleven",
        "content_type": "audio/mpeg",
    }
    requests = []
    options = []

    async def handler(request):
        requests.append(request)
        if request.method == "GET":
            assert request.url == "https://api.elevenlabs.io/v1/models"
            return httpx.Response(state["model_status"], json=state["models"])
        return httpx.Response(state["status"], content=state["audio"], headers={"content-type": state["content_type"]})

    original_client = httpx.AsyncClient

    def client_factory(**kwargs):
        options.append(kwargs)
        return original_client(transport=httpx.MockTransport(handler), **kwargs)

    monkeypatch.setattr("app.speech.httpx.AsyncClient", client_factory)
    return state, requests, options


@pytest.mark.asyncio
async def test_elevenlabs_checks_metadata_before_audio(eleven_http):
    service = SpeechService(eleven_preferences())
    assert await service.synthesize("Сәлем") == b"ID3eleven"
    _, requests, options = eleven_http
    assert [request.method for request in requests] == ["GET", "POST"]
    assert requests[1].url.host == "api.elevenlabs.io"
    assert requests[1].headers["xi-api-key"] == "test-only-secret"
    assert json.loads(requests[1].content) == {"text": "Сәлем", "model_id": "eleven_v3"}
    assert options[0]["verify"].verify_mode == ssl.CERT_REQUIRED
    assert options[0]["verify"].check_hostname is True
    assert options[0]["follow_redirects"] is False


@pytest.mark.asyncio
@pytest.mark.parametrize(("values", "secrets"), [
    ({"tts_provider": "elevenlabs", "elevenlabs_voice_id": "bad/id"}, {"elevenlabs_api_key": "key"}),
    ({"tts_provider": "elevenlabs", "elevenlabs_voice_id": "A" * 20}, {}),
    ({"tts_provider": "elevenlabs", "elevenlabs_voice_id": "A" * 20, "elevenlabs_model": "arbitrary"}, {"elevenlabs_api_key": "key"}),
])
async def test_elevenlabs_configuration_required_before_request(eleven_http, values, secrets):
    service = SpeechService(FakePreferences(values, secrets))
    with pytest.raises(SpeechError):
        await service.synthesize("Сәлем")
    assert not eleven_http[1]


@pytest.mark.asyncio
@pytest.mark.parametrize("metadata", [
    [], [{"model_id": "eleven_v3", "can_do_text_to_speech": True, "languages": [{"language_id": "ru"}]}],
    [{"model_id": "eleven_v3", "can_do_text_to_speech": False, "languages": [{"language_id": "kk"}]}],
])
async def test_unconfirmed_language_never_consumes_synthesis_credits(eleven_http, metadata):
    eleven_http[0]["models"] = metadata
    with pytest.raises(SpeechError, match="растаған жоқ"):
        await SpeechService(eleven_preferences()).synthesize("Сәлем")
    assert [request.method for request in eleven_http[1]] == ["GET"]


@pytest.mark.asyncio
async def test_multilingual_v2_russian_can_be_confirmed(eleven_http):
    eleven_http[0]["models"] = [{"model_id": "eleven_multilingual_v2", "can_do_text_to_speech": True,
                                "languages": [{"language_id": "ru"}]}]
    service = SpeechService(eleven_preferences(elevenlabs_model="eleven_multilingual_v2"))
    assert await service.synthesize("Привет", "ru-RU") == b"ID3eleven"


@pytest.mark.asyncio
async def test_model_metadata_cache_has_ttl(eleven_http, monkeypatch):
    clock = [1000]
    monkeypatch.setattr("app.speech.time", SimpleNamespace(monotonic=lambda: clock[0]))
    service = SpeechService(eleven_preferences())
    await service.synthesize("first")
    await service.synthesize("second")
    assert [request.method for request in eleven_http[1]] == ["GET", "POST", "POST"]
    clock[0] += 301
    await service.synthesize("third")
    assert [request.method for request in eleven_http[1]] == ["GET", "POST", "POST", "GET", "POST"]


@pytest.mark.asyncio
@pytest.mark.parametrize("status", [401, 403, 429, 500])
async def test_provider_errors_are_sanitized(eleven_http, status):
    eleven_http[0]["status"] = status
    with pytest.raises(SpeechError) as caught:
        await SpeechService(eleven_preferences()).synthesize("Сәлем")
    assert "test-only-secret" not in str(caught.value)


@pytest.mark.asyncio
async def test_metadata_error_does_not_send_paid_request(eleven_http):
    eleven_http[0]["model_status"] = 403
    with pytest.raises(SpeechError):
        await SpeechService(eleven_preferences()).synthesize("Сәлем")
    assert [request.method for request in eleven_http[1]] == ["GET"]


@pytest.mark.asyncio
async def test_non_audio_response_rejected(eleven_http):
    eleven_http[0]["content_type"] = "text/html"
    with pytest.raises(SpeechError):
        await SpeechService(eleven_preferences()).synthesize("Сәлем")


@pytest.mark.asyncio
async def test_eleven_audio_stream_size_limit(eleven_http, monkeypatch):
    monkeypatch.setattr("app.speech.MAX_AUDIO_BYTES", 4)
    with pytest.raises(SpeechError, match="үлкен"):
        await SpeechService(eleven_preferences()).synthesize("Сәлем")


def test_edge_extra_ca_preserves_existing_context(monkeypatch):
    monkeypatch.setenv("SSL_CERT_FILE", "/test/platform.pem")
    monkeypatch.setenv("REQUESTS_CA_BUNDLE", "/test/extra.pem")
    context = SimpleNamespace(load_default_certs=Mock(), load_verify_locations=Mock())
    _edge_trust_context(SimpleNamespace(_SSL_CTX=context))
    context.load_default_certs.assert_called_once()
    assert context.load_verify_locations.call_count == 2
    context.load_verify_locations.assert_any_call(cafile="/test/platform.pem")


def test_http_ssl_uses_verified_default_context(monkeypatch):
    context = Mock()
    factory = Mock(return_value=context)
    monkeypatch.setattr("app.speech.ssl.create_default_context", factory)
    monkeypatch.setenv("REQUESTS_CA_BUNDLE", "/test/extra.pem")
    assert _verified_ssl_context() is context
    factory.assert_called_once_with()
    context.load_verify_locations.assert_called_once_with(cafile="/test/extra.pem")
