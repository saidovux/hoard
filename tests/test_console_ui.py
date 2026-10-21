"""UI behavior without a TTY, user account, Telegram, or InquirerPy installation."""
from __future__ import annotations

import asyncio
import io
import webbrowser

from rich.console import Console

from hoard.console_ui import HoardCLI, fmt_bytes, progress_text, progress_view


class Prompt:
    def __init__(self, value):
        self.value = value

    def execute(self):
        return self.value


class FakePrompts:
    def __init__(self, *, selects=None, texts=None, confirms=None, secrets=None):
        self.selects = iter(selects or [])
        self.texts = iter(texts or [])
        self.confirms = iter(confirms or [])
        self.secrets = iter(secrets or [])
        self.menus = []

    def select(self, **kwargs):
        self.menus.append(kwargs)
        return Prompt(next(self.selects))

    def text(self, **kwargs):
        return Prompt(next(self.texts))

    def secret(self, **kwargs):
        return Prompt(next(self.secrets))

    def confirm(self, **kwargs):
        return Prompt(next(self.confirms))


def cli(prompts=None):
    output = io.StringIO()
    app = HoardCLI(
        console=Console(file=output, color_system=None, width=90), prompts=prompts
    )
    return app, output


def test_uses_true_telemetry_and_never_fake_values():
    data = {
        "running": True,
        "progress": {"source": "channel/42", "checked": 7, "downloaded": 2},
        "stats": {"downloads": 9, "sources": 1, "authors": 3},
    }
    assert "channel/42" in progress_text(data)
    assert "64%" not in progress_text(data)
    out = io.StringIO()
    Console(file=out, width=80).print(progress_view(data))
    assert "9" in out.getvalue()
    assert "2" in out.getvalue()


def test_progress_bar_uses_real_total():
    data = {"running": True, "progress": {"file": "clip.mp4", "bytes": 50, "total": 200},
            "stats": {}}
    out = io.StringIO()
    Console(file=out, width=80).print(progress_view(data))
    assert "25%" in out.getvalue()
    assert "clip.mp4" in out.getvalue()


def test_source_wizard_uses_ipc_without_starting_telegram():
    prompts = FakePrompts(
        texts=["https://t.me/examplechannel/123", "~/Videos/demo"],
        confirms=[True, True, True],
    )
    app, _ = cli(prompts)
    requests = []
    app.connect_telegram = lambda **kwargs: True

    def call(name, args=None, timeout=30):
        requests.append((name, args))
        if name == "source_validate":
            return {"title": "Example Channel"}
        return {"message": "ok"}

    app.call = call
    app.get_status = lambda: {"running": False}
    app.add_source()
    assert [item[0] for item in requests] == [
        "source_validate",
        "source_add",
        "start",
        "scan",
    ]
    assert requests[1][1]["url"] == "https://t.me/examplechannel/123"
    assert requests[1][1]["interval"] == 120
    assert requests[1][1]["title"] == "Example Channel"


def test_source_wizard_rejects_bad_link():
    prompts = FakePrompts(texts=["https://phishing.example.com/abc/123"])
    app, out = cli(prompts)
    app.call = lambda *args, **kwargs: (_ for _ in ()).throw(AssertionError("IPC not expected"))
    app.add_source()
    assert "публичная" in out.getvalue()


def test_exit_menu_does_not_issue_stop(monkeypatch):
    import hoard.console_ui as module

    async def fake_start():
        return None

    monkeypatch.setattr(module, "ensure_daemon", fake_start)
    prompts = FakePrompts(selects=["exit"])
    app, _ = cli(prompts)
    app.get_status = lambda: {
        "stats": {"sources": 1},
        "configured": True,
        "running": True,
    }
    app.call = lambda *args, **kwargs: (_ for _ in ()).throw(
        AssertionError("exit must not stop service")
    )
    app.run()
    assert prompts.menus[0]["choices"][0]["value"] == "sources"
    assert set(c["value"] for c in prompts.menus[0]["choices"]) == {
        "sources", "downloads", "catalog", "scan", "enrich", "control", "telegram", "exit"
    }


def test_select_supports_hjkl_and_h_returns_to_parent():
    prompts = FakePrompts(selects=["second"])
    app, _ = cli(prompts)
    choices = [
        {"name": "First", "value": "first"},
        {"name": "Second", "value": "second"},
        {"name": "Back", "value": "back"},
    ]

    assert app.select("Menu", choices) == "second"
    menu = prompts.menus[0]
    assert {binding["key"] for binding in menu["keybindings"]["down"]} >= {"j", "down"}
    assert {binding["key"] for binding in menu["keybindings"]["up"]} >= {"k", "up"}
    assert {binding["key"] for binding in menu["keybindings"]["answer"]} >= {"l", "enter"}
    assert {binding["key"] for binding in menu["keybindings"]["interrupt"]} >= {"h", "c-c"}

    class InterruptedPrompt:
        def execute(self):
            raise KeyboardInterrupt

    class InterruptedPrompts(FakePrompts):
        def select(self, **kwargs):
            return InterruptedPrompt()

    interrupted, _ = cli(InterruptedPrompts())
    assert interrupted.select("Menu", choices) == "back"


