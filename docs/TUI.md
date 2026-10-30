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
  orange block. Modes (e.g. Обслуживание: Задания / Профили / Медиа / Операции) are tabs.
* Numbers fade when they are zero and turn orange when they need attention.

## Pages and navigation levels

The outer Hoard level has exactly three entries: *Обзор* and *Активность* are compatibility aliases
for **Главная**, while **Telegram** and **AI** are tools. Opening Telegram replaces, rather than
duplicates, the sidebar with the tool's own workspace. The old keys still resolve:
`overview`/`activity` open Home, `telegram` opens Telegram Status, `sources` opens the nested
Sources page and `archive` opens contextual **Статус → Обслуживание**.

| Level | Page | Contents |
| --- | --- | --- |
| Hoard | **Главная** | wordmark, compact Telegram activity monitor and quiet queue/archive totals |
| Hoard | **Telegram** | entry into the Telegram workspace; defaults to Status |
| Hoard | **AI** | existing provider settings, records and chat |
| Telegram | **Статус** | live collector work plus read-only archive, FFmpeg-frame and PSL aggregates |
| Telegram context | **Обслуживание** | bounded global Jobs / Profiles / Media catalogs and explicit HTML, enrichment and FFmpeg operations |
| Telegram | **Источники** | paginated publication catalog, local search/filter/sort, stable edit/toggle/scan and publication-scoped photo/frame PSL actions |
| Telegram context | **Публикация** | four source-scoped read-only tabs: Кружки / Профили / Кадры / PSL |
| Telegram | **Web Viewer** | actual loopback-process state and enable/open/disable actions |
| Telegram | **Вход** | saved-session/live-connection state, cached public account data and the existing authorization dialog |

```
 hoard  ›  Telegram  ›  Источники                         ● служба доступна
 ╭ Telegram ────────╮ ╭ Источники ──────────────────────────────────────────────╮
 │ ← Назад в Hoard  │ │ Добавить  Изменить  Вкл./выкл.  Проверить  Проверить все│
 │ ▎1 Статус        │ │ Источник            Состояние  Кружки  Каждые  …        │
 │  2 Источники     │ │ Sample @chan/42     включён        31   120 с  …        │
 │                  │ │                                                             │
 │                  │ │                                                             │
 │  3 Web Viewer  ● │ │                                                             │
 │  4 Вход        ● │ ╰──────────── a добавить  e изменить  s проверить ────────╯
 ╰──────────────────╯
  tab панели  [ ] раздел  h назад                         : команды ? помощь q выход
```

The two lower entries are a separate flex child pinned to the available sidebar bottom, not blank
rows. On short terminals both option lists remain keyboard-reachable and do not overlap. The Web
dot is green only for confirmed ready state, red for stopped/error, and neutral while unknown or
transitioning. The account dot is green only for a verified authorized live client, yellow for an
unverified or intentionally disconnected saved session, red when a full login is needed, and
neutral when state cannot be established.

## Responsive and interactive rules

* Table columns are fitted to the actual page width. Secondary columns are shortened or dropped
  rather than creating a horizontal scrollbar; the same rule applies to Maintenance and AI lists.
* Maintenance has four compact tabs. Jobs, Profiles and Media request one server-paginated page
  only on entry, filter change or explicit refresh. Each catalog retains its own search draft,
  offset, filters and condition-keyed confirmed snapshot. Switching modes immediately installs the
  correct columns and either that snapshot or a loading/error row; rows and actions from another
  query or page are never reused under new conditions. Operations keeps HTML, enrichment, global
  FFmpeg and read-only batch history explicit; simply opening the page starts no work.
* Read-only AI batch history keeps catalog and participant offsets confirmed until their response
  succeeds. Back and Close invalidate pending participant generations, so a late response cannot
  reopen details, replace a newer batch or release the newer request's loading state.
* A table is auto-focused only when it is fully visible. On short terminals the page opens at the
  top (summary and actions first); `Ctrl+PageUp` and `Ctrl+PageDown` scroll the page even while a
  table or the sidebar owns the keyboard.
* When the sidebar is hidden by the responsive breakpoint, focus is moved out of it. It must not
  keep receiving `j`/`k` while invisible.
* Home and Telegram Status poll only bounded `status_brief` data while visible, share the app's
  one-request guard and stop their timers when hidden. The Status archive aggregates use the
  separate `telegram_statistics_get` request only on entry and explicit refresh; a failed refresh
  retains and marks the last confirmed snapshot. Neither surface queries Telegram or files.
  Detailed Maintenance/Sources data is loaded on entry. Operations reads FFmpeg state once and
  polls only while a confirmed active session remains visible. Web is checked once on first
  Telegram-workspace entry and reused by its page, without polling or autostart. Status repaint
  never rebuilds the sidebar, moves its highlight or steals a field's focus.
