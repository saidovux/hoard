# Architecture

Hoard is one local Python application split into an on-demand terminal client and one
background service.

## Process boundary

- `cli.py` is the command dispatcher. `hoard`, `hoard ui` and `hoard tui` launch the Textual
  client; `ui` and `tui` are compatibility aliases. Service commands remain non-interactive and
  do not import Textual.
- `tui/` is the only interactive terminal client: a full-screen Textual application. `app.py`
  owns lifecycle, contextual keyboard navigation, named mutation guards and the single in-flight
  status refresh; `api.py` provides typed operations over the existing `ensure_daemon()` /
  `request()` boundary. `navigation.py` declares the four-entry global level and the Telegram
  workspace (former Overview/Activity map to Home; Sources maps to Telegram Sources) used by the
  contextual sidebar, breadcrumb, level-local digits/cycling and palette;
  `screens/telegram_publication.py` is an internal route whose navigation parent is Sources, so
  dynamic source IDs never enter the global section registry;
  `colors.py` owns the Noir palette; `palette.py` searches sections and app commands;
  `widgets/panel.py`, `widgets/hero.py`, `widgets/status.py`, `widgets/telegram_monitor.py` and
  `widgets/chrome.py` provide the framed-panel layout, wordmark, figures, the compact Home transfer
  strip and key-chip footer. Dialogs, screens, status formatting,
  reusable widgets and TCSS remain separate. The package imports neither storage nor Telethon, and
  never creates a Telegram client. See `docs/TUI.md`.
- `ipc.py` starts the daemon detached when required and sends one JSON request per private
  Unix-socket connection. Client and daemon must both report Hoard 0.7.4.4 and IPC protocol `1`.
  A held instance lock with no ready socket means initialization is in progress, so the client
  waits for that owner instead of spawning a second daemon.
- `service.py` owns the database, three independently limited job runners and the sole
  `TelegramEngine`: the Telegram/collector runner, the paid-AI runner and a dormant one-slot media
  runner that exists only for an explicitly authorized local FFmpeg frame session. A mode lock makes
  Telegram work and FFmpeg mutually exclusive; an explicit Telegram start hard-stops and awaits the
  media runner before receiving priority. A separate asyncio lock serializes login, validation,
  scans and enrichment so one Telethon session is never opened by competing clients.
- `ai/` is provider-neutral and has no Telegram dependency. It defines requests, responses,
  stable errors, configuration, response validation and the provider contract; `deepseek.py`
  implements the first provider over outbound HTTPS. `storage.py`, `chat_service.py`,
  `pricing.py`, `psl.py` and `ipc_api.py` keep AI persistence, orchestration, versioned PSL
  validation, cost calculation and daemon contracts out of the main service module.
- `telegram_storage.py` owns Telegram-specific tables and migrations while `storage.py` retains
  the shared source, job and event primitives for future modules.
- `catalog.py` opens SQLite independently and generates a static browser snapshot without owning a
  Telegram client. The template lives in the installed `hoard.templates.telegram` package.
- `web.py` is the only HTTP exception. An explicit `hoard web` command rebuilds the catalog through
  daemon IPC, binds a short-lived `ThreadingHTTPServer` to `127.0.0.1:0`, and reuses the exact
  catalog template. It never opens SQLite or calls a provider. Exact Host/Origin checks, a random
  bootstrap/session token, HttpOnly SameSite cookie, independent CSRF token and CSP protect every
  write. Only media paths already validated into the current catalog snapshot and still contained
  by one of the daemon-reported archive roots are served through an opaque root-prefixed URL.
  The launcher and server coordinate through one atomically replaced private state file. The TUI
  enable action waits for confirmed readiness and only then requests a fresh one-use URL and opens
  the browser; failure to launch the browser does not stop the ready Web process. Before
  spawning, the parent writes a `starting` record with a random launch identifier; only the child
  writes `preparing`, `ready` (after binding the HTTP socket) and `error`. The parent never writes
  after `Popen()`, so a fast child cannot be overwritten by an older state. A launch/server lock
  and the launch identifier prevent a second server, reject a ready record whose process has
  exited, and let the launcher wait for the real build (up to four minutes), verify a loopback
  connection, and terminate only its own timed-out child. Corrupt and stale state files are
  discarded under the lock. A healthy existing server is reused. Each one-time bootstrap token
  is kept in a short-lived in-memory set; issuing a new URL does not invalidate other pending
  opens, while consuming a token removes it permanently. Authenticated browser sessions remain
  unchanged. A separate authenticated loopback control server, advertised only in the private
  `0600` state file, issues fresh bootstrap URLs and accepts a stop command. It is never exposed
  by the browser app and uses no session/CSRF token. The TUI controls Web exclusively through
  this channel and never kills a PID from JSON. Web enabled by the TUI runs with a manual lifetime
  until stop, daemon shutdown or crash; direct `hoard web --lifetime` keeps its idle lifetime.
  Each published HTML snapshot is detected by a stat token endpoint. Only after a complete atomic
  replacement does the page request `/api/catalog/snapshot`; the new payload and validated media
  allowlist are swapped under a snapshot lock, with the previous allowlist retained for one
  generation so active media is not interrupted. The browser updates its mutable collections,
  maps and search indexes in place and defers visual rebuilds while a modal viewer or playing
  video is active.
