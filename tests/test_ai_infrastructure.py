import asyncio
import json
import sqlite3
from decimal import Decimal

import pytest

from hoard.ai import AIMessage, AIRequest, AIResponse, AIUsage
from hoard.ai.errors import AIConnectionError, AIRequestError
from hoard.ai.executor import AICore
from hoard.ai.pricing import estimate_cost
from hoard.ai.provider import AIProvider
from hoard.models import Post
from hoard.service import Service
from hoard.storage import Store
from hoard.telegram import AuthState


async def wait_for(predicate, seconds=4):
    async with asyncio.timeout(seconds):
        while not predicate():
            await asyncio.sleep(0.02)


class FakeEngine:
    authorization_step = AuthState.DISCONNECTED

    async def disconnect(self):
        return None


class SequenceProvider(AIProvider):
    name = "deepseek"

    def __init__(self, *results):
        self.results = list(results)
        self.requests = []

    @property
    def configured(self):
        return True

    async def complete(self, request):
        self.requests.append(request)
        result = self.results.pop(0)
        if isinstance(result, Exception):
            raise result
        return result


class BlockingProvider(AIProvider):
    name = "deepseek"

    def __init__(self):
        self.started = asyncio.Event()
        self.release = asyncio.Event()
        self.requests = []

    @property
    def configured(self):
        return True

    async def complete(self, request):
        self.requests.append(request)
        self.started.set()
        await self.release.wait()
        return response("done")


def response(content="answer", *, usage=None, model="deepseek-chat"):
    return AIResponse(
        provider="deepseek",
        model=model,
        content=content,
        finish_reason="stop",
        request_id="provider-request-1",
        usage=usage,
    )


def service_with(tmp_path, provider):
    store = Store(tmp_path / "hoard.sqlite3")
    service = Service(
        store=store,
        engine=FakeEngine(),
        ai_core=AICore({"deepseek": provider}),
    )
    return service, store


async def prompt(service, *, instructions="Be concise", **overrides):
    values = {
        "name": "Assistant",
        "scope": "general",
        "instructions": instructions,
        "provider": "deepseek",
        "model": "deepseek-chat",
        "response_format": "text",
        **overrides,
    }
    return (await service.handle("ai_prompt_create", values))["prompt"]


async def chat(service, prompt_id=None, *, scope="general"):
    return (
        await service.handle(
            "ai_chat_create",
            {
                "title": "Persistent chat",
                "scope": scope,
                "provider": "deepseek",
                "model": "deepseek-chat",
                "prompt_id": prompt_id,
            },
        )
    )["chat"]


async def send(service, chat_id, content="hello", operation="operation-1", **extra):
    return (
        await service.handle(
            "ai_chat_send",
            {
                "chat_id": chat_id,
                "content": content,
                "operation_id": operation,
                "external_confirmed": True,
                **extra,
            },
        )
    )["attempt"]


async def test_prompt_crud_versions_activation_and_pagination(tmp_path):
    service, store = service_with(tmp_path, SequenceProvider(response()))
    created = await prompt(service, instructions="Version one")
    await service.handle("ai_prompt_activate", {"id": created["id"], "scope": "general"})
    updated = (
        await service.handle(
            "ai_prompt_update",
            {"id": created["id"], "name": "Renamed", "instructions": "Version two"},
        )
    )["prompt"]

    versions = await service.handle(
        "ai_prompt_versions", {"id": created["id"], "limit": 1, "offset": 0}
    )
    older = await service.handle(
        "ai_prompt_versions", {"id": created["id"], "limit": 1, "offset": 1}
    )
    assert updated["current_version"]["version"] == 2
    assert versions["items"][0]["instructions"] == "Version two"
    assert versions["has_more"] is True
    assert older["items"][0]["instructions"] == "Version one"
    listed = await service.handle("ai_prompt_list", {"scope": "general", "limit": 20})
    assert listed["items"][0]["active"] == 1

    await service.handle("ai_prompt_delete", {"id": created["id"]})
    with pytest.raises(ValueError, match="не найден"):
        await service.handle("ai_prompt_get", {"id": created["id"]})
    assert store.db.execute(
        "SELECT COUNT(*) FROM ai_prompt_versions WHERE prompt_id=?", (created["id"],)
    ).fetchone()[0] == 2
    store.close()


