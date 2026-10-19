"""Local wake lifecycle tests with fake microphone and offline recognizer."""

import json
from pathlib import Path
import queue
import sys
import threading
import time
from types import ModuleType, SimpleNamespace
from unittest.mock import Mock

import pytest

from app.wake import WakeService, extract_wake_command


@pytest.mark.parametrize(("text", "command"), [
    ("Аян", ""), ("айан", ""), ("АЯН, музыка қос", "музыка қос"),
    ("Әй, Аян: күнделік аш", "күнделік аш"), ("аян аян музыка қос", "музыка қос"),
    ("Баян музыка қос", None), ("аянда", None), ("аянға", None),
    ("баян", None), ("айанда", None), ("аян_шақыр", None), ("музыка қос", None),
])
def test_wake_word_boundaries_and_command_tail(text, command):
    assert extract_wake_command(text) == command


def wait_for(predicate, timeout=2):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return
        time.sleep(0.005)
    assert predicate(), "Background worker did not reach the expected state"


class ModelManager:
    def __init__(self, ready=True):
        self.is_ready = ready
        self.install = Mock()

    def ready(self):
        return self.is_ready

    def path(self):
        return Path("/fake/local-kazakh-model") if self.is_ready else None


@pytest.fixture
def harness(monkeypatch):
    state = SimpleNamespace(
        inputs=queue.Queue(), streams=[], open_options=[], recognized=[], terminated=0,
        models=[], voice_calls=[], listen_impl=None, voice_phase="idle",
    )

    class Stream:
        def __init__(self):
            self.closed = False

        def read(self, count, exception_on_overflow):
            assert count == 4000
            assert exception_on_overflow is False
            while not self.closed:
                try:
                    text = state.inputs.get(timeout=0.01)
                except queue.Empty:
                    continue
                if text == "__read_error__":
                    raise OSError("No audio device")
                return text.encode("utf-8")
            raise OSError("Stream closed")

        def stop_stream(self):
            self.closed = True

        def close(self):
            self.closed = True

    class Audio:
        def open(self, **kwargs):
            state.open_options.append(kwargs)
            stream = Stream()
            state.streams.append(stream)
            return stream

        def terminate(self):
            state.terminated += 1

    class Recognizer:
        def __init__(self, model, sample_rate):
            assert sample_rate == 16000
            self.text = ""

        def AcceptWaveform(self, pcm):
            self.text = pcm.decode("utf-8")
            state.recognized.append(self.text)
            return True

        def Result(self):
            if self.text == "__bad_json__":
                return "not json"
            return json.dumps({"text": self.text})

    pyaudio = ModuleType("pyaudio")
    pyaudio.PyAudio = Audio
    pyaudio.paInt16 = 8
    vosk = ModuleType("vosk")
    vosk.SetLogLevel = Mock()

    def model(path):
        state.models.append(path)
        return object()

    vosk.Model = model
    vosk.KaldiRecognizer = Recognizer
    monkeypatch.setitem(sys.modules, "pyaudio", pyaudio)
    monkeypatch.setitem(sys.modules, "vosk", vosk)

    class VoiceError(Exception):
        pass

    class VoiceCancelled(VoiceError):
        pass

    microphone_lock = threading.Lock()

    def listen(language, *, stop_event=None):
        assert all(stream.closed for stream in state.streams)
        assert microphone_lock.acquire(blocking=False), "Wake microphone was not released"
        state.voice_calls.append(language)
        try:
            state.voice_phase = "listening"
            if state.listen_impl:
                return state.listen_impl(stop_event)
            return "музыка қос"
        finally:
            state.voice_phase = "idle"
            microphone_lock.release()

    voice = SimpleNamespace(
        _microphone_lock=microphone_lock, listen=listen, VoiceError=VoiceError,
        VoiceCancelled=VoiceCancelled,
        get_status=lambda: {"phase": state.voice_phase},
    )
    services = []

    def service(manager=None):
        wake = WakeService(SimpleNamespace(), voice, manager or ModelManager())
        services.append(wake)
        return wake

    state.service = service
    state.voice = voice
    state.feed = state.inputs.put
    yield state
    for wake in services:
        wake.close()
    assert not microphone_lock.locked()
    assert all(stream.closed for stream in state.streams)


def test_missing_model_does_not_download_or_open_microphone(harness):
    manager = ModelManager(ready=False)
    wake = harness.service(manager)
    status = wake.start()
    assert status["enabled"] is False
    assert status["phase"] == "model_missing"
    assert status["model_ready"] is False
    assert not harness.streams and not harness.models
    manager.install.assert_not_called()


def test_background_speech_never_calls_google(harness):
    wake = harness.service()
    wake.start()
    for text in ("Баян музыка қос", "аянда", "жай сөйлеп отырмын"):
        harness.feed(text)
    wait_for(lambda: len(harness.recognized) == 3)
    assert not harness.voice_calls
    assert wake.status()["events"] == []
    assert wake.status()["phase"] == "waiting"
    assert harness.open_options[0] == {
        "format": 8, "channels": 1, "rate": 16000, "input": True, "frames_per_buffer": 4000,
    }


