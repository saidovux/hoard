"""Textual application lifecycle, navigation, and bounded status refresh."""

from __future__ import annotations

import asyncio
import uuid
from collections.abc import Awaitable, Callable
from typing import Any, ClassVar

from textual.app import App
from textual.binding import Binding
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

from .ai_dialogs import AIKeyDialog, TelegramAIDialog, TelegramAnalysisDialog
from .api import HoardAPI, IPCStatusAPI
from .dialogs import AuthDialog, ConfirmDialog
from .navigation import SECTIONS, section_index
from .screens import (
    ActivityScreen,
    AIWorkspaceScreen,
    ArchiveScreen,
    OverviewScreen,
    SourcesScreen,
    TelegramScreen,
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


class HelpScreen(ModalScreen[None]):
    BINDINGS = [
        Binding("escape", "dismiss", "Закрыть"),
        Binding("?", "dismiss", "Закрыть"),
    ]

    def compose(self):
        yield Static(
            "КЛАВИАТУРА\n\n"
            "j / ↓        вниз\n"
            "k / ↑        вверх\n"
            "l / → / Enter  открыть\n"
            "h / ← / Esc    назад\n"
            "Tab           следующий элемент\n"
            "Shift+Tab     предыдущий\n"
            "b             скрыть sidebar\n"
            "[ / ]         сменить раздел\n"
            "?             помощь\n"
            "q             выход",
            id="help-card",
        )


class HoardApp(App[None]):
    """Full-screen client backed entirely by the established daemon IPC."""

    CSS_PATH = "theme.tcss"
    TITLE = "Hoard"
    ENABLE_COMMAND_PALETTE = False
    SCREENS: ClassVar[dict[str, type[WorkspaceScreen]]] = {
        "overview": OverviewScreen,
        "telegram": TelegramScreen,
        "sources": SourcesScreen,
        "activity": ActivityScreen,
        "archive": ArchiveScreen,
        "ai": AIWorkspaceScreen,
    }
    BINDINGS = [
        Binding("q", "quit", "Выход"),
        Binding("?", "help", "Помощь"),
        Binding("j", "nav_down", "Вниз"),
        Binding("down", "nav_down", "Вниз"),
        Binding("k", "nav_up", "Вверх"),
        Binding("up", "nav_up", "Вверх"),
        Binding("l,right,enter", "open", "Открыть"),
        Binding("h,left,escape", "back", "Назад"),
        Binding("b", "sidebar", "Sidebar"),
        Binding("[", "previous_section", "Предыдущий раздел"),
        Binding("]", "next_section", "Следующий раздел"),
    ]

    def __init__(self, api: HoardAPI | None = None) -> None:
        super().__init__()
        self.api = api or IPCStatusAPI()
        self.status: dict | None = None
        self.status_error: str | None = None
        self.notice: tuple[str, bool] | None = None
        self._refresh_lock = asyncio.Lock()
        self._operations: set[str] = set()
        self._startup_complete = False
        self._last_narrow: bool | None = None

    def on_mount(self) -> None:
        self.register_theme(BLACK_THEME)
        self.theme = BLACK_THEME.name
        self.push_screen("overview")
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
        await self.refresh_status()

    async def refresh_status(self) -> bool:
        """Perform at most one status request at a time."""
        if self._refresh_lock.locked():
            return False
        async with self._refresh_lock:
            try:
                status = await self.api.status()
            except Exception as exc:  # UI must stay responsive when IPC/network work fails.
                self.status_error = str(exc) or type(exc).__name__
            else:
                self.status = status
                self.status_error = None
            self._render_current()
        return True

    def _render_current(self) -> None:
        screen = self.screen
        if isinstance(screen, WorkspaceScreen) and screen.is_mounted:
            screen.render_status(self.status, self.status_error)

    def set_notice(self, message: str, *, error: bool = False) -> None:
        self.notice = (message, error)
        self._render_current()

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
        if isinstance(self.screen, HelpScreen) or section not in self.SCREENS:
            return
        current = getattr(self.screen, "section", None)
        if current != section:
            self.switch_screen(section)

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
        try:
            result = await self.api.ai_call(
                "ai_telegram_batch_preview", {**selector, "limit": 100, "offset": 0}
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
            TelegramAnalysisDialog(preview),
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
        self.push_screen(
            ConfirmDialog(
                "Отправить фотографии в DeepSeek?",
                "Возрастной допуск 18+ применяется автоматически. Фото будут отправлены "
                "DeepSeek; запрос может быть платным, автоматического повтора не будет.",
                confirm_label="Отправить",
            ),
            lambda confirmed: self.run_worker(
                self._start_legacy_telegram_analysis(author_id, values), exit_on_error=False
            )
            if confirmed
            else None,
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
        self.push_screen(
            ConfirmDialog(
                "Отправить фотографии в DeepSeek?",
                "В DeepSeek будут отправлены только показанные локальные фотографии "
                "подтверждённых профилей. Запрос может быть платным; автоматического "
                "повтора не будет. "
                "PSL субъективен и не является научной оценкой.",
                confirm_label="Отправить",
            ),
            lambda confirmed: self.run_worker(
                self._start_telegram_analysis(selector, choices, operation_id),
                name="telegram-analysis-start",
                exit_on_error=False,
            )
            if confirmed
            else None,
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

    def open_web_interface(self) -> None:
        try:
            from hoard.web import launch_web_process

            launch_web_process()
        except (OSError, RuntimeError) as exc:
            self.set_notice(str(exc), error=True)
            return
        self.set_notice("Локальный веб-интерфейс запускается в браузере.")

    @staticmethod
    def _is_text_entry(widget: Widget | None) -> bool:
        return isinstance(widget, (Input, TextArea))

    @staticmethod
    def _is_interactive(widget: Widget | None) -> bool:
        return isinstance(
            widget, (Button, DataTable, Input, ListView, OptionList, Select, TextArea)
        )

    def check_action(self, action: str, parameters: tuple[object, ...]) -> bool | None:
        if isinstance(self.screen, ModalScreen) and action in {
            "quit",
            "help",
            "nav_down",
            "nav_up",
            "open",
            "back",
            "sidebar",
            "previous_section",
            "next_section",
        }:
            return False
        if self._is_text_entry(self.focused) and action in {
            "quit",
            "help",
            "nav_down",
            "nav_up",
            "open",
            "back",
            "sidebar",
            "previous_section",
            "next_section",
        }:
            return False
        return super().check_action(action, parameters)

    def _sidebar(self) -> Sidebar | None:
        if isinstance(self.screen, WorkspaceScreen):
            return self.screen.query_one(Sidebar)
        return None

    def action_nav_down(self) -> None:
        focused = self.focused
        if self._is_text_entry(focused):
            return
        if isinstance(focused, (DataTable, ListView, OptionList)):
            focused.action_cursor_down()
        elif self._is_interactive(focused):
            self.screen.focus_next()
        elif sidebar := self._sidebar():
            sidebar.move(1)

    def action_nav_up(self) -> None:
        focused = self.focused
        if self._is_text_entry(focused):
            return
        if isinstance(focused, (DataTable, ListView, OptionList)):
            focused.action_cursor_up()
        elif self._is_interactive(focused):
            self.screen.focus_previous()
        elif sidebar := self._sidebar():
            sidebar.move(-1)

    def action_open(self) -> None:
        focused = self.focused
        if self._is_text_entry(focused):
            return
        if isinstance(focused, DataTable):
            focused.action_select_cursor()
        elif isinstance(focused, ListView):
            focused.action_select_cursor()
        elif isinstance(focused, OptionList):
            focused.action_select()
        elif isinstance(focused, Button):
            focused.action_press()
        elif sidebar := self._sidebar():
            sidebar.choose()

    def action_back(self) -> None:
        if self._is_text_entry(self.focused):
            return
        if getattr(self.screen, "section", None) != "overview":
            self.open_section("overview")

    def action_help(self) -> None:
        if isinstance(self.screen, HelpScreen):
            self.pop_screen()
        else:
            self.push_screen(HelpScreen())

    def action_sidebar(self) -> None:
        if not self._is_text_entry(self.focused):
            self.toggle_class("sidebar-hidden")

    def _move_section(self, offset: int) -> None:
        current = getattr(self.screen, "section", "overview")
        index = (section_index(current) + offset) % len(SECTIONS)
        self.open_section(SECTIONS[index].key)

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

    def on_unmount(self) -> None:
        for screen in self.screen_stack:
            if isinstance(screen, ActivityScreen):
                screen.stop_polling()
