"""Cooperative voice cancellation releases real interfaces using fake drivers."""

import importlib.machinery
import sys
import threading
from types import ModuleType, SimpleNamespace

import pytest

from app import voice


@pytest.fixture
def cancellable_audio(monkeypatch):
    assert voice.get_status() == {"phase": "idle"}
    assert voice._microphone_lock.acquire(blocking=False)
    voice._microphone_lock.release()
    state = SimpleNamespace(
        stop=threading.Event(), cancel_at=None, events=[], stream=None,
        read_count=0, google_count=0, google_hook=None,
    )

    class Stream:
        def __init__(self):
            self.closed = False
            self.close_count = 0

        def read(self, *args, **kwargs):
            assert not self.closed
            state.read_count += 1
            state.events.append(("read", voice.get_status()["phase"]))
            if state.cancel_at == "during_" + voice.get_status()["phase"] + "_read":
                state.stop.set()
            return b"fake audio"

        def close(self):
            self.closed = True
            self.close_count += 1
            state.events.append(("close", voice.get_status()["phase"]))
            if state.cancel_at == "before_google":
                state.stop.set()

    class Microphone:
        def __init__(self):
            state.events.append(("construct", voice.get_status()["phase"]))
            state.stream = Stream()
            self.source = SimpleNamespace(stream=state.stream)

        def __enter__(self):
            state.events.append(("open", voice.get_status()["phase"]))
            return self.source

        def __exit__(self, *args):
            self.source.stream.close()

    class Recognizer:
        def adjust_for_ambient_noise(self, source, duration):
            state.events.append(("calibrate", voice.get_status()["phase"]))
            if state.cancel_at == "before_calibrating_read":
                state.stop.set()
            source.stream.read(1024, exception_on_overflow=False)

        def listen(self, source, timeout, phrase_time_limit):
            state.events.append(("listen", voice.get_status()["phase"]))
            if state.cancel_at == "before_listening_read":
                state.stop.set()
            source.stream.read(1024, exception_on_overflow=False)
            return object()

        def recognize_google(self, audio, language, endpoint):
            state.google_count += 1
            state.events.append(("google", voice.get_status()["phase"]))
            assert endpoint == "https://www.google.com/speech-api/v2/recognize"
            assert state.stream.closed
            if state.cancel_at == "after_google":
                state.stop.set()
            if state.google_hook:
                state.google_hook()
            return "Музыка қос: Moldanazar"

    sr = ModuleType("speech_recognition")
    sr.__spec__ = importlib.machinery.ModuleSpec("speech_recognition", loader=None)
    sr.Recognizer = Recognizer
    sr.Microphone = Microphone
    sr.WaitTimeoutError = type("WaitTimeoutError", (Exception,), {})
    sr.UnknownValueError = type("UnknownValueError", (Exception,), {})
    sr.RequestError = type("RequestError", (Exception,), {})
    pyaudio = ModuleType("pyaudio")
    pyaudio.__spec__ = importlib.machinery.ModuleSpec("pyaudio", loader=None)
    monkeypatch.setitem(sys.modules, "speech_recognition", sr)
    monkeypatch.setitem(sys.modules, "pyaudio", pyaudio)
    yield state
    assert voice.get_status() == {"phase": "idle"}
    assert voice._microphone_lock.acquire(blocking=False)
    voice._microphone_lock.release()


def assert_cancelled_capture_can_restart(state):
    assert voice.get_status() == {"phase": "idle"}
    if state.stream is not None:
        assert state.stream.closed
        assert state.stream.close_count == 1
    state.stop.clear()
    state.cancel_at = None
    assert voice.listen("kk-KZ", stop_event=state.stop) == "Музыка қос: Moldanazar"


def test_pre_cancelled_capture_never_opens_microphone(cancellable_audio):
    state = cancellable_audio
    state.stop.set()
    with pytest.raises(voice.VoiceCancelled, match="тоқтатылды"):
        voice.listen("kk-KZ", stop_event=state.stop)
    assert state.events == []
    assert state.google_count == 0
    assert_cancelled_capture_can_restart(state)


@pytest.mark.parametrize(("stage", "reads"), [
    ("before_calibrating_read", 0),
    ("during_calibrating_read", 1),
    ("before_listening_read", 1),
    ("during_listening_read", 2),
])
def test_cancel_checks_both_sides_of_stream_read_and_closes_device(cancellable_audio, stage, reads):
    state = cancellable_audio
    state.cancel_at = stage
    with pytest.raises(voice.VoiceCancelled, match="тоқтатылды"):
        voice.listen("kk-KZ", stop_event=state.stop)
    assert state.read_count == reads
    assert state.google_count == 0
    assert_cancelled_capture_can_restart(state)


def test_cancel_after_capture_prevents_any_google_request(cancellable_audio):
    state = cancellable_audio
    state.cancel_at = "before_google"
    with pytest.raises(voice.VoiceCancelled):
        voice.listen("kk-KZ", stop_event=state.stop)
    assert state.read_count == 2
    assert state.google_count == 0
    assert_cancelled_capture_can_restart(state)


def test_cancel_after_google_discards_transcription_and_releases_capture_lock(cancellable_audio):
    state = cancellable_audio
    state.cancel_at = "after_google"
    with pytest.raises(voice.VoiceCancelled):
        voice.listen("ru-RU", stop_event=state.stop)
    assert state.google_count == 1
    assert_cancelled_capture_can_restart(state)


def test_cancellation_during_inflight_recognition_discards_late_reply(cancellable_audio):
    state = cancellable_audio
    entered = threading.Event()
    release = threading.Event()
    result = []

    def hold_google():
        entered.set()
        assert release.wait(timeout=3)

    def record():
        try:
            result.append(voice.listen("kk-KZ", stop_event=state.stop))
        except Exception as exc:
            result.append(exc)

    state.google_hook = hold_google
    worker = threading.Thread(target=record)
    worker.start()
    try:
        assert entered.wait(timeout=3)
        assert voice.get_status() == {"phase": "recognizing"}
        assert state.stream.closed
        state.stop.set()
    finally:
        release.set()
        worker.join(timeout=3)
    assert not worker.is_alive()
    assert len(result) == 1 and isinstance(result[0], voice.VoiceCancelled)
    state.google_hook = None
    assert_cancelled_capture_can_restart(state)


def test_pre_cancelled_other_request_preserves_active_owner_lock_and_phase(cancellable_audio):
    state = cancellable_audio
    assert voice._microphone_lock.acquire(blocking=False)
    voice._set_phase("listening")
    try:
        state.stop.set()
        with pytest.raises(voice.VoiceCancelled):
            voice.listen("kk-KZ", stop_event=state.stop)
        assert voice.get_status() == {"phase": "listening"}
        assert not voice._microphone_lock.acquire(blocking=False)
        assert state.events == []
    finally:
        voice._set_phase("idle")
        voice._microphone_lock.release()


def test_omitting_optional_stop_event_preserves_normal_capture(cancellable_audio):
    assert voice.listen("kk-KZ") == "Музыка қос: Moldanazar"
    assert cancellable_audio.google_count == 1
    assert cancellable_audio.stream.closed
    assert voice.get_status() == {"phase": "idle"}
