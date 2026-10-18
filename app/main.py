from contextlib import asynccontextmanager
from datetime import datetime
from pathlib import Path
from typing import Literal
from urllib.parse import urlsplit
import asyncio
import os
import sys

from fastapi import FastAPI, HTTPException, Query, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import FileResponse, JSONResponse, Response
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field
from starlette.middleware.trustedhost import TrustedHostMiddleware

from .browser import BrowserError, BrowserService
from .assistant import AssistantService, validate_action
from .music import MusicError, MusicService
from .preferences import Preferences
from .speech import SpeechError, SpeechService
from .storage import Store
from .wake import WakeService
from .wake_model import ModelError, ModelManager
from . import voice


class CommandInput(BaseModel):
    text: str = Field(min_length=1, max_length=10000)
    volume: int = Field(default=60, ge=0, le=100)
    language: Literal["kk-KZ", "ru-RU"] = "kk-KZ"
    history: list[dict] = Field(default_factory=list, max_length=8)


class EntryInput(BaseModel):
    content: str = Field(min_length=1, max_length=10000)


class ReminderInput(BaseModel):
    title: str = Field(min_length=1, max_length=500)
    delay_seconds: int = Field(ge=1, le=2592000)


class BrowserInput(BaseModel):
    action: Literal["open", "click", "fill", "scroll", "back"]
    url: str | None = Field(default=None, max_length=2048)
    target: str | None = Field(default=None, max_length=500)
    value: str | None = Field(default=None, max_length=10000)
    direction: Literal["up", "down"] = "down"
    confirmed: bool = False


class VoiceInput(BaseModel):
    language: Literal["kk-KZ", "ru-RU"] = "kk-KZ"


class SpeechInput(BaseModel):
    text: str = Field(min_length=1, max_length=2000)
    language: Literal["kk-KZ", "ru-RU"] = "kk-KZ"


class ListenInput(BaseModel):
    video_id: str = Field(pattern=r"^[A-Za-z0-9_-]{11}$")
    title: str = Field(min_length=1, max_length=300)
    channel: str = Field(default="", max_length=200)
    session_id: str = Field(pattern=r"^[A-Za-z0-9._-]{8,100}$")
    listened_seconds: float = Field(strict=True, gt=0, le=86400, allow_inf_nan=False)


class WakeActivityInput(BaseModel):
    busy: bool = Field(strict=True)


class SettingsInput(BaseModel):
    llm_enabled: bool | None = None
    llm_model: str | None = Field(default=None, max_length=100)
    tts_provider: Literal["edge", "elevenlabs", "system"] | None = None
    tts_voice: Literal["kk-KZ-DauletNeural", "kk-KZ-AigulNeural"] | None = None
    speech_style: Literal["serious", "natural"] | None = None
    speech_rate: int | None = Field(default=None, strict=True, ge=-30, le=20)
    speech_pitch: int | None = Field(default=None, strict=True, ge=-20, le=10)
    wake_enabled: bool | None = Field(default=None, strict=True)
    wake_language: Literal["kk-KZ", "ru-RU"] | None = None
    elevenlabs_voice_id: str | None = Field(default=None, max_length=64)
    elevenlabs_model: Literal["eleven_v3", "eleven_multilingual_v2"] | None = None
    llm_api_key: str | None = Field(default=None, max_length=4096, repr=False)
    elevenlabs_api_key: str | None = Field(default=None, max_length=4096, repr=False)
    clear_llm_api_key: bool = False
    clear_elevenlabs_api_key: bool = False


class AssistantActionInput(BaseModel):
    intent: Literal["diary_add", "diary_list", "reminder_add", "reminders_list", "time"]
    payload: dict = Field(default_factory=dict)


def execute_local_action(result: dict, store: Store) -> dict:
    intent, payload = result["intent"], result["payload"]
    if intent == "diary_add":
        result["data"] = {"entry": store.add_entry(payload["content"])}
    elif intent == "diary_list":
        result["data"] = {"entries": store.entries()}
    elif intent == "reminder_add":
        result["data"] = {"reminder": store.add_reminder(payload["title"], payload["delay_seconds"])}
    elif intent == "reminders_list":
        result["data"] = {"reminders": store.reminders()}
    elif intent == "time":
        result["payload"] = {"current_time": datetime.now().astimezone().isoformat()}
    return result


