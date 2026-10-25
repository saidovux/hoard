"""Security boundary for the explicit loopback-only browser client."""
from __future__ import annotations

import base64
import http.client
import json
import os
import secrets
import shutil
import subprocess
import threading
import time
from pathlib import Path

import pytest

from hoard import web as web_module
from hoard.catalog import build_catalog
from hoard.models import Post
from hoard.storage import Store
from hoard.web import (
    WebState,
    create_web_server,
    launch_web_process,
    run_web,
)


class FakeDaemon:
    def __init__(self):
        self.key = ""
        self.calls = []

    async def __call__(self, method, args, timeout):
        self.calls.append((method, dict(args), timeout))
        if method == "ai_config_get":
            return {
                "provider": "deepseek",
                "configured": bool(self.key),
                "key_status": "key_saved" if self.key else "not_configured",
            }
        if method == "ai_config_set":
            self.key = str(args["api_key"])
            return {"configured": True, "key_status": "key_saved", "message": "Ключ сохранён"}
        raise RuntimeError("unexpected method")


def request(server, method, path, *, headers=None, body=None):
    connection = http.client.HTTPConnection("127.0.0.1", server.server_address[1], timeout=5)
    connection.request(method, path, body=body, headers=headers or {})
    response = connection.getresponse()
    payload = response.read()
    result = response.status, dict(response.getheaders()), payload
    connection.close()
    return result


def test_web_rejects_old_daemon_reply_clearly(monkeypatch, tmp_path):
    async def daemon_ready():
        return None

    async def old_report(method, args=None, timeout=12):
        assert method == "telegram_report"
        return {}

    monkeypatch.setattr("hoard.web.state_dir", lambda: tmp_path / "state")
    monkeypatch.setattr("hoard.web.ensure_daemon", daemon_ready)
    monkeypatch.setattr("hoard.web.request", old_report)

    with pytest.raises(RuntimeError, match=r"hoard shutdown"):
        run_web(open_browser=False)
    state = json.loads((tmp_path / "state" / "web.ready.json").read_text())
    assert state["status"] == "error"
    assert "hoard shutdown" in state["error"]


class FakeWebProcess:
    def __init__(self, pid: int = 43210):
        self.pid = pid
        self.returncode: int | None = None
        self.terminated = False

    def poll(self) -> int | None:
        return self.returncode

    def terminate(self) -> None:
        self.terminated = True
        self.returncode = -15

    def wait(self, timeout: float | None = None) -> int | None:
        return self.returncode


def _prepare_launcher(monkeypatch, tmp_path):
    monkeypatch.setattr("hoard.web.state_dir", lambda: tmp_path / "state")
    monkeypatch.setattr("hoard.web._WEB_POLL_INTERVAL", 0.01)
    monkeypatch.setattr("hoard.web._web_reachable", lambda port: port > 0)
    monkeypatch.setattr("hoard.web.webbrowser.open", lambda *args, **kwargs: True)


def _popen_writing_state(monkeypatch, process, *, delay=0.1, status="ready", error=""):
    def fake_popen(*args, **kwargs):
        launch_id = str((kwargs.get("env") or {}).get("HOARD_WEB_LAUNCH_ID") or "")

        def publish():
            web_module._write_state(
                status,
                launch_id=launch_id,
                pid=process.pid,
                port=45678 if status == "ready" else 0,
                url="http://127.0.0.1:45678/start/synthetic"
                if status == "ready"
                else "",
                error=error,
            )

        threading.Timer(delay, publish).start()
        return process

    monkeypatch.setattr("hoard.web.subprocess.Popen", fake_popen)
    monkeypatch.setattr(
        "hoard.web._pid_alive", lambda pid: pid == process.pid
    )


def test_launch_web_child_ready_before_popen_returns_is_not_overwritten(
    monkeypatch, tmp_path
):
    _prepare_launcher(monkeypatch, tmp_path)
    process = FakeWebProcess(pid=45678)
    monkeypatch.setattr("hoard.web._pid_alive", lambda pid: pid == process.pid)

    def immediate_ready(*args, **kwargs):
        launch_id = str(
            (kwargs.get("env") or {}).get("HOARD_WEB_LAUNCH_ID") or ""
        )
        web_module._write_state(
            "ready",
            launch_id=launch_id,
            pid=process.pid,
            port=45678,
            url="http://127.0.0.1:45678/start/immediate",
        )
        return process

    monkeypatch.setattr("hoard.web.subprocess.Popen", immediate_ready)

    assert launch_web_process() == "http://127.0.0.1:45678/start/immediate"
    state = json.loads((tmp_path / "state" / "web.ready.json").read_text())
    assert state["status"] == "ready"
    assert state["pid"] == process.pid
    assert state["url"] == "http://127.0.0.1:45678/start/immediate"


