"""Minimal 0.7 AI selection workflow using only synthetic local records."""
from __future__ import annotations

import asyncio
import json
import time

import pytest

from hoard.ai import AICore, AIResponse
from hoard.ai.config import DEFAULT_DEEPSEEK_MODEL
from hoard.ai.provider import AIProvider
from hoard.ai.psl import DEFAULT_PSL_INSTRUCTIONS, PSL_OUTPUT_FORMAT
from hoard.models import Post
from hoard.service import Service
from hoard.storage import Store
from hoard.telegram import AuthState


class NoTelegramEngine:
    authorization_step = AuthState.DISCONNECTED

    async def disconnect(self):
        return None


class FakeProvider(AIProvider):
    name = "deepseek"
    configured = True

    def __init__(self):
        self.requests = []

    async def complete(self, request):
        self.requests.append(request)
        return AIResponse(
            "deepseek",
            "deepseek-chat",
            json.dumps(
                {
                    "status": "completed",
                    "presentation": "female",
                    "estimated_age": 27,
                    "raw_psl_min": 4.0,
                    "raw_psl_max": 5.0,
                    "boosted_psl": 5.5,
                }
            ),
            "stop",
        )


def workflow_service(tmp_path, profile_count=85):
    archive = tmp_path / "archive"
    archive.mkdir()
    store = Store(tmp_path / "hoard.sqlite3")
    source_id = store.add_source(Post("channelx", 77), str(archive))
    for author_id in range(1, profile_count + 1):
        store.telegram.observe_profile(
            author_id=author_id,
            username=f"person{author_id}",
            display_name=f"Person {author_id}",
            source_id=source_id,
        )
    for position, photo_id in enumerate(("newest", "older")):
        path = archive / f"{photo_id}.png"
        path.write_bytes(b"\x89PNG\r\n\x1a\n" + photo_id.encode())
        store.telegram.record_profile_photo(
            1,
            photo_id,
            file_path=str(path),
            size=path.stat().st_size,
            photo_date=f"2026-10-{22 - position:02d}",
        )
    provider = FakeProvider()
    service = Service(
        store=store,
        engine=NoTelegramEngine(),
        ai_core=AICore({"deepseek": provider}),
    )
    return service, store, provider, source_id


def add_profile_photo(store, archive, author_id, photo_id=None, content=None, date="2026-10-22"):
    selected_id = photo_id or f"photo-{author_id}"
    payload = content or (b"\x89PNG\r\n\x1a\n" + str(author_id).encode())
    path = archive / f"{selected_id}.png"
    path.write_bytes(payload)
    store.telegram.record_profile_photo(
        author_id,
        selected_id,
        file_path=str(path),
        size=path.stat().st_size,
        photo_date=date,
    )
    return path


async def test_source_preview_is_local_complete_and_has_no_80_profile_cap(tmp_path):
    service, store, provider, source_id = workflow_service(tmp_path)
    preview = (
        await service.handle(
            "ai_telegram_batch_preview",
            {"source_url": "https://t.me/channelx/77", "limit": 100},
        )
    )["preview"]

    assert preview["selected_count"] == 85
    assert preview["source"]["id"] == source_id
    assert preview["external_request_made"] is False
    assert preview["ready_count"] == 1
    first = next(item for item in preview["items"] if item["author_id"] == 1)
    assert first["selected_photo_ids"] == ["newest", "older"]
    assert first["adult_confirmed"] is True
    assert first["reason"] == ""
    assert provider.requests == []
    assert store.db.execute("SELECT COUNT(*) FROM jobs WHERE kind='ai.chat'").fetchone()[0] == 0

    with pytest.raises(ValueError, match="не найдена"):
        await service.handle(
            "ai_telegram_batch_preview", {"source_url": "https://t.me/unknownx/9"}
        )
    assert provider.requests == []
    store.close()


