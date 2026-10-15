import json
import os

import pytest

from app.preferences import Preferences, SettingsError


def test_defaults_use_kazakh_voice_and_have_no_credentials(tmp_path, monkeypatch):
    for name in ["AYAN_LLM_API_KEY", "OPENAI_API_KEY", "AYAN_ELEVENLABS_API_KEY", "ELEVENLABS_API_KEY"]:
        monkeypatch.delenv(name, raising=False)
    public = Preferences(tmp_path).public()
    assert public["tts_voice"] == "kk-KZ-DauletNeural"
    assert public["llm_model"] == "gpt-4.1-mini"
    assert not public["llm_configured"]


def test_settings_persist_and_never_return_key(tmp_path):
    preferences = Preferences(tmp_path)
    response = preferences.update({"llm_api_key": "fake-key-for-local-tests", "tts_voice": "kk-KZ-AigulNeural"})
    assert response["llm_configured"]
    assert "fake-key-for-local-tests" not in json.dumps(response)
    assert "llm_api_key" not in response
    restored = Preferences(tmp_path)
    assert restored.secret("llm_api_key") == "fake-key-for-local-tests"
    assert restored.get("tts_voice") == "kk-KZ-AigulNeural"
    if os.name != "nt":
        assert preferences.path.stat().st_mode & 0o777 == 0o600


def test_blank_key_preserves_and_clear_removes_saved_key(tmp_path, monkeypatch):
    monkeypatch.delenv("AYAN_LLM_API_KEY", raising=False)
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    preferences = Preferences(tmp_path)
    preferences.update({"llm_api_key": "fake-key-for-local-tests"})
    preferences.update({"llm_api_key": "   ", "llm_model": "gpt-4.1"})
    assert preferences.secret("llm_api_key") == "fake-key-for-local-tests"
    preferences.update({"clear_llm_api_key": True})
    assert preferences.secret("llm_api_key") is None


def test_existing_environment_binding_is_reused(tmp_path, monkeypatch):
    monkeypatch.setenv("AYAN_LLM_API_KEY", "fake-environment-key-for-tests")
    preferences = Preferences(tmp_path)
    assert preferences.secret("llm_api_key") == "fake-environment-key-for-tests"
    preferences.update({"llm_api_key": "fake-saved-key-for-tests"})
    assert preferences.secret("llm_api_key") == "fake-saved-key-for-tests"


@pytest.mark.parametrize("changes", [
    {"llm_model": "https://untrusted.example/model"},
    {"tts_voice": "A-Fictional-Actor"},
    {"tts_provider": "arbitrary-shell"},
    {"llm_enabled": "yes"},
    {"elevenlabs_voice_id": "../../other-endpoint"},
    {"elevenlabs_model": "unsupported-model"},
    {"unknown_setting": "value"},
])
def test_bad_preferences_do_not_overwrite_saved_data(tmp_path, changes):
    preferences = Preferences(tmp_path)
    preferences.update({"tts_voice": "kk-KZ-AigulNeural"})
    original = preferences.path.read_bytes()
    with pytest.raises(SettingsError):
        preferences.update(changes)
    assert preferences.path.read_bytes() == original


def test_windows_secret_encoding_can_roundtrip_with_platform_adapter(tmp_path, monkeypatch):
    # Exercise the secret representation without claiming to run Windows DPAPI.
    preferences = Preferences(tmp_path)
    import app.preferences as module
    calls = []

    def protect(data, *, decrypt=False):
        calls.append(decrypt)
        return data[4:] if decrypt else b"TEST" + data

    monkeypatch.setattr(module, "_windows_protect", protect)
    monkeypatch.setattr(module.os, "name", "nt")
    public = preferences.update({"llm_api_key": "fake-key-for-dpapi-adapter-test"})
    assert public["secret_storage"] == "windows-dpapi"
    assert preferences.secrets["llm_api_key"]["storage"] == "dpapi"
    assert preferences.secret("llm_api_key") == "fake-key-for-dpapi-adapter-test"
    assert calls[0] is False and True in calls
