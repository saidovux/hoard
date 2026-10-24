import sqlite3
from pathlib import Path

import pytest

from hoard.models import Post
from hoard.storage import Store


def test_sources_and_checkpoints(tmp_path):
    store = Store(tmp_path / "db" / "items.sqlite")
    post = Post("channelx", 9)
    sid = store.add_source(post, str(tmp_path / "media"), 120)
    assert store.add_source(post, str(tmp_path / "media"), 120) == sid
    assert len(store.sources()) == 1
    store.checkpoint(sid, 10)
    store.checkpoint(sid, 8)  # never go backwards
    assert store.sources()[0]["last_id"] == 10
    video = tmp_path / "x.mp4"
    video.write_bytes(b"x" * 123)
    video.with_suffix(".json").write_text("{}", encoding="utf-8")
    record = dict(source_id=sid, message_id=11, author_id=123, author_username="test",
                  author_name="A", date="now", media_url="https://t.me/channelx/9?comment=11",
                  file_path=str(video), size=123)
    store.checkpoint(sid, 11, record)
    assert store.seen(sid, 11)
    assert store.stats() == dict(sources=1, downloads=1, authors=1, profile_photos=0)
    store.close()
    again = Store(tmp_path / "db" / "items.sqlite")
    assert again.sources()[0]["last_id"] == 11
    assert again.seen(sid, 11)
    again.remove_source(sid)
    assert again.stats()["downloads"] == 1
    again.close()


def test_single_source_lookup_preserves_fields_and_download_count(tmp_path):
    store = Store(tmp_path / "db.sqlite")
    source_id = store.add_source(
        Post("channelx", 9), str(tmp_path / "media"), 240, title="Channel X"
    )
    video = tmp_path / "video.mp4"
    video.write_bytes(b"video")
    store.checkpoint(
        source_id,
        11,
        {
            "source_id": source_id,
            "message_id": 11,
            "author_id": 101,
            "author_username": "alice",
            "author_name": "Alice",
            "date": "2026-01-01",
            "media_url": "https://t.me/channelx/9?comment=11",
            "file_path": str(video),
            "size": 5,
        },
    )

    source = store.source(source_id)
    assert source is not None
    assert source["username"] == "channelx"
    assert source["title"] == "Channel X"
    assert source["interval"] == 240
    assert source["download_count"] == 1
    assert store.source(999) is None
    store.close()


def test_archive_validation_uses_one_stat_and_preserves_file_type_rules(tmp_path, monkeypatch):
    store = Store(tmp_path / "db.sqlite")
    source_id = store.add_source(Post("channelx", 9), str(tmp_path / "archive"))
    video = tmp_path / "video.mp4"
    video.write_bytes(b"x" * 5)
    store.checkpoint(
        source_id,
        11,
        {
            "source_id": source_id,
            "message_id": 11,
            "author_id": None,
            "author_username": "",
            "author_name": "",
            "date": "2026-01-01",
            "media_url": "https://t.me/channelx/9?comment=11",
            "file_path": str(video),
            "size": 5,
        },
    )
    original_stat = Path.stat
    stat_calls = 0

    def tracked_stat(path, *args, **kwargs):
        nonlocal stat_calls
        if path == video:
            stat_calls += 1
        return original_stat(path, *args, **kwargs)

    def unexpected_is_file(_path):
        raise AssertionError("archive validation must not issue a separate is_file check")

    monkeypatch.setattr(Path, "stat", tracked_stat)
    monkeypatch.setattr(Path, "is_file", unexpected_is_file)

    assert store.earliest_missing_download(source_id) is None
    assert store.seen(source_id, 11) is True
    assert stat_calls == 2

    video.unlink()
    assert store.seen(source_id, 11) is False
    video.mkdir()
    assert store.seen(source_id, 11) is False
    video.rmdir()
    video.write_bytes(b"wrong-size")
    assert store.seen(source_id, 11) is False

    video.unlink()
    target = tmp_path / "target.mp4"
    target.write_bytes(b"x" * 5)
    try:
        video.symlink_to(target)
    except OSError:
        pass  # Windows may prohibit symlink creation without developer privileges.
    else:
        assert store.seen(source_id, 11) is True
    store.close()


def test_profile_upsert_preserves_chronology_and_optional_username_semantics(tmp_path):
    store = Store(tmp_path / "db.sqlite")
    source_id = store.add_source(Post("channelx", 9), str(tmp_path / "archive"))
    observe = store.telegram.observe_profile
    observe(
        author_id=101,
        username="current",
        display_name="Current Name",
        observed_at="2026-01-01",
        source_id=source_id,
    )
    observe(
        author_id=101,
        username="historic",
        display_name="Historic Name",
        observed_at="2024-01-01",
        source_id=source_id,
    )
    observe(
        author_id=101,
        username=None,
        display_name="Newest Name",
        observed_at="2027-01-01",
        source_id=source_id,
    )
    observe(
        author_id=101,
        username="",
        display_name="",
        observed_at="2028-01-01",
        source_id=source_id,
    )

    profile = dict(store.db.execute("SELECT * FROM tg_profiles").fetchone())
    assert profile["current_username"] == ""
    assert profile["display_name"] == "Newest Name"
    assert profile["first_seen"] == "2024-01-01"
    assert profile["last_seen"] == "2028-01-01"
    history = {
        row["username"]: (row["first_seen"], row["last_seen"])
        for row in store.db.execute("SELECT * FROM tg_username_history")
    }
    assert history == {
        "current": ("2026-01-01", "2026-01-01"),
        "historic": ("2024-01-01", "2024-01-01"),
    }
    relation = store.db.execute("SELECT * FROM tg_profile_sources").fetchone()
    assert relation["first_seen"] == "2024-01-01"
    assert relation["last_seen"] == "2028-01-01"
    store.close()


