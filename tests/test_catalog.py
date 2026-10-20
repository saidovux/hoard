from __future__ import annotations

import json
import sqlite3
import sys
from pathlib import Path

import pytest

from hoard.archive_paths import media_reference, object_path, video_note_path
from hoard.catalog import (
    build_catalog,
    build_payload,
    search_posts,
    search_profiles,
    sort_posts,
    sort_profiles,
)
from hoard.cli import main
from hoard.models import Post
from hoard.storage import Store


def payload_from(file: Path) -> dict:
    text = file.read_text(encoding="utf-8")
    prefix = "window.HOARD_CATALOG="
    assert text.startswith(prefix) and text.endswith(";\n")
    return json.loads(text[len(prefix):-2])


def video_record(source_id: int, message_id: int, path: Path, author_id: int = 101) -> dict:
    return {
        "source_id": source_id,
        "message_id": message_id,
        "author_id": author_id,
        "author_username": "first_name",
        "author_name": "First User",
        "date": "2026-01-01T12:00:00+00:00",
        "media_url": f"https://t.me/channelx/7?comment={message_id}",
        "file_path": str(path),
        "size": path.stat().st_size,
    }


def test_new_archive_builds_autonomous_catalog_without_network(tmp_path):
    store = Store(tmp_path / "db.sqlite")
    root = tmp_path / "archive"
    store.add_source(Post("channelx", 7), str(root), title="Channel X")

    report = build_catalog(store)

    assert report == root / "telegram" / "index.html"
    assert {item.name for item in report.parent.iterdir()} >= {
        "index.html", "style.css", "app.js", "data.js", "objects"
    }
    data = payload_from(report.parent / "data.js")
    assert data["posts"][0]["channel_title"] == "Channel X"
    assert data["profiles"] == []
    html = report.read_text(encoding="utf-8")
    assert '<script src="data.js"></script>' in html
    assert "https://" not in html
    store.close()


def create_legacy_database(filename: Path, root: Path) -> tuple[Path, Path]:
    media = root / "channelx" / "7" / "101" / "msg_22.mp4"
    media.parent.mkdir(parents=True)
    media.write_bytes(b"old-video")
    sidecar = media.with_suffix(".json")
    sidecar.write_text(
        json.dumps(
            {
                "author": {"id": 101, "username": "legacy_user", "name": "Legacy User"},
                "comment_url": "https://t.me/channelx/7?comment=22",
            }
        ),
        encoding="utf-8",
    )
    db = sqlite3.connect(filename)
    db.executescript(
        """
        CREATE TABLE sources (
          id INTEGER PRIMARY KEY, username TEXT NOT NULL, post_id INTEGER NOT NULL,
          url TEXT NOT NULL, output TEXT NOT NULL, interval INTEGER NOT NULL DEFAULT 120,
          enabled INTEGER NOT NULL DEFAULT 1, last_id INTEGER NOT NULL DEFAULT 0,
          next_at REAL NOT NULL DEFAULT 0, last_error TEXT NOT NULL DEFAULT '',
          UNIQUE(username,post_id)
        );
        CREATE TABLE downloads (
          source_id INTEGER NOT NULL, message_id INTEGER NOT NULL,
          author_id INTEGER, author_username TEXT NOT NULL DEFAULT '',
          author_name TEXT NOT NULL DEFAULT '', date TEXT NOT NULL,
          media_url TEXT NOT NULL, file_path TEXT NOT NULL, size INTEGER NOT NULL,
          PRIMARY KEY(source_id,message_id)
        );
        """
    )
    db.execute(
        "INSERT INTO sources(id,username,post_id,url,output,last_id) VALUES(1,?,?,?,?,22)",
        ("channelx", 7, "https://t.me/channelx/7", str(root)),
    )
    db.execute(
        """INSERT INTO downloads(source_id,message_id,author_id,author_username,
        author_name,date,media_url,file_path,size) VALUES(1,22,NULL,'','','2025-01-01',?,?,?)""",
        ("https://t.me/channelx/7?comment=22", str(media), media.stat().st_size),
    )
    db.commit()
    db.close()
    return media, sidecar


