# Hoard 0.7.4.6

Hoard is a local-first Linux and Windows archive for Telegram posts, profiles and video notes
found in a channel post's comments. It uses Telethon, SQLite, an autonomous offline HTML catalog
and terminal interfaces. A private background daemon performs all Telegram work; closing an
interface does not stop downloads.

The 0.7.4.6 stabilization release gives the Maintenance Jobs, Profiles and Media tabs independent
search drafts and condition-keyed page snapshots. Switching tabs, paging, or returning before a
debounce completes can no longer expose rows or actions from another catalog. Read-only AI batch
history also rejects late participant responses after Back/Close and keeps confirmed offsets and
rows together when a page fails.

The 0.7.4.5 release completes the TUI migration. The global level is now **Главная**, **Telegram**
and **AI**; the former Archive screen is replaced by contextual **Telegram → Статус →
Обслуживание**. Its bounded Jobs, Profiles and Media catalogs expose durable queue details and
safe retry, legacy/unlinked profiles with global PSL, and all saved video notes without loading
the archive into the client. Operations retain explicit HTML rebuild/open, Telegram enrichment,
global FFmpeg backfill/retry/stop and read-only AI batch history. None starts on page entry.

FFmpeg status now separates completed, successful and failed work. A publication cannot stop a
session owned by another publication or the global archive; the daemon revalidates session ID and
scope at stop time. Source-scoped and global AI batch history is read-only, paginated and reveals
PSL fields only for valid eligible adult results. The old `archive` route remains a compatibility
alias to Maintenance, but it is no longer a sidebar or palette destination.

The 0.7.4.4 release turns the contextual **Telegram → Источники → Публикация** viewer into the
manual FFmpeg and PSL workspace. Stable multi-selection survives pagination inside one publication
and is cleared on source changes. Selected video notes, the current publication, and its failed
frame sets run through one existing media runner with an explicit per-session job allowlist, an
eight-job queued/running ceiling and one FFmpeg process. Stop remains hard, daemon restart revokes
permission, Telegram collection remains mutually exclusive, and no frame operation starts merely
by opening the screen.

Profiles can be selected individually, by visible page, or delegated as the complete publication
selector. Photos, one three-frame video set, or those frames plus one same-author photo pass through
the existing local preview and durable PSL batch; `source_id + author_ids` is validated again during
resume. The visible publication screen lightly polls only its active FFmpeg session and latest
source-scoped AI batch. Paid provider work still requires the preview's explicit send confirmation,
uses automatic 18+ admission for local user profiles, and never retries `outcome_unknown`
automatically. The release also guarantees the latest refresh/search after an in-flight page load,
reloads unfinished tabs on resume, and freezes source IDs before asynchronous media-open calls.

The 0.7.4.3 release adds the contextual **Telegram → Источники → Публикация** viewer. Enter opens
one saved publication while `e` remains edit; returning preserves the Sources query, filter, sort,
page and stable source selection. Four independently paginated tabs expose only that source's
video notes, linked profiles, lightweight stored frame states and confirmed PSL attribution. The
screen is read-only: opening it never contacts Telegram or starts FFmpeg, FFprobe or AI.

Local MP4s, profile photos and valid frames open only after an explicit action. The TUI sends
stable object IDs; the daemon resolves them through SQLite, verifies source/profile ownership,
resolved symlink containment in remembered archive roots, regular-file type and recorded size,
then invokes the platform opener without a shell. Frame lists do not hash files; deep three-frame
validation remains limited to explicitly opened sets. This release also excludes stale/failed
frame rows from Sources' saved-frame count and makes duplicate `source_add` side-effect-free while
reporting `created=false`.

The 0.7.4.2 release turns **Telegram → Источники** into a paginated publication catalog backed by
the dedicated `telegram_sources_overview` IPC operation. Search, enabled-state filtering and
stable sorting run in SQLite before the bounded 50-row page is returned. Each row and its compact
selection summary use publication-scoped download/profile/frame counts; PSL counts include only
unique analyses with a confirmed source link, a completed AI attempt and
`result_status='completed'`. No Telegram lookup, file scan, FFmpeg or periodic heavy refresh is
performed by the catalog.

Each source now stores `collection_mode=full|minimal`; existing rows migrate to `full`. Full mode
retains automatic profile-photo scheduling. Minimal mode still saves ordinary message identity,
the author/source relation and video notes, but performs no extra sender lookup and schedules no
new automatic photo job on that source's behalf. Shared authors remain global: any related full
source may schedule the one deduplicated photo job, while legacy profiles without source links
retain enrichment compatibility. Editing uses stable source ID and cannot change its post,
enable a disabled source, reset its checkpoint/next run or delete historical archive data.

The 0.7.4.1 release turns **Telegram → Статус** into a compact monitoring page. Its live area
polls only bounded `status_brief` data while visible; its archive statistics use a separate
read-only aggregate IPC on entry or explicit refresh. Source, media, profile, FFmpeg-frame and
PSL counts never require Telegram or filesystem scans. Web readiness is checked once on first
Telegram entry without starting it, the Header reports daemon availability, and cached account
identity is clearly separated from a verified live connection.

The 0.7.4.0 release introduces hierarchical TUI workspaces. Opening **Telegram** replaces the
global sidebar with **Статус**, **Источники**, and the bottom-pinned **Web Viewer** and **Вход**
destinations; legacy `telegram` and `sources` routes open the correct nested page. Collector,
source and PSL actions are preserved on dedicated pages. The account page distinguishes an
unverified saved session from a daemon-verified connection and shows only cached public account
details. Enabling Web waits for confirmed loopback readiness and then opens a fresh one-use URL;
a browser-launch failure leaves the server running for an explicit retry.