- The socket and all XDG state directories are user-only. No persistent or remotely reachable
  TCP/HTTP listener exists; the loopback browser session starts only by explicit user action and
  expires after inactivity.

Closing or cancelling the interface does not signal the service. Collector pause/stop cancels
active jobs through `JobRunner`, which returns interrupted jobs to `queued`; concurrent stop
requests are serialized so the loop is cancelled once and cleanup completes. A temporary SQLite
error while scheduling or marking a job records an event and retries with bounded backoff
instead of terminating the only loop that drains durable work. The daemon stays available for
management. The AI runner follows the daemon rather than the collector and uses its own
concurrency limit. Each runner selects only its registered job kinds, so a long AI request
cannot take a Telegram slot or acquire the Telegram lock. Interrupted AI jobs become `failed` and
require an explicit retry because a cancelled paid request may already have reached the provider.
Shutdown closes the Telegram, AI and any manually active media runner, the Telegram client,
database and socket.
Menu keybindings are defined once for arrow and `h/j/k/l` navigation. Telegram IPC failures render
network/VPN guidance inside the active Textual modal while the daemon owns the actual client.

The Textual client is the sole interactive presentation and may enter application mode. Visible
Home and Telegram Status screens each own a moderate 1.5-second timer and stop it when suspended;
both request bounded `status_brief` snapshots through the app's single in-flight lock. Home's
Telegram monitor uses only that snapshot, updates changed content without rebuilding navigation,
and clears transfer slots when the collector stops or IPC becomes unavailable. Telegram Status
loads archival aggregates separately through read-only `telegram_statistics_get` on entry or
manual refresh, never from its live timer. Archive requests detailed status only when opened;
recent collections are bounded and an oversized compatibility response is explicitly truncated
before the 64 KiB IPC boundary. Telegram Sources uses its own bounded
`telegram_sources_overview` request and does not depend on that compatibility array. Screen
suspension, unmount and app exit
stop their timers. IPC startup and request errors render inside the stable black workspace so a slow
or unavailable daemon never produces an unexplained empty screen. Exiting the client sends no
collector, frame-stop, shutdown or Telegram command; previously started frame/Web work stays alive.

The Textual theme sets background, surface and panel to `#000000`; TCSS also fixes computed
backgrounds for containers, navigation components, focus/hover/selected states, inputs, tables,
buttons, menus and modals. The outer workspace exposes **Главная**, **Архив**, **Telegram** and
**AI**. Home keeps the wordmark, one Telegram activity monitor and compact Hoard-wide totals;
historical events and tool summaries remain available through their owning screens/data rather
than global Home panels. Telegram switches that same
sidebar to **Статус**, **Источники**, and bottom-pinned **Web Viewer** and **Вход** pages; no second
sidebar is mounted. `telegram` resolves to Status and `sources` directly to Sources. Archive retains
downloads, profiles, jobs, catalog and video-frame actions, with less frequent AI/frame commands
behind a mode-specific **Ещё…** block. Tables fit or reduce their columns instead of scrolling sideways, and a table is
auto-focused only when it is fully visible. The modular AI screen and dialogs expose only
implemented IPC operations and never import storage, provider transports or Telegram. Every
surface is a black framed-panel workspace with an orange focus border; tests enforce the palette,
responsive classes, visible geometry and disabled/selected states.

## Telegram authorization

The daemon exposes an explicit state machine:

```text
api_required → phone_required → code_required → password_required → authorized
                         ↘──────────────────────────────→ authorized
```

`auth_begin` always checks an existing session first. The phone-code hash stays only in daemon
memory, and repeated requests for the same pending code are deduplicated. Expected Telethon
errors are converted to structured `state`, `error_code`, `retryable` responses; secret request
arguments are never recorded in events or logs. `auth_cancel` clears only a pending challenge,
not credentials or an authorized session.

