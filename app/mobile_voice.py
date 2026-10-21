"""Decode a short recording from a phone and transcribe it on the server."""

from __future__ import annotations

import subprocess

from .voice import VoiceError


MAX_UPLOAD_BYTES = 8 * 1024 * 1024
ALLOWED_AUDIO_TYPES = frozenset({"audio/mp4", "audio/x-m4a", "audio/webm", "audio/wav", "audio/wave", "audio/x-wav"})


def transcribe_upload(data: bytes, content_type: str, language: str, transcription) -> str:
    if content_type not in ALLOWED_AUDIO_TYPES:
        raise VoiceError("iPhone дыбыс пішімі қолдау таппады. Safari браузерін жаңартып, қайта көріңіз.")
    if not data or len(data) > MAX_UPLOAD_BYTES:
        raise VoiceError("Дауыс жазбасы бос немесе тым үлкен. 20 секундтан аспайтын пәрмен айтыңыз.")
    try:
        conversion = subprocess.run(
            ["ffmpeg", "-nostdin", "-hide_banner", "-loglevel", "error", "-i", "pipe:0",
             "-vn", "-sn", "-dn", "-t", "25", "-ac", "1", "-ar", "16000", "-f", "s16le", "pipe:1"],
            input=data, capture_output=True, check=False, timeout=30,
        )
    except (FileNotFoundError, subprocess.TimeoutExpired) as exc:
        raise VoiceError("Серверде дыбыс өңдеу дайын емес. FFmpeg орнатылғанын тексеріңіз.") from exc
    if conversion.returncode or not conversion.stdout or len(conversion.stdout) > 25 * 16000 * 2:
        raise VoiceError("Дауыс жазбасын оқу мүмкін болмады. Қайта жазып көріңіз.")

    import speech_recognition as sr

    audio = sr.AudioData(conversion.stdout, 16000, 2)
    status = transcription.status()
    if status["active_provider"] == "whisper":
        if not status["model_ready"]:
            raise VoiceError("Whisper моделі серверде орнатылмаған. Баптаулардан Google-ды таңдаңыз.")
        return transcription.transcribe(audio, language)
    recognizer = sr.Recognizer()
    recognizer.operation_timeout = 15
    try:
        return recognizer.recognize_google(
            audio, language=language, endpoint="https://www.google.com/speech-api/v2/recognize")[:4000]
    except sr.UnknownValueError as exc:
        raise VoiceError("Дауыс анық естілмеді. Қайта айтыңыз немесе мәтінмен жазыңыз.") from exc
    except sr.RequestError as exc:
        raise VoiceError("Дауыс тану сервисіне қосыла алмадым. Интернетті тексеріңіз.") from exc
