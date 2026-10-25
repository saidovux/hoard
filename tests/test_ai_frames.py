"""Video-note frames through the existing Telegram PSL AI contract."""
from __future__ import annotations

import asyncio
import hashlib
import json
import shutil
import subprocess
import time
from pathlib import Path

import pytest

from hoard.ai import AICore, AIResponse, AIUsage
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


class SequenceProvider(AIProvider):
    name = "deepseek"

    def __init__(self):
        self.requests = []

    @property
    def configured(self):
        return True

    async def complete(self, request):
        self.requests.append(request)
        return AIResponse(
            "deepseek",
            "deepseek-flash",
            json.dumps(
                {
                    "status": "completed",
                    "presentation": "female",
                    "estimated_age": 25,
                    "raw_psl_min": 4.0,
                    "raw_psl_max": 5.0,
                    "boosted_psl": 6.0,
                }
            ),
            "stop",
            request_id="frame-request",
            usage=AIUsage(prompt_tokens=100, completion_tokens=10, total_tokens=110),
        )


def jpeg(path: Path, marker: str) -> Path:
    path.write_bytes(b"\xff\xd8\xff" + marker.encode("ascii") * 32)
    return path


def add_frames(store: Store, source_id: int, author_id: int, message_id: int, root: Path):
    video = root / "hoard_objects" / "video_notes" / f"message_{message_id}.mp4"
    video.parent.mkdir(parents=True, exist_ok=True)
    video.write_bytes(b"video-" + bytes([message_id % 255]))
    store.checkpoint(
        source_id,
        message_id,
        {
            "source_id": source_id,
            "message_id": message_id,
            "author_id": author_id,
            "author_username": f"user{author_id}",
            "author_name": f"Person {author_id}",
            "date": "2026-10-25T00:00:00+00:00",
            "media_url": f"https://t.me/channelx/1?comment={message_id}",
            "file_path": str(video),
            "size": video.stat().st_size,
        },
    )
    frames = []
    for position, factor in enumerate((0.2, 0.5, 0.8), start=1):
        path = jpeg(
            root / f"author_{author_id}_message_{message_id}_frame_{position}.jpg",
            f"{author_id}:{message_id}:{position}",
        )
        frames.append(
            {
                "position": position,
                "timestamp_seconds": 10.0 * factor,
                "file_path": str(path),
                "size": path.stat().st_size,
                "sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
            }
        )
    store.telegram.save_frame_set(
        source_id,
        message_id,
        frame_set_id=f"set-{author_id}-{message_id}",
        algorithm_version=1,
        duration=10.0,
        video_size=video.stat().st_size,
        video_mtime_ns=video.stat().st_mtime_ns,
        frames=frames,
    )
    return video


def frame_service(tmp_path, *, authors=(101,), photos=False):
    root = tmp_path / "archive"
    root.mkdir()
    store = Store(tmp_path / "db.sqlite")
    source_id = store.add_source(Post("channelx", 1), str(root))
    for author_id in authors:
        store.telegram.observe_profile(
            author_id=author_id,
            username=f"user{author_id}",
            display_name=f"Person {author_id}",
            source_id=source_id,
        )
        add_frames(store, source_id, author_id, author_id * 10, root)
        if photos:
            photo = jpeg(root / f"avatar-{author_id}.jpg", f"avatar-{author_id}")
            store.telegram.record_profile_photo(
                author_id,
                f"avatar-{author_id}",
                file_path=str(photo),
                size=photo.stat().st_size,
                photo_date="2026-10-25",
            )
    provider = SequenceProvider()
    service = Service(
        store=store,
        engine=FakeEngine(),
        ai_core=AICore({"deepseek": provider}),
    )
    return service, store, provider, source_id


