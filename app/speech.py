"""Verified-TLS neural speech providers with bounded, in-memory audio caching."""

from __future__ import annotations

import asyncio
import hashlib
import json
import os
import re
import ssl
import time
from collections import OrderedDict
from typing import Any, Protocol

import httpx


class SpeechError(Exception):
    """A speech setup/provider error safe to display to the user."""


class Preferences(Protocol):
    def get(self, name: str, default: Any = None) -> Any: ...
    def secret(self, name: str) -> str | None: ...


EDGE_VOICES = {
    "kk-KZ": ("kk-KZ-DauletNeural", "kk-KZ-AigulNeural"),
    "ru-RU": ("ru-RU-DmitryNeural", "ru-RU-SvetlanaNeural"),
}
# Selecting a preset does not assert language support. The authenticated official
# models endpoint must confirm TTS capability and the requested language before
# any text-to-speech request (which may consume the user's credits).
ELEVENLABS_MODELS = frozenset({"eleven_v3", "eleven_multilingual_v2"})
MAX_TEXT = 2000
MAX_AUDIO_BYTES = 8 * 1024 * 1024
CACHE_LIMIT = 16
CACHE_TTL_SECONDS = 300


def _verified_ssl_context() -> ssl.SSLContext:
    """Respect system/SSL_CERT_FILE trust; add an explicitly supplied CA bundle."""
    context = ssl.create_default_context()
    extra_bundle = os.environ.get("REQUESTS_CA_BUNDLE")
    if extra_bundle:
        context.load_verify_locations(cafile=extra_bundle)
    return context


def _edge_trust_context(communicate_module: Any) -> None:
    # edge-tts 7.3.1 supplies its own certifi context to ws_connect, overriding
    # connector SSL settings. Add platform trust to that context through standard
    # SSLContext methods; keep its certificates and certificate/hostname checks.
    context = communicate_module._SSL_CTX
    context.load_default_certs()
    for name in ("SSL_CERT_FILE", "REQUESTS_CA_BUNDLE"):
        extra_bundle = os.environ.get(name)
        if extra_bundle:
            context.load_verify_locations(cafile=extra_bundle)


