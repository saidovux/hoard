"""Private local Telegram API credentials. Not committed to Git."""
from __future__ import annotations

import json
import os
from pathlib import Path

from .paths import config_dir, ensure_private

TELEGRAM_DOWNLOADS_DEFAULT = 3
TELEGRAM_DOWNLOADS_MAX = 4


def telegram_download_limit(value: str | int | None = None) -> int:
    """Read the independent per-scan transfer limit with a strict safe range."""
    raw = os.environ.get("HOARD_TG_DOWNLOADS", str(TELEGRAM_DOWNLOADS_DEFAULT))
    if value is not None:
        raw = str(value)
    try:
        limit = int(raw)
    except (TypeError, ValueError) as exc:
        raise ValueError(
            "HOARD_TG_DOWNLOADS должен быть целым числом от 1 до 4"
        ) from exc
    if not 1 <= limit <= TELEGRAM_DOWNLOADS_MAX:
        raise ValueError("HOARD_TG_DOWNLOADS должен быть целым числом от 1 до 4")
    return limit


def credentials_file() -> Path:
    return config_dir() / "config.json"


def read_credentials() -> tuple[int, str] | None:
    api_id = os.environ.get("TG_API_ID") or os.environ.get("APP_ID")
    api_hash = os.environ.get("TG_API_HASH") or os.environ.get("APP_HASH")
    if api_id and api_hash:
        try:
            return int(api_id), api_hash
        except ValueError:
            return None
    try:
        data = json.loads(credentials_file().read_text(encoding="utf-8"))
        if data.get("api_id") and data.get("api_hash"):
            return int(data["api_id"]), str(data["api_hash"])
    except (FileNotFoundError, ValueError, TypeError, json.JSONDecodeError):
        return None
    return None


def write_credentials(api_id: int, api_hash: str) -> None:
    if api_id <= 0 or not api_hash.strip():
        raise ValueError("Укажи корректные API ID и API Hash")
    ensure_private(config_dir())
    path = credentials_file()
    temp = path.with_suffix(".tmp")
    fd = os.open(temp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump({"api_id": api_id, "api_hash": api_hash.strip()}, f)
            f.flush()
            os.fsync(f.fileno())
        os.replace(temp, path)
        path.chmod(0o600)
    finally:
        temp.unlink(missing_ok=True)
