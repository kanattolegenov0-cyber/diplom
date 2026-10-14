"""Explicit push-to-talk capture on the Windows computer running AYAN."""
import importlib.util
import threading


class VoiceError(Exception):
    pass


_microphone_lock = threading.Lock()


def available() -> bool:
    return bool(importlib.util.find_spec("speech_recognition") and importlib.util.find_spec("pyaudio"))


def listen(language: str) -> str:
    if language not in {"kk-KZ", "ru-RU"}:
        raise VoiceError("Қазақ немесе орыс тілін таңдаңыз.")
    if not available():
        raise VoiceError("Микрофон модулі орнатылмаған. Windows іске қосу нұсқаулығын пайдаланыңыз.")
    if not _microphone_lock.acquire(blocking=False):
        raise VoiceError("Микрофон қазір тыңдап жатыр. Сәл күтіңіз.")
    try:
        import speech_recognition as sr
        recognizer = sr.Recognizer()
        recognizer.operation_timeout = 10
        with sr.Microphone() as source:
            recognizer.adjust_for_ambient_noise(source, duration=0.4)
            audio = recognizer.listen(source, timeout=5, phrase_time_limit=10)
        # Send audio only after the user presses the microphone; preserve HTTPS.
        return recognizer.recognize_google(audio, language=language,
                                           endpoint="https://www.google.com/speech-api/v2/recognize")
    except sr.WaitTimeoutError as exc:
        raise VoiceError("Дауыс естілмеді. Микрофонды басып, қайта айтыңыз.") from exc
    except sr.UnknownValueError as exc:
        raise VoiceError("Сөзді түсіне алмадым. Қайта айтыңыз немесе мәтін енгізіңіз.") from exc
    except sr.RequestError as exc:
        raise VoiceError("Сөйлеуді тану сервисіне қосыла алмадым. Интернетті тексеріңіз.") from exc
    except (OSError, AttributeError) as exc:
        raise VoiceError("Микрофон ашылмады. Windows микрофон рұқсаты мен құрылғысын тексеріңіз.") from exc
    finally:
        _microphone_lock.release()
