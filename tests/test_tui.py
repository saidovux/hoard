"""Headless coverage for the full-screen Textual client."""

from __future__ import annotations

import asyncio
import subprocess
import sys
from typing import TYPE_CHECKING, Any

import pytest

if TYPE_CHECKING:
    from textual.widgets import Input, OptionList, Static

    from hoard.tui.app import HelpScreen, HoardApp
    from hoard.tui.screens import ActivityScreen, OverviewScreen
    from hoard.tui.widgets import HoardHeader, HoardStatusBar, ProgressView, Sidebar


@pytest.fixture(scope="module", autouse=True)
def load_textual_after_existing_async_tests():
    """Keep Textual initialization local to its own headless test module."""
    from textual.widgets import Input as InputClass
    from textual.widgets import OptionList as OptionListClass
    from textual.widgets import Static as StaticClass

    from hoard.tui.app import HelpScreen as HelpScreenClass
    from hoard.tui.app import HoardApp as HoardAppClass
    from hoard.tui.screens import ActivityScreen as ActivityScreenClass
    from hoard.tui.screens import OverviewScreen as OverviewScreenClass
    from hoard.tui.widgets import HoardHeader as HoardHeaderClass
    from hoard.tui.widgets import HoardStatusBar as HoardStatusBarClass
    from hoard.tui.widgets import ProgressView as ProgressViewClass
    from hoard.tui.widgets import Sidebar as SidebarClass

    globals().update(
        ActivityScreen=ActivityScreenClass,
        HelpScreen=HelpScreenClass,
        HoardApp=HoardAppClass,
        HoardHeader=HoardHeaderClass,
        HoardStatusBar=HoardStatusBarClass,
        Input=InputClass,
        OptionList=OptionListClass,
        OverviewScreen=OverviewScreenClass,
        ProgressView=ProgressViewClass,
        Sidebar=SidebarClass,
        Static=StaticClass,
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


async def wait_for_startup(app: HoardApp, pilot) -> None:
    for _ in range(30):
        await pilot.pause(0.01)
        if app._startup_complete and (app.status is not None or app.status_error):
            return
    raise AssertionError("TUI startup did not complete")


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
        await pilot.press("j", "enter")
        await pilot.pause()
        assert isinstance(app.screen, ActivityScreen)
        assert len(app.screen_stack) == base_depth
        await pilot.press("h")
        await pilot.pause()
        assert isinstance(app.screen, OverviewScreen)
        assert len(app.screen_stack) == base_depth
        await pilot.press("down", "right")
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
        await pilot.press("j", "enter")
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
        await pilot.press("j", "enter")
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

        await pilot.press("j", "enter")
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
