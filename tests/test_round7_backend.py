from __future__ import annotations

import asyncio
import json
from pathlib import Path

import pytest

from hoard.ipc import IPC_MAX_BYTES, encode_ipc_message
from hoard.models import Post
from hoard.service import Service
from hoard.storage import Store
from hoard.telegram import AuthState


class FakeEngine:
    authorization_step = AuthState.DISCONNECTED

    async def disconnect(self) -> None:
        return None


def add_download(
    store: Store,
    source_id: int,
    message_id: int,
    author_id: int,
    archive: Path,
) -> Path:
    video = archive / f"{source_id}-{message_id}.mp4"
    video.write_bytes(b"video" * 20)
    store.checkpoint(
        source_id,
        message_id,
        {
            "source_id": source_id,
            "message_id": message_id,
            "author_id": author_id,
            "author_username": f"user{author_id}",
            "author_name": f"User {author_id}",
            "date": f"2026-10-29T00:{message_id % 60:02d}:00+00:00",
            "media_url": f"https://t.me/source/1?comment={message_id}",
            "file_path": str(video),
            "size": video.stat().st_size,
        },
    )
    return video


def test_jobs_catalog_is_bounded_safe_and_retry_is_idempotent(tmp_path):
    store = Store(tmp_path / "db.sqlite", migrate_legacy=False)
    failed = store.enqueue("telegram.report", {"archive_root": "/private/archive"})
    store.finish_job(failed, "failed", "x" * 500)
    ai_job = store.enqueue("ai.request", {"api_key": "must-not-leak"})
    store.finish_job(ai_job, "failed", "provider error")
    waiting = store.enqueue("telegram.video_frames", {"source_id": 1, "message_id": 2})
    store.finish_job(waiting, "failed", "ffmpeg error")
    store.retry_job(waiting, target_state="waiting")

    page = store.jobs_list(limit=2, offset=0, state="failed", kind="telegram")
    assert page["total"] == 1
    assert page["items"][0]["id"] == failed
    assert page["items"][0]["retryable"] is True
    assert len(page["items"][0]["result"].encode()) <= 300
    assert "payload" not in page["items"][0]
    assert "must-not-leak" not in json.dumps(page)
    assert store.job_details(waiting)["manual_permission_required"] is True

    assert store.retry_job(failed) == failed
    assert store.retry_job(failed) == failed
    state = store.db.execute("SELECT state FROM jobs WHERE id=?", (failed,)).fetchone()[0]
    assert state == "queued"
    done = store.enqueue("telegram.report", {"archive_root": "/other"})
    store.finish_job(done, "done", "ok")
    with pytest.raises(ValueError, match="failed"):
        store.retry_job(done)
    with pytest.raises(ValueError, match="не найдено"):
        store.retry_job(999_999)
    with pytest.raises(ValueError, match="limit"):
        store.jobs_list(limit=101)
    with pytest.raises(ValueError, match="состояние"):
        store.jobs_list(state="unknown")
    store.close()


@pytest.mark.asyncio
async def test_service_rejects_generic_ai_retry_and_keeps_frame_retry_manual(tmp_path):
    store = Store(tmp_path / "db.sqlite", migrate_legacy=False)
    ai_job = store.enqueue("ai.request", {})
    store.finish_job(ai_job, "failed", "failed")
    frame_job = store.enqueue("telegram.video_frames", {"source_id": 1, "message_id": 2})
    store.finish_job(frame_job, "failed", "failed")
    service = Service(store=store, engine=FakeEngine())

    with pytest.raises(ValueError, match="ai_attempt_retry"):
        await service.handle("job_retry", {"id": ai_job})
    first = await service.handle("job_retry", {"id": frame_job})
    second = await service.handle("job_retry", {"id": frame_job})
    assert first["job_id"] == second["job_id"] == frame_job
    state = store.db.execute("SELECT state FROM jobs WHERE id=?", (frame_job,)).fetchone()[0]
    assert state == "waiting"
    assert service.media_runner.running is False
    store.close()


def test_global_media_catalog_is_paginated_searchable_and_does_not_stat_pages(
    tmp_path, monkeypatch
):
    archive = tmp_path / "archive"
    archive.mkdir()
    store = Store(tmp_path / "db.sqlite", migrate_legacy=False)
    source = store.add_source(Post("channel", 7), str(archive))
    for message_id in range(1, 121):
        add_download(store, source, message_id, 1000 + message_id, archive)

    def forbidden_stat(self):
        raise AssertionError("media list must not stat archive files")

    monkeypatch.setattr(Path, "stat", forbidden_stat)
    page = store.telegram_media_list(limit=50, offset=50, query="channel")
    assert page["total"] == 120
    assert len(page["items"]) == 50
    assert page["items"][0]["message_id"] == 70
    assert len(encode_ipc_message({"ok": True, "data": page})) < IPC_MAX_BYTES
    with pytest.raises(ValueError, match="порядок"):
        store.telegram_media_list(sort="raw SQL")
    store.close()


@pytest.mark.asyncio
async def test_global_media_and_catalog_open_use_daemon_validated_files(tmp_path):
    archive = tmp_path / "archive"
    archive.mkdir()
    store = Store(tmp_path / "db.sqlite", migrate_legacy=False)
    source = store.add_source(Post("channel", 7), str(archive))
    video = add_download(store, source, 10, 1010, archive)
    catalog = archive / "telegram_archive.html"
    catalog.write_text("<!doctype html>", encoding="utf-8")
    opened: list[Path] = []
    service = Service(
        store=store,
        engine=FakeEngine(),
        media_opener=lambda path: opened.append(path) is None,
    )

    await service.handle(
        "telegram_media_open",
        {"kind": "video", "source_id": source, "message_id": 10},
    )
    await service.handle("telegram_catalog_open", {})
    assert opened == [video.resolve(), catalog.resolve()]
    with pytest.raises(ValueError, match="не найдено"):
        await service.handle(
            "telegram_media_open",
            {"kind": "video", "source_id": source, "message_id": 999},
        )
    store.close()


