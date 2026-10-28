"""Deterministic read-only Telegram archive statistics."""

from __future__ import annotations

from hoard.models import Post
from hoard.service import Service
from hoard.storage import Store
from hoard.telegram import AuthState


class FakeEngine:
    authorization_step = AuthState.DISCONNECTED

    async def disconnect(self):
        return None


def _insert_downloads(store: Store, source_id: int, count: int) -> None:
    with store.db:
        for message_id in range(1, count + 1):
            store.db.execute(
                """INSERT INTO downloads(
                source_id,message_id,author_id,date,media_url,file_path,size,state
                ) VALUES(?,?,?,?,?,?,?,'saved')""",
                (
                    source_id,
                    message_id,
                    100 + message_id,
                    "2026-01-01",
                    f"https://t.me/test/1?comment={message_id}",
                    f"/archive/{message_id}.mp4",
                    100,
                ),
            )


def _insert_psl_analysis(
    store: Store,
    prompt_version_id: str,
    name: str,
    author_id: int,
    attempt_state: str,
    result_status: str | None,
) -> None:
    with store.db:
        store.db.execute(
            """INSERT INTO ai_attempts(
            id,operation_key,scope,entity_kind,entity_id,provider,prompt_version_id,state,created_at
            ) VALUES(?,?,'telegram','telegram.psl',?,'deepseek',?,?,?)""",
            (
                f"attempt-{name}",
                f"attempt-operation-{name}",
                f"analysis-{name}",
                prompt_version_id,
                attempt_state,
                "2026-01-01T00:00:00+00:00",
            ),
        )
        store.db.execute(
            """INSERT INTO ai_telegram_analyses(
            id,operation_key,author_id,attempt_id,prompt_version_id,schema_version,
            adult_confirmed,result_status,created_at
            ) VALUES(?,?,?,?,?,1,1,?,?)""",
            (
                f"analysis-{name}",
                f"analysis-operation-{name}",
                author_id,
                f"attempt-{name}",
                prompt_version_id,
                result_status,
                "2026-01-01T00:00:00+00:00",
            ),
        )


def populated_store(tmp_path) -> Store:
    store = Store(tmp_path / "hoard.sqlite3")
    first = store.add_source(Post("first", 1), str(tmp_path / "archive"))
    second = store.add_source(Post("second", 2), str(tmp_path / "archive"))
    store.add_source(Post("third", 3), str(tmp_path / "archive"))
    store.set_enabled(second, False)
    _insert_downloads(store, first, 8)

    with store.db:
        store.db.executemany(
            "INSERT INTO tg_profiles(author_id,display_name) VALUES(?,?)",
            ((101, "One"), (102, "Two"), (103, "Three")),
        )
        store.db.executemany(
            """INSERT INTO tg_profile_photos(author_id,photo_id,file_path,status)
            VALUES(?,?,?,?)""",
            (
                (101, "one", "/archive/one.jpg", "saved"),
                (102, "two", "/archive/two.jpg", "saved"),
                (103, "bad", "/archive/bad.jpg", "failed"),
            ),
        )
        store.db.executemany(
            """INSERT INTO tg_post_media(source_id,media_id,kind,file_path,status)
            VALUES(?,?,?,?,?)""",
            (
                (first, "image-1", "image", "/archive/image-1.jpg", "saved"),
                (first, "image-2", "image", "/archive/image-2.jpg", "saved"),
                (first, "video-1", "video", "/archive/video-1.mp4", "saved"),
                (first, "file-1", "file", "/archive/file-1.bin", "saved"),
                (first, "failed", "image", "/archive/failed.jpg", "failed"),
            ),
        )
        statuses = ("saved", "saved", "saved", "failed", "stale", "pending", "running")
        for message_id, status in enumerate(statuses, start=1):
            store.db.execute(
                """INSERT INTO tg_video_frame_sets(
                source_id,message_id,video_path,frame_set_id,status,frame_count
                ) VALUES(?,?,?,?,?,?)""",
                (
                    first,
                    message_id,
                    f"/archive/{message_id}.mp4",
                    f"set-{message_id}",
                    status,
                    message_id if status == "saved" else 0,
                ),
            )
        for message_id, count in ((1, 1), (2, 2), (3, 3), (5, 2)):
            for position in range(1, count + 1):
                store.db.execute(
                    """INSERT INTO tg_video_frames(
                    source_id,message_id,position,file_path,size,sha256
                    ) VALUES(?,?,?,?,?,?)""",
                    (
                        first,
                        message_id,
                        position,
                        f"/archive/frame-{message_id}-{position}.jpg",
                        10,
                        f"hash-{message_id}-{position}",
                    ),
                )
        for state in ("waiting", "queued", "running"):
            store.db.execute(
                "INSERT INTO jobs(kind,payload,state) VALUES('telegram.video_frames','{}',?)",
                (state,),
            )

    prompt_version_id = store.ai.ensure_psl_prompt()["current_version"]["id"]
    for values in (
        ("success-1", 101, "completed", "completed"),
        ("success-2", 101, "completed", "completed"),
        ("success-3", 102, "completed", "completed"),
        ("failed", 103, "failed", None),
        ("invalid", 104, "failed", "invalid_result"),
        ("unknown", 105, "outcome_unknown", None),
        ("insufficient", 106, "completed", "insufficient_data"),
        ("ineligible", 107, "completed", "ineligible"),
        ("queued", 108, "queued", None),
        ("running", 109, "running", None),
    ):
        _insert_psl_analysis(store, prompt_version_id, *values)

    with store.db:
        store.db.execute(
            """INSERT INTO ai_attempts(
            id,scope,provider,state,created_at
            ) VALUES('ordinary-chat','global','deepseek','completed','2026-01-01')"""
        )
        store.db.execute(
            """INSERT INTO ai_telegram_batches(
            id,operation_key,selected_count,created_at
            ) VALUES('batch','batch-operation',1,'2026-01-01')"""
        )
        store.db.execute(
            """INSERT INTO ai_telegram_batch_items(batch_id,author_id,state,reason)
            VALUES('batch',999,'skipped','no photos')"""
        )
    return store


def test_telegram_statistics_have_exact_non_overlapping_semantics(tmp_path):
    store = populated_store(tmp_path)
    result = store.telegram_statistics()

    assert result["sources"] == {"total": 3, "enabled": 2, "disabled": 1}
    assert result["media"] == {
        "video_notes": 8,
        "post_images": 2,
        "post_videos": 1,
        "post_files": 1,
        "profile_photos": 2,
    }
    assert result["profiles"] == {"unique": 3}
    assert result["frames"] == {
        "processed_videos": 3,
        "created_frames": 6,
        "waiting": 1,
        "queued": 1,
        "processing": 1,
        "missing": 1,
        "failed": 1,
        "stale": 1,
    }
    assert result["psl"] == {
        "completed_analyses": 3,
        "unique_profiles": 2,
        "failed_analyses": 2,
        "unknown_outcome": 1,
        "without_result": 2,
        "queued": 1,
        "running": 1,
    }
    store.close()


async def test_telegram_statistics_ipc_is_read_only_and_contains_only_aggregates(tmp_path):
    store = populated_store(tmp_path)
    service = Service(store=store, engine=FakeEngine())
    changes_before = store.db.total_changes

    result = await service.handle("telegram_statistics_get", {})

    assert store.db.total_changes == changes_before
    assert result["frames"]["active"] is False
    assert result["frames"]["processing_mode"] == "off"
    assert not any(key in repr(result) for key in ("file_path", "/archive/", "session"))
    store.close()
