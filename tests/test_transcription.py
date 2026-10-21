import asyncio
import io
import json
import sys
import threading
from types import ModuleType, SimpleNamespace

import httpx
import pytest
import speech_recognition as sr
from fastapi.testclient import TestClient

from app import voice
from app.main import create_app
from app.preferences import Preferences, SettingsError
from app.transcription import TranscriptionService
from app.voice_correction import CorrectionService, apply_dictionary, parse_dictionary


def test_dictionary_replaces_whole_phrases_once_and_preserves_names():
    dictionary = parse_dictionary("молдан азар => Moldanazar\nмузыка кос => музыка қос\nқос => аш")
    assert apply_dictionary("Молдан Азар музыка кос", dictionary) == "Moldanazar музыка қос"
    assert apply_dictionary("қосымша Qazaq Indie 123", dictionary) == "қосымша Qazaq Indie 123"
    assert apply_dictionary("AC/DC қос", parse_dictionary("AC/DC => AC/DC live")) == "AC/DC live қос"


@pytest.mark.parametrize("value", ["no separator", "=>foo", "foo=>", "a=>b=>c", "A=>b\na=>c", "x" * 6001])
def test_invalid_dictionary_rejected_without_losing_settings(tmp_path, value):
    prefs = Preferences(tmp_path)
    prefs.update({"stt_dictionary": "молдан азар => Moldanazar"})
    with pytest.raises(SettingsError):
        prefs.update({"stt_dictionary": value})
    assert Preferences(tmp_path).get("stt_dictionary") == "молдан азар => Moldanazar"


def test_voice_correction_api_does_not_execute_commands(tmp_path):
    with TestClient(create_app(tmp_path)) as client:
        assert client.post('/api/settings', json={'stt_dictionary': 'жат => жаз'}).status_code == 200
        result = client.post('/api/voice/correct', json={'text': 'Күнделікке жат: бүгін жақсы күн'}).json()
        assert result['text'] == 'Күнделікке жаз: бүгін жақсы күн'
        assert result['original'] == 'Күнделікке жат: бүгін жақсы күн'
        assert result['review'] is False
        assert client.get('/api/diary').json()['entries'] == []
        assert client.post('/api/voice/correct', json={'text': '  '}).status_code == 422
        assert client.post('/api/settings', json={'stt_model': '../bad'}).status_code == 422
        assert client.get('/api/voice/recognizer').json()['active_provider'] == 'google'


@pytest.mark.parametrize("reply, expected, changed, warning", [
    ({'text': 'Moldanazar қос'}, 'Moldanazar қос', True, False),
    ({'text': 'Молдан азар қос'}, 'Молдан азар қос', False, False),
    ({'text': ''}, 'Молдан азар қос', False, True),
    ({'text': ['invalid']}, 'Молдан азар қос', False, True),
    ({'text': 'x' * 4001}, 'Молдан азар қос', False, True),
])
@pytest.mark.parametrize('review', [False, True])
def test_ollama_correction_respects_review_setting_and_bounds(tmp_path, reply, expected, changed, warning, review):
    prefs = Preferences(tmp_path)
    prefs.update({'stt_ollama_correction': True, 'stt_review': review})
    calls = []
    def handler(request):
        assert request.url.host == '127.0.0.1'
        assert 'authorization' not in request.headers
        calls.append(request.url.path)
        if request.url.path == '/api/tags':
            return httpx.Response(200, json={'models': [{'name': prefs.get('ollama_model')}]})
        body = json.loads(request.content)
        assert body['stream'] is False
        assert body['messages'][0]['role'] == 'system'
        return httpx.Response(200, json={'done': True, 'message': {'content': json.dumps(reply)}})
    result = asyncio.run(CorrectionService(prefs, transport=httpx.MockTransport(handler)).correct('Молдан азар қос', 'kk-KZ'))
    assert result['text'] == expected and result['ai_changed'] is changed
    assert bool(result['warning']) is warning
    assert result['review'] is review
    assert calls == ['/api/tags', '/api/chat']


def test_remote_backed_ollama_is_not_sent_transcript(tmp_path):
    prefs = Preferences(tmp_path)
    prefs.update({'stt_ollama_correction': True})
    def handler(request):
        assert request.url.path == '/api/tags'
        return httpx.Response(200, json={'models': [{'name': prefs.get('ollama_model'), 'remote_host': 'cloud.example'}]})
    result = asyncio.run(CorrectionService(prefs, transport=httpx.MockTransport(handler)).correct('Мәтін', 'kk-KZ'))
    assert result['text'] == 'Мәтін' and result['warning']
    assert result['review'] is False


@pytest.mark.parametrize('review', [False, True])
def test_review_preference_is_saved_and_reused(tmp_path, review):
    with TestClient(create_app(tmp_path)) as client:
        assert client.post('/api/settings', json={'stt_review': review}).status_code == 200
    with TestClient(create_app(tmp_path)) as client:
        assert client.get('/api/settings').json()['stt_review'] is review
        result = client.post('/api/voice/correct', json={'text': 'Moldanazar қос'}).json()
        assert result['review'] is review