`status_brief` includes a bounded `telegram_account` snapshot. A session file is only evidence of
saved state: it is yellow/unverified until an explicit daemon-side authorization check succeeds.
Green requires a connected client, a confirmed authorized session and the process-local verified
state. Red means no usable confirmed session; unknown failures remain neutral. The daemon caches
only display-safe `id`, name and optional username from `get_me()` after successful login or an
explicit check, never on sidebar render. Cached identity is returned for `saved` state with an
explicit disconnected label, but is withheld for `login_required`. `telegram_account_disconnect` refuses to run while the
collector is active, disconnects the sole daemon-owned client and preserves the session file;
logout/revocation is intentionally not exposed.

After login, scans reuse the connected client immediately. If the process is restarted during
login, the transient challenge is intentionally lost and a new code must be requested.
The Textual authorization dialog stores secrets only in short-lived password inputs, serializes
each operation and clears API Hash, code and password values on completion or cancellation.
Cancelling a pending UI request is followed by `auth_cancel`; neither action stops the daemon.

## Persistence, migration and scheduling

`storage.py` owns a synchronous SQLite connection inside the daemon event loop. Shared tables keep
sources, downloads, durable jobs and a bounded event history. Telegram adds versioned,
idempotently-created tables:

- `tg_profiles`, keyed only by stable Telegram `author_id`;
- `tg_username_history` with first/last observation timestamps per ID and username;
- `tg_profile_sources`, linking one profile to any number of source posts;
- `tg_profile_photos`, storing photo IDs, status, size and local paths;
- `tg_post_metadata` and `tg_post_media` for the parent posts;
- `tg_video_frame_sets` and `tg_video_frames` for one versioned local frame set per video note,
  including actual timestamps, frame sizes, SHA-256 values and source-file fingerprint;
- `tg_settings`, `tg_archive_roots` and `schema_versions` for archive configuration, safe access to
  earlier roots and migrations.

AI storage uses the same daemon-owned connection but a separate additive schema:

- `ai_prompts`, immutable `ai_prompt_versions` and per-scope `ai_active_prompts`;
- `ai_chats`, ordered `ai_messages` and bounded `ai_attachments` links;
- `ai_memory` and immutable `ai_memory_versions` selected explicitly per request;
- `ai_attempts`, `ai_attempt_messages` and `ai_attempt_memory` as frozen execution journals;
- `ai_prices` as user-owned effective-period tariffs whose rows are snapshotted into attempts.
- `ai_telegram_analyses` and `ai_telegram_analysis_photos`, atomically linking a Telegram ID,
  prompt/schema version, immutable selected-image snapshot, paid attempt and validated result.
  The image journal records whether each input was a profile photo or video frame, its source
  message/frame position and timestamp; the analysis row records the image mode and frame-set
  identity.
- `ai_telegram_batches` and `ai_telegram_batch_items`, a minimal durable grouping of existing PSL
  attempts with per-`author_id` skip reasons and idempotent operation keys.

The AI schema records its own version in `schema_versions`. Deletion is soft where history refers
to an object, critical queue/context writes are transactional, and no transaction stays open over
a provider request. New attachment files are mode `0600` under a mode-`0700` data directory.
Identical SHA-256/size content reuses one active attachment row and file; cleanup refuses to delete
content referenced by either chat history or a Telegram analysis.

The migration only adds tables/columns. Its one-time legacy pass reads existing `downloads` and
safe JSON sidecars inside configured archive roots, backfills profile links and file state, and
leaves files and `sources.last_id` untouched. Unique constraints make the operation repeatable.
Old media may stay outside `telegram/`; the report emits a relative path or a `file:` URI when
Windows drives differ. It never creates an implicit second media archive.

`sources.collection_mode` is an additive non-null column with default `full`, so every historical
source preserves the old collection policy. `source_update` changes only title, output, interval
and this validated mode by stable ID; username/post identity, enabled state, `last_id`, `next_at`,
relations and archive rows are untouched. A different post is always a new source.

Every successful video-note checkpoint writes a `tg_video_frame_sets` row and one durable
`telegram.video_frames` job in state `waiting` in the same SQLite transaction. Waiting records show
what still needs work but carry no permission to execute. Daemon/TUI startup, download completion,
restart and generic `job_retry` do not start the media runner; construction of a new service also
demotes legacy `queued`/`running` frame work to `waiting` and resets an interrupted frame set to
`pending`. Only **Извлечь кадры** for one selected video or a confirmed selected/publication/bulk
action grants process-local permission for the current session.

An authorized session uses FFprobe for real duration and at most one FFmpeg process with one decode
and filter thread. On Linux the process gets a best-effort lower priority. Three frames at
20/50/80 percent are written to a temporary directory beside the MP4, checked for unique decodable
JPEG content, then atomically installed as
`<message>_frames/frame_01.jpg`...`frame_03.jpg`; the source MP4 is never modified. A hard stop
revokes permission before cancelling the runner, terminates and reaps the subprocess with a bounded
TERM/KILL fallback, removes unpublished temporary files and returns unfinished work to `waiting`.
Saved MP4s and valid published frames are retained. Missing FFmpeg/FFprobe is an actionable manual
error, not an installation, download or daemon failure.

