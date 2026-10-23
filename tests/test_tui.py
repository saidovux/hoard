"""Headless coverage for the full-screen Textual client."""

from __future__ import annotations

import asyncio
import subprocess
import sys
from typing import TYPE_CHECKING, Any

import pytest

if TYPE_CHECKING:
    from textual.widgets import DataTable, Input, OptionList, SelectionList, Static

    from hoard.tui.ai_dialogs import AIRecordDialog, TelegramAIDialog, TelegramAnalysisDialog
    from hoard.tui.app import HelpScreen, HoardApp
    from hoard.tui.dialogs import AuthDialog, ConfirmDialog, SourceDialog
    from hoard.tui.screens import (
        ActivityScreen,
        AIWorkspaceScreen,
        ArchiveScreen,
        OverviewScreen,
        SourcesScreen,
        TelegramScreen,
    )
    from hoard.tui.widgets import HoardHeader, HoardStatusBar, ProgressView, Sidebar


@pytest.fixture(scope="module", autouse=True)
def load_textual_after_existing_async_tests():
    """Keep Textual initialization local to its own headless test module."""
    from textual.widgets import DataTable as DataTableClass
    from textual.widgets import Input as InputClass
    from textual.widgets import OptionList as OptionListClass
    from textual.widgets import SelectionList as SelectionListClass
    from textual.widgets import Static as StaticClass

    from hoard.tui.ai_dialogs import AIRecordDialog as AIRecordDialogClass
    from hoard.tui.ai_dialogs import TelegramAIDialog as TelegramAIDialogClass
    from hoard.tui.ai_dialogs import TelegramAnalysisDialog as TelegramAnalysisDialogClass
    from hoard.tui.app import HelpScreen as HelpScreenClass
    from hoard.tui.app import HoardApp as HoardAppClass
    from hoard.tui.dialogs import AuthDialog as AuthDialogClass
    from hoard.tui.dialogs import ConfirmDialog as ConfirmDialogClass
    from hoard.tui.dialogs import SourceDialog as SourceDialogClass
    from hoard.tui.screens import ActivityScreen as ActivityScreenClass
    from hoard.tui.screens import AIWorkspaceScreen as AIWorkspaceScreenClass
    from hoard.tui.screens import ArchiveScreen as ArchiveScreenClass
    from hoard.tui.screens import OverviewScreen as OverviewScreenClass
    from hoard.tui.screens import SourcesScreen as SourcesScreenClass
    from hoard.tui.screens import TelegramScreen as TelegramScreenClass
    from hoard.tui.widgets import HoardHeader as HoardHeaderClass
    from hoard.tui.widgets import HoardStatusBar as HoardStatusBarClass
    from hoard.tui.widgets import ProgressView as ProgressViewClass
    from hoard.tui.widgets import Sidebar as SidebarClass

    globals().update(
        ActivityScreen=ActivityScreenClass,
        AIRecordDialog=AIRecordDialogClass,
        AIWorkspaceScreen=AIWorkspaceScreenClass,
        ArchiveScreen=ArchiveScreenClass,
        AuthDialog=AuthDialogClass,
        ConfirmDialog=ConfirmDialogClass,
        DataTable=DataTableClass,
        HelpScreen=HelpScreenClass,
        HoardApp=HoardAppClass,
        HoardHeader=HoardHeaderClass,
        HoardStatusBar=HoardStatusBarClass,
        Input=InputClass,
        OptionList=OptionListClass,
        SelectionList=SelectionListClass,
        OverviewScreen=OverviewScreenClass,
        ProgressView=ProgressViewClass,
        Sidebar=SidebarClass,
        SourceDialog=SourceDialogClass,
        SourcesScreen=SourcesScreenClass,
        Static=StaticClass,
        TelegramScreen=TelegramScreenClass,
        TelegramAIDialog=TelegramAIDialogClass,
        TelegramAnalysisDialog=TelegramAnalysisDialogClass,
    )


def status_fixture(*, total: int | None = 4096) -> dict[str, Any]:
    progress: dict[str, Any] = {
        "source": "sample_channel/42",
        "checked": 17,
        "downloaded": 3,
        "file": "circle.mp4",
        "bytes": 1024,
        "speed_bps": 512,
    }
    if total is not None:
        progress["total"] = total
    return {
        "running": True,
        "collector_state": "running",
        "telegram": "authorized",
        "stats": {"sources": 2, "authors": 14, "downloads": 31},
        "queue": {"queued": 4, "running": 1, "failed": 2},
        "progress": progress,
        "events": [
            {"kind": "ok", "created_at": "2026-10-21 10:11:12", "text": "Сохранён кружок"},
            {"kind": "error", "created_at": "2026-10-21 10:12:13", "text": "Нет связи"},
        ],
        "sources": [
            {
                "id": 1,
                "username": "sample_channel",
                "post_id": 42,
                "url": "https://t.me/sample_channel/42",
                "title": "Sample",
                "output": "/tmp/archive",
                "interval": 120,
                "enabled": 1,
                "download_count": 31,
                "last_checked_at": "2026-10-21 10:12:13",
                "last_result": "Проверено: 17",
                "last_error": "",
            }
        ],
        "downloads": [],
        "profiles": [],
        "jobs": [],
        "archive_root": "/tmp/archive",
        "catalog": "/tmp/archive/telegram_archive.html",
    }


