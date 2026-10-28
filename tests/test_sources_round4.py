"""Round-four source settings, overview attribution and collection policy."""

from __future__ import annotations

import asyncio
import json
import sqlite3
from datetime import UTC, datetime
from types import SimpleNamespace

import pytest

from hoard.ipc import IPC_MAX_BYTES, encode_ipc_message
from hoard.models import Post
from hoard.service import Service
from hoard.storage import Store
from hoard.telegram import AuthState, TelegramEngine


class ServiceEngine:
    authorization_step = AuthState.DISCONNECTED

    async def disconnect(self):
        return None


class ScanClient:
    def __init__(self, messages):
        self.messages = messages

    async def is_user_authorized(self):
        return True

    async def get_entity(self, username):
        return SimpleNamespace(broadcast=True, title=str(username))

    async def get_messages(self, entity, ids):
        return SimpleNamespace(id=ids)

    async def iter_messages(self, entity, reply_to, min_id, reverse):
        del entity, reply_to, reverse
        for message in self.messages:
            if message.id > min_id:
                yield message

    async def download_media(self, message, file, progress_callback=None):
        del message
        file.write(b"video")
        if progress_callback:
            progress_callback(5, 5)
        return file


class DeferredSenderMessage:
    def __init__(self, message_id: int, author_id: int = 101):
        self.id = message_id
        self.video_note = object()
        self.sender_id = author_id
        self.sender = None
        self.date = datetime(2026, 1, 1, tzinfo=UTC)
        self.sender_requests = 0

    async def get_sender(self):
        self.sender_requests += 1
        return SimpleNamespace(username="alice", first_name="Alice", last_name="Author")


def _old_sources_database(filename, archive: str) -> None:
    database = sqlite3.connect(filename)
    database.execute(
        """CREATE TABLE sources (
        id INTEGER PRIMARY KEY, username TEXT NOT NULL, post_id INTEGER NOT NULL,
        url TEXT NOT NULL, output TEXT NOT NULL, interval INTEGER NOT NULL DEFAULT 120,
        enabled INTEGER NOT NULL DEFAULT 1, last_id INTEGER NOT NULL DEFAULT 0,
        next_at REAL NOT NULL DEFAULT 0, last_error TEXT NOT NULL DEFAULT '',
        title TEXT NOT NULL DEFAULT '', last_checked_at TEXT NOT NULL DEFAULT '',
        last_result TEXT NOT NULL DEFAULT '', UNIQUE(username,post_id))"""
    )
    database.execute(
        """INSERT INTO sources(
        id,username,post_id,url,output,enabled,last_id,next_at,title)
        VALUES(7,'legacy',42,'https://t.me/legacy/42',?,0,99,1234.5,'Legacy')""",
        (archive,),
    )
    database.commit()
    database.close()


def test_collection_mode_migration_and_update_preserve_archive_state(tmp_path):
    filename = tmp_path / "legacy.sqlite"
    archive = tmp_path / "archive"
    _old_sources_database(filename, str(archive))

    store = Store(filename)
    migrated = store.source(7)
    assert migrated is not None
    assert migrated["collection_mode"] == "full"
    with store.db:
        store.db.execute(
            """INSERT INTO tg_profiles(author_id,display_name) VALUES(101,'Alice')"""
        )
        store.db.execute(
            """INSERT INTO tg_profile_sources(author_id,source_id) VALUES(101,7)"""
        )
        store.db.execute(
            """INSERT INTO tg_profile_photos(author_id,photo_id,file_path,status)
            VALUES(101,'photo','/archive/photo.jpg','saved')"""
        )
        store.db.execute(
            """INSERT INTO downloads(
            source_id,message_id,author_id,date,media_url,file_path,size)
            VALUES(7,100,101,'2026-01-01','https://t.me/legacy/42?comment=100',
            '/archive/100.mp4',5)"""
        )

    updated = store.update_source(
        7,
        title="Renamed",
        output=str(tmp_path / "new-archive"),
        interval=600,
        collection_mode="minimal",
    )
    assert updated["id"] == 7
    assert updated["enabled"] == 0
    assert updated["last_id"] == 99
    assert updated["next_at"] == 1234.5
    assert updated["collection_mode"] == "minimal"
    assert store.db.execute("SELECT COUNT(*) FROM downloads").fetchone()[0] == 1
    assert store.db.execute("SELECT COUNT(*) FROM tg_profile_sources").fetchone()[0] == 1
    assert store.db.execute("SELECT COUNT(*) FROM tg_profile_photos").fetchone()[0] == 1
    duplicate_id = store.add_source(
        Post("legacy", 42),
        str(tmp_path / "duplicate-output"),
        interval=120,
        collection_mode="full",
    )
    assert duplicate_id == 7
    duplicate = store.source(7)
    assert duplicate["enabled"] == 0
    assert duplicate["last_id"] == 99
    assert duplicate["next_at"] == 1234.5
    assert duplicate["collection_mode"] == "minimal"
    with pytest.raises(ValueError, match="full или minimal"):
        store.update_source(
            7,
            title="Bad",
            output=str(archive),
            interval=120,
            collection_mode="everything",
        )
    store.close()

    reopened = Store(filename)
    assert reopened.source(7)["collection_mode"] == "minimal"
    assert reopened.source(7)["last_id"] == 99
    reopened.close()


