"""Platform-specific lock and Windows-only local control endpoint."""
from __future__ import annotations

import contextlib
import json
import os
import secrets
from collections.abc import Iterator
from pathlib import Path


def is_windows() -> bool:
    return os.name == "nt"


@contextlib.contextmanager
def single_instance(path: Path) -> Iterator[None]:
    """Hold a cross-platform file lock for the daemon lifetime."""
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a+b") as lock:
        if is_windows():
            import msvcrt

            # Lock the first byte directly; reading a byte locked by another
            # process raises PermissionError on Windows before we can test the lock.
            lock.seek(0)
            try:
                msvcrt.locking(lock.fileno(), msvcrt.LK_NBLCK, 1)
            except OSError as exc:
                raise RuntimeError("Hoard уже запущен") from exc
            try:
                yield
            finally:
                lock.seek(0)
                msvcrt.locking(lock.fileno(), msvcrt.LK_UNLCK, 1)
        else:
            import fcntl

            try:
                fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except OSError as exc:
                raise RuntimeError("Hoard уже запущен") from exc
            try:
                yield
            finally:
                fcntl.flock(lock, fcntl.LOCK_UN)


def instance_running(path: Path) -> bool:
    """Return whether another process currently holds the daemon lock."""
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a+b") as lock:
        if is_windows():
            import msvcrt

            lock.seek(0)
            try:
                msvcrt.locking(lock.fileno(), msvcrt.LK_NBLCK, 1)
            except OSError:
                return True
            lock.seek(0)
            msvcrt.locking(lock.fileno(), msvcrt.LK_UNLCK, 1)
            return False
        import fcntl

        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            return True
        fcntl.flock(lock, fcntl.LOCK_UN)
        return False


def write_endpoint(path: Path, port: int, token: str) -> None:
    """Atomically publish the loopback IPC endpoint in per-user state."""
    if not (0 < port < 65536) or len(token) < 32:
        raise ValueError("Некорректный локальный IPC endpoint")
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_name(f".{path.name}.{secrets.token_hex(8)}.tmp")
    try:
        fd = os.open(temp, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as stream:
            json.dump({"port": port, "token": token}, stream)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temp, path)
    finally:
        temp.unlink(missing_ok=True)


def read_endpoint(path: Path) -> tuple[int, str]:
    data = json.loads(path.read_text(encoding="utf-8"))
    port, token = data.get("port"), data.get("token")
    if not isinstance(port, int) or not (0 < port < 65536):
        raise ValueError("Некорректный порт Hoard")
    if not isinstance(token, str) or len(token) < 32:
        raise ValueError("Некорректный токен Hoard")
    return port, token


def remove_endpoint_if_owned(path: Path, token: str) -> None:
    try:
        _, current = read_endpoint(path)
    except (OSError, ValueError, TypeError, json.JSONDecodeError):
        return
    if secrets.compare_digest(current, token):
        path.unlink(missing_ok=True)
