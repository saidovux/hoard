import asyncio
import json
import os

import pytest

from hoard import __version__
from hoard.ipc import (
    IPC_MAX_BYTES,
    IPC_PROTOCOL,
    DaemonCompatibilityError,
    encode_ipc_message,
    ensure_daemon,
)
from hoard.service import serve_connection


@pytest.mark.skipif(os.name == "nt", reason="Unix sockets require Linux/macOS")
async def test_socket_dispatch(tmp_path):
    class Stub:
        async def handle(self, method, args):
            if method == "test":
                return {"value": args.get("value", 0)}
            raise ValueError("bad")

    server = await asyncio.start_unix_server(
        lambda r, w: serve_connection(Stub(), r, w),
        path=str(tmp_path / "s.sock"),
    )
    async with server:
        reader, writer = await asyncio.open_unix_connection(str(tmp_path / "s.sock"))
        writer.write(b'{"method":"test","args":{"value":4}}\n')
        await writer.drain()
        value = json.loads(await reader.readline())
        assert value["ok"] and value["data"]["value"] == 4
        writer.close()
        await writer.wait_closed()


def test_ipc_encoder_rejects_oversized_message():
    with pytest.raises(ValueError, match="64 KiB"):
        encode_ipc_message({"value": "x" * IPC_MAX_BYTES})


async def test_existing_incompatible_daemon_is_not_replaced(monkeypatch):
    started = False

    async def old_status(method, args=None, timeout=12):
        assert method == "status"
        return {"version": "0.7.0", "protocol": IPC_PROTOCOL}

    def unexpected_start(*args, **kwargs):
        nonlocal started
        started = True

    monkeypatch.setattr("hoard.ipc.request", old_status)
    monkeypatch.setattr("hoard.ipc.subprocess.Popen", unexpected_start)

    with pytest.raises(DaemonCompatibilityError, match=r"hoard shutdown"):
        await ensure_daemon()
    assert started is False


async def test_current_daemon_version_and_protocol_are_accepted(monkeypatch):
    async def current_status(method, args=None, timeout=12):
        return {"version": __version__, "protocol": IPC_PROTOCOL}

    monkeypatch.setattr("hoard.ipc.request", current_status)
    await ensure_daemon()


@pytest.mark.skipif(os.name == "nt", reason="Unix sockets require Linux/macOS")
async def test_socket_returns_small_error_instead_of_oversized_response(tmp_path):
    class Stub:
        async def handle(self, method, args):
            return {"value": "x" * IPC_MAX_BYTES}

    server = await asyncio.start_unix_server(
        lambda r, w: serve_connection(Stub(), r, w),
        path=str(tmp_path / "large.sock"),
    )
    async with server:
        reader, writer = await asyncio.open_unix_connection(str(tmp_path / "large.sock"))
        writer.write(b'{"method":"large","args":{}}\n')
        await writer.drain()
        value = json.loads(await reader.readline())
        assert value["ok"] is False
        assert "слишком велик" in value["error"]
        writer.close()
        await writer.wait_closed()


@pytest.mark.skipif(os.name == "nt", reason="Unix sockets require Linux/macOS")
async def test_socket_rejects_oversized_request_with_explicit_error(tmp_path):
    class Stub:
        async def handle(self, method, args):
            raise AssertionError("oversized request must not be dispatched")

    server = await asyncio.start_unix_server(
        lambda r, w: serve_connection(Stub(), r, w),
        path=str(tmp_path / "oversized.sock"),
        limit=IPC_MAX_BYTES,
    )
    async with server:
        reader, writer = await asyncio.open_unix_connection(str(tmp_path / "oversized.sock"))
        writer.write(b"x" * (IPC_MAX_BYTES + 1) + b"\n")
        await writer.drain()
        value = json.loads(await reader.readline())
        assert value["ok"] is False
        assert "64 KiB" in value["error"]
        writer.close()
        await writer.wait_closed()