class FakeAPI:
    def __init__(self, status: dict | None = None, *, error: Exception | None = None) -> None:
        self.result = status if status is not None else status_fixture()
        self.error = error
        self.ensure_calls = 0
        self.status_calls = 0
        self.active_calls = 0
        self.max_active_calls = 0
        self.delay = 0.0
        self.operation_delay = 0.0
        self.calls: list[tuple[str, dict[str, Any]]] = []
        self.responses: dict[str, list[dict[str, Any] | Exception]] = {}

    def queue(self, name: str, *responses: dict[str, Any] | Exception) -> None:
        self.responses[name] = list(responses)

    async def _operation(
        self, name: str, args: dict[str, Any] | None = None, default: dict | None = None
    ) -> dict:
        self.calls.append((name, args or {}))
        if self.operation_delay:
            await asyncio.sleep(self.operation_delay)
        queued = self.responses.get(name, [])
        value = queued.pop(0) if queued else (default or {"message": "Готово"})
        if isinstance(value, Exception):
            raise value
        if value.get("authorized") or value.get("state") == "authorized":
            self.result["telegram"] = "authorized"
        return value

    async def ensure_started(self) -> None:
        self.ensure_calls += 1

    async def status(self) -> dict:
        self.status_calls += 1
        self.active_calls += 1
        self.max_active_calls = max(self.max_active_calls, self.active_calls)
        try:
            if self.delay:
                await asyncio.sleep(self.delay)
            if self.error:
                raise self.error
            return self.result
        finally:
            self.active_calls -= 1

    async def auth_begin(self) -> dict:
        return await self._operation("auth_begin", default={"state": self.result["telegram"]})

    async def save_credentials(self, api_id: int, api_hash: str) -> dict:
        return await self._operation("save_credentials", {"api_id": api_id, "api_hash": "<secret>"})

    async def auth_send_code(self, phone: str, *, force: bool = False) -> dict:
        return await self._operation("auth_send_code", {"phone": phone, "force": force})

    async def auth_submit_code(self, code: str) -> dict:
        return await self._operation("auth_submit_code", {"code": "<secret>"})

    async def auth_submit_password(self, password: str) -> dict:
        return await self._operation("auth_submit_password", {"password": "<secret>"})

    async def auth_cancel(self) -> dict:
        return await self._operation("auth_cancel", default={"state": "phone_required"})

    async def validate_source(self, url: str) -> dict:
        return await self._operation(
            "validate_source",
            {"url": url},
            {
                "url": "https://t.me/sample_channel/42",
                "username": "sample_channel",
                "post_id": 42,
                "title": "Sample",
            },
        )

    async def add_source(self, url: str, output: str, interval: int, *, title: str = "") -> dict:
        return await self._operation(
            "add_source",
            {"url": url, "output": output, "interval": interval, "title": title},
            {"message": "Источник сохранён"},
        )

    async def set_source_enabled(self, source_id: int, enabled: bool) -> dict:
        for source in self.result.get("sources", []):
            if source["id"] == source_id:
                source["enabled"] = int(enabled)
        return await self._operation("set_source_enabled", {"id": source_id, "enabled": enabled})

    async def scan_source(self, source_id: int) -> dict:
        return await self._operation("scan_source", {"id": source_id})

    async def scan_all(self) -> dict:
        return await self._operation("scan_all")

    async def start_collector(self) -> dict:
        self.result["running"] = True
        self.result["collector_state"] = "running"
        return await self._operation("start_collector", default={"message": "Запущен"})

    async def pause_collector(self) -> dict:
        self.result["running"] = False
        self.result["collector_state"] = "paused"
        return await self._operation("pause_collector", default={"message": "Пауза"})

    async def stop_collector(self) -> dict:
        self.result["running"] = False
        self.result["collector_state"] = "stopped"
        return await self._operation("stop_collector", default={"message": "Остановлен"})

    async def rebuild_catalog(self) -> dict:
        return await self._operation(
            "rebuild_catalog", default={"message": "HTML-каталог обновлён"}
        )

    async def enrich_telegram(self) -> dict:
        return await self._operation("enrich_telegram")

    async def retry_job(self, job_id: int) -> dict:
        return await self._operation("retry_job", {"id": job_id})

    async def ai_call(self, method: str, args: dict[str, Any] | None = None) -> dict:
        defaults = {
            "ai_prompt_list": {"items": [], "has_more": False},
            "ai_chat_list": {"items": [], "has_more": False},
            "ai_memory_list": {"items": [], "has_more": False},
            "ai_price_list": {"items": [], "has_more": False},
            "ai_attempt_list": {"items": [], "has_more": False},
            "ai_statistics": {"attempts": 0, "unknown_cost": 0, "estimated_costs": {}},
            "ai_config_get": {
                "provider": "deepseek",
                "model": "deepseek-chat",
                "timeout": 120,
                "configured": False,
            },
        }
        return await self._operation(method, args, defaults.get(method, {"message": "Готово"}))


async def wait_for_startup(app: HoardApp, pilot) -> None:
    for _ in range(30):
        await pilot.pause(0.01)
        if app._startup_complete and (app.status is not None or app.status_error):
            return
    raise AssertionError("TUI startup did not complete")


async def wait_for_condition(pilot, predicate, message="condition was not reached") -> None:
    for _ in range(100):
        await pilot.pause(0.01)
        if predicate():
            return
    raise AssertionError(message)


@pytest.mark.asyncio
async def test_initializes_overview_from_real_status_contract():
    api = FakeAPI()
    app = HoardApp(api)
    async with app.run_test(size=(80, 24)) as pilot:
        await wait_for_startup(app, pilot)
        assert isinstance(app.screen, OverviewScreen)
        assert api.ensure_calls == 1
        assert api.status_calls == 1
        summary = str(app.screen.query_one("#system-summary", Static).content)
        statistics = str(app.screen.query_one("#statistics", Static).content)
        events = str(app.screen.query_one("#events", Static).content)
        assert "подключён" in summary
        assert "работает" in summary
        assert all(number in statistics for number in ("2", "14", "31", "4", "1"))
        assert "Сохранён кружок" in events
        assert "Нет связи" in events