async def test_frames_preview_one_attempt_and_three_images_in_one_message(tmp_path):
    service, store, provider, _source = frame_service(tmp_path)
    try:
        preview = (
            await service.handle(
                "ai_telegram_batch_preview",
                {"author_ids": [101], "image_mode": "frames", "limit": 20, "offset": 0},
            )
        )["preview"]
        assert preview["ready_count"] == 1
        assert preview["selected_image_count"] == 3
        item = preview["items"][0]
        assert item["image_mode"] == "frames"
        assert item["frame_count"] == 3
        assert item["video_id"] == "1:1010"
        assert [image["position"] for image in item["selected_images"]] == [1, 2, 3]
        assert [round(image["timestamp_seconds"], 1) for image in item["selected_images"]] == [
            2.0,
            5.0,
            8.0,
        ]

        started = (
            await service.handle(
                "ai_telegram_batch_start",
                {
                    "author_ids": [101],
                    "image_mode": "frames",
                    "video_ids": {"101": item["video_id"]},
                    "image_snapshots": {"101": item["image_snapshot"]},
                    "operation_id": "frame-operation",
                    "content": "Analyze this single video-note.",
                    "external_confirmed": True,
                },
            )
        )["batch"]
        assert started["queued"] == 1
        analysis_id = started["items"][0]["analysis_id"]
        attempt_id = started["items"][0]["attempt_id"]
        assert analysis_id and attempt_id
        assert (
            store.db.execute(
                "SELECT COUNT(*) FROM jobs WHERE kind='ai.chat' AND payload LIKE ?",
                (f"%{attempt_id}%",),
            ).fetchone()[0]
            == 1
        )
        await service.ai_service.execute_attempt(attempt_id)

        saved = store.ai.telegram_analysis(analysis_id)
        assert saved["image_mode"] == "frames"
        assert saved["video_source_id"] == 1
        assert saved["video_message_id"] == 1010
        assert saved["result_status"] == "completed"
        assert [photo["source_kind"] for photo in saved["photos"]] == [
            "frame",
            "frame",
            "frame",
        ]
        assert len(provider.requests) == 1
        request = provider.requests[0]
        image_messages = [message for message in request.messages if message.images]
        assert len(image_messages) == 1
        assert len(image_messages[0].images) == 3
        assert "видеокружка" in image_messages[0].content
        assert "20%, 50%, 80%" in image_messages[0].content
        expected = [
            Path(frame["file_path"]).read_bytes()
            for frame in store.telegram.frame_rows(1, 1010)
        ]
        assert [image.data for image in image_messages[0].images] == expected
    finally:
        await service.stop_background()
        store.close()


async def test_combined_mode_sends_three_frames_and_one_same_author_photo(tmp_path):
    service, store, provider, _source = frame_service(tmp_path, photos=True)
    try:
        started = (
            await service.handle(
                "ai_telegram_batch_start",
                {
                    "author_ids": [101],
                    "image_mode": "combined",
                    "operation_id": "combined-operation",
                    "content": "Analyze the combined set.",
                    "external_confirmed": True,
                },
            )
        )["batch"]
        attempt_id = started["items"][0]["attempt_id"]
        await service.ai_service.execute_attempt(attempt_id)
        assert len(provider.requests) == 1
        images = [
            image for message in provider.requests[0].messages for image in message.images
        ]
        assert len(images) == 4
        saved = store.ai.telegram_analysis(started["items"][0]["analysis_id"])
        assert [photo["source_kind"] for photo in saved["photos"]] == [
            "frame",
            "frame",
            "frame",
            "profile_photo",
        ]
        assert saved["image_mode"] == "combined"
    finally:
        await service.stop_background()
        store.close()


async def test_repeated_operation_id_does_not_create_a_second_paid_attempt(tmp_path):
    service, store, _provider, _source = frame_service(tmp_path)
    try:
        values = {
            "author_ids": [101],
            "image_mode": "frames",
            "operation_id": "same-frame-operation",
            "content": "Analyze once.",
            "external_confirmed": True,
        }
        first = (await service.handle("ai_telegram_batch_start", values))["batch"]
        second = (await service.handle("ai_telegram_batch_start", values))["batch"]
        assert first["id"] == second["id"]
        assert store.db.execute("SELECT COUNT(*) FROM ai_telegram_batches").fetchone()[0] == 1
        assert (
            store.db.execute(
                "SELECT COUNT(*) FROM jobs WHERE kind='ai.chat'"
            ).fetchone()[0]
            == 1
        )
        assert (
            store.db.execute("SELECT COUNT(*) FROM ai_telegram_analyses").fetchone()[0]
            == 1
        )
    finally:
        await service.stop_background()
        store.close()