The confirmed old-archive action starts a durable cursor and authorizes work only into free slots of
an eight-job queued/running budget while the one-slot media runner drains it. The cursor advances
across scanned records and stops before the next unscanned row when capacity is exhausted, so a
large archive is never loaded into memory. Successful sets are skipped; failed/stale sets require
the confirmed retry action. Closing the TUI does not stop an authorized daemon session, while
**Остановить обработку** performs the hard stop above and restart never resumes it automatically.
Frame completion is coalesced into the existing catalog dirty/revision and atomic publication path;
after a manual session ends, a bounded deferred report publishes if the Telegram runner is stopped.

Publication sessions reuse that same one-slot `JobRunner` and extraction handler. The runner takes
a process-local allowed-job-ID provider; its SQLite dequeue adds an `id IN (...)` predicate, so a
selected session cannot consume an older queued job merely because its kind matches. Explicit
selections validate one source plus 1–100 unique positive message IDs and retain at most 100 waiting
jobs. A publication run uses a source-scoped durable cursor, resumes only that source's interrupted
waiting work, and scans bounded 50-row windows while authorizing only free slots in the shared
eight-job queued/running budget. Source retry resolves at most 100 failed/stale IDs and enters the
same allowlisted sequence. Hard stop and daemon construction clear the allowlist and execution
permission, but not completed frames, waiting jobs or cursor.

Enabled sources become unique `telegram.scan` jobs when due. Telegram jobs remain serialized by
the global session lock, while one scan uses the same authorized client for a bounded set of video
note transfers. `HOARD_TG_DOWNLOADS` selects `1` through `4` transfers (default `3`) independently
of `HOARD_MAX_JOBS`; both active transfers and the traversal reorder buffer are bounded. Progress
keeps its compatible selected-file fields and additionally publishes a bounded `transfers` list
from the same in-memory active-download map. Entries contain only `source_id`, `message_id`, bytes
and total; they are neither persisted nor obtained through extra polling. The TUI pins at most
three identities to stable horizontal slots while the fourth transfer continues normally. Profile
photos, post metadata, post media and report generation remain separate deduplicated job kinds.

The scan coordinator commits cursor movement in encountered traversal order rather than assuming
contiguous Telegram message IDs. A completed media file is first registered with
`checkpoint(..., advance=False)` after it is fully written, fsynced and atomically renamed. The
source cursor then advances only across the continuous processed prefix, including ordinary
comments and valid previously saved media. A retry-deferred or failed transfer blocks that prefix;
later successful files remain durable and are deduplicated on the next scan. Missing or
size-mismatched video notes move the scan cursor back to the earliest affected message. A missing
legacy JSON sidecar alone does not trigger a network re-download because SQLite already has the
required catalog metadata.

Cursor writes for a processed prefix are coalesced into bounded 32-comment batches. A 250 ms
deadline flushes a partial batch even while the scanner awaits the next Telegram item; scan exit
and every blocking failure flush the preceding safe prefix synchronously. No SQLite transaction
is held across iteration or transfer waits. Archive validation follows symbolic links as before,
but one `stat()` now establishes both regular-file type and exact recorded size.

The `scan_source` IPC operation validates that one source exists and is enabled, then enqueues the
same `telegram.scan` payload and `telegram:<id>` unique key used by the scheduler. It therefore
does not reuse the global `scan` command and cannot create a second queued/running job for the
selected post. The status response includes bounded profile summaries with photo/source counts so
the TUI never needs direct database access.

`telegram_sources_overview` validates a maximum 100-row page, non-negative offset, a bounded search
string, nullable enabled filter and a closed sort whitelist. It first selects the source page, then
batch-aggregates only those IDs from `downloads`, `tg_profile_sources`, shared profile photos and
frame tables. PSL attribution unions confirmed `ai_telegram_batches.source_id`,
`ai_telegram_analyses.video_source_id` and analysis-image `source_id`, counts distinct analysis IDs,
and accepts only completed attempts with a completed domain result. Author membership alone never
attributes PSL to a publication. The response contains no media rows, result JSON, credentials or
archive file paths and stays under the existing 64 KiB IPC limit.

The contextual viewer adds bounded read-only `telegram_publication_get`,
`telegram_publication_downloads_list`, `telegram_publication_download_get`,
`telegram_publication_frames_list`, `telegram_publication_profile_get`,
`telegram_publication_psl_list` and `telegram_publication_psl_get` operations; the existing
`ai_telegram_profile_list` remains the profile page and `telegram_frames_get` remains the explicit
deep frame inspection. List queries are source-filtered and page before returning metadata. Frame
pages perform no file stats or hashes. PSL attribution unions batch, analysis-video and
analysis-photo links, checks batch-item author consistency and deduplicates analysis IDs; detail
lookups repeat source membership before returning safe scalar fields.

