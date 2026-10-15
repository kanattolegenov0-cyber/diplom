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


class SettingsInput(BaseModel):
    llm_enabled: bool | None = None
    llm_model: str | None = Field(default=None, max_length=100)
    tts_provider: Literal["edge", "elevenlabs", "system"] | None = None
    tts_voice: Literal["kk-KZ-DauletNeural", "kk-KZ-AigulNeural"] | None = None
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


def create_app(data_dir: Path | None = None, browser=None, music=None, assistant=None, speech=None) -> FastAPI:
    browser = browser or BrowserService()
    music = music or MusicService()

    @asynccontextmanager
    async def lifespan(application):
        directory = data_dir or Path(os.environ.get("AYAN_DATA_DIR", "data"))
        application.state.store = Store(directory)
        application.state.preferences = Preferences(directory)
        application.state.assistant = assistant or AssistantService(application.state.preferences)
        application.state.speech = speech or SpeechService(application.state.preferences)
        yield
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
        return request.app.state.preferences.update(body.model_dump(exclude_none=True, exclude_unset=True))

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

    bundle_root = Path(getattr(sys, "_MEIPASS", Path(__file__).resolve().parent.parent))
    static = bundle_root / "app" / "static"

    @application.get("/")
    async def index():
        return FileResponse(static / "index.html")

    application.mount("/static", StaticFiles(directory=static), name="static")
    return application


app = create_app()
