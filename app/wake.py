"""Local-only wake recognition; cloud recognition starts after an explicit wake."""

from __future__ import annotations

from collections import deque
import inspect
import json
import re
import threading
import time
from typing import Any


_WAKE_WORD = re.compile(r"(?<!\w)(?:аян|айан)(?!\w)", re.IGNORECASE)
_SAMPLE_RATE = 16000
_CHUNK = 4000


def extract_wake_command(text: str) -> str | None:
    """Return the command tail, an empty wake-only tail, or None for no wake."""
    if not isinstance(text, str):
        return None
    match = _WAKE_WORD.search(text)
    if match is None:
        return None
    tail = text[match.end():].strip(" \t\r\n,.:;!?—–-")
    # A repeated name remains one activation rather than a spurious command.
    while tail:
        repeated = _WAKE_WORD.match(tail)
        if repeated is None:
            break
        tail = tail[repeated.end():].strip(" \t\r\n,.:;!?—–-")
    return tail


class WakeService:
    """Own one microphone worker and expose bounded events for the local UI.

    Vosk sees background PCM locally. Only a wake-only final utterance invokes
    voice.listen; Whisper can re-transcribe the bounded same-utterance audio locally.
    A command automatically pauses capture until set_busy(False) acknowledges
    completion. Model installation is always an explicit, separate operation.
    """

    def __init__(self, preferences: Any, voice_module: Any, model_manager: Any) -> None:
        self.preferences = preferences
        self.voice = voice_module
        self.models = model_manager
        self._state_lock = threading.RLock()
        self._control_lock = threading.Lock()
        self._stop_event = threading.Event()
        self._busy = threading.Event()
        self._capture_cancel = threading.Event()
        self._thread: threading.Thread | None = None
        self._stream: Any = None
        self._enabled = False
        self._phase = "stopped"
        self._error: str | None = None
        self._pending_command = False
        self._in_command = False
        self._events: deque[dict[str, Any]] = deque(maxlen=64)
        self._event_id = 0
        self._last_wake: tuple[str, float] | None = None
        self._language = "kk-KZ"

    def _emit(self, kind: str, text: str) -> None:
        with self._state_lock:
            self._event_id += 1
            self._events.append({"id": self._event_id, "type": kind, "text": text})

    def _phase_is(self, phase: str) -> None:
        with self._state_lock:
            if self._enabled:
                self._phase = phase

    def status(self, since: int = 0) -> dict[str, Any]:
        try:
            model_ready = bool(self.models.ready())
        except Exception:
            model_ready = False
        with self._state_lock:
            phase = self._phase
            if self._enabled and self._in_command and not self._busy.is_set():
                try:
                    voice_phase = self.voice.get_status().get("phase", "idle")
                except (AttributeError, TypeError):
                    voice_phase = "idle"
                phase = {"calibrating": "calibrating", "listening": "command",
                         "recognizing": "recognizing"}.get(voice_phase, "calibrating")
            output: dict[str, Any] = {
                "enabled": self._enabled, "phase": phase, "model_ready": model_ready,
                "events": [dict(event) for event in self._events if event["id"] > since],
            }
            if self._error:
                output["error"] = self._error
            return output

    def start(self, language: str = "kk-KZ") -> dict[str, Any]:
        with self._control_lock:
            with self._state_lock:
                if self._thread is not None and self._thread.is_alive():
                    return self.status()
                self._error = None
                if language not in {"kk-KZ", "ru-RU"}:
                    self._phase = "error"
                    self._error = "Қазақ немесе орыс тілін таңдаңыз."
                    return self.status()
                if not self.models.ready() or self.models.path() is None:
                    self._enabled = False
                    self._phase = "model_missing"
                    self._error = "«Аян» деп шақыру үшін баптаулардан жергілікті қазақша Vosk моделін орнатыңыз."
                    return self.status()
                self._language = language
                self._stop_event.clear()
                self._capture_cancel.clear()
                self._pending_command = False
                self._in_command = False
                self._enabled = True
                self._phase = "paused" if self._busy.is_set() else "calibrating"
                self._thread = threading.Thread(target=self._run, name="ayan-local-wake", daemon=True)
                self._thread.start()
            return self.status()

    def _close_stream(self) -> None:
        with self._state_lock:
            stream, self._stream = self._stream, None
        if stream is not None:
            try:
                stream.stop_stream()
            except Exception:
                pass
            try:
                stream.close()
            except Exception:
                pass

    def stop(self) -> dict[str, Any]:
        with self._control_lock:
            self._stop_event.set()
            self._capture_cancel.set()
            with self._state_lock:
                self._enabled = False
                self._pending_command = False
                thread = self._thread
            self._close_stream()
            if thread is not None and thread is not threading.current_thread():
                thread.join(timeout=3)
            with self._state_lock:
                if thread is not None and thread.is_alive():
                    self._phase = "error"
                    self._error = "Микрофон әлі тоқтатылып жатыр. Бірнеше секундтан кейін қайта тексеріңіз."
                else:
                    self._thread = None
                    self._phase = "stopped"
                    self._error = None
                    self._in_command = False
            return self.status()

    def set_busy(self, busy: bool) -> None:
        with self._state_lock:
            if busy:
                self._busy.set()
                self._capture_cancel.set()
                if self._enabled:
                    self._phase = "paused"
            else:
                self._busy.clear()
                self._pending_command = False
                if not self._stop_event.is_set():
                    self._capture_cancel.clear()
        if busy:
            self._close_stream()

    def _paused(self) -> bool:
        with self._state_lock:
            return self._busy.is_set() or self._pending_command

    def _listen_command(self) -> str:
        # Newer voice backends support cooperative cancellation; retaining the
        # one-argument API keeps existing personal installations compatible.
        parameters = inspect.signature(self.voice.listen).parameters
        if "stop_event" in parameters:
            return self.voice.listen(self._language, stop_event=self._capture_cancel)
        if "cancel_event" in parameters:
            return self.voice.listen(self._language, cancel_event=self._capture_cancel)
        return self.voice.listen(self._language)

    def _run(self) -> None:
        audio: Any = None
        microphone_owned = False

        def release_capture() -> None:
            nonlocal microphone_owned
            self._close_stream()
            if microphone_owned:
                self.voice._microphone_lock.release()
                microphone_owned = False

        try:
            import pyaudio
            import vosk

            path = self.models.path()
            if path is None:
                raise RuntimeError("Missing local model")
            if hasattr(vosk, "SetLogLevel"):
                vosk.SetLogLevel(-1)
            model = vosk.Model(str(path))
            audio = pyaudio.PyAudio()
            recognizer: Any = None
            utterance = bytearray()
            while not self._stop_event.is_set():
                if self._paused():
                    release_capture()
                    self._phase_is("paused")
                    self._stop_event.wait(0.05)
                    continue
                if not microphone_owned:
                    if not self.voice._microphone_lock.acquire(blocking=False):
                        self._phase_is("paused")
                        self._stop_event.wait(0.05)
                        continue
                    microphone_owned = True
                    self._phase_is("calibrating")
                    try:
                        stream = audio.open(
                            format=pyaudio.paInt16, channels=1, rate=_SAMPLE_RATE,
                            input=True, frames_per_buffer=_CHUNK,
                        )
                        with self._state_lock:
                            self._stream = stream
                        recognizer = vosk.KaldiRecognizer(model, _SAMPLE_RATE)
                        utterance.clear()
                    except Exception:
                        release_capture()
                        raise
                    if self._stop_event.is_set() or self._paused():
                        release_capture()
                        continue
                    self._phase_is("waiting")
                with self._state_lock:
                    stream = self._stream
                if stream is None:
                    release_capture()
                    continue
                try:
                    pcm = stream.read(_CHUNK, exception_on_overflow=False)
                except Exception:
                    if self._stop_event.is_set() or self._paused():
                        release_capture()
                        continue
                    raise
                if self._stop_event.is_set() or self._paused():
                    continue
                if not pcm:
                    continue
                utterance.extend(pcm)
                # Keep at most 25 seconds in memory, never write wake audio to disk.
                del utterance[:-_SAMPLE_RATE * 2 * 25]
                if not recognizer.AcceptWaveform(pcm):
                    continue
                command_audio = bytes(utterance)
                utterance.clear()
                transcript = json.loads(recognizer.Result()).get("text", "")
                tail = extract_wake_command(transcript)
                if tail is None:
                    continue
                normalized = " ".join(transcript.lower().split())
                now = time.monotonic()
                if self._last_wake and self._last_wake[0] == normalized and now - self._last_wake[1] < 2:
                    continue
                self._last_wake = (normalized, now)
                # Close/release the wake stream before voice.listen acquires the
                # same microphone lock. Background PCM is never sent to Google.
                release_capture()
                self._emit("wake", "Аян")
                if tail and not hasattr(self.voice, "transcribe_wake"):
                    command = tail
                else:
                    with self._state_lock:
                        self._in_command = True
                        self._phase = "recognizing" if tail else "calibrating"
                    try:
                        command = (self.voice.transcribe_wake(command_audio, self._language, tail,
                                                             stop_event=self._capture_cancel)
                                   if tail else self._listen_command())
                    except Exception as exc:
                        if self._stop_event.is_set() or self._busy.is_set():
                            continue
                        cancelled = getattr(self.voice, "VoiceCancelled", None)
                        if cancelled is not None and isinstance(exc, cancelled):
                            # A pause may already have been cleared while the
                            # cancellable recognizer was finishing its cleanup.
                            continue
                        voice_error = getattr(self.voice, "VoiceError", None)
                        if voice_error is not None and isinstance(exc, voice_error):
                            # Silence, unclear speech, or a temporary Google
                            # failure ends this activation, not local wake mode.
                            # A fresh local wake is required before another call.
                            self._last_wake = None
                            self._emit("error", str(exc))
                            continue
                        raise
                    finally:
                        with self._state_lock:
                            self._in_command = False
                if self._stop_event.is_set() or self._busy.is_set():
                    continue
                command = command.strip() if isinstance(command, str) else ""
                if not command:
                    self._last_wake = None
                    self._emit("error", "Сөзді түсіне алмадым. «Аян» деп қайта шақырып, пәрменді айтыңыз.")
                    continue
                with self._state_lock:
                    self._pending_command = True
                    self._phase = "paused"
                self._emit("command", command)
        except Exception as exc:
            if not self._stop_event.is_set():
                voice_error = getattr(self.voice, "VoiceError", None)
                message = (str(exc) if voice_error is not None and isinstance(exc, voice_error) else
                           "«Аян» деп шақыру іске қосылмады. Vosk, PyAudio, микрофон рұқсатын және кіріс құрылғысын тексеріңіз.")
                with self._state_lock:
                    self._error = message
                    self._enabled = False
                    self._phase = "error"
                self._emit("error", message)
        finally:
            release_capture()
            if audio is not None:
                try:
                    audio.terminate()
                except Exception:
                    pass
            with self._state_lock:
                self._in_command = False
                if self._stop_event.is_set():
                    self._enabled = False
                    self._phase = "stopped"
                    self._error = None

    def close(self) -> None:
        self.stop()
