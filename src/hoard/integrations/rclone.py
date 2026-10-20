"""Optional rclone integration, using safe argument lists (never a shell)."""
from __future__ import annotations

import asyncio
import re
import shutil
from pathlib import Path

_REMOTE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_. -]{0,99}:.+$")


def validate_upload(source: str, destination: str) -> tuple[str, str]:
    """Allow one local file or directory; destination must name a remote."""
    if not source.strip():
        raise ValueError("Укажи локальный файл или каталог для отправки")
    path = Path(source).expanduser().resolve()
    if not path.is_file() and not path.is_dir():
        raise ValueError(f"Не найден локальный файл или каталог: {path}")
    if (not _REMOTE.fullmatch(destination) or '://' in destination or
            '\x00' in destination or '\n' in destination or '\r' in destination):
        raise ValueError("Назначение должно быть вида remote:folder/file.ext")
    return str(path), destination


async def upload(source: str, destination: str) -> str:
    src, dst = validate_upload(source, destination)
    binary = shutil.which("rclone")
    if binary is None:
        raise RuntimeError("Установи rclone и настрой облако командой rclone config")
    # Use copy (folder) / copyto (file), NEVER sync: do not delete remote files.
    command = "copy" if Path(src).is_dir() else "copyto"
    process = await asyncio.create_subprocess_exec(
        binary, command, src, dst, "--stats", "0", "--retries", "2",
        stdout=asyncio.subprocess.DEVNULL,
        stderr=asyncio.subprocess.PIPE,
    )
    try:
        _, stderr = await process.communicate()
    except asyncio.CancelledError:
        process.terminate()
        try:
            await asyncio.wait_for(process.wait(), timeout=5)
        except TimeoutError:
            process.kill()
            await process.wait()
        raise
    if process.returncode != 0:
        raise RuntimeError(f"rclone завершился с кодом {process.returncode}: "
                           + stderr.decode(errors="replace")[-400:])
    return f"Загружен {Path(src).name} в {dst}"
