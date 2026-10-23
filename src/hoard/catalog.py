"""Build the autonomous Telegram HTML catalog from SQLite."""
from __future__ import annotations

import json
import os
import re
import tempfile
from contextlib import suppress
from datetime import UTC, datetime
from filecmp import cmp
from importlib import resources
from pathlib import Path
from typing import Any

from .ai.psl import validate_psl_result
from .archive_paths import (
    media_reference,
    path_is_within,
    telegram_catalog_file,
    telegram_objects_dir,
)
from .paths import ensure_private
from .storage import Store

CATALOG_SCHEMA_VERSION = 2
STYLE_TOKEN = "/*__HOARD_STYLE__*/"
DATA_TOKEN = "/*__HOARD_DATA__*/"
APP_TOKEN = "/*__HOARD_APP__*/"


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


def filter_profiles(
    profiles: list[dict[str, Any]], filters: dict[str, Any]
) -> list[dict[str, Any]]:
    """Apply the catalog's composable profile filters with strict null semantics."""

    def flag_matches(actual: bool, requested: Any) -> bool:
        if requested is None:
            return True
        return actual is bool(requested)

    def number(value: Any) -> float | None:
        if value is None or isinstance(value, bool):
            return None
        try:
            return float(value)
        except (TypeError, ValueError):
            return None

    result = search_profiles(profiles, str(filters.get("query") or ""))
    selected: list[dict[str, Any]] = []
    source_id = filters.get("source_id")
    for profile in result:
        latest = profile.get("psl_latest")
        photos = profile.get("photos") or []
        videos = profile.get("video_ids") or []
        if source_id is not None and int(source_id) not in {
            int(item) for item in profile.get("post_ids") or []
        }:
            continue
        if not flag_matches(bool(photos), filters.get("has_photos")):
            continue
        if not flag_matches(bool(videos), filters.get("has_videos")):
            continue
        if not flag_matches(latest is not None, filters.get("has_psl")):
            continue
        numeric = {
            "raw_min": number(latest.get("raw_psl_min")) if latest else None,
            "raw_max": number(latest.get("raw_psl_max")) if latest else None,
            "boosted_min": number(latest.get("boosted_psl")) if latest else None,
            "age_min": number(latest.get("estimated_age")) if latest else None,
            "age_max": number(latest.get("estimated_age")) if latest else None,
        }
        rejected = False
        for name, actual in numeric.items():
            requested = number(filters.get(name))
            if requested is None:
                continue
            if actual is None:
                rejected = True
                break
            if name.endswith("_min") and actual < requested:
                rejected = True
                break
            if name.endswith("_max") and actual > requested:
                rejected = True
                break
        if not rejected:
            selected.append(profile)
    return selected


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
    numeric_keys = {"id", "video_count", "media_count", "raw_psl", "boosted_psl", "age"}

    def value(profile: dict[str, Any]):
        latest = profile.get("psl_latest") or {}
        if key == "id":
            return int(profile["author_id"])
        if key == "username":
            return profile["username"].casefold()
        if key == "discovered":
            return profile["first_seen"]
        if key == "video_count":
            return len(profile["video_ids"])
        if key == "media_count":
            return len(profile.get("video_ids") or []) + len(profile.get("photos") or [])
        if key == "raw_psl":
            low = latest.get("raw_psl_min")
            high = latest.get("raw_psl_max")
            return None if low is None or high is None else (float(low) + float(high)) / 2
        if key == "boosted_psl":
            value = latest.get("boosted_psl")
            return None if value is None else float(value)
        if key == "age":
            value = latest.get("estimated_age")
            return None if value is None else int(value)
        if key == "last_active":
            return profile["last_activity"] or profile["last_seen"]
        return profile["display_name"].casefold()

    if key not in numeric_keys:
        return sorted(profiles, key=value, reverse=descending)
    present = [profile for profile in profiles if value(profile) is not None]
    missing = [profile for profile in profiles if value(profile) is None]
    return sorted(present, key=value, reverse=descending) + missing


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


def _catalog_snapshot(
    store: Store, archive_root: Path
) -> tuple[int, dict[str, list[dict[str, Any]]], list[dict[str, Any]], list[Path]]:
    """Read Telegram, AI and root metadata from one short SQLite snapshot."""
    store.db.execute("BEGIN")
    try:
        revision = store.telegram.catalog_revision()
        rows = store.telegram.catalog_rows()
        analyses = store.ai.catalog_telegram_analyses()
        allowed_roots = store.telegram.allowed_media_roots(archive_root)
    except BaseException:
        store.db.rollback()
        raise
    else:
        store.db.commit()
    return revision, rows, analyses, allowed_roots