async def test_chat_generation_uses_frozen_prompt_versions_and_persists_history(tmp_path):
    provider = SequenceProvider(response("first"), response("second"))
    service, store = service_with(tmp_path, provider)
    selected = await prompt(service, instructions="Version one")
    conversation = await chat(service, selected["id"])
    await service.start_background()
    first = await send(service, conversation["id"], operation="first")
    await wait_for(lambda: store.ai.attempt(first["id"])["state"] == "completed")

    await service.handle(
        "ai_prompt_update", {"id": selected["id"], "instructions": "Version two"}
    )
    second = await send(service, conversation["id"], content="again", operation="second")
    await wait_for(lambda: store.ai.attempt(second["id"])["state"] == "completed")
    history = await service.handle(
        "ai_chat_history", {"id": conversation["id"], "limit": 20}
    )

    assert [item["content"] for item in history["items"]] == [
        "hello",
        "first",
        "again",
        "second",
    ]
    assert store.ai.attempt(first["id"])["prompt_version_id"] != store.ai.attempt(second["id"])[
        "prompt_version_id"
    ]
    assert provider.requests[0].messages[0].content == "Version one"
    assert provider.requests[1].messages[0].content == "Version two"
    await service.stop_background()
    store.close()


async def test_chat_can_explicitly_select_an_older_prompt_version(tmp_path):
    provider = SequenceProvider(response("selected old version"))
    service, store = service_with(tmp_path, provider)
    selected = await prompt(service, instructions="Version one")
    conversation = await chat(service, selected["id"])
    await service.handle(
        "ai_prompt_update", {"id": selected["id"], "instructions": "Version two"}
    )
    versions = await service.handle(
        "ai_prompt_versions", {"id": selected["id"], "limit": 20}
    )
    old_version = next(item for item in versions["items"] if item["version"] == 1)
    attempt = await send(
        service,
        conversation["id"],
        prompt_version_id=old_version["id"],
    )
    await service.ai_service.execute_attempt(attempt["id"])
    assert provider.requests[0].messages[0].content == "Version one"
    assert store.ai.attempt(attempt["id"])["prompt_version_id"] == old_version["id"]
    store.close()


async def test_chat_paid_send_requires_explicit_external_confirmation(tmp_path):
    service, store = service_with(tmp_path, SequenceProvider(response()))
    conversation = await chat(service)
    with pytest.raises(ValueError, match="внешнему"):
        await service.handle(
            "ai_chat_send",
            {
                "chat_id": conversation["id"],
                "content": "do not send",
                "operation_id": "not-confirmed",
            },
        )
    assert store.db.execute("SELECT COUNT(*) FROM jobs WHERE kind='ai.chat'").fetchone()[0] == 0
    store.close()


async def test_chat_crud_archive_filters_and_soft_delete_preserve_history(tmp_path):
    service, store = service_with(tmp_path, SequenceProvider(response("saved")))
    conversation = await chat(service)
    updated = (
        await service.handle(
            "ai_chat_update",
            {"id": conversation["id"], "title": "Archived chat", "archived": True},
        )
    )["chat"]
    assert updated["title"] == "Archived chat"
    assert updated["archived"] == 1
    listed = await service.handle("ai_chat_list", {"archived": True, "limit": 20})
    assert [item["id"] for item in listed["items"]] == [conversation["id"]]

    attempt = await send(service, conversation["id"])
    await service.start_background()
    await wait_for(lambda: store.ai.attempt(attempt["id"])["state"] == "completed")
    await service.handle("ai_chat_delete", {"id": conversation["id"]})
    with pytest.raises(ValueError, match="не найден"):
        await service.handle("ai_chat_get", {"id": conversation["id"]})
    assert store.db.execute(
        "SELECT COUNT(*) FROM ai_messages WHERE chat_id=?", (conversation["id"],)
    ).fetchone()[0] == 2
    assert store.ai.attempt(attempt["id"])["state"] == "completed"
    await service.stop_background()
    store.close()