def _insert_analysis(
    store: Store,
    prompt_version_id: str,
    name: str,
    author_id: int,
    *,
    attempt_state: str = "completed",
    result_status: str | None = "completed",
    video_source_id: int | None = None,
) -> str:
    attempt_id = f"attempt-{name}"
    analysis_id = f"analysis-{name}"
    with store.db:
        store.db.execute(
            """INSERT INTO ai_attempts(
            id,operation_key,scope,entity_kind,entity_id,provider,prompt_version_id,state,created_at)
            VALUES(?,?,'telegram','telegram.psl',?,'deepseek',?,?,?)""",
            (
                attempt_id,
                f"attempt-operation-{name}",
                analysis_id,
                prompt_version_id,
                attempt_state,
                "2026-01-01",
            ),
        )
        store.db.execute(
            """INSERT INTO ai_telegram_analyses(
            id,operation_key,author_id,attempt_id,prompt_version_id,schema_version,
            adult_confirmed,result_status,video_source_id,created_at)
            VALUES(?,?,?,?,?,2,1,?,?,?)""",
            (
                analysis_id,
                f"analysis-operation-{name}",
                author_id,
                attempt_id,
                prompt_version_id,
                result_status,
                video_source_id,
                "2026-01-01",
            ),
        )
    return analysis_id


