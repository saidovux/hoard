"""Durable frame jobs, service extraction and old-archive backfill."""
from __future__ import annotations

import asyncio
import json
import os
import shutil
import subprocess
import time
from pathlib import Path

import pytest

from hoard import video_frames as video_frames_module
from hoard.ai import AICore
from hoard.ai.provider import AIProvider
from hoard.archive_paths import video_note_path
from hoard.catalog import build_catalog
from hoard.models import Post
from hoard.service import Service
from hoard.storage import Store
from hoard.telegram import AuthState


class FakeEngine:
    authorization_step = AuthState.DISCONNECTED

    async def disconnect(self):
        return None


class UnusedProvider(AIProvider):
    name = "deepseek"

    @property
    def configured(self):
        return True

    async def complete(self, request):
        raise AssertionError("frame extraction must never call AI")


def make_service(store: Store) -> Service:
    return Service(
        store=store,
        engine=FakeEngine(),
        ai_core=AICore({"deepseek": UnusedProvider()}),
    )


def make_video(path: Path, *, duration: str = "3") -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    subprocess.run(
        [
            "ffmpeg",
            "-v",
            "error",
            "-f",
            "lavfi",
            "-i",
            "testsrc2=size=320x240:rate=10",
            "-t",
            duration,
            "-pix_fmt",
            "yuv420p",
            "-c:v",
            "libx264",
            "-y",
            str(path),
        ],
        check=True,
        capture_output=True,
    )


def create_download(store: Store, source_id: int, message_id: int, video: Path) -> None:
    store.checkpoint(
        source_id,
        message_id,
        {
            "source_id": source_id,
            "message_id": message_id,
            "author_id": 101,
            "author_username": "alice",
            "author_name": "Alice",
            "date": "2026-10-25T00:00:00+00:00",
            "media_url": f"https://t.me/channelx/7?comment={message_id}",
            "file_path": str(video),
            "size": video.stat().st_size,
        },
    )


def test_checkpoint_creates_one_waiting_frame_job_and_no_ai_job(tmp_path):
    store = Store(tmp_path / "db.sqlite")
    source_id = store.add_source(Post("channelx", 7), str(tmp_path / "archive"))
    video = tmp_path / "archive" / "video.mp4"
    video.parent.mkdir(parents=True)
    video.write_bytes(b"x" * 100)
    create_download(store, source_id, 11, video)
    create_download(store, source_id, 11, video)

    assert store.db.execute("SELECT COUNT(*) FROM tg_video_frame_sets").fetchone()[0] == 1
    jobs = store.db.execute(
        "SELECT kind,state FROM jobs WHERE kind='telegram.video_frames'"
    ).fetchall()
    assert len(jobs) == 1
    assert jobs[0]["state"] == "waiting"
    assert store.db.execute("SELECT COUNT(*) FROM jobs WHERE kind='ai.chat'").fetchone()[0] == 0
    row = store.db.execute(
        "SELECT status,video_path,video_size FROM tg_video_frame_sets"
    ).fetchone()
    assert row["status"] == "pending"
    assert Path(row["video_path"]) == video.resolve()
    store.close()