async def test_duplicate_operation_and_concurrent_branch_create_one_paid_job(tmp_path):
    provider = BlockingProvider()
    service, store = service_with(tmp_path, provider)
    conversation = await chat(service)
    first = await send(service, conversation["id"], operation="same")
    repeated = await send(service, conversation["id"], operation="same")
    assert first["id"] == repeated["id"]
    assert store.db.execute("SELECT COUNT(*) FROM jobs WHERE kind='ai.chat'").fetchone()[0] == 1
    with pytest.raises(ValueError, match="уже выполняется"):
        await send(service, conversation["id"], operation="different")

    await service.start_background()
    await asyncio.wait_for(provider.started.wait(), 2)
    provider.release.set()
    await wait_for(lambda: store.ai.attempt(first["id"])["state"] == "completed")
    assert len(provider.requests) == 1
    await service.stop_background()
    store.close()


async def test_memory_is_explicit_versioned_and_isolated_by_scope(tmp_path):
    provider = SequenceProvider(response())
    service, store = service_with(tmp_path, provider)
    general = (
        await service.handle(
            "ai_memory_create",
            {"scope": "general", "title": "General", "content": "general fact"},
        )
    )["memory"]
    telegram = (
        await service.handle(
            "ai_memory_create",
            {"scope": "telegram", "title": "Telegram", "content": "selected fact"},
        )
    )["memory"]
    conversation = await chat(service, scope="telegram")
    local = (
        await service.handle(
            "ai_memory_create",
            {
                "scope": "chat",
                "chat_id": conversation["id"],
                "title": "Local",
                "content": "chat fact",
            },
        )
    )["memory"]
    with pytest.raises(ValueError, match="другой области"):
        await send(
            service,
            conversation["id"],
            operation="bad-memory",
            memory_ids=[general["id"]],
        )
    attempt = await send(
        service,
        conversation["id"],
        operation="selected-memory",
        memory_ids=[telegram["id"], local["id"]],
    )
    await service.handle(
        "ai_memory_update", {"id": telegram["id"], "content": "changed later"}
    )
    await service.start_background()
    await wait_for(lambda: store.ai.attempt(attempt["id"])["state"] == "completed")
    memory_message = provider.requests[0].messages[0]
    assert memory_message.role == "user"
    assert "selected fact" in memory_message.content
    assert "changed later" not in memory_message.content
    assert "general fact" not in memory_message.content
    await service.stop_background()
    store.close()


async def test_context_limit_never_silently_truncates_prompt_or_new_message(tmp_path):
    service, store = service_with(tmp_path, SequenceProvider(response()))
    selected = await prompt(service, instructions="P" * 900, context_limit=1000)
    conversation = await chat(service, selected["id"])
    with pytest.raises(ValueError, match="не обрезаны"):
        await send(service, conversation["id"], content="U" * 200)
    assert store.db.execute("SELECT COUNT(*) FROM jobs WHERE kind='ai.chat'").fetchone()[0] == 0
    store.close()


async def test_queued_chat_survives_daemon_restart(tmp_path):
    first_provider = SequenceProvider(response("unused"))
    first, store = service_with(tmp_path, first_provider)
    conversation = await chat(first)
    attempt = await send(first, conversation["id"], operation="persistent")
    store.close()

    second_provider = SequenceProvider(response("after restart"))
    second_store = Store(tmp_path / "hoard.sqlite3")
    second = Service(
        store=second_store,
        engine=FakeEngine(),
        ai_core=AICore({"deepseek": second_provider}),
    )
    await second.start_background()
    await wait_for(lambda: second_store.ai.attempt(attempt["id"])["state"] == "completed")
    history = second_store.ai.chat_history(conversation["id"], limit=20, offset=0)
    assert history["items"][-1]["content"] == "after restart"
    await second.stop_background()
    second_store.close()


async def test_running_attempt_becomes_unknown_after_daemon_restart(tmp_path):
    first, store = service_with(tmp_path, SequenceProvider(response("unused")))
    conversation = await chat(first)
    attempt = await send(first, conversation["id"], operation="interrupted")
    store.mark_running(attempt["job_id"])
    store.ai.mark_attempt_running(attempt["id"])
    store.close()

    provider = SequenceProvider(response("must not run"))
    second_store = Store(tmp_path / "hoard.sqlite3")
    second = Service(
        store=second_store,
        engine=FakeEngine(),
        ai_core=AICore({"deepseek": provider}),
    )
    await second.start_background()
    recovered = second_store.ai.attempt(attempt["id"])
    assert recovered["state"] == "outcome_unknown"
    assert recovered["error_code"] == "daemon_interrupted"
    assert second_store.db.execute(
        "SELECT state FROM jobs WHERE id=?", (attempt["job_id"],)
    ).fetchone()[0] == "failed"
    assert provider.requests == []
    await second.stop_background()
    second_store.close()