def test_global_profiles_include_legacy_unlinked_records_and_details(tmp_path):
    store = Store(tmp_path / "db.sqlite", migrate_legacy=False)
    with store.db:
        store.db.execute(
            """INSERT INTO tg_profiles(
            author_id,current_username,display_name,first_seen,last_seen)
            VALUES(501,'legacy_name','Legacy User','2025','2026')"""
        )
        store.db.execute(
            """INSERT INTO tg_username_history(author_id,username,first_seen,last_seen)
            VALUES(501,'old_name','2024','2025')"""
        )
    page = store.telegram.profile_summaries(query="old_name", limit=50)
    assert page["total"] == 1
    assert page["items"][0]["author_id"] == 501
    assert page["items"][0]["source_count"] == 0
    details = store.telegram.get_profile(501)
    assert details["sources"] == []
    assert details["username_history"][0]["username"] == "old_name"
    store.close()


def test_batch_catalog_is_paginated_source_scoped_and_has_no_participant_n_plus_one(tmp_path):
    store = Store(tmp_path / "db.sqlite", migrate_legacy=False)
    source = store.add_source(Post("channel", 7), str(tmp_path / "archive"))
    with store.db:
        for number in range(30):
            store.db.execute(
                """INSERT INTO ai_telegram_batches(
                id,operation_key,source_id,selected_count,request_payload,created_at)
                VALUES(?,?,?,?,?,?)""",
                (
                    f"batch-{number:02d}",
                    f"operation-{number:02d}",
                    source,
                    2,
                    json.dumps({"image_mode": "combined"}),
                    f"2026-10-29T00:{number:02d}:00+00:00",
                ),
            )
            store.db.executemany(
                """INSERT INTO ai_telegram_batch_items(
                batch_id,author_id,state,reason) VALUES(?,?,'skipped',?)""",
                (
                    (f"batch-{number:02d}", 1000 + number * 2, "insufficient_data"),
                    (f"batch-{number:02d}", 1001 + number * 2, "ineligible"),
                ),
            )
    page = store.ai.telegram_batch_catalog(source_id=source, limit=10, offset=10)
    assert page["total"] == 30
    assert len(page["items"]) == 10
    assert all("items" not in item for item in page["items"])
    assert all(item["image_mode"] == "combined" for item in page["items"])
    assert len(encode_ipc_message({"ok": True, "data": page})) < IPC_MAX_BYTES
    store.close()


async def wait_until(predicate, *, timeout: float = 3.0) -> None:
    async with asyncio.timeout(timeout):
        while not predicate():
            await asyncio.sleep(0.01)


@pytest.mark.asyncio
async def test_frame_session_reports_success_separately_from_completed(tmp_path, monkeypatch):
    archive = tmp_path / "archive"
    archive.mkdir()
    store = Store(tmp_path / "db.sqlite", migrate_legacy=False)
    source = store.add_source(Post("channel", 7), str(archive))
    add_download(store, source, 1, 1001, archive)
    add_download(store, source, 2, 1002, archive)
    service = Service(store=store, engine=FakeEngine())
    monkeypatch.setattr(service, "_require_frame_tools", lambda: None)

    async def process(payload):
        if int(payload["message_id"]) == 2:
            raise RuntimeError("synthetic ffmpeg failure")
        return "done"

    service.media_runner.handlers["telegram.video_frames"] = process
    await service.handle(
        "telegram_frames_selected_start",
        {"source_id": source, "message_ids": [1, 2], "confirmed": True},
    )
    await wait_until(lambda: service._frame_mode == "off")
    status = await service.handle("telegram_frames_session_status", {})
    assert status["session"]["completed"] == 2
    assert status["session"]["successful"] == 1
    assert status["session"]["errors"] == 1
    store.close()


@pytest.mark.asyncio
async def test_frame_stop_revalidates_session_and_publication_scope(tmp_path, monkeypatch):
    archive = tmp_path / "archive"
    archive.mkdir()
    store = Store(tmp_path / "db.sqlite", migrate_legacy=False)
    first = store.add_source(Post("first", 1), str(archive))
    second = store.add_source(Post("second", 2), str(archive))
    add_download(store, first, 1, 1001, archive)
    add_download(store, second, 2, 1002, archive)
    service = Service(store=store, engine=FakeEngine())
    monkeypatch.setattr(service, "_require_frame_tools", lambda: None)
    started = asyncio.Event()

    async def process(_payload):
        started.set()
        await asyncio.Event().wait()

    service.media_runner.handlers["telegram.video_frames"] = process
    result = await service.handle(
        "telegram_frames_source_start", {"source_id": first, "confirmed": True}
    )
    session_id = result["session"]["session_id"]
    await asyncio.wait_for(started.wait(), timeout=2)

    with pytest.raises(RuntimeError, match="изменилась"):
        await service.handle(
            "telegram_frames_backfill_pause",
            {"expected_session_id": "stale-session", "expected_source_id": first},
        )
    with pytest.raises(RuntimeError, match="другой публикации"):
        await service.handle(
            "telegram_frames_backfill_pause",
            {"expected_session_id": session_id, "expected_source_id": second},
        )
    assert service.media_runner.running is True
    stopped = await service.handle(
        "telegram_frames_backfill_pause",
        {"expected_session_id": session_id, "expected_source_id": first},
    )
    assert "FFmpeg остановлен" in stopped["message"]
    assert service.media_runner.running is False
    store.close()