def _analysis_item(row: dict[str, Any]) -> tuple[dict[str, Any], bool]:
    attempt_state = _text(row.get("attempt_state"))
    result_status = _text(row.get("result_status"))
    status = result_status or attempt_state or "unknown"
    valid = False
    normalized: dict[str, Any] | None = None
    if result_status == "completed" and attempt_state == "completed":
        try:
            normalized = validate_psl_result(
                {
                    "status": result_status,
                    "presentation": row.get("presentation"),
                    "estimated_age": row.get("estimated_age"),
                    "raw_psl_min": row.get("raw_psl_min"),
                    "raw_psl_max": row.get("raw_psl_max"),
                    "boosted_psl": row.get("boosted_psl"),
                }
            )
        except ValueError:
            status = "invalid_result"
        else:
            valid = True
    elif attempt_state == "outcome_unknown":
        status = "outcome_unknown"
    elif attempt_state == "failed":
        status = "invalid_result" if result_status == "invalid_result" else "failed"

    item = {
        "status": status,
        "analyzed_at": _text(
            row.get("analyzed_at") or row.get("completed_at") or row.get("created_at")
        ),
        "actual_model": _text(row.get("actual_model")),
        "prompt_version_id": _text(row.get("prompt_version_id")),
        "schema_version": int(row.get("schema_version") or 0),
        "attempt_id": _text(row.get("attempt_id")),
        "failure_category": _text(row.get("failure_category")) or None,
        "presentation": normalized["presentation"] if valid and normalized else None,
        "estimated_age": normalized["estimated_age"] if valid and normalized else None,
        "raw_psl_min": normalized["raw_psl_min"] if valid and normalized else None,
        "raw_psl_max": normalized["raw_psl_max"] if valid and normalized else None,
        "boosted_psl": normalized["boosted_psl"] if valid and normalized else None,
    }
    return item, valid


def _build_payload(
    *,
    rows: dict[str, list[dict[str, Any]]],
    analyses: list[dict[str, Any]],
    archive_root: Path,
    allowed_roots: list[Path],
    revision: int,
) -> dict[str, Any]:
    catalog_dir = archive_root.expanduser().resolve()
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
            "photo_error_code": _text(row.get("photo_error_code")),
            "photos_checked_at": _text(row["photos_checked_at"]),
            "username_history": [],
            "post_ids": [],
            "video_ids": [],
            "photos": [],
            "psl_latest": None,
            "psl_history": [],
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

    for row in analyses:
        profile = profiles.get(int(row["author_id"]))
        if profile is None:
            continue
        item, valid = _analysis_item(row)
        profile["psl_history"].append(item)
        if valid and profile["psl_latest"] is None:
            profile["psl_latest"] = dict(item)

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
        "revision": revision,
        "generated_at": datetime.now(UTC).isoformat(),
        "posts": list(posts.values()),
        "profiles": list(profiles.values()),
        "videos": videos,
    }


def build_payload(store: Store, archive_root: Path) -> dict[str, Any]:
    revision, rows, analyses, allowed_roots = _catalog_snapshot(store, archive_root)
    return _build_payload(
        rows=rows,
        analyses=analyses,
        archive_root=archive_root,
        allowed_roots=allowed_roots,
        revision=revision,
    )


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


def _template_text(name: str, template_dir: Path | None) -> str:
    if template_dir is not None:
        return (template_dir / name).read_text(encoding="utf-8")
    package = resources.files("hoard.templates.telegram")
    return package.joinpath(name).read_text(encoding="utf-8")


def _bundle_html(
    index: str,
    *,
    style: str,
    data: str,
    app: str,
) -> str:
    components = {
        "HTML": index,
        "CSS": style,
        "JavaScript": app,
    }
    for label, content in components.items():
        if not content.strip():
            raise ValueError(f"Пустой компонент шаблона: {label}")
    for token in (STYLE_TOKEN, DATA_TOKEN, APP_TOKEN):
        if index.count(token) != 1:
            raise ValueError(f"В HTML-шаблоне нужен ровно один маркер {token}")
    if "</style" in style.casefold():
        raise ValueError("CSS-шаблон содержит закрывающий HTML-тег")
    if "</script" in app.casefold():
        raise ValueError("JavaScript-шаблон содержит закрывающий HTML-тег")
    replacements = {STYLE_TOKEN: style, DATA_TOKEN: data, APP_TOKEN: app}
    pattern = re.compile("|".join(re.escape(token) for token in replacements))
    return pattern.sub(lambda match: replacements[match.group(0)], index)


