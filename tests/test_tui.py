"""Headless coverage for the full-screen Textual client."""

from __future__ import annotations

import asyncio
import subprocess
import sys
import threading
from typing import TYPE_CHECKING, Any

import pytest

if TYPE_CHECKING:
    from textual.widgets import (
        Button,
        DataTable,
        Input,
        OptionList,
        SelectionList,
        Static,
    )

    from hoard.tui.ai_dialogs import (
        AIRecordDialog,
        TelegramAIDialog,
        TelegramAnalysisDialog,
        VideoFramesDialog,
    )
    from hoard.tui.app import HelpScreen, HoardApp
    from hoard.tui.dialogs import AuthDialog, ConfirmDialog, SourceDialog
    from hoard.tui.screens import (
        AIWorkspaceScreen,
        ArchiveScreen,
        HomeScreen,
        TelegramLoginScreen,
        TelegramScreen,
        TelegramSourcesScreen,
    )
    from hoard.tui.widgets import HoardHeader, HoardStatusBar, Sidebar


@pytest.fixture(scope="module", autouse=True)
def load_textual_after_existing_async_tests():
    """Keep Textual initialization local to its own headless test module."""
    from textual.widgets import Button as ButtonClass
    from textual.widgets import DataTable as DataTableClass
    from textual.widgets import Input as InputClass
    from textual.widgets import OptionList as OptionListClass
    from textual.widgets import SelectionList as SelectionListClass
    from textual.widgets import Static as StaticClass

    from hoard.tui.ai_dialogs import AIRecordDialog as AIRecordDialogClass
    from hoard.tui.ai_dialogs import TelegramAIDialog as TelegramAIDialogClass
    from hoard.tui.ai_dialogs import TelegramAnalysisDialog as TelegramAnalysisDialogClass
    from hoard.tui.ai_dialogs import VideoFramesDialog as VideoFramesDialogClass
    from hoard.tui.app import HelpScreen as HelpScreenClass
    from hoard.tui.app import HoardApp as HoardAppClass
    from hoard.tui.dialogs import AuthDialog as AuthDialogClass
    from hoard.tui.dialogs import ConfirmDialog as ConfirmDialogClass
    from hoard.tui.dialogs import SourceDialog as SourceDialogClass
    from hoard.tui.screens import AIWorkspaceScreen as AIWorkspaceScreenClass
    from hoard.tui.screens import ArchiveScreen as ArchiveScreenClass
    from hoard.tui.screens import HomeScreen as HomeScreenClass
    from hoard.tui.screens import TelegramLoginScreen as TelegramLoginScreenClass
    from hoard.tui.screens import TelegramScreen as TelegramScreenClass
    from hoard.tui.screens import TelegramSourcesScreen as TelegramSourcesScreenClass
    from hoard.tui.screens import TelegramWebScreen as TelegramWebScreenClass
    from hoard.tui.widgets import HoardHeader as HoardHeaderClass
    from hoard.tui.widgets import HoardStatusBar as HoardStatusBarClass
    from hoard.tui.widgets import Sidebar as SidebarClass

    globals().update(
        AIRecordDialog=AIRecordDialogClass,
        AIWorkspaceScreen=AIWorkspaceScreenClass,
        ArchiveScreen=ArchiveScreenClass,
        AuthDialog=AuthDialogClass,
        Button=ButtonClass,
        ConfirmDialog=ConfirmDialogClass,
        DataTable=DataTableClass,
        HelpScreen=HelpScreenClass,
        HomeScreen=HomeScreenClass,
        HoardApp=HoardAppClass,
        HoardHeader=HoardHeaderClass,
        HoardStatusBar=HoardStatusBarClass,
        Input=InputClass,
        OptionList=OptionListClass,
        SelectionList=SelectionListClass,
        Sidebar=SidebarClass,
        SourceDialog=SourceDialogClass,
        Static=StaticClass,
        TelegramScreen=TelegramScreenClass,
        TelegramLoginScreen=TelegramLoginScreenClass,
        TelegramSourcesScreen=TelegramSourcesScreenClass,
        TelegramWebScreen=TelegramWebScreenClass,
        TelegramAIDialog=TelegramAIDialogClass,
        TelegramAnalysisDialog=TelegramAnalysisDialogClass,
        VideoFramesDialog=VideoFramesDialogClass,
    )