def test_missing_whisper_never_downloads_or_silently_uses_google(tmp_path, monkeypatch):
    prefs = Preferences(tmp_path)
    service = TranscriptionService(prefs, tmp_path)
    monkeypatch.setattr(voice, 'listen', lambda *a, **kw: 'Google result')
    assert service.listen('kk-KZ') == 'Google result'
    prefs.update({'stt_provider': 'whisper'})
    with pytest.raises(voice.VoiceError, match='Whisper дайын емес'):
        service.listen('kk-KZ')


@pytest.fixture
def whisper(tmp_path, monkeypatch):
    prefs = Preferences(tmp_path)
    prefs.update({'stt_provider': 'whisper'})
    service = TranscriptionService(prefs, tmp_path)
    state = SimpleNamespace(loads=[], calls=[], segments=[SimpleNamespace(text=' Ән қос ', no_speech_prob=0.1, avg_logprob=-0.2)])
    class Model:
        def __init__(self, path, **options):
            state.loads.append((path, options))
        def transcribe(self, audio, **options):
            assert isinstance(audio, io.BytesIO) and audio.read(4) == b'RIFF'
            state.calls.append(options)
            return iter(state.segments), object()
    module = ModuleType('faster_whisper')
    module.WhisperModel = Model
    monkeypatch.setitem(sys.modules, 'faster_whisper', module)
    return service, state


def test_whisper_language_vad_local_loading_and_cache(whisper):
    service, state = whisper
    audio = sr.AudioData(b'\0\0' * 16000, 16000, 2)
    assert service.transcribe(audio, 'kk-KZ') == 'Ән қос'
    assert service.transcribe(audio, 'ru-RU') == 'Ән қос'
    assert len(state.loads) == 1
    assert state.loads[0][1]['local_files_only'] is True
    assert state.loads[0][1]['compute_type'] == 'int8'
    assert [call['language'] for call in state.calls] == ['kk', 'ru']
    assert all(call['vad_filter'] and not call['condition_on_previous_text'] for call in state.calls)


def test_silence_and_cancellation_cannot_execute_hallucinated_text(whisper):
    service, state = whisper
    state.segments[0].no_speech_prob = 0.99
    audio = sr.AudioData(b'\0\0' * 16000, 16000, 2)
    with pytest.raises(voice.VoiceError, match='анық танылмады'):
        service.transcribe(audio, 'kk-KZ')
    cancelled = threading.Event()
    cancelled.set()
    with pytest.raises(voice.VoiceCancelled):
        service.transcribe(audio, 'kk-KZ', stop_event=cancelled)
    assert len(state.calls) == 1


def test_inline_wake_is_retranscribed_only_locally(whisper, monkeypatch):
    service, state = whisper
    monkeypatch.setattr(service, 'status', lambda: {'active_provider': 'whisper'})
    state.segments[0].text = 'Аян, Moldanazar қос'
    assert service.transcribe_wake(b'\0\0' * 16000, 'kk-KZ', 'молдан азар кос') == 'Moldanazar қос'
    assert voice.get_status() == {'phase': 'idle'}
    monkeypatch.setattr(service, 'status', lambda: {'active_provider': 'google'})
    assert service.transcribe_wake(b'unused', 'kk-KZ', 'original') == 'original'
    assert len(state.calls) == 1


@pytest.mark.parametrize('failure', [False, True])
def test_model_setup_validates_before_promoting_and_reuses_installed_model(tmp_path, monkeypatch, failure):
    from app.setup_transcription import main
    target = tmp_path / 'models' / 'whisper-small'
    calls = []
    def download(name, output_dir):
        assert name == 'small' and not target.exists()
        from pathlib import Path
        path = Path(output_dir)
        path.mkdir()
        (path / 'model.bin').write_bytes(b'fixture')
        calls.append('download')
    def model(path, **kwargs):
        assert kwargs['local_files_only'] is True
        if failure:
            raise RuntimeError('Model validation failed')
        calls.append('validate')
        return object()
    module = ModuleType('faster_whisper')
    module.download_model = download
    module.WhisperModel = model
    monkeypatch.setitem(sys.modules, 'faster_whisper', module)
    monkeypatch.setenv('AYAN_DATA_DIR', str(tmp_path))
    monkeypatch.setattr(sys, 'argv', ['setup_transcription', '--model', 'small'])
    if failure:
        with pytest.raises(RuntimeError, match='validation failed'):
            main()
        assert not target.exists()
        assert Preferences(tmp_path).get('stt_model') == 'medium'
    else:
        main()
        assert target.is_dir()
        assert Preferences(tmp_path).get('stt_model') == 'small'
        main()
        assert calls == ['download', 'validate', 'validate']