def test_legacy_sqlite_mp4_and_json_migrate_idempotently(tmp_path):
    filename = tmp_path / "legacy.sqlite"
    root = tmp_path / "legacy-archive"
    media, sidecar = create_legacy_database(filename, root)

    first = Store(filename)
    assert first.sources()[0]["last_id"] == 22
    assert first.telegram.archive_root() == root.resolve()
    assert first.db.execute("SELECT COUNT(*) FROM tg_profiles").fetchone()[0] == 1
    assert first.db.execute("SELECT author_id FROM downloads").fetchone()[0] == 101
    report = build_catalog(first)
    first.close()

    second = Store(filename)
    assert second.db.execute("SELECT COUNT(*) FROM tg_profiles").fetchone()[0] == 1
    assert second.db.execute("SELECT COUNT(*) FROM tg_username_history").fetchone()[0] == 1
    assert second.sources()[0]["last_id"] == 22
    assert media.read_bytes() == b"old-video"
    assert sidecar.is_file()
    data = payload_from(report.parent / "data.js")
    assert data["videos"][0]["available"] is True
    assert data["profiles"][0]["username"] == "legacy_user"
    second.close()


def test_profiles_use_stable_ids_across_posts_and_username_changes(tmp_path):
    store = Store(tmp_path / "db.sqlite")
    root = tmp_path / "archive"
    first = store.add_source(Post("channelx", 7), str(root))
    second = store.add_source(Post("channelz", 8), str(root))
    for sid, message_id in ((first, 22), (second, 33)):
        path = video_note_path(root, sid, 101, message_id)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(b"video")
        record = video_record(sid, message_id, path)
        record["author_username"] = "old_name" if sid == first else "new_name"
        record["date"] = "2025-01-01" if sid == first else "2026-01-01"
        store.checkpoint(sid, message_id, record)
    store.telegram.observe_profile(
        author_id=202,
        username=None,
        display_name="No Username",
        observed_at="2026-02-01",
        source_id=first,
    )
    store.telegram.observe_profile(
        author_id=303,
        username="old_name",
        display_name="Reused Name",
        observed_at="2026-03-01",
        source_id=first,
    )

    data = build_payload(store, root)
    profiles = {item["author_id"]: item for item in data["profiles"]}
    assert profiles[101]["username"] == "new_name"
    assert {item["username"] for item in profiles[101]["username_history"]} == {
        "old_name", "new_name"
    }
    assert profiles[101]["post_ids"] == [first, second]
    assert profiles[202]["username"] == ""
    assert profiles[303]["username"] == "old_name"
    post = next(item for item in data["posts"] if item["source_id"] == first)
    assert post["profile_ids"] == [101, 202, 303]
    store.close()


def test_search_and_sort_cover_profile_history_and_posts():
    profiles = [
        {
            "author_id": 20, "username": "now", "display_name": "Zed",
            "first_seen": "2025", "last_seen": "2026", "last_activity": "2027",
            "video_ids": ["1", "2"],
            "username_history": [{"username": "before"}],
        },
        {
            "author_id": 10, "username": "alpha", "display_name": "Alice",
            "first_seen": "2024", "last_seen": "2024", "last_activity": "",
            "video_ids": [], "username_history": [],
        },
    ]
    posts = [
        {
            "source_id": 2, "post_id": 9, "channel_title": "Zeta",
            "channel_username": "zeta", "url": "https://t.me/zeta/9", "text": "",
            "published_at": "2026", "last_checked_at": "2027", "video_note_count": 3,
        },
        {
            "source_id": 1, "post_id": 7, "channel_title": "Alpha Channel",
            "channel_username": "alpha", "url": "https://t.me/alpha/7", "text": "hello",
            "published_at": "2025", "last_checked_at": "2025", "video_note_count": 1,
        },
    ]
    assert search_profiles(profiles, "@before")[0]["author_id"] == 20
    assert search_profiles(profiles, "10")[0]["display_name"] == "Alice"
    assert [item["author_id"] for item in sort_profiles(profiles, "video_count")] == [10, 20]
    assert search_posts(posts, "alpha/7")[0]["source_id"] == 1
    assert [item["post_id"] for item in sort_posts(posts, "channel")] == [7, 9]


