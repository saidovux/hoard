from __future__ import annotations

import asyncio
import json
from pathlib import Path

import pytest

from hoard.ai import AICore, AIResponse
from hoard.ai.provider import AIProvider
from hoard.models import Post
from hoard.service import Service
from hoard.storage import Store
from hoard.telegram import AuthState


class FakeEngine:
    authorization_step = AuthState.DISCONNECTED

    async def disconnect(self) -> None:
        return None


class FakeProvider(AIProvider):
    name = "deepseek"
    configured = True

    async def complete(self, request):
        del request
        return AIResponse("deepseek", "deepseek-chat", "{}", "stop")


def add_download(
    store: Store,
    source_id: int,
    message_id: int,
    author_id: int,
    root: Path,
) -> None:
    video = root / f"{source_id}-{message_id}.mp4"
    video.write_bytes(b"video" * 32)
    store.checkpoint(
        source_id,
        message_id,
        {
            "source_id": source_id,
            "message_id": message_id,
            "author_id": author_id,
            "author_username": f"user{author_id}",
            "author_name": f"User {author_id}",
            "date": "2026-10-28T00:00:00+00:00",
            "media_url": f"https://t.me/source{source_id}/1?comment={message_id}",
            "file_path": str(video),
            "size": video.stat().st_size,
        },
    )


async def wait_until(predicate, *, timeout: float = 3.0) -> None:
    async with asyncio.timeout(timeout):
        while not predicate():
            await asyncio.sleep(0.01)


async def test_selected_frame_session_is_strictly_scoped_and_bounded(tmp_path, monkeypatch):
    archive = tmp_path / "archive"
    archive.mkdir()
    store = Store(tmp_path / "db.sqlite", migrate_legacy=False)
    first = store.add_source(Post("first", 1), str(archive))
    second = store.add_source(Post("second", 2), str(archive))
    for message_id in range(1, 13):
        add_download(store, first, message_id, 100 + message_id, archive)
    add_download(store, second, 99, 999, archive)
    service = Service(store=store, engine=FakeEngine())
    monkeypatch.setattr(service, "_require_frame_tools", lambda: None)
    calls: list[tuple[int, int]] = []
    maximum_queued = 0

    async def process(payload: dict) -> str:
        nonlocal maximum_queued
        calls.append((int(payload["source_id"]), int(payload["message_id"])))
        maximum_queued = max(
            maximum_queued,
            int(
                store.db.execute(
                    """SELECT COUNT(*) FROM jobs WHERE kind='telegram.video_frames'
                    AND state IN ('queued','running')"""
                ).fetchone()[0]
            ),
        )
        await asyncio.sleep(0.005)
        return "done"

    service.media_runner.handlers["telegram.video_frames"] = process
    chosen = list(range(1, 13))
    result = await service.handle(
        "telegram_frames_selected_start",
        {"source_id": first, "message_ids": chosen, "confirmed": True},
    )
    assert result["session"]["selected_count"] == 12
    await wait_until(lambda: service._frame_mode == "off")

    assert calls == [(first, message_id) for message_id in chosen]
    assert maximum_queued <= 8
    foreign = store.db.execute(
        """SELECT state FROM jobs WHERE kind='telegram.video_frames'
        AND json_extract(payload,'$.source_id')=?""",
        (second,),
    ).fetchone()
    assert foreign is not None and foreign["state"] == "waiting"
    status = await service.handle("telegram_frames_session_status", {})
    assert status["session"]["state"] == "completed"
    assert status["session"]["completed"] == 12
    store.close()


@pytest.mark.parametrize(
    ("message_ids", "error"),
    (([], "хотя бы один"), ([1, 1], "дубликаты"), (list(range(1, 102)), "100")),
)
async def test_selected_frame_validation_rejects_bad_batches(
    tmp_path, monkeypatch, message_ids, error
):
    archive = tmp_path / "archive"
    archive.mkdir()
    store = Store(tmp_path / "db.sqlite", migrate_legacy=False)
    source_id = store.add_source(Post("first", 1), str(archive))
    add_download(store, source_id, 1, 101, archive)
    service = Service(store=store, engine=FakeEngine())
    monkeypatch.setattr(service, "_require_frame_tools", lambda: None)

    with pytest.raises(ValueError, match=error):
        await service.handle(
            "telegram_frames_selected_start",
            {"source_id": source_id, "message_ids": message_ids, "confirmed": True},
        )
    assert service.media_runner.running is False
    store.close()


async def test_selected_frame_validation_rejects_foreign_message(tmp_path, monkeypatch):
    archive = tmp_path / "archive"
    archive.mkdir()
    store = Store(tmp_path / "db.sqlite", migrate_legacy=False)
    first = store.add_source(Post("first", 1), str(archive))
    second = store.add_source(Post("second", 2), str(archive))
    add_download(store, first, 1, 101, archive)
    add_download(store, second, 2, 102, archive)
    service = Service(store=store, engine=FakeEngine())
    monkeypatch.setattr(service, "_require_frame_tools", lambda: None)

    with pytest.raises(ValueError, match="не принадлежит"):
        await service.handle(
            "telegram_frames_selected_start",
            {"source_id": first, "message_ids": [2], "confirmed": True},
        )
    store.close()


