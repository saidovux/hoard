"""Round-five source-scoped readers, attribution and local media safety."""

from __future__ import annotations

import hashlib
from pathlib import Path
from types import SimpleNamespace

import pytest

from hoard import media_open
from hoard.ipc import IPC_MAX_BYTES, encode_ipc_message
from hoard.models import Post
from hoard.service import Service
from hoard.storage import Store


def _download(
    store: Store,
    source_id: int,
    message_id: int,
    author_id: int,
    path: Path,
) -> None:
    with store.db:
        store.db.execute(
            """INSERT INTO downloads(
            source_id,message_id,author_id,author_username,author_name,date,
            media_url,file_path,size,state)
            VALUES(?,?,?,?,?,'2026-10-28',?,?,?,'saved')""",
            (
                source_id,
                message_id,
                author_id,
                f"user_{author_id}",
                f"User {author_id}",
                f"https://t.me/source/{source_id}?comment={message_id}",
                str(path),
                path.stat().st_size if path.exists() else 10,
            ),
        )


def _analysis(
    store: Store,
    prompt_id: str,
    name: str,
    author_id: int,
    *,
    video_source_id: int | None = None,
    attempt_state: str = "completed",
    result_status: str = "completed",
) -> str:
    attempt_id = f"attempt-{name}"
    analysis_id = f"analysis-{name}"
    completed = result_status == "completed" and attempt_state == "completed"
    with store.db:
        store.db.execute(
            """INSERT INTO ai_attempts(
            id,operation_key,scope,entity_kind,entity_id,provider,prompt_version_id,
            state,created_at,completed_at)
            VALUES(?,?,'telegram','telegram.psl',?,'deepseek',?,?,'2026-10-28','2026-10-28')""",
            (attempt_id, f"op-{name}", analysis_id, prompt_id, attempt_state),
        )
        store.db.execute(
            """INSERT INTO ai_telegram_analyses(
            id,operation_key,author_id,attempt_id,prompt_version_id,schema_version,
            adult_confirmed,image_mode,video_source_id,result_status,presentation,
            estimated_age,raw_psl_min,raw_psl_max,boosted_psl,created_at,analyzed_at)
            VALUES(?,?,?,?,?,2,1,'photos',?,?,?, ?,?,?,?,'2026-10-28','2026-10-28')""",
            (
                analysis_id,
                f"analysis-op-{name}",
                author_id,
                attempt_id,
                prompt_id,
                video_source_id,
                result_status,
                "female" if completed else "unknown",
                25 if completed else None,
                5.0 if completed else 7.0,
                6.0 if completed else 8.0,
                6.5 if completed else 8.0,
            ),
        )
    return analysis_id


def test_duplicate_source_add_has_no_creation_side_effects(tmp_path):
    store = Store(tmp_path / "db.sqlite")
    original_root = tmp_path / "first"
    source_id, created = store.add_source_result(Post("channel", 10), str(original_root))
    assert created is True
    store.telegram.clear_catalog_dirty(store.telegram.catalog_revision())

    duplicate_id, duplicate_created = store.add_source_result(
        Post("channel", 10),
        str(tmp_path / "different"),
        collection_mode="minimal",
    )

    assert duplicate_id == source_id
    assert duplicate_created is False
    assert store.source(source_id)["output"] == str(original_root.resolve())
    assert store.source(source_id)["collection_mode"] == "full"
    assert store.telegram.catalog_dirty() is False

    store.close()


@pytest.mark.asyncio
async def test_duplicate_source_service_reports_created_without_wakeup(tmp_path):
    store = Store(tmp_path / "db.sqlite")
    store.add_source(Post("channel", 10), str(tmp_path / "archive"))
    service = Service(store=store, engine=SimpleNamespace())
    wakeups = 0

    def wakeup() -> None:
        nonlocal wakeups
        wakeups += 1

    service.runner.wakeup = wakeup
    result = await service.handle(
        "source_add",
        {"url": "https://t.me/channel/10", "output": str(tmp_path / "other")},
    )
    assert result["created"] is False
    assert result["source_id"] == 1
    assert "уже существует" in result["message"]
    assert wakeups == 0
    store.close()


