"""DeepSeek Chat Completions provider with no automatic paid retries."""
from __future__ import annotations

import asyncio
import json
import urllib.error
import urllib.request
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any, Protocol

from .. import __version__
from .config import DEFAULT_DEEPSEEK_MODEL
from .errors import (
    AIAuthenticationError,
    AIConfigurationError,
    AIConnectionError,
    AIInsufficientBalanceError,
    AIProviderUnavailableError,
    AIRateLimitError,
    AIRequestError,
    AIResponseError,
)
from .models import AIMessage, AIRequest, AIResponse, AIUsage
from .provider import AIProvider

API_URL = "https://api.deepseek.com/chat/completions"
MAX_REQUEST_BYTES = 48 * 1024 * 1024
MAX_RESPONSE_BYTES = 16 * 1024 * 1024


@dataclass(frozen=True)
class HTTPResponse:
    status: int
    body: bytes
    headers: Mapping[str, str]


class JSONTransport(Protocol):
    async def post_json(
        self,
        url: str,
        *,
        headers: Mapping[str, str],
        body: bytes,
        timeout: float,
    ) -> HTTPResponse: ...


class UrllibTransport:
    """Small stdlib transport; blocking I/O stays outside the daemon event loop."""

    async def post_json(
        self,
        url: str,
        *,
        headers: Mapping[str, str],
        body: bytes,
        timeout: float,
    ) -> HTTPResponse:
        try:
            return await asyncio.to_thread(
                self._post_json,
                url,
                headers=dict(headers),
                body=body,
                timeout=timeout,
            )
        except TimeoutError as exc:
            raise AIConnectionError("Таймаут соединения с DeepSeek") from exc
        except urllib.error.URLError as exc:
            raise AIConnectionError("Не удалось подключиться к DeepSeek") from exc
        except OSError as exc:
            raise AIConnectionError("Ошибка сети при обращении к DeepSeek") from exc

    @staticmethod
    def _post_json(
        url: str,
        *,
        headers: Mapping[str, str],
        body: bytes,
        timeout: float,
    ) -> HTTPResponse:
        request = urllib.request.Request(url, data=body, headers=dict(headers), method="POST")
        try:
            response = urllib.request.urlopen(request, timeout=timeout)
        except urllib.error.HTTPError as exc:
            response = exc
        with response:
            payload = response.read(MAX_RESPONSE_BYTES + 1)
            if len(payload) > MAX_RESPONSE_BYTES:
                raise AIResponseError("Ответ DeepSeek превышает безопасный размер")
            return HTTPResponse(
                status=int(response.status),
                body=payload,
                headers=dict(response.headers.items()),
            )