@pytest.mark.skipif(
    shutil.which("ffmpeg") is None or shutil.which("ffprobe") is None,
    reason="FFmpeg is not installed",
)
async def test_service_extracts_three_frames_and_catalog_exposes_safe_media(tmp_path):
    archive = tmp_path / "archive"
    archive.mkdir()
    store = Store(tmp_path / "db.sqlite")
    source_id = store.add_source(Post("channelx", 7), str(archive))
    video = video_note_path(archive, source_id, 101, 11)
    make_video(video)
    original = video.read_bytes()
    create_download(store, source_id, 11, video)
    service = make_service(store)
    try:
        result = await service._telegram_video_frames(
            {"source_id": source_id, "message_id": 11}
        )
        assert "три кадра" in result.lower()
        details = store.telegram.frame_details(source_id, 11)
        assert details is not None and details["valid"] is True
        assert details["status"] == "saved"
        assert [frame["position"] for frame in details["frames"]] == [1, 2, 3]
        assert all(Path(frame["file_path"]).is_file() for frame in details["frames"])
        assert all(
            os.stat(frame["file_path"]).st_mode & 0o777 == 0o600
            for frame in details["frames"]
        )
        assert video.read_bytes() == original
        assert store.db.execute("SELECT COUNT(*) FROM jobs WHERE kind='ai.chat'").fetchone()[0] == 0
        task = service._catalog_task
        assert task is not None
        await asyncio.wait_for(task, timeout=5)
        assert store.telegram.catalog_dirty() is False
        assert (archive / "telegram_archive.html").is_file()

        before_jobs = store.db.execute(
            "SELECT COUNT(*) FROM jobs WHERE kind='telegram.video_frames'"
        ).fetchone()[0]
        create_download(store, source_id, 11, video)
        after_jobs = store.db.execute(
            "SELECT COUNT(*) FROM jobs WHERE kind='telegram.video_frames'"
        ).fetchone()[0]
        assert after_jobs == before_jobs

        repeated = await service._telegram_video_frames(
            {"source_id": source_id, "message_id": 11}
        )
        assert "уже" in repeated.lower()
        assert len(details["frames"]) == 3

        report = build_catalog(store)
        document = report.read_text(encoding="utf-8")
        marker = "window.HOARD_CATALOG="
        payload = json.JSONDecoder().raw_decode(
            document[document.index(marker) + len(marker) :]
        )[0]
        video_item = payload["videos"][0]
        assert video_item["frame_status"] == "saved"
        assert video_item["frame_count"] == 3
        assert len(video_item["frames"]) == 3
        assert all(frame["available"] for frame in video_item["frames"])
        assert all(frame["url"].endswith(".jpg") for frame in video_item["frames"])
        assert all(not frame["url"].startswith("/") for frame in video_item["frames"])
    finally:
        await service.stop_background()
        store.close()


@pytest.mark.skipif(
    shutil.which("ffmpeg") is None or shutil.which("ffprobe") is None,
    reason="FFmpeg is not installed",
)
async def test_missing_ffmpeg_fails_boundedly_and_can_be_retried_without_download(
    tmp_path, monkeypatch
):
    archive = tmp_path / "archive"
    archive.mkdir()
    store = Store(tmp_path / "db.sqlite")
    source_id = store.add_source(Post("channelx", 7), str(archive))
    video = video_note_path(archive, source_id, 101, 11)
    make_video(video)
    create_download(store, source_id, 11, video)
    service = make_service(store)
    real_which = video_frames_module.shutil.which

    def missing_ffmpeg(name):
        if name == "ffmpeg":
            return None
        return real_which(name)

    try:
        monkeypatch.setattr(video_frames_module.shutil, "which", missing_ffmpeg)
        with pytest.raises(RuntimeError, match="FFmpeg"):
            await service._telegram_video_frames({"source_id": source_id, "message_id": 11})
        failed = store.telegram.frame_details(source_id, 11)
        assert failed is not None
        assert failed["status"] == "failed"
        assert failed["error_code"] == "ffmpeg_missing"
        assert video.is_file()

        monkeypatch.undo()
        job_id = store.telegram.queue_frame_job(source_id, 11, reset_failed=True)
        assert job_id is not None
        assert "три кадра" in (
            await service._telegram_video_frames({"source_id": source_id, "message_id": 11})
        ).lower()
        assert store.telegram.frame_details(source_id, 11)["status"] == "saved"
        assert video.is_file()
    finally:
        await service.stop_background()
        store.close()


async def test_backfill_queues_bounded_old_archive_and_can_pause_resume(tmp_path):
    archive = tmp_path / "archive"
    archive.mkdir()
    store = Store(tmp_path / "db.sqlite")
    source_id = store.add_source(Post("channelx", 7), str(archive))
    with store.db:
        store.db.execute(
            "INSERT INTO tg_profiles(author_id,display_name) VALUES(101,'Alice')"
        )
        for message_id in (11, 12, 13):
            video = archive / f"old-{message_id}.mp4"
            video.write_bytes(b"x" * 100)
            store.db.execute(
                """INSERT INTO downloads(source_id,message_id,author_id,author_username,
                author_name,date,media_url,file_path,size,state)
                VALUES(?,?,?,?,?,?,?,?,?,'saved')""",
                (
                    source_id,
                    message_id,
                    101,
                    "alice",
                    "Alice",
                    "2026-01-01",
                    f"https://t.me/channelx/7?comment={message_id}",
                    str(video),
                    video.stat().st_size,
                ),
            )
    service = make_service(store)
    try:
        service._frame_mode = "bulk"
        started = store.telegram.start_frame_backfill()
        assert started["backfill_active"] is True
        service._enqueue_frame_backfill()
        jobs = store.db.execute(
            "SELECT COUNT(*) FROM jobs WHERE kind='telegram.video_frames'"
        ).fetchone()[0]
        assert jobs == 3
        assert store.db.execute("SELECT COUNT(*) FROM tg_video_frame_sets").fetchone()[0] == 3
        paused = await service.handle("telegram_frames_backfill_pause", {})
        assert paused["stats"]["backfill_active"] is False
        assert store.db.execute(
            """SELECT COUNT(*) FROM jobs WHERE kind='telegram.video_frames'
            AND state='waiting'"""
        ).fetchone()[0] == 3
        service._frame_mode = "bulk"
        resumed = store.telegram.start_frame_backfill()
        assert resumed["backfill_active"] is True
        service._enqueue_frame_backfill()
        assert store.db.execute(
            "SELECT COUNT(*) FROM jobs WHERE kind='telegram.video_frames'"
        ).fetchone()[0] == 3
    finally:
        await service.stop_background()
        store.close()