@pytest.mark.asyncio
async def test_theme_computed_backgrounds_and_option_states_are_black():
    app = HoardApp(FakeAPI())
    async with app.run_test(size=(80, 24)) as pilot:
        await wait_for_startup(app, pilot)
        nodes = [app, app.screen, *app.screen.query("*")]
        for node in nodes:
            assert node.styles.background.hex == "#000000", repr(node)

        navigation = app.screen.query_one(OptionList)
        for component in navigation.COMPONENT_CLASSES:
            styles = navigation.get_component_styles(component)
            assert styles.background.hex == "#000000", component
        assert (
            navigation.get_component_styles("option-list--option-highlighted").color.hex
            == "#F79646"
        )

        await pilot.press("?")
        await pilot.pause()
        assert isinstance(app.screen, HelpScreen)
        assert app.screen.styles.background.hex == "#000000"
        assert app.screen.query_one("#help-card").styles.background.hex == "#000000"


@pytest.mark.asyncio
async def test_round_three_screens_tables_buttons_and_dialogs_remain_strict_black():
    status = status_fixture()
    status["telegram"] = "phone_required"
    api = FakeAPI(status)
    api.queue("auth_begin", {"state": "phone_required"})
    app = HoardApp(api)
    async with app.run_test(size=(100, 30)) as pilot:
        await wait_for_startup(app, pilot)
        for section in ("telegram", "sources", "archive"):
            app.open_section(section)
            await pilot.pause()
            for node in [app.screen, *app.screen.query("*")]:
                assert node.styles.background.hex == "#000000", repr(node)
            for table in app.screen.query(DataTable):
                for component in table.COMPONENT_CLASSES:
                    assert table.get_component_styles(component).background.hex == "#000000"

        app.open_auth()
        await wait_for_condition(
            pilot, lambda: getattr(app.screen, "state", None) == "phone_required"
        )
        for node in [app.screen, *app.screen.query("*")]:
            assert node.styles.background.hex == "#000000", repr(node)
        await pilot.press("escape")


@pytest.mark.asyncio
@pytest.mark.parametrize("size", [(40, 16), (60, 20), (80, 24), (120, 35)])
async def test_supported_terminal_sizes_have_valid_layout(size):
    app = HoardApp(FakeAPI())
    async with app.run_test(size=size) as pilot:
        await wait_for_startup(app, pilot)
        await pilot.pause()
        workspace = app.screen.query_one("#workspace")
        assert workspace.size.width > 0
        assert workspace.size.height > 0
        assert app.screen.query_one(HoardHeader).size.height > 0
        assert app.screen.query_one(HoardStatusBar).size.height > 0
        assert app.screen.query_one(Sidebar).display is (size[0] >= 60)


@pytest.mark.asyncio
async def test_live_resize_updates_compact_layout_and_sidebar():
    app = HoardApp(FakeAPI())
    async with app.run_test(size=(80, 24)) as pilot:
        await wait_for_startup(app, pilot)
        await pilot.resize_terminal(70, 20)
        assert app.has_class("compact")
        assert app.screen.query_one(Sidebar).display
        await pilot.resize_terminal(40, 16)
        assert app.has_class("sidebar-hidden")
        await pilot.resize_terminal(120, 35)
        assert not app.has_class("compact")
        assert not app.has_class("sidebar-hidden")


@pytest.mark.asyncio
async def test_keyboard_navigation_switches_and_goes_back_without_nested_screens():
    app = HoardApp(FakeAPI())
    async with app.run_test(size=(80, 24)) as pilot:
        await wait_for_startup(app, pilot)
        base_depth = len(app.screen_stack)
        await pilot.press("j", "j", "j", "enter")
        await pilot.pause()
        assert isinstance(app.screen, ActivityScreen)
        assert len(app.screen_stack) == base_depth
        await pilot.press("h")
        await pilot.pause()
        assert isinstance(app.screen, OverviewScreen)
        assert len(app.screen_stack) == base_depth
        await pilot.press("down", "down", "down", "right")
        await pilot.pause()
        assert isinstance(app.screen, ActivityScreen)
        await pilot.press("escape")
        await pilot.pause()
        assert isinstance(app.screen, OverviewScreen)
        await pilot.press("down", "right", "up", "right")
        await pilot.pause()
        assert isinstance(app.screen, OverviewScreen)


@pytest.mark.asyncio
async def test_letter_bindings_do_not_capture_text_input():
    app = HoardApp(FakeAPI())
    async with app.run_test() as pilot:
        await wait_for_startup(app, pilot)
        input_widget = Input()
        await app.screen.query_one("#workspace").mount(input_widget)
        input_widget.focus()
        await pilot.press("j", "k", "h", "l", "b", "q", "?")
        await pilot.pause()
        assert input_widget.value == "jkhlbq?"
        assert app.is_running


@pytest.mark.asyncio
async def test_q_exits_without_daemon_control_calls():
    api = FakeAPI()
    app = HoardApp(api)
    async with app.run_test() as pilot:
        await wait_for_startup(app, pilot)
        await pilot.press("q")
    assert api.ensure_calls == 1
    assert api.status_calls == 1


@pytest.mark.asyncio
async def test_exit_stops_activity_timer():
    app = HoardApp(FakeAPI())
    async with app.run_test() as pilot:
        await wait_for_startup(app, pilot)
        app.open_section("activity")
        await pilot.pause()
        activity = app.screen
        assert isinstance(activity, ActivityScreen)
        assert activity.poll_timer is not None
        await pilot.press("q")
    assert activity.poll_timer is None


@pytest.mark.asyncio
async def test_empty_status_has_informative_states():
    empty = {
        "running": False,
        "collector_state": "stopped",
        "telegram": None,
        "stats": {},
        "queue": {},
        "progress": {},
        "events": [],
    }
    app = HoardApp(FakeAPI(empty))
    async with app.run_test() as pilot:
        await wait_for_startup(app, pilot)
        assert "данные недоступны" in str(app.screen.query_one("#system-summary", Static).content)
        assert "Событий пока нет" in str(app.screen.query_one("#events", Static).content)


@pytest.mark.asyncio
async def test_activity_uses_only_available_progress_values():
    app = HoardApp(FakeAPI(status_fixture(total=None)))
    async with app.run_test() as pilot:
        await wait_for_startup(app, pilot)
        app.open_section("activity")
        await pilot.pause()
        progress = str(app.screen.query_one(ProgressView).content)
        assert "1.0 KiB" in progress
        assert "512 B/s" in progress
        assert "%" not in progress
        assert "ETA" not in progress


