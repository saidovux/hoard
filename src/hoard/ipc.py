"""Client for the local-only daemon socket, plus detached startup."""
from __future__ import annotations

import asyncio
import json
import subprocess
import sys

from .paths import ensure_private, socket_path, state_dir


async def request(method: str, args: dict | None = None, timeout: float = 12) -> dict:
    reader, writer = await asyncio.wait_for(
        asyncio.open_unix_connection(str(socket_path())), timeout
    )
    try:
        payload = json.dumps(
            {"method": method, "args": args or {}}, ensure_ascii=False
        )
        writer.write((payload + "\n").encode())
        await writer.drain()
        line = await asyncio.wait_for(reader.readline(), timeout)
        if not line:
            raise RuntimeError("Служба Hoard закрыла соединение")
        result = json.loads(line)
        if not result["ok"]:
            raise RuntimeError(result.get("error", "Ошибка службы"))
        return result["data"]
    finally:
        writer.close()
        await writer.wait_closed()


async def ensure_daemon() -> None:
    try:
        await request("status", timeout=1)
        return
    except (OSError, RuntimeError, TimeoutError):
        pass
    ensure_private(state_dir())
    # Start a fully detached service. No TUI/Telegram client remains in launcher.
    log = state_dir() / "daemon.log"
    log.touch(mode=0o600, exist_ok=True)
    log.chmod(0o600)
    with log.open("ab") as f:
        subprocess.Popen([sys.executable, "-m", "hoard", "daemon"],
                         stdin=subprocess.DEVNULL, stdout=f, stderr=f,
                         start_new_session=True, close_fds=True)
    for _ in range(45):
        await asyncio.sleep(0.12)
        try:
            await request("status", timeout=1)
            return
        except (OSError, RuntimeError, TimeoutError):
            continue
    raise RuntimeError(f"Не удалось запустить службу; проверь {log}")