async def test_old_running_legacy_job_is_backfilled_as_unknown_on_upgrade(tmp_path):
    store = Store(tmp_path / "hoard.sqlite3")
    request = AIRequest((AIMessage("user", "legacy interrupted"),))
    job_id = store.enqueue(
        "ai.request", {"provider": None, "request": request.to_dict()}
    )
    store.mark_running(job_id)
    store.close()

    provider = SequenceProvider(response("must not run"))
    second_store = Store(tmp_path / "hoard.sqlite3")
    second = Service(
        store=second_store,
        engine=FakeEngine(),
        ai_core=AICore({"deepseek": provider}),
    )
    await second.start_background()
    recovered = second_store.ai.attempt_by_job(job_id)
    assert recovered["state"] == "outcome_unknown"
    assert recovered["error_code"] == "daemon_interrupted"
    assert provider.requests == []
    await second.stop_background()
    second_store.close()


async def test_invalid_json_still_records_usage_reasoning_and_decimal_cost(tmp_path):
    usage = AIUsage(
        prompt_tokens=30,
        completion_tokens=5,
        total_tokens=35,
        prompt_cache_hit_tokens=10,
        prompt_cache_miss_tokens=20,
        reasoning_tokens=4,
    )
    provider = SequenceProvider(response("{", usage=usage))
    service, store = service_with(tmp_path, provider)
    store.ai.create_price(
        {
            "provider": "deepseek",
            "model": "deepseek-chat",
            "currency": "USD",
            "input_cache_hit_per_million": "0.5",
            "input_cache_miss_per_million": "2",
            "output_per_million": "3",
            "source": "test fixture",
        }
    )
    selected = await prompt(
        service,
        instructions="Return JSON only",
        response_format="json",
        json_schema={"type": "object"},
    )
    conversation = await chat(service, selected["id"])
    attempt = await send(service, conversation["id"])
    await service.start_background()
    await wait_for(lambda: store.ai.attempt(attempt["id"])["state"] == "failed")
    recorded = store.ai.attempt(attempt["id"])
    assert recorded["error_code"] == "invalid_json"
    assert recorded["prompt_tokens"] == 30
    assert recorded["completion_tokens"] == 5
    assert recorded["reasoning_tokens"] == 4
    assert Decimal(recorded["estimated_cost"]) == Decimal("0.000060")
    assert recorded["cost_status"] == "complete"
    assert store.ai.chat_history(conversation["id"], limit=20, offset=0)["items"][-1][
        "role"
    ] == "user"
    await service.stop_background()
    store.close()


async def test_missing_usage_and_partial_tariff_never_become_fake_zero(tmp_path):
    provider = SequenceProvider(response("ok"))
    service, store = service_with(tmp_path, provider)
    conversation = await chat(service)
    attempt = await send(service, conversation["id"])
    await service.start_background()
    await wait_for(lambda: store.ai.attempt(attempt["id"])["state"] == "completed")
    recorded = store.ai.attempt(attempt["id"])
    assert recorded["prompt_tokens"] is None
    assert recorded["estimated_cost"] is None
    assert recorded["cost_status"] == "unknown"
    partial = estimate_cost(
        AIUsage(completion_tokens=10),
        {"currency": "USD", "output_per_million": "3"},
    )
    assert partial.status == "partial"
    assert partial.amount == "0.00003"
    await service.stop_background()
    store.close()


