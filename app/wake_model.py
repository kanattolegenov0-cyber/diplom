"""Install an official Kazakh Vosk model without sending microphone audio online."""
from __future__ import annotations

from contextlib import contextmanager
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import re
import ssl
import stat
import tempfile
import threading
from urllib.parse import unquote, urljoin, urlsplit
import zipfile

import httpx


METADATA_URL = "https://alphacephei.com/vosk/models/model-list.json"
MAX_METADATA_BYTES = 2 * 1024 * 1024
MAX_DOWNLOAD_BYTES = 200 * 1024 * 1024
MAX_EXTRACTED_BYTES = 1024 * 1024 * 1024
MAX_ARCHIVE_MEMBERS = 10000
RECEIPT_NAME = ".ayan-model.json"
_HASH_LENGTHS = {"md5": 32, "sha1": 40, "sha224": 56, "sha256": 64, "sha384": 96, "sha512": 128}
_NETWORK_ERROR = (
    "Қазақша Vosk моделін ресми сайттан жүктеу мүмкін болмады. "
    "Интернет пен alphacephei.com сайтына қолжетімділікті тексеріңіз. "
    "Дайын жергілікті модель үшін AYAN_WAKE_MODEL_PATH көрсетуге болады."
)


class ModelError(RuntimeError):
    """A model installation problem that can be displayed without credentials."""


def _official_url(url: str, *, metadata: bool = False) -> str:
    try:
        parts = urlsplit(url)
        valid = (
            parts.scheme == "https"
            and parts.hostname == "alphacephei.com"
            and parts.port in (None, 443)
            and parts.username is None and parts.password is None
            and not parts.query and not parts.fragment
            and parts.path.startswith("/vosk/models/")
            and parts.path.endswith(".json" if metadata else ".zip")
        )
        decoded = unquote(parts.path)
        if "\\" in decoded or any(segment in {".", ".."} for segment in decoded.split("/")):
            valid = False
    except (TypeError, ValueError):
        valid = False
    if not valid:
        raise ModelError("Vosk метадерегіндегі жүктеу адресі ресми HTTPS адресі емес.")
    return url


def _checksums(record: dict) -> dict[str, str]:
    output: dict[str, str] = {}

    def add(algorithm: str, digest: object) -> None:
        algorithm = algorithm.casefold().replace("-", "").removesuffix("sum")
        if algorithm not in _HASH_LENGTHS or not isinstance(digest, str):
            raise ModelError("Vosk моделінің жарияланған бақылау сомасын тексеру мүмкін емес.")
        digest = digest.strip().lower()
        if not re.fullmatch(rf"[a-f0-9]{{{_HASH_LENGTHS[algorithm]}}}", digest):
            raise ModelError("Vosk моделінің жарияланған бақылау сомасы дұрыс емес.")
        if algorithm in output and output[algorithm] != digest:
            raise ModelError("Vosk метадерегіндегі бақылау сомалары бір-біріне сәйкес емес.")
        output[algorithm] = digest

    for key, value in record.items():
        normalized = key.casefold().replace("-", "").removesuffix("sum")
        if normalized in _HASH_LENGTHS and value not in (None, ""):
            add(normalized, value)
    for key in ("checksum", "checksums"):
        value = record.get(key)
        if value in (None, "", {}):
            continue
        if isinstance(value, dict):
            if "algorithm" in value and "value" in value:
                add(str(value["algorithm"]), value["value"])
            else:
                for algorithm, digest in value.items():
                    add(str(algorithm), digest)
        elif isinstance(value, str):
            if ":" in value:
                algorithm, digest = value.split(":", 1)
            else:
                digest = value.strip()
                algorithm = next((name for name, length in _HASH_LENGTHS.items() if len(digest) == length), "")
            add(algorithm, digest)
        else:
            raise ModelError("Vosk моделінің жарияланған бақылау сомасын тексеру мүмкін емес.")
    return output


