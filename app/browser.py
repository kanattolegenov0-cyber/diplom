"""A small, single-user browser with public-network navigation guards."""

from __future__ import annotations

import asyncio
import base64
import ipaddress
import os
import shutil
import socket
import sys
from pathlib import Path
from typing import Any
from urllib.parse import unquote, urljoin, urlsplit, urlunsplit


class BrowserError(Exception):
    """An actionable browser error that can be shown to the user."""


def _is_public_address(address: str) -> bool:
    try:
        ip = ipaddress.ip_address(address)
    except ValueError:
        return False
    return ip.is_global and not ip.is_multicast and not ip.is_reserved


def validate_url_structure(url: str) -> str:
    """Validate and normalize an HTTP(S) URL without performing network access."""
    if not isinstance(url, str) or not url.strip() or len(url) > 4096:
        raise BrowserError("Сайттың дұрыс http:// немесе https:// мекенжайын көрсетіңіз.")
    url = url.strip()
    if any(character.isspace() or ord(character) < 32 for character in url) or "\\" in url:
        raise BrowserError("Сайт мекенжайында рұқсат етілмеген таңбалар бар.")
    try:
        parsed = urlsplit(url)
        hostname = parsed.hostname
        port = parsed.port
    except ValueError as exc:
        raise BrowserError("Сайт мекенжайы дұрыс емес.") from exc
    if parsed.scheme.lower() not in {"http", "https"} or not hostname:
        raise BrowserError("Тек ашық http:// және https:// сайттарын ашуға болады.")
    if parsed.username is not None or parsed.password is not None:
        raise BrowserError("Мекенжайдың ішінде логин немесе құпиясөз болмауы керек.")
    hostname = hostname.lower().rstrip(".")
    if not hostname or "%" in hostname or hostname == "localhost" or hostname.endswith(
        (".localhost", ".local", ".internal")
    ):
        raise BrowserError("Жергілікті және ішкі желі мекенжайларын ашуға болмайды.")
    try:
        address = ipaddress.ip_address(hostname)
    except ValueError:
        try:
            hostname = hostname.encode("idna").decode("ascii")
        except UnicodeError as exc:
            raise BrowserError("Сайт домені дұрыс емес.") from exc
        if len(hostname) > 253 or any(
            not label or len(label) > 63 or label.startswith("-") or label.endswith("-")
            or any(not (character.isascii() and (character.isalnum() or character == "-"))
                   for character in label)
            for label in hostname.split(".")
        ):
            raise BrowserError("Сайт домені дұрыс емес.")
        host = hostname
    else:
        if not _is_public_address(str(address)):
            raise BrowserError("Жергілікті, жеке және резервтік IP мекенжайларын ашуға болмайды.")
        host = f"[{address}]" if address.version == 6 else str(address)
    if port is not None and port == 0:
        raise BrowserError("Сайт порты дұрыс емес.")
    netloc = host + (f":{port}" if port is not None else "")
    return urlunsplit((parsed.scheme.lower(), netloc, parsed.path or "/", parsed.query, parsed.fragment))


async def _resolve_public_destination(url: str) -> tuple[str, list[str]]:
    normalized = validate_url_structure(url)
    parsed = urlsplit(normalized)
    hostname = parsed.hostname
    assert hostname is not None
    try:
        ipaddress.ip_address(hostname)
    except ValueError:
        if os.environ.get("AYAN_PROXY_DNS") == "1":
            if not _environment_proxy():
                raise BrowserError(
                    "AYAN_PROXY_DNS=1 үшін ортада сенімді HTTPS_PROXY немесе HTTP_PROXY қажет."
                )
            # Explicit cloud mode: the configured, trusted egress proxy resolves
            # hostnames and owns destination-network policy. We cannot verify
            # its DNS answers locally; URL/literal-IP guards still apply.
            return normalized, []
        port = parsed.port or (443 if parsed.scheme == "https" else 80)
        try:
            answers = await asyncio.wait_for(
                asyncio.to_thread(socket.getaddrinfo, hostname, port, type=socket.SOCK_STREAM),
                timeout=5,
            )
        except (OSError, asyncio.TimeoutError) as exc:
            raise BrowserError("Сайттың доменін табу мүмкін болмады. Мекенжайды тексеріңіз.") from exc
        if not answers or any(not _is_public_address(answer[4][0]) for answer in answers):
            raise BrowserError("Бұл сайт ішкі немесе рұқсат етілмеген желіге бағыттайды.")
        return normalized, list(dict.fromkeys(answer[4][0] for answer in answers))
    return normalized, [hostname]


