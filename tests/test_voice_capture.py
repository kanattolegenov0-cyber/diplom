"""Exercise the installed speech detector with local PCM, without a microphone/network."""
import audioop
import math
import struct
import wave

import speech_recognition as sr

from app import voice


def test_real_detector_captures_signal_after_quiet_calibration(tmp_path, monkeypatch):
    path = tmp_path / "input.wav"
    sample_rate = 16000
    silence = b"\x00\x00" * sample_rate
    signal = b"".join(struct.pack("<h", int(2500 * math.sin(2 * math.pi * 440 * n / sample_rate)))
                      for n in range(sample_rate))
    with wave.open(str(path), "wb") as audio:
        audio.setnchannels(1)
        audio.setsampwidth(2)
        audio.setframerate(sample_rate)
        audio.writeframes(silence + signal + silence * 2)
    monkeypatch.setattr(voice, "available", lambda: True)
    monkeypatch.setattr(sr, "Microphone", lambda: sr.AudioFile(str(path)))
    captured = []

    def recognize(self, audio, language, endpoint):
        assert voice.get_status() == {"phase": "recognizing"}
        assert language == "kk-KZ" and endpoint.startswith("https://")
        captured.append(audio.get_raw_data())
        return "Көмек"

    monkeypatch.setattr(sr.Recognizer, "recognize_google", recognize)
    assert voice.listen("kk-KZ") == "Көмек"
    assert len(captured) == 1 and audioop.rms(captured[0], 2) > 1000
    assert voice.get_status() == {"phase": "idle"}