`telegram_publication_media_open` accepts only stable video/photo/frame IDs. `Store` resolves the
SQLite row, verifies publication ownership (and profile membership for globally owned photos),
resolves symlinks, checks the regular file and known size under remembered archive roots, and only
then calls the platform opener without a shell. Bulk responses and detail cards never expose an
absolute path. Explicit frame resolution retains the existing three-position, MP4 fingerprint and
SHA-256 contract; the list never invokes it.

The operation panel adds `telegram_frames_selected_start`, `telegram_frames_source_start`,
`telegram_frames_source_retry_failed` and `telegram_frames_session_status`; all starts require an
explicit confirmation and repeat source ownership checks inside the daemon. The existing
`telegram_frames_backfill_pause` remains the one hard-stop contract for every session type. The TUI
polls only session status and one source-filtered durable AI-batch summary while the publication
screen is visible. Page requests use per-tab generations plus one pending-reload bit, and media-open
workers receive an immutable source/object snapshot rather than mutable screen state.

Duplicate source insertion uses a compatibility-preserving integer `Store.add_source()` over an
internal `(id, created)` result. Archive-root/catalog dirty side effects and runner wakeup occur
only for a real INSERT. Sources frame aggregation counts frame rows only when their set is saved;
failed and stale sets remain separate counters.

Profile photo enumeration is capped at 30 per check. Saved Telegram photo IDs prevent duplicate
downloads. Telegram may expose an animated avatar through `video_sizes`; Hoard downloads the
largest still `PhotoSize` sibling instead, so the `.jpg` file is decodable by the HTML gallery.
A still-less video is skipped, and an existing video container under a photo path is re-fetched on
the next check. The scheduler backfills due profile IDs already in SQLite and retries temporary
failures without rewinding any source checkpoint. Privacy/RPC failures and FloodWait retry times
are recorded on the profile and do not fail the source scan. Authorization loss, inaccessible
peers, privacy/access restrictions, FloodWait and network failures have distinct retry classes;
local filesystem write failures fail the photo job and are never mislabeled as network trouble.
A full source keeps the existing due-photo scheduling. A minimal source observes only identity
already carried by the message (including stable sender ID), records `tg_profile_sources` and
downloads the video note, but neither resolves a missing sender nor creates a photo job. Global
enrichment selects a due author only when at least one linked source is full; profiles with no
source relation keep the legacy policy. Existing queued work and saved shared photos are retained,
and the active unique key still deduplicates an author shared by minimal and full sources.
A Telegram transfer with no **increasing received-byte counter** for two minutes is cancelled as
a retryable network stall; repeated callbacks that report the same count do not refresh the timer,
so a dropped connection cannot leave one scan task alive forever. Cancellation is awaited until
the transfer stops before the caller closes the stream or removes `.part`, avoiding writes into a
reused file. Telethon's download loop propagates `CancelledError`; an arbitrary coroutine that
suppresses cancellation indefinitely would delay shutdown by design rather than risk corruption.

## Offline catalog

`<archive>/telegram_archive.html` is the only generated site file. The repository and wheel keep
HTML, CSS and JavaScript as three maintainable source resources; `catalog.py` embeds both assets
and the normalized SQLite snapshot into one HTML document at build time. This works under
`file://` without CORS-sensitive `fetch`. The generator validates media paths against configured
archive roots, escapes script-sensitive characters, writes one adjacent temporary file and
atomically replaces the report only after a complete successful build. The user-owned
`hoard_objects/` tree is never replaced.

Telegram tables, all PSL analysis rows and remembered archive roots are read in one short SQLite
snapshot. PSL is joined to profiles by stable `author_id` in memory, without a per-profile query.
The latest displayable score requires both the domain result and attempt to be completed and must
still pass the versioned adult-only validator. Deterministic completion-time/ID ordering keeps an
older success active when a newer attempt failed, became outcome-unknown, was ineligible or stored
an invalid result. Non-success history exports only status/audit metadata; possible-minor ages and
all score fields are null. AI chat text, prompt content, memory, raw responses, private attachment
paths and provider job payloads do not enter the snapshot.