def create_app(data_dir: Path | None = None, browser=None, music=None, assistant=None,
               speech=None, wake=None, wake_model=None) -> FastAPI:
    browser = browser or BrowserService()
    music = music or MusicService()

    @asynccontextmanager
    async def lifespan(application):
        directory = data_dir or Path(os.environ.get("AYAN_DATA_DIR", "data"))
        application.state.store = Store(directory)
        application.state.preferences = Preferences(directory)
        application.state.assistant = assistant or AssistantService(application.state.preferences)
        application.state.speech = speech or SpeechService(application.state.preferences)
        application.state.wake_model = wake_model or ModelManager(directory)
        application.state.wake = wake or WakeService(
            application.state.preferences, voice, application.state.wake_model)
        try:
            if os.environ.get("AYAN_DESKTOP_MODE") == "1" and application.state.preferences.get("wake_enabled"):
                await asyncio.to_thread(application.state.wake.start,
                                        application.state.preferences.get("wake_language"))
            yield
        finally:
            await asyncio.to_thread(application.state.wake.close)
            await browser.close()

    application = FastAPI(title="AYAN — жеке көмекші", version="0.1.0", lifespan=lifespan)
    application.add_middleware(TrustedHostMiddleware, allowed_hosts=["localhost", "127.0.0.1", "[::1]", "testserver"])

    @application.middleware("http")
    async def local_requests(request: Request, call_next):
        origin = request.headers.get("origin")
        if origin and origin != str(request.base_url).rstrip("/"):
            return JSONResponse({"detail": "Басқа сайттан басқаруға рұқсат жоқ."}, status_code=403)
        response = await call_next(request)
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["X-Frame-Options"] = "DENY"
        response.headers["Referrer-Policy"] = "strict-origin-when-cross-origin"
        return response

    @application.exception_handler(ValueError)
    async def value_error(request, exc):
        return JSONResponse({"detail": str(exc)}, status_code=422)

    @application.exception_handler(RequestValidationError)
    async def validation_error(request, exc):
        # Never echo secret input in settings validation errors.
        if request.url.path == "/api/settings":
            return JSONResponse({"detail": "Баптау мәндері дұрыс емес. Модель мен дауыс баптауын тексеріңіз."}, status_code=422)
        return JSONResponse({"detail": "Сұрау мәндері дұрыс емес."}, status_code=422)

    @application.get("/api/health")
    async def health():
        return {"status": "ok", "name": "AYAN", "browser_available": await browser.available(),
                "voice_client_side": True, "desktop_mode": os.environ.get("AYAN_DESKTOP_MODE") == "1",
                "native_voice_available": voice.available()}

    @application.post("/api/command")
    async def command(body: CommandInput, request: Request):
        now = datetime.now().astimezone()
        context = {"current_time": now.isoformat(), "timezone": str(now.tzinfo)}
        result = await request.app.state.assistant.resolve(
            body.text, body.volume, body.language, body.history, context)
        return execute_local_action(result, request.app.state.store)

    @application.post("/api/assistant/action")
    async def assistant_action(body: AssistantActionInput, request: Request):
        action = validate_action({"intent": body.intent, "payload": body.payload})
        messages = {"diary_add": "Күнделікке жазылды.", "diary_list": "Күнделік жазбалары.",
                    "reminder_add": "Еске салу қосылды.", "reminders_list": "Еске салулар тізімі.",
                    "time": "Ағымдағы уақыт."}
        return execute_local_action({**action, "message": messages[body.intent]}, request.app.state.store)

    @application.get("/api/settings")
    async def settings(request: Request):
        return request.app.state.preferences.public()

    @application.post("/api/settings")
    async def update_settings(body: SettingsInput, request: Request):
        updates = body.model_dump(exclude_none=True, exclude_unset=True)
        result = request.app.state.preferences.update(updates)
        if os.environ.get("AYAN_DESKTOP_MODE") == "1" and {"wake_enabled", "wake_language"} & updates.keys():
            service = request.app.state.wake
            if not result["wake_enabled"]:
                await asyncio.to_thread(service.stop)
            else:
                if "wake_language" in updates:
                    await asyncio.to_thread(service.stop)
                await asyncio.to_thread(service.start, result["wake_language"])
        return result

    @application.post("/api/speech")
    async def speech_response(body: SpeechInput, request: Request):
        try:
            audio = await request.app.state.speech.synthesize(body.text, body.language)
            return Response(audio, media_type="audio/mpeg", headers={"Cache-Control": "no-store"})
        except SpeechError as exc:
            raise HTTPException(502, str(exc)) from exc

    @application.get("/api/diary")
    async def diary(request: Request):
        return {"entries": request.app.state.store.entries()}

    @application.post("/api/diary", status_code=201)
    async def add_diary(body: EntryInput, request: Request):
        return request.app.state.store.add_entry(body.content)

    @application.delete("/api/diary/{entry_id}")
    async def delete_diary(entry_id: int, request: Request):
        if not request.app.state.store.delete_entry(entry_id):
            raise HTTPException(404, "Жазба табылмады.")
        return {"ok": True}

    @application.get("/api/reminders")
    async def reminders(request: Request):
        return {"reminders": request.app.state.store.reminders()}

    @application.post("/api/reminders", status_code=201)
    async def add_reminder(body: ReminderInput, request: Request):
        return request.app.state.store.add_reminder(body.title, body.delay_seconds)

    @application.post("/api/reminders/{reminder_id}/complete")
    async def complete(reminder_id: int, request: Request):
        if not request.app.state.store.complete_reminder(reminder_id):
            raise HTTPException(404, "Еске салу табылмады.")
        return {"ok": True}

    @application.get("/api/music/search")
    async def music_search(q: str = Query(min_length=1, max_length=300)):
        if not q.strip():
            raise HTTPException(422, "Ән атауын енгізіңіз.")
        try:
            return await music.search(q)
        except MusicError as exc:
            raise HTTPException(502, str(exc)) from exc

    @application.get("/api/music/playlist")
    async def music_playlist(request: Request):
        return {"tracks": request.app.state.store.frequent_tracks()}

    @application.post("/api/music/listens")
    async def music_listen(body: ListenInput, request: Request):
        return request.app.state.store.record_listen(**body.model_dump())

    @application.post("/api/browser/action")
    async def browser_action(body: BrowserInput):
        if body.action == "click" and not body.confirmed:
            raise HTTPException(409, "Батырманы басуды алдымен растаңыз.")
        try:
            return await browser.execute(body.action, body.url, body.target, body.value, body.direction)
        except BrowserError as exc:
            raise HTTPException(400, str(exc)) from exc

    @application.post("/api/voice/listen")
    async def voice_listen(body: VoiceInput):
        if os.environ.get("AYAN_DESKTOP_MODE") != "1":
            raise HTTPException(409, "Компьютер микрофоны тек desktop режимінде іске қосылады.")
        try:
            return {"text": await asyncio.to_thread(voice.listen, body.language)}
        except voice.VoiceError as exc:
            raise HTTPException(503, str(exc)) from exc

    @application.get("/api/voice/status")
    async def voice_status():
        if os.environ.get("AYAN_DESKTOP_MODE") != "1":
            raise HTTPException(409, "Микрофон күйі тек desktop режимінде қолжетімді.")
        return voice.get_status()

    def require_desktop():
        if os.environ.get("AYAN_DESKTOP_MODE") != "1":
            raise HTTPException(409, "«Аян» деп шақыру режимі Windows бағдарламасында қолжетімді.")

    def wake_status_payload(request: Request, since: int = 0):
        return {**request.app.state.wake.status(since),
                "voice_phase": voice.get_status()["phase"],
                "model": request.app.state.wake_model.info()}

    @application.get("/api/wake/status")
    async def wake_status(request: Request, since: int = Query(default=0, ge=0)):
        require_desktop()
        return wake_status_payload(request, since)

    @application.post("/api/wake/start")
    async def wake_start(body: VoiceInput, request: Request):
        require_desktop()
        await asyncio.to_thread(request.app.state.wake.start, body.language)
        return wake_status_payload(request)

    @application.post("/api/wake/stop")
    async def wake_stop(request: Request):
        require_desktop()
        await asyncio.to_thread(request.app.state.wake.stop)
        return wake_status_payload(request)

    @application.post("/api/wake/activity")
    async def wake_activity(body: WakeActivityInput, request: Request):
        require_desktop()
        await asyncio.to_thread(request.app.state.wake.set_busy, body.busy)
        return wake_status_payload(request)

    @application.post("/api/wake/model/install")
    async def wake_model_install(request: Request):
        require_desktop()
        try:
            await asyncio.to_thread(request.app.state.wake_model.install)
        except ModelError as exc:
            raise HTTPException(502, str(exc)) from exc
        if request.app.state.preferences.get("wake_enabled"):
            await asyncio.to_thread(request.app.state.wake.start,
                                    request.app.state.preferences.get("wake_language"))
        return wake_status_payload(request)

    bundle_root = Path(getattr(sys, "_MEIPASS", Path(__file__).resolve().parent.parent))
    static = bundle_root / "app" / "static"

    @application.get("/")
    async def index():
        return FileResponse(static / "index.html")

    application.mount("/static", StaticFiles(directory=static), name="static")
    return application


app = create_app()
