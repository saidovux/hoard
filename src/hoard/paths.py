"""User state paths: Linux XDG and Windows AppData."""
from __future__ import annotations

import os
from pathlib import Path

from .platform_support import is_windows


def config_dir() -> Path:
    if is_windows():
        return Path(os.environ.get("APPDATA") or Path.home() / "AppData/Roaming") / "Hoard"
    return Path(os.environ.get("XDG_CONFIG_HOME", Path.home() / ".config")) / "hoard"


def state_dir() -> Path:
    if is_windows():
        return (Path(os.environ.get("LOCALAPPDATA") or Path.home() / "AppData/Local")
                / "Hoard" / "state")
    return Path(os.environ.get("XDG_STATE_HOME", Path.home() / ".local/state")) / "hoard"


def data_dir() -> Path:
    if is_windows():
        return (Path(os.environ.get("LOCALAPPDATA") or Path.home() / "AppData/Local")
                / "Hoard" / "data")
    return Path(os.environ.get("XDG_DATA_HOME", Path.home() / ".local/share")) / "hoard"


def ensure_private(directory: Path) -> Path:
    directory.mkdir(parents=True, exist_ok=True, mode=0o700)
    if not is_windows():
        directory.chmod(0o700)
    return directory


def socket_path() -> Path:
    return state_dir() / "hoard.sock"


def endpoint_path() -> Path:
    return state_dir() / "endpoint.json"


def db_path() -> Path:
    return data_dir() / "hoard.sqlite3"


def session_path() -> Path:
    return data_dir() / "telegram"