async def validate_public_url(url: str) -> str:
    """Validate public browsing destinations.

    By default all locally resolved IPs must be public. AYAN_PROXY_DNS=1 is an
    explicit cloud opt-in that delegates hostname resolution and destination
    policy to an existing trusted HTTP(S) proxy. That mode cannot inspect the
    proxy's DNS answers, but still rejects unsafe URL structures and IP literals.
    """
    normalized, _ = await _resolve_public_destination(url)
    return normalized


def _environment_proxy() -> dict[str, str] | None:
    """Reuse the environment's egress proxy without displaying credentials."""
    value = (os.environ.get("HTTPS_PROXY") or os.environ.get("https_proxy")
             or os.environ.get("HTTP_PROXY") or os.environ.get("http_proxy"))
    if not value:
        return None
    try:
        parsed = urlsplit(value if "://" in value else f"http://{value}")
        hostname, port = parsed.hostname, parsed.port
    except ValueError as exc:
        raise BrowserError("Ортадағы браузер проксиінің параметрлері дұрыс емес.") from exc
    if parsed.scheme not in {"http", "https", "socks5"} or not hostname:
        raise BrowserError("Ортадағы браузер проксиінің параметрлері дұрыс емес.")
    host = f"[{hostname}]" if ":" in hostname else hostname
    proxy = {"server": f"{parsed.scheme}://{host}" + (f":{port}" if port else "")}
    if parsed.username is not None:
        proxy["username"] = unquote(parsed.username)
    if parsed.password is not None:
        proxy["password"] = unquote(parsed.password)
    bypass = os.environ.get("NO_PROXY") or os.environ.get("no_proxy")
    if bypass:
        proxy["bypass"] = bypass
    return proxy


def _chromium_executable(playwright: Any) -> str | None:
    override = os.environ.get("AYAN_CHROMIUM_EXECUTABLE")
    candidates = [override] if override else [playwright.chromium.executable_path]
    if not override and sys.platform.startswith("linux"):
        candidates.append(shutil.which("chromium"))
    return next((str(path) for path in candidates if path and Path(path).is_file()
                 and os.access(path, os.X_OK)), None)


