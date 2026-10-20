import asyncio
import json

import pytest

from hoard import ipc, paths
from hoard.platform_support import read_endpoint, single_instance, write_endpoint
from hoard.service import serve_connection


def test_appdata_paths(monkeypatch, tmp_path):
    monkeypatch.setattr(paths, 'is_windows', lambda: True)
    monkeypatch.setenv('APPDATA', str(tmp_path / 'roaming'))
    monkeypatch.setenv('LOCALAPPDATA', str(tmp_path / 'local'))
    assert paths.config_dir() == tmp_path / 'roaming' / 'Hoard'
    assert paths.data_dir() == tmp_path / 'local' / 'Hoard' / 'data'


def test_lock(tmp_path):
    with single_instance(tmp_path / 'service.lock'):
        with pytest.raises(RuntimeError):
            with single_instance(tmp_path / 'service.lock'):
                pass


async def test_loopback_authenticated(monkeypatch, tmp_path):
    class Stub:
        calls = 0
        async def handle(self, method, args):
            self.calls += 1
            return {'method': method}
    stub = Stub()
    token = 's' * 44
    server = await asyncio.start_server(
        lambda r, w: serve_connection(stub, r, w, token), '127.0.0.1', 0
    )
    endpoint = tmp_path / 'endpoint.json'
    write_endpoint(endpoint, server.sockets[0].getsockname()[1], token)
    assert read_endpoint(endpoint)[1] == token
    monkeypatch.setattr(ipc, 'is_windows', lambda: True)
    monkeypatch.setattr(ipc, 'endpoint_path', lambda: endpoint)
    try:
        assert await ipc.request('ping') == {'method': 'ping'}
        reader, writer = await asyncio.open_connection(
            '127.0.0.1', server.sockets[0].getsockname()[1]
        )
        writer.write(b'{"method":"stop","token":"wrong","args":{}}\n')
        await writer.drain()
        assert json.loads(await reader.readline())['ok'] is False
        assert stub.calls == 1
        writer.close()
        await writer.wait_closed()
    finally:
        server.close()
        await server.wait_closed()


async def test_detached_daemon_start_and_shutdown(monkeypatch, tmp_path):
    for key, directory in {
        'XDG_STATE_HOME': 'state', 'XDG_DATA_HOME': 'data',
        'XDG_CONFIG_HOME': 'config', 'APPDATA': 'roaming',
        'LOCALAPPDATA': 'local',
    }.items():
        monkeypatch.setenv(key, str(tmp_path / directory))
    await ipc.ensure_daemon()
    assert (await ipc.request('status'))['running'] is False
    assert 'завершает' in (await ipc.request('shutdown'))['message']
    for _ in range(60):
        try:
            await ipc.request('status', timeout=0.25)
        except (OSError, RuntimeError, TimeoutError, ValueError):
            break
        await asyncio.sleep(0.1)
    else:
        pytest.fail('Daemon remains reachable after shutdown')