def test_two_concurrent_launches_spawn_only_one_child(monkeypatch, tmp_path):
    _prepare_launcher(monkeypatch, tmp_path)
    process = FakeWebProcess(pid=56789)
    monkeypatch.setattr("hoard.web._pid_alive", lambda pid: pid == process.pid)
    popen_started = threading.Event()
    second_started = threading.Event()
    release_child = threading.Event()
    calls = 0
    guard = threading.Lock()

    def controlled_popen(*args, **kwargs):
        nonlocal calls
        with guard:
            calls += 1
        launch_id = str(
            (kwargs.get("env") or {}).get("HOARD_WEB_LAUNCH_ID") or ""
        )
        popen_started.set()
        release_child.wait(5)
        web_module._write_state(
            "ready",
            launch_id=launch_id,
            pid=process.pid,
            port=45678,
            url="http://127.0.0.1:45678/start/concurrent",
        )
        return process

    monkeypatch.setattr("hoard.web.subprocess.Popen", controlled_popen)
    results: list[str] = []
    errors: list[BaseException] = []

    def launch() -> None:
        try:
            results.append(launch_web_process())
        except BaseException as exc:
            errors.append(exc)

    first = threading.Thread(target=launch, daemon=True)
    first.start()
    assert popen_started.wait(5)

    def second_launch() -> None:
        second_started.set()
        launch()

    second = threading.Thread(target=second_launch, daemon=True)
    second.start()
    assert second_started.wait(5)
    release_child.set()

    first.join(5)
    second.join(5)
    assert first.is_alive() is False
    assert second.is_alive() is False
    assert errors == []
    assert calls == 1
    assert results == [
        "http://127.0.0.1:45678/start/concurrent",
        "http://127.0.0.1:45678/start/concurrent",
    ]


def test_corrupted_state_does_not_block_launch(monkeypatch, tmp_path):
    _prepare_launcher(monkeypatch, tmp_path)
    state_path = tmp_path / "state" / "web.ready.json"
    state_path.parent.mkdir(parents=True)
    state_path.write_text("{broken", encoding="utf-8")
    process = FakeWebProcess(pid=54321)
    _popen_writing_state(monkeypatch, process, delay=0.05)

    assert launch_web_process() == "http://127.0.0.1:45678/start/synthetic"
    assert json.loads(state_path.read_text())["pid"] == process.pid


def test_launcher_gives_child_manual_lifetime_and_launch_identity(
    monkeypatch, tmp_path
):
    _prepare_launcher(monkeypatch, tmp_path)
    process = FakeWebProcess(pid=54321)
    monkeypatch.setattr("hoard.web._pid_alive", lambda pid: pid == process.pid)
    captured: dict = {}

    def fake_popen(*args, **kwargs):
        captured.update(kwargs)
        launch_id = kwargs["env"]["HOARD_WEB_LAUNCH_ID"]
        web_module._write_state(
            "ready",
            launch_id=launch_id,
            pid=process.pid,
            port=45678,
            url="http://127.0.0.1:45678/start/manual",
        )
        return process

    monkeypatch.setattr("hoard.web.subprocess.Popen", fake_popen)

    assert launch_web_process() == "http://127.0.0.1:45678/start/manual"
    assert captured["env"]["HOARD_WEB_MANUAL"] == "1"
    assert len(captured["env"]["HOARD_WEB_LAUNCH_ID"]) == 32


def test_launch_web_waits_for_real_ready_state(monkeypatch, tmp_path):
    _prepare_launcher(monkeypatch, tmp_path)
    process = FakeWebProcess()
    _popen_writing_state(monkeypatch, process, delay=0.15)

    started = time.monotonic()
    url = launch_web_process()

    assert url == "http://127.0.0.1:45678/start/synthetic"
    assert time.monotonic() - started >= 0.1
    state = json.loads((tmp_path / "state" / "web.ready.json").read_text())
    assert state["status"] == "ready"
    assert state["pid"] == process.pid