def test_publication_pages_are_scoped_bounded_and_frame_list_is_light(tmp_path, monkeypatch):
    store = Store(tmp_path / "db.sqlite")
    archive = tmp_path / "archive"
    archive.mkdir()
    first = store.add_source(Post("first", 1), str(archive))
    second = store.add_source(Post("second", 2), str(archive))
    with store.db:
        store.db.executemany(
            "INSERT INTO tg_profiles(author_id,current_username,display_name) VALUES(?,?,?)",
            ((101, "shared", "Shared"), (102, "first", "First")),
        )
        store.db.executemany(
            "INSERT INTO tg_profile_sources(author_id,source_id) VALUES(?,?)",
            ((101, first), (101, second), (102, first)),
        )
    for index in range(1, 121):
        path = archive / f"first-{index}.mp4"
        path.write_bytes(b"x")
        _download(store, first, index, 101 if index % 2 else 102, path)
    other = archive / "other.mp4"
    other.write_bytes(b"x")
    _download(store, second, 999, 101, other)
    with store.db:
        store.db.executemany(
            """INSERT INTO tg_video_frame_sets(
            source_id,message_id,video_path,status,frame_count,error)
            VALUES(?,?,?,?,?,?)""",
            (
                (first, 120, str(archive / "first-120.mp4"), "saved", 3, ""),
                (first, 119, str(archive / "first-119.mp4"), "failed", 0, "bad"),
                (first, 118, str(archive / "first-118.mp4"), "stale", 3, "old"),
                (first, 117, str(archive / "first-117.mp4"), "pending", 0, ""),
            ),
        )

    monkeypatch.setattr(
        store.telegram,
        "frame_details",
        lambda *_args, **_kwargs: pytest.fail("list must not deep-check frame files"),
    )
    downloads = store.telegram_publication_downloads(first, limit=50, offset=50)
    frames = store.telegram_publication_frames(first, limit=50)
    profiles = store.telegram.profile_summaries(source_id=first, limit=50)

    assert downloads["total"] == 120
    assert [item["message_id"] for item in downloads["items"]] == list(range(70, 20, -1))
    assert all(item["message_id"] != 999 for item in downloads["items"])
    assert frames["total"] == 120
    assert {_item["frame_status"] for _item in frames["items"]} >= {
        "saved", "failed", "stale", "pending", "missing"
    }
    assert {item["author_id"] for item in profiles["items"]} == {101, 102}
    jobs_before = store.db.execute("SELECT COUNT(*) FROM jobs").fetchone()[0]
    profile = store.telegram_publication_profile(first, 101)
    assert profile["source_video_count"] == 60
    assert store.db.execute("SELECT COUNT(*) FROM jobs").fetchone()[0] == jobs_before
    with pytest.raises(ValueError, match="не относится"):
        store.telegram_publication_profile(second, 102)
    encoded = encode_ipc_message({"ok": True, "data": downloads})
    assert len(encoded) < IPC_MAX_BYTES
    with pytest.raises(ValueError, match="limit"):
        store.telegram_publication_downloads(first, limit=101)
    store.close()


def test_publication_psl_uses_confirmed_union_and_hides_invalid_scores(tmp_path):
    store = Store(tmp_path / "db.sqlite")
    first = store.add_source(Post("first", 1), str(tmp_path / "archive"))
    second = store.add_source(Post("second", 2), str(tmp_path / "archive"))
    with store.db:
        store.db.execute(
            "INSERT INTO tg_profiles(author_id,current_username) VALUES(101,'shared')"
        )
        store.db.executemany(
            "INSERT INTO tg_profile_sources(author_id,source_id) VALUES(101,?)",
            ((first,), (second,)),
        )
    prompt = store.ai.ensure_psl_prompt()["current_version"]["id"]
    video_a = _analysis(store, prompt, "video-a", 101, video_source_id=first)
    video_b = _analysis(store, prompt, "video-b", 101, video_source_id=second)
    invalid = _analysis(
        store,
        prompt,
        "invalid",
        101,
        video_source_id=first,
        result_status="invalid_result",
    )
    batch = _analysis(store, prompt, "batch", 101)
    photo = _analysis(store, prompt, "photo", 101)
    mismatch = _analysis(store, prompt, "mismatch", 101)
    with store.db:
        store.db.execute(
            """INSERT INTO ai_telegram_batches(
            id,operation_key,source_id,selected_count,created_at)
            VALUES('batch-a','batch-op',?,2,'2026')""",
            (first,),
        )
        store.db.execute(
            """INSERT INTO ai_telegram_batch_items(
            batch_id,author_id,analysis_id,state) VALUES('batch-a',101,?,'queued')""",
            (batch,),
        )
        store.db.execute(
            """INSERT INTO ai_telegram_batch_items(
            batch_id,author_id,analysis_id,state) VALUES('batch-a',999,?,'queued')""",
            (mismatch,),
        )
        store.db.executemany(
            """INSERT INTO ai_attachments(
            id,media_type,size,sha256,file_path,created_at)
            VALUES(?,'image/jpeg',10,'digest',?,'2026')""",
            (("attachment-photo", "/private/photo.jpg"),
             ("attachment-video", "/private/video-frame.jpg")),
        )
        store.db.execute(
            """INSERT INTO ai_telegram_analysis_photos(
            analysis_id,photo_id,attachment_id,position,source_path,source_sha256,size,
            source_kind,source_id)
            VALUES(?,'photo','attachment-photo',1,'/private/photo.jpg','digest',10,
            'profile_photo',?)""",
            (photo, first),
        )
        # The same analysis can carry two confirmed source links but must appear once.
        store.db.execute(
            """INSERT INTO ai_telegram_analysis_photos(
            analysis_id,photo_id,attachment_id,position,source_path,source_sha256,size,
            source_kind,source_id)
            VALUES(?,'frame','attachment-video',1,'/private/video-frame.jpg','digest',10,
            'video_frame',?)""",
            (video_a, first),
        )

    first_page = store.telegram_publication_psl(first)
    second_page = store.telegram_publication_psl(second)
    first_ids = {item["analysis_id"] for item in first_page["items"]}
    assert first_ids == {video_a, invalid, batch, photo}
    assert len(first_page["items"]) == 4
    assert {item["analysis_id"] for item in second_page["items"]} == {video_b}
    invalid_item = next(item for item in first_page["items"] if item["analysis_id"] == invalid)
    assert invalid_item["failure_category"] == "invalid_psl_result"
    assert invalid_item["estimated_age"] is None
    assert invalid_item["raw_psl_min"] is None
    assert invalid_item["raw_psl_max"] is None
    assert invalid_item["boosted_psl"] is None
    with pytest.raises(ValueError, match="не относится"):
        store.telegram_publication_psl_details(first, video_b)
    assert store.telegram_publication_psl_details(first, video_a)["boosted_psl"] == 6.5
    store.close()


