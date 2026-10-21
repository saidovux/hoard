# Architecture

Hoard is one local Python application split into an on-demand terminal client and one
background service.

## Process boundary

- `console_ui.py` renders the inline Rich/InquirerPy interface. It never imports or creates
  a `TelegramClient`; every operation goes through IPC.
- `tui/` is the optional full-screen Textual client launched only by `hoard tui`. `app.py` owns
  lifecycle, keyboard navigation and the single in-flight status refresh; `api.py` adapts the
  existing `ensure_daemon()` / `request("status")` calls; screens, status formatting, reusable
  widgets and TCSS remain separate. It imports neither storage nor Telethon.
- `ipc.py` starts the daemon detached when required and sends one JSON request per private
  Unix-socket connection.
- `service.py` owns the database, two independently limited job runners and the sole
  `TelegramEngine`. An asyncio lock
  serializes login, validation, scans and enrichment so one Telethon session is never opened by
  competing clients.
- `ai/` is provider-neutral and has no Telegram dependency. It defines requests, responses,
  stable errors, configuration, response validation and the provider contract; `deepseek.py`
  implements the first provider over outbound HTTPS.
- `telegram_storage.py` owns Telegram-specific tables and migrations while `storage.py` retains
  the shared source, job and event primitives for future modules.
- `catalog.py` opens SQLite independently and generates a static browser snapshot without owning a
  Telegram client. The template lives in the installed `hoard.templates.telegram` package.
- The socket and all XDG state directories are user-only. There is no TCP/HTTP listener.

Closing or cancelling the interface does not signal the service. Collector pause/stop cancels
active jobs through `JobRunner`, which returns interrupted jobs to `queued`; the daemon stays
available for management. The AI runner follows the daemon rather than the collector and uses its
own concurrency limit. Each runner selects only its registered job kinds, so a long AI request
cannot take a Telegram slot or acquire the Telegram lock. Interrupted AI jobs become `failed` and
require an explicit retry because a cancelled paid request may already have reached the provider.
Shutdown closes both runners, the Telegram client, database and socket.
Menu keybindings are defined once for arrow and `h/j/k/l` navigation. Telegram IPC calls render
an inline elapsed-time status with network/VPN guidance while the daemon owns the actual client.
Views use a bounded responsive workspace and clear only the primary terminal screen between
completed prompts; the UI never enters the alternate screen or refreshes while closed.

The Textual client is a separate presentation option and may enter application mode. Its Overview
screen performs no idle polling. The Activity screen owns a moderate 1.5-second timer only while
active, and the app's async lock drops overlapping ticks. Screen suspension, unmount and app exit
stop the timer. IPC startup and request errors render inside the stable black workspace so a slow
or unavailable daemon never produces an unexplained empty screen. Exiting the client sends no
collector, shutdown or Telegram command.

The Textual theme sets background, surface and panel to `#000000`; TCSS also fixes computed
backgrounds for containers, navigation components, focus/hover/selected states, inputs, tables,
menus and modals. Only Overview and Activity are exposed in round 2. Telegram workflows, AI chat
and settings remain hidden until their implementation rounds.

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

After login, scans reuse the connected client immediately. If the process is restarted during
login, the transient challenge is intentionally lost and a new code must be requested.

## Persistence, migration and scheduling

`storage.py` owns a synchronous SQLite connection inside the daemon event loop. Shared tables keep
sources, downloads, durable jobs and a bounded event history. Telegram adds versioned,
idempotently-created tables:

- `tg_profiles`, keyed only by stable Telegram `author_id`;
- `tg_username_history` with first/last observation timestamps per ID and username;
- `tg_profile_sources`, linking one profile to any number of source posts;
- `tg_profile_photos`, storing photo IDs, status, size and local paths;
- `tg_post_metadata` and `tg_post_media` for the parent posts;
- `tg_settings`, `tg_archive_roots` and `schema_versions` for archive configuration, safe access to
  earlier roots and migrations.

