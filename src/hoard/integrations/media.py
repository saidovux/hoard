"""Optional yt-dlp adapter. User-controlled public web links only."""
from __future__ import annotations

import asyncio
import shutil
from pathlib import Path
from urllib.parse import urlsplit


def validate_media(url: str, directory: str) -> tuple[str, str]:
    parsed = urlsplit(url)
    if (
        parsed.scheme not in {"https", "http"}
        or not parsed.hostname
        or parsed.username
        or parsed.password
    ):
        raise ValueError("Для медиа нужна обычная http(s)-ссылка")
    if not directory.strip():
        raise ValueError("Укажи каталог для скачивания")
    folder = Path(directory).expanduser().resolve()
    return url, str(folder)


async def download(url: str, directory: str) -> str:
    url, directory = validate_media(url, directory)
    executable = shutil.which("yt-dlp")
    if executable is None:
        raise RuntimeError("Установи yt-dlp, чтобы скачивать медиа с сайтов")
    Path(directory).mkdir(parents=True, exist_ok=True)
    proc = await asyncio.create_subprocess_exec(
        executable, "--no-playlist", "--no-progress", "--no-overwrites",
        "-P", directory, "--print", "after_move:filepath", url,
        stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
    )
    try:
        stdout, stderr = await proc.communicate()
    except asyncio.CancelledError:
        proc.terminate()
        try:
            await asyncio.wait_for(proc.wait(), 5)
        except TimeoutError:
            proc.kill()
            await proc.wait()
        raise
    if proc.returncode:
        raise RuntimeError(f"yt-dlp ({proc.returncode}): {stderr.decode(errors='replace')[-400:]}")
    result = stdout.decode(errors="replace").strip().splitlines()
    return f"Медиа сохранено: {result[-1]}" if result else "Медиа скачано"