async def test_source_frame_session_never_runs_another_publication(tmp_path, monkeypatch):
    archive = tmp_path / "archive"
    archive.mkdir()
    store = Store(tmp_path / "db.sqlite", migrate_legacy=False)
    first = store.add_source(Post("first", 1), str(archive))
    second = store.add_source(Post("second", 2), str(archive))
    for message_id in (1, 2, 3):
        add_download(store, first, message_id, 100 + message_id, archive)
    add_download(store, second, 4, 104, archive)
    service = Service(store=store, engine=FakeEngine())
    monkeypatch.setattr(service, "_require_frame_tools", lambda: None)
    calls: list[tuple[int, int]] = []

    async def process(payload: dict) -> str:
        source = int(payload["source_id"])
        message = int(payload["message_id"])
        calls.append((source, message))
        with store.db:
            store.db.execute(
                """UPDATE tg_video_frame_sets SET status='saved'
                WHERE source_id=? AND message_id=?""",
                (source, message),
            )
        return "done"

    service.media_runner.handlers["telegram.video_frames"] = process
    await service.handle(
        "telegram_frames_source_start", {"source_id": first, "confirmed": True}
    )
    await wait_until(lambda: service._frame_mode == "off")

    assert calls == [(first, 1), (first, 2), (first, 3)]
    assert store.telegram.get_setting(f"frames_source_complete:{first}") == "1"
    other = store.db.execute(
        "SELECT state,payload FROM jobs WHERE unique_key=?",
        (f"video-frames:{second}:4",),
    ).fetchone()
    assert other["state"] == "waiting"
    assert json.loads(other["payload"])["source_id"] == second
    store.close()


def test_psl_selector_supports_source_plus_explicit_authors(tmp_path):
    archive = tmp_path / "archive"
    archive.mkdir()
    store = Store(tmp_path / "db.sqlite", migrate_legacy=False)
    first = store.add_source(Post("first", 1), str(archive))
    second = store.add_source(Post("second", 2), str(archive))
    for source_id, author_id in ((first, 101), (first, 102), (second, 103)):
        store.telegram.observe_profile(
            author_id=author_id,
            username=f"user{author_id}",
            display_name=f"User {author_id}",
            source_id=source_id,
        )
        photo = archive / f"{author_id}.jpg"
        photo.write_bytes(f"photo-{author_id}".encode())
        store.telegram.record_profile_photo(
            author_id,
            f"photo-{author_id}",
            file_path=str(photo),
            size=photo.stat().st_size,
            photo_date="2026-10-28",
        )
    service = Service(store=store, engine=FakeEngine())

    preview = service.ai_service.preview_telegram_psl_batch(
        {"source_id": first, "author_ids": [102], "image_mode": "photos"}
    )
    assert preview["selected_count"] == 1
    assert preview["items"][0]["author_id"] == 102
    assert preview["source"]["id"] == first
    with pytest.raises(ValueError, match="не принадлежит"):
        service.ai_service.preview_telegram_psl_batch(
            {"source_id": first, "author_ids": [103], "image_mode": "photos"}
        )
    store.close()


async def test_selected_publication_psl_batch_is_durable_scoped_and_idempotent(tmp_path):
    archive = tmp_path / "archive"
    archive.mkdir()
    store = Store(tmp_path / "db.sqlite", migrate_legacy=False)
    source_id = store.add_source(Post("first", 1), str(archive))
    for author_id in (101, 102):
        store.telegram.observe_profile(
            author_id=author_id,
            username=f"user{author_id}",
            display_name=f"User {author_id}",
            source_id=source_id,
        )
        photo = archive / f"{author_id}.jpg"
        photo.write_bytes(f"photo-{author_id}".encode())
        store.telegram.record_profile_photo(
            author_id,
            f"photo-{author_id}",
            file_path=str(photo),
            size=photo.stat().st_size,
            photo_date="2026-10-28",
        )
    service = Service(
        store=store,
        engine=FakeEngine(),
        ai_core=AICore({"deepseek": FakeProvider()}),
    )
    preview = await service.handle(
        "ai_telegram_batch_preview",
        {"source_id": source_id, "author_ids": [102], "image_mode": "photos"},
    )
    request = {
        "source_id": source_id,
        "author_ids": [102],
        "image_mode": "photos",
        "operation_id": "round6-selected",
        "external_confirmed": True,
        "selection_snapshot": preview["preview"]["selection_snapshot"],
    }

    selected_photo = archive / "102.jpg"
    original_photo = selected_photo.read_bytes()
    selected_photo.write_bytes(original_photo + b"changed")
    with pytest.raises(ValueError, match="изменились после предпросмотра"):
        await service.handle("ai_telegram_batch_start", request)
    selected_photo.write_bytes(original_photo)

    first = await service.handle("ai_telegram_batch_start", request)
    repeated = await service.handle("ai_telegram_batch_start", request)
    batch = first["batch"]
    assert repeated["batch"]["id"] == batch["id"]
    assert batch["source_id"] == source_id
    assert batch["selected_count"] == 1
    assert batch["items"][0]["author_id"] == 102
    listed = await service.handle(
        "ai_telegram_batch_list", {"source_id": source_id, "limit": 10}
    )
    assert [item["id"] for item in listed["items"]] == [batch["id"]]
    assert store.db.execute(
        "SELECT COUNT(*) FROM ai_telegram_batches WHERE operation_key=?",
        ("telegram.psl.batch:round6-selected",),
    ).fetchone()[0] == 1
    store.close()