* Opening an AI conversation hides the list workspace, gives the message field focus and scrolls
  the conversation to its top. Returning restores the list and focus.
* Maintenance and Telegram Sources tables preserve stable-ID selection across refreshes and
  terminal resizes. Sources keys records by `sources.id`; a changed query clears an obsolete
  selection before any action can use it. Opening a publication keeps that Sources screen instance
  alive, so returning restores its query, filter, sort, offset, cursor and scroll.

The Home wordmark comes in two sizes (`widgets/hero.py`): six rows when the terminal is at least
30 rows tall and 76 columns wide, three rows below that, and none under 22 rows — the header
already carries the name there. Only the compact version is shown beside it when space permits;
collector state belongs to the monitor rather than being repeated by the logo.

## Home Telegram monitor

The Home monitor describes only the current scan: `progress.source`, comments `checked`, newly
saved video notes `downloaded`, and the current `active_downloads`. A total or remainder is shown
only when the daemon actually supplies one; message IDs, queue length and archive totals are never
used as substitutes. Queue and archive totals remain a separate compact Hoard-wide line.

The title dot is green only when the daemon confirms an authorized connected account and a running
collector, yellow for a confirmed account with the collector stopped or paused, red when login or
recovery is required, and neutral for unknown/IPC-unavailable state. The dot is unrelated to Web
Viewer state.

`progress.transfers` contains at most the active Telegram video-note downloads. Each entry is
identified by `(source_id, message_id)` and carries received `bytes` plus an optional positive
`total`. The monitor keeps a temporary identity-to-slot map: surviving transfers do not move when
another finishes, and the next transfer occupies the free slot. One horizontal strip always has
three segments. The collector may run four downloads (`HOARD_TG_DOWNLOADS=4`); the fourth remains
active and is included in the count without displacing the three visible slots. Unknown sizes use
a neutral activity mark, not a fabricated percentage. Slots are cleared on stop or IPC failure.

## Telegram Status

The upper **Текущая работа** panel reports only current `status_brief` facts: source, collector
state, scan stage, checked and newly saved counts, active transfers and aggregate speed. Unknown
remaining work stays `—`. The lower panel is an archival snapshot: all/enabled sources, saved
media and profile photos, unique profiles, exact saved frame sets and rows, frame-job states, and
PSL outcomes linked to actual analyses. FFmpeg activity and its explicit processing mode are
display-only. Refreshing statistics cannot start Telegram, FFmpeg, Web or AI work.

## Telegram Sources

Sources is a bounded catalog rather than a rendering of compatibility `status.sources`. On entry,
explicit refresh and mutations it requests at most 50 rows through `telegram_sources_overview`;
the daemon caps every request at 100. Search covers saved username, title, post ID and post URL.
The all/enabled/disabled filter and the added/last-check/download-count sort are applied in SQLite
before pagination. Search uses a 300 ms debounce, requests are serialized, and generation checks
discard stale results or replies that arrive after leaving the page. There is no polling timer.

Wide terminals show source, state, video notes, unique linked profiles, successfully saved FFmpeg
sets, confirmed PSL analyses and last check. Medium terminals keep source, video notes, profiles
and state; narrow terminals keep source and video notes. The selected-source panel adds interval,
collection mode, last result/error, photos of linked authors, actual frame rows and separate
failed/stale set counts. Enter or `l` opens the contextual publication viewer; `e` still opens
source settings. The viewer inherits the Sources navigation entry, never appears as a fifth
sidebar/palette item, and renders `Telegram › Источники › @channel/post` in the breadcrumb.

Creation retains the access-validation wizard. Editing first reads the current source by stable ID,
keeps the link immutable and calls `source_update` locally; enabled state, checkpoint, next run and
historical relations/media are unchanged. `Полный` schedules the existing profile-photo workflow.
`Минимальный` stores ordinary message identity and author/source links but does not fetch missing
sender details or schedule new automatic photo work. A shared author can still be scheduled once by
another full source. Existing photos are never copied or removed.

## Telegram Publication

