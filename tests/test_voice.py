"""Microphone readiness and error handling without recording real audio."""

import importlib.machinery
import sys
import threading
from types import ModuleType, SimpleNamespace
from urllib.error import URLError

import pytest

from app import voice


@pytest.fixture
def audio_modules(monkeypatch):
    """Model the drivers' interfaces and record phases at their boundaries."""
    assert voice.get_status() == {"phase": "idle"}
    assert voice._microphone_lock.acquire(blocking=False)
    voice._microphone_lock.release()
    events = []
    state = SimpleNamespace(
        stream=object(), capture_error=None, recognition_error=None,
        open_error=None, calibration_error=None, listen_hook=None,
        audio=object(), recognizer=None,
    )
    sr = ModuleType("speech_recognition")
    sr.__spec__ = importlib.machinery.ModuleSpec("speech_recognition", loader=None)
    sr.WaitTimeoutError = type("WaitTimeoutError", (Exception,), {})
    sr.UnknownValueError = type("UnknownValueError", (Exception,), {})
    sr.RequestError = type("RequestError", (Exception,), {})

    class Microphone:
        def __init__(self):
            events.append(("microphone", voice.get_status()["phase"]))

        def __enter__(self):
            events.append(("open", voice.get_status()["phase"]))
            if state.open_error:
                raise state.open_error
            return SimpleNamespace(stream=state.stream)

        def __exit__(self, *args):
            events.append(("close", voice.get_status()["phase"]))

    class Recognizer:
        def __init__(self):
            state.recognizer = self

        def adjust_for_ambient_noise(self, source, duration):
            events.append(("calibrate", voice.get_status()["phase"], duration))
            assert source.stream is not None
            if state.calibration_error:
                raise state.calibration_error

        def listen(self, source, timeout, phrase_time_limit):
            events.append(("listen", voice.get_status()["phase"], timeout, phrase_time_limit))
            if state.listen_hook:
                state.listen_hook()
            if state.capture_error:
                raise state.capture_error
            return state.audio

        def recognize_google(self, audio, language, endpoint):
            assert audio is state.audio
            events.append(("recognize", voice.get_status()["phase"], language, endpoint))
            if state.recognition_error:
                raise state.recognition_error
            return "Музыка қос: Moldanazar"

    sr.Microphone = Microphone
    sr.Recognizer = Recognizer
    pyaudio = ModuleType("pyaudio")
    pyaudio.__spec__ = importlib.machinery.ModuleSpec("pyaudio", loader=None)
    monkeypatch.setitem(sys.modules, "speech_recognition", sr)
    monkeypatch.setitem(sys.modules, "pyaudio", pyaudio)
    yield SimpleNamespace(sr=sr, state=state, events=events)
    assert voice.get_status() == {"phase": "idle"}
    assert voice._microphone_lock.acquire(blocking=False)
    voice._microphone_lock.release()


def test_capture_readiness_precedes_recognition_and_resets(audio_modules):
    assert voice.available() is True
    assert voice.listen("kk-KZ") == "Музыка қос: Moldanazar"
    assert audio_modules.events == [
        ("microphone", "calibrating"),
        ("open", "calibrating"),
        ("calibrate", "calibrating", 1.0),
        ("listen", "listening", 8, 12),
        ("close", "listening"),
        ("recognize", "recognizing", "kk-KZ", "https://www.google.com/speech-api/v2/recognize"),
    ]
    assert audio_modules.state.recognizer.operation_timeout == 10
    assert voice.get_status() == {"phase": "idle"}


def test_russian_language_is_forwarded(audio_modules):
    voice.listen("ru-RU")
    assert audio_modules.events[-1][2] == "ru-RU"


def test_wait_timeout_has_windows_input_and_readiness_guidance(audio_modules):
    audio_modules.state.capture_error = audio_modules.sr.WaitTimeoutError()
    with pytest.raises(voice.VoiceError, match="Дауыс естілмеді") as caught:
        voice.listen("kk-KZ")
    assert "Windows" in str(caught.value)
    assert "әдепкі микрофон" in str(caught.value)
    assert "дыбыс деңгейін" in str(caught.value)
    assert "Айтыңыз" in str(caught.value)
    assert not any(event[0] == "recognize" for event in audio_modules.events)