def _rewrite_obsolete_object_paths(
    store: Store, legacy_objects: Path, current_objects: Path
) -> None:
    changed = False
    with store.db:
        for table in ("downloads", "tg_profile_photos", "tg_post_media"):
            rows = list(store.db.execute(f"SELECT rowid,file_path FROM {table}"))
            for row in rows:
                try:
                    source = Path(str(row["file_path"])).expanduser().resolve(strict=False)
                    relative = source.relative_to(legacy_objects.resolve(strict=False))
                except (OSError, RuntimeError, ValueError):
                    continue
                destination = current_objects.joinpath(relative).resolve(strict=False)
                if destination.is_file() and path_is_within(destination, current_objects):
                    store.db.execute(
                        f"UPDATE {table} SET file_path=? WHERE rowid=?",
                        (str(destination), int(row["rowid"])),
                    )
                    changed = True
    if changed:
        store.telegram.set_catalog_dirty()


def _migrate_obsolete_split_catalog(store: Store, archive_root: Path) -> None:
    """Retire the short-lived split catalog without losing its media objects."""
    legacy = archive_root / "telegram"
    if not legacy.is_dir():
        return
    legacy_objects = legacy / "objects"
    current_objects = telegram_objects_dir(archive_root)
    if legacy_objects.is_dir() and not legacy_objects.is_symlink():
        for source in sorted(item for item in legacy_objects.rglob("*") if item.is_file()):
            if source.is_symlink() or not path_is_within(source, legacy_objects):
                continue
            relative = source.relative_to(legacy_objects)
            destination = current_objects / relative
            if not path_is_within(destination, current_objects):
                continue
            ensure_private(destination.parent)
            if destination.exists() or destination.is_symlink():
                if (
                    destination.is_file()
                    and not destination.is_symlink()
                    and cmp(source, destination, shallow=False)
                ):
                    source.unlink()
                continue
            os.replace(source, destination)
        _rewrite_obsolete_object_paths(store, legacy_objects, current_objects)
    for name in ("index.html", "style.css", "app.js", "data.js"):
        with suppress(OSError):
            (legacy / name).unlink(missing_ok=True)
    # Conflicting or unexpected files are deliberately preserved. In the normal
    # generated layout every directory is empty here and disappears.
    with suppress(OSError):
        directories = sorted(
            (item for item in legacy.rglob("*") if item.is_dir()),
            key=lambda item: len(item.parts),
            reverse=True,
        )
        for directory in directories:
            with suppress(OSError):
                directory.rmdir()
        legacy.rmdir()


def build_catalog(
    store: Store,
    *,
    archive_root: str | Path | None = None,
    template_dir: Path | None = None,
) -> Path:
    root = store.telegram.archive_root(archive_root, persist=archive_root is not None)
    root.mkdir(parents=True, exist_ok=True, mode=0o700)
    target = telegram_catalog_file(root)
    ensure_private(telegram_objects_dir(root))
    temporary: Path | None = None
    try:
        index = _template_text("index.html", template_dir)
        style = _template_text("style.css", template_dir)
        app = _template_text("app.js", template_dir)
        _migrate_obsolete_split_catalog(store, root)
        revision, rows, analyses, allowed_roots = _catalog_snapshot(store, root)
        payload = _build_payload(
            rows=rows,
            analyses=analyses,
            archive_root=root,
            allowed_roots=allowed_roots,
            revision=revision,
        )
        content = _bundle_html(
            index,
            style=style,
            data=_serialize_data(payload),
            app=app,
        )
        descriptor, temporary_name = tempfile.mkstemp(
            prefix=".telegram_archive-", suffix=".tmp", dir=root
        )
        temporary = Path(temporary_name)
        with os.fdopen(descriptor, "w", encoding="utf-8", newline="\n") as stream:
            stream.write(content)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, target)
        temporary = None
        if os.name != "nt":
            target.chmod(0o600)
        store.telegram.clear_catalog_dirty(revision)
    except BaseException:
        store.telegram.set_catalog_dirty()
        raise
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)
    return target


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