@pytest.mark.skipif(
    shutil.which("ffmpeg") is None or shutil.which("ffprobe") is None,
    reason="FFmpeg is not installed",
)
async def test_waiting_frame_job_requires_manual_run_after_daemon_restart(tmp_path):
    archive = tmp_path / "archive"
    archive.mkdir()
    filename = tmp_path / "db.sqlite"
    first = Store(filename)
    source_id = first.add_source(Post("channelx", 7), str(archive))
    video = video_note_path(archive, source_id, 101, 11)
    make_video(video)
    create_download(first, source_id, 11, video)
    first.close()

    second = Store(filename)
    service = make_service(second)
    try:
        await service.start_background()
        await asyncio.sleep(0.1)
        assert second.telegram.frame_details(source_id, 11)["status"] == "pending"
        assert service.media_runner.running is False
        await service.handle(
            "telegram_frames_process", {"source_id": source_id, "message_id": 11}
        )
        deadline = time.monotonic() + 10
        while time.monotonic() < deadline:
            details = second.telegram.frame_details(source_id, 11)
            if details is not None and details["status"] == "saved":
                break
            await asyncio.sleep(0.05)
        details = second.telegram.frame_details(source_id, 11)
        assert details is not None and details["status"] == "saved"
        assert details["valid"] is True
    finally:
        await service.stop_background()
        second.close()


@pytest.mark.skipif(
    shutil.which("ffmpeg") is None or shutil.which("ffprobe") is None,
    reason="FFmpeg is not installed",
)
async def test_source_video_change_marks_existing_frames_stale(tmp_path):
    archive = tmp_path / "archive"
    archive.mkdir()
    store = Store(tmp_path / "db.sqlite")
    source_id = store.add_source(Post("channelx", 7), str(archive))
    video = video_note_path(archive, source_id, 101, 11)
    make_video(video)
    create_download(store, source_id, 11, video)
    service = make_service(store)
    try:
        await service._telegram_video_frames({"source_id": source_id, "message_id": 11})
        assert store.telegram.frame_details(source_id, 11)["status"] == "saved"
        with video.open("ab") as stream:
            stream.write(b"changed")
        details = store.telegram.frame_details(source_id, 11)
        assert details["status"] == "stale"
        assert details["valid"] is False
        assert "изменился" in details["error"].lower()
        assert store.db.execute("SELECT COUNT(*) FROM jobs WHERE kind='ai.chat'").fetchone()[0] == 0
    finally:
        await service.stop_background()
        store.close()


