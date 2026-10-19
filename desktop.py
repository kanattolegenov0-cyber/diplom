"""Native Windows entry point for Ayan, with a browser fallback."""

from __future__ import annotations

import argparse
import ctypes
import html
import logging
import os
from pathlib import Path
import shutil
import socket
import subprocess
import sys
import threading
import time
import urllib.request
import webbrowser


LOGGER = logging.getLogger("ayan.desktop")


def configure_environment() -> Path:
    """Keep user data and downloaded browsers outside the application bundle."""
    local_app_data = os.environ.get("LOCALAPPDATA")
    if local_app_data:
        default_data_dir = Path(local_app_data) / "Ayan"
    else:
        default_data_dir = Path(os.environ.get("XDG_DATA_HOME", Path.home() / ".local" / "share")) / "Ayan"
    os.environ.setdefault("AYAN_DATA_DIR", str(default_data_dir))
    data_dir = Path(os.environ["AYAN_DATA_DIR"]).expanduser().resolve()
    data_dir.mkdir(parents=True, exist_ok=True)
    os.environ["AYAN_DATA_DIR"] = str(data_dir)
    os.environ.setdefault("PLAYWRIGHT_BROWSERS_PATH", str(data_dir / "browsers"))
    os.environ["AYAN_DESKTOP_MODE"] = "1"
    return data_dir


def configure_logging(data_dir: Path) -> Path:
    log_path = data_dir / "desktop.log"
    handlers: list[logging.Handler] = [logging.FileHandler(log_path, encoding="utf-8")]
    if sys.stderr is not None:
        handlers.append(logging.StreamHandler(sys.stderr))
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
        handlers=handlers,
        force=True,
    )
    return log_path


def install_browser() -> None:
    """Use Playwright's verified installer, including inside a frozen bundle."""
    LOGGER.info("Installing Chromium into %s", os.environ["PLAYWRIGHT_BROWSERS_PATH"])
    if not getattr(sys, "frozen", False):
        subprocess.run([sys.executable, "-m", "playwright", "install", "chromium"], check=True)
        return

    from playwright.__main__ import main as playwright_main

    previous_argv = sys.argv
    try:
        sys.argv = ["playwright", "install", "chromium"]
        try:
            playwright_main()
        except SystemExit as exc:
            if exc.code not in (None, 0):
                raise RuntimeError(f"Chromium installation failed (exit {exc.code}).") from exc
    finally:
        sys.argv = previous_argv


def ensure_browser() -> None:
    override = os.environ.get("AYAN_CHROMIUM_EXECUTABLE")
    if override:
        if not Path(override).is_file():
            raise RuntimeError("AYAN_CHROMIUM_EXECUTABLE does not point to an existing browser.")
        return
    if sys.platform.startswith("linux") and shutil.which("chromium"):
        return
    from playwright.sync_api import sync_playwright

    with sync_playwright() as playwright:
        browser_executable = Path(playwright.chromium.executable_path)
    if not browser_executable.is_file():
        install_browser()
    if not browser_executable.is_file():
        raise RuntimeError("Playwright Chromium is still missing after installation.")


class LocalServer:
    """Run the local API in a worker thread and release its socket on exit."""

    def __init__(self, port: int = 0) -> None:
        self.port = port
        self.url = ""
        self.server = None
        self.thread: threading.Thread | None = None
        self.listener: socket.socket | None = None

    def start(self) -> None:
        import uvicorn
        from app.main import app

        self.listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        try:
            self.listener.bind(("127.0.0.1", self.port))
            self.listener.listen(128)
            self.port = self.listener.getsockname()[1]
            self.url = f"http://127.0.0.1:{self.port}"
            config = uvicorn.Config(
                app,
                host="127.0.0.1",
                port=self.port,
                loop="asyncio",
                log_config=None,
                access_log=False,
            )
            self.server = uvicorn.Server(config)
            self.thread = threading.Thread(
                target=self.server.run,
                kwargs={"sockets": [self.listener]},
                name="ayan-server",
                daemon=True,
            )
            self.thread.start()
            deadline = time.monotonic() + 30
            while not self.server.started:
                if not self.thread.is_alive():
                    raise RuntimeError("Ayan's local server stopped during startup. See desktop.log.")
                if time.monotonic() > deadline:
                    raise TimeoutError("Ayan's local server did not start within 30 seconds.")
                time.sleep(0.05)
        except BaseException:
            self.stop()
            raise

    def stop(self) -> None:
        if self.server is not None:
            self.server.should_exit = True
        if self.thread is not None and self.thread.is_alive():
            self.thread.join(timeout=5)
        if self.listener is not None:
            self.listener.close()
            self.listener = None


