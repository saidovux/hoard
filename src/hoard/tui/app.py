"""Textual application lifecycle, navigation, and bounded status refresh."""

from __future__ import annotations

import asyncio
import uuid
from collections.abc import Awaitable, Callable
from typing import Any, ClassVar

from rich.text import Text
from textual.app import App
from textual.binding import Binding
from textual.containers import VerticalScroll
from textual.events import Resize
from textual.screen import ModalScreen
from textual.theme import Theme
from textual.widget import Widget
from textual.widgets import (
    Button,
    DataTable,
    Input,
    ListView,
    OptionList,
    Select,
    Static,
    TextArea,
)

from hoard import __version__

from .ai_dialogs import AIKeyDialog, TelegramAIDialog, TelegramAnalysisDialog
from .api import HoardAPI, IPCStatusAPI
from .colors import ACCENT, MUTED, TEXT
from .dialogs import AuthDialog
from .navigation import (
    SECTIONS,
    breadcrumb,
    palette_sections,
    resolve,
    section_index,
    sections_for,
    workspace,
)
from .palette import Command, PaletteScreen
from .screens import (
    AIWorkspaceScreen,
    ArchiveScreen,
    HomeScreen,
    TelegramLoginScreen,
    TelegramPublicationScreen,
    TelegramSourcesScreen,
    TelegramStatusScreen,
    TelegramWebScreen,
)
from .screens.base import WorkspaceScreen
from .widgets import Sidebar

BLACK_THEME = Theme(
    name="hoard-black",
    primary="#F79646",
    secondary="#888888",
    warning="#F79646",
    error="#F79646",
    success="#FFFFFF",
    accent="#F79646",
    foreground="#FFFFFF",
    background="#000000",
    surface="#000000",
    panel="#000000",
    boost="#000000",
    dark=True,
    luminosity_spread=0.0,
    text_alpha=1.0,
    variables={
        "footer-background": "#000000",
        "footer-foreground": "#888888",
        "input-cursor-background": "#F79646",
        "input-selection-background": "#000000",
    },
)


def _help_text() -> Text:
    sections = (
        (
            "Навигация",
            (
                ("j k  ↑ ↓", "выбор"),
                ("l  ⏎", "открыть"),
                ("h  esc", "назад"),
                ("[ ]", "предыдущий / следующий раздел"),
                (f"1 – {min(9, len(SECTIONS))}", "перейти к разделу"),
                ("tab  ⇧tab", "следующий / предыдущий элемент"),
                ("ctrl+pgup/dn", "прокрутить страницу"),
            ),
        ),
        (
            "Общее",
            (
                (":  ctrl+k", "палитра команд"),
                ("b", "скрыть / показать меню"),
                ("?", "эта подсказка"),
                ("q", "выход"),
            ),
        ),
    )
    text = Text()
    text.append("hoard", style=f"bold {ACCENT}")
    text.append(f"  v{__version__}", style=MUTED)
    for title, rows in sections:
        text.append(f"\n\n{title.upper()}\n", style=f"bold {MUTED}")
        for position, (keys, meaning) in enumerate(rows):
            if position:
                text.append("\n")
            text.append(keys.ljust(12), style=f"bold {ACCENT}")
            text.append(meaning, style=TEXT)
    return text


class HelpScreen(ModalScreen[None]):
    BINDINGS = [
        Binding("escape", "dismiss", "Закрыть"),
        Binding("?", "dismiss", "Закрыть"),
    ]

    def compose(self):
        with VerticalScroll(id="help-card"):
            yield Static(_help_text(), id="help-text")


#: Actions that must never fire while a modal is open or text is being typed.
GLOBAL_ACTIONS = frozenset(
    {
        "quit",
        "help",
        "palette",
        "nav_down",
        "nav_up",
        "open",
        "back",
        "sidebar",
        "previous_section",
        "next_section",
        "goto_section",
        "scroll_workspace",
    }
)