async def test_backfill_never_fills_more_than_eight_slots_and_resumes_without_loss(tmp_path):
    archive = tmp_path / "archive"
    archive.mkdir()
    store = Store(tmp_path / "backfill.sqlite")
    source_id = store.add_source(Post("channelx", 7), str(archive))
    with store.db:
        store.db.execute(
            "INSERT INTO tg_profiles(author_id,display_name) VALUES(101,'Alice')"
        )
        for index in range(1, 31):
            video = archive / f"old-{index}.mp4"
            video.write_bytes(b"x" * 100)
            store.db.execute(
                """INSERT INTO downloads(source_id,message_id,author_id,author_username,
                author_name,date,media_url,file_path,size,state)
                VALUES(?,?,?,?,?,?,?,?,?,'saved')""",
                (
                    source_id,
                    index,
                    101,
                    "alice",
                    "Alice",
                    f"2026-01-{index:02d}",
                    f"https://t.me/channelx/7?comment={index}",
                    str(video),
                    video.stat().st_size,
                ),
            )
        # Two already-ready sets must be skipped without consuming a queue slot.
        for index in (1, 2):
            store.db.execute(
                """INSERT INTO tg_video_frame_sets(
                source_id,message_id,author_id,video_path,video_size,video_mtime_ns,
                frame_set_id,status,frame_count,updated_at)
                VALUES(?,?,?,?,?,?,?,'saved',3,'2026-01-01')""",
                (
                    source_id,
                    index,
                    101,
                    str(archive / f"old-{index}.mp4"),
                    100,
                    0,
                    f"ready-{index}",
                ),
            )
    service = make_service(store)
    try:
        service._frame_mode = "bulk"
        store.telegram.start_frame_backfill()
        service._enqueue_frame_backfill()
        active = store.db.execute(
            """SELECT COUNT(*) FROM jobs WHERE kind='telegram.video_frames'
            AND state IN ('queued','running')"""
        ).fetchone()[0]
        assert active == 8
        assert store.db.execute(
            "SELECT COUNT(*) FROM tg_video_frame_sets WHERE status='pending'"
        ).fetchone()[0] == 8
        cursor_after_first = store.telegram.get_setting("frames_backfill_cursor")
        service._enqueue_frame_backfill()
        assert store.db.execute(
            """SELECT COUNT(*) FROM jobs WHERE kind='telegram.video_frames'
            AND state IN ('queued','running')"""
        ).fetchone()[0] == 8
        assert store.telegram.get_setting("frames_backfill_cursor") == cursor_after_first

        # Simulate completion of four jobs, then restart the service.  The
        # scheduler may fill only the four free slots and must continue from
        # the persisted cursor.
        with store.db:
            store.db.execute(
                """UPDATE jobs SET state='done' WHERE id IN (
                SELECT id FROM jobs WHERE kind='telegram.video_frames'
                AND state='queued' ORDER BY id LIMIT 4)"""
            )
            store.db.execute(
                """UPDATE tg_video_frame_sets SET status='saved'
                WHERE status='pending' AND (source_id,message_id) IN (
                SELECT source_id,message_id FROM downloads
                WHERE message_id BETWEEN 3 AND 6)"""
            )
        service2 = make_service(store)
        service2._frame_mode = "bulk"
        store.telegram.start_frame_backfill()
        service2._enqueue_frame_backfill()
        assert store.db.execute(
            """SELECT COUNT(*) FROM jobs WHERE kind='telegram.video_frames'
            AND state IN ('queued','running')"""
        ).fetchone()[0] == 8

        paused = await service2.handle("telegram_frames_backfill_pause", {})
        assert paused["stats"]["backfill_active"] is False
        with store.db:
            store.db.execute(
                """UPDATE jobs SET state='done' WHERE kind='telegram.video_frames'
                AND state='waiting'"""
            )
            store.db.execute(
                "UPDATE tg_video_frame_sets SET status='saved' WHERE status='pending'"
            )
        service2._enqueue_frame_backfill()
        assert store.db.execute(
            """SELECT COUNT(*) FROM jobs WHERE kind='telegram.video_frames'
            AND state IN ('queued','running')"""
        ).fetchone()[0] == 0

        service2._frame_mode = "bulk"
        store.telegram.start_frame_backfill()
        while True:
            service2._enqueue_frame_backfill()
            active = store.db.execute(
                """SELECT COUNT(*) FROM jobs WHERE kind='telegram.video_frames'
                AND state IN ('queued','running')"""
            ).fetchone()[0]
            if active == 0 and store.telegram.frame_stats()["backfill_complete"]:
                break
            with store.db:
                store.db.execute(
                    """UPDATE jobs SET state='done' WHERE kind='telegram.video_frames'
                    AND state='queued'"""
                )
                store.db.execute(
                    "UPDATE tg_video_frame_sets SET status='saved' WHERE status='pending'"
                )
        stats = store.telegram.frame_stats()
        assert stats["missing"] == 0
        assert stats["backfill_complete"] is True
        assert stats["saved"] == 30
        assert store.db.execute(
            "SELECT COUNT(*) FROM tg_video_frame_sets"
        ).fetchone()[0] == 30
        assert store.db.execute(
            """SELECT COUNT(DISTINCT unique_key) FROM jobs
            WHERE kind='telegram.video_frames'"""
        ).fetchone()[0] == store.db.execute(
            "SELECT COUNT(*) FROM jobs WHERE kind='telegram.video_frames'"
        ).fetchone()[0]
    finally:
        await service.stop_background()
        await service2.stop_background()
        store.close()