@pytest.mark.asyncio
async def test_ipc_error_is_visible_and_ui_can_close():
    app = HoardApp(FakeAPI(error=ConnectionError("socket недоступен")))
    async with app.run_test() as pilot:
        await wait_for_startup(app, pilot)
        error = app.screen.query_one("#status-error", Static)
        assert error.display
        assert "socket недоступен" in str(error.content)
        await pilot.press("q")


@pytest.mark.asyncio
async def test_polling_only_runs_on_activity_and_never_overlaps(monkeypatch):
    monkeypatch.setattr(ActivityScreen, "POLL_INTERVAL", 0.02)
    api = FakeAPI()
    api.delay = 0.06
    app = HoardApp(api)
    async with app.run_test() as pilot:
        await wait_for_startup(app, pilot)
        overview_calls = api.status_calls
        await pilot.pause(0.08)
        assert api.status_calls == overview_calls

        app.open_section("activity")
        await pilot.pause(0.16)
        assert api.status_calls > overview_calls
        assert api.max_active_calls == 1

        activity = app.screen
        assert isinstance(activity, ActivityScreen)
        await pilot.press("h")
        await pilot.pause(0.08)
        assert activity.poll_timer is None
        stopped_calls = api.status_calls
        await pilot.pause(0.08)
        assert api.status_calls == stopped_calls


@pytest.mark.asyncio
async def test_authorization_walks_daemon_states_through_2fa_without_storing_secrets():
    status = status_fixture()
    status["telegram"] = "api_required"
    api = FakeAPI(status)
    api.queue("auth_begin", {"state": "api_required"}, {"state": "phone_required"})
    api.queue("auth_send_code", {"state": "code_required", "message": "Код отправлен"})
    api.queue(
        "auth_submit_code",
        {"state": "password_required", "message": "Нужен пароль"},
    )
    api.queue(
        "auth_submit_password",
        {
            "state": "password_required",
            "error": "Неверный пароль 2FA",
            "error_code": "password_invalid",
            "retryable": True,
        },
        {"state": "authorized", "authorized": True, "message": "Подключено"},
    )
    app = HoardApp(api)
    async with app.run_test(size=(80, 24)) as pilot:
        await wait_for_startup(app, pilot)
        app.open_auth()
        await wait_for_condition(
            pilot, lambda: getattr(app.screen, "state", None) == "api_required"
        )
        dialog = app.screen
        assert isinstance(dialog, AuthDialog)
        dialog.query_one("#api-id", Input).value = "12345"
        dialog.query_one("#api-hash", Input).value = "super-secret-hash"
        dialog.query_one("#auth-primary").press()
        await wait_for_condition(
            pilot, lambda: getattr(app.screen, "state", None) == "phone_required"
        )
        dialog.query_one("#phone", Input).value = "+79990000000"
        dialog.query_one("#auth-primary").press()
        await wait_for_condition(
            pilot, lambda: getattr(app.screen, "state", None) == "code_required"
        )
        dialog.query_one("#code", Input).value = "54321"
        dialog.query_one("#auth-primary").press()
        await wait_for_condition(
            pilot, lambda: getattr(app.screen, "state", None) == "password_required"
        )
        dialog.query_one("#password", Input).value = "secret-2fa"
        dialog.query_one("#auth-primary").press()
        await wait_for_condition(
            pilot,
            lambda: "Неверный пароль" in str(dialog.query_one("#auth-error", Static).content),
        )
        dialog.query_one("#password", Input).value = "correct-2fa"
        dialog.query_one("#auth-primary").press()
        await wait_for_condition(pilot, lambda: not isinstance(app.screen, AuthDialog))

    names = [name for name, _ in api.calls]
    assert names == [
        "auth_begin",
        "save_credentials",
        "auth_begin",
        "auth_send_code",
        "auth_submit_code",
        "auth_submit_password",
        "auth_submit_password",
    ]
    serialized = repr(api.calls)
    assert "super-secret-hash" not in serialized
    assert "54321" not in serialized
    assert "secret-2fa" not in serialized
    assert "correct-2fa" not in serialized


@pytest.mark.asyncio
async def test_authorization_reuses_session_and_handles_code_errors_flood_and_network():
    status = status_fixture()
    api = FakeAPI(status)
    api.queue("auth_begin", {"state": "authorized", "authorized": True})
    app = HoardApp(api)
    async with app.run_test() as pilot:
        await wait_for_startup(app, pilot)
        app.open_auth()
        await wait_for_condition(pilot, lambda: not isinstance(app.screen, AuthDialog))
        assert [name for name, _ in api.calls].count("auth_begin") == 1

        api.result["telegram"] = "code_required"
        api.queue("auth_begin", {"state": "code_required"})
        api.queue(
            "auth_submit_code",
            {
                "state": "code_required",
                "error": "Неверный код",
                "error_code": "code_invalid",
                "retryable": True,
            },
            {
                "state": "code_required",
                "error": "Слишком много попыток",
                "error_code": "flood_wait",
                "retryable": False,
            },
            {
                "state": "phone_required",
                "error": "Срок действия кода истёк",
                "error_code": "code_expired",
                "retryable": True,
            },
        )
        app.open_auth()
        await wait_for_condition(
            pilot, lambda: getattr(app.screen, "state", None) == "code_required"
        )
        dialog = app.screen
        dialog.query_one("#code", Input).value = "11111"
        dialog.query_one("#auth-primary").press()
        await wait_for_condition(
            pilot,
            lambda: "Неверный" in str(dialog.query_one("#auth-error", Static).content),
        )
        await wait_for_condition(pilot, lambda: not dialog._busy)
        dialog.query_one("#auth-primary").press()
        await wait_for_condition(
            pilot,
            lambda: (
                "Автоматический повтор отключён"
                in str(dialog.query_one("#auth-error", Static).content)
            ),
        )
        attempts = [name for name, _ in api.calls].count("auth_submit_code")
        await pilot.pause(0.08)
        assert [name for name, _ in api.calls].count("auth_submit_code") == attempts
        dialog.query_one("#auth-primary").press()
        await wait_for_condition(
            pilot, lambda: getattr(app.screen, "state", None) == "phone_required"
        )
        assert "истёк" in str(dialog.query_one("#auth-error", Static).content)
        await pilot.press("escape")
        await wait_for_condition(pilot, lambda: not isinstance(app.screen, AuthDialog))

        api.result["telegram"] = "phone_required"
        api.queue(
            "auth_begin", ConnectionError("VPN или сеть недоступны"), {"state": "phone_required"}
        )
        app.open_auth()
        await wait_for_condition(
            pilot,
            lambda: (
                isinstance(app.screen, AuthDialog)
                and "VPN" in str(app.screen.query_one("#auth-error", Static).content)
            ),
        )
        app.screen.query_one("#auth-primary").press()
        await wait_for_condition(
            pilot, lambda: getattr(app.screen, "state", None) == "phone_required"
        )
        await pilot.press("escape")
        await wait_for_condition(pilot, lambda: not isinstance(app.screen, AuthDialog))

        api.queue("auth_begin", {"state": "future_auth_state"})
        app.open_auth()
        await wait_for_condition(pilot, lambda: getattr(app.screen, "state", None) == "unknown")
        assert "Неизвестное состояние" in str(app.screen.query_one("#auth-error", Static).content)
        await pilot.press("escape")