def test_sources_overview_has_source_scoped_counts_and_confirmed_psl(tmp_path):
    store = Store(tmp_path / "db.sqlite")
    first = store.add_source(
        Post("alpha", 10), str(tmp_path / "archive"), title="Alpha publication"
    )
    second = store.add_source(Post("beta", 20), str(tmp_path / "archive"))
    store.set_enabled(second, False)
    with store.db:
        store.db.executemany(
            "INSERT INTO tg_profiles(author_id,display_name) VALUES(?,?)",
            ((101, "Shared"), (102, "Alpha only")),
        )
        store.db.executemany(
            "INSERT INTO tg_profile_sources(author_id,source_id) VALUES(?,?)",
            ((101, first), (101, second), (102, first)),
        )
        store.db.execute(
            """INSERT INTO tg_profile_photos(author_id,photo_id,file_path,status)
            VALUES(101,'shared','/private/shared.jpg','saved')"""
        )
        store.db.executemany(
            """INSERT INTO downloads(
            source_id,message_id,author_id,date,media_url,file_path,size)
            VALUES(?,?,?,?,?,?,?)""",
            (
                (first, 1, 101, "2026", "https://t.me/alpha/10?comment=1", "/a/1", 1),
                (first, 2, 101, "2026", "https://t.me/alpha/10?comment=2", "/a/2", 1),
                (second, 3, 101, "2026", "https://t.me/beta/20?comment=3", "/b/3", 1),
            ),
        )
        store.db.executemany(
            """INSERT INTO tg_video_frame_sets(
            source_id,message_id,video_path,status,frame_count)
            VALUES(?,?,?,?,?)""",
            (
                (first, 1, "/a/1", "saved", 2),
                (first, 2, "/a/2", "failed", 0),
                (first, 4, "/a/4", "stale", 1),
            ),
        )
        store.db.executemany(
            """INSERT INTO tg_video_frames(source_id,message_id,position,file_path)
            VALUES(?,?,?,?)""",
            ((first, 1, 1, "/f/1"), (first, 1, 2, "/f/2"), (first, 4, 1, "/f/3")),
        )

    prompt = store.ai.ensure_psl_prompt()["current_version"]["id"]
    linked = _insert_analysis(
        store, prompt, "linked", 101, video_source_id=first
    )
    other = _insert_analysis(store, prompt, "other", 101, video_source_id=second)
    unscoped = _insert_analysis(store, prompt, "unscoped", 101)
    incomplete = _insert_analysis(
        store,
        prompt,
        "incomplete",
        102,
        result_status="insufficient_data",
        video_source_id=first,
    )
    failed = _insert_analysis(
        store,
        prompt,
        "failed-attempt",
        102,
        attempt_state="failed",
        video_source_id=first,
    )
    with store.db:
        store.db.execute(
            """INSERT INTO ai_telegram_batches(id,operation_key,source_id,selected_count,created_at)
            VALUES('batch-a','batch-a-operation',?,1,'2026')""",
            (first,),
        )
        for analysis_id, author_id in (
            (linked, 101),
            (incomplete, 102),
            (failed, 103),
        ):
            store.db.execute(
                """INSERT INTO ai_telegram_batch_items(
                batch_id,author_id,analysis_id,state) VALUES('batch-a',?,?,'queued')""",
                (author_id, analysis_id),
            )
        store.db.execute(
            """INSERT INTO ai_telegram_analysis_photos(
            analysis_id,photo_id,attachment_id,position,source_path,source_sha256,size,
            source_kind,source_id)
            SELECT ?, 'frame-linked', id, 99, file_path, sha256, size, 'video_frame', ?
            FROM ai_attachments LIMIT 0""",
            (linked, first),
        )

    overview = store.telegram_sources_overview(limit=50, sort="id_asc")
    assert overview["total"] == 2
    alpha, beta = overview["items"]
    assert alpha["id"] == first
    assert alpha["download_count"] == 2
    assert alpha["profile_count"] == 2
    assert alpha["profile_photo_count"] == 1
    assert alpha["frame_set_count"] == 1
    assert alpha["frame_count"] == 3
    assert alpha["frame_failed_count"] == 1
    assert alpha["frame_stale_count"] == 1
    assert alpha["psl_count"] == 1
    assert beta["psl_count"] == 1
    assert unscoped not in json.dumps(overview)
    assert other not in json.dumps(alpha)
    assert "output" not in alpha and "/private/" not in repr(overview)

    searched = store.telegram_sources_overview(query="alpha/10", limit=50)
    assert [item["id"] for item in searched["items"]] == [first]
    by_username = store.telegram_sources_overview(query="@alpha", limit=50)
    assert [item["id"] for item in by_username["items"]] == [first]
    disabled = store.telegram_sources_overview(enabled=False, limit=50)
    assert [item["id"] for item in disabled["items"]] == [second]
    with pytest.raises(ValueError, match="limit"):
        store.telegram_sources_overview(limit=101)
    with pytest.raises(ValueError, match="порядок"):
        store.telegram_sources_overview(sort="DROP TABLE sources")
    store.close()


def test_sources_overview_stays_bounded_for_one_thousand_sources(tmp_path):
    store = Store(tmp_path / "scale.sqlite")
    archive = str(tmp_path / "archive")
    with store.db:
        store.db.executemany(
            """INSERT INTO sources(
            username,post_id,url,output,title,last_error,last_result,collection_mode)
            VALUES(?,?,?,?,?,?,?,?)""",
            (
                (
                    f"channel_{source_id}",
                    source_id,
                    f"https://t.me/channel_{source_id}/{source_id}",
                    archive,
                    "Я" * 200,
                    "О" * 500,
                    "Р" * 500,
                    "full",
                )
                for source_id in range(1, 1001)
            ),
        )

    result = store.telegram_sources_overview(limit=100)
    encoded = encode_ipc_message({"ok": True, "data": result})

    assert result["total"] == 1000
    assert len(result["items"]) == 100
    assert len(encoded) < IPC_MAX_BYTES
    assert all(len(item["last_error"].encode()) <= 56 for item in result["items"])
    store.close()