async def test_backfill_coexists_with_new_download_and_refills_only_free_slots(tmp_path):
    archive = tmp_path / "archive"
    archive.mkdir()
    store = Store(tmp_path / "coexist.sqlite")
    source_id = store.add_source(Post("channelx", 7), str(archive))
    with store.db:
        store.db.execute(
            "INSERT INTO tg_profiles(author_id,display_name) VALUES(101,'Alice')"
        )
        for index in range(1, 21):
            video = archive / f"old-{index}.mp4"
            video.write_bytes(b"x" * 100)
            store.db.execute(
                """INSERT INTO downloads(source_id,message_id,author_id,author_username,
                author_name,date,media_url,file_path,size,state)
                VALUES(?,?,?,?,?,?,?,?,?,'saved')""",
                (
                    source_id,
                    index,
                    101,
                    "alice",
                    "Alice",
                    f"2026-01-{index:02d}",
                    f"https://t.me/channelx/7?comment={index}",
                    str(video),
                    video.stat().st_size,
                ),
            )
    service = make_service(store)
    try:
        service._frame_mode = "bulk"
        store.telegram.start_frame_backfill()
        service._enqueue_frame_backfill()
        assert store.db.execute(
            """SELECT COUNT(*) FROM jobs WHERE kind='telegram.video_frames'
            AND state IN ('queued','running')"""
        ).fetchone()[0] == 8

        new_video = archive / "new.mp4"
        new_video.write_bytes(b"new" * 50)
        create_download(store, source_id, 100, new_video)
        new_job = store.db.execute(
            """SELECT 1 FROM jobs WHERE kind='telegram.video_frames'
            AND unique_key=? AND state='waiting'""",
            (f"video-frames:{source_id}:100",),
        ).fetchone()
        assert new_job is not None
        assert store.db.execute(
            """SELECT COUNT(*) FROM jobs WHERE kind='telegram.video_frames'
            AND state IN ('queued','running')"""
        ).fetchone()[0] == 8
        assert store.db.execute(
            "SELECT COUNT(*) FROM jobs WHERE kind='telegram.video_frames' AND state='waiting'"
        ).fetchone()[0] == 1

        with store.db:
            store.db.execute(
                """UPDATE jobs SET state='done' WHERE id IN (
                SELECT id FROM jobs WHERE kind='telegram.video_frames'
                AND state='queued' ORDER BY id LIMIT 6)"""
            )
            store.db.execute(
                "UPDATE tg_video_frame_sets SET status='saved' WHERE status='pending'"
            )
        service._enqueue_frame_backfill()
        assert store.db.execute(
            """SELECT COUNT(*) FROM jobs WHERE kind='telegram.video_frames'
            AND state IN ('queued','running')"""
        ).fetchone()[0] == 8

        await service.handle("telegram_frames_backfill_pause", {})
        with store.db:
            store.db.execute(
                """UPDATE jobs SET state='done' WHERE kind='telegram.video_frames'
                AND state='queued'"""
            )
            store.db.execute(
                "UPDATE tg_video_frame_sets SET status='saved' WHERE status='pending'"
            )
        service._enqueue_frame_backfill()
        assert store.db.execute(
            """SELECT COUNT(*) FROM jobs WHERE kind='telegram.video_frames'
            AND state IN ('queued','running')"""
        ).fetchone()[0] == 0

        service._frame_mode = "bulk"
        store.telegram.start_frame_backfill()
        while True:
            service._enqueue_frame_backfill()
            active = store.db.execute(
                """SELECT COUNT(*) FROM jobs WHERE kind='telegram.video_frames'
                AND state IN ('queued','running')"""
            ).fetchone()[0]
            if active == 0 and store.telegram.frame_stats()["backfill_complete"]:
                break
            with store.db:
                store.db.execute(
                    """UPDATE jobs SET state='done' WHERE kind='telegram.video_frames'
                    AND state='queued'"""
                )
                store.db.execute(
                    "UPDATE tg_video_frame_sets SET status='saved' WHERE status='pending'"
                )
        assert store.telegram.frame_stats()["missing"] == 0
        assert store.db.execute(
            "SELECT COUNT(*) FROM tg_video_frame_sets"
        ).fetchone()[0] == 21
    finally:
        await service.stop_background()
        store.close()
