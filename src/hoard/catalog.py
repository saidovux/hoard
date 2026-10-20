"""Build the autonomous Telegram HTML catalog from SQLite."""
from __future__ import annotations

import json
import os
import shutil
import tempfile
from datetime import UTC, datetime
from importlib import resources
from pathlib import Path
from typing import Any

from .archive_paths import media_reference, telegram_catalog_dir
from .paths import ensure_private
from .storage import Store

CATALOG_SCHEMA_VERSION = 1
TEMPLATE_FILES = ("index.html", "style.css", "app.js")


def _text(value: object) -> str:
    return str(value or "")


def _profile_search_blob(profile: dict[str, Any]) -> str:
    history = " ".join(item["username"] for item in profile["username_history"])
    return " ".join(
        [
            str(profile["author_id"]),
            profile["username"],
            profile["display_name"],
            history,
        ]
    ).casefold()


def search_profiles(profiles: list[dict[str, Any]], query: str) -> list[dict[str, Any]]:
    needle = query.strip().casefold().lstrip("@")
    if not needle:
        return list(profiles)
    return [profile for profile in profiles if needle in _profile_search_blob(profile)]


def search_posts(posts: list[dict[str, Any]], query: str) -> list[dict[str, Any]]:
    needle = query.strip().casefold().lstrip("@")
    if not needle:
        return list(posts)
    return [
        post
        for post in posts
        if needle
        in " ".join(
            [
                _text(post.get("source_id")),
                _text(post.get("post_id")),
                _text(post.get("channel_title")),
                _text(post.get("channel_username")),
                _text(post.get("url")),
                _text(post.get("text")),
            ]
        ).casefold()
    ]


def sort_profiles(
    profiles: list[dict[str, Any]], key: str, *, descending: bool = False
) -> list[dict[str, Any]]:
    def value(profile: dict[str, Any]):
        if key == "id":
            return int(profile["author_id"])
        if key == "username":
            return profile["username"].casefold()
        if key == "discovered":
            return profile["first_seen"]
        if key == "video_count":
            return len(profile["video_ids"])
        if key == "last_active":
            return profile["last_activity"] or profile["last_seen"]
        return profile["display_name"].casefold()

    return sorted(profiles, key=value, reverse=descending)


def sort_posts(
    posts: list[dict[str, Any]], key: str, *, descending: bool = False
) -> list[dict[str, Any]]:
    def value(post: dict[str, Any]):
        if key == "post_id":
            return int(post["post_id"])
        if key == "checked":
            return post["last_checked_at"]
        if key == "channel":
            return (post["channel_title"] or post["channel_username"]).casefold()
        if key == "video_count":
            return int(post["video_note_count"])
        return post["published_at"]

    return sorted(posts, key=value, reverse=descending)


def _media_item(
    row: dict[str, Any], *, catalog_dir: Path, allowed_roots: list[Path]
) -> dict[str, Any]:
    reference = media_reference(
        _text(row.get("file_path")),
        catalog_dir=catalog_dir,
        allowed_roots=allowed_roots,
        expected_size=int(row.get("size") or 0),
    )
    state = _text(row.get("status") or row.get("state") or "saved")
    if state == "saved" and not reference["available"]:
        state = "corrupt" if reference["exists"] else "missing"
    return {
        "url": reference["url"],
        "available": reference["available"],
        "valid_path": reference["valid"],
        "size": int(row.get("size") or 0),
        "state": state,
        "error": _text(row.get("error")),
    }