class HoardApp(App[None]):
    """Full-screen client backed entirely by the established daemon IPC."""

    CSS_PATH = "theme.tcss"
    TITLE = "Hoard"
    ENABLE_COMMAND_PALETTE = False
    SCREENS: ClassVar[dict[str, type[WorkspaceScreen]]] = {
        "home": HomeScreen,
        "archive": ArchiveScreen,
        "telegram_status": TelegramStatusScreen,
        "telegram_sources": TelegramSourcesScreen,
        "telegram_web": TelegramWebScreen,
        "telegram_login": TelegramLoginScreen,
        "telegram_publication": TelegramPublicationScreen,
        "ai": AIWorkspaceScreen,
    }
    BINDINGS = [
        Binding("q", "quit", "Выход"),
        Binding("?", "help", "Помощь"),
        Binding("j", "nav_down", "Вниз"),
        Binding("down", "nav_down", "Вниз"),
        Binding("k", "nav_up", "Вверх"),
        Binding("up", "nav_up", "Вверх"),
        Binding("l,enter", "open", "Открыть"),
        Binding("h,escape", "back", "Назад"),
        Binding("b", "sidebar", "Sidebar"),
        Binding("[", "previous_section", "Предыдущий раздел"),
        Binding("]", "next_section", "Следующий раздел"),
        Binding(":", "palette", "Команды"),
        Binding("ctrl+k", "palette", "Команды", show=False),
        Binding("ctrl+pageup", "scroll_workspace(-1)", "Страница вверх", show=False),
        Binding("ctrl+pagedown", "scroll_workspace(1)", "Страница вниз", show=False),
        *(
            Binding(str(number), f"goto_section({number - 1})", show=False)
            for number in range(1, 10)
        ),
    ]

    def __init__(self, api: HoardAPI | None = None) -> None:
        super().__init__()
        self.api = api or IPCStatusAPI()
        self.status: dict | None = None
        self.status_error: str | None = None
        self.notice: tuple[str, bool] | None = None
        self.web_state: dict[str, Any] = {"state": "unknown"}
        self._web_state_revision = 0
        self._web_check_task: asyncio.Task[dict[str, Any]] | None = None
        self._refresh_lock = asyncio.Lock()
        self._operations: set[str] = set()
        self._startup_complete = False
        self._last_narrow: bool | None = None
        self._telegram_return_section = "home"
        self.publication_source: dict[str, Any] | None = None

    def on_mount(self) -> None:
        self.register_theme(BLACK_THEME)
        self.theme = BLACK_THEME.name
        self.push_screen("home")
        self.run_worker(self._startup(), name="daemon-startup", exit_on_error=False)

    async def _startup(self) -> None:
        try:
            await self.api.ensure_started()
        except Exception as exc:  # UI boundary: keep an actionable screen on any IPC failure.
            self.status_error = str(exc) or type(exc).__name__
            self._startup_complete = True
            self._render_current()
            return
        self._startup_complete = True
        await self.refresh_status(brief=True)

    async def refresh_status(
        self, *, brief: bool = False, wait: bool = False, render: bool = True
    ) -> bool:
        """Perform at most one status request at a time."""
        if self._refresh_lock.locked() and not wait:
            return False
        async with self._refresh_lock:
            try:
                status = (
                    await self.api.status_brief()
                    if brief
                    else await self.api.status()
                )
            except Exception as exc:  # UI must stay responsive when IPC/network work fails.
                self.status_error = str(exc) or type(exc).__name__
            else:
                if brief and self.status is not None:
                    changed = any(self.status.get(key) != value for key, value in status.items())
                    if not changed and self.status_error is None:
                        return True
                    self.status = {**self.status, **status}
                else:
                    self.status = status
                self.status_error = None
            if render:
                self._render_current()
        return True

    def _render_current(self) -> None:
        screen = self.screen
        if isinstance(screen, WorkspaceScreen) and screen.is_mounted:
            screen.render_status(self.status, self.status_error)

    def set_notice(self, message: str, *, error: bool = False) -> None:
        self.notice = (message, error)
        self._render_current()

    def remember_web_state(self, value: dict[str, Any]) -> None:
        """Publish a command result and invalidate any older Web status check."""
        self._web_state_revision += 1
        self.web_state = dict(value)
        self._render_current()

    async def refresh_web_state(self, *, force: bool = False) -> dict[str, Any]:
        """Share one Web probe and ignore results older than an explicit command."""
        current = self._web_check_task
        if current is not None and not current.done() and not force:
            return await asyncio.shield(current)

        revision = self._web_state_revision

        async def fetch() -> dict[str, Any]:
            try:
                result = dict(await self.api.web_status())
            except Exception as exc:
                result = {"state": "error", "error": str(exc) or type(exc).__name__}
            if revision == self._web_state_revision:
                self.web_state = dict(result)
                self._render_current()
            return result

        task = asyncio.create_task(fetch())
        self._web_check_task = task
        try:
            return await asyncio.shield(task)
        finally:
            if self._web_check_task is task:
                self._web_check_task = None

    async def execute(
        self,
        key: str,
        operation: Callable[[], Awaitable[dict[str, Any]]],
        *,
        pending: str,
        refresh: bool = True,
    ) -> dict[str, Any] | None:
        """Run one named IPC mutation at a time and present only confirmed results."""
        if key in self._operations:
            self.set_notice("Эта операция уже выполняется.", error=True)
            return None
        self._operations.add(key)
        self.set_notice(pending)
        try:
            result = await operation()
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            self.set_notice(str(exc) or type(exc).__name__, error=True)
            return None
        finally:
            self._operations.discard(key)
        if refresh:
            await self.refresh_status()
        self.set_notice(str(result.get("message") or "Готово"))
        return result

    def open_section(self, section: str) -> None:
        section = resolve(section)
        if isinstance(self.screen, HelpScreen) or section not in self.SCREENS:
            return
        current = getattr(self.screen, "section", None)
        entering_telegram = (
            workspace(section) == "telegram" and workspace(str(current or "")) is None
        )
        if entering_telegram:
            current_section = resolve(str(current or "home"))
            self._telegram_return_section = (
                current_section if current_section in {"home", "archive", "ai"} else "home"
            )
        if current != section:
            self.switch_screen(section)
            if entering_telegram and str(self.web_state.get("state") or "unknown") == "unknown":
                self.run_worker(
                    self.refresh_web_state(), name="telegram-web-initial", exit_on_error=False
                )
            if section in {"archive", "telegram_sources"}:
                self.run_worker(
                    self.refresh_status(wait=True),
                    name=f"{section}-details",
                    exit_on_error=False,
                )

    def open_publication(self, source: dict[str, Any]) -> None:
        """Open one local publication without authorization or network work."""
        source_id = int(source.get("id") or 0)
        if source_id <= 0:
            self.set_notice("Публикация не выбрана.", error=True)
            return
        self.publication_source = dict(source)
        self.open_section("telegram_publication")

    def leave_workspace(self) -> None:
        """Return from a tool menu to the outer Hoard level."""
        self.open_section(self._telegram_return_section)

    def open_auth(self, on_authorized: Callable[[], None] | None = None) -> None:
        def complete(authorized: bool | None) -> None:
            if authorized and on_authorized is not None:
                on_authorized()

        self.push_screen(AuthDialog(self.api), complete)

    def ensure_telegram(self, on_authorized: Callable[[], None]) -> None:
        if str((self.status or {}).get("telegram") or "") == "authorized":
            on_authorized()
        else:
            self.open_auth(on_authorized)

    def ensure_ai(self, on_ready: Callable[[], None]) -> None:
        """Resume the initiating action after the one-field DeepSeek onboarding."""
        self.run_worker(
            self._ensure_ai(on_ready), name="ai-configuration-check", exit_on_error=False
        )

    async def _ensure_ai(self, on_ready: Callable[[], None]) -> None:
        try:
            config = await self.api.ai_call("ai_config_get")
        except Exception as exc:
            self.set_notice(str(exc) or type(exc).__name__, error=True)
            return
        if config.get("configured") or "key_status" not in config:
            on_ready()
            return
        self.push_screen(
            AIKeyDialog(),
            lambda key: self.run_worker(
                self._save_ai_key(key, on_ready),
                name="ai-key-save",
                exit_on_error=False,
            )
            if key
            else None,
        )

    async def _save_ai_key(self, key: str, on_ready: Callable[[], None]) -> None:
        try:
            result = await self.api.ai_call(
                "ai_config_set", {"provider": "deepseek", "api_key": key}
            )
        except Exception as exc:
            self.set_notice(str(exc) or type(exc).__name__, error=True)
            return
        self.set_notice(str(result.get("message") or "Ключ сохранён"))
        await self.refresh_status()
        on_ready()

    def open_telegram_analysis(self, selector: dict[str, Any]) -> None:
        self.ensure_ai(
            lambda: self.run_worker(
                self._preview_telegram_analysis(selector),
                name="telegram-analysis-preview",
                exit_on_error=False,
            )
        )

    async def _preview_telegram_analysis(self, selector: dict[str, Any]) -> None:
        page_limit = 20

        async def load_page(offset: int) -> dict[str, Any]:
            result = await self.api.ai_call(
                "ai_telegram_batch_preview",
                {**selector, "limit": page_limit, "offset": offset},
            )
            preview_page = result.get("preview")
            if not isinstance(preview_page, dict):
                raise RuntimeError("Daemon не вернул страницу preview анализа")
            return preview_page

        try:
            result = await self.api.ai_call(
                "ai_telegram_batch_preview",
                {**selector, "limit": page_limit, "offset": 0},
            )
        except Exception as exc:
            self.set_notice(str(exc) or type(exc).__name__, error=True)
            return
        preview = result.get("preview")
        if preview is None and len(selector.get("author_ids") or []) == 1:
            await self._open_legacy_telegram_analysis(int(selector["author_ids"][0]))
            return
        if not isinstance(preview, dict):
            self.set_notice("Daemon не вернул preview анализа.", error=True)
            return
        self.push_screen(
            TelegramAnalysisDialog(preview, page_loader=load_page),
            lambda choices: self._confirm_telegram_analysis(selector, choices),
        )

    async def _open_legacy_telegram_analysis(self, author_id: int) -> None:
        """Compatibility with pre-0.7 test doubles and older daemon contracts."""
        try:
            profile, prompts, analyses = await asyncio.gather(
                self.api.ai_call("ai_telegram_profile_get", {"author_id": author_id}),
                self.api.ai_call("ai_prompt_list", {"scope": "telegram", "limit": 100}),
                self.api.ai_call(
                    "ai_telegram_analysis_list", {"author_id": author_id, "limit": 20}
                ),
            )
        except Exception as exc:
            self.set_notice(str(exc) or type(exc).__name__, error=True)
            return
        self.push_screen(
            TelegramAIDialog(
                profile["profile"],
                list(prompts.get("items") or []),
                list(analyses.get("items") or []),
            ),
            lambda values: self._confirm_legacy_telegram_analysis(author_id, values),
        )

    def _confirm_legacy_telegram_analysis(
        self, author_id: int, values: dict[str, Any] | None
    ) -> None:
        if not values or values.get("open_photo_id"):
            return
        self.run_worker(
            self._start_legacy_telegram_analysis(author_id, values),
            name="telegram-analysis-start",
            exit_on_error=False,
        )

    async def _start_legacy_telegram_analysis(
        self, author_id: int, values: dict[str, Any]
    ) -> None:
        try:
            await self.api.ai_call(
                "ai_telegram_psl_start",
                {
                    "author_id": author_id,
                    "photo_ids": values["photo_ids"],
                    "content": values["content"],
                    "prompt_version_id": values["prompt_version_id"],
                    "provider": "deepseek",
                    "model": values["model"],
                    "operation_id": uuid.uuid4().hex,
                    "adult_confirmed": True,
                    "external_confirmed": True,
                },
            )
        except Exception as exc:
            self.set_notice(str(exc) or type(exc).__name__, error=True)
            return
        self.set_notice("Telegram AI-анализ поставлен в очередь.")

    def _confirm_telegram_analysis(
        self, selector: dict[str, Any], choices: dict[str, Any] | None
    ) -> None:
        if not choices:
            return
        operation_id = uuid.uuid4().hex
        self.run_worker(
            self._start_telegram_analysis(selector, choices, operation_id),
            name="telegram-analysis-start",
            exit_on_error=False,
        )

    async def _start_telegram_analysis(
        self, selector: dict[str, Any], choices: dict[str, Any], operation_id: str
    ) -> None:
        try:
            result = await self.api.ai_call(
                "ai_telegram_batch_start",
                {
                    **selector,
                    **choices,
                    "operation_id": operation_id,
                    "external_confirmed": True,
                },
            )
        except Exception as exc:
            self.set_notice(str(exc) or type(exc).__name__, error=True)
            return
        batch = result["batch"]
        self.set_notice(
            f"Группа создана: {batch.get('selected_count', 0)} профилей; "
            f"в очереди {batch.get('queued', 0)}; пропущено {batch.get('skipped', 0)}."
        )
        await self._poll_telegram_batch(str(batch["id"]))

    async def _poll_telegram_batch(self, batch_id: str) -> None:
        for _ in range(60):
            await asyncio.sleep(1)
            try:
                result = await self.api.ai_call(
                    "ai_telegram_batch_get", {"id": batch_id, "limit": 100}
                )
            except Exception as exc:
                self.set_notice(str(exc) or type(exc).__name__, error=True)
                return
            batch = result["batch"]
            cost = (
                f"≈ ${batch['estimated_cost_usd']}"
                if batch.get("estimated_cost_usd") is not None
                else "стоимость неизвестна"
            )
            self.set_notice(
                f"AI: очередь {batch.get('queued', 0)} · готово {batch.get('completed', 0)} · "
                f"пропущено {batch.get('skipped', 0)} · ошибки {batch.get('errors', 0)} · {cost}",
                error=bool(batch.get("errors")),
            )
            if batch.get("terminal"):
                return
        self.set_notice("Группа продолжает выполняться в daemon; её можно открыть позже.")

    def reopen_active_telegram_batch(self) -> None:
        self.run_worker(
            self._reopen_active_telegram_batch(),
            name="telegram-analysis-reopen",
            exit_on_error=False,
        )

    async def _reopen_active_telegram_batch(self) -> None:
        try:
            result = await self.api.ai_call(
                "ai_telegram_batch_list", {"active_only": True, "limit": 1}
            )
        except Exception as exc:
            self.set_notice(str(exc) or type(exc).__name__, error=True)
            return
        items = list(result.get("items") or [])
        if not items:
            self.set_notice("Активных Telegram AI-групп нет.")
            return
        batch = items[0]
        self.set_notice(f"Возобновляем наблюдение за группой {batch['id']}…")
        await self._poll_telegram_batch(str(batch["id"]))

    def open_web_interface(self) -> None:
        # Preparation may take as long as the real HTML build. Keep the TUI
        # responsive and make the waiting state explicit instead of blocking.
        self.set_notice("Подготавливаю локальный веб-интерфейс…")
        self.run_worker(
            self._launch_web_interface(), name="launch-web-interface", exit_on_error=False
        )

    async def _launch_web_interface(self) -> None:
        try:
            from hoard.web import launch_web_process

            url = await asyncio.to_thread(launch_web_process)
        except (OSError, RuntimeError) as exc:
            self.set_notice(str(exc), error=True)
            return
        self.set_notice(f"Веб-интерфейс готов: {url}")

    @staticmethod
    def _is_text_entry(widget: Widget | None) -> bool:
        return isinstance(widget, (Input, TextArea))

    @staticmethod
    def _is_interactive(widget: Widget | None) -> bool:
        return isinstance(
            widget, (Button, DataTable, Input, ListView, OptionList, Select, TextArea)
        )

    def check_action(self, action: str, parameters: tuple[object, ...]) -> bool | None:
        if isinstance(self.screen, ModalScreen) and action in GLOBAL_ACTIONS:
            return False
        if self._is_text_entry(self.focused) and action in GLOBAL_ACTIONS:
            return False
        return super().check_action(action, parameters)

    def _sidebar(self) -> Sidebar | None:
        if isinstance(self.screen, WorkspaceScreen):
            return self.screen.query_one(Sidebar)
        return None

    def _visible_sidebar(self) -> Sidebar | None:
        sidebar = self._sidebar()
        return sidebar if sidebar is not None and sidebar.display else None

    def _focus_is_inside(self, widget: Widget | None, ancestor: Widget) -> bool:
        return widget is not None and (widget is ancestor or ancestor in widget.ancestors)

    def _repair_focus(self) -> None:
        """Keep focus on a visible control when the responsive sidebar changes."""
        sidebar = self._sidebar()
        if sidebar is None:
            return
        if not sidebar.display:
            if self._focus_is_inside(self.focused, sidebar):
                self.set_focus(None)
        elif self.focused is None:
            sidebar.query_one(OptionList).focus()

    def action_nav_down(self) -> None:
        focused = self.focused
        if self._is_text_entry(focused):
            return
        sidebar = self._visible_sidebar()
        if sidebar is not None and self._focus_is_inside(focused, sidebar):
            sidebar.move(1)
            return
        if isinstance(focused, (DataTable, ListView, OptionList)):
            focused.action_cursor_down()
        elif self._is_interactive(focused):
            self.screen.focus_next()
        elif sidebar := self._visible_sidebar():
            sidebar.move(1)

    def action_nav_up(self) -> None:
        focused = self.focused
        if self._is_text_entry(focused):
            return
        sidebar = self._visible_sidebar()
        if sidebar is not None and self._focus_is_inside(focused, sidebar):
            sidebar.move(-1)
            return
        if isinstance(focused, (DataTable, ListView, OptionList)):
            focused.action_cursor_up()
        elif self._is_interactive(focused):
            self.screen.focus_previous()
        elif sidebar := self._visible_sidebar():
            sidebar.move(-1)

    def action_open(self) -> None:
        focused = self.focused
        if self._is_text_entry(focused):
            return
        sidebar = self._visible_sidebar()
        if sidebar is not None and self._focus_is_inside(focused, sidebar):
            sidebar.choose()
        elif isinstance(focused, DataTable):
            focused.action_select_cursor()
        elif isinstance(focused, ListView):
            focused.action_select_cursor()
        elif isinstance(focused, OptionList):
            focused.action_select()
        elif isinstance(focused, Button):
            focused.action_press()
        elif sidebar := self._visible_sidebar():
            sidebar.choose()
        elif isinstance(self.screen, WorkspaceScreen):
            self.screen.open_selected()

    def action_back(self) -> None:
        if self._is_text_entry(self.focused):
            return
        current = str(getattr(self.screen, "section", "home"))
        if workspace(current) == "telegram":
            if current == "telegram_publication":
                self.open_section("telegram_sources")
            elif current == "telegram_status":
                self.leave_workspace()
            else:
                self.open_section("telegram_status")
        elif current != "home":
            self.open_section("home")

    def action_help(self) -> None:
        if isinstance(self.screen, HelpScreen):
            self.pop_screen()
        else:
            self.push_screen(HelpScreen())

    def commands(self) -> list[Command]:
        """Everything the palette offers: each section, then the app-level commands."""
        entries = [
            Command(
                f"goto:{section.key}",
                section.label,
                hint=str(section_index(section.key) + 1),
                keywords=section.keywords,
                context=breadcrumb(section.key)[0].label if section.parent else "",
            )
            for section in palette_sections()
        ]
        entries += [
            Command("sidebar", "Показать / скрыть меню", "b", keywords=("sidebar", "навигация")),
            Command("help", "Горячие клавиши", "?", keywords=("помощь", "клавиши", "help")),
            Command("quit", "Выйти", "q", keywords=("закрыть", "exit", "quit")),
        ]
        return entries

    def action_palette(self) -> None:
        self.push_screen(PaletteScreen(self.commands()), self._run_command)

    def _run_command(self, key: str | None) -> None:
        if not key:
            return
        if key.startswith("goto:"):
            self.open_section(key.removeprefix("goto:"))
        elif key == "sidebar":
            self.action_sidebar()
        elif key == "help":
            self.action_help()
        elif key == "quit":
            self.exit()

    def action_goto_section(self, index: int) -> None:
        sections = sections_for(str(getattr(self.screen, "section", "home")))
        if 0 <= index < len(sections) and not self._is_text_entry(self.focused):
            self.open_section(sections[index].key)

    def action_sidebar(self) -> None:
        if not self._is_text_entry(self.focused):
            self.toggle_class("sidebar-hidden")
            self.call_after_refresh(self._repair_focus)

    def _move_section(self, offset: int) -> None:
        current = getattr(self.screen, "section", "home")
        sections = sections_for(str(current))
        index = (section_index(str(current)) + offset) % len(sections)
        self.open_section(sections[index].key)

    def action_scroll_workspace(self, direction: int) -> None:
        if not isinstance(self.screen, WorkspaceScreen):
            return
        workspace = self.screen.query_one("#workspace", VerticalScroll)
        if direction < 0:
            workspace.scroll_page_up(animate=False)
        else:
            workspace.scroll_page_down(animate=False)

    def action_previous_section(self) -> None:
        if not self._is_text_entry(self.focused):
            self._move_section(-1)

    def action_next_section(self) -> None:
        if not self._is_text_entry(self.focused):
            self._move_section(1)

    def on_resize(self, event: Resize) -> None:
        narrow = event.size.width < 60
        if narrow != self._last_narrow:
            self.set_class(narrow, "sidebar-hidden")
            self._last_narrow = narrow
        self.set_class(event.size.width < 76, "compact")
        self.set_class(event.size.width < 96, "narrow")
        self.set_class(event.size.width < 110, "medium")
        self.set_class(event.size.height < 30, "short")
        self.set_class(event.size.height < 22, "tiny")
        self.call_after_refresh(self._repair_focus)
        if isinstance(self.screen, WorkspaceScreen) and self.screen.is_mounted:
            self.screen.call_after_refresh(self.screen.on_app_resize)

    def on_unmount(self) -> None:
        for screen in self.screen_stack:
            if isinstance(screen, (HomeScreen, TelegramStatusScreen)):
                screen.stop_polling()
        if self._web_check_task is not None and not self._web_check_task.done():
            self._web_check_task.cancel()