Publication is one screen with four local read-only data tabs plus one explicit operation panel.
Кружки and Кадры use source-filtered daemon IPC in stable `message_id DESC` order; Профили reuses
`profile_summaries(source_id=...)` and its local username/name/ID search; PSL uses the union of
batch, video and analysis-photo source links with distinct analysis IDs and batch-author
consistency. Each tab requests 50 rows, the daemon caps pages at 100, and switching tabs does not
prefetch the other three. A refresh/search/page request made during an older request is retained as
one pending reload; the stale response cannot overwrite it. Returning to an unfinished tab starts
the missing request, while a valid completed snapshot remains available.

Space or **Выбрать / снять** marks the stable message/author ID under the current cursor; **Выбрать
видимые** affects only the loaded 50-row page, and **Снять все** clears that object type. Marks
survive pagination and sorting within one publication, never follow row indexes, are capped at 100,
and are cleared when the source changes. Кружки/Кадры expose confirmed **Извлечь выбранные**,
**Обработать публикацию**, source-scoped **Повторить ошибки** and hard **Остановить FFmpeg** actions.
The compact monitor reports the real session ID scope, completed/waiting/running/error totals and
current message. One 1.5-second status timer exists only while this screen is visible; leaving the
screen stops polling but not explicitly started daemon work.

Профили supports the same stable multi-selection and the `photos`, `frames` and `combined` image
modes. **Анализировать выбранных** sends `source_id + author_ids`; **Анализировать публикацию**
keeps the complete selector server-side. Both open the existing local preview before any external
request. A single profile may choose its exact ready video; batch rows show the actual selected
video and skip reason. The submit label states that images go to DeepSeek and may cost money.
Automatic 18+ admission remains limited to local user profiles; channels/chats remain ineligible.
The latest durable source-scoped batch is visible again after returning to the screen.

The frame table reads saved SQLite status only, including video notes with no frame-set row. Deep
MP4/size/mtime/SHA-256 validation runs only after explicitly opening one set, and the reused
`VideoFramesDialog` is view-only here. Failed, stale, ineligible, insufficient, invalid and
outcome-unknown PSL rows never expose scores. The detail view omits prompts, raw responses, costs,
tokens and private paths.

Video, photo and frame buttons send stable IDs rather than paths. The source ID is frozen at click
time before the asynchronous IPC call. The daemon checks publication
ownership, global-photo author membership, archive-root containment after symlink resolution,
regular-file type and known size before calling the platform opener without `shell=True`. Opening a
tab, detail card, preview or media item starts neither Telegram, Web, FFmpeg/FFprobe nor paid AI;
only the named confirmed operation controls do so.

## Keyboard

`j`/`k`, arrows — move · `l`, Enter — open · `space` — mark current publication object ·
`v` — mark visible page · `x` — clear marks · `h`, Esc — back · `tab` — next panel ·
`[` `]` — previous/next section of the current level · `1`–`9` — jump within the current visible
level · `:` or Ctrl+K — command palette · `b` — toggle sidebar · `?` — help · `q` — quit (the
daemon keeps running). From Publication, back opens Sources. From Sources/Web/Login, back opens
Status; from Status it restores the
global section from which Telegram was entered, falling back to Home. The explicit sidebar back
always leaves Telegram. Modals consume Escape first. Global keys are disabled in modals and while
typing, and `j`/`k`/arrows remain native to focused tables and lists.

## Responsive classes

`HoardApp.on_resize` sets classes that the stylesheet keys on, and calls the screen's
`on_app_resize()` so widgets that depend on density repaint:

| Class | Condition | Effect |
| --- | --- | --- |
| `narrow` | width < 96 | Home labels and shared action layouts tighten |
| `medium` | width < 110 | action grids drop to two or three columns |
| `compact` | width < 76 | one-column actions, tighter padding, the small wordmark |
| `sidebar-hidden` | width < 60 | sidebar hidden; use `[` `]`, digits or `:` |
| `short` | height < 30 | the small wordmark, statistics on one line |
| `tiny` | height < 22 | wordmark hidden, smaller tables |

## Adding a tool

1. Write a `WorkspaceScreen` subclass in `tui/screens/` (set `section`, optionally `HINTS`),
   compose its page from `Panel`s and register the concrete screen in `HoardApp.SCREENS`.
2. Append a global tool entry to `SECTIONS`. If it has an inner workspace, give the entry a
   concrete default `target`, declare only implemented child `Section`s with `parent=<tool key>`,
   and add their tuple to `WORKSPACES`. The sidebar, breadcrumb, level-local `[`/`]` and digits,
   aliases and palette then share the same routes.
3. Add extra entries to `HoardApp.commands()` only when the tool has a real global command. Home
   remains activity-first and does not grow a generic tool-summary panel.

Unfinished integrations must stay out of `SECTIONS` — the interface only shows what works.
