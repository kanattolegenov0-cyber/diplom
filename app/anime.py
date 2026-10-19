"""Find AnimeGo series and verified episode links without fetching arbitrary hosts."""

from __future__ import annotations

import asyncio
from difflib import SequenceMatcher
from html.parser import HTMLParser
import re
from urllib.parse import quote, urljoin, urlsplit

import httpx


BASE = "https://animego.work"
GACHIAKUTA = f"{BASE}/370-gachiakuta-2025.html"
_SERIES_PATH = re.compile(r"^/\d+-[^/?#]+\.html$")
_NUMBER = re.compile(r"(?<!\d)(\d{1,3})(?:\s*[-–]?\s*(?:серия|серий|эпизод|episode|ep\.?|сер\.?))(?!\w)|(?:серия|серий|эпизод|episode|ep\.?|сер\.?)[\s№#.:]*(\d{1,3})(?!\d)", re.I)
_SEASON = re.compile(r"(?:сезон|season|маусым)[\s№#.:]*(\d{1,2})", re.I)


class AnimeError(Exception):
    pass


def safe_anime_url(url: str) -> str | None:
    try:
        parts = urlsplit(urljoin(BASE + "/", url))
    except ValueError:
        return None
    if parts.scheme != "https" or parts.hostname != "animego.work" or parts.port is not None or parts.username or parts.password:
        return None
    return parts.geturl()


def _clean_title(value: str) -> str:
    value = re.sub(r"\s+", " ", value).strip()
    return re.sub(r"\s*\(\d{4}\)\s*$", "", value).strip()


def _score(query: str, title: str) -> float:
    query, title = query.casefold().strip(), title.casefold().strip()
    if not query or not title:
        return 0
    if query in title or title in query:
        return 1.0
    return SequenceMatcher(None, query, title).ratio()


class _Links(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.links: list[tuple[str, str]] = []
        self._href: str | None = None
        self._text: list[str] = []

    def handle_starttag(self, tag, attrs):
        if tag == "a":
            attrs = dict(attrs)
            self._href = attrs.get("href")
            self._text = [attrs.get("title", ""), attrs.get("aria-label", "")]

    def handle_data(self, data):
        if self._href is not None:
            self._text.append(data)

    def handle_endtag(self, tag):
        if tag == "a" and self._href is not None:
            self.links.append((self._href, _clean_title(" ".join(self._text))))
            self._href = None
            self._text = []


class AnimeService:
    def __init__(self, client: httpx.AsyncClient | None = None):
        self.client = client
        self.catalog = {GACHIAKUTA: "Гачиакута / Gachiakuta"}

    async def _get(self, url: str) -> str:
        safe = safe_anime_url(url)
        if not safe:
            raise AnimeError("Аниме сайтының мекенжайы жарамсыз.")
        async def fetch(client):
            async with client.stream("GET", safe, follow_redirects=False) as response:
                if response.status_code != 200:
                    raise AnimeError("AnimeGo сайты жауап бермеді. Кейін қайталап көріңіз.")
                content = bytearray()
                async for chunk in response.aiter_bytes():
                    content.extend(chunk)
                    if len(content) > 1_000_000:
                        raise AnimeError("AnimeGo беті тым үлкен.")
                return content.decode(response.encoding or "utf-8", errors="replace")
        try:
            if self.client:
                return await asyncio.wait_for(fetch(self.client), 12)
            async with httpx.AsyncClient(timeout=10, headers={"User-Agent": "AYAN/1.0"}) as client:
                return await asyncio.wait_for(fetch(client), 12)
        except (httpx.HTTPError, asyncio.TimeoutError) as exc:
            raise AnimeError("AnimeGo сайтына қосылу мүмкін болмады. Интернетті тексеріңіз.") from exc

    async def search(self, query: str) -> dict:
        query = query.strip()
        if not query:
            raise AnimeError("Аниме атауын жазыңыз.")
        seeds = [{"title": "Гачиакута / Gachiakuta", "url": GACHIAKUTA, "source": "saved"}]
        if safe_anime_url(query) == GACHIAKUTA or _score(query, "гачиакута") >= .55 or _score(query, "gachiakuta") >= .55:
            return {"results": seeds}
        cached = [{"title": title, "url": url, "source": "saved"} for url, title in self.catalog.items()
                  if _score(query, title) >= .65]

        async def site_results(term):
            search_url = f"{BASE}/index.php?do=search&subaction=search&story={quote(term)}"
            page = await self._get(search_url)
            parser = _Links()
            parser.feed(page)
            found = []
            seen = set()
            for href, title in parser.links:
                url = safe_anime_url(href)
                if not url or not _SERIES_PATH.fullmatch(urlsplit(url).path) or url in seen:
                    continue
                seen.add(url)
                if not title:
                    title = urlsplit(url).path.rsplit("/", 1)[-1].split("-", 1)[-1].removesuffix(".html").replace("-", " ")
                found.append({"title": title[:200], "url": url, "source": "animego"})
                self.catalog[url] = title[:200]
            return found

        try:
            results = await site_results(query)
            if not results and len(query) >= 6:
                results = await site_results(query[:-2])
        except AnimeError:
            if not cached:
                raise
            results = []
        results.extend(item for item in cached if item["url"] not in {found["url"] for found in results})
        results.sort(key=lambda item: _score(query, item["title"]), reverse=True)
        return {"results": results[:15]}

    async def episodes(self, url: str) -> dict:
        safe = safe_anime_url(url)
        if not safe or not _SERIES_PATH.fullmatch(urlsplit(safe).path):
            raise AnimeError("Аниме бетінің сілтемесі жарамсыз.")
        page = await self._get(safe)
        parser = _Links()
        parser.feed(page)
        episodes = []
        seen = set()
        for href, title in parser.links:
            match = _NUMBER.search(title)
            link = safe_anime_url(href)
            if not match or not link or href.strip().startswith("#") or link == safe or link in seen:
                continue
            episode = int(match.group(1) or match.group(2))
            season_match = _SEASON.search(title)
            season = int(season_match.group(1)) if season_match else 1
            if not 1 <= episode <= 999 or not 1 <= season <= 99:
                continue
            seen.add(link)
            episodes.append({"season": season, "episode": episode, "title": title[:200], "url": link})
        episodes.sort(key=lambda item: (item["season"], item["episode"]))
        return {"url": safe, "episodes": episodes[:500]}