async def test_batch_uses_automatic_adult_admission_and_is_idempotent(tmp_path):
    service, store, provider, source_id = workflow_service(tmp_path, profile_count=3)
    third = tmp_path / "archive" / "third.png"
    third.write_bytes(b"\x89PNG\r\n\x1a\nthird")
    store.telegram.record_profile_photo(
        3,
        "third",
        file_path=str(third),
        size=third.stat().st_size,
        photo_date="2026-10-22",
    )
    values = {
        "source_id": source_id,
        "confirmed_author_ids": [1, 2],
        "operation_id": "stable-operation",
        "external_confirmed": True,
    }
    first = (await service.handle("ai_telegram_batch_start", values))["batch"]
    repeated = (await service.handle("ai_telegram_batch_start", values))["batch"]

    assert repeated["id"] == first["id"]
    assert first["selected_count"] == 3
    assert first["queued"] == 2
    assert first["skipped"] == 1
    reasons = {item["author_id"]: item["reason"] for item in first["items"]}
    assert "фотограф" in reasons[2]
    assert reasons[3] == ""
    assert store.db.execute("SELECT COUNT(*) FROM jobs WHERE kind='ai.chat'").fetchone()[0] == 2

    queued = [item for item in first["items"] if item["analysis_id"]]
    for item in queued:
        analysis = store.ai.telegram_analysis(item["analysis_id"])
        await service.ai_service.execute_attempt(analysis["attempt_id"])
    finished = store.ai.telegram_batch(first["id"])
    assert finished["completed"] == 2
    assert finished["queued"] == 0
    assert finished["terminal"] is True
    assert len(provider.requests) == 2
    assert len(provider.requests[0].messages[-1].images) == 2

    database = store.filename
    store.close()
    reopened = Store(database)
    assert reopened.ai.telegram_batch(first["id"])["completed"] == 2
    reopened.close()


async def test_selected_profiles_deduplicate_only_by_author_id(tmp_path):
    service, store, _provider, _source_id = workflow_service(tmp_path, profile_count=2)
    with store.db:
        store.db.execute("UPDATE tg_profiles SET current_username='same' WHERE author_id IN (1,2)")
    preview = (
        await service.handle(
            "ai_telegram_batch_preview",
            {"author_ids": [1, 1, 2, 2], "confirmed_author_ids": [1]},
        )
    )["preview"]
    assert preview["selected_count"] == 2
    assert [item["author_id"] for item in preview["items"]] == [1, 2]
    store.close()


async def test_batch_resumes_after_partial_queue(tmp_path, monkeypatch):
    service, store, _provider, _source_id = workflow_service(tmp_path, profile_count=3)
    archive = tmp_path / "archive"
    add_profile_photo(store, archive, 2)
    add_profile_photo(store, archive, 3)
    original = store.ai.link_telegram_batch_analysis
    calls = 0

    def fail_second_link(batch_id, author_id, analysis_id):
        nonlocal calls
        calls += 1
        if calls == 2:
            raise RuntimeError("simulated link interruption")
        return original(batch_id, author_id, analysis_id)

    monkeypatch.setattr(store.ai, "link_telegram_batch_analysis", fail_second_link)
    values = {
        "author_ids": [1, 2, 3],
        "operation_id": "resume-operation",
        "external_confirmed": True,
    }
    with pytest.raises(RuntimeError, match="link interruption"):
        await service.handle("ai_telegram_batch_start", values)
    monkeypatch.setattr(store.ai, "link_telegram_batch_analysis", original)
    resumed = (await service.handle("ai_telegram_batch_start", values))["batch"]

    assert resumed["queued"] == 3
    assert resumed["terminal"] is False
    assert store.db.execute("SELECT COUNT(*) FROM ai_telegram_analyses").fetchone()[0] == 3
    assert store.db.execute("SELECT COUNT(*) FROM jobs WHERE kind='ai.chat'").fetchone()[0] == 3
    store.close()


