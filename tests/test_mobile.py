"""Phone install assets, bounded audio upload, and the optional remote host."""

from io import BytesIO
import json
import wave

from fastapi.testclient import TestClient

from app.main import create_app
from app.mobile_voice import MAX_UPLOAD_BYTES, transcribe_upload


def _wav():
    buffer = BytesIO()
    with wave.open(buffer, "wb") as output:
        output.setnchannels(1)
        output.setsampwidth(2)
        output.setframerate(16000)
        output.writeframes(b"\x00\x00" * 16000)
    return buffer.getvalue()


def test_phone_audio_is_decoded_before_transcription():
    class Transcription:
        def status(self):
            return {"active_provider": "whisper", "model_ready": True}

        def transcribe(self, audio, language):
            assert audio.sample_rate == 16000
            assert audio.sample_width == 2
            assert language == "kk-KZ"
            return "Ән қос"

    assert transcribe_upload(_wav(), "audio/wav", "kk-KZ", Transcription()) == "Ән қос"


def test_phone_upload_is_bounded_and_pwa_assets_are_served(tmp_path, monkeypatch):
    monkeypatch.setattr("app.main.transcribe_upload", lambda data, kind, language, service: "Музыка қос")
    with TestClient(create_app(tmp_path)) as client:
        assert client.post("/api/voice/upload?language=kk-KZ", content=_wav(), headers={"content-type": "audio/wav"}).json() == {"text": "Музыка қос"}
        assert client.post("/api/voice/upload", content=b"test", headers={"content-type": "text/plain"}).status_code == 415
        assert client.post("/api/voice/upload", content=b"x" * (MAX_UPLOAD_BYTES + 1), headers={"content-type": "audio/wav"}).status_code == 413
        assert client.post("/api/voice/upload?language=en-US", content=_wav(), headers={"content-type": "audio/wav"}).status_code == 422
        manifest = client.get("/static/manifest.webmanifest")
        assert manifest.status_code == 200
        assert json.loads(manifest.content)["display"] == "standalone"
        assert client.get("/static/icon-180.png").headers["content-type"] == "image/png"
        worker = client.get("/service-worker.js")
        assert worker.status_code == 200 and worker.headers["cache-control"] == "no-cache"
        assert "/api/" not in worker.text


def test_remote_host_is_explicit_and_keeps_same_origin(tmp_path, monkeypatch):
    monkeypatch.setenv("AYAN_PUBLIC_HOST", "ayan.example.com")
    with TestClient(create_app(tmp_path), base_url="https://ayan.example.com") as client:
        assert client.get("/").status_code == 200
        assert client.post("/api/diary", headers={"Origin": "https://ayan.example.com"}, json={"content": "Су іш"}).status_code == 201
        assert client.post("/api/diary", headers={"Origin": "https://evil.example"}, json={"content": "x"}).status_code == 403
        assert client.get("/", headers={"Host": "evil.example"}).status_code == 400
