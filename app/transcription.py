"""Optional local Whisper; capture never initiates a model download."""
from __future__ import annotations

import importlib.util
import io
from pathlib import Path
import threading

from . import voice


MODELS = {"small", "medium", "large-v3"}


class TranscriptionService:
    VoiceError = voice.VoiceError
    VoiceCancelled = voice.VoiceCancelled
    _microphone_lock = voice._microphone_lock

    def __init__(self, preferences, directory: Path):
        self.preferences = preferences
        self.directory = directory / "models"
        self._lock = threading.Lock()
        self._model = None
        self._loaded = None

    def path(self):
        return self.directory / ("whisper-" + self.preferences.get("stt_model", "small"))

    def status(self):
        installed = importlib.util.find_spec("faster_whisper") is not None
        path = self.path()
        ready = installed and all((path / name).is_file() for name in
                                  ("model.bin", "config.json", "tokenizer.json"))
        provider = self.preferences.get("stt_provider", "auto")
        return {"provider": provider, "model": self.preferences.get("stt_model", "small"),
                "installed": installed, "model_ready": ready,
                "active_provider": "whisper" if provider == "whisper" or (provider == "auto" and ready) else "google"}

    def listen(self, language, *, stop_event=None):
        if self.status()["active_provider"] == "whisper":
            if not self.status()["model_ready"]:
                raise voice.VoiceError("Whisper дайын емес. setup-whisper.bat файлын іске қосыңыз немесе Google таңдаңыз.")
            return voice.listen(language, stop_event=stop_event, transcribe=self.transcribe)
        return voice.listen(language, **({"stop_event": stop_event} if stop_event is not None else {}))

    def get_status(self):
        return voice.get_status()

    @staticmethod
    def _check_cancel(stop_event):
        if stop_event is not None and stop_event.is_set():
            raise voice.VoiceCancelled("Дауыс тану тоқтатылды.")

    def transcribe(self, audio, language, *, stop_event=None):
        self._check_cancel(stop_event)
        with self._lock:
            try:
                from faster_whisper import WhisperModel
                path = self.path()
                if self._loaded != path:
                    # Drop the previous model before loading another large model.
                    self._model = None
                    self._loaded = None
                    self._model = WhisperModel(str(path), device="cpu", compute_type="int8",
                                               cpu_threads=2, local_files_only=True)
                    self._loaded = path
                self._check_cancel(stop_event)
                segments, _ = self._model.transcribe(
                    io.BytesIO(audio.get_wav_data(convert_rate=16000, convert_width=2)),
                    language={"kk-KZ": "kk", "ru-RU": "ru"}[language], task="transcribe",
                    beam_size=5, temperature=0, vad_filter=True,
                    condition_on_previous_text=False,
                )
                parts = []
                for segment in segments:
                    self._check_cancel(stop_event)
                    if segment.no_speech_prob < 0.6 and segment.avg_logprob > -1.0:
                        parts.append(segment.text.strip())
                self._check_cancel(stop_event)
                text = " ".join(parts).strip()
                if not text:
                    raise voice.VoiceError("Сөз анық танылмады. Микрофонға жақынырақ, қайта айтыңыз.")
                return text[:4000]
            except voice.VoiceError:
                raise
            except Exception as exc:
                raise voice.VoiceError("Whisper жадыға сыймады немесе іске қосылмады. Баптаулардан small моделін таңдаңыз да setup-whisper.bat small командасымен орнатыңыз, кейін Аянды қайта іске қосыңыз.") from exc

    def transcribe_wake(self, pcm, language, fallback, *, stop_event=None):
        # Background audio stays local. Google never receives wake recordings.
        if self.status()["active_provider"] != "whisper":
            return fallback
        import speech_recognition as sr
        from .wake import extract_wake_command
        voice._set_phase("recognizing")
        try:
            text = self.transcribe(sr.AudioData(pcm, 16000, 2), language, stop_event=stop_event)
        finally:
            voice._set_phase("idle")
        tail = extract_wake_command(text)
        return tail if tail is not None else text
