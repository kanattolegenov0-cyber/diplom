"""Search public YouTube metadata; playback uses YouTube's own embedded player."""
import asyncio
from collections import OrderedDict
import re
import time
from urllib.parse import parse_qs, urlsplit
from yt_dlp import YoutubeDL


class MusicError(Exception):
    pass


def video_id(text: str) -> str | None:
    if re.fullmatch(r"[A-Za-z0-9_-]{11}", text):
        return text
    parsed = urlsplit(text)
    if parsed.hostname in {"youtu.be", "www.youtu.be"}:
        candidate = parsed.path.strip("/")
    elif parsed.hostname in {"youtube.com", "www.youtube.com", "m.youtube.com", "www.youtube-nocookie.com"}:
        candidate = parse_qs(parsed.query).get("v", [""])[0]
        if parsed.path.startswith(("/embed/", "/shorts/")):
            candidate = parsed.path.split("/")[2]
    else:
        return None
    return candidate if re.fullmatch(r"[A-Za-z0-9_-]{11}", candidate) else None


class QuietLogger:
    def debug(self, message):
        pass

    def warning(self, message):
        pass

    def error(self, message):
        pass


class MusicService:
    def __init__(self):
        self.cache = OrderedDict()
        self.lock = asyncio.Lock()

    async def search(self, query: str) -> dict:
        query = query.strip()
        direct_id = video_id(query)
        if direct_id:
            return {"tracks": [{"id": direct_id, "title": "YouTube бейнесі", "channel": "YouTube",
                                "thumbnail": f"https://i.ytimg.com/vi/{direct_id}/mqdefault.jpg"}], "source": "youtube"}
        async with self.lock:
            cached = self.cache.get(query.casefold())
            if cached and time.monotonic() - cached[0] < 300:
                return cached[1]
            try:
                result = await asyncio.wait_for(asyncio.to_thread(self._search, query), timeout=25)
            except (Exception, asyncio.TimeoutError) as exc:
                raise MusicError("YouTube іздеуі қазір қолжетімсіз. Интернетті тексеріңіз немесе бейненің сілтемесін енгізіңіз.") from exc
            self.cache[query.casefold()] = (time.monotonic(), result)
            if len(self.cache) > 64:
                self.cache.popitem(last=False)
            return result

    def _search(self, query: str) -> dict:
        options = {"quiet": True, "no_warnings": True, "logger": QuietLogger(),
                   "extract_flat": True, "skip_download": True, "socket_timeout": 8,
                   "retries": 0, "extractor_retries": 0, "playlistend": 5,
                   "nocheckcertificate": False}
        with YoutubeDL(options) as youtube:
            info = youtube.extract_info(f"ytsearch5:{query}", download=False)
        tracks = []
        for entry in (info or {}).get("entries", []):
            if not entry or not re.fullmatch(r"[A-Za-z0-9_-]{11}", entry.get("id", "")):
                continue
            identifier = entry["id"]
            tracks.append({"id": identifier, "title": entry.get("title") or "YouTube",
                           "channel": entry.get("channel") or entry.get("uploader") or "YouTube",
                           "thumbnail": f"https://i.ytimg.com/vi/{identifier}/mqdefault.jpg"})
        if not tracks:
            raise MusicError("Бұл сұрау бойынша бейне табылмады.")
        return {"tracks": tracks, "source": "youtube"}
