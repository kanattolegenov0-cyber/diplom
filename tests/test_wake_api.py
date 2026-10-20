from fastapi.testclient import TestClient
import pytest

from app.main import create_app
from app.preferences import Preferences
from app.wake_model import ModelError


class Models:
    def __init__(self):
        self.installed = False
        self.installs = 0
        self.failure = False

    def info(self):
        return {"ready": self.installed}

    def install(self):
        self.installs += 1
        if self.failure:
            raise ModelError("Модель жүктелмеді.")
        self.installed = True
        return self.info()


class Wake:
    def __init__(self, models):
        self.models = models
        self.enabled = False
        self.calls = []
        self.events = []

    def status(self, since=0):
        return {"enabled": self.enabled, "phase": "waiting" if self.enabled else "stopped",
                "model_ready": self.models.installed,
                "events": [event for event in self.events if event["id"] > since]}

    def start(self, language):
        self.calls.append(("start", language))
        self.enabled = self.models.installed
        return self.status()

    def stop(self):
        self.calls.append(("stop",))
        self.enabled = False
        return self.status()

    def set_busy(self, busy):
        self.calls.append(("busy", busy))

    def close(self):
        self.calls.append(("close",))


@pytest.fixture
def setup(tmp_path, monkeypatch):
    monkeypatch.setenv("AYAN_DESKTOP_MODE", "1")
    models = Models()
    wake = Wake(models)
    return tmp_path, models, wake


def test_startup_only_starts_saved_mode_and_never_downloads(setup):
    path, models, wake = setup
    with TestClient(create_app(path, wake=wake, wake_model=models)) as client:
        assert wake.calls == [("start", "kk-KZ")]
        assert models.installs == 0
        assert client.get("/api/wake/status").json()["model_ready"] is False
    assert wake.calls[-1] == ("close",)


def test_disabled_saved_mode_does_not_start(setup):
    path, models, wake = setup
    Preferences(path).update({"wake_enabled": False})
    with TestClient(create_app(path, wake=wake, wake_model=models)):
        assert wake.calls == []


def test_browser_mode_cannot_start_or_install_native_capture(setup, monkeypatch):
    path, models, wake = setup
    monkeypatch.delenv("AYAN_DESKTOP_MODE")
    with TestClient(create_app(path, wake=wake, wake_model=models)) as client:
        for route, body in [("start", {"language": "kk-KZ"}), ("stop", {}),
                            ("activity", {"busy": True}), ("model/install", {})]:
            assert client.post(f"/api/wake/{route}", json=body).status_code == 409
        assert client.get("/api/wake/status").status_code == 409
        assert wake.calls == [] and models.installs == 0


def test_install_is_explicit_and_start_stop_do_not_change_preference(setup):
    path, models, wake = setup
    with TestClient(create_app(path, wake=wake, wake_model=models)) as client:
        response = client.post("/api/wake/model/install", json={})
        assert response.status_code == 200 and response.json()["enabled"]
        assert models.installs == 1
        assert client.post("/api/wake/stop", json={}).json()["enabled"] is False
        assert client.get("/api/settings").json()["wake_enabled"] is True
        assert client.post("/api/wake/start", json={"language": "ru-RU"}).json()["enabled"] is True
        assert client.get("/api/settings").json()["wake_language"] == "kk-KZ"
        client.post("/api/wake/activity", json={"busy": True})
        client.post("/api/wake/activity", json={"busy": False})
        assert wake.calls[-2:] == [("busy", True), ("busy", False)]


def test_settings_persist_disabled_state_and_language_restart(setup):
    path, models, wake = setup
    models.installed = True
    with TestClient(create_app(path, wake=wake, wake_model=models)) as client:
        client.post("/api/settings", json={"wake_enabled": False})
        assert wake.calls[-1] == ("stop",)
        client.post("/api/settings", json={"wake_enabled": True, "wake_language": "ru-RU"})
        assert wake.calls[-2:] == [("stop",), ("start", "ru-RU")]
        calls_after_wake_change = list(wake.calls)
        client.post("/api/settings", json={"speech_style": "natural", "speech_rate": 0, "speech_pitch": 0})
        assert wake.calls == calls_after_wake_change
    assert Preferences(path).get("wake_language") == "ru-RU"


def test_event_cursor_readiness_and_download_failure_are_truthful(setup, monkeypatch):
    path, models, wake = setup
    wake.events = [{"id": 1, "type": "wake", "text": "Аян"},
                   {"id": 2, "type": "command", "text": "уақыт"}]
    monkeypatch.setattr("app.main.voice.get_status", lambda: {"phase": "listening"})
    with TestClient(create_app(path, wake=wake, wake_model=models)) as client:
        status = client.get("/api/wake/status?since=1").json()
        assert status["events"] == wake.events[1:]
        assert status["voice_phase"] == "listening"
        assert client.get("/api/wake/status?since=-1").status_code == 422
        models.failure = True
        response = client.post("/api/wake/model/install", json={})
        assert response.status_code == 502
        assert response.json()["detail"] == "Модель жүктелмеді."
        assert not wake.enabled


@pytest.mark.parametrize("settings", [
    {"speech_style": "actor"}, {"speech_rate": -31}, {"speech_pitch": 11},
    {"speech_rate": True}, {"wake_enabled": "yes"}, {"wake_language": "en-US"},
])
def test_new_settings_validate_without_opening_microphone(setup, settings):
    path, models, wake = setup
    with TestClient(create_app(path, wake=wake, wake_model=models)) as client:
        before = list(wake.calls)
        assert client.post("/api/settings", json=settings).status_code == 422
        assert wake.calls == before
