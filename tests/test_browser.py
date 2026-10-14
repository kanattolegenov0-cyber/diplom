"""Browser policy and action tests; no websites or external DNS are contacted."""

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from app.browser import (
    BrowserError, BrowserService, _chromium_executable, _environment_proxy,
    validate_public_url, validate_url_structure,
)


@pytest.fixture(autouse=True)
def default_local_dns_mode(monkeypatch):
    monkeypatch.delenv("AYAN_PROXY_DNS", raising=False)


@pytest.mark.parametrize("url", [
    "file:///etc/passwd", "javascript:alert(1)", "data:text/html,hello", "ftp://example.com",
    "http://localhost", "http://foo.localhost", "http://127.0.0.1", "http://10.0.0.1",
    "http://169.254.169.254", "http://192.168.1.1", "http://[::1]", "http://[fc00::1]",
    "http://224.0.0.1", "http://[ff02::1]", "https://user:secret@example.com",
    "https://example.com:70000", "http://example.com:0", "https://exa mple.com",
    "https://example.com\\@localhost", "https://%31%32%37.0.0.1", "https://",
])
def test_url_structure_rejects_unsafe_destinations(url):
    with pytest.raises(BrowserError):
        validate_url_structure(url)


@pytest.mark.parametrize(("url", "expected"), [
    (" HTTPS://Example.COM ", "https://example.com/"),
    ("https://example.com/a?q=b#section", "https://example.com/a?q=b#section"),
    ("https://93.184.216.34", "https://93.184.216.34/"),
    ("https://[2606:4700:4700::1111]", "https://[2606:4700:4700::1111]/"),
    ("https://bücher.de", "https://xn--bcher-kva.de/"),
])
def test_url_structure_normalizes_public_urls(url, expected):
    assert validate_url_structure(url) == expected


def dns_answers(*addresses):
    return [(2, 1, 6, "", (address, 443)) for address in addresses]


@pytest.fixture
def public_dns(monkeypatch):
    monkeypatch.setattr("app.browser.socket.getaddrinfo", lambda *args, **kwargs: dns_answers("93.184.216.34"))


@pytest.mark.asyncio
@pytest.mark.parametrize("addresses", [
    ["127.0.0.1"], ["93.184.216.34", "10.0.0.1"], ["::1"], [],
])
async def test_dns_rejects_any_private_answer(monkeypatch, addresses):
    monkeypatch.setattr("app.browser.socket.getaddrinfo", lambda *args, **kwargs: dns_answers(*addresses))
    with pytest.raises(BrowserError):
        await validate_public_url("https://example.com")


@pytest.mark.asyncio
async def test_numeric_host_cannot_hide_private_ip(monkeypatch):
    monkeypatch.setattr("app.browser.socket.getaddrinfo", lambda *args, **kwargs: dns_answers("127.0.0.1"))
    with pytest.raises(BrowserError):
        await validate_public_url("http://2130706433")


@pytest.mark.asyncio
async def test_public_dns_is_allowed(public_dns):
    assert await validate_public_url("https://example.com") == "https://example.com/"


@pytest.mark.asyncio
async def test_dns_failure_is_user_facing(monkeypatch):
    def fail(*args, **kwargs):
        raise OSError("unreachable")

    monkeypatch.setattr("app.browser.socket.getaddrinfo", fail)
    with pytest.raises(BrowserError):
        await validate_public_url("https://example.com")


@pytest.mark.asyncio
async def test_explicit_proxy_dns_does_not_resolve_locally(monkeypatch):
    monkeypatch.setenv("AYAN_PROXY_DNS", "1")
    monkeypatch.setenv("HTTPS_PROXY", "http://trusted-proxy.example:8080")

    def unexpected_dns(*args, **kwargs):
        raise AssertionError("Explicit proxy DNS mode must use the configured proxy")

    monkeypatch.setattr("app.browser.socket.getaddrinfo", unexpected_dns)
    assert await validate_public_url("https://github.com") == "https://github.com/"


@pytest.mark.asyncio
async def test_proxy_dns_requires_configured_proxy(monkeypatch):
    monkeypatch.setenv("AYAN_PROXY_DNS", "1")
    for name in ("HTTPS_PROXY", "https_proxy", "HTTP_PROXY", "http_proxy"):
        monkeypatch.delenv(name, raising=False)
    with pytest.raises(BrowserError, match="HTTPS_PROXY"):
        await validate_public_url("https://example.com")


@pytest.mark.asyncio
@pytest.mark.parametrize("url", ["http://127.0.0.1", "http://10.0.0.1", "http://localhost", "file:///etc/passwd"])
async def test_proxy_dns_still_rejects_unsafe_structure(monkeypatch, url):
    monkeypatch.setenv("AYAN_PROXY_DNS", "1")
    monkeypatch.setenv("HTTPS_PROXY", "http://trusted-proxy.example:8080")
    with pytest.raises(BrowserError):
        await validate_public_url(url)


@pytest.mark.asyncio
@pytest.mark.parametrize(("url", "allow"), [
    ("https://example.com/image.png", True), ("http://127.0.0.1/secret", False),
    ("file:///etc/passwd", False),
])
async def test_request_guard_checks_subresources(public_dns, url, allow):
    service = BrowserService()
    route = SimpleNamespace(
        request=SimpleNamespace(url=url, is_navigation_request=lambda: False),
        abort=AsyncMock(), continue_=AsyncMock(),
    )
    await service._guard_request(route)
    assert route.continue_.await_count == int(allow)
    assert route.abort.await_count == int(not allow)