class DeepSeekProvider(AIProvider):
    name = "deepseek"

    def __init__(
        self,
        api_key: str,
        *,
        model: str = DEFAULT_DEEPSEEK_MODEL,
        timeout: float = 120,
        transport: JSONTransport | None = None,
    ) -> None:
        self._api_key = api_key.strip()
        self.model = model
        self.timeout = timeout
        self.transport = transport or UrllibTransport()

    @property
    def configured(self) -> bool:
        return bool(self._api_key)

    async def complete(self, request: AIRequest) -> AIResponse:
        if not self.configured:
            # AICore normally catches this first; direct provider calls remain safe.
            raise AIConfigurationError("API-ключ DeepSeek не настроен")
        payload = self._payload(request)
        body = json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
        if len(body) > MAX_REQUEST_BYTES:
            raise AIRequestError("Запрос превышает лимит DeepSeek 48 MiB")
        response = await self.transport.post_json(
            API_URL,
            headers={
                "Authorization": f"Bearer {self._api_key}",
                "Content-Type": "application/json",
                "Accept": "application/json",
                "User-Agent": f"Hoard/{__version__}",
            },
            body=body,
            timeout=self.timeout,
        )
        if response.status < 200 or response.status >= 300:
            try:
                data = self._decode(response.body)
            except AIResponseError:
                data = {}
            self._raise_http_error(response.status, data)
        data = self._decode(response.body)
        try:
            return self._response(data, fallback_model=str(payload["model"]))
        except AIResponseError as exc:
            # Keep billable metadata even when the provider's content envelope is
            # malformed. Do not retain the raw response or rejected content.
            try:
                usage = self._usage(data.get("usage"))
            except AIResponseError:
                usage = None
            choices = data.get("choices")
            choice = choices[0] if isinstance(choices, list) and choices else {}
            finish_reason = choice.get("finish_reason") if isinstance(choice, dict) else ""
            exc.provider_response = AIResponse(
                provider="deepseek",
                model=str(data.get("model") or payload["model"]),
                content="",
                finish_reason=str(finish_reason or ""),
                request_id=str(data.get("id") or ""),
                usage=usage,
            )
            raise

    def _payload(self, request: AIRequest) -> dict[str, Any]:
        if request.response_format == "json":
            prompt = "\n".join(message.content for message in request.messages)
            if "json" not in prompt.casefold():
                raise AIRequestError(
                    "Для JSON Output инструкция должна явно содержать слово JSON"
                )
        payload: dict[str, Any] = {
            "model": request.model or self.model,
            "messages": [self._message(message) for message in request.messages],
            "stream": False,
        }
        if request.response_format == "json":
            payload["response_format"] = {"type": "json_object"}
        if request.max_tokens is not None:
            payload["max_tokens"] = request.max_tokens
        if request.temperature is not None:
            payload["temperature"] = request.temperature
        return payload

    @staticmethod
    def _message(message: AIMessage) -> dict[str, Any]:
        if not message.images:
            return {"role": message.role, "content": message.content}
        parts: list[dict[str, Any]] = []
        if message.content:
            parts.append({"type": "text", "text": message.content})
        parts.extend(
            {
                "type": "image_url",
                "image_url": {"url": image.data_url(), "detail": image.detail},
            }
            for image in message.images
        )
        return {"role": message.role, "content": parts}

    @staticmethod
    def _decode(body: bytes) -> dict[str, Any]:
        try:
            value = json.loads(body.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise AIResponseError("DeepSeek вернул ответ не в формате JSON") from exc
        if not isinstance(value, dict):
            raise AIResponseError("DeepSeek вернул ответ неожиданного формата")
        return value

    @staticmethod
    def _error_message(data: dict[str, Any]) -> str:
        error = data.get("error")
        if isinstance(error, dict) and isinstance(error.get("message"), str):
            return error["message"][:300]
        return "DeepSeek отклонил запрос"

    @classmethod
    def _raise_http_error(cls, status: int, data: dict[str, Any]) -> None:
        provider_message = cls._error_message(data)
        if status == 401:
            raise AIAuthenticationError("DeepSeek отклонил API-ключ")
        if status == 402:
            raise AIInsufficientBalanceError("Недостаточно средств на балансе DeepSeek")
        if status == 429:
            raise AIRateLimitError("DeepSeek ограничил частоту или параллельность запросов")
        if status in {400, 422}:
            raise AIRequestError(f"DeepSeek отклонил параметры: {provider_message}")
        if status >= 500:
            raise AIProviderUnavailableError(f"DeepSeek временно недоступен ({status})")
        raise AIResponseError(f"Неожиданный ответ DeepSeek: HTTP {status}")

    @staticmethod
    def _response(data: dict[str, Any], *, fallback_model: str) -> AIResponse:
        choices = data.get("choices")
        if not isinstance(choices, list) or not choices or not isinstance(choices[0], dict):
            raise AIResponseError("В ответе DeepSeek нет результата")
        choice = choices[0]
        message = choice.get("message")
        if not isinstance(message, dict):
            raise AIResponseError("В ответе DeepSeek нет сообщения")
        content = message.get("content")
        finish_reason = choice.get("finish_reason")
        if content is not None and not isinstance(content, str):
            raise AIResponseError("Текст ответа DeepSeek имеет неверный формат")
        if not isinstance(finish_reason, str):
            raise AIResponseError("DeepSeek не указал finish_reason")
        model = data.get("model")
        request_id = data.get("id")
        return AIResponse(
            provider="deepseek",
            model=str(model) if model else fallback_model,
            content=content or "",
            finish_reason=finish_reason,
            request_id=str(request_id) if request_id else "",
            usage=DeepSeekProvider._usage(data.get("usage")),
        )

    @staticmethod
    def _usage(value: Any) -> AIUsage | None:
        if value is None:
            return None
        if not isinstance(value, dict):
            raise AIResponseError("DeepSeek вернул usage неожиданного формата")

        def counter(container: dict[str, Any], name: str) -> int | None:
            raw = container.get(name)
            if raw is None:
                return None
            if not isinstance(raw, int) or isinstance(raw, bool) or raw < 0:
                raise AIResponseError(f"DeepSeek вернул некорректный usage: {name}")
            return raw

        prompt_details = value.get("prompt_tokens_details")
        if not isinstance(prompt_details, dict):
            prompt_details = {}
        completion_details = value.get("completion_tokens_details")
        if not isinstance(completion_details, dict):
            completion_details = {}

        cache_hit = counter(value, "prompt_cache_hit_tokens")
        if cache_hit is None:
            cache_hit = counter(prompt_details, "cached_tokens")
        cache_miss = counter(value, "prompt_cache_miss_tokens")
        if cache_miss is None:
            cache_miss = counter(prompt_details, "cache_miss_tokens")

        return AIUsage(
            prompt_tokens=counter(value, "prompt_tokens"),
            completion_tokens=counter(value, "completion_tokens"),
            total_tokens=counter(value, "total_tokens"),
            prompt_cache_hit_tokens=cache_hit,
            prompt_cache_miss_tokens=cache_miss,
            reasoning_tokens=counter(completion_details, "reasoning_tokens"),
        )