Every catalog-visible Telegram or PSL mutation sets `catalog_dirty` and increments a monotonic
`catalog_revision` in the same transaction. The builder records the revision belonging to its
SQLite snapshot, publishes the complete temporary file, then clears dirty with a conditional
update only if the revision is unchanged. A concurrent mutation therefore stays dirty and the
existing Telegram job loop reconciles another deduplicated report job; no scan or second queue
system is required. It also reconciles a durable dirty marker after restart. Three immediate
passes absorb a short mutation burst, followed by a two-second durable cooldown when changes keep
arriving. Filesystem failures retain their reason and use bounded exponential backoff from 30
seconds to one hour; an explicit retry bypasses that automatic delay, coalesces with an already
active report job instead of creating a duplicate, and a successful report clears the previous
error.

Report generation runs in a worker thread, but cancellation keeps the per-daemon report lock until
that worker actually exits. Repeated cancellation is absorbed until `asyncio.to_thread()` is
finished, and concurrent runner stops are serialized, so a replacement generation cannot write
the same final HTML concurrently with a cancelled predecessor. The builder also holds a
cross-process advisory lock in `hoard_objects/.catalog.lock` from before its SQLite snapshot until
the final file and dirty marker are written. That covers a direct `hoard telegram report` running
beside the daemon: an older snapshot cannot finish last and overwrite a newer catalog. The
temporary-file `flush`, `fsync` and atomic `os.replace` boundary remains unchanged. Re-registering
an identical normalized archive root only updates its service timestamp, and a post-metadata
refresh invalidates the catalog only when a published value changes; a real root change remains
atomic with its revision increment.

The report builder retires the short-lived split layout by moving
`<archive>/telegram/objects/` into `hoard_objects/` and rewriting only matching media paths in
SQLite. It never overwrites a conflicting file. Older user archive layouts elsewhere stay in
place and remain available through validated paths.

The browser uses DOM construction and `textContent`, never HTML interpolation. Original links are
restricted to HTTPS `t.me`; local media URLs accept only traversal-free relative or `file:`
references produced by the generator. Separate post/profile list state preserves queries,
composable filters, sorting, loaded batches and scroll positions. Local hash/history routes support
browser Back/Forward without a server or router dependency. Lists and galleries remain bounded, images are
lazy, the lightbox creates one full-size node at a time, and videos use `preload=none` without
autoplay. Missing round-six fields remain optional so an older normalized snapshot degrades to an
unscored profile rather than breaking rendering.

The 0.7.3.0 Web surface uses the same Black Archive 03 template for the autonomous `file://`
snapshot and the explicit loopback client. Publication and profile lists, post sections and long
profile galleries append bounded batches with `IntersectionObserver` instead of replacing the DOM
through page buttons; the stored batch limits remain in force. A non-observer fallback exposes a
local “Показать ещё” button. Search, filters, sorting, loaded batches and scroll are preserved when
a profile or publication is opened and the browser returns, and a live catalog snapshot reuses the
same loaded counts before restoring the scroll position. Media viewers, playing video and the
existing AI preview are not replaced during a snapshot update. The UI renders only real catalog
records and ready local frames; it does not start FFmpeg, invent statuses or add analysis data.

## Interactive loopback catalog

`hoard web` first asks the daemon to rebuild the ordinary snapshot, then serves that exact bundled
HTML from a random `127.0.0.1` port. Interactive controls are activated by a small in-memory
bootstrap object; without it the same JavaScript performs no HTTP request, preserving autonomous
`file://` behavior. Filters, pages, selected `author_id` values and hash routes are mirrored to
`sessionStorage` before an automatic post-analysis refresh.

The interactive initial route is the profile list with the readable-photo filter enabled, so the
first page is an avatar catalog rather than a page of recently observed profiles without photos.
Only photo rows whose file still passes the strict media URL/size validation count as visible;
the profile detail reuses that same set for its incrementally loaded gallery. The autonomous `file://`
catalog retains its historical default route and unfiltered profile list.

Browser writes map to a closed set of daemon IPC calls: key save, PSL preview, batch start and
catalog refresh. The API key crosses the browser boundary only in the explicit save request and is
never returned. The browser process does not read SQLite. Media serving accepts only resolved
files in the snapshot's explicit daemon-provided archive-root allowlist and supports bounded byte
ranges;
SQLite, Telegram sessions, AI attachments and arbitrary filesystem paths have no route.

## Progress and errors

Telethon's progress callback supplies current and total bytes. Hoard keeps independent transfer
state and speed for each active video note. IPC retains the established single-file fields and
adds `active_downloads`, `aggregate_speed_bps` and `transfers`; aggregate speed is the sum of
current per-file rates and never replaces `speed_bps`. Each transfer is stably identified by its
source and message IDs. The UI derives a segment fraction only from a positive total and never
invents a remainder, percentage or ETA. Routine Telegram failures are stored as actionable
messages; exception class details are returned only when local `HOARD_DEBUG=1` is explicitly
enabled.

