from contextlib import asynccontextmanager
from datetime import datetime
from pathlib import Path
from typing import Literal
from urllib.parse import urlsplit
import asyncio
import os
import sys

from fastapi import FastAPI, HTTPException, Query, Request
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field
from starlette.middleware.trustedhost import TrustedHostMiddleware

from .browser import BrowserError, BrowserService
from .commands import parse_command
from .music import MusicError, MusicService
from .storage import Store
from . import voice


class CommandInput(BaseModel):
    text: str = Field(min_length=1, max_length=10000)
    volume: int = Field(default=60, ge=0, le=100)


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


def create_app(data_dir: Path | None = None, browser=None, music=None) -> FastAPI:
    browser = browser or BrowserService()
    music = music or MusicService()

    @asynccontextmanager
    async def lifespan(application):
        application.state.store = Store(data_dir or Path(os.environ.get("AYAN_DATA_DIR", "data")))
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

    @application.get("/api/health")
    async def health():
        return {"status": "ok", "name": "AYAN", "browser_available": await browser.available(),
                "voice_client_side": True, "desktop_mode": os.environ.get("AYAN_DESKTOP_MODE") == "1",
                "native_voice_available": voice.available()}

    @application.post("/api/command")
    async def command(body: CommandInput, request: Request):
        result = parse_command(body.text, body.volume)
        store = request.app.state.store
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

    bundle_root = Path(getattr(sys, "_MEIPASS", Path(__file__).resolve().parent.parent))
    static = bundle_root / "app" / "static"

    @application.get("/")
    async def index():
        return FileResponse(static / "index.html")

    application.mount("/static", StaticFiles(directory=static), name="static")
    return application


app = create_app()