def test_silent_microphone_open_failure_is_not_sent_to_google(audio_modules):
    audio_modules.state.stream = None
    with pytest.raises(voice.VoiceError, match="Микрофон ашылмады"):
        voice.listen("kk-KZ")
    assert audio_modules.events == [
        ("microphone", "calibrating"), ("open", "calibrating"), ("close", "calibrating")
    ]


@pytest.mark.parametrize("stage,error", [
    ("open_error", OSError("no device")),
    ("open_error", AttributeError("PyAudio unavailable")),
    ("calibration_error", OSError("input disconnected")),
    ("capture_error", OSError("input disconnected")),
])
def test_capture_failures_are_microphone_errors_and_release_lock(audio_modules, stage, error):
    setattr(audio_modules.state, stage, error)
    with pytest.raises(voice.VoiceError, match="Микрофон ашылмады"):
        voice.listen("kk-KZ")
    setattr(audio_modules.state, stage, None)
    assert voice.listen("kk-KZ")


def test_unrecognized_audio_is_distinct_from_no_audio(audio_modules):
    audio_modules.state.recognition_error = audio_modules.sr.UnknownValueError()
    with pytest.raises(voice.VoiceError, match="Сөзді түсіне алмадым"):
        voice.listen("kk-KZ")
    assert audio_modules.events[-1][1] == "recognizing"


@pytest.mark.parametrize("kind", ["request", "timeout", "connection", "url"])
def test_google_network_failures_are_not_microphone_errors(audio_modules, kind):
    errors = {
        "request": audio_modules.sr.RequestError("service unavailable"),
        "timeout": TimeoutError("network timed out"),
        "connection": ConnectionResetError("connection reset"),
        "url": URLError("network unreachable"),
    }
    audio_modules.state.recognition_error = errors[kind]
    with pytest.raises(voice.VoiceError, match="Интернетті тексеріңіз") as caught:
        voice.listen("kk-KZ")
    assert "Микрофон ашылмады" not in str(caught.value)


def test_flac_converter_failure_is_not_a_microphone_failure(audio_modules):
    audio_modules.state.recognition_error = OSError("FLAC conversion utility not available")
    with pytest.raises(voice.VoiceError, match="FLAC") as caught:
        voice.listen("kk-KZ")
    assert "Микрофон ашылмады" not in str(caught.value)
    audio_modules.state.recognition_error = None
    assert voice.listen("kk-KZ")


def test_parallel_recording_is_busy_without_changing_active_phase(audio_modules):
    entered = threading.Event()
    finish = threading.Event()
    results = []

    def hold_recording():
        entered.set()
        assert finish.wait(timeout=3)

    def record():
        try:
            results.append(voice.listen("kk-KZ"))
        except Exception as exc:
            results.append(exc)

    audio_modules.state.listen_hook = hold_recording
    worker = threading.Thread(target=record)
    worker.start()
    try:
        assert entered.wait(timeout=3)
        assert voice.get_status() == {"phase": "listening"}
        with pytest.raises(voice.VoiceError, match="Сәл күтіңіз"):
            voice.listen("ru-RU")
        assert voice.get_status() == {"phase": "listening"}
    finally:
        finish.set()
        worker.join(timeout=3)
    assert not worker.is_alive()
    assert results == ["Музыка қос: Moldanazar"]
    assert sum(event[0] == "microphone" for event in audio_modules.events) == 1


def test_status_is_a_snapshot_not_shared_mutable_state(audio_modules):
    status = voice.get_status()
    status["phase"] = "listening"
    assert voice.get_status() == {"phase": "idle"}


def test_invalid_language_does_not_open_device(audio_modules):
    with pytest.raises(voice.VoiceError, match="Қазақ немесе орыс"):
        voice.listen("en-US")
    assert audio_modules.events == []


def test_availability_checks_metadata_without_opening_hardware(monkeypatch, audio_modules):
    calls = []

    def find_spec(name):
        calls.append(name)
        return None if name == "pyaudio" else object()

    monkeypatch.setattr(voice.importlib.util, "find_spec", find_spec)
    assert voice.available() is False
    with pytest.raises(voice.VoiceError, match="Микрофон модулі орнатылмаған"):
        voice.listen("kk-KZ")
    assert calls == ["speech_recognition", "pyaudio", "speech_recognition", "pyaudio"]
    assert audio_modules.events == []