@pytest.mark.asyncio
async def test_cancel_during_slow_authorization_cancels_ui_then_notifies_daemon():
    status = status_fixture()
    status["telegram"] = "phone_required"
    api = FakeAPI(status)
    api.operation_delay = 0.15
    api.queue("auth_begin", {"state": "phone_required"})
    app = HoardApp(api)
    async with app.run_test() as pilot:
        await wait_for_startup(app, pilot)
        app.open_auth()
        await pilot.pause(0.02)
        await pilot.press("escape")
        await wait_for_condition(pilot, lambda: not isinstance(app.screen, AuthDialog))
    assert "auth_cancel" in [name for name, _ in api.calls]
    assert "stop_collector" not in [name for name, _ in api.calls]


@pytest.mark.asyncio
async def test_auth_dialog_timeout_returns_to_retry_state():
    status = status_fixture()
    status["telegram"] = "phone_required"
    api = FakeAPI(status)
    api.queue("auth_begin", TimeoutError(), {"state": "phone_required"})
    app = HoardApp(api)
    async with app.run_test() as pilot:
        await wait_for_startup(app, pilot)
        app.open_auth()
        await wait_for_condition(
            pilot,
            lambda: (
                isinstance(app.screen, AuthDialog)
                and getattr(app.screen, "state", None) == "unknown"
            ),
        )
        dialog = app.screen
        error = str(dialog.query_one("#auth-error", Static).content)
        assert "сеть или VPN" in error
        assert "TimeoutError" not in error
        assert str(dialog.query_one("#auth-primary").label) == "Проверить снова"

        dialog.query_one("#auth-primary").press()
        await wait_for_condition(
            pilot, lambda: getattr(app.screen, "state", None) == "phone_required"
        )


@pytest.mark.asyncio
async def test_source_flow_validates_upserts_and_does_not_create_duplicate_scan():
    api = FakeAPI()
    app = HoardApp(api)
    async with app.run_test(size=(100, 30)) as pilot:
        await wait_for_startup(app, pilot)
        app.open_section("sources")
        await pilot.pause()
        assert isinstance(app.screen, SourcesScreen)
        await pilot.click("#source-add")
        await wait_for_condition(pilot, lambda: isinstance(app.screen, SourceDialog))
        dialog = app.screen
        dialog.query_one("#source-url", Input).value = "https://t.me/sample_channel/42"
        dialog.query_one("#source-primary").press()
        await wait_for_condition(pilot, lambda: getattr(app.screen, "step", None) == "parameters")
        await wait_for_condition(pilot, lambda: not dialog._busy)
        dialog.query_one("#source-output", Input).value = "/tmp/archive"
        dialog.query_one("#source-interval", Input).value = "300"
        dialog.query_one("#source-title", Input).value = "Updated title"
        dialog.query_one("#source-primary").press()
        await wait_for_condition(pilot, lambda: getattr(app.screen, "step", None) == "done")
        await wait_for_condition(pilot, lambda: not dialog._busy)
        assert "ближайшую проверку" in str(dialog.query_one("#source-message", Static).content)
        assert dialog.query_one("#source-start").display
        dialog.query_one("#source-start").press()
        await wait_for_condition(
            pilot, lambda: any(name == "start_collector" for name, _ in api.calls)
        )
        await wait_for_condition(pilot, lambda: not dialog._busy)
        dialog.query_one("#source-primary").press()
        await wait_for_condition(pilot, lambda: isinstance(app.screen, SourcesScreen))

        await pilot.click("#source-edit")
        await wait_for_condition(pilot, lambda: isinstance(app.screen, SourceDialog))
        dialog = app.screen
        assert dialog.query_one("#source-url", Input).value.endswith("/42")
        dialog.query_one("#source-primary").press()
        await wait_for_condition(pilot, lambda: getattr(app.screen, "step", None) == "parameters")
        await wait_for_condition(pilot, lambda: not dialog._busy)
        dialog.query_one("#source-interval", Input).value = "600"
        dialog.query_one("#source-primary").press()
        await wait_for_condition(pilot, lambda: getattr(app.screen, "step", None) == "done")
        await wait_for_condition(pilot, lambda: not dialog._busy)
        dialog.query_one("#source-primary").press()
        await wait_for_condition(pilot, lambda: isinstance(app.screen, SourcesScreen))

    names = [name for name, _ in api.calls]
    assert names.count("validate_source") == 2
    assert names.count("add_source") == 2
    assert names.count("start_collector") == 1
    assert "scan_source" not in names


