"""Private daemon IPC and background startup; no external listeners."""
from __future__ import annotations

import asyncio
import json
import subprocess
import sys

from . import __version__
from .config import telegram_download_limit
from .paths import endpoint_path, ensure_private, socket_path, state_dir
from .platform_support import instance_running, is_windows, read_endpoint

IPC_MAX_BYTES = 64 * 1024
IPC_PROTOCOL = 1


class DaemonCompatibilityError(RuntimeError):
    """A responding daemon is too old or otherwise incompatible with this client."""


def validate_daemon_status(status: dict) -> None:
    daemon_version = str(status.get("version") or "неизвестна")
    protocol = status.get("protocol")
    if daemon_version != __version__ or protocol != IPC_PROTOCOL:
        raise DaemonCompatibilityError(
            f"Запущена устаревшая служба Hoard (daemon {daemon_version}, клиент {__version__}).\n"
            "Выполни: hoard shutdown\n"
            "Затем снова запусти: hoard"
        )


def encode_ipc_message(value: dict) -> bytes:
    payload = (json.dumps(value, ensure_ascii=False, separators=(",", ":")) + "\n").encode()
    if len(payload) > IPC_MAX_BYTES:
        raise ValueError("IPC-сообщение превышает лимит 64 KiB; используй пагинацию или вложение")
    return payload


async def request(method: str, args: dict | None = None, timeout: float = 12) -> dict:
    payload = {"method": method, "args": args or {}}
    if is_windows():
        port, token = read_endpoint(endpoint_path())
        payload["token"] = token
        encoded = encode_ipc_message(payload)
        reader, writer = await asyncio.wait_for(
            asyncio.open_connection("127.0.0.1", port), timeout
        )
    else:
        encoded = encode_ipc_message(payload)
        reader, writer = await asyncio.wait_for(
            asyncio.open_unix_connection(str(socket_path())), timeout
        )
    try:
        writer.write(encoded)
        await writer.drain()
        try:
            line = await asyncio.wait_for(reader.readline(), timeout)
        except ValueError as exc:
            raise RuntimeError(
                "Ответ службы превышает лимит IPC; запроси меньшую страницу"
            ) from exc
        if not line:
            raise RuntimeError("Служба Hoard закрыла соединение")
        if len(line) > IPC_MAX_BYTES:
            raise RuntimeError("Ответ службы превышает лимит IPC; запроси меньшую страницу")
        result = json.loads(line)
        if not result["ok"]:
            raise RuntimeError(result.get("error", "Ошибка службы"))
        return result["data"]
    finally:
        writer.close()
        await writer.wait_closed()


async def _daemon_status(*, timeout: float) -> dict:
    """Use the bounded probe, while still identifying pre-migration daemons."""
    try:
        return await request("status_brief", timeout=timeout)
    except RuntimeError as exc:
        if "Неизвестная команда" not in str(exc):
            raise
        return await request("status", timeout=timeout)


async def ensure_daemon() -> None:
    try:
        status = await _daemon_status(timeout=1)
        validate_daemon_status(status)
        return
    except DaemonCompatibilityError:
        raise
    except (OSError, RuntimeError, TimeoutError, ValueError, json.JSONDecodeError):
        pass
    ensure_private(state_dir())
    log = state_dir() / "daemon.log"
    log.touch(mode=0o600, exist_ok=True)
    if instance_running(state_dir() / "service.lock"):
        for _ in range(100):
            await asyncio.sleep(0.12)
            try:
                status = await _daemon_status(timeout=1)
                validate_daemon_status(status)
                return
            except DaemonCompatibilityError:
                raise
            except (OSError, RuntimeError, TimeoutError, ValueError, json.JSONDecodeError):
                continue
        raise RuntimeError(
            f"Процесс daemon работает, но ещё инициализируется; проверь {log}"
        )
    # Validate inherited daemon configuration before detaching so the invoking
    # terminal receives the actionable value error instead of only a log path.
    telegram_download_limit()
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
        process = subprocess.Popen(command, **options)
    for _ in range(100):
        await asyncio.sleep(0.12)
        if process.poll() is not None:
            raise RuntimeError(f"Процесс daemon завершился при запуске; проверь {log}")
        try:
            status = await _daemon_status(timeout=1)
            validate_daemon_status(status)
            return
        except DaemonCompatibilityError:
            raise
        except (OSError, RuntimeError, TimeoutError, ValueError, json.JSONDecodeError):
            continue
    raise RuntimeError(f"Daemon запущен, но не завершил инициализацию; проверь {log}")
