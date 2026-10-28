"""Explicitly open one daemon-validated local media file without a shell."""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
from pathlib import Path


def open_local_file(path: Path) -> bool:
    """Hand one already validated file to the platform's default application."""
    if sys.platform == "win32":
        try:
            os.startfile(path)  # type: ignore[attr-defined]
        except OSError as exc:
            raise RuntimeError("Не удалось открыть внешнее приложение") from exc
        return True

    command = "open" if sys.platform == "darwin" else "xdg-open"
    executable = shutil.which(command)
    if executable is None:
        raise RuntimeError("Не найдено приложение для открытия локального файла")
    try:
        subprocess.Popen(
            [executable, str(path)],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            close_fds=True,
        )
    except OSError as exc:
        raise RuntimeError("Не удалось открыть внешнее приложение") from exc
    return True


__all__ = ["open_local_file"]