@pytest.mark.asyncio
async def test_sources_toggle_targeted_and_global_scan_are_distinct():
    api = FakeAPI()
    app = HoardApp(api)
    async with app.run_test() as pilot:
        await wait_for_startup(app, pilot)
        app.open_section("sources")
        await pilot.pause()
        await pilot.click("#source-toggle")
        await wait_for_condition(
            pilot, lambda: any(name == "set_source_enabled" for name, _ in api.calls)
        )
        await wait_for_condition(pilot, lambda: not app._operations)
        # Re-enable so the selected-source scan remains a valid user action.
        app.screen.query_one("#source-toggle").press()
        await wait_for_condition(
            pilot,
            lambda: [name for name, _ in api.calls].count("set_source_enabled") == 2,
        )
        await pilot.click("#source-scan")
        await wait_for_condition(pilot, lambda: any(name == "scan_source" for name, _ in api.calls))
        await wait_for_condition(pilot, lambda: not app._operations)
        await pilot.click("#source-scan-all")
        await wait_for_condition(pilot, lambda: any(name == "scan_all" for name, _ in api.calls))
    assert ("scan_source", {"id": 1}) in api.calls


@pytest.mark.asyncio
async def test_double_source_action_is_dropped_while_first_ipc_call_is_running():
    api = FakeAPI()
    api.operation_delay = 0.12
    app = HoardApp(api)
    async with app.run_test() as pilot:
        await wait_for_startup(app, pilot)
        app.open_section("sources")
        await pilot.pause()
        screen = app.screen
        screen.action_scan_source()
        screen.action_scan_source()
        await pilot.pause(0.03)
        assert [name for name, _ in api.calls].count("scan_source") == 1
        await wait_for_condition(pilot, lambda: not app._operations)


@pytest.mark.asyncio
async def test_start_collector_connects_first_when_session_is_not_authorized():
    status = status_fixture()
    status["telegram"] = "disconnected"
    status["running"] = False
    status["collector_state"] = "stopped"
    api = FakeAPI(status)
    api.queue("auth_begin", {"state": "authorized", "authorized": True})
    app = HoardApp(api)
    async with app.run_test() as pilot:
        await wait_for_startup(app, pilot)
        app.open_section("telegram")
        await pilot.pause()
        await pilot.click("#collector-start")
        await wait_for_condition(
            pilot, lambda: any(name == "start_collector" for name, _ in api.calls)
        )
    names = [name for name, _ in api.calls]
    assert names.index("auth_begin") < names.index("start_collector")


@pytest.mark.asyncio
async def test_collector_controls_confirm_active_interrupt_and_refresh_state():
    api = FakeAPI()
    app = HoardApp(api)
    async with app.run_test() as pilot:
        await wait_for_startup(app, pilot)
        app.open_section("telegram")
        await pilot.pause()
        await pilot.click("#collector-pause")
        await wait_for_condition(
            pilot,
            lambda: isinstance(app.screen, ConfirmDialog)
            and len(app.screen.query("#confirm")) == 1,
        )
        dialog = app.screen
        assert "pause_collector" not in [name for name, _ in api.calls]
        dialog.query_one("#confirm").press()
        await wait_for_condition(
            pilot, lambda: any(name == "pause_collector" for name, _ in api.calls)
        )
        assert api.result["collector_state"] == "paused"

        await pilot.click("#collector-start")
        await wait_for_condition(
            pilot, lambda: any(name == "start_collector" for name, _ in api.calls)
        )
        assert api.result["running"] is True

        await pilot.click("#collector-stop")
        await wait_for_condition(
            pilot,
            lambda: isinstance(app.screen, ConfirmDialog)
            and len(app.screen.query("#confirm")) == 1,
        )
        app.screen.query_one("#confirm").press()
        await wait_for_condition(
            pilot, lambda: any(name == "stop_collector" for name, _ in api.calls)
        )
        assert api.result["collector_state"] == "stopped"


@pytest.mark.asyncio
async def test_archive_views_real_results_and_reports_failures_without_false_success():
    status = status_fixture()
    status["downloads"] = [
        {
            "date": "2026-10-21",
            "author_name": "Alice",
            "source_name": "sample_channel",
            "post_id": 42,
            "size": 2048,
            "state": "saved",
        }
    ]
    status["profiles"] = [
        {
            "display_name": "Alice",
            "current_username": "alice",
            "author_id": 99,
            "source_count": 1,
            "photo_count": 3,
            "last_seen": "2026-10-21",
            "photo_error": "",
        }
    ]
    status["stats"]["profile_photos"] = 3
    status["jobs"] = [
        {
            "id": 7,
            "kind": "telegram.scan",
            "state": "failed",
            "attempts": 1,
            "result": "Нет связи",
            "updated_at": "2026-10-21",
        }
    ]
    api = FakeAPI(status)
    api.queue(
        "rebuild_catalog",
        RuntimeError("Генерация HTML не удалась"),
        {"message": "HTML-каталог обновлён"},
    )
    app = HoardApp(api)
    async with app.run_test(size=(100, 30)) as pilot:
        await wait_for_startup(app, pilot)
        app.open_section("archive")
        await pilot.pause()
        screen = app.screen
        assert isinstance(screen, ArchiveScreen)
        assert "Фото профилей 3" in str(screen.query_one("#archive-summary", Static).content)

        await pilot.click("#mode-profiles")
        assert screen.mode == "profiles"
        await pilot.click("#mode-jobs")
        assert screen.mode == "jobs"
        await pilot.click("#job-retry")
        await wait_for_condition(pilot, lambda: any(name == "retry_job" for name, _ in api.calls))

        await pilot.click("#catalog-build")
        await wait_for_condition(
            pilot,
            lambda: app.notice is not None and "не удалась" in app.notice[0],
        )
        assert app.notice == ("Генерация HTML не удалась", True)
        await wait_for_condition(pilot, lambda: not app._operations)
        screen.query_one("#catalog-build").press()
        await wait_for_condition(
            pilot,
            lambda: app.notice is not None and app.notice[0] == "HTML-каталог обновлён",
        )

        await pilot.click("#telegram-enrich")
        await wait_for_condition(
            pilot,
            lambda: isinstance(app.screen, ConfirmDialog)
            and len(app.screen.query("#confirm")) == 1,
        )
        app.screen.query_one("#confirm").press()
        await wait_for_condition(
            pilot, lambda: any(name == "enrich_telegram" for name, _ in api.calls)
        )


