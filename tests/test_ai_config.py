import json
import os
import stat

import pytest

from hoard.ai.config import AIConfig, ai_config_file, read_ai_config, write_ai_config


def test_ai_config_is_private_and_environment_key_wins(monkeypatch, tmp_path):
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path))
    write_ai_config(AIConfig(api_key="file-secret", max_parallel=4))

    path = ai_config_file()
    assert json.loads(path.read_text())["deepseek"]["api_key"] == "file-secret"
    if os.name != "nt":
        assert stat.S_IMODE(path.stat().st_mode) == 0o600
    assert "file-secret" not in repr(read_ai_config())
    assert read_ai_config().key_source == "file"

    monkeypatch.setenv("DEEPSEEK_API_KEY", "environment-secret")
    loaded = read_ai_config()
    assert loaded.api_key == "environment-secret"
    assert loaded.model == "deepseek-flash"
    assert loaded.max_parallel == 4
    assert loaded.key_source == "env"


def test_invalid_ai_config_does_not_expose_secret(monkeypatch, tmp_path):
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path))
    monkeypatch.setenv("DEEPSEEK_API_KEY", "never-print-this")
    monkeypatch.setenv("HOARD_AI_MAX_JOBS", "invalid")

    with pytest.raises(Exception) as caught:
        read_ai_config()

    assert "never-print-this" not in str(caught.value)