def test_launch_web_reuses_ready_server_without_spawning(monkeypatch, tmp_path):
    _prepare_launcher(monkeypatch, tmp_path)
    web_module._write_state(
        "ready",
        pid=os.getpid(),
        port=45678,
        url="http://127.0.0.1:45678/start/existing",
    )

    def forbidden(*args, **kwargs):
        raise AssertionError("Popen must not run for a live ready server")

    monkeypatch.setattr("hoard.web.subprocess.Popen", forbidden)

    assert launch_web_process() == "http://127.0.0.1:45678/start/existing"


def test_launch_web_ignores_stale_ready_state(monkeypatch, tmp_path):
    _prepare_launcher(monkeypatch, tmp_path)
    web_module._write_state(
        "ready",
        pid=999999,
        port=45678,
        url="http://127.0.0.1:45678/start/stale",
    )
    monkeypatch.setattr("hoard.web._pid_alive", lambda pid: False)
    process = FakeWebProcess(pid=54321)
    _popen_writing_state(monkeypatch, process, delay=0.1)

    assert launch_web_process() == "http://127.0.0.1:45678/start/synthetic"
    state = json.loads((tmp_path / "state" / "web.ready.json").read_text())
    assert state["pid"] == process.pid


def test_launch_web_surfaces_child_error(monkeypatch, tmp_path):
    _prepare_launcher(monkeypatch, tmp_path)
    process = FakeWebProcess()
    _popen_writing_state(
        monkeypatch,
        process,
        delay=0.05,
        status="error",
        error="RuntimeError: synthetic preparation failure",
    )

    with pytest.raises(RuntimeError, match="synthetic preparation failure"):
        launch_web_process()


def test_launch_web_timeout_terminates_owned_child(monkeypatch, tmp_path):
    _prepare_launcher(monkeypatch, tmp_path)
    monkeypatch.setattr("hoard.web._WEB_LAUNCH_TIMEOUT", 0.1)
    process = FakeWebProcess()

    def fake_popen(*args, **kwargs):
        return process

    monkeypatch.setattr("hoard.web.subprocess.Popen", fake_popen)

    with pytest.raises(RuntimeError, match="не завершил подготовку"):
        launch_web_process()
    assert process.terminated is True


def test_web_requires_bootstrap_cookie_exact_host_origin_and_csrf(tmp_path):
    catalog = tmp_path / "telegram_archive.html"
    media = tmp_path / "avatar.png"
    media.write_bytes(b"synthetic-image")
    snapshot = {
        "posts": [],
        "videos": [],
        "profiles": [
            {
                "photos": [
                    {
                        "url": "avatar.png",
                        "available": True,
                        "size": media.stat().st_size,
                    }
                ]
            }
        ],
    }
    catalog.write_text(
        '<!doctype html><style data-hoard-bundle="style"></style>'
        '<script data-hoard-bundle="data">window.HOARD_CATALOG='
        + json.dumps(snapshot)
        + ";</script>"
        '<script data-hoard-bundle="app"></script>',
        encoding="utf-8",
    )
    daemon = FakeDaemon()
    state = WebState(Path(catalog), requester=daemon, lifetime=120)
    server = create_web_server(state)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    host = state.host_header
    try:
        status, _headers, _payload = request(server, "GET", "/", headers={"Host": "evil.test"})
        assert status == 421

        status, headers, _payload = request(
            server, "GET", f"/start/{state.bootstrap_token}", headers={"Host": host}
        )
        assert status == 303
        cookie = headers["Set-Cookie"].split(";", 1)[0]
        assert "HttpOnly" in headers["Set-Cookie"]
        assert "SameSite=Strict" in headers["Set-Cookie"]

        common = {"Host": host, "Cookie": cookie}
        status, headers, payload = request(server, "GET", "/", headers=common)
        assert status == 200
        assert "frame-ancestors 'none'" in headers["Content-Security-Policy"]
        text = payload.decode()
        assert state.csrf_token in text
        assert state.session_token not in text
        assert "API-ключ" not in text

        media_url = next(iter(state.media_files))
        assert media_url.startswith("/media/r0/")
        assert media_url in text
        status, _headers, payload = request(server, "GET", media_url, headers=common)
        assert status == 200
        assert payload == b"synthetic-image"

        body = json.dumps({"api_key": "web-secret"})
        write_headers = {**common, "Content-Type": "application/json"}
        status, _headers, _payload = request(
            server, "POST", "/api/config", headers=write_headers, body=body
        )
        assert status == 403
        status, _headers, _payload = request(
            server,
            "POST",
            "/api/config",
            headers={
                **write_headers,
                "Origin": "http://evil.test",
                "X-Hoard-CSRF": state.csrf_token,
            },
            body=body,
        )
        assert status == 403

        status, _headers, payload = request(
            server,
            "POST",
            "/api/config",
            headers={
                **write_headers,
                "Origin": state.origin,
                "X-Hoard-CSRF": state.csrf_token,
            },
            body=body,
        )
        assert status == 200
        assert b"web-secret" not in payload
        assert daemon.key == "web-secret"

        status, _headers, _payload = request(
            server, "GET", "/hoard_objects/../hoard.sqlite3", headers=common
        )
        assert status == 404
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


