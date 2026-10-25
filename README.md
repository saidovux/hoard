# Hoard 0.7.2.2

Hoard is a local-first Linux and Windows archive for Telegram posts, profiles and video notes
found in a channel post's comments. It uses Telethon, SQLite, an autonomous offline HTML catalog
and terminal interfaces. A private background daemon performs all Telegram work; closing an
interface does not stop downloads.

This release adds one minimal AI workflow across the TUI and an explicit local browser client.
It combines saved publications, profiles, photos, video notes, extracted video-note frames and
explicitly requested PSL history. The portable `telegram_archive.html` remains fully autonomous and makes no browser
network requests. `hoard web` is a separate, short-lived loopback client; AI analysis is never
started automatically. The project has no persistent or remotely accessible web service, Redis,
Docker or Electron.

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

The 0.7.2.0 release adds automatic local frame extraction for every newly saved Telegram
video note. A separate media job runs at most one FFmpeg process, writes three JPEG frames at
20%, 50% and 80% of the real duration, and never delays the Telegram download path. A failed or
cancelled extraction keeps the source MP4 and remains retryable. FFmpeg and FFprobe are optional
runtime tools: without them the rest of Hoard continues to work and the UI explains how to
retry after installation. Previously saved video notes are not mass-processed on upgrade; the
Archive screen has an explicit resumable **Обработать старые кружки** action.

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

It has four pages: **Главная** (HOARD wordmark, current task, progress, queue, archive figures,
one line per tool and latest events), **Архив**, and, in the **ИНСТРУМЕНТЫ** group, **Telegram**
(account, collector, separate Web process and watched sources) and **AI**. Former pages remain
compatible aliases: `overview`/`activity` open **Главная**, `sources` opens **Telegram**. The
Archive page keeps downloads, profiles, jobs, catalog actions and the video-frame workflow.
Telegram keeps login including 2FA, collector start/pause/stop, source validation and editing,
selected-source or global scans, and Web start/open/stop. All operations use the private daemon
IPC layer; the TUI never opens SQLite or creates a Telegram client. Only the visible Home screen
polls, with one status request in flight at a time. See `docs/TUI.md` for the design contract.

The interface keeps the entire Textual surface, including navigation, focus, hover, palette and
help modal states, on `#000000`. Text is white, secondary text `#888888`, frames `#7B4B23`,
hairlines `#262626` and the accent `#F79646`. Pages are framed panels with titles in the border;
the focused panel lights up, the footer shows key chips, tables and buttons use an orange selection
block. Use `j`/`k` or arrows to move, `l`, Right or Enter to open, `h`, Left or Esc to return,
`1`–`9` to jump to a page, `:` or Ctrl+K for the command palette, `?` for help, `b` to toggle the
sidebar and `q` to close only the UI. `[` and `]` switch pages even when the sidebar is hidden.
On terminals narrower than 60 columns the sidebar hides automatically; dense tables drop less
important columns instead of scrolling sideways.

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
installation. The 0.7.2.2 client accepts only a daemon reporting the same application version and
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

Run `hoard`, open **Telegram** and choose **Подключить**. Hoard performs the complete login as one
flow through the background daemon:

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
5. saves or updates the source, schedules its nearest check and offers to start the collector.

**Проверить выбранный** queues only the highlighted post and uses the same unique job key as the
scheduler, so repeated presses cannot create parallel duplicate scans. **Проверить все** keeps the
existing global behavior. Editing preserves the current archive directory until it is explicitly
changed and never moves or deletes existing files.

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
truncated archive files. The progress view reports one current file with its own byte count and
speed, plus the active-transfer count and aggregate speed when downloads overlap. Percentages are
shown only when Telegram supplies a total; queue state and recent errors remain visible.

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

Post and profile pages preserve list query, filters, sorting, pagination and scroll context.
Browser Back/Forward works through local hash history for `Источники → Пост → Профиль` and
`Все профили → Профиль`. Photo galleries render one bounded page at a time, lazy-load images and
open a keyboard-accessible viewer with arrow navigation and Escape. Video notes use HTML5
controls, never autoplay and retain `preload="none"`; their saved comment date and original
Telegram link are shown. Missing or rejected local paths render an inert placeholder.

Completed Telegram PSL analyses are exported in one batch and joined only by `author_id`. A
profile's current value is the newest deterministically ordered result whose analysis and attempt
both completed and whose adult-only PSL fields still pass the versioned validator. A later failed,
unknown, ineligible or invalid attempt remains in compact history without replacing that value.
Possible-minor ages, provider payloads, chat text, prompt content, memory, API keys and private AI
attachment paths are never exported. The catalog labels PSL as subjective and non-scientific.

The TUI Archive screen can rebuild the snapshot and shows its path together with downloads,
profiles and jobs. Open the resulting single HTML file in a local browser when needed.

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
`--no-open` and its idle `--lifetime`; Web enabled from **Telegram → Web** uses a manual lifetime
until explicitly stopped. Closing the TUI leaves it running. Each **Открыть Web** asks the running
process for a fresh one-time URL; used URLs are rejected and do not invalidate other browser
sessions. A fresh URL and the stop command are available only through a separate authenticated
loopback control channel advertised in the private mode-`0600` state file. The browser page never
receives that token and has no server-stop route.

While the browser is visible it polls a tiny published-catalog-version endpoint. It requests the
new normalized snapshot only when the atomically replaced HTML changes, then updates collections,
search indexes, counters and media allowlists in place. Existing filters, search, pagination,
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
visible once; the TUI can reopen the newest active group from **Архив → Профили**.

The autonomous `<archive>/telegram_archive.html` does not contain a session, API key or active
backend configuration and keeps working through `file://` exactly as before. Closing the Web or
TUI client does not stop the daemon or its durable AI groups. Browser video cards show the three
extracted frames; the frame-specific AI action uses that exact local set.

## Video-note frames and FFmpeg

Each newly downloaded video note gets a durable local job. A separate one-slot media runner uses
system `ffprobe` to read the actual duration and `ffmpeg` to save three JPEG frames at 20%, 50%
and 80%. FFmpeg never runs inside the Telegram event loop, never modifies the MP4 and never starts
an AI request. Frames live next to their source as
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

Existing video notes are not mass-processed after an upgrade. In **Архив → Кружки** choose
**Обработать старые кружки** to start the bounded resumable backfill; **Пауза очереди кадров**
is a soft pause: it stops placing new backfill jobs, while the current FFmpeg process and already
queued jobs finish. **Повторить ошибки кадров** retries failed/stale sets after fixing the
dependency. Processing continues in the daemon when the TUI or Web is closed and does not
download the MP4 again. Frame completion marks the catalog dirty; an open Web receives the new
frames without a reload. The autonomous `file://` catalog also displays frames when their files
remain inside the archive.

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

In **Архив → Профили**, analyze one saved profile or mark several and choose **Анализировать
выбранные**. In **Telegram → Источники**, choose **Анализ профилей** for a saved publication. The same
actions are available in `hoard web`. Hoard resolves stable `author_id` values from SQLite, uses
the complete `tg_profile_sources` relation and defaults to the four newest available local photos
per profile; it never downloads a new photo or expands Telegram collection for AI. A single
profile still allows manual photo choice without typing IDs.

Before queueing, local user profiles receive automatic 18+ admission and the UI confirms
transmission to DeepSeek. Channels/chats, missing or changed photos and over-limit selections are
retained in a small durable batch as skipped with an explicit reason. Repeating one logical action
returns the original paid attempts.

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
