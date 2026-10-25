from __future__ import annotations

import json
import sqlite3
import sys
from pathlib import Path

import pytest

from hoard.ai.models import AIResponse
from hoard.ai.pricing import CostEstimate
from hoard.archive_paths import media_reference, object_path, video_note_path
from hoard.catalog import (
    APP_TOKEN,
    DATA_TOKEN,
    STYLE_TOKEN,
    build_catalog,
    build_payload,
    filter_profiles,
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


def insert_catalog_analysis(
    store: Store,
    *,
    analysis_id: str,
    author_id: int,
    state: str,
    completed_at: str | None,
    result_status: str | None = None,
    presentation: str | None = None,
    age: int | None = None,
    raw_min: float | None = None,
    raw_max: float | None = None,
    boosted: float | None = None,
) -> str:
    prompt = store.ai.ensure_psl_prompt()["current_version"]
    attempt_id = f"attempt-{analysis_id}"
    created_at = completed_at or "2026-01-01T00:00:00+00:00"
    with store.db:
        store.db.execute(
            """INSERT INTO ai_attempts(id,operation_key,scope,entity_kind,entity_id,
            provider,requested_model,actual_model,prompt_version_id,state,created_at,completed_at)
            VALUES(?,?,'telegram','telegram.psl',?,'deepseek','requested-model',
            'actual-model',?,?,?,?)""",
            (
                attempt_id,
                f"operation-{analysis_id}",
                analysis_id,
                prompt["id"],
                state,
                created_at,
                completed_at,
            ),
        )
        store.db.execute(
            """INSERT INTO ai_telegram_analyses(id,operation_key,author_id,attempt_id,
            prompt_version_id,schema_version,adult_confirmed,result_status,presentation,
            estimated_age,raw_psl_min,raw_psl_max,boosted_psl,actual_model,analyzed_at,
            created_at) VALUES(?,?,?,?,?,1,1,?,?,?,?,?,?,?,?,?)""",
            (
                analysis_id,
                f"operation-{analysis_id}",
                author_id,
                attempt_id,
                prompt["id"],
                result_status,
                presentation,
                age,
                raw_min,
                raw_max,
                boosted,
                "actual-model" if completed_at else None,
                completed_at,
                created_at,
            ),
        )
    return attempt_id


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


def test_catalog_template_markers_inside_data_are_not_replaced(tmp_path):
    store = Store(tmp_path / "db.sqlite")
    root = tmp_path / "archive"
    title = f"Literal {STYLE_TOKEN} {DATA_TOKEN} {APP_TOKEN} </script>"
    store.add_source(Post("channelx", 7), str(root), title=title)

    report = build_catalog(store)

    assert payload_from(report)["posts"][0]["channel_title"] == title
    html = report.read_text(encoding="utf-8")
    assert html.count('<script data-hoard-bundle="app">') == 1
    assert html.count("let catalog = window.HOARD_CATALOG || {};") == 1
    assert "</script><script>" not in html
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


def test_psl_export_selects_latest_valid_completed_result_by_author_id(tmp_path):
    store = Store(tmp_path / "db.sqlite")
    root = tmp_path / "archive"
    source_id = store.add_source(Post("channelx", 7), str(root))
    store.telegram.observe_profile(
        author_id=101,
        username="old_name",
        display_name="Synthetic Person",
        observed_at="2025-01-01",
        source_id=source_id,
    )
    store.telegram.observe_profile(
        author_id=101,
        username="new_name",
        display_name="Synthetic Person",
        observed_at="2026-01-01",
        source_id=source_id,
    )
    insert_catalog_analysis(
        store,
        analysis_id="analysis-a",
        author_id=101,
        state="completed",
        completed_at="2026-03-01T00:00:00+00:00",
        result_status="completed",
        presentation="female",
        age=25,
        raw_min=4,
        raw_max=5,
        boosted=5.5,
    )
    insert_catalog_analysis(
        store,
        analysis_id="analysis-z",
        author_id=101,
        state="completed",
        completed_at="2026-03-01T00:00:00+00:00",
        result_status="completed",
        presentation="female",
        age=27,
        raw_min=6,
        raw_max=7,
        boosted=7.5,
    )
    insert_catalog_analysis(
        store,
        analysis_id="analysis-failed",
        author_id=101,
        state="failed",
        completed_at="2026-04-01T00:00:00+00:00",
    )
    insert_catalog_analysis(
        store,
        analysis_id="analysis-invalid-old-data",
        author_id=101,
        state="completed",
        completed_at="2026-05-01T00:00:00+00:00",
        result_status="completed",
        presentation="female",
        age=16,
        raw_min=7,
        raw_max=8,
        boosted=8,
    )

    profile = build_payload(store, root)["profiles"][0]

    assert profile["author_id"] == 101
    assert profile["username"] == "new_name"
    assert profile["psl_latest"]["raw_psl_min"] == 6.0
    assert profile["psl_latest"]["attempt_id"] == "attempt-analysis-z"
    assert [item["status"] for item in profile["psl_history"]] == [
        "invalid_result", "failed", "completed", "completed"
    ]
    assert profile["psl_history"][0]["estimated_age"] is None
    assert profile["psl_history"][1]["raw_psl_min"] is None
    assert "instructions" not in profile["psl_history"][0]
    store.close()


def test_profiles_without_successful_psl_keep_null_and_numeric_filters_exclude_null():
    scored = {
        "author_id": 1,
        "username": "scored",
        "display_name": "Scored",
        "first_seen": "2024",
        "last_seen": "2026",
        "last_activity": "2026",
        "post_ids": [9],
        "video_ids": ["v"],
        "photos": [{"photo_id": "p"}],
        "username_history": [],
        "psl_latest": {
            "status": "completed", "raw_psl_min": 4.5, "raw_psl_max": 5.5,
            "boosted_psl": 6.0, "estimated_age": 24,
        },
    }
    unscored = {
        "author_id": 2,
        "username": "unscored",
        "display_name": "Unscored",
        "first_seen": "2025",
        "last_seen": "2025",
        "last_activity": "",
        "post_ids": [],
        "video_ids": [],
        "photos": [],
        "username_history": [],
        "psl_latest": None,
    }
    profiles = [unscored, scored]

    assert filter_profiles(profiles, {}) == profiles
    assert filter_profiles(profiles, {"has_psl": True}) == [scored]
    assert filter_profiles(profiles, {"has_psl": False}) == [unscored]
    assert filter_profiles(profiles, {"raw_min": 4, "raw_max": 6}) == [scored]
    assert filter_profiles(profiles, {"boosted_min": 6, "age_min": 21}) == [scored]
    assert filter_profiles(profiles, {"source_id": 9, "has_photos": True}) == [scored]
    assert sort_profiles(profiles, "raw_psl", descending=True) == [scored, unscored]
    assert sort_profiles(profiles, "raw_psl", descending=False) == [scored, unscored]


def test_psl_finish_marks_catalog_dirty_in_same_terminal_update(tmp_path):
    store = Store(tmp_path / "db.sqlite")
    attempt_id = insert_catalog_analysis(
        store,
        analysis_id="analysis-running",
        author_id=101,
        state="queued",
        completed_at=None,
    )
    store.telegram.set_catalog_dirty(False)
    before = store.telegram.catalog_revision()
    response = AIResponse("deepseek", "actual-model", "{}", "stop")

    store.ai.finish_attempt(
        attempt_id,
        state="completed",
        usage=None,
        response=response,
        error_code=None,
        cost=CostEstimate(None, None, "unknown", {}),
        price=None,
        analysis_result={
            "status": "completed",
            "presentation": "female",
            "estimated_age": 30,
            "raw_psl_min": 5.0,
            "raw_psl_max": 6.0,
            "boosted_psl": 6.5,
        },
    )

    assert store.telegram.catalog_dirty() is True
    assert store.telegram.catalog_revision() == before + 1
    row = store.db.execute(
        "SELECT result_status,raw_psl_min FROM ai_telegram_analyses WHERE id='analysis-running'"
    ).fetchone()
    assert tuple(row) == ("completed", 5.0)
    store.close()


def test_catalog_revision_preserves_change_during_atomic_generation(tmp_path, monkeypatch):
    import hoard.catalog as catalog_module

    store = Store(tmp_path / "db.sqlite")
    root = tmp_path / "archive"
    store.add_source(Post("channelx", 7), str(root))
    build_catalog(store)
    assert store.telegram.catalog_dirty() is False
    original_bundle = catalog_module._bundle_html
    changed = False

    def mutate_after_snapshot(*args, **kwargs):
        nonlocal changed
        if not changed:
            concurrent = Store(store.filename)
            concurrent.telegram.observe_profile(
                author_id=202, username="concurrent", display_name="Concurrent"
            )
            concurrent.close()
            changed = True
        return original_bundle(*args, **kwargs)

    monkeypatch.setattr(catalog_module, "_bundle_html", mutate_after_snapshot)
    report = build_catalog(store)

    assert store.telegram.catalog_dirty() is True
    assert payload_from(report)["profiles"] == []
    build_catalog(store)
    assert store.telegram.catalog_dirty() is False
    assert payload_from(report)["profiles"][0]["author_id"] == 202
    store.close()


def test_large_synthetic_catalog_keeps_one_profile_record_per_telegram_id(tmp_path):
    store = Store(tmp_path / "db.sqlite")
    root = tmp_path / "archive"
    with store.db:
        store.db.executemany(
            """INSERT INTO tg_profiles(author_id,current_username,display_name,first_seen,last_seen)
            VALUES(?,?,?,?,?)""",
            (
                (index, f"user{index}", f"Synthetic {index}", "2025", "2026")
                for index in range(1, 10_001)
            ),
        )
        store.telegram._set_dirty_sql()

    report = build_catalog(store, archive_root=root)
    payload = payload_from(report)

    assert len(payload["profiles"]) == 10_000
    assert payload["profiles"][0]["author_id"] == 1
    assert payload["profiles"][-1]["author_id"] == 10_000
    assert report.stat().st_size < 15 * 1024 * 1024
    store.close()


def test_frontend_has_independent_list_state_local_history_and_bounded_media():
    app = Path("src/hoard/templates/telegram/app.js").read_text(encoding="utf-8")

    assert "const lists =" in app
    assert "posts:" in app and "profiles:" in app
    assert "history.pushState" not in app  # Dynamic dispatch avoids duplicated route writes.
    assert '"pushState"' in app and '"replaceState"' in app
    assert 'window.addEventListener("popstate"' in app
    assert "rememberScroll" in app
    assert "state.detail" not in app
    assert "MEDIA_PAGE_SIZE" in app and "VIDEO_PAGE_SIZE" in app
    assert 'image.loading = "lazy"' in app
    assert 'video.preload = "none"' in app
    assert "fetch(" not in app
    assert ".innerHTML" not in app
    assert "external_confirmed: true" in app
    assert "Возрастной допуск 18+ применяется автоматически" in app
    assert "external-confirmed" not in app
    assert 'photos: "yes"' in app
    assert 'const defaultView = interactiveEnabled ? "profiles" : "posts"' in app
    assert "profilePhotos(profile)" in app