def _catalog_document(snapshot: dict) -> str:
    return (
        '<!doctype html><style data-hoard-bundle="style"></style>'
        '<script data-hoard-bundle="data">window.HOARD_CATALOG='
        + json.dumps(snapshot)
        + ";</script>"
        '<script data-hoard-bundle="app"></script>'
    )


def _login(server, state):
    status, headers, _ = request(
        server,
        "GET",
        f"/start/{state.bootstrap_token}",
        headers={"Host": state.host_header},
    )
    assert status == 303
    return {
        "Host": state.host_header,
        "Cookie": headers["Set-Cookie"].split(";", 1)[0],
    }


def test_each_open_gets_a_new_one_time_bootstrap_token(tmp_path):
    catalog = tmp_path / "telegram_archive.html"
    catalog.write_text(_catalog_document({"posts": [], "profiles": [], "videos": []}))
    state = WebState(catalog, requester=FakeDaemon())
    server = create_web_server(state)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        first_token = state.bootstrap_token
        common = _login(server, state)
        assert common

        status, _headers, _payload = request(
            server,
            "GET",
            f"/start/{first_token}",
            headers={"Host": state.host_header},
        )
        assert status == 404

        token_a = state.issue_bootstrap().rsplit("/", 1)[-1]
        token_b = state.issue_bootstrap().rsplit("/", 1)[-1]
        assert token_a != token_b
        assert token_a != first_token
        for token in (token_a, token_b):
            status, _headers, _payload = request(
                server, "GET", f"/start/{token}", headers={"Host": state.host_header}
            )
            assert status == 303
        status, _headers, _payload = request(
            server,
            "GET",
            f"/start/{token_a}",
            headers={"Host": state.host_header},
        )
        assert status == 404
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


def test_catalog_version_and_snapshot_deliver_only_published_data(tmp_path):
    root = tmp_path / "archive"
    root.mkdir()
    media_one = root / "one.jpg"
    media_two = root / "two.jpg"
    media_one.write_bytes(b"one")
    media_two.write_bytes(b"two")
    catalog = root / "telegram_archive.html"
    first = {
        "generated_at": "2026-10-25T00:00:00+00:00",
        "revision": 1,
        "posts": [
            {
                "source_id": 1,
                "post_id": 10,
                "channel_username": "sample",
                "channel_title": "Sample",
                "profile_ids": [],
                "video_ids": [],
                "media": [],
            }
        ],
        "profiles": [],
        "videos": [],
    }
    catalog.write_text(_catalog_document(first))
    state = WebState(catalog, requester=FakeDaemon())
    server = create_web_server(state)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        common = _login(server, state)
        status, _headers, payload = request(
            server, "GET", "/api/catalog/version", headers=common
        )
        assert status == 200
        first_token = json.loads(payload)["token"]
        status, _headers, payload = request(
            server, "GET", "/api/catalog/snapshot", headers=common
        )
        assert status == 200
        assert json.loads(payload)["catalog"]["revision"] == 1

        second = {
            **first,
            "generated_at": "2026-10-25T00:01:00+00:00",
            "revision": 2,
            "profiles": [
                {
                    "author_id": 7,
                    "display_name": "New",
                    "photos": [
                        {
                            "photo_id": "7",
                            "url": "two.jpg",
                            "available": True,
                            "size": media_two.stat().st_size,
                        }
                    ],
                }
            ],
        }
        temporary = root / ".next.html"
        temporary.write_text(_catalog_document(second))
        os.replace(temporary, catalog)

        status, _headers, payload = request(
            server, "GET", "/api/catalog/version", headers=common
        )
        assert status == 200
        second_token = json.loads(payload)["token"]
        assert second_token != first_token
        assert len(payload) < 1000

        status, _headers, payload = request(
            server, "GET", "/api/catalog/snapshot", headers=common
        )
        assert status == 200
        result = json.loads(payload)
        assert result["token"] == second_token
        assert result["catalog"]["revision"] == 2
        assert result["catalog"]["profiles"][0]["author_id"] == 7
        media_url = result["catalog"]["profiles"][0]["photos"][0]["url"]
        assert media_url.startswith("/media/r0/")
        status, _headers, payload = request(server, "GET", media_url, headers=common)
        assert status == 200
        assert payload == b"two"

        # A broken in-progress publication must not replace the last good view.
        catalog.write_text("<!doctype html><p>broken</p>")
        status, _headers, payload = request(
            server, "GET", "/api/catalog/snapshot", headers=common
        )
        assert status == 503
        assert state.catalog_payload["revision"] == 2
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