class SpeechService:
    def __init__(self, preferences: Preferences) -> None:
        self.preferences = preferences
        self._lock = asyncio.Lock()
        self._cache: OrderedDict[tuple[str, ...], tuple[float, bytes]] = OrderedDict()
        self._model_cache: OrderedDict[str, tuple[float, dict[str, frozenset[str]]]] = OrderedDict()

    def _settings(self, language: str) -> tuple[str, str, str]:
        provider = self.preferences.get("tts_provider", "edge")
        if provider == "edge":
            selected = self.preferences.get("tts_voice", "kk-KZ-DauletNeural")
            all_voices = [voice for voices in EDGE_VOICES.values() for voice in voices]
            if selected not in all_voices:
                raise SpeechError("Таңдалған дауыс қолдау таппайды. Daulet немесе Aigul дауысын таңдаңыз.")
            voices = EDGE_VOICES[language]
            if selected not in voices:
                selected = voices[1 if selected.endswith(("AigulNeural", "SvetlanaNeural")) else 0]
            return provider, selected, ""
        if provider != "elevenlabs":
            raise SpeechError("Дауыс провайдері дұрыс емес. Edge немесе ElevenLabs таңдаңыз.")
        voice = self.preferences.get("elevenlabs_voice_id", "")
        if not isinstance(voice, str) or not re.fullmatch(r"[A-Za-z0-9]{20,64}", voice):
            raise SpeechError("ElevenLabs үшін өзіңізге қолжетімді дауыстың дұрыс Voice ID мәнін сақтаңыз.")
        if not self.preferences.secret("elevenlabs_api_key"):
            raise SpeechError("ElevenLabs API кілтін баптауларда сақтаңыз; кілтті чатқа жібермеңіз.")
        model = self.preferences.get("elevenlabs_model", "eleven_v3")
        if model not in ELEVENLABS_MODELS:
            raise SpeechError("Бұл ElevenLabs моделі қолдау таппайды. Eleven v3 немесе Multilingual v2 таңдаңыз.")
        return provider, voice, model

    async def synthesize(self, text: str, language: str = "kk-KZ") -> bytes:
        if not isinstance(text, str) or not text.strip():
            raise SpeechError("Дыбысталатын мәтін бос болмауы керек.")
        if len(text) > MAX_TEXT:
            raise SpeechError("Дыбыстауға 2 000 таңбадан аспайтын мәтін жіберіңіз.")
        language = {"kk": "kk-KZ", "ru": "ru-RU"}.get(language, language)
        if language not in EDGE_VOICES:
            raise SpeechError("Қазір қазақ және орыс тілдеріндегі дыбыстау қолжетімді.")
        provider, voice, model = self._settings(language)
        text = text.strip()
        cache_key = (provider, voice, model, language, hashlib.sha256(text.encode()).hexdigest())
        async with self._lock:
            now = time.monotonic()
            for key, (created, _) in list(self._cache.items()):
                if now - created >= CACHE_TTL_SECONDS:
                    self._cache.pop(key)
            if cache_key in self._cache:
                self._cache.move_to_end(cache_key)
                return self._cache[cache_key][1]
            try:
                async with asyncio.timeout(30):
                    audio = (await self._edge(text, voice) if provider == "edge"
                             else await self._elevenlabs(text, voice, model, language))
                if not audio:
                    raise SpeechError("Дауыс сервері бос жауап берді. Қайта көріңіз.")
                if len(audio) > MAX_AUDIO_BYTES:
                    raise SpeechError("Дауыс жауабы тым үлкен. Мәтінді қысқартыңыз.")
            except SpeechError:
                raise
            except asyncio.TimeoutError as exc:
                raise SpeechError("Дауыс сервері уақытында жауап бермеді. Желі қосылымын тексеріңіз.") from exc
            except Exception as exc:
                raise SpeechError(
                    "Дыбысты алу мүмкін болмады. Дауыс провайдерінің желіге қолжетімділігін және баптауларды тексеріңіз."
                ) from exc
            self._cache[cache_key] = (time.monotonic(), audio)
            while len(self._cache) > CACHE_LIMIT:
                self._cache.popitem(last=False)
            return audio

    async def _edge(self, text: str, voice: str) -> bytes:
        try:
            import edge_tts
            import edge_tts.communicate as communicate_module
        except ImportError as exc:
            raise SpeechError("Қазақша дауысты қолдану үшін edge-tts тәуелділігін орнатыңыз.") from exc
        _edge_trust_context(communicate_module)
        proxy = (os.environ.get("HTTPS_PROXY") or os.environ.get("https_proxy")
                 or os.environ.get("HTTP_PROXY") or os.environ.get("http_proxy"))
        communicator = edge_tts.Communicate(
            text, voice=voice, proxy=proxy, connect_timeout=10, receive_timeout=20,
        )
        audio = bytearray()
        async for chunk in communicator.stream():
            if chunk.get("type") != "audio":
                continue
            data = chunk.get("data")
            if not isinstance(data, bytes):
                raise SpeechError("Дауыс серверінің жауабы дұрыс емес.")
            if len(audio) + len(data) > MAX_AUDIO_BYTES:
                raise SpeechError("Дауыс жауабы тым үлкен. Мәтінді қысқартыңыз.")
            audio.extend(data)
        return bytes(audio)

    @staticmethod
    def _check_elevenlabs_status(response: Any) -> None:
        if response.status_code in {401, 403}:
            raise SpeechError("ElevenLabs кілтіне немесе таңдалған дауысқа рұқсат жоқ. Баптауларды тексеріңіз.")
        if response.status_code == 429:
            raise SpeechError("ElevenLabs сұрау лимиті аяқталды. Кейінірек қайталаңыз.")
        response.raise_for_status()

    async def _require_model_language(
        self, client: httpx.AsyncClient, key: str, model: str, language: str,
    ) -> None:
        now = time.monotonic()
        for cached_key, (created, _) in list(self._model_cache.items()):
            if now - created >= CACHE_TTL_SECONDS:
                self._model_cache.pop(cached_key)
        key_digest = hashlib.sha256(key.encode()).hexdigest()
        cached = self._model_cache.get(key_digest)
        if cached:
            models = cached[1]
            self._model_cache.move_to_end(key_digest)
        else:
            async with client.stream(
                "GET", "https://api.elevenlabs.io/v1/models",
                headers={"xi-api-key": key, "Accept": "application/json"},
            ) as response:
                self._check_elevenlabs_status(response)
                metadata = bytearray()
                async for chunk in response.aiter_bytes():
                    if len(metadata) + len(chunk) > 1024 * 1024:
                        raise SpeechError("ElevenLabs модельдер тізімі тым үлкен.")
                    metadata.extend(chunk)
            try:
                records = json.loads(metadata)
                if not isinstance(records, list):
                    raise ValueError("Expected model records")
                models = {
                    record["model_id"]: frozenset(
                        language_record["language_id"] for language_record in record.get("languages", [])
                        if isinstance(language_record, dict) and isinstance(language_record.get("language_id"), str)
                    )
                    for record in records if isinstance(record, dict)
                    and isinstance(record.get("model_id"), str)
                    and record.get("can_do_text_to_speech") is True
                }
            except (ValueError, TypeError, KeyError) as exc:
                raise SpeechError("ElevenLabs модельдер тізімін тексеру мүмкін болмады.") from exc
            self._model_cache[key_digest] = (time.monotonic(), models)
            while len(self._model_cache) > 4:
                self._model_cache.popitem(last=False)
        if language.split("-", 1)[0] not in models.get(model, frozenset()):
            raise SpeechError(
                "ElevenLabs таңдалған модельдің осы тілді дыбыстай алатынын растаған жоқ. "
                "Қазақша жауап үшін Edge дауысын таңдаңыз."
            )

    async def _elevenlabs(self, text: str, voice: str, model: str, language: str) -> bytes:
        # Voice IDs are validated before reaching this method; the host is fixed.
        key = self.preferences.secret("elevenlabs_api_key")
        if not key:
            raise SpeechError("ElevenLabs API кілтін баптауларда сақтаңыз.")
        async with httpx.AsyncClient(
            timeout=25, verify=_verified_ssl_context(), follow_redirects=False,
        ) as client:
            await self._require_model_language(client, key, model, language)
            async with client.stream(
                "POST", f"https://api.elevenlabs.io/v1/text-to-speech/{voice}",
                params={"output_format": "mp3_44100_128"},
                headers={"xi-api-key": key, "Accept": "audio/mpeg"},
                json={"text": text, "model_id": model},
            ) as response:
                self._check_elevenlabs_status(response)
                if response.headers.get("content-type", "").split(";", 1)[0] not in {"audio/mpeg", "audio/mp3"}:
                    raise SpeechError("ElevenLabs аудионың орнына басқа жауап берді.")
                audio = bytearray()
                async for chunk in response.aiter_bytes():
                    if len(audio) + len(chunk) > MAX_AUDIO_BYTES:
                        raise SpeechError("Дауыс жауабы тым үлкен. Мәтінді қысқартыңыз.")
                    audio.extend(chunk)
                return bytes(audio)