class FakeLocator:
    def __init__(self, exists=False, href=None):
        self.exists = exists
        self.href = href
        self.click = AsyncMock()
        self.fill = AsyncMock()

    async def count(self):
        return int(self.exists)

    def nth(self, index):
        return self

    async def is_visible(self):
        return self.exists

    async def get_attribute(self, attribute):
        return self.href


class FakePage:
    def __init__(self):
        self.url = "about:blank"
        self.history = []
        self.mouse = SimpleNamespace(wheel=AsyncMock())
        self.wait_for_timeout = AsyncMock()
        self.field = FakeLocator(True)
        self.button = FakeLocator(True)
        self.link = FakeLocator(True, "https://example.com/next")
        self.main_frame = SimpleNamespace(url=self.url)

    async def goto(self, url, **kwargs):
        self.history.append(self.url)
        self.url = url

    async def go_back(self, **kwargs):
        if self.history:
            self.url = self.history.pop()

    async def screenshot(self, **kwargs):
        return b"PNG"

    async def title(self):
        return "Test website"

    def get_by_label(self, target, exact):
        assert exact is True
        return self.field if target == "Search" else FakeLocator()

    def get_by_placeholder(self, target, exact):
        assert exact is True
        return FakeLocator()

    def get_by_role(self, role, name, exact):
        assert exact is True
        if role == "button" and name == "Submit":
            return self.button
        if role == "link" and name == "Next":
            return self.link
        return FakeLocator()


@pytest.fixture
def fake_service(public_dns):
    service = BrowserService()
    service._page = FakePage()
    service._ensure_started = AsyncMock()
    return service


@pytest.mark.asyncio
async def test_open_returns_preview(fake_service):
    result = await fake_service.execute("open", url="https://example.com")
    assert result["url"] == "https://example.com/"
    assert result["title"] == "Test website"
    assert result["screenshot"] == "UE5H"
    assert result["message"]


@pytest.mark.asyncio
async def test_browser_actions(fake_service):
    await fake_service.execute("open", url="https://example.com")
    await fake_service.execute("fill", target="Search", value="Kazakh music")
    fake_service._page.field.fill.assert_awaited_once_with("Kazakh music")
    await fake_service.execute("click", target="Submit")
    fake_service._page.button.click.assert_awaited_once()
    await fake_service.execute("scroll", direction="up")
    fake_service._page.mouse.wheel.assert_awaited_once_with(0, -650)
    await fake_service.execute("open", url="https://example.com/next")
    result = await fake_service.execute("back")
    assert result["url"] == "https://example.com/"


@pytest.mark.asyncio
async def test_unsafe_link_rejected_before_click(fake_service):
    await fake_service.execute("open", url="https://example.com")
    fake_service._page.link.href = "file:///etc/passwd"
    with pytest.raises(BrowserError):
        await fake_service.execute("click", target="Next")
    fake_service._page.link.click.assert_not_awaited()


@pytest.mark.asyncio
async def test_redirect_to_private_page_is_reset(fake_service):
    async def redirected_goto(url, **kwargs):
        fake_service._page.url = "http://10.0.0.1/" if url != "about:blank" else url

    fake_service._page.goto = redirected_goto
    with pytest.raises(BrowserError):
        await fake_service.execute("open", url="https://example.com")
    assert fake_service._page.url == "about:blank"


@pytest.mark.asyncio
async def test_background_unsafe_navigation_is_reset(fake_service):
    fake_service._page.url = "file:///etc/passwd"
    fake_service._page.main_frame.url = fake_service._page.url
    await fake_service._guard_frame(fake_service._page.main_frame)
    assert fake_service._page.url == "about:blank"
    assert fake_service._blocked_navigation


@pytest.mark.asyncio
async def test_browser_actions_require_open_page(fake_service):
    with pytest.raises(BrowserError):
        await fake_service.execute("fill", target="Search", value="hello")


@pytest.mark.asyncio
async def test_unknown_target_is_user_facing(fake_service):
    await fake_service.execute("open", url="https://example.com")
    with pytest.raises(BrowserError, match="Missing"):
        await fake_service.execute("click", target="Missing")


@pytest.mark.asyncio
async def test_actions_are_serialized(fake_service):
    active = 0
    maximum = 0

    async def slow_goto(url, **kwargs):
        nonlocal active, maximum
        active += 1
        maximum = max(maximum, active)
        await asyncio.sleep(0.01)
        fake_service._page.url = url
        active -= 1

    fake_service._page.goto = slow_goto
    await asyncio.gather(
        fake_service.execute("open", url="https://example.com/one"),
        fake_service.execute("open", url="https://example.com/two"),
    )
    assert maximum == 1


def test_environment_proxy_separates_credentials(monkeypatch):
    monkeypatch.setenv("HTTPS_PROXY", "http://name:password@proxy.example:8080")
    monkeypatch.setenv("NO_PROXY", "localhost")
    assert _environment_proxy() == {
        "server": "http://proxy.example:8080", "username": "name",
        "password": "password", "bypass": "localhost",
    }


def test_chromium_override_is_respected(monkeypatch, tmp_path):
    executable = tmp_path / "chromium"
    executable.write_text("test executable")
    executable.chmod(0o700)
    monkeypatch.setenv("AYAN_CHROMIUM_EXECUTABLE", str(executable))
    playwright = SimpleNamespace(chromium=SimpleNamespace(executable_path="/not/installed"))
    assert _chromium_executable(playwright) == str(executable)
    monkeypatch.setenv("AYAN_CHROMIUM_EXECUTABLE", "/not/installed")
    assert _chromium_executable(playwright) is None