def _select_model(records: object) -> dict:
    if not isinstance(records, list):
        raise ModelError("Ресми Vosk модельдер тізімінің пішімі дұрыс емес.")
    candidates = []
    for record in records:
        if not isinstance(record, dict):
            continue
        if str(record.get("lang", "")).lower() not in {"kz", "kk"}:
            continue
        if str(record.get("type", "")).lower() != "small":
            continue
        obsolete = record.get("obsolete", False)
        if obsolete is True or str(obsolete).lower() in {"true", "1"}:
            continue
        name = record.get("name", "")
        if not isinstance(name, str) or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]{0,119}", name):
            continue
        version = str(record.get("version", ""))
        if not re.fullmatch(r"\d+(?:\.\d+)*", version):
            match = re.search(r"-(\d+(?:\.\d+)*)$", name)
            if not match:
                continue
            version = match.group(1)
        # Development/preview names are never an automatic wake-model choice.
        if re.search(r"(?:^|[-_.])(?:alpha|beta|dev|preview|rc)\d*(?:[-_.]|$)", name, re.I):
            continue
        candidates.append((tuple(int(part) for part in version.split(".")), name, record))
    if not candidates:
        raise ModelError("Ресми тізімде тұрақты шағын қазақша Vosk моделі табылмады. Кейінірек қайталаңыз немесе жергілікті модельді көрсетіңіз.")
    chosen = dict(max(candidates, key=lambda item: (item[0], item[1]))[2])
    chosen["url"] = _official_url(chosen.get("url", ""))
    chosen["checksums"] = _checksums(chosen)
    return chosen


def _valid_model(path: Path) -> bool:
    try:
        return (
            path.is_dir()
            and all((path / name).is_file() and (path / name).stat().st_size > 0
                    for name in ("am/final.mdl", "conf/model.conf"))
            and (path / "graph").is_dir()
            and any(item.is_file() and item.stat().st_size > 0 for item in (path / "graph").glob("*.fst"))
        )
    except (OSError, ValueError):
        return False


def _safe_member(member: zipfile.ZipInfo) -> PurePosixPath:
    name = member.orig_filename
    parts = PurePosixPath(name)
    mode = stat.S_IFMT(member.external_attr >> 16)
    reserved = {"con", "prn", "aux", "nul"} | {f"{prefix}{number}" for prefix in ("com", "lpt") for number in range(1, 10)}
    if (
        not name or not parts.parts or "\x00" in name or "\\" in name or len(name) > 1024
        or parts.is_absolute() or ".." in parts.parts or len(parts.parts) > 50
        or mode not in (0, stat.S_IFREG, stat.S_IFDIR)
        or bool(member.flag_bits & 1)
        or bool(member.external_attr & 0x400)  # Windows reparse points
        or any(":" in part or part.endswith((".", " "))
               or part.split(".", 1)[0].lower() in reserved for part in parts.parts)
    ):
        raise ModelError("Vosk архивінде қауіпсіз емес файл жолы немесе сілтеме бар.")
    return parts


def _extract(archive_path: Path, destination: Path) -> None:
    with zipfile.ZipFile(archive_path) as archive:
        members = archive.infolist()
        if len(members) > MAX_ARCHIVE_MEMBERS or sum(member.file_size for member in members) > MAX_EXTRACTED_BYTES:
            raise ModelError("Vosk архивінің ашылған көлемі рұқсат етілген шектен асты.")
        checked = []
        seen = set()
        for member in members:
            relative = _safe_member(member)
            key = str(relative).casefold()
            if key in seen:
                raise ModelError("Vosk архивінде қайталанатын файл жолы бар.")
            seen.add(key)
            checked.append((member, relative))
        total_written = 0
        for member, relative in checked:
            target = destination.joinpath(*relative.parts)
            if member.is_dir():
                target.mkdir(parents=True, exist_ok=True)
                continue
            target.parent.mkdir(parents=True, exist_ok=True)
            with archive.open(member) as source, target.open("xb") as output:
                while chunk := source.read(64 * 1024):
                    total_written += len(chunk)
                    if total_written > MAX_EXTRACTED_BYTES:
                        raise ModelError("Vosk архивінің ашылған көлемі рұқсат етілген шектен асты.")
                    output.write(chunk)