class BrowserService:
    """Keep one browser page alive; serialize actions from the assistant UI."""

    def __init__(self) -> None:
        self._lock = asyncio.Lock()
        self._playwright: Any = None
        self._browser: Any = None
        self._context: Any = None
        self._page: Any = None
        self._blocked_navigation: str | None = None

    async def available(self) -> bool:
        if self._browser is not None:
            return self._browser.is_connected()
        try:
            from playwright.async_api import async_playwright

            async with async_playwright() as playwright:
                return _chromium_executable(playwright) is not None
        except (ImportError, OSError, RuntimeError):
            return False

    async def _guard_request(self, route: Any) -> None:
        try:
            await validate_public_url(route.request.url)
        except BrowserError as exc:
            if route.request.is_navigation_request():
                self._blocked_navigation = str(exc)
            await route.abort("blockedbyclient")
        else:
            await route.continue_()

    async def _guard_frame(self, frame: Any) -> None:
        if self._page is None or frame != self._page.main_frame or frame.url == "about:blank":
            return
        try:
            await validate_public_url(frame.url)
        except BrowserError as exc:
            self._blocked_navigation = str(exc)
            try:
                await self._page.goto("about:blank")
            except Exception:
                pass

    async def _ensure_started(self) -> None:
        if self._browser is not None and self._browser.is_connected():
            return
        await self._close_unlocked()
        try:
            from playwright.async_api import async_playwright

            self._playwright = await async_playwright().start()
            executable = _chromium_executable(self._playwright)
            if executable is None:
                raise BrowserError("Браузер орнатылмаған. Playwright Chromium орнату қажет.")
            launch_options: dict[str, Any] = {
                "headless": True, "timeout": 12000, "executable_path": executable,
            }
            proxy = _environment_proxy()
            if proxy:
                launch_options["proxy"] = proxy
            self._browser = await self._playwright.chromium.launch(**launch_options)
            self._context = await self._browser.new_context(
                viewport={"width": 1280, "height": 800},
                accept_downloads=False,
                service_workers="block",
                ignore_https_errors=False,
            )
            await self._context.route("**/*", self._guard_request)

            async def block_websocket(route: Any) -> None:
                await route.close()

            await self._context.route_web_socket("**/*", block_websocket)
            self._page = await self._context.new_page()
            self._page.set_default_timeout(4000)
            self._page.set_default_navigation_timeout(12000)
            self._page.on("framenavigated", self._guard_frame)

            async def close_popup(page: Any) -> None:
                if page is not self._page:
                    await page.close()

            self._context.on("page", close_popup)

            async def cancel_download(download: Any) -> None:
                await download.cancel()

            self._page.on("download", cancel_download)
        except BaseException:
            await self._close_unlocked()
            raise

    async def _find_target(self, target: str, *, fill: bool = False) -> Any:
        if not target or len(target) > 512:
            raise BrowserError("Батырманың не өрістің сайттағы нақты атауын көрсетіңіз.")
        candidates = [
            self._page.get_by_label(target, exact=True),
            self._page.get_by_placeholder(target, exact=True),
        ]
        if not fill:
            candidates.extend([
                self._page.get_by_role("button", name=target, exact=True),
                self._page.get_by_role("link", name=target, exact=True),
            ])
        for candidate in candidates:
            for index in range(await candidate.count()):
                locator = candidate.nth(index)
                if await locator.is_visible():
                    return locator
        raise BrowserError(f'Сайттан «{target}» табылмады. Беттегі нақты атауын қолданыңыз.')

    async def execute(
        self,
        action: str,
        url: str | None = None,
        target: str | None = None,
        value: str | None = None,
        direction: str = "down",
    ) -> dict[str, str]:
        if action not in {"open", "click", "fill", "scroll", "back"}:
            raise BrowserError("Бұл браузер әрекеті қолдау таппайды.")
        async with self._lock:
            self._blocked_navigation = None
            try:
                async with asyncio.timeout(15):
                    normalized = await validate_public_url(url or "") if action == "open" else None
                    await self._ensure_started()
                    if action != "open" and self._page.url == "about:blank":
                        raise BrowserError("Алдымен сайтты ашыңыз.")
                    if action == "open":
                        await self._page.goto(normalized, wait_until="domcontentloaded")
                        message = "Сайт ашылды."
                    elif action == "click":
                        locator = await self._find_target(target or "")
                        href = await locator.get_attribute("href")
                        if href:
                            await validate_public_url(urljoin(self._page.url, href))
                        await locator.click()
                        message = f'«{target}» басылды.'
                    elif action == "fill":
                        if value is None or len(value) > 10000:
                            raise BrowserError("Өріске жазылатын мәтінді көрсетіңіз (10 000 таңбаға дейін).")
                        locator = await self._find_target(target or "", fill=True)
                        await locator.fill(value)
                        message = f'«{target}» өрісі толтырылды.'
                    elif action == "scroll":
                        if direction not in {"up", "down"}:
                            raise BrowserError("Жылжыту бағыты up немесе down болуы керек.")
                        await self._page.mouse.wheel(0, -650 if direction == "up" else 650)
                        await self._page.wait_for_timeout(150)
                        message = "Бет жылжытылды."
                    else:
                        previous_url = self._page.url
                        await self._page.go_back(wait_until="domcontentloaded")
                        message = "Алдыңғы бет ашылды." if self._page.url != previous_url else "Алдыңғы бет жоқ."

                    if self._page.url == "about:blank" and action == "back":
                        raise BrowserError("Алдыңғы ашық сайт жоқ. Сайтты қайта ашыңыз.")
                    try:
                        await validate_public_url(self._page.url)
                    except BrowserError:
                        await self._page.goto("about:blank")
                        raise
                    screenshot = await self._page.screenshot(type="png", timeout=3000)
                    if self._blocked_navigation:
                        raise BrowserError(self._blocked_navigation)
                    return {
                        "url": self._page.url,
                        "title": await self._page.title(),
                        "screenshot": base64.b64encode(screenshot).decode("ascii"),
                        "message": message,
                    }
            except BrowserError:
                raise
            except asyncio.TimeoutError as exc:
                raise BrowserError("Браузер әрекеті тым ұзақ орындалды. Қайта көріңіз.") from exc
            except Exception as exc:
                if self._blocked_navigation:
                    raise BrowserError(self._blocked_navigation) from exc
                raise BrowserError("Сайт әрекетін орындау мүмкін болмады. Мекенжайды және беттегі атауды тексеріңіз.") from exc

    async def _close_unlocked(self) -> None:
        for attribute in ("_context", "_browser", "_playwright"):
            resource = getattr(self, attribute)
            setattr(self, attribute, None)
            if resource is not None:
                try:
                    await (resource.stop() if attribute == "_playwright" else resource.close())
                except Exception:
                    pass
        self._page = None

    async def close(self) -> None:
        async with self._lock:
            await self._close_unlocked()