async def test_batch_uses_a_separate_video_for_each_author_and_skips_missing_frames(tmp_path):
    service, store, _provider, _source = frame_service(tmp_path, authors=(101, 202))
    try:
        store.telegram.observe_profile(
            author_id=303, username="missing", display_name="No Frames"
        )
        preview = (
            await service.handle(
                "ai_telegram_batch_preview",
                {"author_ids": [101, 202, 303], "image_mode": "frames"},
            )
        )["preview"]
        assert preview["ready_count"] == 2
        assert preview["skipped_count"] == 1
        by_author = {item["author_id"]: item for item in preview["items"]}
        assert by_author[303]["reason"]
        started = (
            await service.handle(
                "ai_telegram_batch_start",
                {
                    "author_ids": [101, 202, 303],
                    "image_mode": "frames",
                    "operation_id": "multi-author-frames",
                    "content": "Analyze each author separately.",
                    "external_confirmed": True,
                },
            )
        )["batch"]
        assert started["selected_count"] == 3
        assert started["skipped"] == 1
        assert started["queued"] == 2
        assert started["items"][0]["video_id"] != started["items"][1]["video_id"]
        assert (
            store.db.execute("SELECT COUNT(*) FROM ai_telegram_batches").fetchone()[0] == 1
        )
    finally:
        await service.stop_background()
        store.close()


async def test_changed_frame_after_preview_is_skipped_without_paid_attempt(tmp_path):
    service, store, _provider, source_id = frame_service(tmp_path)
    try:
        preview = (
            await service.handle(
                "ai_telegram_batch_preview",
                {"author_ids": [101], "image_mode": "frames"},
            )
        )["preview"]
        item = preview["items"][0]
        frame = store.telegram.frame_rows(source_id, 1010)[0]
        Path(frame["file_path"]).write_bytes(b"\xff\xd8\xffchanged-frame-content")
        batch = (
            await service.handle(
                "ai_telegram_batch_start",
                {
                    "author_ids": [101],
                    "image_mode": "frames",
                    "video_ids": {"101": item["video_id"]},
                    "image_snapshots": {"101": item["image_snapshot"]},
                    "operation_id": "changed-frame",
                    "content": "Must not be sent.",
                    "external_confirmed": True,
                },
            )
        )["batch"]
        assert batch["items"][0]["state"] == "skipped"
        assert batch["items"][0]["reason"]
        assert (
            store.db.execute("SELECT COUNT(*) FROM jobs WHERE kind='ai.chat'").fetchone()[0]
            == 0
        )
    finally:
        await service.stop_background()
        store.close()


async def test_frame_mode_rejects_a_text_only_model_before_paid_job(tmp_path):
    service, store, _provider, _source = frame_service(tmp_path)
    try:
        batch = (
            await service.handle(
                "ai_telegram_batch_start",
                {
                    "author_ids": [101],
                    "image_mode": "frames",
                    "model": "deepseek-reasoner",
                    "operation_id": "unsupported-model",
                    "content": "Must not reach the provider.",
                    "external_confirmed": True,
                },
            )
        )["batch"]
        assert batch["errors"] == 1
        assert batch["items"][0]["state"] == "failed"
        assert "модель" in batch["items"][0]["reason"].lower()
        assert (
            store.db.execute("SELECT COUNT(*) FROM jobs WHERE kind='ai.chat'").fetchone()[0]
            == 0
        )
    finally:
        await service.stop_background()
        store.close()


