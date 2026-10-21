"""Independent Hoard AI core; Telegram is only a future consumer."""
from __future__ import annotations

from .config import AIConfig, read_ai_config, write_ai_config
from .deepseek import DeepSeekProvider
from .executor import AICore
from .models import AIImage, AIMessage, AIRequest, AIResponse, AIUsage


def create_default_core(config: AIConfig | None = None) -> AICore:
    settings = config or read_ai_config()
    provider = DeepSeekProvider(
        settings.api_key,
        model=settings.model,
        timeout=settings.timeout,
    )
    return AICore({provider.name: provider}, default_provider=settings.provider)


__all__ = [
    "AIConfig",
    "AICore",
    "AIImage",
    "AIMessage",
    "AIRequest",
    "AIResponse",
    "AIUsage",
    "create_default_core",
    "read_ai_config",
    "write_ai_config",
]
