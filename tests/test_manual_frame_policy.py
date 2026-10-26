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
from hoard.tui.api import IPCStatusAPI


class FakeEngine:
    authorization_step = AuthState.DISCONNECTED

    async def disconnect(self) -> None:
        return None


def add_download(store: Store, source_id: int, message_id: int, root: Path) -> None:
    video = root / f"video-{message_id}.mp4"
    video.write_bytes(b"video" * 32)
    store.checkpoint(
        source_id,
        message_id,
        {
            "source_id": source_id,
            "message_id": message_id,
            "author_id": 100 + message_id,
            "author_username": f"user{message_id}",
            "author_name": f"User {message_id}",
            "date": "2026-10-26T00:00:00+00:00",
            "media_url": f"https://t.me/channelx/7?comment={message_id}",
            "file_path": str(video),
            "size": video.stat().st_size,
        },
    )


async def wait_until(predicate, *, timeout: float = 3.0) -> None:
    async with asyncio.timeout(timeout):
        while not predicate():
            await asyncio.sleep(0.01)


async def test_daemon_start_and_new_download_leave_ffmpeg_permission_off(tmp_path):
    archive = tmp_path / "archive"
    archive.mkdir()
    store = Store(tmp_path / "db.sqlite", migrate_legacy=False)
    source_id = store.add_source(Post("channelx", 7), str(archive))
    add_download(store, source_id, 11, archive)
    service = Service(store=store, engine=FakeEngine())

    await service.start_background()
    await asyncio.sleep(0.05)

    job = store.db.execute(
        "SELECT state FROM jobs WHERE kind='telegram.video_frames'"
    ).fetchone()
    assert job is not None and job["state"] == "waiting"
    assert service.media_runner.running is False
    assert service._frame_mode == "off"
    await service.stop_background()
    store.close()


async def test_restart_revokes_old_queued_and_running_frame_jobs(tmp_path):
    archive = tmp_path / "archive"
    archive.mkdir()
    filename = tmp_path / "db.sqlite"
    first = Store(filename, migrate_legacy=False)
    source_id = first.add_source(Post("channelx", 7), str(archive))
    add_download(first, source_id, 11, archive)
    with first.db:
        first.db.execute(
            "UPDATE jobs SET state='running' WHERE kind='telegram.video_frames'"
        )
        first.db.execute(
            "UPDATE tg_video_frame_sets SET status='running' WHERE source_id=?",
            (source_id,),
        )
        first.telegram._set_setting_sql("frames_backfill_active", "1")
    first.close()

    second = Store(filename, migrate_legacy=False)
    service = Service(store=second, engine=FakeEngine())
    await service.start_background()

    assert second.db.execute(
        "SELECT state FROM jobs WHERE kind='telegram.video_frames'"
    ).fetchone()[0] == "waiting"
    assert second.telegram.frame_set(source_id, 11)["status"] == "pending"
    assert second.telegram.frame_backfill_active() is False
    assert service.media_runner.running is False
    await service.stop_background()
    second.close()


async def test_single_manual_run_executes_only_selected_waiting_job(tmp_path, monkeypatch):
    archive = tmp_path / "archive"
    archive.mkdir()
    store = Store(tmp_path / "db.sqlite", migrate_legacy=False)
    source_id = store.add_source(Post("channelx", 7), str(archive))
    add_download(store, source_id, 11, archive)
    add_download(store, source_id, 12, archive)
    service = Service(store=store, engine=FakeEngine())
    monkeypatch.setattr(service, "_require_frame_tools", lambda: None)
    calls: list[int] = []

    async def process(payload: dict) -> str:
        calls.append(int(payload["message_id"]))
        return "done"

    service.media_runner.handlers["telegram.video_frames"] = process
    result = await service.handle(
        "telegram_frames_process", {"source_id": source_id, "message_id": 12}
    )
    assert result["job_id"] is not None
    await wait_until(lambda: service._frame_mode == "off")

    assert calls == [12]
    states = {
        json.loads(row["payload"])["message_id"]: row["state"]
        for row in store.db.execute(
            "SELECT payload,state FROM jobs WHERE kind='telegram.video_frames'"
        )
    }
    assert states == {11: "waiting", 12: "done"}
    assert service.media_runner.running is False
    store.close()


