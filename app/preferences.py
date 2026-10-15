"""Local settings. API keys stay server-side and use Windows user encryption."""
from __future__ import annotations

import base64
import ctypes
from ctypes import wintypes
import json
import os
from pathlib import Path
import re
import threading


DEFAULTS = {
    "llm_enabled": True,
    "llm_model": "gpt-4.1-mini",
    "tts_provider": "edge",
    "tts_voice": "kk-KZ-DauletNeural",
    "elevenlabs_voice_id": "",
    "elevenlabs_model": "eleven_v3",
}
SECRET_ENV = {
    "llm_api_key": ("AYAN_LLM_API_KEY", "OPENAI_API_KEY"),
    "elevenlabs_api_key": ("AYAN_ELEVENLABS_API_KEY", "ELEVENLABS_API_KEY"),
}


class SettingsError(ValueError):
    pass


def _windows_protect(data: bytes, *, decrypt: bool = False) -> bytes:
    """DPAPI binds a key to the signed-in Windows account, outside the repo."""
    class DataBlob(ctypes.Structure):
        _fields_ = [("size", wintypes.DWORD), ("data", ctypes.POINTER(ctypes.c_ubyte))]

    buffer = (ctypes.c_ubyte * len(data)).from_buffer_copy(data)
    incoming = DataBlob(len(data), ctypes.cast(buffer, ctypes.POINTER(ctypes.c_ubyte)))
    outgoing = DataBlob()
    crypt32 = ctypes.WinDLL("crypt32", use_last_error=True)
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    operation = crypt32.CryptUnprotectData if decrypt else crypt32.CryptProtectData
    operation.argtypes = [ctypes.POINTER(DataBlob), ctypes.c_void_p, ctypes.c_void_p,
                          ctypes.c_void_p, ctypes.c_void_p, wintypes.DWORD, ctypes.POINTER(DataBlob)]
    operation.restype = wintypes.BOOL
    kernel32.LocalFree.argtypes = [ctypes.c_void_p]
    kernel32.LocalFree.restype = ctypes.c_void_p
    if not operation(ctypes.byref(incoming), None, None, None, None, 1, ctypes.byref(outgoing)):
        raise SettingsError("API кілтін Windows сақтау қоймасында өңдеу мүмкін болмады. Баптауды қайта сақтаңыз.")
    try:
        return ctypes.string_at(outgoing.data, outgoing.size)
    finally:
        kernel32.LocalFree(outgoing.data)


class Preferences:
    def __init__(self, directory: Path):
        directory.mkdir(parents=True, exist_ok=True)
        self.path = directory / "preferences.json"
        self.lock = threading.RLock()
        self.settings = dict(DEFAULTS)
        self.secrets = {}
        if self.path.is_file():
            try:
                saved = json.loads(self.path.read_text(encoding="utf-8"))
                self.settings.update({k: v for k, v in saved.get("settings", {}).items() if k in DEFAULTS})
                self.secrets = {k: v for k, v in saved.get("secrets", {}).items() if k in SECRET_ENV and isinstance(v, dict)}
                self._validate(self.settings)
            except (OSError, ValueError, AttributeError, TypeError) as exc:
                raise SettingsError("Баптау файлын оқу мүмкін болмады. preferences.json файлын тексеріңіз.") from exc

    def get(self, name: str, default=None):
        with self.lock:
            return self.settings.get(name, default)

    def _decode_secret(self, saved: dict) -> str | None:
        try:
            data = base64.b64decode(saved["value"], validate=True)
            if saved["storage"] == "dpapi":
                if os.name != "nt":
                    return None
                data = _windows_protect(data, decrypt=True)
            elif saved["storage"] != "local":
                return None
            return data.decode("utf-8")
        except (KeyError, ValueError, UnicodeError, SettingsError):
            return None

    def secret(self, name: str) -> str | None:
        if name not in SECRET_ENV:
            return None
        with self.lock:
            saved = self._decode_secret(self.secrets[name]) if name in self.secrets else None
            if saved:
                return saved
            return next((os.environ[key] for key in SECRET_ENV[name] if os.environ.get(key)), None)

    def public(self) -> dict:
        with self.lock:
            output = dict(self.settings)
            output["llm_configured"] = bool(self.secret("llm_api_key"))
            output["elevenlabs_configured"] = bool(self.secret("elevenlabs_api_key"))
            output["secret_storage"] = "windows-dpapi" if os.name == "nt" else "local-file"
            return output

    @staticmethod
    def _validate(settings: dict) -> None:
        if type(settings["llm_enabled"]) is not bool:
            raise SettingsError("Модельді қосу баптауы дұрыс емес.")
        if not isinstance(settings["llm_model"], str) or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]{0,99}", settings["llm_model"]):
            raise SettingsError("GPT моделінің атауы дұрыс емес.")
        if settings["tts_provider"] not in {"edge", "elevenlabs", "system"}:
            raise SettingsError("Дауыс сервисін таңдаңыз.")
        if settings["tts_voice"] not in {"kk-KZ-DauletNeural", "kk-KZ-AigulNeural"}:
            raise SettingsError("Қазақша Дәулет немесе Айгүл дауысын таңдаңыз.")
        if not isinstance(settings["elevenlabs_voice_id"], str) or not re.fullmatch(r"[A-Za-z0-9]{0,64}", settings["elevenlabs_voice_id"]):
            raise SettingsError("ElevenLabs voice ID дұрыс емес.")
        if settings["elevenlabs_model"] not in {"eleven_v3", "eleven_multilingual_v2"}:
            raise SettingsError("ElevenLabs моделінің атауы дұрыс емес.")

    def update(self, updates: dict) -> dict:
        allowed = set(DEFAULTS) | set(SECRET_ENV) | {"clear_llm_api_key", "clear_elevenlabs_api_key"}
        if set(updates) - allowed:
            raise SettingsError("Баптау өрістері дұрыс емес.")
        with self.lock:
            settings = {**self.settings, **{k: v for k, v in updates.items() if k in DEFAULTS}}
            self._validate(settings)
            secrets = dict(self.secrets)
            for key in SECRET_ENV:
                if updates.get("clear_" + key):
                    secrets.pop(key, None)
                value = updates.get(key)
                if value is not None:
                    if not isinstance(value, str) or len(value) > 4096:
                        raise SettingsError("API кілтінің форматы дұрыс емес.")
                    value = value.strip()
                    if value:
                        encrypted = _windows_protect(value.encode("utf-8")) if os.name == "nt" else value.encode("utf-8")
                        secrets[key] = {"storage": "dpapi" if os.name == "nt" else "local",
                                        "value": base64.b64encode(encrypted).decode("ascii")}
            content = json.dumps({"settings": settings, "secrets": secrets}, ensure_ascii=False, indent=2)
            temporary = self.path.with_name("preferences.json.tmp")
            try:
                descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
                with os.fdopen(descriptor, "w", encoding="utf-8") as file:
                    file.write(content)
                os.replace(temporary, self.path)
                if os.name != "nt":
                    os.chmod(self.path, 0o600)
            except OSError as exc:
                raise SettingsError("Баптауды сақтау мүмкін болмады. Деректер қалтасының рұқсатын тексеріңіз.") from exc
            self.settings, self.secrets = settings, secrets
            return self.public()
