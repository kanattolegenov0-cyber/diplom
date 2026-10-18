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
    assert calls[0][1]["rate"] == "-8%"
    assert calls[0][1]["pitch"] == "-8Hz"
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


@pytest.mark.asyncio
@pytest.mark.parametrize(("settings", "rate", "pitch"), [
    ({"speech_style": "natural"}, "+0%", "+0Hz"),
    ({"speech_style": "serious", "speech_rate": -30, "speech_pitch": -20}, "-30%", "-20Hz"),
    ({"speech_style": "serious", "speech_rate": 20, "speech_pitch": 10}, "+20%", "+10Hz"),
    ({"speech_style": "natural", "speech_rate": -12, "speech_pitch": 4}, "-12%", "+4Hz"),
])
async def test_edge_uses_selected_style_and_valid_prosody_bounds(edge_mock, settings, rate, pitch):
    await SpeechService(FakePreferences(settings)).synthesize("Дауыс мәнерін тексеру")
    assert edge_mock[1][0][1]["rate"] == rate
    assert edge_mock[1][0][1]["pitch"] == pitch


@pytest.mark.asyncio
@pytest.mark.parametrize(("name", "value"), [
    ("speech_style", "angry"), ("speech_style", None),
    ("speech_style", []), ("speech_style", {}),
    ("speech_rate", -31), ("speech_rate", 21), ("speech_rate", True),
    ("speech_rate", -8.5), ("speech_rate", "-8"), ("speech_rate", None),
    ("speech_pitch", -21), ("speech_pitch", 11), ("speech_pitch", False),
    ("speech_pitch", -8.5), ("speech_pitch", "-8"), ("speech_pitch", None),
])
async def test_invalid_prosody_fails_before_provider_request(edge_mock, name, value):
    service = SpeechService(FakePreferences({name: value}))
    with pytest.raises(SpeechError):
        await service.synthesize("Сәлем")
    assert edge_mock[1] == []
    assert not service._cache


@pytest.mark.asyncio
async def test_audio_cache_distinguishes_style_rate_and_pitch():
    preferences = FakePreferences()
    service = SpeechService(preferences)
    service._edge = AsyncMock(side_effect=[b"default", b"slower", b"lower", b"natural"])
    assert await service.synthesize("Бірдей мәтін") == b"default"
    assert await service.synthesize("Бірдей мәтін") == b"default"
    preferences.values["speech_rate"] = -15
    assert await service.synthesize("Бірдей мәтін") == b"slower"
    preferences.values["speech_pitch"] = -12
    assert await service.synthesize("Бірдей мәтін") == b"lower"
    preferences.values["speech_style"] = "natural"
    assert await service.synthesize("Бірдей мәтін") == b"natural"
    preferences.values.clear()
    assert await service.synthesize("Бірдей мәтін") == b"default"
    assert service._edge.await_count == 4


@pytest.mark.asyncio
async def test_queued_synthesis_keeps_cache_and_provider_prosody_consistent():
    preferences = FakePreferences()
    service = SpeechService(preferences)
    calls = []

    async def synthesize_with_snapshot(text, voice, *, prosody=None):
        calls.append(prosody)
        return str(prosody).encode()

    service._edge = synthesize_with_snapshot
    await service._lock.acquire()
    pending = asyncio.create_task(service.synthesize("Бірдей мәтін"))
    try:
        # The pending request captures its settings, then waits for the provider lock.
        await asyncio.sleep(0)
        preferences.values.update(speech_style="natural", speech_rate=15, speech_pitch=5)
    finally:
        service._lock.release()
    old_audio = await pending
    assert calls == [("serious", -8, -8)]
    assert old_audio == str(("serious", -8, -8)).encode()
    assert await service.synthesize("Бірдей мәтін") == str(("natural", 15, 5)).encode()
    preferences.values.clear()
    assert await service.synthesize("Бірдей мәтін") == old_audio
    assert calls == [("serious", -8, -8), ("natural", 15, 5)]


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
    assert json.loads(requests[1].content) == {
        "text": "Сәлем", "model_id": "eleven_v3", "voice_settings": {
            "stability": 0.5, "similarity_boost": 0.85, "style": 0.15,
            "use_speaker_boost": True, "speed": 0.92,
        },
    }
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
@pytest.mark.parametrize(("model", "style", "rate", "stability", "style_amount"), [
    ("eleven_v3", "serious", -30, 0.5, 0.15),
    ("eleven_v3", "natural", 20, 0.5, 0.3),
    ("eleven_multilingual_v2", "serious", -8, 0.7, 0.15),
    ("eleven_multilingual_v2", "natural", 0, 0.5, 0.3),
])
async def test_elevenlabs_receives_supported_voice_settings(eleven_http, model, style, rate, stability, style_amount):
    eleven_http[0]["models"] = [{
        "model_id": model, "can_do_text_to_speech": True,
        "languages": [{"language_id": "ru"}],
    }]
    service = SpeechService(eleven_preferences(
        elevenlabs_model=model, speech_style=style, speech_rate=rate,
    ))
    await service.synthesize("Говорите спокойно", "ru-RU")
    body = json.loads(eleven_http[1][-1].content)
    settings = body["voice_settings"]
    assert settings == {
        "stability": stability, "similarity_boost": 0.85, "style": style_amount,
        "use_speaker_boost": True, "speed": pytest.approx(1 + rate / 100),
    }


@pytest.mark.asyncio
async def test_invalid_prosody_never_spends_elevenlabs_credits(eleven_http):
    with pytest.raises(SpeechError):
        await SpeechService(eleven_preferences(speech_pitch=11)).synthesize("Сәлем")
    assert eleven_http[1] == []


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