async def test_bulk_requires_confirmation_and_retry_does_not_grant_permission(
    tmp_path, monkeypatch
):
    archive = tmp_path / "archive"
    archive.mkdir()
    store = Store(tmp_path / "db.sqlite", migrate_legacy=False)
    source_id = store.add_source(Post("channelx", 7), str(archive))
    add_download(store, source_id, 11, archive)
    service = Service(store=store, engine=FakeEngine())
    monkeypatch.setattr(service, "_require_frame_tools", lambda: None)

    with pytest.raises(ValueError, match="подтверждения"):
        await service.handle("telegram_frames_backfill_start", {})

    job_id = store.db.execute(
        "SELECT id FROM jobs WHERE kind='telegram.video_frames'"
    ).fetchone()[0]
    with store.db:
        store.db.execute("UPDATE jobs SET state='failed' WHERE id=?", (job_id,))
    retried = await service.handle("job_retry", {"id": job_id})
    assert "явного запуска" in retried["message"]
    state = store.db.execute("SELECT state FROM jobs WHERE id=?", (job_id,)).fetchone()[0]
    assert state == "waiting"
    assert service.media_runner.running is False
    store.close()


async def test_confirmed_bulk_run_processes_all_waiting_with_one_worker(
    tmp_path, monkeypatch
):
    archive = tmp_path / "archive"
    archive.mkdir()
    store = Store(tmp_path / "db.sqlite", migrate_legacy=False)
    source_id = store.add_source(Post("channelx", 7), str(archive))
    for message_id in (11, 12, 13):
        add_download(store, source_id, message_id, archive)
    service = Service(store=store, engine=FakeEngine())
    monkeypatch.setattr(service, "_require_frame_tools", lambda: None)
    calls: list[int] = []
    active = 0
    maximum_active = 0

    async def process(payload: dict) -> str:
        nonlocal active, maximum_active
        message_id = int(payload["message_id"])
        calls.append(message_id)
        active += 1
        maximum_active = max(maximum_active, active)
        await asyncio.sleep(0.01)
        with store.db:
            store.db.execute(
                """UPDATE tg_video_frame_sets SET status='saved'
                WHERE source_id=? AND message_id=?""",
                (source_id, message_id),
            )
        active -= 1
        return "done"

    service.media_runner.handlers["telegram.video_frames"] = process
    result = await service.handle(
        "telegram_frames_backfill_start", {"confirmed": True}
    )
    assert "Ручная обработка запущена" in result["message"]
    await wait_until(lambda: service._frame_mode == "off")

    assert sorted(calls) == [11, 12, 13]
    assert maximum_active == 1
    assert service.media_runner.running is False
    assert store.db.execute(
        "SELECT COUNT(*) FROM jobs WHERE kind='telegram.video_frames' AND state='waiting'"
    ).fetchone()[0] == 0
    store.close()


async def test_hard_stop_cleans_partial_files_and_job_can_run_after_restart(
    tmp_path, monkeypatch
):
    archive = tmp_path / "archive"
    archive.mkdir()
    filename = tmp_path / "db.sqlite"
    store = Store(filename, migrate_legacy=False)
    source_id = store.add_source(Post("channelx", 7), str(archive))
    add_download(store, source_id, 11, archive)
    service = Service(store=store, engine=FakeEngine())
    monkeypatch.setattr(service, "_require_frame_tools", lambda: None)
    extraction_started = asyncio.Event()
    extraction_cancelled = asyncio.Event()

    async def blocking_extract(_video, destination):
        (Path(destination) / "frame_01.jpg").write_bytes(b"partial")
        extraction_started.set()
        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            extraction_cancelled.set()
            raise

    monkeypatch.setattr("hoard.service.extract_frames", blocking_extract)
    await service.handle(
        "telegram_frames_process", {"source_id": source_id, "message_id": 11}
    )
    await asyncio.wait_for(extraction_started.wait(), timeout=2)
    stopped = await service.handle("telegram_frames_backfill_pause", {})

    assert "FFmpeg остановлен" in stopped["message"]
    assert extraction_cancelled.is_set()
    assert list(archive.glob(".*_frames-*.tmp")) == []
    assert not (archive / "video-11_frames").exists()
    assert (archive / "video-11.mp4").is_file()
    assert store.telegram.frame_set(source_id, 11)["status"] == "pending"
    assert store.db.execute(
        "SELECT state FROM jobs WHERE kind='telegram.video_frames'"
    ).fetchone()[0] == "waiting"
    await service.stop_background()
    store.close()

    reopened = Store(filename, migrate_legacy=False)
    restarted = Service(store=reopened, engine=FakeEngine())
    monkeypatch.setattr(restarted, "_require_frame_tools", lambda: None)
    calls: list[int] = []

    async def finish(payload: dict) -> str:
        calls.append(int(payload["message_id"]))
        return "done"

    restarted.media_runner.handlers["telegram.video_frames"] = finish
    await restarted.start_background()
    await asyncio.sleep(0.05)
    assert calls == []
    await restarted.handle(
        "telegram_frames_process", {"source_id": source_id, "message_id": 11}
    )
    await wait_until(lambda: restarted._frame_mode == "off")
    assert calls == [11]
    await restarted.stop_background()
    reopened.close()