async def test_daemon_start_resumes_pending_batch(tmp_path, monkeypatch):
    service, store, _provider, _source_id = workflow_service(tmp_path, profile_count=2)
    add_profile_photo(store, tmp_path / "archive", 2)
    original = store.ai.link_telegram_batch_analysis
    interrupted = False

    def fail_once(batch_id, author_id, analysis_id):
        nonlocal interrupted
        if not interrupted:
            interrupted = True
            raise RuntimeError("simulated daemon interruption")
        return original(batch_id, author_id, analysis_id)

    monkeypatch.setattr(store.ai, "link_telegram_batch_analysis", fail_once)
    with pytest.raises(RuntimeError, match="daemon interruption"):
        await service.handle(
            "ai_telegram_batch_start",
            {
                "author_ids": [1, 2],
                "operation_id": "daemon-resume",
                "external_confirmed": True,
            },
        )
    monkeypatch.setattr(store.ai, "link_telegram_batch_analysis", original)
    await service.start_background()
    batch_id = store.db.execute(
        "SELECT id FROM ai_telegram_batches WHERE operation_key=?",
        ("telegram.psl.batch:daemon-resume",),
    ).fetchone()[0]
    batch = store.ai.telegram_batch(str(batch_id))
    assert not store.ai.pending_telegram_batch_items(str(batch_id))
    assert len({item["analysis_id"] for item in batch["items"]}) == 2
    assert store.db.execute("SELECT COUNT(*) FROM ai_telegram_analyses").fetchone()[0] == 2
    await service.stop_background()
    store.close()


async def test_batch_technical_error_is_failed_not_skipped(tmp_path, monkeypatch):
    service, store, _provider, _source_id = workflow_service(tmp_path, profile_count=1)

    def denied(**_kwargs):
        raise PermissionError("simulated permission failure")

    monkeypatch.setattr(service.ai_service, "queue_telegram_psl", denied)
    batch = (
        await service.handle(
            "ai_telegram_batch_start",
            {
                "author_ids": [1],
                "operation_id": "technical-failure",
                "external_confirmed": True,
            },
        )
    )["batch"]

    assert batch["items"][0]["state"] == "failed"
    assert "PermissionError" in batch["items"][0]["reason"]
    assert batch["errors"] == 1
    assert batch["skipped"] == 0
    assert batch["terminal"] is True
    store.close()


async def test_new_operation_id_does_not_duplicate_inflight_analysis(tmp_path):
    service, store, _provider, _source_id = workflow_service(tmp_path, profile_count=1)
    first = (
        await service.handle(
            "ai_telegram_batch_start",
            {
                "author_ids": [1],
                "operation_id": "first-action",
                "external_confirmed": True,
            },
        )
    )["batch"]
    second = (
        await service.handle(
            "ai_telegram_batch_start",
            {
                "author_ids": [1],
                "operation_id": "second-action",
                "external_confirmed": True,
            },
        )
    )["batch"]

    assert first["id"] != second["id"]
    assert first["items"][0]["analysis_id"] == second["items"][0]["analysis_id"]
    assert store.db.execute("SELECT COUNT(*) FROM ai_telegram_analyses").fetchone()[0] == 1
    assert store.db.execute("SELECT COUNT(*) FROM jobs WHERE kind='ai.chat'").fetchone()[0] == 1
    store.close()


