import os
import pytest
import asyncio
import json

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