@pytest.mark.asyncio
async def test_context_navigation_keeps_tables_native_and_hidden_sidebar_reachable():
    api = FakeAPI()
    app = HoardApp(api)
    async with app.run_test(size=(40, 16)) as pilot:
        await wait_for_startup(app, pilot)
        assert app.has_class("sidebar-hidden")
        await pilot.press("]")
        await pilot.pause()
        assert isinstance(app.screen, TelegramScreen)
        await pilot.press("]")
        await pilot.pause()
        assert isinstance(app.screen, SourcesScreen)
        table = app.screen.query_one("#sources-table", DataTable)
        table.focus()
        before = table.cursor_row
        await pilot.press("j")
        assert app.screen is table.screen
        assert table.cursor_row >= before
        await pilot.press("enter")
        await wait_for_condition(pilot, lambda: isinstance(app.screen, SourceDialog))
        source_input = app.screen.query_one("#source-url", Input)
        source_input.focus()
        previous = source_input.value
        await pilot.press("q")
        assert "q" in source_input.value
        assert source_input.value != previous
        assert app.is_running
        await pilot.press("escape")


@pytest.mark.asyncio
@pytest.mark.parametrize("size", [(40, 16), (60, 20), (80, 24), (120, 35)])
async def test_all_round_three_workspaces_survive_supported_sizes(size):
    app = HoardApp(FakeAPI())
    async with app.run_test(size=size) as pilot:
        await wait_for_startup(app, pilot)
        for section in ("telegram", "sources", "activity", "archive", "ai", "overview"):
            app.open_section(section)
            await pilot.pause()
            workspace = app.screen.query_one("#workspace")
            assert workspace.size.width > 0
            assert workspace.size.height > 0
            for table in app.screen.query(DataTable):
                assert table.size.width >= 0
                assert table.size.height >= 0


@pytest.mark.asyncio
async def test_ai_workspace_lists_real_records_and_keeps_strict_black_theme():
    api = FakeAPI()
    api.queue(
        "ai_prompt_list",
        {
            "items": [
                {
                    "id": "prompt-1",
                    "name": "Assistant",
                    "scope": "general",
                    "version": 2,
                    "version_id": "prompt-version-2",
                    "model": "deepseek-chat",
                    "response_format": "text",
                    "active": 1,
                }
            ]
        },
    )
    api.queue(
        "ai_chat_list",
        {
            "items": [
                {
                    "id": "chat-1",
                    "title": "Local chat",
                    "provider": "deepseek",
                    "model": "deepseek-chat",
                    "prompt_id": "prompt-1",
                    "message_count": 3,
                }
            ]
        },
    )
    app = HoardApp(api)
    async with app.run_test(size=(110, 35)) as pilot:
        await wait_for_startup(app, pilot)
        app.open_section("ai")
        await wait_for_condition(
            pilot,
            lambda: isinstance(app.screen, AIWorkspaceScreen)
            and app.screen.query_one("#ai-table", DataTable).row_count == 1,
        )
        assert "Local chat" in str(app.screen.query_one("#ai-table", DataTable).get_row_at(0))
        for node in [app.screen, *app.screen.query("*")]:
            assert node.styles.background.hex == "#000000", repr(node)
        await pilot.click("#ai-config")
        await wait_for_condition(pilot, lambda: isinstance(app.screen, AIRecordDialog))
        key = app.screen.query_one("#record-key", Input)
        assert key.password
        await pilot.press("escape")


@pytest.mark.asyncio
async def test_telegram_ai_requires_visible_adult_and_external_confirmation():
    status = status_fixture()
    status["profiles"] = [
        {
            "display_name": "Synthetic",
            "current_username": "synthetic",
            "author_id": 99,
            "source_count": 1,
            "photo_count": 1,
            "last_seen": "2026-10-22",
            "photo_error": "",
        }
    ]
    api = FakeAPI(status)
    api.queue(
        "ai_telegram_profile_get",
        {
            "profile": {
                "author_id": 99,
                "display_name": "Synthetic",
                "photos": [
                    {
                        "photo_id": "photo-1",
                        "photo_date": "2026-10-22",
                        "available": True,
                    }
                ],
            }
        },
    )
    api.queue(
        "ai_prompt_list",
        {
            "items": [
                {
                    "id": "telegram-prompt",
                    "version_id": "telegram-prompt-v1",
                    "name": "Telegram PSL",
                    "version": 1,
                }
            ]
        },
    )
    api.queue("ai_telegram_analysis_list", {"items": []})
    app = HoardApp(api)
    async with app.run_test(size=(100, 32)) as pilot:
        await wait_for_startup(app, pilot)
        app.open_section("archive")
        await pilot.pause()
        await pilot.click("#mode-profiles")
        await wait_for_condition(
            pilot,
            lambda: bool(getattr(app.screen, "_profile_ids", [])),
        )
        await pilot.click("#telegram-ai")
        await wait_for_condition(pilot, lambda: isinstance(app.screen, TelegramAIDialog))
        app.screen.query_one("#telegram-ai-photo-ids", Input).value = "photo-1"
        disclosure = str(app.screen.query_one(".dialog-message", Static).content)
        assert "18+" in disclosure
        assert "DeepSeek" in disclosure
        assert "платным" in disclosure
        app.screen.query_one("#telegram-ai-submit").press()
        await wait_for_condition(
            pilot, lambda: any(name == "ai_telegram_psl_start" for name, _ in api.calls)
        )
        start_args = next(args for name, args in api.calls if name == "ai_telegram_psl_start")
        assert start_args["adult_confirmed"] is True
        assert start_args["external_confirmed"] is True