async def test_batch_uses_preview_photos(tmp_path):
    service, store, _provider, _source_id = workflow_service(tmp_path, profile_count=1)
    preview = (
        await service.handle("ai_telegram_batch_preview", {"author_ids": [1]})
    )["preview"]
    item = preview["items"][0]
    add_profile_photo(
        store,
        tmp_path / "archive",
        1,
        photo_id="new-after-preview",
        date="2026-10-23",
    )
    batch = (
        await service.handle(
            "ai_telegram_batch_start",
            {
                "author_ids": [1],
                "photo_ids": {"1": item["selected_photo_ids"]},
                "photo_snapshots": {"1": item["photo_snapshot"]},
                "operation_id": "preview-photos",
                "external_confirmed": True,
            },
        )
    )["batch"]
    analysis = store.ai.telegram_analysis(batch["items"][0]["analysis_id"])
    assert [photo["photo_id"] for photo in analysis["photos"]] == ["newest", "older"]

    original = tmp_path / "archive" / "newest.png"
    original.write_bytes(b"changed-same-size"[: original.stat().st_size])
    rejected = (
        await service.handle(
            "ai_telegram_batch_start",
            {
                "author_ids": [1],
                "photo_ids": {"1": item["selected_photo_ids"]},
                "photo_snapshots": {"1": item["photo_snapshot"]},
                "operation_id": "changed-preview-photo",
                "external_confirmed": True,
            },
        )
    )["batch"]
    assert rejected["items"][0]["state"] == "skipped"
    assert rejected["items"][0]["reason"] == "Фото изменились после предпросмотра"
    store.close()


async def test_channel_peer_id_is_not_ready_not_error(tmp_path):
    service, store, _provider, source_id = workflow_service(tmp_path, profile_count=1)
    channel_id = -1001234567890
    store.telegram.observe_profile(
        author_id=channel_id,
        username="channel_sender",
        display_name="Channel sender",
        source_id=source_id,
    )
    preview = (
        await service.handle(
            "ai_telegram_batch_preview", {"author_ids": [1, channel_id]}
        )
    )["preview"]
    channel = next(item for item in preview["items"] if item["author_id"] == channel_id)
    assert channel["ready"] is False
    assert channel["reason"] == "Это канал или чат, а не профиль человека"
    with pytest.raises(ValueError, match="confirmed_author_ids"):
        service.ai_service._author_ids([True], field="confirmed_author_ids")
    store.close()


async def test_large_batch_does_not_stall_event_loop(tmp_path):
    service, store, _provider, _source_id = workflow_service(tmp_path, profile_count=500)
    archive = tmp_path / "archive"
    for author_id in range(2, 501):
        add_profile_photo(store, archive, author_id)

    maximum_gap = 0.0
    running = True

    async def heartbeat():
        nonlocal maximum_gap
        previous = time.perf_counter()
        while running:
            await asyncio.sleep(0.005)
            current = time.perf_counter()
            maximum_gap = max(maximum_gap, current - previous)
            previous = current

    task = asyncio.create_task(heartbeat())
    await service.handle(
        "ai_telegram_batch_start",
        {
            "author_ids": list(range(1, 501)),
            "operation_id": "large-batch",
            "external_confirmed": True,
        },
    )
    running = False
    await task
    assert maximum_gap < 0.1
    assert store.db.execute("SELECT COUNT(*) FROM ai_telegram_analyses").fetchone()[0] == 500
    store.close()


async def test_builtin_psl_contract_is_not_duplicated_and_uses_provider_default(tmp_path):
    service, store, _provider, _source_id = workflow_service(tmp_path, profile_count=1)
    analysis = (
        await service.handle(
            "ai_telegram_psl_start",
            {
                "author_id": 1,
                "photo_ids": ["newest"],
                "content": "Analyze JSON.",
                "operation_id": "builtin-contract",
                "external_confirmed": True,
            },
        )
    )["analysis"]
    request = service.ai_service.load_request(analysis["attempt_id"])
    assert [message.content for message in request.messages].count(DEFAULT_PSL_INSTRUCTIONS) == 1
    assert [message.content for message in request.messages].count(PSL_OUTPUT_FORMAT) == 1
    assert set(request.json_schema["required"]) == {
        "status",
        "presentation",
        "estimated_age",
        "raw_psl_min",
        "raw_psl_max",
        "boosted_psl",
    }
    assert request.model == DEFAULT_DEEPSEEK_MODEL
    store.close()