async def test_telegram_start_cancels_frames_before_runner_starts(tmp_path, monkeypatch):
    archive = tmp_path / "archive"
    archive.mkdir()
    store = Store(tmp_path / "db.sqlite", migrate_legacy=False)
    source_id = store.add_source(Post("channelx", 7), str(archive))
    store.set_enabled(source_id, False)
    add_download(store, source_id, 11, archive)
    service = Service(store=store, engine=FakeEngine())
    monkeypatch.setattr(service, "_require_frame_tools", lambda: None)
    started = asyncio.Event()
    cancelled = asyncio.Event()

    async def process(_payload: dict) -> str:
        started.set()
        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            cancelled.set()
            raise

    service.media_runner.handlers["telegram.video_frames"] = process
    await service.handle(
        "telegram_frames_process", {"source_id": source_id, "message_id": 11}
    )
    await asyncio.wait_for(started.wait(), timeout=2)

    result = await service.handle("start", {})

    assert "обработка кадров остановлена" in result["message"].lower()
    assert cancelled.is_set()
    assert service.running is True
    assert service.media_runner.running is False
    assert service._frame_mode == "off"
    assert store.db.execute(
        "SELECT state FROM jobs WHERE kind='telegram.video_frames'"
    ).fetchone()[0] == "waiting"
    await service.stop()
    store.close()


async def test_simultaneous_manual_frames_and_telegram_never_leave_both_modes_running(
    tmp_path, monkeypatch
):
    archive = tmp_path / "archive"
    archive.mkdir()
    store = Store(tmp_path / "db.sqlite", migrate_legacy=False)
    source_id = store.add_source(Post("channelx", 7), str(archive))
    store.set_enabled(source_id, False)
    add_download(store, source_id, 11, archive)
    service = Service(store=store, engine=FakeEngine())
    monkeypatch.setattr(service, "_require_frame_tools", lambda: None)

    async def process(_payload: dict) -> str:
        await asyncio.sleep(0.2)
        return "done"

    service.media_runner.handlers["telegram.video_frames"] = process
    results = await asyncio.gather(
        service.handle(
            "telegram_frames_process", {"source_id": source_id, "message_id": 11}
        ),
        service.handle("start", {}),
        return_exceptions=True,
    )

    assert any(isinstance(item, dict) and "message" in item for item in results)
    assert not (service.running and service.media_runner.running)
    assert service.running is True
    await service.stop()
    store.close()


async def test_brief_status_is_bounded_and_omits_large_detail_lists(tmp_path):
    store = Store(tmp_path / "db.sqlite", migrate_legacy=False)
    service = Service(store=store, engine=FakeEngine())

    brief = await service.handle("status_brief", {})
    payload = encode_ipc_message({"ok": True, "data": brief})

    assert len(payload) < IPC_MAX_BYTES
    assert {"sources", "downloads", "profiles", "jobs", "video_frames"}.isdisjoint(brief)
    assert {"stats", "queue", "events", "progress"}.issubset(brief)
    store.close()


async def test_full_status_preserves_shape_and_truncates_to_ipc_limit(tmp_path):
    store = Store(tmp_path / "db.sqlite", migrate_legacy=False)
    with store.db:
        store.db.executemany(
            """INSERT INTO sources(username,post_id,url,output,interval,title,last_error)
            VALUES(?,?,?,?,120,?,?)""",
            [
                (
                    f"channel_{index}",
                    index,
                    f"https://t.me/channel_{index}/{index}",
                    str(tmp_path),
                    f"Channel {index}",
                    "x" * 500,
                )
                for index in range(300)
            ],
        )
    service = Service(store=store, engine=FakeEngine())

    status = await service.handle("status", {})
    payload = encode_ipc_message({"ok": True, "data": status})

    assert len(payload) < IPC_MAX_BYTES
    assert {"sources", "downloads", "profiles", "jobs", "events"}.issubset(status)
    assert status["status_truncated"]["sources"]["available"] == 300
    store.close()


async def test_typed_telegram_api_uses_private_ipc_without_ai_alias(monkeypatch):
    calls: list[tuple[str, dict, float]] = []

    async def fake_request(method: str, args=None, *, timeout: float):
        calls.append((method, args or {}, timeout))
        return {"message": "ok"}

    monkeypatch.setattr("hoard.tui.api.request", fake_request)
    api = IPCStatusAPI()
    await api.telegram_call("telegram_frames_get", {"source_id": 1, "message_id": 2})

    assert calls[0][0] == "telegram_frames_get"
    with pytest.raises(ValueError, match="Telegram IPC"):
        await api.telegram_call("ai_prompt_list", {})