async def test_historical_cost_snapshot_does_not_change_with_tariffs(tmp_path):
    usage = AIUsage(
        prompt_tokens=2,
        completion_tokens=1,
        total_tokens=3,
        prompt_cache_hit_tokens=1,
        prompt_cache_miss_tokens=1,
    )
    service, store = service_with(tmp_path, SequenceProvider(response(usage=usage)))
    original = store.ai.create_price(
        {
            "provider": "deepseek",
            "model": "deepseek-chat",
            "currency": "USD",
            "input_cache_hit_per_million": "1",
            "input_cache_miss_per_million": "2",
            "output_per_million": "3",
            "source": "old",
        }
    )
    conversation = await chat(service)
    attempt = await send(service, conversation["id"])
    await service.start_background()
    await wait_for(lambda: store.ai.attempt(attempt["id"])["state"] == "completed")
    before = store.ai.attempt(attempt["id"])
    store.ai.delete_price(original["id"])
    store.ai.create_price(
        {
            "provider": "deepseek",
            "model": "deepseek-chat",
            "currency": "USD",
            "input_cache_hit_per_million": "100",
            "input_cache_miss_per_million": "200",
            "output_per_million": "300",
            "source": "new",
        }
    )
    after = store.ai.attempt(attempt["id"])
    assert after["estimated_cost"] == before["estimated_cost"]
    assert after["price_snapshot"]["source"] == "old"
    await service.stop_background()
    store.close()


async def test_connection_loss_is_unknown_and_retry_creates_new_attempt(tmp_path):
    provider = SequenceProvider(AIConnectionError("connection lost"), response("retried"))
    service, store = service_with(tmp_path, provider)
    conversation = await chat(service)
    first = await send(service, conversation["id"])
    await service.start_background()
    await wait_for(lambda: store.ai.attempt(first["id"])["state"] == "outcome_unknown")
    with pytest.raises(ValueError, match="ai_attempt_retry"):
        await service.handle("job_retry", {"id": first["job_id"]})
    retried = (
        await service.handle(
            "ai_attempt_retry", {"id": first["id"], "operation_id": "explicit-retry"}
        )
    )["attempt"]
    repeated = (
        await service.handle(
            "ai_attempt_retry", {"id": first["id"], "operation_id": "explicit-retry"}
        )
    )["attempt"]
    assert retried["id"] != first["id"]
    assert repeated["id"] == retried["id"]
    await wait_for(lambda: store.ai.attempt(retried["id"])["state"] == "completed")
    assert len(provider.requests) == 2
    await service.stop_background()
    store.close()


async def test_new_image_path_uses_private_attachment_reference_not_job_base64(tmp_path):
    provider = SequenceProvider(response("image ok"))
    service, store = service_with(tmp_path, provider)
    image_file = tmp_path / "selected.png"
    image_file.write_bytes(b"\x89PNG\r\n\x1a\nmock-image")
    attachment = store.ai.import_attachment(str(image_file))
    conversation = await chat(service)
    attempt = await send(
        service,
        conversation["id"],
        operation="image",
        attachment_ids=[attachment["id"]],
    )
    job_payload = store.db.execute(
        "SELECT payload FROM jobs WHERE id=?", (attempt["job_id"],)
    ).fetchone()[0]
    assert json.loads(job_payload) == {"attempt_id": attempt["id"]}
    assert "mock-image" not in job_payload
    with pytest.raises(ValueError, match="связано"):
        store.ai.delete_attachment(attachment["id"])
    await service.start_background()
    await wait_for(lambda: store.ai.attempt(attempt["id"])["state"] == "completed")
    assert provider.requests[0].messages[-1].images[0].media_type == "image/png"
    await service.stop_background()
    store.close()


async def test_legacy_ai_jobs_are_accounted_without_changing_payload(tmp_path):
    usage = AIUsage(prompt_tokens=1, completion_tokens=1, total_tokens=2)
    service, store = service_with(tmp_path, SequenceProvider(response(usage=usage)))
    jid = service.enqueue_ai(AIRequest((AIMessage("user", "legacy message"),)))
    payload = store.db.execute("SELECT payload FROM jobs WHERE id=?", (jid,)).fetchone()[0]
    assert "api_key" not in payload
    assert store.ai.attempt_by_job(jid)["state"] == "queued"
    await service.start_background()
    await wait_for(
        lambda: store.db.execute("SELECT state FROM jobs WHERE id=?", (jid,)).fetchone()[0]
        == "done"
    )
    attempt = store.db.execute("SELECT * FROM ai_attempts WHERE job_id=?", (jid,)).fetchone()
    assert attempt["state"] == "completed"
    assert attempt["prompt_tokens"] == 1
    await service.stop_background()
    store.close()


