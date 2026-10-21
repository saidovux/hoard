"""Stable, provider-neutral errors for Hoard AI."""
from __future__ import annotations


class AIError(RuntimeError):
    """Base error exposed to callers without credentials or request bodies."""

    default_code = "ai_error"
    default_retryable = False

    def __init__(
        self,
        message: str,
        *,
        code: str | None = None,
        retryable: bool | None = None,
    ) -> None:
        super().__init__(message)
        self.code = code or self.default_code
        self.retryable = self.default_retryable if retryable is None else retryable


class AIConfigurationError(AIError):
    default_code = "configuration_error"


class AIRequestError(AIError):
    default_code = "invalid_request"


class AIAuthenticationError(AIError):
    default_code = "authentication_error"


class AIInsufficientBalanceError(AIError):
    default_code = "insufficient_balance"


class AIRateLimitError(AIError):
    default_code = "rate_limit"
    default_retryable = True


class AIConnectionError(AIError):
    default_code = "connection_error"
    default_retryable = True


class AIProviderUnavailableError(AIError):
    default_code = "provider_unavailable"
    default_retryable = True


class AIResponseError(AIError):
    default_code = "invalid_response"


class AIIncompleteResponseError(AIResponseError):
    default_code = "incomplete_response"

    def __init__(self, finish_reason: str) -> None:
        retryable = finish_reason in {"aborted", "insufficient_system_resource"}
        super().__init__(
            f"AI-провайдер не завершил ответ: finish_reason={finish_reason}",
            retryable=retryable,
        )
        self.finish_reason = finish_reason


class AIJSONError(AIResponseError):
    default_code = "invalid_json"


class AIJSONSchemaError(AIResponseError):
    default_code = "json_schema_mismatch"
