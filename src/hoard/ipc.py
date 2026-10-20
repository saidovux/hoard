"""Private daemon IPC and background startup; no external listeners."""
from __future__ import annotations

import asyncio
import json
import subprocess
import sys

from .paths import endpoint_path, ensure_private, socket_path, state_dir
from .platform_support import is_windows, read_endpoint


async def request(method: str, args: dict | None = None, timeout: float = 12) -> dict:
    if is_windows():
        port, token = read_endpoint(endpoint_path())
        reader, writer = await asyncio.wait_for(
            asyncio.open_connection("127.0.0.1", port), timeout
        )
    else:
        token = None
        reader, writer = await asyncio.wait_for(
            asyncio.open_unix_connection(str(socket_path())), timeout
        )
    try:
        payload = {"method": method, "args": args or {}}
        if token is not None:
            payload["token"] = token
        writer.write((json.dumps(payload, ensure_ascii=False) + "\n").encode())
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
    except (OSError, RuntimeError, TimeoutError, ValueError, json.JSONDecodeError):
        pass
    ensure_private(state_dir())
    log = state_dir() / "daemon.log"
    log.touch(mode=0o600, exist_ok=True)
    command = [sys.executable, "-m", "hoard", "daemon"]
    options = {
        "stdin": subprocess.DEVNULL,
        "stdout": None,
        "stderr": None,
        "close_fds": True,
    }
    if is_windows():
        options["creationflags"] = subprocess.DETACHED_PROCESS | subprocess.CREATE_NEW_PROCESS_GROUP
    else:
        options["start_new_session"] = True
    with log.open("ab") as stream:
        options["stdout"] = stream
        options["stderr"] = stream
        subprocess.Popen(command, **options)
    for _ in range(45):
        await asyncio.sleep(0.12)
        try:
            await request("status", timeout=1)
            return
        except (OSError, RuntimeError, TimeoutError, ValueError, json.JSONDecodeError):
            continue
    raise RuntimeError(f"Не удалось запустить службу; проверь {log}")