def test_wake_only_releases_local_stream_then_captures_command(harness):
    wake = harness.service()
    wake.start()
    harness.feed("Аян")
    wait_for(lambda: any(event["type"] == "command" for event in wake.status()["events"]))
    assert harness.voice_calls == ["kk-KZ"]
    assert wake.status()["phase"] == "paused"
    assert [(event["type"], event["text"]) for event in wake.status()["events"]] == [
        ("wake", "Аян"), ("command", "музыка қос"),
    ]
    assert all(stream.closed for stream in harness.streams)


def test_same_utterance_keeps_command_first_words_without_google(harness):
    wake = harness.service()
    wake.start()
    harness.feed("аян ютубтан Димаштың әнін қос")
    wait_for(lambda: len(wake.status()["events"]) == 2)
    assert wake.status()["events"][1]["text"] == "ютубтан Димаштың әнін қос"
    assert not harness.voice_calls


def test_same_utterance_uses_local_retranscription_after_releasing_microphone(harness):
    calls = []
    def retranscribe(pcm, language, fallback, *, stop_event):
        assert all(stream.closed for stream in harness.streams)
        assert not stop_event.is_set()
        calls.append((pcm, language, fallback))
        return "Moldanazar әнін қос"
    harness.voice.transcribe_wake = retranscribe
    wake = harness.service()
    wake.start()
    harness.feed("жай әңгіме")
    harness.feed("аян молдан азар әнін қос")
    wait_for(lambda: len(wake.status()["events"]) == 2)
    assert calls == [("аян молдан азар әнін қос".encode(), "kk-KZ", "молдан азар әнін қос")]
    assert wake.status()["events"][1]["text"] == "Moldanazar әнін қос"
    assert not harness.voice_calls


def test_command_pauses_until_acknowledged_and_duplicate_is_suppressed(harness):
    wake = harness.service()
    wake.start()
    harness.feed("аян музыка қос")
    wait_for(lambda: len(wake.status()["events"]) == 2)
    opens = len(harness.streams)
    time.sleep(0.06)
    assert len(harness.streams) == opens
    wake.set_busy(False)
    harness.feed("аян музыка қос")
    harness.feed("аян күнделік аш")
    wait_for(lambda: len(wake.status()["events"]) == 4)
    assert [event["text"] for event in wake.status()["events"] if event["type"] == "command"] == [
        "музыка қос", "күнделік аш",
    ]


def test_busy_closes_microphone_and_resumes_when_cleared(harness):
    wake = harness.service()
    wake.start()
    wait_for(lambda: wake.status()["phase"] == "waiting")
    wake.set_busy(True)
    wait_for(lambda: not harness.voice._microphone_lock.locked())
    assert wake.status()["phase"] == "paused"
    assert all(stream.closed for stream in harness.streams)
    wake.set_busy(False)
    wait_for(lambda: wake.status()["phase"] == "waiting")
    assert len(harness.streams) == 2


def test_start_while_busy_does_not_open_microphone(harness):
    wake = harness.service()
    wake.set_busy(True)
    wake.start()
    wait_for(lambda: len(harness.models) == 1)
    assert wake.status()["phase"] == "paused"
    assert not harness.streams


def test_push_to_talk_microphone_lock_is_respected(harness):
    harness.voice._microphone_lock.acquire()
    wake = harness.service()
    try:
        wake.start()
        wait_for(lambda: wake.status()["phase"] == "paused")
        assert not harness.streams
    finally:
        harness.voice._microphone_lock.release()
    wait_for(lambda: wake.status()["phase"] == "waiting")


def test_stop_releases_device_thread_and_audio_driver(harness):
    wake = harness.service()
    wake.start()
    wait_for(lambda: wake.status()["phase"] == "waiting")
    status = wake.stop()
    assert status["enabled"] is False
    assert status["phase"] == "stopped"
    assert wake._thread is None
    assert not harness.voice._microphone_lock.locked()
    assert harness.terminated == 1


def test_stop_cancels_command_capture_and_discards_command(harness):
    capturing = threading.Event()

    def wait_command(stop_event):
        capturing.set()
        assert stop_event.wait(2)
        raise harness.voice.VoiceError("Тоқтатылды")

    harness.listen_impl = wait_command
    wake = harness.service()
    wake.start()
    harness.feed("аян")
    assert capturing.wait(2)
    assert wake.stop()["phase"] == "stopped"
    assert not any(event["type"] == "command" for event in wake.status()["events"])
    assert not any(event["type"] == "error" for event in wake.status()["events"])
    assert "error" not in wake.status()
    assert not harness.voice._microphone_lock.locked()


def test_busy_cancels_command_capture_without_failing_service(harness):
    capturing = threading.Event()

    def wait_command(stop_event):
        capturing.set()
        assert stop_event.wait(2)
        raise harness.voice.VoiceError("Тоқтатылды")

    harness.listen_impl = wait_command
    wake = harness.service()
    wake.start()
    harness.feed("аян")
    assert capturing.wait(2)
    wake.set_busy(True)
    wait_for(lambda: not harness.voice._microphone_lock.locked())
    assert wake.status()["phase"] == "paused"
    assert wake.status()["enabled"] is True
    assert not any(event["type"] == "error" for event in wake.status()["events"])


