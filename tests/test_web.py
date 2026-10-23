"""Security boundary for the explicit loopback-only browser client."""
from __future__ import annotations

import http.client
import json
import threading
from pathlib import Path

import pytest

from hoard.web import WebState, create_web_server, run_web


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


def test_web_rejects_old_daemon_reply_clearly(monkeypatch):
    async def daemon_ready():
        return None

    async def old_report(method, args=None, timeout=12):
        assert method == "telegram_report"
        return {}

    monkeypatch.setattr("hoard.web.ensure_daemon", daemon_ready)
    monkeypatch.setattr("hoard.web.request", old_report)

    with pytest.raises(RuntimeError, match=r"hoard shutdown"):
        run_web(open_browser=False)


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