async def test_terminal_attempt_accounting_and_assistant_write_are_idempotent(tmp_path):
    provider = SequenceProvider(response("only once"))
    service, store = service_with(tmp_path, provider)
    conversation = await chat(service)
    attempt = await send(service, conversation["id"])

    assert await service.ai_service.execute_attempt(attempt["id"]) == "only once"
    assert await service.ai_service.execute_attempt(attempt["id"]) == "only once"
    assert len(provider.requests) == 1
    assert store.db.execute(
        "SELECT COUNT(*) FROM ai_messages WHERE attempt_id=? AND role='assistant'",
        (attempt["id"],),
    ).fetchone()[0] == 1
    state = await service.handle("ai_job_state", {"job_id": attempt["job_id"]})
    assert state["attempt"]["state"] == "completed"
    assert state["job"]["state"] == "queued"
    store.close()


async def test_ai_job_error_does_not_copy_provider_or_chat_text_to_general_log(tmp_path):
    secret = "private message echoed by provider"
    provider = SequenceProvider(AIRequestError(secret))
    service, store = service_with(tmp_path, provider)
    conversation = await chat(service)
    attempt = await send(service, conversation["id"], content="private chat content")
    await service.start_background()
    await wait_for(lambda: store.ai.attempt(attempt["id"])["state"] == "failed")
    result = store.db.execute(
        "SELECT result FROM jobs WHERE id=?", (attempt["job_id"],)
    ).fetchone()[0]
    assert secret not in result
    assert "private chat content" not in result
    assert store.ai.attempt(attempt["id"])["error_code"] == "invalid_request"
    await service.stop_background()
    store.close()


def test_ai_schema_is_idempotent_and_preserves_existing_telegram_rows(tmp_path):
    filename = tmp_path / "existing.sqlite3"
    first = Store(filename)
    source_id = first.add_source(Post("channelx", 7), str(tmp_path / "archive"))
    first.ai.create_memory({"scope": "general", "title": "Remember", "content": "value"})
    first.close()
    second = Store(filename)
    assert second.sources()[0]["id"] == source_id
    assert second.ai.list_memory(scope="general", chat_id=None, limit=20, offset=0)["items"][0][
        "content"
    ] == "value"
    assert second.db.execute(
        "SELECT version FROM schema_versions WHERE module='ai'"
    ).fetchone()[0] == 2
    second.close()


def test_migration_adds_ai_schema_to_minimal_legacy_database(tmp_path):
    filename = tmp_path / "legacy.sqlite3"
    db = sqlite3.connect(filename)
    db.execute(
        """CREATE TABLE sources (
        id INTEGER PRIMARY KEY,username TEXT NOT NULL,post_id INTEGER NOT NULL,
        url TEXT NOT NULL,output TEXT NOT NULL,interval INTEGER NOT NULL DEFAULT 120,
        enabled INTEGER NOT NULL DEFAULT 1,last_id INTEGER NOT NULL DEFAULT 0,
        next_at REAL NOT NULL DEFAULT 0,last_error TEXT NOT NULL DEFAULT '',
        UNIQUE(username,post_id))"""
    )
    db.execute(
        "INSERT INTO sources(username,post_id,url,output) VALUES('legacy',1,'https://t.me/legacy/1',?)",
        (str(tmp_path),),
    )
    db.commit()
    db.close()
    store = Store(filename)
    assert store.sources()[0]["username"] == "legacy"
    assert store.db.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name='ai_attempts'"
    ).fetchone()
    store.close()


async def test_statistics_filters_and_unknown_cost_count(tmp_path):
    service, store = service_with(tmp_path, SequenceProvider(response("ok")))
    conversation = await chat(service)
    attempt = await send(service, conversation["id"])
    await service.start_background()
    await wait_for(lambda: store.ai.attempt(attempt["id"])["state"] == "completed")
    stats = await service.handle(
        "ai_statistics", {"provider": "deepseek", "scope": "general", "chat_id": conversation["id"]}
    )
    assert stats["attempts"] == 1
    assert stats["completed"] == 1
    assert stats["unknown_cost"] == 1
    assert stats["estimated_costs"] == {}
    await service.stop_background()
    store.close()