class ModelManager:
    def __init__(self, data_dir: Path, *, transport: httpx.BaseTransport | None = None):
        self.data_dir = Path(data_dir)
        self.model_dir = self.data_dir / "wake-model"
        self._transport = transport
        self._install_lock = threading.Lock()
        self._state_lock = threading.RLock()
        self._installing = False
        self._error: str | None = None
        self._selected: dict = {}
        self._downloaded = 0
        self._total: int | None = None

    def path(self) -> Path | None:
        override = os.environ.get("AYAN_WAKE_MODEL_PATH")
        try:
            candidate = Path(override).expanduser().resolve() if override else self.model_dir
            if not override and candidate.is_symlink():
                return None
            return candidate if _valid_model(candidate) else None
        except (OSError, ValueError):
            return None

    def ready(self) -> bool:
        return self.path() is not None

    def info(self) -> dict:
        path = self.path()
        override = bool(os.environ.get("AYAN_WAKE_MODEL_PATH"))
        receipt = {}
        if path is not None and not override:
            try:
                receipt_path = path / RECEIPT_NAME
                if receipt_path.stat().st_size <= 64 * 1024:
                    receipt = json.loads(receipt_path.read_text(encoding="utf-8"))
                    if not isinstance(receipt, dict):
                        receipt = {}
            except (OSError, ValueError):
                pass
        with self._state_lock:
            return {
                "ready": path is not None,
                "status": "installing" if self._installing else "ready" if path else "error" if self._error else "missing",
                "source": "override" if override else "official" if receipt else "local" if path else "missing",
                "path": str(path) if path else None,
                "model": self._selected.get("name") or receipt.get("model") or (path.name if path else None),
                "size": self._selected.get("size") or receipt.get("size"),
                "downloaded_bytes": self._downloaded or receipt.get("downloaded_bytes", 0),
                "total_bytes": self._total,
                "verified_checksums": receipt.get("verified_checksums", {}),
                "metadata_url": METADATA_URL,
                "error": self._error,
            }

    @contextmanager
    def _stream(self, client: httpx.Client, url: str, *, metadata: bool = False):
        for _ in range(4):
            _official_url(url, metadata=metadata)
            with client.stream("GET", url) as response:
                if response.is_redirect:
                    location = response.headers.get("location")
                    if not location:
                        raise ModelError("Vosk сайтының қайта бағыттау адресі дұрыс емес.")
                    url = urljoin(url, location)
                    continue
                response.raise_for_status()
                yield response
                return
        raise ModelError("Vosk сайты тым көп рет басқа адреске бағыттады.")

    def _metadata(self, client: httpx.Client) -> dict:
        content = bytearray()
        with self._stream(client, METADATA_URL, metadata=True) as response:
            for chunk in response.iter_bytes():
                if len(content) + len(chunk) > MAX_METADATA_BYTES:
                    raise ModelError("Vosk модельдер тізімінің көлемі рұқсат етілген шектен асты.")
                content.extend(chunk)
        try:
            return _select_model(json.loads(content))
        except (ValueError, TypeError) as exc:
            raise ModelError("Ресми Vosk модельдер тізімін оқу мүмкін болмады.") from exc

    def _download(self, client: httpx.Client, selected: dict, destination: Path) -> tuple[int, dict[str, str]]:
        expected = selected["checksums"]
        hashes = {algorithm: hashlib.new(algorithm, usedforsecurity=False) for algorithm in set(expected) | {"sha256"}}
        downloaded = 0
        with self._stream(client, selected["url"]) as response:
            length = response.headers.get("content-length")
            if length is not None:
                try:
                    total = int(length)
                except ValueError as exc:
                    raise ModelError("Vosk жүктеу көлемі дұрыс көрсетілмеген.") from exc
                if total < 0 or total > MAX_DOWNLOAD_BYTES:
                    raise ModelError("Vosk моделі 200 МБ жүктеу шегінен үлкен.")
                with self._state_lock:
                    self._total = total
            with destination.open("xb") as output:
                for chunk in response.iter_bytes(chunk_size=64 * 1024):
                    downloaded += len(chunk)
                    if downloaded > MAX_DOWNLOAD_BYTES:
                        raise ModelError("Vosk моделі 200 МБ жүктеу шегінен үлкен.")
                    for digest in hashes.values():
                        digest.update(chunk)
                    output.write(chunk)
                    with self._state_lock:
                        self._downloaded = downloaded
        digests = {algorithm: digest.hexdigest() for algorithm, digest in hashes.items()}
        if any(digests[algorithm] != value for algorithm, value in expected.items()):
            raise ModelError("Vosk моделінің бақылау сомасы ресми метадерекке сәйкес емес. Модель қолданылған жоқ.")
        return downloaded, digests

    def install(self) -> dict:
        if not self._install_lock.acquire(blocking=False):
            raise ModelError("Vosk моделі қазір орнатылып жатыр. Аяқталғанша күтіңіз.")
        try:
            if self.ready():
                return self.info()
            if os.environ.get("AYAN_WAKE_MODEL_PATH"):
                raise ModelError("AYAN_WAKE_MODEL_PATH көрсеткен жерде толық Vosk моделі жоқ. am/final.mdl, conf/model.conf және graph файлдарын тексеріңіз.")
            if self.model_dir.exists() or self.model_dir.is_symlink():
                raise ModelError("wake-model қалтасында бұрыннан файлдар бар. Олар өзгертілмеді; қалтаны тексеріңіз немесе AYAN_WAKE_MODEL_PATH арқылы дайын модельді көрсетіңіз.")
            with self._state_lock:
                self._installing, self._error = True, None
                self._downloaded, self._total = 0, None
            self.data_dir.mkdir(parents=True, exist_ok=True)
            with tempfile.TemporaryDirectory(prefix=".wake-model-", dir=self.data_dir) as temporary:
                staging = Path(temporary)
                with httpx.Client(
                    verify=ssl.create_default_context(),
                    timeout=httpx.Timeout(120, connect=15),
                    follow_redirects=False,
                    transport=self._transport,
                ) as client:
                    selected = self._metadata(client)
                    with self._state_lock:
                        self._selected = selected
                    downloaded, digests = self._download(client, selected, staging / "model.zip")
                unpacked = staging / "unpacked"
                _extract(staging / "model.zip", unpacked)
                candidate = unpacked / selected["name"]
                if not _valid_model(candidate):
                    candidates = [path for path in (unpacked, *unpacked.iterdir()) if _valid_model(path)]
                    if len(candidates) != 1:
                        raise ModelError("Vosk архивінде толық модель табылмады: am/final.mdl, conf/model.conf және graph қажет.")
                    candidate = candidates[0]
                receipt = {
                    "model": selected["name"], "size": selected.get("size"),
                    "url": selected["url"], "metadata_url": METADATA_URL,
                    "downloaded_bytes": downloaded,
                    "verified_checksums": selected["checksums"],
                    "sha256": digests["sha256"],
                    "installed_at": datetime.now(timezone.utc).isoformat(),
                }
                receipt_path = candidate / RECEIPT_NAME
                if receipt_path.exists():
                    raise ModelError("Vosk архивінде күтпеген орнату деректері бар.")
                receipt_path.write_text(json.dumps(receipt, ensure_ascii=False, indent=2), encoding="utf-8")
                if self.model_dir.exists() or self.model_dir.is_symlink():
                    raise ModelError("wake-model қалтасы орнату кезінде пайда болды. Бұрынғы файлдар өзгертілмеді.")
                candidate.rename(self.model_dir)
            with self._state_lock:
                self._installing = False
            return self.info()
        except ModelError as exc:
            with self._state_lock:
                self._error = str(exc)
            raise
        except (httpx.HTTPError, ssl.SSLError) as exc:
            with self._state_lock:
                self._error = _NETWORK_ERROR
            raise ModelError(_NETWORK_ERROR) from exc
        except (OSError, ValueError, zipfile.BadZipFile, EOFError) as exc:
            message = "Vosk моделін орнату мүмкін болмады. Дисктегі бос орынды және ресми модель архивін тексеріңіз; бұрынғы файлдар өзгертілмеді."
            with self._state_lock:
                self._error = message
            raise ModelError(message) from exc
        finally:
            with self._state_lock:
                self._installing = False
            self._install_lock.release()
