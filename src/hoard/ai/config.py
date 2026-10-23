"""Private AI provider configuration, separate from Telegram credentials."""
from __future__ import annotations

import json
import os
from dataclasses import dataclass, field
from pathlib import Path

from ..paths import config_dir, ensure_private
from .errors import AIConfigurationError

DEFAULT_DEEPSEEK_MODEL = "deepseek-flash"


def ai_config_file() -> Path:
    return config_dir() / "ai.json"


@dataclass(frozen=True)
class AIConfig:
    provider: str = "deepseek"
    api_key: str = field(default="", repr=False)
    model: str = DEFAULT_DEEPSEEK_MODEL
    timeout: float = 120.0
    max_parallel: int = 2
    key_source: str = "none"

    def __post_init__(self) -> None:
        if self.provider != "deepseek":
            raise AIConfigurationError(f"Неизвестный AI-провайдер: {self.provider}")
        if not self.model.strip():
            raise AIConfigurationError("Название AI-модели не может быть пустым")
        if not 1 <= self.timeout <= 600:
            raise AIConfigurationError("Таймаут AI должен быть от 1 до 600 секунд")
        if not 1 <= self.max_parallel <= 100:
            raise AIConfigurationError("Параллельность AI должна быть от 1 до 100")
        if self.key_source not in {"env", "file", "none"}:
            raise AIConfigurationError("Неизвестный источник API-ключа")

    @property
    def configured(self) -> bool:
        return bool(self.api_key.strip())


def read_ai_config() -> AIConfig:
    data: dict = {}
    try:
        loaded = json.loads(ai_config_file().read_text(encoding="utf-8"))
        if isinstance(loaded, dict):
            data = loaded
    except FileNotFoundError:
        pass
    except (OSError, json.JSONDecodeError, TypeError) as exc:
        raise AIConfigurationError("Не удалось прочитать конфигурацию AI") from exc

    deepseek = data.get("deepseek", {})
    if not isinstance(deepseek, dict):
        raise AIConfigurationError("Раздел deepseek в ai.json должен быть объектом")
    environment_key = os.environ.get("DEEPSEEK_API_KEY", "").strip()
    file_key = str(deepseek.get("api_key", "")).strip()
    api_key = environment_key or file_key
    key_source = "env" if environment_key else "file" if file_key else "none"
    model = os.environ.get("DEEPSEEK_MODEL") or deepseek.get(
        "model", DEFAULT_DEEPSEEK_MODEL
    )
    raw_parallel = os.environ.get("HOARD_AI_MAX_JOBS", data.get("max_parallel", 2))
    raw_timeout = os.environ.get("HOARD_AI_TIMEOUT", deepseek.get("timeout", 120))
    try:
        return AIConfig(
            provider=str(data.get("provider", "deepseek")),
            api_key=str(api_key),
            model=str(model),
            timeout=float(raw_timeout),
            max_parallel=int(raw_parallel),
            key_source=key_source,
        )
    except (TypeError, ValueError) as exc:
        raise AIConfigurationError("Неверные значения в конфигурации AI") from exc


def write_ai_config(config: AIConfig) -> None:
    """Atomically store provider settings in a user-only file."""
    if not config.configured:
        raise AIConfigurationError("Укажи API-ключ DeepSeek")
    ensure_private(config_dir())
    path = ai_config_file()
    temp = path.with_suffix(".tmp")
    payload = {
        "provider": config.provider,
        "max_parallel": config.max_parallel,
        "deepseek": {
            "api_key": config.api_key.strip(),
            "model": config.model,
            "timeout": config.timeout,
        },
    }
    fd = os.open(temp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as stream:
            json.dump(payload, stream, ensure_ascii=False, indent=2)
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temp, path)
        path.chmod(0o600)
    finally:
        temp.unlink(missing_ok=True)