def status_fixture(*, total: int | None = 4096) -> dict[str, Any]:
    progress: dict[str, Any] = {
        "source": "sample_channel/42",
        "checked": 17,
        "downloaded": 3,
        "file": "circle.mp4",
        "bytes": 1024,
        "speed_bps": 512,
        "active_downloads": 1,
        "aggregate_speed_bps": 512,
        "transfers": [
            {
                "source_id": 1,
                "message_id": 101,
                "bytes": 1024,
                "total": total or 0,
            }
        ],
    }
    if total is not None:
        progress["total"] = total
    return {
        "running": True,
        "collector_state": "running",
        "telegram": "authorized",
        "configured": True,
        "telegram_account": {
            "state": "connected",
            "connected": True,
            "session_present": True,
            "verified": True,
            "profile": {"id": 101, "name": "Test User", "username": "tester"},
        },
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
        self.web_state: dict[str, Any] = {"state": "stopped"}
        self.statistics: dict[str, Any] = {
            "sources": {"total": 3, "enabled": 2, "disabled": 1},
            "media": {
                "video_notes": 31,
                "post_images": 4,
                "post_videos": 2,
                "post_files": 1,
                "profile_photos": 9,
            },
            "profiles": {"unique": 14},
            "frames": {
                "processed_videos": 5,
                "created_frames": 13,
                "waiting": 2,
                "queued": 1,
                "processing": 1,
                "missing": 7,
                "failed": 3,
                "stale": 1,
                "active": False,
                "processing_mode": "off",
            },
            "psl": {
                "unique_profiles": 6,
                "completed_analyses": 8,
                "failed_analyses": 2,
                "unknown_outcome": 1,
                "without_result": 3,
                "queued": 1,
                "running": 0,
            },
        }

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

    async def status_brief(self) -> dict:
        return await self.status()

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

    async def telegram_account_check(self) -> dict:
        self.result["telegram"] = "authorized"
        self.result["telegram_account"] = {
            **self.result.get("telegram_account", {}),
            "state": "connected",
            "connected": True,
            "verified": True,
        }
        return await self._operation(
            "telegram_account_check", default={"message": "Сессия подтверждена"}
        )

    async def telegram_account_disconnect(self) -> dict:
        self.result["telegram"] = "disconnected"
        self.result["telegram_account"] = {
            **self.result.get("telegram_account", {}),
            "state": "saved",
            "connected": False,
        }
        return await self._operation(
            "telegram_account_disconnect", default={"message": "Соединение отключено"}
        )

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

    async def telegram_call(
        self, method: str, args: dict[str, Any] | None = None
    ) -> dict:
        return await self._operation(method, args)

    async def telegram_statistics(self) -> dict[str, Any]:
        return await self._operation("telegram_statistics_get", default=self.statistics)

    async def web_status(self) -> dict:
        self.calls.append(("web_status", {}))
        return dict(self.web_state)

    async def start_web(self) -> dict:
        self.web_state = {"state": "ready", "port": 45678}
        return await self._operation(
            "start_web", default={"state": "ready", "message": "Web включён"}
        )

    async def open_web(self) -> dict:
        return await self._operation(
            "open_web",
            default={
                "state": "ready",
                "url": "http://127.0.0.1:45678/start/test",
                "opened": True,
                "message": "Web открыт в браузере",
            },
        )

    async def stop_web(self) -> dict:
        self.web_state = {"state": "stopped"}
        return await self._operation(
            "stop_web", default={"state": "stopped", "message": "Web остановлен"}
        )

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
async def test_initializes_home_from_real_status_contract():
    from hoard.tui.widgets import TelegramMonitor

    api = FakeAPI()
    app = HoardApp(api)
    async with app.run_test(size=(80, 24)) as pilot:
        await wait_for_startup(app, pilot)
        assert isinstance(app.screen, HomeScreen)
        assert api.ensure_calls == 1
        assert api.status_calls == 1
        monitor = app.screen.query_one(TelegramMonitor)
        assert all(value in str(monitor.content) for value in ("sample_channel/42", "17", "3"))
        summary = str(app.screen.query_one("#home-summary", Static).content)
        assert all(number in summary for number in ("2", "14", "31", "4", "1"))
        assert not list(app.screen.query("#tools-panel"))
        assert not list(app.screen.query("#events-panel"))
        assert not list(app.screen.query("#now-panel"))


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
        for section in (
            "home",
            "telegram_status",
            "telegram_sources",
            "telegram_web",
            "telegram_login",
            "archive",
            "ai",
        ):
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
    from hoard.tui.widgets import TelegramMonitor

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
        monitor = app.screen.query_one(TelegramMonitor)
        assert monitor.virtual_size.width <= monitor.size.width
        assert str(monitor.content).splitlines()[-1].count("│") == 2


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
async def test_keyboard_navigation_enters_and_leaves_telegram_workspace():
    app = HoardApp(FakeAPI())
    async with app.run_test(size=(80, 24)) as pilot:
        await wait_for_startup(app, pilot)
        base_depth = len(app.screen_stack)
        # Home, Archive, then the cursor steps over the "ИНСТРУМЕНТЫ" header to Telegram.
        await pilot.press("j", "j", "enter")
        await pilot.pause()
        assert isinstance(app.screen, TelegramScreen)
        assert app.screen.section == "telegram_status"
        assert len(app.screen_stack) == base_depth
        await pilot.press("h")
        await pilot.pause()
        assert isinstance(app.screen, HomeScreen)
        assert len(app.screen_stack) == base_depth
        await pilot.press("down", "down", "enter")
        await pilot.pause()
        assert isinstance(app.screen, TelegramScreen)
        await pilot.press("escape")
        await pilot.pause()
        assert isinstance(app.screen, HomeScreen)
        # A list page hands the keyboard to its table; "h" still walks back out.
        await pilot.press("down", "enter")
        await pilot.pause()
        assert isinstance(app.screen, ArchiveScreen)
        assert isinstance(app.focused, DataTable)
        await pilot.press("h")
        await pilot.pause()
        assert isinstance(app.screen, HomeScreen)


@pytest.mark.asyncio
async def test_telegram_sidebar_has_pinned_lower_actions_and_returns_to_origin():
    app = HoardApp(FakeAPI())
    async with app.run_test(size=(80, 24)) as pilot:
        await wait_for_startup(app, pilot)
        app.open_section("archive")
        await pilot.pause()
        app.open_section("telegram")
        await pilot.pause()

        sidebar = app.screen.query_one(Sidebar)
        primary = sidebar.query_one("#navigation", OptionList)
        secondary = sidebar.query_one("#navigation-secondary", OptionList)
        assert [
            str(primary.get_option_at_index(index).id)
            for index in range(primary.option_count)
        ] == [Sidebar.BACK, "telegram_status", "telegram_sources"]
        assert [
            str(secondary.get_option_at_index(index).id)
            for index in range(secondary.option_count)
        ] == ["telegram_web", "telegram_login"]
        assert secondary.region.bottom <= sidebar.content_region.bottom
        assert sidebar.content_region.bottom - secondary.region.bottom <= 1
        assert primary.region.bottom <= secondary.region.y

        app.open_section("telegram_sources")
        await pilot.press("h")
        await pilot.pause()
        assert isinstance(app.screen, TelegramScreen)
        await pilot.press("h")
        await pilot.pause()
        assert isinstance(app.screen, ArchiveScreen)

        app.open_section("telegram")
        await pilot.pause()
        primary = app.screen.query_one("#navigation", OptionList)
        primary.highlighted = primary.get_option_index(Sidebar.BACK)
        primary.focus()
        await pilot.press("enter")
        await pilot.pause()
        assert isinstance(app.screen, ArchiveScreen)


@pytest.mark.asyncio
async def test_workspace_navigation_crosses_pinned_lists_with_j_and_k():
    app = HoardApp(FakeAPI())
    async with app.run_test(size=(60, 20)) as pilot:
        await wait_for_startup(app, pilot)
        app.open_section("telegram")
        await pilot.pause()
        sidebar = app.screen.query_one(Sidebar)
        primary = sidebar.query_one("#navigation", OptionList)
        secondary = sidebar.query_one("#navigation-secondary", OptionList)
        primary.highlighted = primary.option_count - 1
        primary.focus()
        await pilot.press("j")
        assert app.focused is secondary
        assert secondary.highlighted == 0
        await pilot.press("k")
        assert app.focused is primary
        assert primary.highlighted == primary.option_count - 1


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
async def test_exit_stops_home_timer():
    app = HoardApp(FakeAPI())
    async with app.run_test() as pilot:
        await wait_for_startup(app, pilot)
        home = app.screen
        assert isinstance(home, HomeScreen)
        assert home.poll_timer is not None
        await pilot.press("q")
    assert home.poll_timer is None


@pytest.mark.asyncio
async def test_empty_status_has_informative_states():
    from hoard.tui.widgets import TelegramMonitor

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
        monitor = app.screen.query_one(TelegramMonitor)
        assert "Источник" in str(monitor.content)
        assert "0 загрузок" in str(monitor.content)
        assert monitor.slot_keys == (None, None, None)


@pytest.mark.asyncio
async def test_home_uses_only_available_progress_values():
    from hoard.tui.widgets import TelegramMonitor

    app = HoardApp(FakeAPI(status_fixture(total=None)))
    async with app.run_test() as pilot:
        await wait_for_startup(app, pilot)
        progress = str(app.screen.query_one(TelegramMonitor).content)
        assert "sample_channel/42" in progress
        assert "17" in progress and "3" in progress
        assert "•" in progress
        assert "%" not in progress
        assert "ETA" not in progress


def test_telegram_home_indicator_requires_confirmed_account_and_collector_state():
    from hoard.tui.widgets.telegram_monitor import telegram_indicator

    status = status_fixture()
    assert telegram_indicator(status) == "green"
    assert telegram_indicator({**status, "collector_state": "paused", "running": False}) == "yellow"
    assert telegram_indicator({**status, "running": False}) == "yellow"
    assert telegram_indicator({**status, "telegram_account": {}}) == "neutral"
    assert telegram_indicator(
        {**status, "telegram_account": {"state": "saved", "verified": None}}
    ) == "red"
    assert telegram_indicator({**status, "telegram": "phone_required"}) == "red"
    assert telegram_indicator(status, available=False) == "neutral"


@pytest.mark.asyncio
async def test_telegram_monitor_keeps_three_stable_slots_for_four_transfers_and_clears_stale_data():
    from hoard.tui.widgets import TelegramMonitor

    def transfer(source_id: int, message_id: int, received: object, total: object) -> dict:
        return {
            "source_id": source_id,
            "message_id": message_id,
            "bytes": received,
            "total": total,
        }

    status = status_fixture()
    progress = {
        **status["progress"],
        "active_downloads": 4,
        "transfers": [
            transfer(1, 101, 10, 100),
            transfer(1, 102, 20, 0),
            transfer(1, 103, 30, 100),
            transfer(1, 104, 40, 100),
        ],
    }
    api = FakeAPI({**status, "progress": progress})
    app = HoardApp(api)
    async with app.run_test(size=(80, 24)) as pilot:
        await wait_for_startup(app, pilot)
        monitor = app.screen.query_one(TelegramMonitor)
        assert monitor.slot_keys == ((1, 101), (1, 102), (1, 103))
        assert "4 загрузок" in str(monitor.content)
        assert "•" in str(monitor.content)

        api.result = {
            **api.result,
            "progress": {
                **progress,
                "active_downloads": 3,
                "transfers": [
                    transfer(1, 103, 60, 100),
                    transfer(1, 104, 50, 100),
                    transfer(1, 101, 70, 100),
                ],
            },
        }
        await app.refresh_status(brief=True, wait=True)
        assert monitor.slot_keys == ((1, 101), (1, 104), (1, 103))

        api.result = {
            **api.result,
            "progress": {
                **api.result["progress"],
                "transfers": [
                    transfer(1, 104, 80, 100),
                    transfer(1, 101, 90, 100),
                    transfer(1, 103, 75, 100),
                ],
            },
        }
        await app.refresh_status(brief=True, wait=True)
        assert monitor.slot_keys == ((1, 101), (1, 104), (1, 103))

        api.result = {**api.result, "running": False, "collector_state": "stopped"}
        await app.refresh_status(brief=True, wait=True)
        assert monitor.slot_keys == (None, None, None)
        assert "0 загрузок" in str(monitor.content)

        api.result = {**api.result, "running": True, "collector_state": "running"}
        api.error = ConnectionError("daemon unavailable")
        await app.refresh_status(brief=True, wait=True)
        assert monitor.slot_keys == (None, None, None)
        assert "Данные Telegram недоступны" in str(monitor.content)


@pytest.mark.asyncio
async def test_telegram_monitor_handles_zero_to_four_transfers_and_invalid_sizes():
    from hoard.tui.widgets import TelegramMonitor

    app = HoardApp(FakeAPI())
    async with app.run_test(size=(80, 24)) as pilot:
        await wait_for_startup(app, pilot)
        monitor = app.screen.query_one(TelegramMonitor)
        for count in range(5):
            status = status_fixture()
            status["progress"]["active_downloads"] = count
            status["progress"]["transfers"] = [
                {
                    "source_id": 1,
                    "message_id": 200 + index,
                    "bytes": index * 10,
                    "total": -1 if index == 1 else 100,
                }
                for index in range(count)
            ]
            monitor.show_status(status)
            assert sum(key is not None for key in monitor.slot_keys) == min(count, 3)
            assert str(monitor.content).splitlines()[-1].count("│") == 2

        invalid = status_fixture()
        invalid["progress"]["active_downloads"] = 2
        invalid["progress"]["transfers"] = [
            {"source_id": 1, "message_id": 301, "bytes": "invalid", "total": 100},
            {"source_id": 1, "message_id": 302, "bytes": 50, "total": "unknown"},
        ]
        monitor.show_status(invalid)
        assert monitor.slot_keys == ((1, 302), None, None)
        assert "•" in str(monitor.content)


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
async def test_polling_only_runs_on_home_and_never_overlaps(monkeypatch):
    monkeypatch.setattr(HomeScreen, "POLL_INTERVAL", 0.02)
    api = FakeAPI()
    api.delay = 0.06
    app = HoardApp(api)
    async with app.run_test() as pilot:
        await wait_for_startup(app, pilot)
        startup_calls = api.status_calls
        await pilot.pause(0.16)
        assert api.status_calls > startup_calls
        assert api.max_active_calls == 1

        home = app.screen
        assert isinstance(home, HomeScreen)
        await pilot.press("2")
        await pilot.pause(0.12)
        assert isinstance(app.screen, ArchiveScreen)
        assert home.poll_timer is None
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
async def test_login_page_shows_cached_account_checks_and_disconnects_safely():
    status = status_fixture()
    status["running"] = False
    status["collector_state"] = "stopped"
    status["telegram"] = "disconnected"
    status["telegram_account"].update(
        state="saved",
        connected=False,
        verified=None,
    )
    api = FakeAPI(status)
    app = HoardApp(api)
    async with app.run_test(size=(100, 30)) as pilot:
        await wait_for_startup(app, pilot)
        app.open_section("telegram_login")
        await pilot.pause()
        assert isinstance(app.screen, TelegramLoginScreen)
        summary = str(app.screen.query_one("#account-summary", Static).content)
        assert "сессия сохранена" in summary
        assert "Последний подтверждённый аккаунт — сейчас не подключён" in summary
        assert "Test User" in summary and "@tester" in summary and "101" in summary

        login_option = app.screen.query_one(
            "#navigation-secondary", OptionList
        ).get_option_at_index(1)
        assert "#e3b341" in repr(login_option.prompt).lower()

        await pilot.click("#account-check")
        await wait_for_condition(
            pilot,
            lambda: any(name == "telegram_account_check" for name, _ in api.calls),
        )
        await wait_for_condition(pilot, lambda: not app._operations)
        assert "подключён" in str(app.screen.query_one("#account-summary", Static).content)

        await pilot.click("#account-disconnect")
        await wait_for_condition(
            pilot,
            lambda: any(name == "telegram_account_disconnect" for name, _ in api.calls),
        )
        await wait_for_condition(pilot, lambda: not app._operations)
        summary = str(app.screen.query_one("#account-summary", Static).content)
        assert "сессия сохранена" in summary and "соединение" in summary


@pytest.mark.asyncio
async def test_credentials_alone_never_make_account_indicator_green():
    status = status_fixture()
    status["telegram"] = "phone_required"
    status["configured"] = True
    status["telegram_account"] = {
        "state": "login_required",
        "connected": False,
        "session_present": False,
        "verified": False,
        "profile": {},
    }
    app = HoardApp(FakeAPI(status))
    async with app.run_test(size=(100, 30)) as pilot:
        await wait_for_startup(app, pilot)
        app.open_section("telegram_login")
        await pilot.pause()
        summary = str(app.screen.query_one("#account-summary", Static).content)
        assert "требуется вход" in summary
        option = app.screen.query_one("#navigation-secondary", OptionList).get_option_at_index(1)
        prompt = repr(option.prompt).lower()
        assert "#e5534b" in prompt
        assert "#55c878" not in prompt


@pytest.mark.asyncio
async def test_login_required_never_displays_stale_cached_identity():
    status = status_fixture()
    status["telegram"] = "phone_required"
    status["telegram_account"] = {
        "state": "login_required",
        "connected": False,
        "session_present": True,
        "verified": False,
        "profile": {"id": 101, "name": "Stale User", "username": "stale"},
    }
    app = HoardApp(FakeAPI(status))
    async with app.run_test(size=(100, 30)) as pilot:
        await wait_for_startup(app, pilot)
        app.open_section("telegram_login")
        await pilot.pause()
        summary = str(app.screen.query_one("#account-summary", Static).content)
        assert "требуется вход" in summary
        assert "Stale User" not in summary
        assert "@stale" not in summary
        assert "Telegram ID" not in summary


@pytest.mark.asyncio
async def test_login_page_refuses_disconnect_while_collector_runs():
    api = FakeAPI()
    app = HoardApp(api)
    async with app.run_test(size=(100, 30)) as pilot:
        await wait_for_startup(app, pilot)
        app.open_section("telegram_login")
        await pilot.pause()
        await pilot.click("#account-disconnect")
        await pilot.pause()
        assert "telegram_account_disconnect" not in [name for name, _ in api.calls]
        assert app.notice == (
            "Сначала останови или поставь Telegram-сборщик на паузу.",
            True,
        )


@pytest.mark.asyncio
async def test_source_flow_validates_upserts_and_does_not_create_duplicate_scan():
    api = FakeAPI()
    app = HoardApp(api)
    async with app.run_test(size=(100, 30)) as pilot:
        await wait_for_startup(app, pilot)
        app.open_section("telegram_sources")
        await pilot.pause()
        assert isinstance(app.screen, TelegramSourcesScreen)
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
        await wait_for_condition(pilot, lambda: isinstance(app.screen, TelegramSourcesScreen))

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
        await wait_for_condition(pilot, lambda: isinstance(app.screen, TelegramSourcesScreen))

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
        app.open_section("telegram_sources")
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
        app.open_section("telegram_sources")
        await pilot.pause()
        screen = app.screen
        screen.action_scan_source()
        screen.action_scan_source()
        await pilot.pause(0.03)
        assert [name for name, _ in api.calls].count("scan_source") == 1
        await wait_for_condition(pilot, lambda: not app._operations)


@pytest.mark.asyncio
async def test_start_collector_routes_to_login_when_session_is_not_authorized():
    status = status_fixture()
    status["telegram"] = "disconnected"
    status["telegram_account"].update(state="saved", connected=False)
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
        await wait_for_condition(pilot, lambda: isinstance(app.screen, TelegramLoginScreen))
        assert "start_collector" not in [name for name, _ in api.calls]
        await pilot.click("#account-connect")
        await wait_for_condition(pilot, lambda: not isinstance(app.screen, AuthDialog))
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
async def test_telegram_status_is_standalone_and_shows_live_work_and_statistics():
    api = FakeAPI()
    app = HoardApp(api)
    async with app.run_test(size=(100, 30)) as pilot:
        await wait_for_startup(app, pilot)
        app.open_section("telegram")
        await wait_for_condition(
            pilot,
            lambda: any(name == "telegram_statistics_get" for name, _ in api.calls),
        )
        current = str(app.screen.query_one("#telegram-current", Static).content)
        assert "sample_channel/42" in current
        assert "Работает" in current
        assert "Скачивание медиа" in current
        assert all(value in current for value in ("17", "3 новых кружков", "1 активных"))
        statistics = str(app.screen.query_one("#telegram-statistics", Static).content)
        assert all(group in statistics for group in ("ИСТОЧНИКИ", "МЕДИА", "КАДРЫ FFMPEG", "PSL"))
        assert "31" in statistics and "13" in statistics
        assert len(app.screen.query("#sources-table")) == 0
        assert len(app.screen.query("#phone")) == 0


def test_telegram_status_labels_only_confirmed_live_states():
    status = status_fixture()
    assert TelegramScreen._collector_state(status, None) == "Работает"
    assert TelegramScreen._stage(status) == "Скачивание медиа"

    status["progress"] = {}
    assert TelegramScreen._stage(status) == "Ожидание"
    status.update(running=False, collector_state="paused")
    assert TelegramScreen._collector_state(status, None) == "На паузе"
    status["collector_state"] = "stopped"
    assert TelegramScreen._collector_state(status, None) == "Остановлен"
    status["telegram_account"]["state"] = "login_required"
    assert TelegramScreen._collector_state(status, None) == "Требуется подключение"
    assert TelegramScreen._collector_state(status, "socket closed") == "Служба недоступна"


@pytest.mark.asyncio
async def test_telegram_status_polling_has_one_request_and_stops_when_hidden(monkeypatch):
    monkeypatch.setattr(TelegramScreen, "POLL_INTERVAL", 0.03)
    api = FakeAPI()
    app = HoardApp(api)
    async with app.run_test(size=(100, 30)) as pilot:
        await wait_for_startup(app, pilot)
        api.delay = 0.06
        baseline = api.status_calls
        app.open_section("telegram")
        await wait_for_condition(pilot, lambda: api.status_calls > baseline)
        status_screen = app.screen
        first_timer = status_screen.poll_timer
        await pilot.pause(0.16)
        assert first_timer is not None
        assert api.max_active_calls == 1

        app.open_section("archive")
        await pilot.pause(0.12)
        assert status_screen.poll_timer is None
        hidden_count = api.status_calls
        await pilot.pause(0.12)
        assert api.status_calls == hidden_count

        app.open_section("telegram")
        await wait_for_condition(pilot, lambda: api.status_calls > hidden_count)
        assert app.screen.poll_timer is not None
        assert app.screen.poll_timer is not first_timer
        assert api.max_active_calls == 1


@pytest.mark.asyncio
async def test_telegram_statistics_refresh_is_serial_and_preserves_previous_snapshot():
    api = FakeAPI()
    app = HoardApp(api)
    async with app.run_test(size=(100, 30)) as pilot:
        await wait_for_startup(app, pilot)
        app.open_section("telegram")
        await wait_for_condition(
            pilot,
            lambda: "Обновлено:" in str(
                app.screen.query_one("#telegram-statistics", Static).content
            ),
        )
        initial_calls = [name for name, _ in api.calls].count("telegram_statistics_get")
        assert initial_calls == 1
        app._render_current()
        await pilot.resize_terminal(80, 24)
        await pilot.pause()
        assert [name for name, _ in api.calls].count("telegram_statistics_get") == 1

        api.operation_delay = 0.08
        api.queue("telegram_statistics_get", RuntimeError("statistics unavailable"))
        button = app.screen.query_one("#telegram-statistics-refresh", Button)
        button.press()
        button.press()
        await wait_for_condition(
            pilot,
            lambda: "Данные предыдущего обновления" in str(
                app.screen.query_one("#telegram-statistics", Static).content
            ),
        )
        assert [name for name, _ in api.calls].count("telegram_statistics_get") == 2
        statistics = str(app.screen.query_one("#telegram-statistics", Static).content)
        assert "statistics unavailable" in statistics
        assert "31" in statistics and "13" in statistics
        assert button.disabled is False

        app.open_section("home")
        await pilot.pause()
        app.open_section("telegram")
        await wait_for_condition(
            pilot,
            lambda: [name for name, _ in api.calls].count("telegram_statistics_get") == 3,
        )


@pytest.mark.asyncio
async def test_hidden_statistics_result_is_discarded_and_reloaded_on_return():
    api = FakeAPI()
    api.operation_delay = 0.08
    app = HoardApp(api)
    async with app.run_test(size=(100, 30)) as pilot:
        await wait_for_startup(app, pilot)
        app.open_section("telegram")
        await wait_for_condition(
            pilot,
            lambda: [name for name, _ in api.calls].count("telegram_statistics_get") == 1,
        )
        app.open_section("home")
        await pilot.pause()
        app.open_section("telegram")
        await wait_for_condition(
            pilot,
            lambda: [name for name, _ in api.calls].count("telegram_statistics_get") == 2,
        )
        await wait_for_condition(
            pilot,
            lambda: "Обновлено:" in str(
                app.screen.query_one("#telegram-statistics", Static).content
            ),
        )
        assert app.notice is None


@pytest.mark.asyncio
async def test_telegram_status_remains_scrollable_without_horizontal_overflow():
    app = HoardApp(FakeAPI())
    async with app.run_test(size=(120, 35)) as pilot:
        await wait_for_startup(app, pilot)
        app.open_section("telegram")
        for size in ((120, 35), (80, 24), (60, 20), (40, 16)):
            await pilot.resize_terminal(*size)
            await pilot.pause()
            workspace = app.screen.query_one("#workspace")
            assert workspace.virtual_size.width <= workspace.size.width
            assert len(app.screen.query("#collector-start")) == 1
            assert len(app.screen.query("#telegram-statistics-refresh")) == 1


@pytest.mark.asyncio
async def test_first_telegram_entry_checks_web_once_without_starting_or_opening_it():
    api = FakeAPI()
    app = HoardApp(api)
    async with app.run_test(size=(100, 30)) as pilot:
        await wait_for_startup(app, pilot)
        app.open_section("telegram")
        await wait_for_condition(
            pilot, lambda: [name for name, _ in api.calls].count("web_status") == 1
        )
        app._render_current()
        app._render_current()
        await pilot.pause()
        names = [name for name, _ in api.calls]
        assert names.count("web_status") == 1
        assert "start_web" not in names
        assert "open_web" not in names


@pytest.mark.asyncio
async def test_late_web_probe_cannot_overwrite_newer_command_state():
    class SlowWebAPI(FakeAPI):
        def __init__(self) -> None:
            super().__init__()
            self.web_started = asyncio.Event()
            self.web_release = asyncio.Event()

        async def web_status(self) -> dict:
            self.calls.append(("web_status", {}))
            self.web_started.set()
            await self.web_release.wait()
            return {"state": "stopped"}

    api = SlowWebAPI()
    app = HoardApp(api)
    async with app.run_test(size=(100, 30)) as pilot:
        await wait_for_startup(app, pilot)
        probe = asyncio.create_task(app.refresh_web_state())
        await asyncio.wait_for(api.web_started.wait(), timeout=1)
        app.remember_web_state({"state": "ready", "port": 45678})
        api.web_release.set()
        await probe
        await pilot.pause()
        assert app.web_state == {"state": "ready", "port": 45678}


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
        assert isinstance(app.screen, ArchiveScreen)
        await pilot.press("3")
        await pilot.pause()
        assert isinstance(app.screen, TelegramScreen)
        await pilot.press("2")
        await pilot.pause()
        assert isinstance(app.screen, TelegramSourcesScreen)
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
        for section in (
            "telegram_status",
            "telegram_sources",
            "telegram_web",
            "telegram_login",
            "archive",
            "ai",
            "home",
        ):
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
from hoard import __version__
from hoard.ipc import IPC_PROTOCOL
async def fake_request(*args, **kwargs):
    return {"version": __version__, "protocol": IPC_PROTOCOL, "running": False}
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



@pytest.mark.asyncio
async def test_navigation_registry_groups_tools_and_nested_telegram_pages():
    from hoard.tui.navigation import (
        GROUP_TITLES,
        SECTIONS,
        TELEGRAM_SECTIONS,
        breadcrumb,
    )
    keys = [section.key for section in SECTIONS]
    assert keys == ["home", "archive", "telegram", "ai"]
    assert len(keys) == len(set(keys))
    assert {"home", "archive", "ai"} <= set(HoardApp.SCREENS)
    assert {section.key for section in TELEGRAM_SECTIONS} <= set(HoardApp.SCREENS)
    assert [section.label for section in breadcrumb("telegram")] == ["Telegram", "Статус"]
    assert [section.label for section in breadcrumb("sources")] == ["Telegram", "Источники"]
    assert [section.label for section in breadcrumb("home")] == ["Главная"]

    app = HoardApp(FakeAPI())
    async with app.run_test(size=(100, 30)) as pilot:
        await wait_for_startup(app, pilot)
        navigation = app.screen.query_one(OptionList)
        options = [navigation.get_option_at_index(i) for i in range(navigation.option_count)]
        headers = [option for option in options if option.disabled]
        assert len(headers) == 1
        assert GROUP_TITLES["tools"] in str(headers[0].prompt)
        assert [str(option.id) for option in options if not option.disabled] == keys


def test_legacy_routes_resolve_to_concrete_navigation_levels():
    from hoard.tui import navigation

    assert [navigation.resolve(key) for key in ("overview", "activity", "sources")] == [
        "home",
        "home",
        "telegram_sources",
    ]
    assert navigation.resolve("archive") == "archive"
    assert navigation.resolve("telegram") == "telegram_status"
    assert navigation.section_index("sources") == 1
    assert navigation.section_index("telegram") == 0


@pytest.mark.asyncio
async def test_legacy_section_keys_open_the_page_that_absorbed_them():
    app = HoardApp(FakeAPI())
    async with app.run_test(size=(100, 30)) as pilot:
        await wait_for_startup(app, pilot)
        app.open_section("sources")
        await pilot.pause()
        assert isinstance(app.screen, TelegramSourcesScreen)
        app.open_section("overview")
        await pilot.pause()
        assert isinstance(app.screen, HomeScreen)
        app.open_section("activity")
        await pilot.pause()
        assert isinstance(app.screen, HomeScreen)


@pytest.mark.asyncio
async def test_header_breadcrumb_and_footer_hints_follow_the_screen():
    app = HoardApp(FakeAPI())
    async with app.run_test(size=(120, 35)) as pilot:
        await wait_for_startup(app, pilot)
        header = str(app.screen.query_one(HoardHeader).content)
        footer = app.screen.query_one(HoardStatusBar).content
        assert "Главная" in header and "служба доступна" in header
        app.status = {**(app.status or {}), "running": False, "collector_state": "paused"}
        app._render_current()
        header = str(app.screen.query_one(HoardHeader).content)
        assert "служба доступна" in header and "пауза" not in header.lower()
        assert "выбор" in str(footer) and "команды" in str(footer) and "выход" in str(footer)
        # Each shortcut is a reversed (block) chip.
        assert any("reverse" in str(span.style) for span in footer.spans)

        app.open_section("telegram")
        await pilot.pause()
        header = str(app.screen.query_one(HoardHeader).content)
        footer = str(app.screen.query_one(HoardStatusBar).content)
        assert "Telegram" in header and "Статус" in header and "Источники" not in header
        assert "панели" in footer and "помощь" in footer
        app.open_section("telegram_sources")
        await pilot.pause()
        # The source shortcuts live on the frame of the panel they act on, not in the footer.
        hints = str(app.screen.query_one("#sources-panel").border_subtitle)
        assert "добавить" in hints and "изменить" in hints and "проверить" in hints
        assert "добавить" not in footer


@pytest.mark.asyncio
async def test_footer_keeps_global_shortcuts_on_the_narrowest_terminal():
    app = HoardApp(FakeAPI())
    async with app.run_test(size=(40, 16)) as pilot:
        await wait_for_startup(app, pilot)
        await pilot.pause()
        footer = str(app.screen.query_one(HoardStatusBar).content)
        assert len(footer) <= 40
        assert ":" in footer and "?" in footer and "q" in footer


@pytest.mark.asyncio
async def test_digit_shortcuts_jump_to_sections_and_leave_text_entry_alone():
    app = HoardApp(FakeAPI())
    async with app.run_test(size=(100, 30)) as pilot:
        await wait_for_startup(app, pilot)
        await pilot.press("3")
        await pilot.pause()
        assert isinstance(app.screen, TelegramScreen)
        await pilot.press("2")
        await pilot.pause()
        assert isinstance(app.screen, TelegramSourcesScreen)
        await pilot.press("9")
        await pilot.pause()
        assert isinstance(app.screen, TelegramSourcesScreen)
        await pilot.press("1")
        await pilot.pause()
        assert isinstance(app.screen, TelegramScreen)
        await pilot.press("h")
        await pilot.pause()
        assert isinstance(app.screen, HomeScreen)

        input_widget = Input()
        await app.screen.query_one("#workspace").mount(input_widget)
        input_widget.focus()
        await pilot.press("3", ":")
        await pilot.pause()
        assert input_widget.value == "3:"
        assert isinstance(app.screen, HomeScreen)


def test_palette_matching_is_word_based_and_prefers_label_prefixes():
    from hoard.tui.palette import Command, matches

    commands = [
        Command("a", "Архив", keywords=("кружки",)),
        Command("b", "Источники", context="Telegram", keywords=("посты",)),
        Command("c", "Telegram"),
    ]
    assert [item.key for item in matches(commands, "")] == ["a", "b", "c"]
    assert [item.key for item in matches(commands, "tele")] == ["c", "b"]
    assert [item.key for item in matches(commands, "telegram ист")] == ["b"]
    assert [item.key for item in matches(commands, "КРУЖКИ")] == ["a"]
    assert matches(commands, "нет такого") == []


@pytest.mark.asyncio
async def test_command_palette_filters_navigates_and_never_leaks_keys():
    from hoard.tui.palette import PaletteScreen

    app = HoardApp(FakeAPI())
    async with app.run_test(size=(100, 30)) as pilot:
        await wait_for_startup(app, pilot)
        await pilot.press(":")
        await pilot.pause()
        assert isinstance(app.screen, PaletteScreen)
        options = app.screen.query_one("#palette-list", OptionList)
        assert options.option_count == len(app.commands())

        # Letters that are global shortcuts elsewhere are plain text here.
        await pilot.press("q", "j", "1")
        await pilot.pause()
        assert isinstance(app.screen, PaletteScreen)
        assert app.screen.query_one("#palette-input", Input).value == "qj1"
        assert options.option_count == 1  # the disabled "nothing found" row
        assert options.get_option_at_index(0).disabled
        await pilot.press("enter")
        await pilot.pause()
        assert isinstance(app.screen, PaletteScreen)

        await pilot.press("escape")
        await pilot.pause()
        assert isinstance(app.screen, HomeScreen)
        assert app.is_running

        await pilot.press("ctrl+k")
        await pilot.pause()
        assert isinstance(app.screen, PaletteScreen)
        options = app.screen.query_one("#palette-list", OptionList)
        await pilot.press(*"сборщик")
        await pilot.pause()
        assert options.option_count == 1
        assert str(options.get_option_at_index(0).id) == "goto:telegram_status"
        await pilot.press("enter")
        await wait_for_condition(pilot, lambda: isinstance(app.screen, TelegramScreen))
        assert len(app.screen_stack) == 2  # the palette left no screen behind


@pytest.mark.asyncio
async def test_command_palette_runs_app_commands():
    from hoard.tui.palette import PaletteScreen

    app = HoardApp(FakeAPI())
    async with app.run_test(size=(100, 30)) as pilot:
        await wait_for_startup(app, pilot)
        await pilot.press(":")
        await pilot.press(*"клавиши")
        await pilot.press("enter")
        await wait_for_condition(pilot, lambda: isinstance(app.screen, HelpScreen))
        await pilot.press("escape")
        await pilot.pause()

        await pilot.press(":")
        await pilot.press(*"меню")
        await pilot.press("enter")
        await pilot.pause()
        assert not isinstance(app.screen, PaletteScreen)
        assert app.has_class("sidebar-hidden")


@pytest.mark.asyncio
async def test_help_and_palette_describe_and_keep_the_black_theme():
    from hoard import __version__
    from hoard.tui.palette import PaletteScreen

    app = HoardApp(FakeAPI())
    async with app.run_test(size=(100, 30)) as pilot:
        await wait_for_startup(app, pilot)
        await pilot.press("?")
        await pilot.pause()
        text = str(app.screen.query_one("#help-text", Static).content)
        assert __version__ in text
        assert "1 – 4" in text and "палитра" in text and "ctrl+k" in text
        for node in [app.screen, *app.screen.query("*")]:
            assert node.styles.background.hex == "#000000", repr(node)
        await pilot.press("escape")
        await pilot.pause()

        await pilot.press(":")
        await pilot.pause()
        assert isinstance(app.screen, PaletteScreen)
        for node in [app.screen, *app.screen.query("*")]:
            assert node.styles.background.hex == "#000000", repr(node)
        navigation = app.screen.query_one("#palette-list", OptionList)
        assert (
            navigation.get_component_styles("option-list--option-highlighted").color.hex
            == "#F79646"
        )


@pytest.mark.asyncio
async def test_responsive_classes_follow_terminal_size():
    app = HoardApp(FakeAPI())
    async with app.run_test(size=(120, 35)) as pilot:
        await wait_for_startup(app, pilot)
        wide = {"compact", "narrow", "medium", "short", "tiny"}
        assert not wide & set(app.classes)
        await pilot.resize_terminal(80, 24)
        assert {"narrow", "medium", "short"} <= set(app.classes)
        assert not {"compact", "tiny"} & set(app.classes)
        await pilot.resize_terminal(40, 16)
        assert wide <= set(app.classes)
        await pilot.resize_terminal(120, 35)
        assert not wide & set(app.classes)


@pytest.mark.asyncio
async def test_home_keeps_hoard_totals_compact_below_the_telegram_monitor():
    app = HoardApp(FakeAPI(status_fixture()))
    async with app.run_test(size=(120, 35)) as pilot:
        await wait_for_startup(app, pilot)
        await pilot.pause()
        wide = str(app.screen.query_one("#home-summary", Static).content)
        assert "\n" not in wide
        assert "Очередь" in wide and "Архив" in wide
        assert all(number in wide for number in ("2", "14", "31", "4", "1"))

        await pilot.resize_terminal(40, 16)
        await pilot.pause()
        narrow = str(app.screen.query_one("#home-summary", Static).content)
        assert narrow.count("\n") == 1
        assert all(number in narrow for number in ("2", "14", "31", "4", "1"))


@pytest.mark.asyncio
async def test_actions_and_tables_are_borderless_words_not_boxes():
    from textual.widgets import Button

    app = HoardApp(FakeAPI())
    async with app.run_test(size=(120, 35)) as pilot:
        await wait_for_startup(app, pilot)
        for section in ("home", "telegram_status", "telegram_sources", "archive"):
            app.open_section(section)
            await pilot.pause()
            for button in app.screen.query(Button):
                assert button.styles.border_top[0] in {"", "none"}, button.id
                assert button.region.height <= 1, button.id
            for table in app.screen.query(DataTable):
                assert table.styles.border_top[0] in {"", "none"}, table.id


@pytest.mark.asyncio
async def test_home_shows_only_the_logo_until_the_daemon_answers():
    from hoard.tui.widgets import Hero

    app = HoardApp(FakeAPI(error=ConnectionError("socket недоступен")))
    async with app.run_test(size=(120, 35)) as pilot:
        await wait_for_startup(app, pilot)
        assert isinstance(app.screen, HomeScreen)
        assert app.screen.query_one(Hero).display
        assert not app.screen.query_one("#telegram-monitor-panel").display
        assert not app.screen.query_one("#home-summary").display
        assert app.screen.query_one("#status-error", Static).display


@pytest.mark.asyncio
async def test_home_carries_the_program_name_and_picks_a_logo_that_fits():
    from hoard.tui.widgets.hero import LOGO_BIG, LOGO_SMALL, logo_text

    app = HoardApp(FakeAPI())
    async with app.run_test(size=(120, 35)) as pilot:
        await wait_for_startup(app, pilot)
        big, small = app.screen.query_one("#hero-big"), app.screen.query_one("#hero-small")
        assert big.display and not small.display
        text = str(big.content)
        assert "v0.7.4.1" in text
        assert "локальный архив" not in text
        assert "всё хранится" not in text
        assert "сборщик" not in text
        # Letters are painted as coloured blanks, so the solid glyph never reaches the terminal.
        assert "█" not in text
        assert len(logo_text(LOGO_BIG, shadow=True)) == len(LOGO_BIG)
        await pilot.resize_terminal(80, 24)
        await pilot.pause()
        assert small.display and not big.display
        assert all(ch in str(small.content) for ch in "╦╠╩")
        assert LOGO_SMALL[0][:3] in str(small.content)
        await pilot.resize_terminal(40, 16)
        await pilot.pause()
        assert not big.display and not small.display


@pytest.mark.asyncio
async def test_panels_carry_their_title_in_the_border_and_light_up_with_focus():
    from hoard.tui.widgets import Panel

    app = HoardApp(FakeAPI())
    async with app.run_test(size=(120, 35)) as pilot:
        await wait_for_startup(app, pilot)
        titles = {str(panel.border_title) for panel in app.screen.query(Panel)}
        assert any("Telegram " in title for title in titles)
        assert not {"Сейчас", "Инструменты", "События"} & titles
        app.open_section("telegram_sources")
        await pilot.pause()
        panel = app.screen.query_one("#sources-panel", Panel)
        table = app.screen.query_one("#sources-table", DataTable)
        assert app.focused is table
        assert panel.has_pseudo_class("focus-within")
        assert str(panel.styles.border_top[1]) != str(
            app.screen.query_one(Sidebar).styles.border_top[1]
        )


@pytest.mark.asyncio
async def test_sources_table_is_compact_and_trims_timestamps():
    app = HoardApp(FakeAPI())
    async with app.run_test(size=(120, 35)) as pilot:
        await wait_for_startup(app, pilot)
        app.open_section("telegram_sources")
        await pilot.pause()
        table = app.screen.query_one("#sources-table", DataTable)
        labels = [str(column.label) for column in table.columns.values()]
        assert labels == ["Источник", "Состояние", "Кружки", "Каждые", "Проверка", "Результат"]
        row = [str(cell) for cell in table.get_row_at(0)]
        assert "2026-10-21 10:12" in row and "2026-10-21 10:12:13" not in row
        assert table.virtual_size.width <= table.size.width  # no horizontal scrollbar at 120 cols


@pytest.mark.asyncio
async def test_sources_table_drops_schedule_columns_instead_of_scrolling_sideways():
    app = HoardApp(FakeAPI())
    async with app.run_test(size=(100, 30)) as pilot:
        await wait_for_startup(app, pilot)
        app.open_section("telegram_sources")
        await pilot.pause()
        table = app.screen.query_one("#sources-table", DataTable)
        assert [str(column.label) for column in table.columns.values()] == [
            "Источник",
            "Состояние",
            "Кружки",
            "Результат",
        ]
        assert table.virtual_size.width <= table.size.width
        assert app.screen.selected_source_id == 1
        await pilot.resize_terminal(130, 35)
        await pilot.pause()
        table = app.screen.query_one("#sources-table", DataTable)
        assert len(table.columns) == 6
        assert app.screen.selected_source_id == 1
        await pilot.resize_terminal(80, 24)
        await pilot.pause()
        table = app.screen.query_one("#sources-table", DataTable)
        assert str(table.get_row_at(0)[0]) == "Sample"
        assert table.virtual_size.width <= table.size.width


@pytest.mark.asyncio
async def test_web_launch_runs_off_the_event_loop(monkeypatch):
    started = threading.Event()
    release = threading.Event()

    def slow_launch():
        started.set()
        release.wait(5)
        return "http://127.0.0.1:45678/start/test"

    monkeypatch.setattr("hoard.web.launch_web_process", slow_launch)
    app = HoardApp(FakeAPI())
    async with app.run_test() as pilot:
        await wait_for_startup(app, pilot)
        app.open_web_interface()
        assert await asyncio.to_thread(started.wait, 1)
        assert app.notice == ("Подготавливаю локальный веб-интерфейс…", False)

        await pilot.press("?")
        assert isinstance(app.screen, HelpScreen)
        await pilot.press("escape")
        await wait_for_condition(pilot, lambda: not isinstance(app.screen, HelpScreen))

        release.set()
        await wait_for_condition(
            pilot,
            lambda: app.notice is not None and app.notice[0].startswith("Веб-интерфейс готов"),
        )
    assert app.notice == ("Веб-интерфейс готов: http://127.0.0.1:45678/start/test", False)


@pytest.mark.asyncio
async def test_telegram_web_controls_reflect_state_and_are_nonblocking():
    api = FakeAPI()
    app = HoardApp(api)
    async with app.run_test(size=(100, 30)) as pilot:
        await wait_for_startup(app, pilot)
        app.open_section("telegram_web")
        await wait_for_condition(
            pilot,
            lambda: "выключен" in str(app.screen.query_one("#web-summary", Static).content),
        )
        assert "start_web" not in [name for name, _ in api.calls]
        screen = app.screen
        assert screen.query_one("#web-start", Button).disabled is False
        assert screen.query_one("#web-open", Button).disabled is True
        assert screen.query_one("#web-stop", Button).disabled is True

        screen.query_one("#web-start").press()
        await wait_for_condition(
            pilot,
            lambda: "работает" in str(app.screen.query_one("#web-summary", Static).content),
        )
        assert [name for name, _ in api.calls].count("open_web") == 1
        assert screen.query_one("#web-start", Button).disabled is True
        assert screen.query_one("#web-open", Button).disabled is False
        assert screen.query_one("#web-stop", Button).disabled is False

        screen.query_one("#web-open").press()
        await wait_for_condition(
            pilot, lambda: [name for name, _ in api.calls].count("open_web") == 2
        )
        await wait_for_condition(
            pilot,
            lambda: not screen._web_loading and not app._operations,
        )

        screen.query_one("#web-stop").press()
        await wait_for_condition(
            pilot,
            lambda: "выключен" in str(app.screen.query_one("#web-summary", Static).content),
        )
        assert any(name == "stop_web" for name, _ in api.calls)
        assert not any(
            name in {"stop_collector", "pause_collector"} for name, _ in api.calls
        )

    assert [name for name, _ in api.calls].count("web_status") >= 1


@pytest.mark.asyncio
async def test_web_browser_failure_keeps_ready_server_running():
    api = FakeAPI()
    api.queue("open_web", RuntimeError("Браузер недоступен"))
    app = HoardApp(api)
    async with app.run_test(size=(100, 30)) as pilot:
        await wait_for_startup(app, pilot)
        app.open_section("telegram_web")
        await wait_for_condition(
            pilot,
            lambda: "выключен" in str(app.screen.query_one("#web-summary", Static).content),
        )
        await pilot.click("#web-start")
        await wait_for_condition(
            pilot,
            lambda: app.notice is not None and "браузер" in app.notice[0].lower(),
        )
        await wait_for_condition(pilot, lambda: not app.screen._web_loading)
        assert app.notice == (
            "Web Viewer работает, но браузер не открылся: Браузер недоступен",
            True,
        )
        assert api.web_state["state"] == "ready"
        assert "stop_web" not in [name for name, _ in api.calls]
        assert "работает" in str(app.screen.query_one("#web-summary", Static).content)


@pytest.mark.asyncio
async def test_telegram_web_state_is_recovered_on_reopen():
    api = FakeAPI()
    api.web_state = {"state": "ready", "port": 45678, "controllable": True}
    app = HoardApp(api)
    async with app.run_test(size=(100, 30)) as pilot:
        await wait_for_startup(app, pilot)
        app.open_section("telegram_web")
        await wait_for_condition(
            pilot,
            lambda: "работает" in str(app.screen.query_one("#web-summary", Static).content),
        )
        assert "127.0.0.1:45678" in str(
            app.screen.query_one("#web-summary", Static).content
        )
        assert app.screen.query_one("#web-open", Button).disabled is False


@pytest.mark.asyncio
async def test_archive_frame_controls_open_local_set_and_start_backfill():
    status = status_fixture()
    status["video_frames"] = {
        "total": 1,
        "saved": 1,
        "failed": 0,
        "pending": 0,
        "missing": 0,
        "actionable": 1,
        "processing": 0,
        "backfill_active": False,
        "backfill_complete": False,
    }
    status["downloads"] = [
        {
            "source_id": 1,
            "message_id": 10,
            "author_id": 99,
            "author_name": "Alice",
            "source_name": "sample_channel",
            "post_id": 42,
            "date": "2026-10-21",
            "size": 2048,
            "state": "saved",
            "frame_status": "saved",
            "frame_count": 3,
        }
    ]
    details = {
        "video_id": "1:10",
        "source_id": 1,
        "message_id": 10,
        "author_id": 99,
        "status": "saved",
        "valid": True,
        "frame_count": 3,
        "frames": [
            {
                "image_id": f"frame:1:10:{position}",
                "position": position,
                "timestamp_seconds": position * 1.0,
                "file_path": f"/tmp/frame-{position}.jpg",
                "size": 100,
                "available": True,
            }
            for position in (1, 2, 3)
        ],
    }
    api = FakeAPI(status)
    api.queue("telegram_frames_get", {"frames": details, "stats": status["video_frames"]})
    app = HoardApp(api)
    async with app.run_test(size=(100, 30)) as pilot:
        await wait_for_startup(app, pilot)
        app.open_section("archive")
        await pilot.pause()
        screen = app.screen
        assert isinstance(screen, ArchiveScreen)
        assert screen.query_one("#frames-view", Button).display is True
        assert screen.query_one("#telegram-ai-frames", Button).display is False
        assert "1 готовы" in str(screen.query_one("#archive-summary", Static).content)

        screen.query_one("#frames-view").press()
        await wait_for_condition(
            pilot, lambda: isinstance(app.screen, VideoFramesDialog)
        )
        dialog = app.screen
        await wait_for_condition(
            pilot, lambda: bool(dialog.query("#frames-analyze"))
        )
        assert "Кадры видеокружка 1:10" in str(
            dialog.query_one(".dialog-title", Static).content
        )
        assert dialog.query_one("#frames-analyze", Button).disabled is False
        await pilot.press("escape")
        await pilot.pause()

        screen.query_one("#frames-process").press()
        await wait_for_condition(
            pilot,
            lambda: any(name == "telegram_frames_process" for name, _ in api.calls),
        )

        screen.query_one("#frames-backfill").press()
        await wait_for_condition(pilot, lambda: isinstance(app.screen, ConfirmDialog))
        assert "нагружает CPU" in str(app.screen.query_one(".dialog-message", Static).content)
        app.screen.query_one("#confirm").press()
        await wait_for_condition(
            pilot,
            lambda: any(
                name == "telegram_frames_backfill_start" for name, _ in api.calls
            ),
        )

        await pilot.click("#mode-profiles")
        assert screen.mode == "profiles"
        assert screen.query_one("#frames-view", Button).display is False
        assert screen.query_one("#telegram-ai-frames", Button).display is True
        assert screen.query_one("#telegram-ai-combined", Button).display is True


@pytest.mark.asyncio
async def test_profile_frame_ai_preview_shows_video_and_frame_choice():
    status = status_fixture()
    status["profiles"] = [
        {
            "display_name": "Alice",
            "current_username": "alice",
            "author_id": 99,
            "source_count": 1,
            "photo_count": 1,
            "last_seen": "2026-10-21",
            "photo_error": "",
        }
    ]
    api = FakeAPI(status)
    api.queue(
        "ai_config_get",
        {"provider": "deepseek", "model": "deepseek-flash", "configured": True},
    )
    preview = {
        "selected_count": 1,
        "ready_count": 1,
        "skipped_count": 0,
        "selected_image_count": 3,
        "selected_photo_count": 0,
        "image_mode": "frames",
        "photo_count": 1,
        "limit": 20,
        "offset": 0,
        "has_more": False,
        "items": [
            {
                "author_id": 99,
                "display_name": "Alice",
                "username": "alice",
                "ready": True,
                "reason": "",
                "photo_count": 1,
                "image_count": 3,
                "frame_count": 3,
                "video_count": 1,
                "image_mode": "frames",
                "video_id": "1:10",
                "selected_photo_ids": [],
                "selected_image_ids": ["frame:1:10:1", "frame:1:10:2", "frame:1:10:3"],
                "selected_images": [
                    {"image_id": f"frame:1:10:{position}", "kind": "frame", "position": position}
                    for position in (1, 2, 3)
                ],
                "videos": [
                    {
                        "video_id": "1:10",
                        "message_id": 10,
                        "message_date": "2026-10-21",
                    }
                ],
                "image_snapshot": "snapshot",
                "photo_snapshot": "",
                "photos": [],
                "adult_confirmed": True,
                "previous": None,
            }
        ],
    }
    api.queue("ai_telegram_batch_preview", {"preview": preview})
    app = HoardApp(api)
    async with app.run_test(size=(100, 30)) as pilot:
        await wait_for_startup(app, pilot)
        app.open_section("archive")
        await pilot.pause()
        await pilot.click("#mode-profiles")
        await wait_for_condition(
            pilot,
            lambda: bool(getattr(app.screen, "_profile_ids", [])),
        )
        app.screen.query_one("#telegram-ai-frames").press()
        await wait_for_condition(
            pilot, lambda: isinstance(app.screen, TelegramAnalysisDialog)
        )
        dialog = app.screen
        await wait_for_condition(
            pilot,
            lambda: "кадры: 3"
            in str(dialog.query_one("#analysis-page-items", Static).content),
        )
        chooser = dialog.query_one("#analysis-video", OptionList)
        assert chooser.highlighted == 0
        assert chooser.get_option_at_index(0).id == "1:10"
        assert any(name == "ai_telegram_batch_preview" for name, _ in api.calls)


@pytest.mark.asyncio
async def test_home_polling_keeps_sidebar_selection(monkeypatch):
    from hoard.tui.screens.home import HomeScreen

    monkeypatch.setattr(HomeScreen, "POLL_INTERVAL", 0.05)
    api = FakeAPI()
    app = HoardApp(api)
    async with app.run_test(size=(80, 24)) as pilot:
        await wait_for_startup(app, pilot)
        navigation = app.screen.query_one(OptionList)
        navigation.focus()
        await pilot.press("j")
        assert navigation.get_option_at_index(navigation.highlighted).id == "archive"
        await wait_for_condition(pilot, lambda: api.status_calls >= 2)
        assert navigation.get_option_at_index(navigation.highlighted).id == "archive"


@pytest.mark.asyncio
async def test_archive_preserves_cursor_across_refresh_resize_and_profile_toggle():
    status = status_fixture()
    status["downloads"] = [
        {
            "source_id": 1,
            "message_id": 100 + index,
            "date": f"2026-10-21T10:{index:02d}:00+00:00",
            "author_name": f"Author {index}",
            "source_name": "sample_channel",
            "post_id": 42,
            "size": 1024,
            "frame_status": "saved",
            "frame_count": 3,
            "state": "saved",
        }
        for index in range(6)
    ]
    status["profiles"] = [
        {
            "display_name": f"Profile {index}",
            "current_username": f"profile_{index}",
            "author_id": 1000 + index,
            "source_count": 1,
            "photo_count": 0,
            "video_count": 1,
            "frame_set_count": 1,
            "last_seen": "2026-10-21",
            "photo_error": "",
        }
        for index in range(6)
    ]
    app = HoardApp(FakeAPI(status))
    async with app.run_test(size=(120, 35)) as pilot:
        await wait_for_startup(app, pilot)
        app.open_section("archive")
        await pilot.pause()
        table = app.screen.query_one("#archive-table", DataTable)
        table.move_cursor(row=4, scroll=False)
        app.set_notice("refresh")
        await pilot.pause()
        assert table.cursor_row == 4

        await pilot.resize_terminal(100, 30)
        await pilot.pause()
        table = app.screen.query_one("#archive-table", DataTable)
        assert table.cursor_row == 4

        await pilot.click("#mode-profiles")
        await wait_for_condition(pilot, lambda: len(app.screen._profile_ids) == 6)
        table = app.screen.query_one("#archive-table", DataTable)
        table.move_cursor(row=3, scroll=False)
        app.screen._toggle_profile_selection()
        await pilot.pause()
        assert table.cursor_row == 3
        assert 1003 in app.screen._selected_profiles


@pytest.mark.asyncio
async def test_source_table_fits_long_real_values_at_supported_widths():
    status = status_fixture()
    status["sources"][0].update(
        {
            "title": "",
            "username": "very_long_source_username",
            "last_error": "Нет связи с Telegram: очень длинное описание сетевой ошибки",
        }
    )
    app = HoardApp(FakeAPI(status))
    async with app.run_test(size=(120, 35)) as pilot:
        await wait_for_startup(app, pilot)
        app.open_section("telegram_sources")
        await pilot.pause()
        for width, height in ((120, 35), (100, 30), (80, 24)):
            if (app.size.width, app.size.height) != (width, height):
                await pilot.resize_terminal(width, height)
                await pilot.pause()
            table = app.screen.query_one("#sources-table", DataTable)
            assert table.virtual_size.width <= table.size.width
        await pilot.resize_terminal(60, 20)
        await pilot.pause()
        table = app.screen.query_one("#sources-table", DataTable)
        assert table.virtual_size.width <= table.size.width


@pytest.mark.asyncio
async def test_workspace_is_not_a_dead_tab_stop_and_hidden_sidebar_drops_focus():
    app = HoardApp(FakeAPI())
    async with app.run_test(size=(80, 24)) as pilot:
        await wait_for_startup(app, pilot)
        workspace = app.screen.query_one("#workspace")
        assert workspace.can_focus is False
        await pilot.press("ctrl+pagedown")
        await pilot.pause()
        assert workspace.scroll_offset.y == 0  # compact Home fits without a scroll region
        await pilot.resize_terminal(40, 16)
        await pilot.pause()
        assert app.focused is None
        await pilot.resize_terminal(80, 24)
        await pilot.pause()
        assert getattr(app.focused, "id", None) == "navigation"


@pytest.mark.asyncio
async def test_ai_opens_chat_with_visible_focus_and_returns_to_table():
    from textual.widgets import TextArea

    api = FakeAPI()
    api.queue(
        "ai_config_get",
        {"configured": True, "key_status": "verified", "key_source": "file"},
    )
    api.queue(
        "ai_chat_list",
        {
            "items": [
                {
                    "id": 1,
                    "title": "Test chat",
                    "provider": "deepseek",
                    "model": "deepseek-chat",
                    "prompt_id": "prompt-1",
                    "message_count": 2,
                }
            ],
            "has_more": False,
        },
    )
    api.queue(
        "ai_chat_history",
        {
            "items": [
                {"role": "user", "state": "completed", "content": "hello"},
                {"role": "assistant", "state": "completed", "content": "hi"},
            ],
            "has_more": False,
        },
    )
    app = HoardApp(api)
    async with app.run_test(size=(80, 24)) as pilot:
        await wait_for_startup(app, pilot)
        app.open_section("ai")
        await wait_for_condition(pilot, lambda: isinstance(app.focused, DataTable))
        await pilot.press("enter")
        await wait_for_condition(pilot, lambda: isinstance(app.focused, TextArea))
        assert app.screen.query_one("#ai-main").display is False
        assert app.screen.query_one("#ai-chat-pane").display is True
        assert app.screen.query_one("#ai-message").is_on_screen

        app.screen.query_one("#ai-chat-back").press()
        await pilot.pause()
        assert app.screen.query_one("#ai-main").display is True
        assert app.screen.query_one("#ai-chat-pane").display is False
        assert isinstance(app.focused, DataTable)


@pytest.mark.asyncio
async def test_archive_actions_fit_and_advanced_actions_are_opt_in():
    app = HoardApp(FakeAPI())
    async with app.run_test(size=(100, 30)) as pilot:
        await wait_for_startup(app, pilot)
        app.open_section("archive")
        await pilot.pause()
        screen = app.screen
        assert screen.query_one("#archive-advanced").display is False
        for mode in ("downloads", "profiles", "jobs"):
            await pilot.click(f"#mode-{mode}")
            await pilot.pause()
            for button in screen.query(Button):
                if button.display and button.region.width:
                    assert button.region.right <= app.size.width, (mode, button.id)
        await pilot.click("#mode-profiles")
        await pilot.click("#archive-more")
        await pilot.pause()
        assert screen.query_one("#archive-advanced").display is True
        for button in screen.query(Button):
            if button.display and button.region.width:
                assert button.region.right <= app.size.width, button.id
