"""Provider-neutral request and response values for Hoard AI."""
from __future__ import annotations

import base64
import binascii
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from .errors import AIRequestError, AIResponseError

MAX_IMAGE_BYTES = 32 * 1024 * 1024
MAX_IMAGES = 600
_DETAILS = {"auto", "low", "high", "original"}
_ROLES = {"system", "user", "assistant"}


def _detect_media_type(data: bytes) -> str:
    if data.startswith(b"\xff\xd8\xff"):
        return "image/jpeg"
    if data.startswith(b"\x89PNG\r\n\x1a\n"):
        return "image/png"
    if data.startswith((b"GIF87a", b"GIF89a")):
        return "image/gif"
    if len(data) >= 12 and data.startswith(b"RIFF") and data[8:12] == b"WEBP":
        return "image/webp"
    raise AIRequestError("Поддерживаются только JPEG, PNG, GIF и WebP")


@dataclass(frozen=True)
class AIImage:
    """An explicitly selected image; raw bytes are excluded from repr and logs."""

    data: bytes = field(repr=False)
    detail: str = "auto"
    media_type: str = field(init=False)

    def __post_init__(self) -> None:
        if not self.data:
            raise AIRequestError("Изображение пустое")
        if len(self.data) > MAX_IMAGE_BYTES:
            raise AIRequestError("Изображение превышает лимит DeepSeek 32 MiB")
        if self.detail not in _DETAILS:
            raise AIRequestError("detail должен быть auto, low, high или original")
        object.__setattr__(self, "media_type", _detect_media_type(self.data))

    @classmethod
    def from_file(cls, path: str | Path, *, detail: str = "auto") -> AIImage:
        """Read only the path explicitly supplied by the caller."""
        selected = Path(path)
        try:
            size = selected.stat().st_size
        except OSError as exc:
            raise AIRequestError("Не удалось прочитать выбранное изображение") from exc
        if size > MAX_IMAGE_BYTES:
            raise AIRequestError("Изображение превышает лимит DeepSeek 32 MiB")
        try:
            return cls(selected.read_bytes(), detail=detail)
        except OSError as exc:
            raise AIRequestError("Не удалось прочитать выбранное изображение") from exc

    def data_url(self) -> str:
        encoded = base64.b64encode(self.data).decode("ascii")
        return f"data:{self.media_type};base64,{encoded}"

    def to_dict(self) -> dict[str, str]:
        return {
            "data": base64.b64encode(self.data).decode("ascii"),
            "detail": self.detail,
        }

    @classmethod
    def from_dict(cls, value: dict[str, Any]) -> AIImage:
        if not isinstance(value, dict) or not isinstance(value.get("data"), str):
            raise AIRequestError("Неверное описание изображения")
        try:
            data = base64.b64decode(value["data"], validate=True)
        except (ValueError, binascii.Error) as exc:
            raise AIRequestError("Неверные данные изображения") from exc
        return cls(data, detail=str(value.get("detail", "auto")))


@dataclass(frozen=True)
class AIMessage:
    role: str
    content: str
    images: tuple[AIImage, ...] = ()

    def __post_init__(self) -> None:
        object.__setattr__(self, "images", tuple(self.images))
        if self.role not in _ROLES:
            raise AIRequestError("Роль сообщения должна быть system, user или assistant")
        if self.images and self.role != "user":
            raise AIRequestError("DeepSeek принимает изображения только в сообщениях user")
        if not self.content.strip() and not self.images:
            raise AIRequestError("Сообщение не может быть пустым")

    def to_dict(self) -> dict[str, Any]:
        return {
            "role": self.role,
            "content": self.content,
            "images": [image.to_dict() for image in self.images],
        }

    @classmethod
    def from_dict(cls, value: dict[str, Any]) -> AIMessage:
        if not isinstance(value, dict):
            raise AIRequestError("Неверное сообщение AI")
        raw_images = value.get("images", [])
        if not isinstance(raw_images, list):
            raise AIRequestError("Неверный список изображений")
        return cls(
            role=str(value.get("role", "")),
            content=str(value.get("content", "")),
            images=tuple(AIImage.from_dict(item) for item in raw_images),
        )