The global **Главная** is now activity-first: the HOARD wordmark, one compact Telegram monitor and
one quiet line of queue/archive totals. The monitor shows current scan counters and one horizontal
strip with three identity-stable download segments. Telegram may still run four concurrent
downloads; all four are counted while three remain visible. Unknown totals and remaining work are
shown as unknown rather than inferred from message IDs, archive totals or queue length.

The 0.7.3.0 release redesigns the explicit local Web client as **Black Archive 03**. It applies a
compact black-and-orange workspace to the existing catalog: publication cards, the open
publication, profile cards, the open profile with photos, video notes, ready frames, username
history and PSL history, the media viewer and the existing AI preview. Lists and long detail
sections now load incrementally while scrolling, preserving search, filters, loaded cards and
scroll position across opening a profile or publication and returning. The autonomous
`file://` catalog uses the same rendering and makes no extra HTTP requests. No Telegram,
daemon, SQLite, AI protocol, FFmpeg or TUI behavior changed; video frames remain strictly
manual and Web never starts extraction.

This release adds one minimal AI workflow across the TUI and an explicit local browser client.
It combines saved publications, profiles, photos, video notes, extracted video-note frames and
explicitly requested PSL history. The portable `telegram_archive.html` remains fully autonomous and makes no browser
network requests. `hoard web` is a separate, short-lived loopback client; AI analysis is never
started automatically. The project has no persistent or remotely accessible web service, Redis,
Docker or Electron.

The 0.7.2.4 patch makes video-frame extraction strictly manual and removes the largest measured
TUI polling costs. New and interrupted frame jobs remain visibly `waiting`; FFmpeg/FFprobe start
only from **Извлечь кадры** or a confirmed **Обработать ожидающие** / **Повторить ошибки** action.
Restart and generic job retry never preserve execution permission. Telegram collection and FFmpeg
are mutually exclusive: a frame action asks the user to pause Telegram, while an explicit Telegram
start hard-stops and awaits frame processing before downloads resume. **Остановить обработку**
terminates the current subprocess, removes unpublished temporary frames and preserves the MP4,
completed frames and resumable cursor. Closing only the TUI still leaves an explicitly started
daemon-side frame session running.

Home now polls a compact `status_brief` snapshot, skips unchanged redraws and loads detailed archive
data only when a relevant page opens. The full `hoard status` contract remains available with
bounded recent records. Frame statistics use one aggregate query, idle frame scheduling performs no
database work, and recent profile summaries aggregate only the requested rows. Frame controls use a
dedicated Telegram IPC call instead of the AI-only boundary. FFmpeg runs with one decode/filter
thread and a best-effort lower Linux priority.

The 0.7.1.1 patch makes `hoard web` open directly on profiles that have readable local photos.
Their newest available photo is shown as the card avatar; opening a profile shows its complete
paginated set of available local profile photos. Resetting the photo filter reveals profiles that
do not yet have a saved avatar.

The 0.7.1.2 patch installs `cryptg` with Hoard. Telethon detects it automatically and uses its
native AES-IGE implementation for Telegram transport encryption; Hoard does not replace or wrap
Telethon's cryptography. This reduces local cryptographic processing time for video notes, profile
photos and post media. End-to-end download speed can still be limited by the network, Telegram
and Telethon's request sizing. Telegram jobs remain serialized around one authorized client.

The 0.7.1.3 patch requests video-note documents from Telegram in 512 KiB parts instead of
Telethon's 128 KiB default for files up to 100 MiB. It uses the same Telethon primitive as
`download_media()` and passes message context for file-reference refresh, DC migration, CDN and
retry handling. Hoard checks that private Telethon contract before using it and falls back to the
original `download_media()` path if the installed client or media is incompatible. Profile photos
and post media are unchanged. Larger requests reduce sequential round trips but do not guarantee
the same end-to-end improvement on a real Telegram connection.

The 0.7.1.4 patch downloads up to three independent video notes concurrently inside one source
scan while retaining the single authorized client and global Telegram lock. Scheduling is bounded,
and the source cursor advances only across the continuous successfully processed traversal prefix:
a later completed file is registered durably but cannot hide an earlier failure. Set
`HOARD_TG_DOWNLOADS` before daemon startup to select `1` through `4`; the default is `3` and any
other value fails with an explicit configuration error. This limit is independent of the generic
job-runner and AI limits.

The 0.7.1.5 patch coalesces ordered scan checkpoints into bounded batches, while a short timer
persists a partial batch during slow Telegram iteration. Successfully written files are still
registered independently before cursor movement, and a failed, deferred or unfinished transfer
still blocks the cursor without discarding later durable files. Profile history updates now use
equivalent SQLite upserts, archive validation uses one filesystem stat per saved file, and empty
retry cleanup no longer opens a write transaction.

The 0.7.1.6 patch makes autonomous catalog delivery recoverable. While the collector is running,
its existing job loop reconciles every durable dirty marker into one deduplicated report job,
including changes completed by the independent AI runner and changes left across a restart. A
short cooldown bounds rebuilding under continuous mutations, while filesystem failures use a
durable 30-second-to-1-hour backoff; explicit manual builds remain available immediately. Report
cancellation now waits for the real worker thread before releasing its writer lock. Reusing the
same normalized archive root or refreshing only unpublished metadata timestamps no longer creates
false catalog revisions.

The 0.7.1.7 patch hardens the same boundary. Repeated cancellation of a report waits until the
HTML writer has really stopped, and concurrent pause/shutdown requests cancel a job runner only
once. A temporary SQLite error in the job scheduler now produces an event and a bounded retry
delay instead of silently stopping all queued work. Catalog generation is serialized for both the
daemon and a direct `hoard telegram report` by an interprocess lock acquired before the SQLite
snapshot, so an older snapshot cannot be published after a newer one. Retrying a failed report no
longer waits for the automatic backoff or creates a duplicate active job, and a successful report
clears the previous diagnostic error.

