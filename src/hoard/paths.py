"""XDG paths. Runtime state is not stored in the Git repository."""
from __future__ import annotations

import os
from pathlib import Path


def config_dir() -> Path:
    return Path(os.environ.get("XDG_CONFIG_HOME", Path.home() / ".config")) / "hoard"


def state_dir() -> Path:
    return Path(os.environ.get("XDG_STATE_HOME", Path.home() / ".local/state")) / "hoard"


def data_dir() -> Path:
    return Path(os.environ.get("XDG_DATA_HOME", Path.home() / ".local/share")) / "hoard"


def ensure_private(directory: Path) -> Path:
    directory.mkdir(parents=True, exist_ok=True, mode=0o700)
    try:
        directory.chmod(0o700)
    except OSError:
        pass
    return directory


def socket_path() -> Path:
    # User-owned state path; UNIX socket restricted to 0600 after binding.
    return state_dir() / "hoard.sock"


def db_path() -> Path:
    return data_dir() / "hoard.sqlite3"


def session_path() -> Path:
    return data_dir() / "telegram"
