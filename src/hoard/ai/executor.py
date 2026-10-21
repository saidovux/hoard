"""Provider-neutral execution and strict response validation."""
from __future__ import annotations

import json
from dataclasses import replace
from typing import Any

from jsonschema import Draft202012Validator
from jsonschema.exceptions import SchemaError

from .errors import (
    AIConfigurationError,
    AIIncompleteResponseError,
    AIJSONError,
    AIJSONSchemaError,
    AIResponseError,
)
from .models import AIRequest, AIResponse
from .provider import AIProvider


class AICore:
    def __init__(self, providers: dict[str, AIProvider], *, default_provider: str = "deepseek"):
        self.providers = dict(providers)
        self.default_provider = default_provider
        if default_provider not in self.providers:
            raise AIConfigurationError("AI-провайдер по умолчанию не зарегистрирован")

    def is_configured(self, provider: str | None = None) -> bool:
        selected = self.providers.get(provider or self.default_provider)
        return bool(selected and selected.configured)

    async def execute(self, request: AIRequest, *, provider: str | None = None) -> AIResponse:
        selected_name = provider or self.default_provider
        try:
            selected = self.providers[selected_name]
        except KeyError as exc:
            raise AIConfigurationError(f"AI-провайдер не зарегистрирован: {selected_name}") from exc
        if not selected.configured:
            raise AIConfigurationError("API-ключ DeepSeek не настроен")

        validator = self.validate(request)
        response = await selected.complete(request)
        try:
            if response.finish_reason != "stop":
                raise AIIncompleteResponseError(response.finish_reason or "missing")
            content = response.content.strip()
            if not content:
                raise AIResponseError("DeepSeek вернул пустой ответ")
            response = replace(response, content=content)
            if request.response_format == "json":
                response = replace(response, structured=self._parse_json(content, validator))
        except AIResponseError as exc:
            exc.provider_response = response
            raise
        return response

    @classmethod
    def validate(cls, request: AIRequest) -> Draft202012Validator | None:
        """Validate provider-neutral constraints without making a paid request."""
        return cls._schema_validator(request.json_schema)

    @staticmethod
    def _schema_validator(schema: dict[str, Any] | None) -> Draft202012Validator | None:
        if schema is None:
            return None
        if not isinstance(schema, dict):
            raise AIJSONSchemaError("JSON Schema должна быть объектом")
        AICore._reject_external_refs(schema)
        try:
            Draft202012Validator.check_schema(schema)
        except SchemaError as exc:
            raise AIJSONSchemaError("Передана некорректная JSON Schema") from exc
        return Draft202012Validator(schema)

    @staticmethod
    def _parse_json(
        content: str,
        validator: Draft202012Validator | None,
    ) -> dict[str, Any]:
        try:
            value = json.loads(content)
        except json.JSONDecodeError as exc:
            raise AIJSONError("DeepSeek вернул некорректный JSON") from exc
        if not isinstance(value, dict):
            raise AIJSONError("DeepSeek должен вернуть JSON-объект")
        if validator is not None:
            try:
                error = next(validator.iter_errors(value), None)
            except Exception as exc:
                raise AIJSONSchemaError("Не удалось применить JSON Schema") from exc
            if error is not None:
                location = ".".join(str(part) for part in error.absolute_path) or "<root>"
                raise AIJSONSchemaError(
                    f"JSON не соответствует схеме в поле {location}"
                ) from error
        return value

    @staticmethod
    def _reject_external_refs(value: Any) -> None:
        if isinstance(value, dict):
            reference = value.get("$ref")
            if isinstance(reference, str) and not reference.startswith("#"):
                raise AIJSONSchemaError("Внешние ссылки в JSON Schema запрещены")
            for child in value.values():
                AICore._reject_external_refs(child)
        elif isinstance(value, list):
            for child in value:
                AICore._reject_external_refs(child)