The 0.7.1.8 patch comes from a live Telegram/archive pass. Web startup now publishes explicit
`preparing`, `ready` and `error` states, waits through the real HTML build instead of failing
after five seconds, reuses a healthy server and never leaves an orphan child on timeout. The TUI
shows preparation and the resulting URL. Telegram video avatars are downloaded through a still
`PhotoSize` sibling, so their `.jpg` files are real images rather than MP4 content that a browser
cannot display; existing broken video-avatar records are repaired on their next profile check.
A transfer that makes no progress for two minutes is aborted with a retryable network error
instead of holding the collector indefinitely after a dropped connection.

The 0.7.1.9 patch closes the remaining startup and transfer races. Web state files are published
atomically and carry a launch identifier owned by the spawned server; the parent writes only the
pre-spawn record and never overwrites a child's `preparing`/`ready` state. The launch lock is held
until the server is ready, so concurrent requests reuse one process. Telegram inactivity now
tracks strictly increasing received bytes: repeated progress callbacks with the same count no
longer postpone the two-minute timeout. Cancellation is awaited until the transfer really stops
before the stream is closed or `.part` is removed; Telethon does not suppress cancellation, and
an arbitrary third-party coroutine that does would be allowed to finish rather than risk writing
into a reused file.

The 0.7.1.11 release adds complete Telegram Web lifecycle control and live catalog refresh.
The Textual Telegram screen can enable, open and stop the separate loopback Web process; it
survives closing the TUI. Each open receives a fresh one-time bootstrap URL through a private
authenticated loopback control channel, while used URLs stay invalid and existing browser
sessions remain valid. A managed Web runs until an explicit stop, daemon shutdown or crash,
independent of the browser-session lifetime. The open page polls a small catalog-version endpoint
and fetches a new normalized snapshot only after a complete HTML publication; data, indexes,
counters and media allowlists are replaced without a full page reload, while search, filters,
pagination, selection, scroll and active media playback are preserved.

The 0.7.2.3 patch fixes the interaction and layout regressions found in the Noir v2 port. Home
polling no longer resets a sidebar selection every 1.5 seconds; Archive reloads, terminal resizes
and profile multi-selection keep the table cursor and scroll position. Action rows are allocated
by the available terminal width, and table columns are fitted or reduced instead of producing
horizontal scrolling. Archive’s advanced frame/profile commands are grouped behind a visible
**Ещё…** control, while the primary actions stay on one compact row. On short terminals pages
open at the top instead of auto-scrolling a barely visible table, `Ctrl+PageUp` / `Ctrl+PageDown`
scroll the page, hidden sidebars cannot keep keyboard focus, and the AI chat pane replaces the
list view and focuses its message field immediately.

The 0.7.2.2 release ports the Hoard TUI to the Noir v2 terminal-native design while preserving
all current 0.7.2.1 functionality. The six former pages become four: **Главная** merges Overview
and Activity, and **Telegram** now owns both the account/collector and Sources. The old
`overview`, `activity` and `sources` navigation keys still resolve as aliases. Every page is built
from framed panels whose titles sit in the border, focus lights the active frame, and the footer
shows screen shortcuts as key chips. A command palette (`:` or Ctrl+K), digit page shortcuts,
responsive tables and the large/small HOARD wordmark are included. Web lifecycle controls retain
their own panel on Telegram; Archive keeps frame inspection, frame AI and bounded backfill actions
integrated into the same visual language, with no functional regression.

The 0.7.2.1 patch tightens two frame-processing boundaries. When AI analysis starts from
one saved publication, frame selection is restricted to video notes from that same source in
preview, queueing and durable batch resume; an explicit foreign `video_id` is rejected and no
newer video from another publication can be substituted. Profile-only analysis keeps access to
all of that profile's saved video notes. The old-archive backfill now adds work only into free
slots of an eight-job queued/running budget rather than adding another fifty jobs; new download
jobs keep their immediate path and are not delayed by old work. The cursor advances
only across scanned records, stops before unscanned work when the budget is full, and keeps
failed or already-ready records from stalling the traversal. Its pause is explicitly soft:
new backfill work stops, while the current FFmpeg process and already queued jobs finish normally.

The 0.7.2.0 release originally added automatic local frame extraction for every newly saved Telegram
video note. A separate media job runs at most one FFmpeg process, writes three JPEG frames at
20%, 50% and 80% of the real duration, and never delays the Telegram download path. A failed or
cancelled extraction keeps the source MP4 and remains retryable. FFmpeg and FFprobe are optional
runtime tools: without them the rest of Hoard continues to work and the UI explains how to
retry after installation. Previously saved video notes are not mass-processed on upgrade; the
Archive screen has an explicit resumable **Обработать старые кружки** action. Version 0.7.2.4
supersedes the automatic-start and soft-pause policy with the manual policy described above.

Frames are stored next to their MP4, represented in SQLite, and published through the existing
atomic catalog path, so an open Telegram Web receives them without a page reload. The TUI and
Web show all three frames separately from profile photos. Explicit Telegram AI analysis can use
profile photos, three frames from one selected video note, or three frames plus one profile photo
of the same author. Frames are never mixed between people. All three frames are sent in one user
message and one AI attempt; downloading a video never starts a paid request. Frame/photo
fingerprints are checked again after preview, and existing operation-id, queue, retry, history and
cost accounting rules remain in force.