@pytest.mark.asyncio
async def test_publication_media_resolver_uses_stable_ids_and_fake_opener(tmp_path):
    archive = tmp_path / "archive"
    archive.mkdir()
    store = Store(tmp_path / "db.sqlite")
    source_id = store.add_source(Post("source", 1), str(archive))
    video = archive / "video.mp4"
    video.write_bytes(b"video")
    _download(store, source_id, 10, 101, video)
    photo = archive / "photo.jpg"
    photo.write_bytes(b"photo")
    with store.db:
        store.db.execute(
            "INSERT INTO tg_profiles(author_id,current_username) VALUES(101,'alice')"
        )
        store.db.execute(
            "INSERT INTO tg_profile_sources(author_id,source_id) VALUES(101,?)", (source_id,)
        )
        store.db.execute(
            """INSERT INTO tg_profile_photos(
            author_id,photo_id,file_path,size,status) VALUES(101,'p1',?,?,'saved')""",
            (str(photo), photo.stat().st_size),
        )
        video_stat = video.stat()
        store.db.execute(
            """INSERT INTO tg_video_frame_sets(
            source_id,message_id,author_id,video_path,video_size,video_mtime_ns,
            frame_set_id,status,frame_count)
            VALUES(?,?,?,?,?,?,?,'saved',3)""",
            (
                source_id,
                10,
                101,
                str(video),
                video_stat.st_size,
                video_stat.st_mtime_ns,
                "set-10",
            ),
        )
        for position in (1, 2, 3):
            frame = archive / f"frame-{position}.jpg"
            frame.write_bytes(f"frame-{position}".encode())
            store.db.execute(
                """INSERT INTO tg_video_frames(
                source_id,message_id,position,timestamp_seconds,file_path,size,sha256)
                VALUES(?,?,?,?,?,?,?)""",
                (
                    source_id,
                    10,
                    position,
                    float(position),
                    str(frame),
                    frame.stat().st_size,
                    hashlib.sha256(frame.read_bytes()).hexdigest(),
                ),
            )

    opened: list[Path] = []
    service = Service(
        store=store,
        engine=SimpleNamespace(),
        media_opener=lambda path: opened.append(path) is None,
    )
    result = await service.handle(
        "telegram_publication_media_open",
        {"source_id": source_id, "kind": "video", "message_id": 10},
    )
    assert result["opened"] is True
    assert opened == [video.resolve()]
    await service.handle(
        "telegram_publication_media_open",
        {
            "source_id": source_id,
            "kind": "photo",
            "author_id": 101,
            "photo_id": "p1",
        },
    )
    assert opened[-1] == photo.resolve()
    await service.handle(
        "telegram_publication_media_open",
        {"source_id": source_id, "kind": "frame", "message_id": 10, "position": 2},
    )
    assert opened[-1] == (archive / "frame-2.jpg").resolve()
    refusing = Service(
        store=store, engine=SimpleNamespace(), media_opener=lambda _path: False
    )
    with pytest.raises(RuntimeError, match="не подтвердило"):
        await refusing.handle(
            "telegram_publication_media_open",
            {"source_id": source_id, "kind": "video", "message_id": 10},
        )

    outside = tmp_path / "outside.mp4"
    outside.write_bytes(b"outside")
    link = archive / "escape.mp4"
    link.symlink_to(outside)
    _download(store, source_id, 11, 101, link)
    with pytest.raises(ValueError, match="запрещён"):
        store.resolve_publication_media(source_id, "video", message_id=11)
    with pytest.raises(ValueError, match="не найдено"):
        store.resolve_publication_media(source_id, "video", message_id=999)
    with pytest.raises(ValueError, match="не найдено"):
        store.resolve_publication_media(
            source_id, "photo", author_id=101, photo_id="missing"
        )

    video.write_bytes(b"changed")
    with pytest.raises(ValueError, match="повреждён"):
        store.resolve_publication_media(source_id, "video", message_id=10)
    store.close()


def test_platform_opener_reports_missing_viewer(tmp_path, monkeypatch):
    media = tmp_path / "media.jpg"
    media.write_bytes(b"image")
    monkeypatch.setattr(media_open.sys, "platform", "linux")
    monkeypatch.setattr(media_open.shutil, "which", lambda _name: None)
    with pytest.raises(RuntimeError, match="Не найдено приложение"):
        media_open.open_local_file(media)
