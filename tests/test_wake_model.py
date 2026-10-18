"""Installer tests use synthetic metadata and tiny ZIPs, never a full model."""
from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
import hashlib
import io
import json
from pathlib import Path
import stat
import threading
import zipfile

import httpx
import pytest

import app.wake_model as wake_model
from app.wake_model import METADATA_URL, ModelError, ModelManager


FAKE_NAME = "vosk-model-small-kz-0.99"
FAKE_URL = f"https://alphacephei.com/vosk/models/{FAKE_NAME}.zip"


@pytest.fixture(autouse=True)
def no_external_model_override(monkeypatch):
    monkeypatch.delenv("AYAN_WAKE_MODEL_PATH", raising=False)


def model_files(base: Path) -> None:
    for relative in ("am/final.mdl", "conf/model.conf", "graph/HCLr.fst"):
        target = base / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(b"synthetic model artifact")


def archive_bytes(*, extra=None, with_model=True) -> bytes:
    stream = io.BytesIO()
    with zipfile.ZipFile(stream, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        if with_model:
            for name in ("am/final.mdl", "conf/model.conf", "graph/HCLr.fst"):
                archive.writestr(f"{FAKE_NAME}/{name}", b"synthetic model artifact")
        for name, content in extra or []:
            archive.writestr(name, content)
    return stream.getvalue()


def record(**updates) -> dict:
    return {"name": FAKE_NAME, "lang": "kz", "type": "small", "version": "0.99",
            "obsolete": "false", "url": FAKE_URL, "size": "small test fixture", **updates}


def transport_for(archive: bytes, *, records=None, download_headers=None, download_status=200):
    requests = []

    def handler(request):
        requests.append(str(request.url))
        if str(request.url) == METADATA_URL:
            return httpx.Response(200, json=records if records is not None else [record(md5=hashlib.md5(archive).hexdigest())])
        return httpx.Response(download_status, content=archive, headers=download_headers)

    return httpx.MockTransport(handler), requests


def test_verified_model_install_is_atomic_and_reusable(tmp_path):
    archive = archive_bytes()
    digest = hashlib.sha256(archive).hexdigest()
    transport, requests = transport_for(archive, records=[record(sha256=digest)])
    manager = ModelManager(tmp_path, transport=transport)
    assert not manager.ready()
    assert manager.info()["status"] == "missing"
    result = manager.install()
    assert result["ready"] is True
    assert result["status"] == "ready"
    assert result["source"] == "official"
    assert result["model"] == FAKE_NAME
    assert result["verified_checksums"] == {"sha256": digest}
    assert result["downloaded_bytes"] == len(archive)
    assert manager.path() == tmp_path / "wake-model"
    assert list(tmp_path.glob(".wake-model-*")) == []
    assert len(requests) == 2
    assert manager.install()["ready"] is True
    assert len(requests) == 2
    assert ModelManager(tmp_path).info()["verified_checksums"] == {"sha256": digest}


def test_selects_latest_stable_small_kazakh_model(tmp_path):
    archive = archive_bytes()
    records = [
        record(name="vosk-model-small-kz-0.10", version="0.10"),
        record(name=FAKE_NAME, lang="kk", version="0.99"),
        record(name="vosk-model-small-kz-4.0-beta", version="4.0"),
        record(name="vosk-model-small-ru-5.0", lang="ru", version="5.0"),
        record(name="vosk-model-kz-5.0", type="big", version="5.0"),
        record(name="vosk-model-small-kz-8.0", obsolete="true", version="8.0"),
    ]
    transport, _ = transport_for(archive, records=records)
    assert ModelManager(tmp_path, transport=transport).install()["model"] == FAKE_NAME


def test_version_can_come_from_official_model_name(tmp_path):
    transport, _ = transport_for(archive_bytes(), records=[record(version=None)])
    assert ModelManager(tmp_path, transport=transport).install()["ready"]


def test_published_md5_is_verified_and_recorded(tmp_path):
    archive = archive_bytes()
    transport, _ = transport_for(archive)
    result = ModelManager(tmp_path, transport=transport).install()
    assert result["verified_checksums"] == {"md5": hashlib.md5(archive).hexdigest()}


@pytest.mark.parametrize("checksums", [
    {"md5": "0" * 32}, {"sha256": "0" * 64},
    {"checksum": {"algorithm": "sha256", "value": "0" * 64}},
])
def test_mismatched_checksums_never_install(tmp_path, checksums):
    transport, _ = transport_for(archive_bytes(), records=[record(**checksums)])
    manager = ModelManager(tmp_path, transport=transport)
    with pytest.raises(ModelError, match="бақылау сомасы"):
        manager.install()
    assert not manager.ready()
    assert not (tmp_path / "wake-model").exists()
    assert list(tmp_path.glob(".wake-model-*")) == []


@pytest.mark.parametrize("checksums", [{"md5": "invalid"}, {"checksum": "crc32:0000"}, {"sha256": 123}])
def test_unverifiable_published_checksums_fail_before_download(tmp_path, checksums):
    transport, requests = transport_for(archive_bytes(), records=[record(**checksums)])
    with pytest.raises(ModelError):
        ModelManager(tmp_path, transport=transport).install()
    assert requests == [METADATA_URL]


def test_all_published_checksums_must_match(tmp_path):
    archive = archive_bytes()
    transport, _ = transport_for(archive, records=[record(md5=hashlib.md5(archive).hexdigest(), sha256="0" * 64)])
    with pytest.raises(ModelError, match="бақылау сомасы"):
        ModelManager(tmp_path, transport=transport).install()


def test_local_override_never_downloads(tmp_path, monkeypatch):
    local = tmp_path / "my-local-model"
    model_files(local)
    monkeypatch.setenv("AYAN_WAKE_MODEL_PATH", str(local))
    manager = ModelManager(tmp_path / "data")
    assert manager.ready()
    assert manager.path() == local
    assert manager.install()["source"] == "override"
    assert not (tmp_path / "data").exists()


def test_invalid_local_override_does_not_silently_install_elsewhere(tmp_path, monkeypatch):
    monkeypatch.setenv("AYAN_WAKE_MODEL_PATH", str(tmp_path / "missing"))
    manager = ModelManager(tmp_path)
    assert manager.path() is None
    with pytest.raises(ModelError, match="AYAN_WAKE_MODEL_PATH"):
        manager.install()
    assert not (tmp_path / "wake-model").exists()


@pytest.mark.parametrize("missing", ["am/final.mdl", "conf/model.conf", "graph/HCLr.fst"])
def test_partial_local_model_is_not_ready(tmp_path, monkeypatch, missing):
    local = tmp_path / "local"
    model_files(local)
    (local / missing).unlink()
    monkeypatch.setenv("AYAN_WAKE_MODEL_PATH", str(local))
    assert not ModelManager(tmp_path).ready()


def test_unknown_existing_directory_is_never_overwritten(tmp_path):
    existing = tmp_path / "wake-model"
    existing.mkdir()
    user_file = existing / "my-notes.txt"
    user_file.write_text("preserve this", encoding="utf-8")
    with pytest.raises(ModelError, match="бұрыннан файлдар"):
        ModelManager(tmp_path).install()
    assert user_file.read_text() == "preserve this"
    assert sorted(tmp_path.iterdir()) == [existing]


def test_existing_default_symlink_is_never_replaced(tmp_path):
    target = tmp_path / "actual"
    model_files(target)
    (tmp_path / "wake-model").symlink_to(target, target_is_directory=True)
    manager = ModelManager(tmp_path)
    assert not manager.ready()
    with pytest.raises(ModelError):
        manager.install()
    assert (tmp_path / "wake-model").is_symlink()


@pytest.mark.parametrize("unsafe", [
    "../../escape.txt", "/absolute.txt", "C:/windows.txt", "a\\..\\escape.txt",
    f"{FAKE_NAME}/am/data:stream", f"{FAKE_NAME}/nul.txt", f"{FAKE_NAME}/folder./test",
])
def test_unsafe_archive_paths_are_rejected(tmp_path, unsafe):
    archive = archive_bytes(extra=[(unsafe, b"unsafe")])
    transport, _ = transport_for(archive)
    with pytest.raises(ModelError, match="қауіпсіз емес"):
        ModelManager(tmp_path, transport=transport).install()
    assert not (tmp_path / "wake-model").exists()
    assert list(tmp_path.glob(".wake-model-*")) == []


def test_zip_symlinks_are_rejected(tmp_path):
    entry = zipfile.ZipInfo(f"{FAKE_NAME}/link")
    entry.create_system = 3
    entry.external_attr = (stat.S_IFLNK | 0o777) << 16
    archive = archive_bytes(extra=[(entry, b"../../outside")])
    transport, _ = transport_for(archive)
    with pytest.raises(ModelError, match="қауіпсіз емес"):
        ModelManager(tmp_path, transport=transport).install()
    assert not (tmp_path / "wake-model").exists()


def test_windows_reparse_points_are_rejected(tmp_path):
    entry = zipfile.ZipInfo(f"{FAKE_NAME}/junction")
    entry.create_system = 0
    entry.external_attr = 0x400
    archive = archive_bytes(extra=[(entry, b"outside")])
    transport, _ = transport_for(archive)
    with pytest.raises(ModelError, match="қауіпсіз емес"):
        ModelManager(tmp_path, transport=transport).install()


def test_windows_case_collisions_are_rejected(tmp_path):
    archive = archive_bytes(extra=[(f"{FAKE_NAME}/AM/FINAL.MDL", b"collision")])
    transport, _ = transport_for(archive)
    with pytest.raises(ModelError, match="қайталанатын"):
        ModelManager(tmp_path, transport=transport).install()


@pytest.mark.parametrize("url", [
    "http://alphacephei.com/vosk/models/model.zip",
    "https://other.example/model.zip",
    "https://alphacephei.com.example/vosk/models/model.zip",
    "https://user:fixture@alphacephei.com/vosk/models/model.zip",
    "https://alphacephei.com/vosk/models/../outside.zip",
    "https://alphacephei.com/vosk/models/%2e%2e/outside.zip",
])
def test_downloads_are_restricted_to_official_https_host(tmp_path, url):
    transport, requests = transport_for(archive_bytes(), records=[record(url=url)])
    with pytest.raises(ModelError, match="ресми HTTPS"):
        ModelManager(tmp_path, transport=transport).install()
    assert requests == [METADATA_URL]


def test_redirect_cannot_escape_official_host(tmp_path):
    requests = []

    def handler(request):
        requests.append(str(request.url))
        return httpx.Response(302, headers={"location": "https://other.example/models.json"})

    with pytest.raises(ModelError, match="ресми HTTPS"):
        ModelManager(tmp_path, transport=httpx.MockTransport(handler)).install()
    assert requests == [METADATA_URL]


def test_official_same_host_redirect_is_allowed(tmp_path):
    archive = archive_bytes()

    def handler(request):
        if str(request.url) == METADATA_URL:
            return httpx.Response(200, json=[record()])
        if str(request.url) == FAKE_URL:
            return httpx.Response(302, headers={"location": "/vosk/models/redirected-model.zip"})
        return httpx.Response(200, content=archive)

    assert ModelManager(tmp_path, transport=httpx.MockTransport(handler)).install()["ready"]


def test_oversized_advertised_download_is_not_consumed(tmp_path, monkeypatch):
    monkeypatch.setattr(wake_model, "MAX_DOWNLOAD_BYTES", 5)
    transport, _ = transport_for(archive_bytes(), download_headers={"content-length": "5000"})
    with pytest.raises(ModelError, match="200 МБ"):
        ModelManager(tmp_path, transport=transport).install()
    assert not (tmp_path / "wake-model").exists()


def test_streaming_download_is_bounded_without_content_length(tmp_path, monkeypatch):
    monkeypatch.setattr(wake_model, "MAX_DOWNLOAD_BYTES", 10)

    class Streaming(httpx.SyncByteStream):
        def __iter__(self):
            yield b"a" * 8
            yield b"b" * 8

    def handler(request):
        if str(request.url) == METADATA_URL:
            return httpx.Response(200, json=[record()])
        return httpx.Response(200, stream=Streaming())

    with pytest.raises(ModelError, match="200 МБ"):
        ModelManager(tmp_path, transport=httpx.MockTransport(handler)).install()


def test_metadata_payload_is_bounded(tmp_path, monkeypatch):
    monkeypatch.setattr(wake_model, "MAX_METADATA_BYTES", 10)
    transport, _ = transport_for(archive_bytes())
    with pytest.raises(ModelError, match="тізімінің көлемі"):
        ModelManager(tmp_path, transport=transport).install()


def test_uncompressed_archive_size_is_bounded(tmp_path, monkeypatch):
    monkeypatch.setattr(wake_model, "MAX_EXTRACTED_BYTES", 20)
    transport, _ = transport_for(archive_bytes())
    with pytest.raises(ModelError, match="ашылған көлемі"):
        ModelManager(tmp_path, transport=transport).install()


@pytest.mark.parametrize("records", [None, {}, [], [record(lang="ru")], [record(type="big")]])
def test_missing_or_invalid_official_metadata_never_claims_ready(tmp_path, records):
    def handler(request):
        return httpx.Response(200, json=records)

    manager = ModelManager(tmp_path, transport=httpx.MockTransport(handler))
    with pytest.raises(ModelError):
        manager.install()
    assert not manager.ready()
    assert manager.info()["status"] == "error"


def test_incomplete_archive_is_not_installed(tmp_path):
    transport, _ = transport_for(archive_bytes(extra=[("README.txt", b"not a model")], with_model=False))
    with pytest.raises(ModelError):
        ModelManager(tmp_path, transport=transport).install()
    assert not (tmp_path / "wake-model").exists()


def test_network_error_does_not_expose_proxy_credentials(tmp_path):
    def handler(request):
        raise httpx.ProxyError("private proxy details must stay private")

    manager = ModelManager(tmp_path, transport=httpx.MockTransport(handler))
    with pytest.raises(ModelError) as caught:
        manager.install()
    assert "private proxy details" not in str(caught.value)
    assert "alphacephei.com" in str(caught.value)
    assert not manager.ready()


def test_only_one_install_runs_and_progress_is_visible(tmp_path):
    archive = archive_bytes()
    entered, release = threading.Event(), threading.Event()

    def handler(request):
        if str(request.url) == METADATA_URL:
            entered.set()
            assert release.wait(5)
            return httpx.Response(200, json=[record()])
        return httpx.Response(200, content=archive)

    manager = ModelManager(tmp_path, transport=httpx.MockTransport(handler))
    with ThreadPoolExecutor(max_workers=1) as pool:
        future = pool.submit(manager.install)
        assert entered.wait(5)
        assert manager.info()["status"] == "installing"
        try:
            with pytest.raises(ModelError, match="қазір орнатылып"):
                manager.install()
        finally:
            release.set()
        assert future.result(timeout=5)["ready"]