def build_payload(store: Store, archive_root: Path) -> dict[str, Any]:
    rows = store.telegram.catalog_rows()
    catalog_dir = telegram_catalog_dir(archive_root)
    allowed_roots = store.telegram.allowed_media_roots(archive_root)
    metadata = {int(row["source_id"]): row for row in rows["post_metadata"]}
    post_media: dict[int, list[dict[str, Any]]] = {}
    for raw in rows["post_media"]:
        item = _media_item(raw, catalog_dir=catalog_dir, allowed_roots=allowed_roots)
        item.update(
            {
                "media_id": _text(raw["media_id"]),
                "kind": _text(raw["kind"]),
            }
        )
        post_media.setdefault(int(raw["source_id"]), []).append(item)

    profiles: dict[int, dict[str, Any]] = {}
    for row in rows["profiles"]:
        author_id = int(row["author_id"])
        profiles[author_id] = {
            "author_id": author_id,
            "username": _text(row["current_username"]),
            "display_name": _text(row["display_name"]),
            "first_seen": _text(row["first_seen"]),
            "last_seen": _text(row["last_seen"]),
            "last_activity": "",
            "photo_error": _text(row["photo_error"]),
            "photos_checked_at": _text(row["photos_checked_at"]),
            "username_history": [],
            "post_ids": [],
            "video_ids": [],
            "photos": [],
        }
    for row in rows["username_history"]:
        profile = profiles.get(int(row["author_id"]))
        if profile is not None:
            profile["username_history"].append(
                {
                    "username": _text(row["username"]),
                    "first_seen": _text(row["first_seen"]),
                    "last_seen": _text(row["last_seen"]),
                }
            )
    for row in rows["profile_sources"]:
        profile = profiles.get(int(row["author_id"]))
        if profile is not None:
            profile["post_ids"].append(int(row["source_id"]))
    for raw in rows["profile_photos"]:
        profile = profiles.get(int(raw["author_id"]))
        if profile is None:
            continue
        item = _media_item(raw, catalog_dir=catalog_dir, allowed_roots=allowed_roots)
        item.update(
            {
                "photo_id": _text(raw["photo_id"]),
                "date": _text(raw["photo_date"]),
            }
        )
        profile["photos"].append(item)

    posts: dict[int, dict[str, Any]] = {}
    for source in rows["sources"]:
        source_id = int(source["id"])
        meta = metadata.get(source_id, {})
        posts[source_id] = {
            "source_id": source_id,
            "channel_username": _text(source["username"]),
            "channel_title": _text(meta.get("channel_title") or source["title"]),
            "post_id": int(source["post_id"]),
            "url": _text(source["url"]),
            "text": _text(meta.get("post_text")),
            "published_at": _text(meta.get("published_at")),
            "last_checked_at": _text(source["last_checked_at"]),
            "enabled": bool(source["enabled"]),
            "last_result": _text(source["last_result"]),
            "last_error": _text(source["last_error"]),
            "metadata_status": _text(meta.get("status") or "partial"),
            "profile_ids": [],
            "video_ids": [],
            "media": post_media.get(source_id, []),
            "video_note_count": 0,
            "author_count": 0,
        }

    videos: list[dict[str, Any]] = []
    authors_by_post: dict[int, set[int]] = {}
    for row in rows["profile_sources"]:
        source_id = int(row["source_id"])
        author_id = int(row["author_id"])
        if source_id in posts and author_id in profiles:
            authors_by_post.setdefault(source_id, set()).add(author_id)
    for raw in rows["downloads"]:
        source_id = int(raw["source_id"])
        message_id = int(raw["message_id"])
        video_id = f"{source_id}:{message_id}"
        item = _media_item(raw, catalog_dir=catalog_dir, allowed_roots=allowed_roots)
        author_id = raw.get("author_id")
        author_id = int(author_id) if author_id is not None else None
        item.update(
            {
                "video_id": video_id,
                "source_id": source_id,
                "message_id": message_id,
                "author_id": author_id,
                "message_date": _text(raw["date"]),
                "observed_at": _text(raw.get("observed_at")),
                "comment_url": _text(raw["media_url"]),
            }
        )
        videos.append(item)
        post = posts.get(source_id)
        if post is not None:
            post["video_ids"].append(video_id)
            post["video_note_count"] += 1
        if author_id is not None:
            authors_by_post.setdefault(source_id, set()).add(author_id)
            profile = profiles.get(author_id)
            if profile is not None:
                profile["video_ids"].append(video_id)
                profile["last_activity"] = max(
                    profile["last_activity"], _text(raw["date"])
                )
    for source_id, author_ids in authors_by_post.items():
        if source_id in posts:
            posts[source_id]["profile_ids"] = sorted(author_ids)
            posts[source_id]["author_count"] = len(author_ids)

    return {
        "schema_version": CATALOG_SCHEMA_VERSION,
        "generated_at": datetime.now(UTC).isoformat(),
        "posts": list(posts.values()),
        "profiles": list(profiles.values()),
        "videos": videos,
    }


def _serialize_data(payload: dict[str, Any]) -> str:
    data = json.dumps(payload, ensure_ascii=False, separators=(",", ":"))
    data = (
        data.replace("&", "\\u0026")
        .replace("<", "\\u003c")
        .replace(">", "\\u003e")
        .replace("\u2028", "\\u2028")
        .replace("\u2029", "\\u2029")
    )
    return f"window.HOARD_CATALOG={data};\n"


def _template_bytes(name: str, template_dir: Path | None) -> bytes:
    if template_dir is not None:
        return (template_dir / name).read_bytes()
    package = resources.files("hoard.templates.telegram")
    return package.joinpath(name).read_bytes()


def build_catalog(
    store: Store,
    *,
    archive_root: str | Path | None = None,
    template_dir: Path | None = None,
) -> Path:
    root = store.telegram.archive_root(archive_root, persist=archive_root is not None)
    target = telegram_catalog_dir(root)
    ensure_private(target)
    ensure_private(target / "objects")
    stage = Path(tempfile.mkdtemp(prefix=".catalog-build-", dir=target))
    try:
        for name in TEMPLATE_FILES:
            content = _template_bytes(name, template_dir)
            if not content:
                raise ValueError(f"Пустой файл шаблона: {name}")
            (stage / name).write_bytes(content)
        store.telegram.set_catalog_dirty(False)
        payload = build_payload(store, root)
        data_file = stage / "data.js"
        with data_file.open("w", encoding="utf-8", newline="\n") as stream:
            stream.write(_serialize_data(payload))
            stream.flush()
            os.fsync(stream.fileno())
        names = (*TEMPLATE_FILES, "data.js")
        previous: dict[str, Path | None] = {}
        for name in names:
            destination = target / name
            if destination.is_file():
                backup = stage / f"previous-{name}"
                shutil.copyfile(destination, backup)
                previous[name] = backup
            else:
                previous[name] = None
        replaced: list[str] = []
        try:
            for name in names:
                destination = target / name
                os.replace(stage / name, destination)
                replaced.append(name)
                if os.name != "nt":
                    destination.chmod(0o600)
        except BaseException:
            for name in reversed(replaced):
                destination = target / name
                backup = previous[name]
                if backup is None:
                    destination.unlink(missing_ok=True)
                elif backup.exists():
                    os.replace(backup, destination)
            raise
    except BaseException:
        store.telegram.set_catalog_dirty()
        raise
    finally:
        shutil.rmtree(stage, ignore_errors=True)
    return target / "index.html"


def build_catalog_from_db(
    database: Path,
    *,
    archive_root: str | Path | None = None,
) -> Path:
    store = Store(database)
    try:
        return build_catalog(store, archive_root=archive_root)
    finally:
        store.close()