def test_missing_files_unavailable_photos_and_malicious_paths_are_safe(tmp_path):
    store = Store(tmp_path / "db.sqlite")
    root = tmp_path / "archive"
    sid = store.add_source(Post("channelx", 7), str(root), title="<img src=x onerror=alert(1)>")
    missing = video_note_path(root, sid, 101, 22)
    store.telegram.observe_profile(
        author_id=101, username="evil</script><script>", display_name="<b>Evil</b>",
        source_id=sid,
    )
    store.telegram.record_profile_photo(
        101, "1", file_path=str(tmp_path / "outside.jpg"), size=99, photo_date="2026"
    )
    store.checkpoint(
        sid,
        22,
        {
            "source_id": sid, "message_id": 22, "author_id": 101,
            "author_username": "evil</script><script>", "author_name": "<b>Evil</b>",
            "date": "2026", "media_url": "https://t.me/channelx/7?comment=22",
            "file_path": str(missing), "size": 5,
        },
    )

    report = build_catalog(store)
    raw = (report.parent / "data.js").read_text(encoding="utf-8")
    data = payload_from(report.parent / "data.js")
    assert "<script>" not in raw and "<img" not in raw
    assert data["videos"][0]["available"] is False
    assert data["profiles"][0]["photos"][0]["valid_path"] is False
    assert data["profiles"][0]["photos"][0]["url"] == ""
    app = (report.parent / "app.js").read_text(encoding="utf-8")
    assert ".innerHTML" not in app
    assert "fetch(" not in app
    store.close()


def test_template_refresh_preserves_objects_and_rolls_back_on_error(tmp_path):
    store = Store(tmp_path / "db.sqlite")
    root = tmp_path / "archive"
    store.add_source(Post("channelx", 7), str(root))
    report = build_catalog(store)
    kept = report.parent / "objects" / "keep.bin"
    kept.write_bytes(b"keep")
    template = tmp_path / "template"
    template.mkdir()
    (template / "index.html").write_text("new index", encoding="utf-8")
    (template / "style.css").write_text("new style", encoding="utf-8")
    (template / "app.js").write_text("new app", encoding="utf-8")
    build_catalog(store, template_dir=template)
    assert report.read_text(encoding="utf-8") == "new index"
    assert kept.read_bytes() == b"keep"

    (template / "style.css").write_text("", encoding="utf-8")
    with pytest.raises(ValueError):
        build_catalog(store, template_dir=template)
    assert report.read_text(encoding="utf-8") == "new index"
    assert kept.read_bytes() == b"keep"
    store.close()


def test_report_command_runs_without_daemon(tmp_path, monkeypatch, capsys):
    data_home = tmp_path / "data"
    archive = tmp_path / "offline"
    monkeypatch.setenv("XDG_DATA_HOME", str(data_home))
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path / "local-app-data"))
    monkeypatch.setattr(
        sys, "argv", ["hoard", "telegram", "report", "--archive", str(archive)]
    )

    main()

    assert (archive / "telegram" / "index.html").is_file()
    assert "HTML-каталог обновлён" in capsys.readouterr().out


def test_object_paths_reject_traversal_and_media_reference_falls_back_to_uri(
    tmp_path, monkeypatch
):
    with pytest.raises(ValueError):
        object_path(tmp_path, "..", "escape")
    local = tmp_path / "archive" / "telegram" / "objects" / "clip.mp4"
    local.parent.mkdir(parents=True)
    local.write_bytes(b"x")
    monkeypatch.setattr(
        "hoard.archive_paths.os.path.relpath",
        lambda *_: (_ for _ in ()).throw(ValueError()),
    )
    reference = media_reference(
        str(local), catalog_dir=tmp_path / "archive" / "telegram",
        allowed_roots=[tmp_path / "archive"], expected_size=1,
    )
    assert reference["url"].startswith("file:")
    assert reference["available"] is True


def test_changed_archive_root_keeps_previous_media_root_available(tmp_path):
    store = Store(tmp_path / "db.sqlite")
    first = tmp_path / "first"
    second = tmp_path / "second"
    store.add_source(Post("channelx", 7), str(first))
    store.telegram.archive_root(second, persist=True)

    roots = store.telegram.allowed_media_roots(second)

    assert first.resolve() in roots
    assert second.resolve() in roots
    store.close()
