"""Safe, cross-platform paths for the local Telegram archive and catalog."""
from __future__ import annotations

import os
from pathlib import Path
from urllib.parse import quote

CATALOG_FILENAME = "telegram_archive.html"
OBJECTS_DIRECTORY = "hoard_objects"


def default_archive_root() -> Path:
    configured = os.environ.get("HOARD_ARCHIVE_HOME")
    if configured:
        return Path(configured).expanduser().resolve()
    return (Path.home() / "Videos" / "hoard").resolve()


def telegram_catalog_file(archive_root: Path) -> Path:
    return archive_root.expanduser().resolve() / CATALOG_FILENAME


def telegram_objects_dir(archive_root: Path) -> Path:
    return archive_root.expanduser().resolve() / OBJECTS_DIRECTORY


def _safe_component(value: str | int) -> str:
    component = str(value)
    if (
        not component
        or component in {".", ".."}
        or "/" in component
        or "\\" in component
        or Path(component).name != component
    ):
        raise ValueError("Некорректный компонент пути архива")
    return component


def object_path(archive_root: Path, *parts: str | int) -> Path:
    """Build a path below hoard_objects without accepting path traversal."""
    objects = telegram_objects_dir(archive_root)
    candidate = objects.joinpath(*(_safe_component(part) for part in parts))
    resolved_objects = objects.resolve(strict=False)
    resolved_candidate = candidate.resolve(strict=False)
    if not path_is_within(resolved_candidate, resolved_objects):
        raise ValueError("Путь объекта выходит за пределы Telegram-архива")
    return candidate


def video_note_path(
    archive_root: Path,
    source_id: int,
    author_id: int | None,
    message_id: int,
) -> Path:
    author = f"author_{author_id}" if author_id is not None else "author_unknown"
    return object_path(
        archive_root,
        "video_notes",
        f"source_{int(source_id)}",
        author,
        f"message_{int(message_id)}.mp4",
    )


def profile_photo_path(archive_root: Path, author_id: int, photo_id: int | str) -> Path:
    return object_path(
        archive_root,
        "profiles",
        f"author_{int(author_id)}",
        "photos",
        f"photo_{_safe_component(photo_id)}.jpg",
    )


def post_media_path(
    archive_root: Path,
    source_id: int,
    media_id: int | str,
    suffix: str,
) -> Path:
    normalized = suffix.lower()
    if normalized not in {".jpg", ".jpeg", ".png", ".webp", ".mp4", ".webm", ".bin"}:
        normalized = ".bin"
    return object_path(
        archive_root,
        "posts",
        f"source_{int(source_id)}",
        f"media_{_safe_component(media_id)}{normalized}",
    )


def path_is_within(candidate: Path, root: Path) -> bool:
    try:
        candidate.resolve(strict=False).relative_to(root.resolve(strict=False))
    except (OSError, ValueError):
        return False
    return True


def media_reference(
    filename: str,
    *,
    catalog_dir: Path,
    allowed_roots: list[Path],
    expected_size: int | None = None,
) -> dict[str, object]:
    """Return a browser-safe local URL only for a path in an approved archive root."""
    if not filename or "\x00" in filename:
        return {"url": "", "available": False, "exists": False, "valid": False}
    try:
        candidate = Path(filename).expanduser().resolve(strict=False)
    except (OSError, RuntimeError, ValueError):
        return {"url": "", "available": False, "exists": False, "valid": False}
    if not any(path_is_within(candidate, root) for root in allowed_roots):
        return {"url": "", "available": False, "exists": False, "valid": False}
    exists = candidate.is_file()
    available = exists
    if available and expected_size is not None and expected_size > 0:
        try:
            available = candidate.stat().st_size == expected_size
        except OSError:
            available = False
    try:
        relative = os.path.relpath(candidate, catalog_dir.resolve(strict=False))
        url = quote(relative.replace(os.sep, "/"), safe="/._-~")
    except (OSError, ValueError):
        try:
            url = candidate.as_uri()
        except ValueError:
            return {"url": "", "available": False, "exists": exists, "valid": False}
    return {"url": url, "available": available, "exists": exists, "valid": True}