The 0.7.1.10 release makes the existing full-screen Textual workspace the only interactive
terminal interface. `hoard`, `hoard ui` and `hoard tui` open the same Textual application; `ui`
and `tui` remain compatibility aliases. The old inline Rich/InquirerPy client and its InquirerPy
dependency are removed. Service commands (`status`, `start`, `stop`, `scan`, `add`, `retry`,
`telegram`, `web`, `shutdown`) remain non-interactive and do not import Textual. The Archive
screen also retains a direct “Открыть HTML” action for an existing local catalog.

## Terminal interface

```bash
hoard
```

The global level has three entries: **Главная** (HOARD wordmark, Telegram activity monitor and
compact queue/archive figures), **Telegram** and **AI**. Opening Telegram switches the same
sidebar to its workspace:
**Статус**, **Источники**, and bottom-pinned **Web Viewer** and **Вход** entries plus a direct
**Назад в Hoard** action. Former keys remain compatible aliases: `overview`/`activity` open
**Главная**, `telegram` opens **Telegram → Статус**, `sources` opens
**Telegram → Источники**, and legacy `archive` opens contextual **Обслуживание**.

Telegram Status owns collector start/pause/stop, a live current-work summary, and archive
statistics refreshed only on entry or request. Sources is a paginated local catalog with search,
enabled-state filtering, stable sorting, editing, selected/global scans and publication-scoped PSL
actions. Enter opens the selected publication viewer; `e` edits it. The viewer's Кружки, Профили,
Кадры and PSL tabs load one bounded server page at a time and retain their own offsets.
Login reuses the daemon authorization flow, shows cached public account details, and separates a
saved but unverified session from a verified live connection. Web Viewer starts/opens/stops only
the separate loopback process. Status opens contextual **Обслуживание** for paginated global jobs,
profiles and media plus explicit archive operations. All operations use private daemon IPC; the TUI never opens SQLite
or creates a Telegram client. Visible Home and Telegram Status each own a bounded compact-status
timer only while on screen. Maintenance catalogs load only on entry, filtering or explicit refresh;
only a visible active FFmpeg session is polled. Sources uses its dedicated bounded IPC without
polling. Web readiness is checked once on first entry to the Telegram
workspace. See `docs/TUI.md`.

The interface keeps the entire Textual surface, including navigation, focus, hover, palette and
help modal states, on `#000000`. Text is white, secondary text `#888888`, frames `#7B4B23`,
hairlines `#262626` and the accent `#F79646`. Pages are framed panels with titles in the border;
the focused panel lights up, the footer shows key chips, tables and buttons use an orange selection
block. Use `j`/`k` or arrows to move, `l` or Enter to open, and `h` or Esc to return.
Inside Telegram, back moves from Publication to Sources, from other nested pages to Status and
from Status to the previous Hoard section. `1`–`9` and `[`/`]` address only the visible navigation
level. Use `:` or Ctrl+K for the
command palette, `?` for help, `b` to toggle the sidebar and `q` to close only the UI. Section
cycling keeps working when the sidebar is hidden;
`Ctrl+PageUp` / `Ctrl+PageDown` scroll the current page. On terminals narrower than 60 columns the
sidebar hides automatically. Action grids and table columns adapt to the available width, dropping
or shortening secondary columns instead of scrolling sideways.

`hoard ui` and `hoard tui` are compatibility aliases for the same Textual application.
Scripts and all existing non-interactive commands are unchanged, do not require a TTY and do not
import Textual. Closing the TUI never stops the daemon or an already running Web session; reopen
`hoard` to reconnect over the existing private IPC socket.

## Install on Linux

Hoard requires Python 3.11 or newer and a terminal. On Arch/EndeavourOS, install Python and
Git first, then run:

```bash
git clone https://github.com/saidovux/hoard.git
cd hoard
python -m venv .venv
source .venv/bin/activate
python -m pip install --upgrade pip
python -m pip install -e .
hoard
```

For development, install the test tools with `python -m pip install -e '.[dev]'`.

An upgrade keeps the existing XDG configuration, database, Telegram session and archive.
If an older daemon is still running, execute `hoard shutdown` before starting the upgraded
installation. The 0.7.4.6 client accepts only a daemon reporting the same application version and
IPC protocol `1`; it reports the mismatch instead of starting a competing service. If a matching
daemon already owns the service lock but is still initializing, clients wait for it and show a
specific startup diagnostic.

## Windows installation (PowerShell)

Install Python 3.11+ and Git; open Windows Terminal:

```powershell
git clone https://github.com/saidovux/hoard.git
cd hoard
py -3 -m venv .venv
.\.venv\Scripts\python.exe -m pip install -e .
.\.venv\Scripts\hoard.exe
```

No PowerShell execution policy changes are needed. Windows stores credentials in
`%APPDATA%\Hoard` and data and service state under `%LOCALAPPDATA%\Hoard`.
The background service binds only to 127.0.0.1 using a random port and a
per-start token saved in `state\endpoint.json`. Keep the token file private.
Use `hoard shutdown` to fully close the service. Windows CI does not test live Telegram.

## First launch and Telegram login

Run `hoard`, open **Telegram → Вход** and choose **Подключить**. Hoard performs the complete login
as one flow through the background daemon:

1. It checks the saved session and finishes immediately if that session is still valid.
2. On first use it asks for the API ID and API Hash from
   [my.telegram.org/apps](https://my.telegram.org/apps).
3. It asks for the phone number, requests one Telegram code and immediately asks for it.
4. If the account uses two-step verification, it asks for that password next.
5. It verifies the session and returns to the task that initiated login.

API Hashes, login codes and 2FA passwords use hidden prompts and are not written to logs.
Cancelling login leaves the daemon running and does not delete an existing session. Invalid
codes and passwords can be retried; an expired code is requested again only after confirmation.
Every Telegram network step displays a spinner, elapsed seconds and an explicit network/VPN hint,
so a blocked connection no longer looks like an empty terminal. Ctrl+C cancels the login flow
without stopping the daemon.

Only the daemon opens the Telethon session, so the interface never creates a competing
Telegram client.

## Add a post and archive video notes

Open **Telegram → Источники** → **Добавить**. The wizard:

1. accepts a public post URL such as `https://t.me/examplechannel/123`;
2. connects Telegram if needed and verifies that the account can access the post;
3. asks for the archive folder;
4. uses a 120-second interval or lets you enter 30–86400 seconds;
5. chooses full or minimal profile collection;
6. saves the new source, schedules its nearest check and offers to start the collector.

**Проверить выбранный** queues only the highlighted post and uses the same unique job key as the
scheduler, so repeated presses cannot create parallel duplicate scans. **Проверить все** keeps the
existing global behavior. Editing is a separate local operation: the post URL is immutable, while
title, archive directory, interval and collection mode may change. It preserves enabled state,
checkpoint and next run and never moves or deletes existing files. Full collection schedules the
existing bounded profile-photo checks. Minimal collection saves visible sender identity and the
source relation but performs no additional profile lookup or automatic photo scheduling.

Hoard reads the linked comment thread and stores Telegram round video messages. Each MP4 gets a
JSON sidecar containing the source post, comment link, message ID, date and the visible sender
name/username and ID when available. Profile identity is keyed by Telegram ID, so a username
change does not split one account into multiple catalog entries. Observation dates are labels for
when Hoard saw a username, not claims about the exact time it changed.

The report is one portable HTML file. New media objects use stable ID-based paths beside it:

```text
<archive>/
├── telegram_archive.html
└── hoard_objects/
    ├── video_notes/source_<id>/author_<id>/message_<id>.mp4
    ├── profiles/author_<id>/photos/photo_<id>.jpg
    └── posts/source_<id>/media_<id>.<ext>
```

SQLite checkpoints and file-size checks prevent duplicate downloads and recover missing or
truncated archive files. Progress retains one compatible current-file view with its byte count and
speed, plus active-transfer count and aggregate speed. The bounded `progress.transfers` list gives
the TUI independent `(source_id, message_id, bytes, total)` snapshots without paths or history.
Percentages are derived only when Telegram supplies a positive total; no remaining count is
fabricated.

Existing pre-0.4 paths are not moved, copied or deleted. Their SQLite records and safe JSON
sidecars are migrated in place and referenced by the catalog. Existing source cursors are kept.

## Offline HTML catalog

Choose **Обновить HTML-каталог** in the terminal interface or run:

```bash
hoard telegram report
hoard telegram report --archive ~/Videos/hoard
```

The command reads SQLite and rebuilds the snapshot without starting the daemon, connecting to
Telegram or requiring internet access. Open `<archive>/telegram_archive.html` directly through
`file://` in Firefox, Chromium or another local browser. CSS, JavaScript and the normalized
SQLite snapshot are embedded in that single HTML file; it has no companion site assets, web
server, `fetch`, CDN, analytics or external font dependency. Media remains as local files beside
the report rather than being embedded as Base64.

The **Источники** view presents saved posts as responsive cards with channel identity, text,
publication/check dates, local media preview and profile/video counts. **Все профили** deduplicates
people by stable Telegram `author_id`, so a username change does not split history. Search includes
IDs, names, current and observed usernames and related sources. Profile filters combine source,
photo/video availability, successful PSL, Raw/Boosted PSL and adult-age ranges. Name, username,
ID, discovery/activity dates, media count and PSL fields are sortable; missing numeric values are
always last. Post search and sorting remain available independently.

Post and profile pages preserve list query, filters, sorting, loaded batches and scroll context.
Browser Back/Forward works through local hash history for `Источники → Пост → Профиль` and
`Все профили → Профиль`. Photo galleries append bounded batches while scrolling, lazy-load images
and open a keyboard-accessible viewer with arrow navigation and Escape. Video notes use HTML5
controls, never autoplay and retain `preload="none"`; their saved comment date and original
Telegram link are shown. Missing or rejected local paths render an inert placeholder.

Completed Telegram PSL analyses are exported in one batch and joined only by `author_id`. A
profile's current value is the newest deterministically ordered result whose analysis and attempt
both completed and whose adult-only PSL fields still pass the versioned validator. A later failed,
unknown, ineligible or invalid attempt remains in compact history without replacing that value.
Possible-minor ages, provider payloads, chat text, prompt content, memory, API keys and private AI
attachment paths are never exported. The catalog labels PSL as subjective and non-scientific.

**Telegram → Статус → Обслуживание → Операции** can rebuild the snapshot or independently open
the already published file after daemon-side path and regular-file validation.

For maintainability, source HTML, CSS and JavaScript remain separate and are packaged at
`src/hoard/templates/telegram/`; the generator assembles them only at report time. Each report
atomically replaces `telegram_archive.html` while preserving `hoard_objects/`. SQLite remains the
source of truth. A monotonic catalog revision prevents a build from clearing the dirty marker
when Telegram or PSL data changes during generation; the daemon then coalesces and rebuilds the
newer snapshot. Media from the short-lived split layout `<archive>/telegram/objects/` is safely
moved into `hoard_objects/` and its SQLite paths are updated; unrelated legacy archive layouts
remain in place. Conflicting or unexpected files are preserved instead of overwritten.

Moving the complete archive keeps new relative `hoard_objects/` links working. Older media stored
outside the selected archive may require its original absolute location (or drive on Windows), so
one HTML file by itself is not promised to contain or relocate media. The catalog warns that it
contains personal data and should be reviewed before sharing.

## Local interactive Web

Run the browser workflow explicitly:

```bash
hoard web
```

Hoard rebuilds the same catalog through daemon IPC, binds a local HTTP server to a random
`127.0.0.1` port and opens it in the default browser. The browser view reuses the packaged catalog
HTML, CSS and JavaScript; no CDN or third-party browser dependency is loaded. A one-time bootstrap
URL creates an HttpOnly, SameSite session cookie. Exact Host and Origin checks, a separate CSRF
token, a strict content security policy and closed write routes prevent unrelated local pages from
driving Hoard. The server never listens on a LAN address. The direct `hoard web` command keeps
`--no-open` and its idle `--lifetime`; Web enabled from **Telegram → Web Viewer** waits for the
confirmed ready state, opens the browser automatically and then uses a manual lifetime until
explicitly stopped. Closing the TUI leaves it running. Each **Открыть** asks the running
process for a fresh one-time URL; used URLs are rejected and do not invalidate other browser
sessions. A fresh URL and the stop command are available only through a separate authenticated
loopback control channel advertised in the private mode-`0600` state file. The browser page never
receives that token and has no server-stop route.

While the browser is visible it polls a tiny published-catalog-version endpoint. It requests the
new normalized snapshot only when the atomically replaced HTML changes, then updates collections,
search indexes, counters and media allowlists in place. Existing filters, search, loaded batches,
selection, route and scroll are retained; an open image viewer or playing video defers only the
visual rebuild. New Telegram and completed AI data therefore appear without a page reload, even
when the TUI is closed. The autonomous `file://` catalog remains independent and makes no local
HTTP requests.

Interactive mode adds contextual actions to saved publications and profiles. It can analyze one
profile, selected profiles, or every locally linked profile of a saved publication. The image
source is explicit: saved profile photos, three frames of one selected video note, or those three
frames plus one profile photo of the same author. Before any provider request it shows a local
preview, lets the user choose a profile's photos or a ready video-note frame set, applies the
configured automatic 18+ admission to local user profiles, and places the external-transfer and
possible-cost disclosure directly above one explicit send button. There is no second confirmation
dialog. Unknown post links do not start Telegram collection.
Progress shows queued, completed, skipped and failed profiles, each skip reason and an estimated
USD cost when a matching user tariff exists. Otherwise cost is explicitly unknown. Finished
results refresh automatically without a manual HTML rebuild. The browser remembers one active
batch in `sessionStorage`, resumes polling after reopening the page and keeps the final result
visible once; the TUI can browse active and historical groups from **AI** or publication-scoped
history from **Telegram → Источники → Публикация**.

The autonomous `<archive>/telegram_archive.html` does not contain a session, API key or active
backend configuration and keeps working through `file://` exactly as before. Closing the Web or
TUI client does not stop the daemon or its durable AI groups. Browser video cards show the three
extracted frames; the frame-specific AI action uses that exact local set.

## Video-note frames and FFmpeg

Each newly downloaded video note gets a durable local `waiting` record, not execution permission.
Daemon/TUI startup, a download, restart and generic job retry never start FFmpeg or FFprobe. In
**Telegram → Источники → Публикация**, use a selected/publication action. For the whole archive,
confirm the bounded resumable action in **Telegram → Статус → Обслуживание → Операции**. In the
publication workspace,
mark one or more visible/local video notes and use **Извлечь выбранные**, or confirm
**Обработать публикацию** / source-scoped **Повторить ошибки**. A separate one-slot media runner uses
system `ffprobe` to read the actual duration and `ffmpeg` to save three JPEG frames at 20%, 50%
and 80%. It uses one decode and filter thread, gets a best-effort lower priority on Linux, never
runs inside the Telegram event loop, never modifies the MP4 and never starts an AI request. Frames
live next to their source as
`hoard_objects/video_notes/source_N/author_ID/message_M_frames/frame_01.jpg` through
`frame_03.jpg`; SQLite stores their positions, timestamps, sizes, SHA-256 values and the source
fingerprint. If three distinct decodable frames cannot be produced, the set is marked failed
instead of reporting a fabricated partial success.

FFmpeg is optional for the rest of Hoard. Install it with the system package manager, for example:

```bash
sudo pacman -S ffmpeg
# Debian/Ubuntu: sudo apt install ffmpeg
# Windows: winget install Gyan.FFmpeg
```

Existing video notes are not mass-processed after an upgrade. **Обработать ожидающие** requires a
confirmation that shows the pending count and CPU warning; **Повторить ошибки** does the same for
failed/stale sets after fixing the dependency. **Остановить обработку** revokes the current manual
permission, terminates and reaps FFmpeg/FFprobe, removes unpublished temporary files and returns
unfinished jobs to `waiting` without rewinding the durable cursor. No frame session resumes after a
daemon restart. Telegram collection and FFmpeg cannot be active together; starting Telegram has
priority and first completes that hard stop. Processing continues in the daemon when only the TUI
or Web is closed and does not download the MP4 again. Frame completion marks the catalog dirty;
publication is coalesced for a manual batch, and an open Web receives the new frames without a
reload. The autonomous `file://` catalog also displays frames when their files remain inside the
archive.

Selected runs are capped at 100 unique message IDs and daemon-validates that every ID belongs to
the source. A process-local job allowlist prevents the shared runner from consuming work from any
other publication. Publication runs keep a source-scoped durable cursor and authorize only free
slots under the existing eight queued/running job ceiling; they never pass thousands of IDs through
IPC. The publication monitor polls only while that screen is visible. Leaving or closing the TUI
does not stop the explicitly started daemon session.

Optional enrichment obtains post text/media and up to 30 latest profile photos visible to the
authorized account:

```bash
hoard telegram enrich
```

Enrichment is queued separately from video-note scans, respects Telegram privacy and FloodWait,
deduplicates media IDs and does not stop the primary scan when a profile photo is unavailable.
The collector automatically queues an initial photo check for profiles already stored in SQLite
and durable retries when a temporary failure becomes due; it does not rewind source checkpoints.
Authorization, unavailable-peer, access/privacy, FloodWait and network failures retain separate
retry categories. A local archive write error fails the photo job visibly instead of being
reported as a network/VPN problem.
Start the collector first if it is stopped. Stories, hidden data and unrelated message history
are not requested. The offline report remains usable without enrichment.

## Hoard AI workspace — 0.5.0

`hoard.ai` is an independent subsystem; it does not import Telegram or change Telegram archives.
DeepSeek is its first provider through a generic provider interface. The default model is
`deepseek-flash`. The core accepts text and explicitly selected JPEG, PNG, GIF or WebP images, and
supports normal responses and JSON Output with local Draft 2020-12 JSON Schema validation.

Set `DEEPSEEK_API_KEY` for the daemon environment, or choose **Добавить API-ключ** in TUI/Web to
store it in the private `~/.config/hoard/ai.json` file. First-run onboarding asks only for the key;
models, tariffs, prompts, memory and concurrency remain optional advanced tools. The UI labels a
new key as saved but unverified until a provider request succeeds and distinguishes an
authentication error. The key field is masked and the daemon never returns the saved secret. The
key is never placed in job payloads, responses, HTML or public logs. AI requests have no automatic
provider retry: an error, truncated response, empty response or interrupted paid job requires an
explicit user decision. Configuration metadata reports only the key source: `env`, `file` or
`none`; an environment key takes precedence and cannot be overwritten from the UI.

The single Hoard daemon owns two independent job pools. `hoard start`, `stop` and `pause` control
the Telegram pool; the AI pool follows the daemon lifecycle and keeps working when the Telegram
collector is stopped. Their concurrency limits are separate: `HOARD_MAX_JOBS` for the existing
pool and `HOARD_AI_MAX_JOBS` (default `2`) for AI. A long DeepSeek request therefore cannot occupy
a Telegram worker or Telegram lock.

`HOARD_TG_DOWNLOADS` is a third, narrower limit: it controls only simultaneous video-note
transfers within one serialized Telegram scan (`1` to `4`, default `3`). It neither increases the
number of JobRunner workers nor permits another Telegram job to bypass the global session lock.

The AI screen provides persistent chats with rename/archive, paginated history, text and up to
four explicitly selected local images, provider/model/prompt-version selection, versioned prompt
editing and activation, scoped memory, tariff entry, general/Telegram statistics and attempt
state. Opening a chat or a saved result never sends anything. **Отправить** first shows exactly
what crosses the external-provider boundary and may cost money. Only the confirmed send creates a
paid operation with a unique `operation_id`, frozen context and private attachment references.

The daemon records `queued`, `running`, `completed`, `failed` and `outcome_unknown` attempts.
Duplicate operation IDs resolve to the original attempt, only one generation can run per chat,
and an interrupted or connection-ambiguous request is never retried automatically. An explicit
`ai_attempt_retry` creates a new attempt and job. Provider-reported prompt, completion, cache-hit,
cache-miss and reasoning counters follow the official
[DeepSeek Chat Completions usage schema](https://api-docs.deepseek.com/api/create-chat-completion/)
and remain unknown when omitted. Usage returned with an invalid JSON, schema mismatch, empty
result or incomplete result is retained because the call may have been billable.

Tariffs are local user data selected by provider, actual model and effective period. Values are
stored and calculated with decimal arithmetic. Hoard ships no assumed tariff, performs no
currency conversion and labels the result as an estimated cost. Missing usage or tariff
components produce `partial` or `unknown`, never a fictional zero; completed attempts retain the
exact tariff snapshot used at the time.

The bounded daemon IPC contracts used by the UI include:

```text
ai_config_get/set
ai_prompt_create/get/list/update/delete/versions/version_get/activate
ai_chat_create/get/list/update/delete/history/send
ai_memory_create/get/list/update/delete
ai_attachment_import/get/delete
ai_price_create/list/delete
ai_attempt_get/list/retry  ai_job_state  ai_statistics
ai_telegram_profile_get/list  ai_telegram_source_get  ai_telegram_photos/photo_get
ai_telegram_psl_start  ai_telegram_analysis_get/list
ai_telegram_batch_preview/start/get/list
telegram_frames_selected_start/source_start/source_retry_failed/session_status
```

Lists and history are paginated, and both IPC directions retain the 64 KiB message limit. New chat
image jobs store only attachment IDs; the explicitly selected image is copied once into a private
local directory instead of being duplicated as Base64 in SQLite jobs. Identical content reuses
one attachment row/file and is deleted only while unreferenced. Batch preview returns compact
photo IDs and counts, keeps full photo details only for one profile and stays below 48 KiB for the
100-profile/20-photo regression case. Old serialized `ai.request` payloads remain supported and
now receive accounting records without a payload migration.

Prompts, chosen memory and message history are sent to the configured external provider only after
the explicit send action. Memory is never inferred from Telegram profiles, never shared between
unrelated scopes and is presented to the model as user-selected data rather than system
instructions. Provider behavior and orchestration are covered with fakes; the test suite makes no
paid DeepSeek requests.

## Telegram AI and subjective PSL

In **Telegram → Статус → Обслуживание → Профили**, analyze one saved profile or mark several and
choose **Анализировать выбранных**. In **Telegram → Источники → Публикация**, mark up to 100 profiles and choose
**Анализировать выбранных**, or delegate the complete source selector with **Анализировать
публикацию**. The same actions are available in `hoard web`. Hoard resolves stable `author_id`
values from SQLite, uses
the complete `tg_profile_sources` relation and defaults to the four newest available local photos
per profile; it never downloads a new photo or expands Telegram collection for AI. A single
profile still allows manual photo choice without typing IDs.

Before queueing, local user profiles receive automatic 18+ admission and the UI confirms
transmission to DeepSeek. Channels/chats, missing or changed photos and over-limit selections are
retained in a small durable batch as skipped with an explicit reason. Repeating one logical action
returns the original paid attempts.

Publication selection supports saved profile photos, three valid frames from one exact video note,
or those three frames plus one saved photo of the same author. The daemon accepts
`source_id + author_ids`, rejects foreign authors, restricts video options to that source and
repeats the ownership check during durable batch resume. Preview shows ready/skipped counts, actual
image IDs/video IDs and skip reasons without contacting DeepSeek. Only its explicit send button
creates the paid batch; `outcome_unknown` is never retried automatically.

Batch items expose `pending`, `queued`, `running`, `completed`, `skipped`, `failed` or
`outcome_unknown`; totals and costs are SQL aggregates independent of the requested item page.
Public PSL failures use only `invalid_json`, `json_schema_mismatch`, `invalid_psl_result`,
`provider_error` or `outcome_unknown`, without provider error text.

The PSL result is a subjective, non-scientific visual estimate, not a fact or measurement. The
versioned JSON contract stores `presentation`, approximate `estimated_age`, `raw_psl_min`,
`raw_psl_max` and `boosted_psl`, with scores limited to 0–8. Female PSL values are accepted only
for clearly adult female visual presentation. Male/unknown presentation, a possible minor,
uncertain adulthood or insufficient images receives no score. Terminal result status is
`completed`, `insufficient_data`, `ineligible` or `invalid_result`.

The daemon verifies the Telegram ID, photo ownership and archive path, copies a frozen snapshot
to the private AI attachment directory and atomically links it to the attempt. Duplicate operation
IDs return the existing attempt. Connection ambiguity becomes `outcome_unknown`; Hoard warns that
the provider may have charged for it and never retries automatically. A manual retry creates a
new paid attempt linked to the original operation. Historical results, actual model, usage,
currency-separated estimated costs and selected photo IDs remain in SQLite. The offline HTML
exports only the minimal validated result and compact status history described above.

The built-in PSL instructions first reject animals, drawings, anime, fictional characters, memes,
logos, clearly synthetic avatars and unrelated or uncertain images. Images are never treated as
proof of account ownership, and Hoard does not identify people by face or recognize celebrities.

## Background operation

The TUI communicates with one user-owned daemon over a mode-`0600` Unix socket on Linux (Windows
uses authenticated loopback IPC). Pressing `q`, choosing exit, or closing the terminal closes
only the interface. The collector continues until it is paused, stopped or the daemon is shut
down.

- **Pause** cancels active work safely and leaves it queued for **Continue**.
- **Stop** stops the collector while keeping the management daemon available.
- `hoard shutdown` closes both the collector and daemon cleanly.
- Reopening `hoard` reconnects to the existing daemon.

The Textual TUI is intentionally full-screen and restores the original terminal on exit. Its
Home and management screens are idle; only the visible **Главная** screen refreshes status.
Lists support arrow keys and Vim navigation: `j`/`k` move, `l` or Enter selects, and `h` returns.
The daemon and its Telegram and AI queues keep running after exit.

## Non-interactive commands

```bash
hoard status --json
hoard start
hoard stop
hoard scan
hoard add https://t.me/examplechannel/123 --output ~/Videos/hoard --interval 120
hoard telegram report
hoard telegram enrich
hoard web
hoard shutdown
```

`hoard add` preserves the script-friendly behavior and saves a syntactically valid source.
The interactive wizard additionally performs the live access check.

## Local files and privacy

- `~/.config/hoard/config.json` — Telegram API credentials, mode `0600`
- `~/.config/hoard/ai.json` — optional DeepSeek configuration, mode `0600`
- `~/.local/share/hoard/telegram.session` — Telethon session
- `~/.local/share/hoard/hoard.sqlite3` — Telegram and AI metadata, chats, jobs and events
- `~/.local/share/hoard/ai_attachments/` — private copies of explicitly imported AI images
- `~/.local/state/hoard/hoard.sock` — private IPC socket
- `~/.local/state/hoard/daemon.log` — daemon diagnostics

The standard `XDG_CONFIG_HOME`, `XDG_DATA_HOME` and `XDG_STATE_HOME` variables override
these roots. On Windows the equivalent roots are under `%APPDATA%` and `%LOCALAPPDATA%`.
`HOARD_ARCHIVE_HOME` can provide the initial archive root. Never publish the files above, the
generated catalog or a personal archive: profiles and media are personal local data.

## Diagnostics and verification

```bash
hoard status --json
tail -n 100 ~/.local/state/hoard/daemon.log
python -m compileall -q src tests
ruff check .
pytest -q
python -m pip wheel . --no-deps -w dist
```

Set `HOARD_DEBUG=1` only while diagnosing a local daemon problem; normal IPC errors omit
exception class details. Automated tests use fake Telegram clients and require no account,
code or network access. Live login and downloads still require your own authorized Telegram
account and content you are permitted to archive. The release procedure verifies the generated
`file://` catalog in a locally installed Chromium when available, including browser Back/Forward
context; generation and security behavior are also covered without a browser.
GitHub Actions is manual-dispatch only and is not required for release verification.

See [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md) for implementation details.