@dataclass(frozen=True)
class AIRequest:
    messages: tuple[AIMessage, ...]
    model: str | None = None
    response_format: str = "text"
    json_schema: dict[str, Any] | None = None
    max_tokens: int | None = None
    temperature: float | None = None

    def __post_init__(self) -> None:
        object.__setattr__(self, "messages", tuple(self.messages))
        if not self.messages:
            raise AIRequestError("Нужно хотя бы одно сообщение")
        if not all(isinstance(message, AIMessage) for message in self.messages):
            raise AIRequestError("messages должен содержать только AIMessage")
        if self.response_format not in {"text", "json"}:
            raise AIRequestError("response_format должен быть text или json")
        if self.json_schema is not None and self.response_format != "json":
            raise AIRequestError("JSON Schema можно использовать только с JSON-ответом")
        if self.model is not None and not self.model.strip():
            raise AIRequestError("Название модели не может быть пустым")
        if self.max_tokens is not None:
            if not isinstance(self.max_tokens, int) or isinstance(self.max_tokens, bool):
                raise AIRequestError("max_tokens должен быть целым числом")
            if not 1 <= self.max_tokens <= 393_216:
                raise AIRequestError("max_tokens должен быть от 1 до 393216")
        if self.temperature is not None:
            if not isinstance(self.temperature, (int, float)) or isinstance(
                self.temperature, bool
            ):
                raise AIRequestError("temperature должна быть числом")
            if not 0 <= self.temperature <= 2:
                raise AIRequestError("temperature должна быть от 0 до 2")
        images = sum(len(message.images) for message in self.messages)
        if images > MAX_IMAGES:
            raise AIRequestError("В одном запросе DeepSeek допускается не более 600 изображений")

    def to_dict(self) -> dict[str, Any]:
        return {
            "messages": [message.to_dict() for message in self.messages],
            "model": self.model,
            "response_format": self.response_format,
            "json_schema": self.json_schema,
            "max_tokens": self.max_tokens,
            "temperature": self.temperature,
        }

    @classmethod
    def from_dict(cls, value: dict[str, Any]) -> AIRequest:
        if not isinstance(value, dict) or not isinstance(value.get("messages"), list):
            raise AIRequestError("Неверный запрос AI")
        model = value.get("model")
        return cls(
            messages=tuple(AIMessage.from_dict(item) for item in value["messages"]),
            model=str(model) if model is not None else None,
            response_format=str(value.get("response_format", "text")),
            json_schema=value.get("json_schema"),
            max_tokens=value.get("max_tokens"),
            temperature=value.get("temperature"),
        )


@dataclass(frozen=True)
class AIUsage:
    """Provider-reported token counters; missing values stay unknown."""

    prompt_tokens: int | None = None
    completion_tokens: int | None = None
    total_tokens: int | None = None
    prompt_cache_hit_tokens: int | None = None
    prompt_cache_miss_tokens: int | None = None
    reasoning_tokens: int | None = None

    def __post_init__(self) -> None:
        for name in (
            "prompt_tokens",
            "completion_tokens",
            "total_tokens",
            "prompt_cache_hit_tokens",
            "prompt_cache_miss_tokens",
            "reasoning_tokens",
        ):
            value = getattr(self, name)
            if value is not None and (
                not isinstance(value, int) or isinstance(value, bool) or value < 0
            ):
                raise AIResponseError(f"Некорректный счётчик usage: {name}")

    def to_dict(self) -> dict[str, int | None]:
        return {
            "prompt_tokens": self.prompt_tokens,
            "completion_tokens": self.completion_tokens,
            "total_tokens": self.total_tokens,
            "prompt_cache_hit_tokens": self.prompt_cache_hit_tokens,
            "prompt_cache_miss_tokens": self.prompt_cache_miss_tokens,
            "reasoning_tokens": self.reasoning_tokens,
        }


@dataclass(frozen=True)
class AIResponse:
    provider: str
    model: str
    content: str
    finish_reason: str
    request_id: str = ""
    structured: Any | None = None
    usage: AIUsage | None = None
