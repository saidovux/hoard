import json
import urllib.error

import pytest

from hoard.ai import AIImage, AIMessage, AIRequest
from hoard.ai.deepseek import DeepSeekProvider, HTTPResponse, UrllibTransport
from hoard.ai.errors import (
    AIAuthenticationError,
    AIConnectionError,
    AIIncompleteResponseError,
    AIInsufficientBalanceError,
    AIJSONError,
    AIJSONSchemaError,
    AIProviderUnavailableError,
    AIRateLimitError,
    AIRequestError,
    AIResponseError,
)
from hoard.ai.executor import AICore


def api_response(content, *, finish_reason="stop", model="deepseek-flash"):
    return {
        "id": "request-1",
        "model": model,
        "choices": [
            {
                "finish_reason": finish_reason,
                "message": {"role": "assistant", "content": content},
            }
        ],
    }


class FakeTransport:
    def __init__(self, *responses):
        self.responses = list(responses)
        self.calls = []

    async def post_json(self, url, *, headers, body, timeout):
        self.calls.append(
            {
                "url": url,
                "headers": dict(headers),
                "body": json.loads(body),
                "timeout": timeout,
            }
        )
        status, payload = self.responses.pop(0)
        return HTTPResponse(
            status=status,
            body=json.dumps(payload).encode(),
            headers={},
        )


class RawTransport:
    def __init__(self, status, body):
        self.status = status
        self.body = body
        self.calls = 0

    async def post_json(self, url, *, headers, body, timeout):
        self.calls += 1
        return HTTPResponse(status=self.status, body=self.body, headers={})


def make_core(transport):
    provider = DeepSeekProvider("secret-key", transport=transport)
    return AICore({"deepseek": provider})


async def test_deepseek_text_request_uses_current_default_model_once():
    transport = FakeTransport((200, api_response("  Готово  ")))
    request = AIRequest((AIMessage("user", "Ответь кратко"),))

    response = await make_core(transport).execute(request)

    assert response.content == "Готово"
    assert response.model == "deepseek-flash"
    assert response.request_id == "request-1"
    assert len(transport.calls) == 1
    call = transport.calls[0]
    assert call["url"] == "https://api.deepseek.com/chat/completions"
    assert call["body"] == {
        "model": "deepseek-flash",
        "messages": [{"role": "user", "content": "Ответь кратко"}],
        "stream": False,
    }
    assert call["headers"]["Authorization"] == "Bearer secret-key"


async def test_deepseek_visual_request_embeds_only_explicit_image():
    image = AIImage(b"\x89PNG\r\n\x1a\nmock", detail="low")
    transport = FakeTransport((200, api_response("Изображение описано")))
    request = AIRequest((AIMessage("user", "Опиши изображение", (image,)),))

    response = await make_core(transport).execute(request)

    assert response.content == "Изображение описано"
    content = transport.calls[0]["body"]["messages"][0]["content"]
    assert content[0] == {"type": "text", "text": "Опиши изображение"}
    assert content[1]["type"] == "image_url"
    assert content[1]["image_url"]["detail"] == "low"
    assert content[1]["image_url"]["url"].startswith("data:image/png;base64,")
    assert "mock" not in repr(image)


async def test_json_output_is_parsed_and_validated_against_schema():
    transport = FakeTransport((200, api_response('{"score": 7}')))
    request = AIRequest(
        (AIMessage("system", "Верни JSON с целым score"),),
        response_format="json",
        json_schema={
            "type": "object",
            "properties": {"score": {"type": "integer"}},
            "required": ["score"],
            "additionalProperties": False,
        },
        max_tokens=100,
    )

    response = await make_core(transport).execute(request)

    assert response.structured == {"score": 7}
    body = transport.calls[0]["body"]
    assert body["response_format"] == {"type": "json_object"}
    assert body["max_tokens"] == 100


async def test_invalid_schema_fails_before_paid_request():
    transport = FakeTransport((200, api_response("{}")))
    request = AIRequest(
        (AIMessage("user", "Верни JSON"),),
        response_format="json",
        json_schema={"type": "not-a-json-schema-type"},
    )

    with pytest.raises(AIJSONSchemaError):
        await make_core(transport).execute(request)

    assert transport.calls == []


async def test_json_schema_mismatch_is_not_success():
    transport = FakeTransport((200, api_response("{}")))
    request = AIRequest(
        (AIMessage("user", "Верни JSON"),),
        response_format="json",
        json_schema={"type": "object", "required": ["score"]},
    )

    with pytest.raises(AIJSONSchemaError):
        await make_core(transport).execute(request)

    assert len(transport.calls) == 1


@pytest.mark.parametrize(
    ("status", "error"),
    [
        (400, AIRequestError),
        (401, AIAuthenticationError),
        (402, AIInsufficientBalanceError),
        (429, AIRateLimitError),
        (503, AIProviderUnavailableError),
    ],
)
async def test_provider_errors_are_typed_and_never_automatically_retried(status, error):
    transport = FakeTransport((status, {"error": {"message": "provider detail"}}))
    request = AIRequest((AIMessage("user", "test"),))

    with pytest.raises(error):
        await make_core(transport).execute(request)

    assert len(transport.calls) == 1


async def test_non_json_server_error_keeps_provider_unavailable_classification():
    transport = RawTransport(503, b"temporarily unavailable")

    with pytest.raises(AIProviderUnavailableError):
        await make_core(transport).execute(AIRequest((AIMessage("user", "test"),)))

    assert transport.calls == 1


async def test_network_error_is_typed_without_retry(monkeypatch):
    calls = 0

    def fail(*args, **kwargs):
        nonlocal calls
        calls += 1
        raise urllib.error.URLError("offline")

    monkeypatch.setattr(UrllibTransport, "_post_json", fail)
    provider = DeepSeekProvider("secret-key", transport=UrllibTransport())

    with pytest.raises(AIConnectionError):
        await AICore({"deepseek": provider}).execute(
            AIRequest((AIMessage("user", "test"),))
        )

    assert calls == 1


@pytest.mark.parametrize(
    ("payload", "error"),
    [
        (api_response("", finish_reason="stop"), AIResponseError),
        (api_response("partial", finish_reason="length"), AIIncompleteResponseError),
        ({"model": "deepseek-flash", "choices": []}, AIResponseError),
    ],
)
async def test_empty_incomplete_and_malformed_responses_are_not_success(payload, error):
    transport = FakeTransport((200, payload))

    with pytest.raises(error):
        await make_core(transport).execute(AIRequest((AIMessage("user", "test"),)))

    assert len(transport.calls) == 1


async def test_malformed_json_content_is_not_success():
    transport = FakeTransport((200, api_response("{")))
    request = AIRequest(
        (AIMessage("user", "Верни JSON"),),
        response_format="json",
    )

    with pytest.raises(AIJSONError):
        await make_core(transport).execute(request)


async def test_external_json_schema_reference_fails_before_request():
    transport = FakeTransport((200, api_response("{}")))
    request = AIRequest(
        (AIMessage("user", "Верни JSON"),),
        response_format="json",
        json_schema={"$ref": "https://example.invalid/schema.json"},
    )

    with pytest.raises(AIJSONSchemaError):
        await make_core(transport).execute(request)

    assert transport.calls == []


async def test_json_mode_requires_explicit_json_instruction_before_request():
    transport = FakeTransport((200, api_response("{}")))
    request = AIRequest(
        (AIMessage("user", "Верни объект"),),
        response_format="json",
    )

    with pytest.raises(AIRequestError):
        await make_core(transport).execute(request)

    assert transport.calls == []
