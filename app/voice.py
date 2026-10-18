"""Explicit push-to-talk capture on the Windows computer running AYAN."""
import importlib.util
import threading
from urllib.error import URLError


class VoiceError(Exception):
    pass


class VoiceCancelled(VoiceError):
    """Capture was stopped intentionally; do not treat it as a recognition error."""


class _CancellableStream:
    def __init__(self, stream, stop_event):
        self.stream = stream
        self.stop_event = stop_event

    def read(self, *args, **kwargs):
        if self.stop_event.is_set():
            raise VoiceCancelled("Микрофон тыңдауы тоқтатылды.")
        data = self.stream.read(*args, **kwargs)
        if self.stop_event.is_set():
            raise VoiceCancelled("Микрофон тыңдауы тоқтатылды.")
        return data

    def __getattr__(self, name):
        return getattr(self.stream, name)


_microphone_lock = threading.Lock()
_phase_lock = threading.Lock()
_phase = "idle"


def get_status() -> dict:
    """Report readiness without opening a microphone or importing audio drivers."""
    with _phase_lock:
        return {"phase": _phase}


def _set_phase(phase: str) -> None:
    global _phase
    with _phase_lock:
        _phase = phase


def available() -> bool:
    return bool(importlib.util.find_spec("speech_recognition") and importlib.util.find_spec("pyaudio"))


def listen(language: str, *, stop_event=None) -> str:
    if language not in {"kk-KZ", "ru-RU"}:
        raise VoiceError("Қазақ немесе орыс тілін таңдаңыз.")
    if not available():
        raise VoiceError("Микрофон модулі орнатылмаған. Windows іске қосу нұсқаулығын пайдаланыңыз.")
    if stop_event is not None and stop_event.is_set():
        raise VoiceCancelled("Микрофон тыңдауы тоқтатылды.")
    if not _microphone_lock.acquire(blocking=False):
        raise VoiceError("Микрофон қазір тыңдап жатыр. Сәл күтіңіз.")
    try:
        try:
            import speech_recognition as sr
        except ImportError as exc:
            raise VoiceError("Микрофон модулі орнатылмаған. Windows іске қосу нұсқаулығын пайдаланыңыз.") from exc
        recognizer = sr.Recognizer()
        recognizer.operation_timeout = 10
        _set_phase("calibrating")
        try:
            with sr.Microphone() as source:
                # Microphone.__enter__ can suppress an open error and leave no stream.
                if source.stream is None:
                    raise VoiceError("Микрофон ашылмады. Windows-та әдепкі кіріс құрылғысын және микрофон рұқсатын тексеріңіз.")
                if stop_event is not None:
                    source.stream = _CancellableStream(source.stream, stop_event)
                # The UI asks for silence until calibration completes and says “Айтыңыз”.
                recognizer.adjust_for_ambient_noise(source, duration=1.0)
                _set_phase("listening")
                audio = recognizer.listen(source, timeout=8, phrase_time_limit=12)
        except sr.WaitTimeoutError as exc:
            raise VoiceError(
                "Дауыс естілмеді. Windows → Параметрлер → Жүйе → Дыбыс → Кіріс бөлімінде "
                "дұрыс әдепкі микрофонды таңдап, кіріс дыбыс деңгейін тексеріңіз. "
                "Қайта басып, «Айтыңыз» белгісі шыққаннан кейін сөйлеңіз."
            ) from exc
        except (OSError, AttributeError) as exc:
            raise VoiceError("Микрофон ашылмады. Windows микрофон рұқсатын, әдепкі кіріс құрылғысын және PyAudio орнатылуын тексеріңіз.") from exc
        _set_phase("recognizing")
        if stop_event is not None and stop_event.is_set():
            raise VoiceCancelled("Микрофон тыңдауы тоқтатылды.")
        # Explicit push-to-talk or a local wake activation precedes this call.
        try:
            result = recognizer.recognize_google(audio, language=language,
                                                endpoint="https://www.google.com/speech-api/v2/recognize")
            if stop_event is not None and stop_event.is_set():
                raise VoiceCancelled("Микрофон тыңдауы тоқтатылды.")
            return result
        except sr.UnknownValueError as exc:
            raise VoiceError("Сөзді түсіне алмадым. Қайта айтыңыз немесе мәтін енгізіңіз.") from exc
        except (sr.RequestError, TimeoutError, ConnectionError, URLError) as exc:
            raise VoiceError("Сөйлеуді тану сервисіне қосыла алмадым. Интернетті тексеріңіз.") from exc
        except OSError as exc:
            raise VoiceError("Дыбысты FLAC форматына түрлендіру іске аспады. Windows-та SpeechRecognition және FLAC түрлендіргішінің орнатылуын тексеріңіз.") from exc
    finally:
        _set_phase("idle")
        _microphone_lock.release()