@pytest.mark.asyncio
async def test_minimal_telegram_analysis_picker_uses_default_photos_and_one_operation_id():
    status = status_fixture()
    status["profiles"] = [
        {
            "display_name": "Synthetic",
            "current_username": "synthetic",
            "author_id": 99,
            "source_count": 1,
            "photo_count": 1,
            "last_seen": "2026-10-22",
            "photo_error": "",
        }
    ]
    api = FakeAPI(status)
    api.queue(
        "ai_config_get",
        {"configured": True, "key_status": "key_saved", "provider": "deepseek"},
    )
    api.queue(
        "ai_telegram_batch_preview",
        {
            "preview": {
                "selected_count": 1,
                "photo_count": 1,
                "selected_photo_count": 1,
                "ready_count": 1,
                "skipped_count": 0,
                "limit": 20,
                "offset": 0,
                "has_more": False,
                "items": [
                    {
                        "author_id": 99,
                        "display_name": "Synthetic",
                        "username": "synthetic",
                        "photo_count": 1,
                        "photos": [
                            {
                                "photo_id": "photo-1",
                                "date": "2026-10-22",
                                "size": 100,
                                "available": True,
                            }
                        ],
                        "selected_photo_ids": ["photo-1"],
                        "photo_snapshot": "snapshot-1",
                        "reason": "",
                        "previous": None,
                    }
                ],
            }
        },
    )
    api.queue(
        "ai_telegram_batch_start",
        {
            "batch": {
                "id": "batch-1",
                "selected_count": 1,
                "queued": 1,
                "skipped": 0,
            }
        },
    )
    api.queue(
        "ai_telegram_batch_get",
        {"batch": {"id": "batch-1", "terminal": True, "completed": 1}},
    )
    app = HoardApp(api)
    async with app.run_test(size=(100, 32)) as pilot:
        await wait_for_startup(app, pilot)
        app.open_section("archive")
        await pilot.pause()
        await pilot.click("#mode-profiles")
        await wait_for_condition(
            pilot,
            lambda: bool(getattr(app.screen, "_profile_ids", [])),
        )
        await pilot.click("#telegram-ai")
        await wait_for_condition(
            pilot,
            lambda: isinstance(app.screen, TelegramAnalysisDialog),
            f"notice={app.notice!r}; calls={api.calls!r}",
        )
        photos = app.screen.query_one("#analysis-photos", SelectionList)
        assert photos.selected == ["photo-1"]
        dialog_text = " ".join(str(node.content) for node in app.screen.query(Static))
        assert "18+" in dialog_text
        assert "DeepSeek" in str(app.screen.query_one("#analysis-submit").label)
        app.screen.query_one("#analysis-submit").press()
        await wait_for_condition(
            pilot, lambda: any(name == "ai_telegram_batch_start" for name, _ in api.calls)
        )
        start_args = next(
            args for name, args in api.calls if name == "ai_telegram_batch_start"
        )
        assert len(start_args["operation_id"]) == 32
        assert start_args["photo_ids"] == {"99": ["photo-1"]}
        assert start_args["photo_snapshots"] == {"99": "snapshot-1"}
        assert "confirmed_author_ids" not in start_args


@pytest.mark.asyncio
async def test_analysis_dialog_actions_visible_at_120x30():
    preview = {
        "selected_count": 100,
        "photo_count": 2000,
        "selected_photo_count": 400,
        "ready_count": 100,
        "skipped_count": 0,
        "limit": 20,
        "offset": 0,
        "has_more": True,
        "items": [
            {
                "author_id": author_id,
                "display_name": f"Synthetic Profile {author_id}",
                "username": f"person{author_id}",
                "photo_count": 20,
                "photos": [],
                "selected_photo_ids": [f"photo-{index}" for index in range(4)],
                "photo_snapshot": f"snapshot-{author_id}",
                "reason": "",
                "previous": None,
            }
            for author_id in range(1, 21)
        ],
    }
    app = HoardApp(FakeAPI())
    async with app.run_test(size=(120, 30)) as pilot:
        await wait_for_startup(app, pilot)
        app.push_screen(TelegramAnalysisDialog(preview))
        await wait_for_condition(
            pilot, lambda: isinstance(app.screen, TelegramAnalysisDialog)
        )
        submit = app.screen.query_one("#analysis-submit")
        cancel = app.screen.query_one("#analysis-cancel")
        scroll = app.screen.query_one("#analysis-scroll")

        assert submit.region.height > 0
        assert cancel.region.height > 0
        assert submit.region.bottom <= app.size.height
        assert cancel.region.bottom <= app.size.height
        assert scroll.max_scroll_y > 0


def test_cli_module_keeps_textual_lazy_for_noninteractive_commands():
    code = """
import sys
import hoard.cli as cli
async def fake_request(*args, **kwargs):
    return {"running": False}
cli.request = fake_request
sys.argv = ["hoard", "status", "--json"]
cli.main()
print("TEXTUAL_IMPORTED", any(
    name == "textual" or name.startswith("textual.") for name in sys.modules
))
"""
    result = subprocess.run(
        [sys.executable, "-c", code], capture_output=True, text=True, check=False
    )
    assert result.returncode == 0, result.stderr
    assert '"running": false' in result.stdout.lower()
    assert "TEXTUAL_IMPORTED False" in result.stdout


def test_tui_imports_neither_telethon_nor_storage():
    code = """
import sys
import hoard.tui.app
print("TELETHON", any(name == "telethon" or name.startswith("telethon.") for name in sys.modules))
print("STORAGE", "hoard.storage" in sys.modules)
"""
    result = subprocess.run(
        [sys.executable, "-c", code], capture_output=True, text=True, check=False
    )
    assert result.returncode == 0, result.stderr
    assert "TELETHON False" in result.stdout
    assert "STORAGE False" in result.stdout