def test_loopback_control_issues_urls_and_stops_without_browser_route(tmp_path):
    catalog = tmp_path / "telegram_archive.html"
    catalog.write_text(_catalog_document({"posts": [], "profiles": [], "videos": []}))
    state = WebState(catalog, requester=FakeDaemon())
    state.control_token = secrets.token_urlsafe(32)
    control = web_module.HoardControlServer(("127.0.0.1", 0), state)
    thread = threading.Thread(target=control.serve_forever, daemon=True)
    thread.start()
    try:
        value = {
            "control_port": state.control_port,
            "control_token": state.control_token,
        }
        result = web_module._control_request(value, "open")
        assert result["url"].startswith(state.origin + "/start/")
        status, _headers, _payload = request(
            control,
            "POST",
            "/open",
            headers={"X-Hoard-Control": "wrong"},
        )
        assert status == 403
        web_module._control_request(value, "stop")
        assert state.stop_event.is_set()
    finally:
        control.shutdown()
        control.server_close()
        thread.join(timeout=5)


def test_web_serves_catalog_media_from_each_explicit_allowed_root(tmp_path):
    catalog_root = tmp_path / "catalog"
    media_root = tmp_path / "source-media"
    catalog_root.mkdir()
    media_root.mkdir()
    catalog = catalog_root / "telegram_archive.html"
    media = media_root / "avatar.png"
    media.write_bytes(b"second-root-image")
    snapshot = {
        "posts": [],
        "videos": [],
        "profiles": [
            {
                "photos": [
                    {
                        "url": "../source-media/avatar.png",
                        "available": True,
                        "size": media.stat().st_size,
                    }
                ]
            }
        ],
    }
    catalog.write_text(
        '<!doctype html><style data-hoard-bundle="style"></style>'
        '<script data-hoard-bundle="data">window.HOARD_CATALOG='
        + json.dumps(snapshot)
        + ";</script>"
        '<script data-hoard-bundle="app"></script>',
        encoding="utf-8",
    )
    state = WebState(
        catalog,
        allowed_media_roots=(catalog_root, media_root),
        requester=FakeDaemon(),
    )
    server = create_web_server(state)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        status, headers, _payload = request(
            server,
            "GET",
            f"/start/{state.bootstrap_token}",
            headers={"Host": state.host_header},
        )
        assert status == 303
        common = {
            "Host": state.host_header,
            "Cookie": headers["Set-Cookie"].split(";", 1)[0],
        }
        media_url = next(iter(state.media_files))
        assert media_url == "/media/r1/avatar.png"
        status, _headers, payload = request(server, "GET", media_url, headers=common)
        assert status == 200
        assert payload == b"second-root-image"
        status, _headers, _payload = request(
            server, "GET", "/media/r1/%2e%2e/secret", headers=common
        )
        assert status == 404
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