def two_source_service(tmp_path):
    root = tmp_path / "two-source-archive"
    root.mkdir()
    store = Store(tmp_path / "two-source.sqlite")
    source_a = store.add_source(Post("channel_a", 1), str(root))
    source_b = store.add_source(Post("channel_b", 2), str(root))
    store.telegram.observe_profile(
        author_id=101, username="same_person", display_name="Same Person", source_id=source_a
    )
    store.telegram.observe_profile(
        author_id=101, username="same_person", display_name="Same Person", source_id=source_b
    )
    add_frames(store, source_a, 101, 1010, root)
    add_frames(store, source_b, 101, 2020, root)
    avatar = jpeg(root / "same-person-avatar.jpg", "same-avatar")
    store.telegram.record_profile_photo(
        101,
        "same-avatar",
        file_path=str(avatar),
        size=avatar.stat().st_size,
        photo_date="2026-10-25",
    )
    with store.db:
        store.db.execute(
            "UPDATE downloads SET date='2026-01-01T00:00:00+00:00' "
            "WHERE source_id=? AND message_id=?",
            (source_a, 1010),
        )
        store.db.execute(
            "UPDATE downloads SET date='2026-12-01T00:00:00+00:00' "
            "WHERE source_id=? AND message_id=?",
            (source_b, 2020),
        )
    provider = SequenceProvider()
    service = Service(
        store=store,
        engine=FakeEngine(),
        ai_core=AICore({"deepseek": provider}),
    )
    return service, store, provider, source_a, source_b


async def test_publication_scope_limits_frame_selection_and_rejects_other_source(tmp_path):
    service, store, _provider, source_a, source_b = two_source_service(tmp_path)
    try:
        unrestricted = (
            await service.handle(
                "ai_telegram_batch_preview", {"author_ids": [101], "image_mode": "frames"}
            )
        )["preview"]["items"][0]
        assert unrestricted["video_id"] == f"{source_b}:2020"

        scoped = (
            await service.handle(
                "ai_telegram_batch_preview",
                {"source_id": source_a, "image_mode": "frames"},
            )
        )["preview"]
        scoped_item = scoped["items"][0]
        assert scoped_item["ready"] is True
        assert scoped_item["video_id"] == f"{source_a}:1010"

        combined = (
            await service.handle(
                "ai_telegram_batch_preview",
                {"source_id": source_a, "image_mode": "combined"},
            )
        )["preview"]["items"][0]
        assert combined["ready"] is True
        assert combined["video_id"] == f"{source_a}:1010"
        assert combined["image_count"] == 4

        wrong = (
            await service.handle(
                "ai_telegram_batch_preview",
                {
                    "source_id": source_a,
                    "image_mode": "frames",
                    "video_ids": {"101": f"{source_b}:2020"},
                },
            )
        )["preview"]["items"][0]
        assert wrong["ready"] is False
        assert "публикаци" in wrong["reason"].lower()

        wrong_combined = (
            await service.handle(
                "ai_telegram_batch_preview",
                {
                    "source_id": source_a,
                    "image_mode": "combined",
                    "video_ids": {"101": f"{source_b}:2020"},
                },
            )
        )["preview"]["items"][0]
        assert wrong_combined["ready"] is False
        with pytest.raises(ValueError, match="публикаци"):
            service.ai_service.queue_telegram_psl(
                author_id=101,
                content="Must not queue.",
                operation_id="direct-foreign-video",
                adult_confirmed=True,
                image_mode="frames",
                video_id=f"{source_b}:2020",
                frame_source_id=source_a,
            )
        assert (
            store.db.execute("SELECT COUNT(*) FROM jobs WHERE kind='ai.chat'").fetchone()[0]
            == 0
        )

        batch = (
            await service.handle(
                "ai_telegram_batch_start",
                {
                    "source_id": source_a,
                    "image_mode": "frames",
                    "operation_id": "scoped-source-a",
                    "content": "Analyze only source A.",
                    "external_confirmed": True,
                },
            )
        )["batch"]
        assert batch["items"][0]["video_id"] == f"{source_a}:1010"
        assert batch["items"][0]["analysis_id"]
        request_payload = json.loads(
            store.db.execute(
                "SELECT request_payload FROM ai_telegram_batches WHERE id=?",
                (batch["id"],),
            ).fetchone()[0]
        )
        assert request_payload["source_id"] == source_a
        analysis = store.ai.telegram_analysis(batch["items"][0]["analysis_id"])
        assert analysis["video_source_id"] == source_a
        assert analysis["video_message_id"] == 1010

        # Remove A's ready frames: source B must not be used as a fallback.
        with store.db:
            store.db.execute(
                "UPDATE tg_video_frame_sets SET status='failed' "
                "WHERE source_id=? AND message_id=?",
                (source_a, 1010),
            )
        missing = (
            await service.handle(
                "ai_telegram_batch_preview",
                {"source_id": source_a, "image_mode": "frames"},
            )
        )["preview"]
        assert missing["ready_count"] == 0
        assert missing["items"][0]["video_id"] == ""
        assert "публикаци" in missing["items"][0]["reason"].lower()
    finally:
        await service.stop_background()
        store.close()