def test_command_readiness_phase_tracks_existing_voice_backend(harness):
    capturing = threading.Event()

    def wait_command(stop_event):
        harness.voice_phase = "calibrating"
        capturing.set()
        stop_event.wait(2)
        raise harness.voice.VoiceError("Тоқтатылды")

    harness.listen_impl = wait_command
    wake = harness.service()
    wake.start()
    harness.feed("аян")
    assert capturing.wait(2)
    assert wake.status()["phase"] == "calibrating"
    harness.voice_phase = "listening"
    assert wake.status()["phase"] == "command"
    harness.voice_phase = "recognizing"
    assert wake.status()["phase"] == "recognizing"


@pytest.mark.parametrize("input_text", ["__read_error__", "__bad_json__"])
def test_capture_failure_stops_without_automatic_restart(harness, input_text):
    wake = harness.service()
    wake.start()
    harness.feed(input_text)
    wait_for(lambda: wake.status()["phase"] == "error")
    assert wake.status()["enabled"] is False
    assert wake.status()["events"][-1]["type"] == "error"
    wait_for(lambda: not harness.voice._microphone_lock.locked())
    opens = len(harness.streams)
    time.sleep(0.08)
    assert len(harness.streams) == opens
    wake.start()
    wait_for(lambda: wake.status()["phase"] == "waiting")
    assert len(harness.streams) == opens + 1


@pytest.mark.parametrize("message", [
    "Дауыс естілмеді.", "Сөзді түсіне алмадым.",
    "Сөйлеуді тану сервисіне қосыла алмадым.",
])
def test_command_recognition_failure_keeps_local_wake_enabled(harness, message):
    def fail_command(stop_event):
        raise harness.voice.VoiceError(message)

    harness.listen_impl = fail_command
    wake = harness.service()
    wake.start()
    harness.feed("аян")
    wait_for(lambda: len(wake.status()["events"]) == 2 and wake.status()["phase"] == "waiting")
    assert wake.status()["enabled"] is True
    assert "error" not in wake.status()
    assert [event["type"] for event in wake.status()["events"]] == ["wake", "error"]
    assert wake.status()["events"][-1]["text"] == message
    time.sleep(0.06)
    assert harness.voice_calls == ["kk-KZ"]
    # A fresh wake can retry immediately; background listening never retries
    # Google by itself, including after a transient connection failure.
    harness.listen_impl = None
    harness.feed("аян")
    wait_for(lambda: len(wake.status()["events"]) == 4)
    assert [event["type"] for event in wake.status()["events"]] == ["wake", "error", "wake", "command"]
    assert harness.voice_calls == ["kk-KZ", "kk-KZ"]


def test_empty_command_recovers_without_another_google_call(harness):
    harness.listen_impl = lambda stop_event: "   "
    wake = harness.service()
    wake.start()
    harness.feed("аян")
    wait_for(lambda: len(wake.status()["events"]) == 2 and wake.status()["phase"] == "waiting")
    assert wake.status()["enabled"] is True
    assert wake.status()["events"][-1]["type"] == "error"
    assert harness.voice_calls == ["kk-KZ"]


def test_late_pause_cancellation_does_not_emit_false_error(harness):
    def cancelled_command(stop_event):
        raise harness.voice.VoiceCancelled("Тоқтатылды")

    harness.listen_impl = cancelled_command
    wake = harness.service()
    wake.start()
    harness.feed("аян")
    wait_for(lambda: harness.voice_calls and wake.status()["phase"] == "waiting")
    assert wake.status()["enabled"] is True
    assert [event["type"] for event in wake.status()["events"]] == ["wake"]
    assert "error" not in wake.stop()


def test_stopping_after_fatal_error_clears_status_error(harness):
    wake = harness.service()
    wake.start()
    harness.feed("__read_error__")
    wait_for(lambda: wake.status()["phase"] == "error")
    assert "error" in wake.status()
    status = wake.stop()
    assert status["phase"] == "stopped"
    assert "error" not in status


def test_russian_command_capture_uses_requested_language(harness):
    wake = harness.service()
    wake.start("ru-RU")
    harness.feed("аян")
    wait_for(lambda: len(wake.status()["events"]) == 2)
    assert harness.voice_calls == ["ru-RU"]


def test_repeated_start_keeps_one_worker(harness):
    wake = harness.service()
    wake.start()
    wait_for(lambda: wake.status()["phase"] == "waiting")
    thread = wake._thread
    wake.start()
    assert wake._thread is thread
    assert len(harness.models) == 1


def test_events_are_bounded_and_filtered_by_id(harness):
    wake = harness.service()
    for index in range(100):
        wake._emit("wake", str(index))
    events = wake.status()["events"]
    assert len(events) == 64
    assert events[0]["id"] == 37
    assert [event["id"] for event in wake.status(since=98)["events"]] == [99, 100]