async def test_minimal_scan_preserves_identity_relation_without_profile_lookup_or_job(tmp_path):
    store = Store(tmp_path / "db.sqlite")
    minimal = store.add_source(
        Post("minimal", 1),
        str(tmp_path / "archive"),
        collection_mode="minimal",
    )
    full = store.add_source(
        Post("full", 2), str(tmp_path / "archive"), collection_mode="full"
    )
    minimal_message = DeferredSenderMessage(11)
    full_message = DeferredSenderMessage(12)
    engine = TelegramEngine(store)

    engine.connect = lambda: asyncio.sleep(0, result=ScanClient([minimal_message]))
    assert await engine.scan(store.source(minimal)) == (1, 1)
    assert minimal_message.sender_requests == 0
    profile = store.db.execute(
        "SELECT * FROM tg_profiles WHERE author_id=101"
    ).fetchone()
    assert profile["current_username"] == ""
    assert store.db.execute(
        "SELECT COUNT(*) FROM tg_profile_sources WHERE source_id=?", (minimal,)
    ).fetchone()[0] == 1
    assert store.db.execute(
        "SELECT COUNT(*) FROM jobs WHERE kind='telegram.profile_photos'"
    ).fetchone()[0] == 0

    engine.connect = lambda: asyncio.sleep(0, result=ScanClient([full_message]))
    assert await engine.scan(store.source(full)) == (1, 1)
    assert full_message.sender_requests == 1
    assert store.db.execute(
        "SELECT COUNT(*) FROM tg_profile_sources WHERE author_id=101"
    ).fetchone()[0] == 2
    assert store.db.execute(
        "SELECT COUNT(*) FROM jobs WHERE kind='telegram.profile_photos'"
    ).fetchone()[0] == 1
    store.close()


async def test_enrichment_respects_minimal_sources_and_keeps_legacy_profiles(tmp_path):
    store = Store(tmp_path / "db.sqlite")
    minimal = store.add_source(
        Post("minimal", 1), str(tmp_path / "archive"), collection_mode="minimal"
    )
    full = store.add_source(
        Post("full", 2), str(tmp_path / "archive"), collection_mode="full"
    )
    with store.db:
        store.db.executemany(
            "INSERT INTO tg_profiles(author_id,display_name,photos_retry_at) VALUES(?,?,0)",
            ((101, "Minimal"), (102, "Full"), (103, "Shared"), (104, "Legacy")),
        )
        store.db.executemany(
            "INSERT INTO tg_profile_sources(author_id,source_id) VALUES(?,?)",
            ((101, minimal), (102, full), (103, minimal), (103, full)),
        )
    service = Service(store=store, engine=ServiceEngine())

    await service.handle("telegram_enrich", {})

    photo_authors = {
        json.loads(row["payload"])["author_id"]
        for row in store.db.execute(
            "SELECT payload FROM jobs WHERE kind='telegram.profile_photos'"
        )
    }
    assert photo_authors == {102, 103, 104}
    assert store.db.execute(
        "SELECT COUNT(*) FROM jobs WHERE kind='telegram.enrich_source'"
    ).fetchone()[0] == 2
    store.close()


async def test_source_update_ipc_is_local_and_preserves_disabled_checkpoint(tmp_path):
    store = Store(tmp_path / "db.sqlite")
    source_id = store.add_source(Post("channel", 9), str(tmp_path / "archive"))
    store.set_enabled(source_id, False)
    with store.db:
        store.db.execute(
            "UPDATE sources SET last_id=88,next_at=9876.5 WHERE id=?", (source_id,)
        )
    service = Service(store=store, engine=ServiceEngine())

    result = await service.handle(
        "source_update",
        {
            "id": source_id,
            "title": "Changed",
            "output": str(tmp_path / "archive-2"),
            "interval": 300,
            "collection_mode": "minimal",
        },
    )

    assert result["source"]["enabled"] == 0
    assert result["source"]["last_id"] == 88
    assert result["source"]["next_at"] == 9876.5
    assert store.db.execute("SELECT COUNT(*) FROM jobs").fetchone()[0] == 0
    with pytest.raises(ValueError, match="full или minimal"):
        await service.handle(
            "source_update",
            {
                "id": source_id,
                "title": "Bad",
                "output": str(tmp_path),
                "interval": 120,
                "collection_mode": "bad",
            },
        )
    store.close()