async def test_pending_batch_with_foreign_video_stays_scoped_after_store_reopen(tmp_path):
    service, store, _provider, source_a, source_b = two_source_service(tmp_path)
    foreign_preview = (
        await service.handle(
            "ai_telegram_batch_preview", {"author_ids": [101], "image_mode": "frames"}
        )
    )["preview"]["items"][0]
    assert foreign_preview["video_id"] == f"{source_b}:2020"
    operation = "persisted-source-scope"
    batch = store.ai.create_telegram_batch(
        operation_key=f"telegram.psl.batch:{operation}",
        source_id=source_a,
        selected_count=1,
        items=[
            {
                "author_id": 101,
                "state": "pending",
                "reason": "",
                "photo_count": 3,
                "photo_ids": foreign_preview["selected_image_ids"],
                "photo_snapshot": foreign_preview["image_snapshot"],
                "image_mode": "frames",
                "video_id": foreign_preview["video_id"],
            }
        ],
        request_payload={
            "operation_id": operation,
            "content": "Must stay in source A.",
            "image_mode": "frames",
            "source_id": source_a,
        },
    )
    await service.stop_background()
    store.close()

    reopened = Store(tmp_path / "two-source.sqlite")
    provider = SequenceProvider()
    restarted = Service(
        store=reopened,
        engine=FakeEngine(),
        ai_core=AICore({"deepseek": provider}),
    )
    try:
        resumed = await restarted.ai_service.resume_telegram_psl_batch(batch["id"])
        assert resumed["items"][0]["state"] == "skipped"
        assert resumed["items"][0]["reason"]
        assert (
            reopened.db.execute(
                "SELECT COUNT(*) FROM jobs WHERE kind='ai.chat'"
            ).fetchone()[0]
            == 0
        )
        assert provider.requests == []
    finally:
        await restarted.stop_background()
        reopened.close()


async def test_pending_publication_batch_resumes_its_own_video_after_restart(tmp_path):
    service, store, _provider, source_a, _source_b = two_source_service(tmp_path)
    scoped = (
        await service.handle(
            "ai_telegram_batch_preview", {"source_id": source_a, "image_mode": "frames"}
        )
    )["preview"]["items"][0]
    assert scoped["video_id"] == f"{source_a}:1010"
    operation = "resume-source-a"
    batch = store.ai.create_telegram_batch(
        operation_key=f"telegram.psl.batch:{operation}",
        source_id=source_a,
        selected_count=1,
        items=[
            {
                "author_id": 101,
                "state": "pending",
                "reason": "",
                "photo_count": 3,
                "photo_ids": scoped["selected_image_ids"],
                "photo_snapshot": scoped["image_snapshot"],
                "image_mode": "frames",
                "video_id": scoped["video_id"],
            }
        ],
        request_payload={
            "operation_id": operation,
            "content": "Resume source A.",
            "image_mode": "frames",
            "source_id": source_a,
        },
    )
    await service.stop_background()
    store.close()

    reopened = Store(tmp_path / "two-source.sqlite")
    restarted = Service(
        store=reopened,
        engine=FakeEngine(),
        ai_core=AICore({"deepseek": SequenceProvider()}),
    )
    try:
        resumed = await restarted.ai_service.resume_telegram_psl_batch(batch["id"])
        analysis_id = resumed["items"][0]["analysis_id"]
        assert analysis_id
        analysis = reopened.ai.telegram_analysis(analysis_id)
        assert analysis["video_source_id"] == source_a
        assert analysis["video_message_id"] == 1010
        assert (
            reopened.db.execute(
                "SELECT COUNT(*) FROM jobs WHERE kind='ai.chat'"
            ).fetchone()[0]
            == 1
        )
    finally:
        await restarted.stop_background()
        reopened.close()


