# Hoard TUI design

`hoard tui` is the full-screen Textual client. It is a thin IPC client of the daemon (see
`ARCHITECTURE.md`) and is designed so that Telegram is the *first tool*, not the whole product.

## Visual language

Black ground, one orange accent, two greys. Colour carries meaning; decoration does not. The
interface is meant to feel like a terminal program (htop, lazygit, k9s): everything lives in framed
panels with the title set into the border, shortcuts are shown as key chips, and selection is an
inverse bar.

| Colour | Role |
| --- | --- |
| `#000000` | every surface — enforced by tests for every widget and modal |
| `#FFFFFF` | primary text |
| `#888888` | labels, hints, inactive items, table headers |
| `#F79646` | where you are, what is alive, focus, errors, the primary action |
| `#7B4B23` | frames at rest and the logo's shadow (a dim shade of the accent) |
| `#262626` | hairlines and progress tracks |

* **Panels.** A `Panel` is a rounded frame with its title in the top border and, when it owns
  keys, the shortcuts in the bottom one (`a добавить  e изменить  s проверить`). At rest the frame
  is dim; as soon as something inside has focus (`:focus-within`) it turns orange, so the eye
  always knows where the keyboard is.
* **Selection** is an orange block (`reverse`) in the sidebar, in tables and on buttons; the
  background of the widget itself stays black.
* **Key chips.** In the footer a key is an orange block with the action next to it; the left side
  belongs to the screen, the right side is global (`: команды  ? помощь  q выход`).
* Actions are plain words in a row; the primary one is orange and the focused one becomes an
  orange block. Modes (e.g. Archive: Кружки / Профили / Задания) are tabs.
* Numbers fade when they are zero and turn orange when they need attention.

## Pages

One page per concern. Pages that used to be separate were merged: *Обзор* and *Активность* are
now **Главная**, and *Источники* lives on the **Telegram** page. The old keys still resolve
(`navigation.ALIASES`), so scripts, palette habits and tests keep working.

| Page | Contents |
| --- | --- |
| **Главная** | the HOARD wordmark with the daemon state; panels *Сейчас* (current task + progress), *Очередь*, *Архив*, *Инструменты* (one line per tool) and *События* |
| **Архив** | Кружки / Профили / Задания, catalogue actions, manual selected-frame extraction, confirmed bounded batch processing and hard stop; less frequent frame/profile AI commands sit behind **Ещё…** |
| **Telegram** | panels *Сборщик* (account and collector control), *Web* (separate loopback process) and *Источники* (table + add / edit / toggle / scan / frame AI) |
| **AI** | provider settings, record list and chat |

```
 hoard › Telegram                                                  ● работает      header
 ╭ Разделы ───╮ ╭ Сборщик ─────────────────────────────────────────────────────╮
 │ ▎1 Главная │ │ подключён · сборщик работает                                  │
 │  2 Архив   │ │ Подключить   Запустить   Пауза   Остановить                   │   panels
 │ ИНСТРУМЕНТЫ│ ╰───────────────────────────────────────────────────────────────╯
 │  3 Telegram│ ╭ Источники ────────────────────────────────────────────────────╮
 │  4 AI      │ │ Источник            Состояние  Кружки  Каждые  Проверка  …    │
 │            │ │ Sample  @chan/42    включён        31   120 с  2026-10-21 …   │
 ╰────────────╯ ╰────────────────── a добавить   e изменить   s проверить ───────╯
  a добавить  e изменить  s проверить  tab панели            : команды ? помощь q выход
```

The reference layout was designed before the Web process and video-frame workflow existed. Those
current features are integrated as additional panels/toolbars with the same framing, typography and
key-chip language: **Web** sits on Telegram, and frame inspection/backfill/AI actions sit inside the
Archive page. No old functional control was removed.

## Responsive and interactive rules

* Table columns are fitted to the actual page width. Secondary columns are shortened or dropped
  rather than creating a horizontal scrollbar; the same rule applies to Archive and AI lists.
* Archive has a compact primary action row. Frame backfill, pause/retry and the less common AI
  modes live behind the **Ещё…** tab, so opening the page does not turn the action block into a
  wall of controls. The advanced block is mode-specific and closes when the mode changes.
* A table is auto-focused only when it is fully visible. On short terminals the page opens at the
  top (summary and actions first); `Ctrl+PageUp` and `Ctrl+PageDown` scroll the page even while a
  table or the sidebar owns the keyboard.
* When the sidebar is hidden by the responsive breakpoint, focus is moved out of it. It must not
  keep receiving `j`/`k` while invisible.
* Home polls only bounded `status_brief` data, keeps one request in flight and skips unchanged
  redraws. Detailed Archive/Telegram data is loaded on entry; polling must not rebuild the sidebar
  or move the navigation highlight.
* Opening an AI conversation hides the list workspace, gives the message field focus and scrolls
  the conversation to its top. Returning restores the list and focus.
* Archive and Telegram tables preserve the selected record across status refreshes, terminal
  resizes and profile multi-selection.

The Home wordmark comes in two sizes (`widgets/hero.py`): six rows when the terminal is at least
30 rows tall and 76 columns wide, three rows below that, and none under 22 rows — the header
already carries the name there. The text beside it (tagline, version, collector state) degrades
by width, never wrapping over the letters.

## Keyboard

`j`/`k`, arrows — move · `l`, →, Enter — open · `h`, ←, Esc — back · `tab` — next panel ·
`[` `]` — previous/next section · `1`–`9` — jump to a section · `:` or Ctrl+K — command palette ·
`b` — toggle sidebar · `?` — help · `q` — quit (the daemon keeps running). Global keys are
disabled in modals and while typing in an input. A focused table keeps `←`/`→` for itself; use
`h` to go back.

## Responsive classes

`HoardApp.on_resize` sets classes that the stylesheet keys on, and calls the screen's
`on_app_resize()` so widgets that depend on density repaint:

| Class | Condition | Effect |
| --- | --- | --- |
| `narrow` | width < 96 | Home becomes a single column of panels |
| `medium` | width < 110 | action grids drop to two or three columns |
| `compact` | width < 76 | one-column actions, tighter padding, the small wordmark |
| `sidebar-hidden` | width < 60 | sidebar hidden; use `[` `]`, digits or `:` |
| `short` | height < 30 | the small wordmark, statistics on one line |
| `tiny` | height < 22 | wordmark hidden, smaller tables |

## Adding a tool

1. Write a `WorkspaceScreen` subclass in `tui/screens/` (set `section`, optionally `HINTS`),
   compose its page from `Panel`s and register it in `HoardApp.SCREENS`.
2. Append a `Section` to `SECTIONS` in `tui/navigation.py` (`group=TOOLS`; give sub-pages
   `parent=<tool key>`). The sidebar, breadcrumb, `[` `]`, digit shortcuts and the palette pick it
   up; the first nine sections get digits automatically.
3. Optionally add a one-line summary to `MODULE_SUMMARIES` in `tui/screens/home.py` so the tool
   appears in the *Инструменты* panel on Home, and extra entries to `HoardApp.commands()` for the
   palette.

Unfinished integrations must stay out of `SECTIONS` — the interface only shows what works.