def show_error(message: str, log_path: Path) -> None:
    description = f"{message}\n\nҚате журналы / Error log: {log_path}"
    if sys.stderr is not None:
        print(description, file=sys.stderr)
    elif sys.platform == "win32":
        ctypes.windll.user32.MessageBoxW(None, description, "Аян — іске қосу қатесі", 0x10)


def startup_html(log_path: Path) -> str:
    return f"""<!doctype html><html lang="kk"><meta charset="utf-8">
    <style>body{{background:#111827;color:#eef2ff;font:18px 'Segoe UI',sans-serif;
    max-width:700px;margin:12vh auto;padding:30px}}h1{{font-size:42px}}p{{line-height:1.6}}
    small{{color:#a5b4c5;overflow-wrap:anywhere}}</style><h1>Аян / AYAN</h1>
    <p>Көмекші іске қосылып жатыр…</p>
    <p>Алғашқы іске қосқанда браузер жүктелуі мүмкін. Интернет байланысы қажет.</p>
    <small>Журнал: {html.escape(str(log_path))}</small></html>"""


def run_native(server: LocalServer, log_path: Path, debug: bool) -> None:
    import webview

    cancelled = threading.Event()
    window = webview.create_window(
        "Аян / AYAN",
        html=startup_html(log_path),
        width=1280,
        height=850,
        min_size=(760, 600),
    )

    def bootstrap() -> None:
        try:
            ensure_browser()
            if cancelled.is_set():
                return
            server.start()
            if cancelled.is_set():
                server.stop()
                return
            window.load_url(server.url)
        except Exception as exc:
            LOGGER.exception("Startup failed")
            window.load_html(
                startup_html(log_path).replace(
                    "<p>Көмекші іске қосылып жатыр…</p>",
                    "<h2>Іске қосу мүмкін болмады</h2><p>" + html.escape(str(exc)) + "</p>",
                )
            )

    try:
        webview.start(bootstrap, gui="edgechromium" if sys.platform == "win32" else None, debug=debug)
    finally:
        cancelled.set()
        server.stop()


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Аян / AYAN — жергілікті дауыстық көмекші")
    parser.add_argument("--web", action="store_true", help="Open in the default browser instead of a native window.")
    parser.add_argument("--install-browser", action="store_true", help="Install Playwright Chromium and exit.")
    parser.add_argument("--check", action="store_true", help="Check Chromium and the local dashboard without opening a window.")
    parser.add_argument("--port", type=int, default=0, help="Loopback port; default 0 selects an available port.")
    parser.add_argument("--debug", action="store_true", help="Enable the native webview developer tools.")
    args = parser.parse_args()
    if not 0 <= args.port <= 65535:
        parser.error("--port must be between 0 and 65535")
    return args


def main() -> int:
    args = parse_args()
    data_dir = configure_environment()
    log_path = configure_logging(data_dir)
    server = LocalServer(args.port)
    try:
        if args.install_browser:
            install_browser()
            return 0
        if args.web or args.check:
            ensure_browser()
            server.start()
            if args.check:
                with urllib.request.urlopen(server.url, timeout=15) as response:
                    if response.status != 200:
                        raise RuntimeError(f"Dashboard returned HTTP {response.status}.")
                LOGGER.info("Chromium and local dashboard checks passed")
                return 0
            LOGGER.info("Open %s; press Ctrl+C to stop", server.url)
            webbrowser.open(server.url)
            while server.thread is not None and server.thread.is_alive():
                time.sleep(0.25)
            raise RuntimeError("The local server stopped unexpectedly.")
        run_native(server, log_path, args.debug)
        return 0
    except KeyboardInterrupt:
        return 0
    except Exception as exc:
        LOGGER.exception("Ayan could not start")
        show_error(str(exc), log_path)
        return 1
    finally:
        server.stop()


if __name__ == "__main__":
    raise SystemExit(main())
