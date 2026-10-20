import sqlite3

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
    assert store.stats() == dict(sources=1, downloads=1, authors=1)
    store.close()
    again = Store(tmp_path / "db" / "items.sqlite")
    assert again.sources()[0]["last_id"] == 11
    assert again.seen(sid, 11)
    again.remove_source(sid)
    assert again.stats()["downloads"] == 1
    again.close()


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
