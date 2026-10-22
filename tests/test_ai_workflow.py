"""Minimal 0.7 AI selection workflow using only synthetic local records."""
from __future__ import annotations

import json

import pytest

from hoard.ai import AICore, AIResponse
from hoard.ai.provider import AIProvider
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
    assert preview["ready_count"] == 0
    first = next(item for item in preview["items"] if item["author_id"] == 1)
    assert first["selected_photo_ids"] == ["newest", "older"]
    assert "Совершеннолетие" in first["reason"]
    assert provider.requests == []
    assert store.db.execute("SELECT COUNT(*) FROM jobs WHERE kind='ai.chat'").fetchone()[0] == 0

    with pytest.raises(ValueError, match="не найдена"):
        await service.handle(
            "ai_telegram_batch_preview", {"source_url": "https://t.me/unknownx/9"}
        )
    assert provider.requests == []
    store.close()


async def test_batch_skips_unconfirmed_and_missing_photos_and_is_idempotent(tmp_path):
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
    assert first["queued"] == 1
    assert first["skipped"] == 2
    reasons = {item["author_id"]: item["reason"] for item in first["items"]}
    assert "фотограф" in reasons[2]
    assert "Совершеннолетие" in reasons[3]
    assert store.db.execute("SELECT COUNT(*) FROM jobs WHERE kind='ai.chat'").fetchone()[0] == 1

    queued = next(item for item in first["items"] if item["analysis_id"])
    analysis = store.ai.telegram_analysis(queued["analysis_id"])
    await service.ai_service.execute_attempt(analysis["attempt_id"])
    finished = store.ai.telegram_batch(first["id"])
    assert finished["completed"] == 1
    assert finished["queued"] == 0
    assert finished["terminal"] is True
    assert provider.requests and len(provider.requests[0].messages[-1].images) == 2

    database = store.filename
    store.close()
    reopened = Store(database)
    assert reopened.ai.telegram_batch(first["id"])["completed"] == 1
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