`cryptg` is a required runtime dependency. Telethon imports it as its preferred native AES-IGE
backend independently of Hoard's transfer adapter. Hoard does not implement Telegram
cryptography. Concurrency is limited to the bounded per-scan scheduler; Telegram jobs and client
ownership remain serialized.

Video-note documents use the small `telegram_download.py` compatibility adapter. Telethon 1.45's
public `download_file(part_size_kb=...)` cannot forward the message context required to refresh an
expired document reference, so the adapter checks the private `_download_file` signature and uses
that existing recovery-aware primitive with 512 KiB parts. The primitive retains its sender, DC,
CDN, retry and cancellation behavior. An unknown contract, missing document or unknown size takes
the unchanged `download_media()` fallback. The surrounding `.part`, `fsync`, atomic replacement
and sidecar boundaries remain in `TelegramEngine.download_one()`. Durable media registration and
ordered cursor movement are coordinated separately by `TelegramEngine.scan()`.

## AI Core — round 1

`AIProvider` is the asynchronous boundary for providers. `AICore` selects a provider and performs
provider-neutral completion checks. DeepSeek uses the OpenAI-compatible
`POST https://api.deepseek.com/chat/completions` endpoint and defaults to `deepseek-flash`.
The transport is injectable so text and multimodal behavior can be tested without a network or
paid request; the production stdlib HTTP call runs in a worker thread and never blocks the daemon
event loop.

Callers construct `AIRequest` values from messages and, only after an explicit action, `AIImage`
bytes. Image signatures, per-image size, image count and total encoded request size are checked
before transmission. Images are accepted only in `user` messages. No filesystem discovery or
automatic Telegram profile analysis exists in the core; the higher-level workflow remains an
explicit daemon operation.

JSON requests set `response_format={"type":"json_object"}` and must explicitly instruct the model
to return JSON. A result is successful only when content is non-empty and `finish_reason` is
`stop`. JSON is parsed and optionally checked with a Draft 2020-12 schema. Invalid schemas fail
before the paid call; malformed, empty, schema-invalid and truncated provider responses fail after
one call and are never silently accepted. External schema references are rejected, and model
output is treated only as data: round 1 sends no tools and executes no generated commands.

DeepSeek errors are mapped to stable configuration, authentication, balance, rate-limit,
connection, request and provider-availability errors with a `retryable` signal, but round 1 never
acts on that signal automatically. The key comes from `DEEPSEEK_API_KEY` or the user-only
`ai.json`; configuration metadata exposes only `key_source=env|file|none`, with the environment
taking precedence. The secret is excluded from object representations and never enters SQLite job
payloads or events. Telegram PSL and browser interaction belong to later rounds.

## AI infrastructure — round 4

`AIChatService` is the single assembly path for persistent conversations. An explicit
`ai_chat_send` freezes the prompt version, selected memory versions, a contiguous recent-history
slice and attachment IDs in one short transaction. The order is deterministic: system prompt,
memory marked as user-selected data, then historical and new chat messages. Stored audit metadata
names the conservative character limit and records used and omitted history counts; it never
presents that limit as an exact token count. Required prompt, memory and new-message content is
rejected with an actionable error rather than silently truncated.

The new job payload is only an attempt ID. At execution, the daemon rebuilds the request from the
frozen SQLite links, verifies each private attachment's size and digest, and calls the existing
`AICore`. Legacy `ai.request` payloads remain readable and are journaled without migration.
Operation keys and a partial unique index allow only one queued/running generation per chat and
make duplicate IPC delivery idempotent.

Attempts move through `queued`, `running`, `completed`, `failed` or `outcome_unknown`. Provider
usage is attached in memory to validation errors so prompt, output, cache-hit, cache-miss and
reasoning counts survive rejected content without storing a raw provider response. Cancellation,
connection ambiguity and a daemon restart after dispatch become `outcome_unknown`; the AI runner
does not recover them automatically. Only `ai_attempt_retry` creates a new attempt and paid job.
Terminal accounting and assistant insertion are idempotent.

`pricing.py` uses `Decimal` and only computes components supported by both actual usage and the
selected provider/model tariff. Reasoning tokens are descriptive output detail and are not added
to completion tokens again. Missing splits produce `partial` or `unknown`, never zero. Each result
stores the applied tariff snapshot and is not recalculated after tariff edits. Statistics are SQL
aggregates filtered by time, provider, actual/requested model, scope or chat and group estimated
cost by currency without conversion.

AI CRUD, history, send, retry and statistics operations remain behind the same private IPC
boundary. Lists are capped and paginated. Requests and responses are encoded before sending and
must fit 64 KiB; oversized pages return an explicit smaller-page error. Job failures receive only
a safe generic summary, while the attempt keeps a stable code. Chat content, provider error text,
images and API keys do not enter events or common job diagnostics.