@pytest.mark.skipif(shutil.which("chromium") is None, reason="Chromium is not installed")
def test_catalog_poll_updates_open_browser_without_reload(tmp_path, monkeypatch):
    root = tmp_path / "archive"
    root.mkdir()
    one = root / "one.jpg"
    two = root / "two.jpg"
    one.write_bytes(b"synthetic-one")
    two.write_bytes(b"synthetic-two")
    store = Store(tmp_path / "db.sqlite")
    store.add_source(Post("synthetic_channel", 1), str(root))
    store.telegram.observe_profile(
        author_id=1, username="user1", display_name="Old Name", source_id=1
    )
    store.telegram.record_profile_photo(
        1, "1", file_path=str(one), size=one.stat().st_size, photo_date="2026-10-25"
    )
    catalog = build_catalog(store)
    store.close()
    state = WebState(catalog, requester=FakeDaemon())
    server = create_web_server(state)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    original_version = state.published_version
    swapped = False
    update_trigger = threading.Event()
    update_done = threading.Event()

    def publish_new_catalog():
        try:
            update_trigger.wait(10)
            concurrent = Store(tmp_path / "db.sqlite")
            try:
                concurrent.telegram.observe_profile(
                    author_id=1, username="user1", display_name="New Name", source_id=1
                )
                concurrent.telegram.observe_profile(
                    author_id=2, username="user2", display_name="Second", source_id=1
                )
                concurrent.telegram.record_profile_photo(
                    2, "2", file_path=str(two), size=two.stat().st_size, photo_date="2026-10-25"
                )
                build_catalog(concurrent)
            finally:
                concurrent.close()
        finally:
            update_done.set()

    updater = threading.Thread(target=publish_new_catalog, daemon=True)
    updater.start()

    def updating_version():
        nonlocal swapped
        if not swapped:
            swapped = True
            update_trigger.set()
            assert update_done.wait(10)
        return original_version()

    monkeypatch.setattr(state, "published_version", updating_version)
    profile = tmp_path / "chromium-refresh"
    command = [
        str(shutil.which("chromium")),
        "--headless=new",
        "--no-sandbox",
        "--disable-gpu",
        "--disable-background-networking",
        f"--user-data-dir={profile}",
        "--virtual-time-budget=12000",
        "--dump-dom",
    ]
    try:
        result = subprocess.run(
            [*command, state.start_url],
            capture_output=True,
            text=True,
            timeout=40,
            check=False,
        )
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)
        update_trigger.set()
        updater.join(timeout=5)

    assert result.returncode == 0, result.stderr
    assert "New Name" in result.stdout
    assert "Second" in result.stdout
    assert "2 профилей" in result.stdout


@pytest.mark.skipif(shutil.which("chromium") is None, reason="Chromium is not installed")
def test_interactive_web_opens_photo_profiles_and_profile_gallery_in_chromium(tmp_path):
    archive = tmp_path / "archive"
    archive.mkdir()
    store = Store(tmp_path / "hoard.sqlite3")
    source_id = store.add_source(Post("synthetic_channel", 42), str(archive))
    store.telegram.observe_profile(
        author_id=12345,
        username="photo_user",
        display_name="Photo User",
        source_id=source_id,
    )
    image = base64.b64decode(
        "iVBORw0KGgoAAAANSUhEUgAAAAIAAAACCAIAAAD91JpzAAAAFElEQVR42mNk"
        "+M9QzwAEYBxVSFUAANMCA/2BTpYAAAAASUVORK5CYII="
    )
    for index in (1, 2):
        path = archive / "profile_photos" / f"photo-{index}.png"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(image)
        store.telegram.record_profile_photo(
            12345,
            f"photo-{index}",
            file_path=str(path),
            size=len(image),
            photo_date=f"2026-10-2{index}",
        )
    catalog = build_catalog(store, archive_root=archive)
    roots = tuple(store.telegram.allowed_media_roots(archive))
    store.close()

    state = WebState(catalog, allowed_media_roots=roots, requester=FakeDaemon())
    server = create_web_server(state)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    profile = tmp_path / "chromium-profile"
    command = [
        str(shutil.which("chromium")),
        "--headless=new",
        "--no-sandbox",
        "--disable-gpu",
        "--disable-background-networking",
        f"--user-data-dir={profile}",
        "--dump-dom",
    ]
    try:
        listing = subprocess.run(
            [*command, state.start_url],
            capture_output=True,
            text=True,
            timeout=30,
            check=False,
        )
        assert listing.returncode == 0, listing.stderr
        assert '<h1 id="title">Все профили</h1>' in listing.stdout
        assert 'class="avatar"' in listing.stdout
        assert 'src="/media/' in listing.stdout
        assert 'data-loaded="true"' in listing.stdout
        assert "data-load-error" not in listing.stdout
        assert "Photo User" in listing.stdout

        detail = subprocess.run(
            [*command, f"{state.origin}/#profile/12345/profiles"],
            capture_output=True,
            text=True,
            timeout=30,
            check=False,
        )
        assert detail.returncode == 0, detail.stderr
        assert ">Фотографии</h2>" in detail.stdout
        assert detail.stdout.count('alt="Фото профиля"') == 2
        assert detail.stdout.count('src="/media/') >= 3  # Avatar and two gallery photos.
        assert detail.stdout.count('data-loaded="true"') >= 3
        assert "data-load-error" not in detail.stdout
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)
