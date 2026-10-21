"""Small UI-facing adapter around Hoard's existing private IPC."""

from __future__ import annotations

from typing import Protocol

from hoard.ipc import ensure_daemon, request


class StatusAPI(Protocol):
    """The only daemon operations required by the round-two TUI."""

    async def ensure_started(self) -> None: ...

    async def status(self) -> dict: ...


class IPCStatusAPI:
    """Production adapter; endpoint and authentication stay owned by hoard.ipc."""

    async def ensure_started(self) -> None:
        await ensure_daemon()

    async def status(self) -> dict:
        return await request("status", timeout=4)
