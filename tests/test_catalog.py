from __future__ import annotations

import json
import sqlite3
import sys
from pathlib import Path

import pytest

from hoard.archive_paths import media_reference, object_path, video_note_path
from hoard.catalog import (
    APP_TOKEN,
    DATA_TOKEN,
    STYLE_TOKEN,
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
    start = text.index(prefix) + len(prefix)
    return json.JSONDecoder().raw_decode(text[start:])[0]


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

    assert report == root / "telegram_archive.html"
    assert {item.name for item in root.iterdir()} == {
        "telegram_archive.html", "hoard_objects"
    }
    data = payload_from(report)
    assert data["posts"][0]["channel_title"] == "Channel X"
    assert data["profiles"] == []
    html = report.read_text(encoding="utf-8")
    assert '<style data-hoard-bundle="style">' in html
    assert '<script data-hoard-bundle="data">' in html
    assert '<script data-hoard-bundle="app">' in html
    assert "<script src=" not in html
    assert "<link rel=\"stylesheet\"" not in html
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
    data = payload_from(report)
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
    raw = report.read_text(encoding="utf-8")
    data = payload_from(report)
    assert "<img src=x onerror=alert(1)>" not in raw
    assert "</script><script>" not in raw
    assert data["videos"][0]["available"] is False
    assert data["profiles"][0]["photos"][0]["valid_path"] is False
    assert data["profiles"][0]["photos"][0]["url"] == ""
    assert ".innerHTML" not in raw
    assert "fetch(" not in raw
    store.close()


def test_template_refresh_preserves_objects_and_rolls_back_on_error(tmp_path):
    store = Store(tmp_path / "db.sqlite")
    root = tmp_path / "archive"
    store.add_source(Post("channelx", 7), str(root))
    legacy = root / "telegram"
    (legacy / "objects").mkdir(parents=True)
    for name in ("index.html", "style.css", "app.js", "data.js"):
        (legacy / name).write_text("obsolete", encoding="utf-8")
    report = build_catalog(store)
    assert not legacy.exists()
    kept = root / "hoard_objects" / "keep.bin"
    kept.write_bytes(b"keep")
    template = tmp_path / "template"
    template.mkdir()
    custom_index = (
        f"<!doctype html><style>{STYLE_TOKEN}</style>"
        f"<script>{DATA_TOKEN}</script><script>{APP_TOKEN}</script>"
    )
    (template / "index.html").write_text(custom_index, encoding="utf-8")
    (template / "style.css").write_text("new style", encoding="utf-8")
    (template / "app.js").write_text("new app", encoding="utf-8")
    build_catalog(store, template_dir=template)
    before_error = report.read_text(encoding="utf-8")
    assert "new style" in before_error
    assert "new app" in before_error
    assert "window.HOARD_CATALOG=" in before_error
    assert kept.read_bytes() == b"keep"

    (template / "style.css").write_text("", encoding="utf-8")
    with pytest.raises(ValueError):
        build_catalog(store, template_dir=template)
    assert report.read_text(encoding="utf-8") == before_error
    assert kept.read_bytes() == b"keep"
    store.close()


def test_split_catalog_objects_move_out_of_telegram_and_keep_database_links(tmp_path):
    store = Store(tmp_path / "db.sqlite")
    root = tmp_path / "archive"
    source_id = store.add_source(Post("channelx", 7), str(root))
    old_video = (
        root
        / "telegram"
        / "objects"
        / "video_notes"
        / f"source_{source_id}"
        / "author_101"
        / "message_22.mp4"
    )
    old_video.parent.mkdir(parents=True)
    old_video.write_bytes(b"video")
    old_video.with_suffix(".json").write_text("{}", encoding="utf-8")
    store.checkpoint(source_id, 22, video_record(source_id, 22, old_video))

    report = build_catalog(store)

    new_video = (
        root
        / "hoard_objects"
        / "video_notes"
        / f"source_{source_id}"
        / "author_101"
        / "message_22.mp4"
    )
    assert report == root / "telegram_archive.html"
    assert new_video.read_bytes() == b"video"
    assert new_video.with_suffix(".json").read_text(encoding="utf-8") == "{}"
    assert not (root / "telegram").exists()
    assert store.db.execute(
        "SELECT file_path FROM downloads WHERE source_id=? AND message_id=22",
        (source_id,),
    ).fetchone()[0] == str(new_video.resolve())
    assert payload_from(report)["videos"][0]["url"].startswith("hoard_objects/")
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

    assert (archive / "telegram_archive.html").is_file()
    assert "HTML-каталог обновлён" in capsys.readouterr().out


def test_object_paths_reject_traversal_and_media_reference_falls_back_to_uri(
    tmp_path, monkeypatch
):
    with pytest.raises(ValueError):
        object_path(tmp_path, "..", "escape")
    local = tmp_path / "archive" / "hoard_objects" / "clip.mp4"
    local.parent.mkdir(parents=True)
    local.write_bytes(b"x")
    monkeypatch.setattr(
        "hoard.archive_paths.os.path.relpath",
        lambda *_: (_ for _ in ()).throw(ValueError()),
    )
    reference = media_reference(
        str(local), catalog_dir=tmp_path / "archive",
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
