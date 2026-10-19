"""Discover local Ollama models without credentials or an external connection."""
from __future__ import annotations

import asyncio
import json
import re

import httpx


OLLAMA_BASE_URL = "http://127.0.0.1:11434"
DEFAULT_OLLAMA_MODEL = "qwen2.5:3b"
MAX_RESPONSE_BYTES = 1024 * 1024
_MODEL_NAME = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]*(?:/[A-Za-z0-9][A-Za-z0-9._-]*)*(?::[A-Za-z0-9][A-Za-z0-9._-]*)?")


def valid_model_name(value: object) -> bool:
    if not isinstance(value, str) or not 1 <= len(value) <= 120 or not _MODEL_NAME.fullmatch(value):
        return False
    if any(part in {".", ".."} for part in value.split("/")):
        return False
    return not value.casefold().endswith(("-cloud", ":cloud"))


def _tagged(name: str) -> str:
    return name if ":" in name.rsplit("/", 1)[-1] else name + ":latest"


class OllamaService:
    def __init__(self, preferences, *, transport=None):
        self.preferences = preferences
        self.transport = transport

    async def status(self) -> dict:
        selected = self.preferences.get("ollama_model", DEFAULT_OLLAMA_MODEL)
        result = {"available": False, "models": [], "selected_model": selected, "model_ready": False}
        if not valid_model_name(selected):
            return {**result, "error": "Баптаулардан дұрыс жергілікті Ollama моделін таңдаңыз."}
        try:
            async with asyncio.timeout(7):
                async with httpx.AsyncClient(
                    timeout=httpx.Timeout(5, connect=2), trust_env=False,
                    follow_redirects=False, transport=self.transport,
                ) as client:
                    async with client.stream("GET", OLLAMA_BASE_URL + "/api/tags") as response:
                        if response.status_code != 200:
                            return {**result, "error": "Жергілікті Ollama қызметі жауап бермеді. Ollama бағдарламасын ашып, қайта тексеріңіз."}
                        content = bytearray()
                        async for chunk in response.aiter_bytes():
                            if len(content) + len(chunk) > MAX_RESPONSE_BYTES:
                                raise ValueError("Ollama model list is too large")
                            content.extend(chunk)
            records = json.loads(content)
            if not isinstance(records, dict) or not isinstance(records.get("models"), list):
                raise ValueError("Invalid model list")
            names = set()
            for record in records["models"]:
                if not isinstance(record, dict) or record.get("remote_host") or record.get("remote_model"):
                    continue
                name = record.get("name", record.get("model"))
                if valid_model_name(name):
                    names.add(name)
            ready = _tagged(selected) in {_tagged(name) for name in names}
            result.update(available=True, models=sorted(names), model_ready=ready)
            if not ready:
                result["error"] = f"{selected} моделі орнатылмаған. Терминалда «ollama pull {selected}» пәрменін орындаңыз."
            return result
        except (httpx.TimeoutException, asyncio.TimeoutError):
            return {**result, "error": "Ollama уақытында жауап бермеді. Бағдарламаны ашып, қайта тексеріңіз."}
        except (httpx.HTTPError, OSError):
            return {**result, "error": "Ollama іске қосылмаған. Ollama бағдарламасын ашып, қайта тексеріңіз."}
        except (ValueError, TypeError):
            return {**result, "error": "Ollama модельдер тізімін оқу мүмкін болмады. Ollama бағдарламасын жаңартып, қайта тексеріңіз."}