def test_cancel_stop_does_not_issue_ipc():
    prompts = FakePrompts(selects=["stop"], confirms=[False])
    app, _ = cli(prompts)
    app.call = lambda *args, **kwargs: (_ for _ in ()).throw(AssertionError("stop unconfirmed"))
    app.control_menu({"running": True, "collector_state": "running"})


def test_seamless_auth_walks_all_required_states():
    prompts = FakePrompts(
        texts=["12345", "+79990000000"],
        secrets=["api-hash", "11111", "2fa-password"],
    )
    app, _ = cli(prompts)
    replies = {
        "auth_begin": iter(
            [
                {"state": "api_required"},
                {"state": "phone_required"},
            ]
        ),
        "credentials": iter([{"message": "saved"}]),
        "auth_send_code": iter([{"state": "code_required"}]),
        "auth_submit_code": iter([{"state": "password_required"}]),
        "auth_submit_password": iter(
            [{"state": "authorized", "authorized": True}]
        ),
    }
    calls = []

    def call(name, args=None, timeout=30):
        calls.append((name, args))
        return next(replies[name])

    app.call = call
    assert app.connect_telegram() is True
    assert [name for name, _ in calls] == [
        "auth_begin",
        "credentials",
        "auth_begin",
        "auth_send_code",
        "auth_submit_code",
        "auth_submit_password",
    ]


def test_auth_cancel_is_sent_when_prompt_is_cancelled():
    prompts = FakePrompts()
    app, _ = cli(prompts)
    calls = []

    def call(name, args=None, timeout=30):
        calls.append(name)
        if name == "auth_begin":
            return {"state": "phone_required"}
        return {"state": "phone_required"}

    app.call = call
    app.text = lambda *args, **kwargs: (_ for _ in ()).throw(KeyboardInterrupt())
    assert app.connect_telegram() is False
    assert calls == ["auth_begin", "auth_cancel"]


def test_network_wait_shows_elapsed_time_and_vpn_hint(monkeypatch):
    import hoard.console_ui as module

    async def delayed_request(method, args, timeout):
        await asyncio.sleep(0.25)
        return {"state": "phone_required"}

    updates = []

    class Activity:
        def __init__(self, initial):
            updates.append(str(initial))

        def __enter__(self):
            return self

        def __exit__(self, *args):
            return False

        def update(self, value):
            updates.append(str(value))

    app, _ = cli()
    monkeypatch.setattr(module, "request", delayed_request)
    monkeypatch.setattr(
        app.console,
        "status",
        lambda initial, **kwargs: Activity(initial),
    )

    assert app.call("auth_begin") == {"state": "phone_required"}
    assert any("VPN" in item and "с" in item for item in updates)


def test_network_timeout_has_actionable_message(monkeypatch):
    app, output = cli()
    monkeypatch.setattr(
        app,
        "api_with_activity",
        lambda *args, **kwargs: (_ for _ in ()).throw(TimeoutError()),
    )

    assert app.call("auth_send_code") is None
    assert "интернет и VPN" in output.getvalue()


def test_control_menu_only_shows_actions_for_current_state():
    prompts = FakePrompts(selects=["back"])
    app, _ = cli(prompts)
    app.control_menu({"running": False, "collector_state": "stopped"})
    assert [choice["value"] for choice in prompts.menus[0]["choices"]] == [
        "start",
        "back",
    ]


def test_catalog_menu_opens_existing_single_file(tmp_path, monkeypatch):
    catalog = tmp_path / "telegram_archive.html"
    catalog.write_text("<!doctype html>", encoding="utf-8")
    prompts = FakePrompts(selects=["open"])
    app, output = cli(prompts)
    opened = []
    monkeypatch.setattr(webbrowser, "open", lambda url, new=0: opened.append((url, new)) or True)

    app.catalog_menu({"catalog": str(catalog)})

    assert opened == [(catalog.resolve().as_uri(), 2)]
    assert "открыт" in output.getvalue()


def test_main_menu_hides_collector_actions_without_sources(monkeypatch):
    import hoard.console_ui as module

    async def fake_start():
        return None

    monkeypatch.setattr(module, "ensure_daemon", fake_start)
    prompts = FakePrompts(selects=["exit"])
    app, _ = cli(prompts)
    app.get_status = lambda: {
        "stats": {"sources": 0},
        "telegram": "disconnected",
        "collector_state": "stopped",
        "running": False,
    }
    app.run()
    assert [item["value"] for item in prompts.menus[0]["choices"]] == [
        "sources",
        "downloads",
        "catalog",
        "telegram",
        "exit",
    ]


def test_byte_formatting():
    assert fmt_bytes(1024) == "1.0 KiB"
    assert fmt_bytes(None) == "—"