def test_checkpoint_registers_media_profile_and_catalog_without_advancing(
    tmp_path, monkeypatch
):
    monkeypatch.setattr("hoard.storage.utc_now", lambda: "2026-10-24T12:00:00+00:00")
    store = Store(tmp_path / "db.sqlite")
    source_id = store.add_source(Post("channelx", 9), str(tmp_path / "archive"))
    video = tmp_path / "video.mp4"
    video.write_bytes(b"video")
    store.telegram.set_catalog_dirty(False)
    revision = store.telegram.catalog_revision()

    store.checkpoint(
        source_id,
        11,
        {
            "source_id": source_id,
            "message_id": 11,
            "author_id": 101,
            "author_username": "alice",
            "author_name": "Alice",
            "date": "2025-01-01",
            "media_url": "https://t.me/channelx/9?comment=11",
            "file_path": str(video),
            "size": 5,
        },
        advance=False,
    )

    assert store.source(source_id)["last_id"] == 0
    assert store.seen(source_id, 11)
    profile = store.db.execute("SELECT * FROM tg_profiles WHERE author_id=101").fetchone()
    assert profile["first_seen"] == "2026-10-24T12:00:00+00:00"
    assert profile["last_seen"] == "2026-10-24T12:00:00+00:00"
    assert store.db.execute("SELECT COUNT(*) FROM tg_username_history").fetchone()[0] == 1
    assert store.db.execute("SELECT COUNT(*) FROM tg_profile_sources").fetchone()[0] == 1
    assert store.telegram.catalog_dirty() is True
    assert store.telegram.catalog_revision() == revision + 1
    store.close()


def test_clearing_absent_media_failure_avoids_write_transaction(tmp_path):
    store = Store(tmp_path / "db.sqlite")
    source_id = store.add_source(Post("channelx", 9), str(tmp_path / "archive"))
    statements = []
    store.db.set_trace_callback(statements.append)

    store.telegram.clear_media_failure(source_id, 11)

    store.db.set_trace_callback(None)
    assert any("SELECT 1 FROM tg_media_failures" in statement for statement in statements)
    assert not any(statement.startswith("BEGIN") for statement in statements)

    store.telegram.record_media_failure(source_id, 11, error="temporary")
    store.telegram.clear_media_failure(source_id, 11)
    assert store.db.execute("SELECT COUNT(*) FROM tg_media_failures").fetchone()[0] == 0
    store.close()


def test_interval_limits(tmp_path):
    store = Store(tmp_path / "test.sqlite")
    with pytest.raises(ValueError):
        store.add_source(Post("foobar", 1), str(tmp_path), 10)
    store.close()


def test_existing_source_schema_is_extended_without_losing_rows(tmp_path):
    filename = tmp_path / "old.sqlite"
    db = sqlite3.connect(filename)
    db.execute(
        """CREATE TABLE sources (
        id INTEGER PRIMARY KEY, username TEXT NOT NULL, post_id INTEGER NOT NULL,
        url TEXT NOT NULL, output TEXT NOT NULL, interval INTEGER NOT NULL DEFAULT 120,
        enabled INTEGER NOT NULL DEFAULT 1, last_id INTEGER NOT NULL DEFAULT 0,
        next_at REAL NOT NULL DEFAULT 0, last_error TEXT NOT NULL DEFAULT '',
        UNIQUE(username,post_id))"""
    )
    db.execute(
        """INSERT INTO sources(username,post_id,url,output)
        VALUES('channelx',7,'https://t.me/channelx/7',?)""",
        (str(tmp_path),),
    )
    db.commit()
    db.close()

    store = Store(filename)
    source = store.sources()[0]
    assert source["username"] == "channelx"
    assert source["title"] == ""
    assert source["last_result"] == ""
    assert source["download_count"] == 0
    store.close()


def test_source_status_records_last_result_and_clears_error(tmp_path):
    store = Store(tmp_path / "test.sqlite")
    sid = store.add_source(Post("channelx", 7), str(tmp_path), title="Channel X")
    store.schedule(sid, 120, error="temporary", result="Ошибка проверки")
    assert store.sources()[0]["last_error"] == "temporary"

    store.schedule(sid, 120, result="Проверено: 4; новых кружков: 1")
    source = store.sources()[0]
    assert source["title"] == "Channel X"
    assert source["last_checked_at"]
    assert source["last_error"] == ""
    assert source["last_result"].startswith("Проверено")
    store.close()