The migration only adds tables/columns. Its one-time legacy pass reads existing `downloads` and
safe JSON sidecars inside configured archive roots, backfills profile links and file state, and
leaves files and `sources.last_id` untouched. Unique constraints make the operation repeatable.
Old media may stay outside `telegram/`; the report emits a relative path or a `file:` URI when
Windows drives differ. It never creates an implicit second media archive.

Enabled sources become unique `telegram.scan` jobs when due. Telegram scans are serialized,
while the generic queue retains extension points for other modules. Profile photos, post metadata,
post media and report generation are separate deduplicated job kinds. A source checkpoint is
advanced after each processed comment. Media records are committed only after a file is fully
written, fsynced and atomically renamed. Missing or size-mismatched video notes move the scan
cursor back to the earliest affected message. A missing legacy JSON sidecar alone does not trigger
a network re-download because SQLite already has the required catalog metadata.

Profile photo enumeration is capped at 30 per check. Saved Telegram photo IDs prevent duplicate
downloads. The scheduler backfills due profile IDs already in SQLite and retries temporary
failures without rewinding any source checkpoint. Privacy/RPC failures and FloodWait retry times
are recorded on the profile and do not fail the source scan.

## Offline catalog

`<archive>/telegram_archive.html` is the only generated site file. The repository and wheel keep
HTML, CSS and JavaScript as three maintainable source resources; `catalog.py` embeds both assets
and the normalized SQLite snapshot into one HTML document at build time. This works under
`file://` without CORS-sensitive `fetch`. The generator validates media paths against configured
archive roots, escapes script-sensitive characters, writes one adjacent temporary file and
atomically replaces the report only after a complete successful build. The user-owned
`hoard_objects/` tree is never replaced. A dirty marker coalesces changes so a report is generated
after a scan or enrichment operation rather than after every message.

The report builder retires the short-lived split layout by moving
`<archive>/telegram/objects/` into `hoard_objects/` and rewriting only matching media paths in
SQLite. It never overwrites a conflicting file. Older user archive layouts elsewhere stay in
place and remain available through validated paths.

The browser uses DOM construction and `textContent`, never HTML interpolation. Original links are
restricted to HTTPS `t.me`; local media URLs accept only relative or `file:` references produced by
the generator. Pagination limits rows to 80, images are lazy, and videos use `preload=none`.

## Progress and errors

Telethon's progress callback supplies current and total bytes. Hoard measures transfer speed
from callback deltas and exposes it over IPC together with queue counts. The UI shows a percent
only when the total is non-zero and never invents an ETA. Routine Telegram failures are stored
as actionable messages; exception class details are returned only when local `HOARD_DEBUG=1`
is explicitly enabled.

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
automatic Telegram profile analysis exists in the core.

JSON requests set `response_format={"type":"json_object"}` and must explicitly instruct the model
to return JSON. A result is successful only when content is non-empty and `finish_reason` is
`stop`. JSON is parsed and optionally checked with a Draft 2020-12 schema. Invalid schemas fail
before the paid call; malformed, empty, schema-invalid and truncated provider responses fail after
one call and are never silently accepted. External schema references are rejected, and model
output is treated only as data: round 1 sends no tools and executes no generated commands.

DeepSeek errors are mapped to stable configuration, authentication, balance, rate-limit,
connection, request and provider-availability errors with a `retryable` signal, but round 1 never
acts on that signal automatically. The key comes from `DEEPSEEK_API_KEY` or the user-only
`ai.json`; it is excluded from object representations and never enters SQLite job payloads or
events. Token/cost persistence, prompts, chats, Telegram PSL and browser interaction belong to
later rounds.

## Current scope

The interactive product is the Telegram archive and its static local catalog. Optional
media/rclone adapters remain dormant code paths and are not presented as user-facing tools. There
is no browser service, AI user interface, PSL automation, multi-user infrastructure or collection
beyond source posts, video-note authors and the visible profile photos explicitly requested for
this module. The AI Core is internal infrastructure in round 1 and does not expand data collection
on its own.