## AI workspace and Telegram PSL

The Textual AI workspace calls the same daemon IPC for chat CRUD/history, provider/model and
prompt-version selection, prompt and memory editing, private attachment import, tariffs,
currency-separated statistics and attempt inspection. API-key writes are atomic mode-`0600` file
updates; reads return only configuration metadata and a configured flag. Every paid send requires
an explicit external-provider confirmation and a caller operation ID. The UI polls only the
newly created attempt for a bounded time, displays only a confirmed completed response, and
leaves `outcome_unknown` visible for a separately confirmed manual retry.

Telegram PSL begins from one saved `author_id` and one to four saved `photo_id` values. The daemon
rechecks profile ownership, `saved` status, file size and containment within remembered archive
roots before importing private copies. Jobs contain only an attempt ID. The original Telegram
photo IDs, source path/digest snapshot and private attachment links are frozen transactionally;
opening an existing analysis performs only a read. No new Telegram download is triggered.

Telegram AI can select three modes without creating a second provider workflow: saved profile
photos; three frames from one saved video note; or those three frames plus one profile photo of
the same author. Frame handles are resolved from the local frame set, never by arbitrary path or
another person's files. A publication-scoped analysis carries its resolved `source_id` through
preview, the durable batch request and resume; frame options and explicit video IDs are filtered to
that source. Profile-only analysis without a publication can still choose among all of that person's
saved sources. The three frames are attached in timestamp order to one user message and
one `ai_attempts` row; preview fingerprints are rechecked before queueing. The request asks the
model to treat them as moments of one video and keeps the same PSL output contract. No download
or frame-extraction completion queues an AI attempt. `_telegram_psl_candidates()` accepts the
backward-compatible global `author_ids` selector, the complete `source_id` selector, and a bounded
explicit `source_id + author_ids` intersection. Durable batch resume passes both source and item
author again, so stale state cannot bypass membership or select frames from another publication.

`psl.py` defines schema version 2. Its five metrics are visual `presentation`, approximate
`estimated_age`, `raw_psl_min`, `raw_psl_max` and `boosted_psl`; numeric scores are bounded to
0–8. The workflow also stores `completed`, `insufficient_data`, `ineligible` or `invalid_result`.
Scores are valid only for clearly adult female visual presentation. Male/unknown presentation,
possible minors, uncertain adulthood and insufficient images retain null scores. Adult status is
confirmed explicitly outside image inference before queueing. The result is documented and shown
as subjective and non-scientific.

The AI attempt and domain result finish in one SQLite transaction. A connection/cancellation
ambiguity remains `outcome_unknown` without a domain result; invalid provider JSON/domain data is
`failed` plus `invalid_result`. A manual paid retry reuses the frozen photo attachments, creates a
new attempt/analysis row and points `root_analysis_id` to the original. The round-six catalog
consumes these rows in one batch while retaining the same storage contract.

The selection contract accepts stable local `author_id` values, one saved `source_id`/post URL, or
up to 100 explicitly selected author IDs within one source. Source links are resolved against
`sources` and the complete
`tg_profile_sources` relation; unknown links fail without scheduling Telegram work. Preview
revalidates local files, defaults to four newest photos, reports prior analysis and makes no
provider call. Local user profiles receive automatic 18+ admission while channels/chats remain
ineligible. The TUI and Web preview paginate compact profile/photo-ID records, retaining full photo
metadata only for a single profile. One explicit send button follows the external-transfer and
possible-cost disclosure; clients set `external_confirmed`, while the Web server never invents it.
Start delegates every eligible item to the existing `queue_telegram_psl()`. A durable batch only
groups those attempts and skipped reasons; it is not a second runner or retry system. Its public
states are `pending`, `queued`, `running`, `completed`, `skipped`, `failed` and
`outcome_unknown`; totals/costs are SQL aggregates and items are paginated. Public PSL failure
categories are limited to `invalid_json`, `json_schema_mismatch`, `invalid_psl_result`,
`provider_error` and `outcome_unknown`. Active batches can be rediscovered over IPC, and Web keeps
only the current batch ID in `sessionStorage` to resume polling without reloading the page.

## Current scope

The product includes the Telegram archive, autonomous static catalog, explicit local frame
extraction for selected or confirmed batches of video notes, a resumable old-frame backfill, an explicit short-lived
loopback browser client, AI chats and manually initiated Telegram PSL from profile photos or one
video-note frame set for one, selected or publication-linked local profiles. Optional media/rclone adapters remain dormant code paths and
are not presented as user-facing tools. There is no persistent browser service, remote access,
PSL automation, multi-user infrastructure or collection beyond source posts, video-note authors
and the visible profile photos explicitly requested for this module. AI does not expand Telegram
collection on its own.