@pytest.mark.skipif(
    shutil.which("ffmpeg") is None or shutil.which("ffprobe") is None,
    reason="FFmpeg is not installed",
)
async def test_full_synthetic_mp4_to_saved_ai_result_acceptance(tmp_path):
    root = tmp_path / "acceptance-archive"
    root.mkdir()
    store = Store(tmp_path / "acceptance.sqlite")
    source_id = store.add_source(Post("acceptance_channel", 7), str(root))
    store.telegram.observe_profile(
        author_id=101,
        username="acceptance_user",
        display_name="Acceptance User",
        source_id=source_id,
    )
    video = video_note_path(root, source_id, 101, 11)
    video.parent.mkdir(parents=True, exist_ok=True)
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
            "3",
            "-pix_fmt",
            "yuv420p",
            "-c:v",
            "libx264",
            "-y",
            str(video),
        ],
        check=True,
        capture_output=True,
    )
    original = video.read_bytes()
    store.checkpoint(
        source_id,
        11,
        {
            "source_id": source_id,
            "message_id": 11,
            "author_id": 101,
            "author_username": "acceptance_user",
            "author_name": "Acceptance User",
            "date": "2026-10-25T00:00:00+00:00",
            "media_url": "https://t.me/acceptance_channel/7?comment=11",
            "file_path": str(video),
            "size": video.stat().st_size,
        },
    )
    provider = SequenceProvider()
    service = Service(
        store=store,
        engine=FakeEngine(),
        ai_core=AICore({"deepseek": provider}),
    )
    try:
        await service.start_background()
        deadline = time.monotonic() + 10
        while time.monotonic() < deadline:
            details = store.telegram.frame_details(source_id, 11)
            if details is not None and details["status"] == "saved":
                break
            await asyncio.sleep(0.05)
        details = store.telegram.frame_details(source_id, 11)
        assert details is not None and details["valid"] is True
        assert [frame["position"] for frame in details["frames"]] == [1, 2, 3]
        assert video.read_bytes() == original
        assert (
            store.db.execute("SELECT COUNT(*) FROM jobs WHERE kind='ai.chat'").fetchone()[0]
            == 0
        )

        store.ai.create_price(
            {
                "provider": "deepseek",
                "model": "deepseek-flash",
                "currency": "USD",
                "input_cache_hit_per_million": "0.10",
                "input_cache_miss_per_million": "0.20",
                "output_per_million": "0.30",
                "source": "synthetic acceptance",
            }
        )
        preview = (
            await service.handle(
                "ai_telegram_batch_preview",
                {"source_id": source_id, "image_mode": "frames"},
            )
        )["preview"]
        assert preview["ready_count"] == 1
        assert preview["items"][0]["video_id"] == f"{source_id}:11"
        batch = (
            await service.handle(
                "ai_telegram_batch_start",
                {
                    "source_id": source_id,
                    "image_mode": "frames",
                    "operation_id": "acceptance-frame-operation",
                    "content": "Analyze this synthetic video note.",
                    "external_confirmed": True,
                },
            )
        )["batch"]
        attempt_id = batch["items"][0]["attempt_id"]
        await service.ai_service.execute_attempt(attempt_id)
        saved = store.ai.telegram_analysis(batch["items"][0]["analysis_id"])
        assert saved["result_status"] == "completed"
        assert saved["image_mode"] == "frames"
        assert saved["video_source_id"] == source_id
        assert saved["video_message_id"] == 11
        assert saved["frame_set_id"]
        assert saved["attempt"]["total_tokens"] == 110
        assert saved["attempt"]["estimated_cost"] is not None
        assert saved["attempt"]["cost_currency"] == "USD"
        assert len(provider.requests) == 1
        image_messages = [
            message for message in provider.requests[0].messages if message.images
        ]
        assert len(image_messages) == 1 and len(image_messages[0].images) == 3
        report = build_catalog(store)
        document = report.read_text(encoding="utf-8")
        assert "frame_01.jpg" in document
        assert "frame_03.jpg" in document
        assert '"frame_status":"saved"' in document
    finally:
        await service.stop_background()
        store.close()
