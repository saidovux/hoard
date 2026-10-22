# Hoard 0.5.0

Hoard is a local-first Linux and Windows archive for Telegram posts, profiles and video notes
found in a channel post's comments. It uses Telethon, SQLite, an autonomous offline HTML catalog
and terminal interfaces. A private background daemon performs all Telegram work; closing an
interface does not stop downloads.

This release adds an explicit AI workspace and optional, manually initiated analysis of locally
saved Telegram profile photos. It never analyzes a profile on its own. The project has no web
server, Redis, Docker or Electron.

## Terminal interfaces

The round-three interface is a complete Telegram workspace:

```bash
hoard tui
```

Its implemented sections are **Обзор**, **Telegram**, **Источники**, **Активность**, **Архив** and
**AI**.
They cover login including 2FA, collector start/pause/stop, source validation and editing,
selected-source or global scans, downloads, profile summaries, jobs, errors, enrichment,
manual HTML generation, AI chats and AI settings. All operations use the private daemon IPC
layer; the TUI never opens
SQLite or creates a Telegram client. Only the visible Activity screen polls, with one status
request in flight at a time.

The entire Textual surface, including navigation, focus, hover and help modal states, uses
`#000000`; text is white, secondary text is `#888888`, separators are `#262626` and the accent is
`#F79646`. Use `j`/`k` or arrows to move, `l`, Right or Enter to open, `h`, Left or Esc to return,
`?` for help, `b` to toggle the sidebar and `q` to close only the UI. `[` and `]` switch sections
even when the sidebar is hidden. Tables, buttons and inputs retain their native keyboard behavior.
On terminals narrower than 60 columns the sidebar hides automatically.

`hoard` and `hoard ui` continue to open the established inline
Rich/InquirerPy interface. Scripts and all existing non-interactive commands are unchanged and do
not import Textual.

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
hoard tui
```

For development, install the test tools with `python -m pip install -e '.[dev]'`.

An upgrade keeps the existing XDG configuration, database, Telegram session and archive.
If an older daemon is still running, execute `hoard shutdown` before starting the upgraded
installation.

## Windows installation (PowerShell)

Install Python 3.11+ and Git; open Windows Terminal:

```powershell
git clone https://github.com/saidovux/hoard.git
cd hoard
py -3 -m venv .venv
.\.venv\Scripts\python.exe -m pip install -e .
.\.venv\Scripts\hoard.exe tui
```

No PowerShell execution policy changes are needed. Windows stores credentials in
`%APPDATA%\Hoard` and data and service state under `%LOCALAPPDATA%\Hoard`.
The background service binds only to 127.0.0.1 using a random port and a
per-start token saved in `state\endpoint.json`. Keep the token file private.
Use `hoard shutdown` to fully close the service. Windows CI does not test live Telegram.

## First launch and Telegram login

Run `hoard tui`, open **Telegram** and choose **Подключить**. Hoard performs the complete login as one
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

Open **Источники** → **Добавить**. The wizard:

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
truncated archive files. The progress view reports the current file, real byte count, real
percentage when Telegram supplies a total, measured speed, queue state and recent errors.

Existing pre-0.4 paths are not moved, copied or deleted. Their SQLite records and safe JSON
sidecars are migrated in place and referenced by the catalog. Existing source cursors are kept.

## Offline HTML catalog

Choose **Обновить HTML-каталог** in the terminal interface or run:

```bash
hoard telegram report
hoard telegram report --archive ~/Videos/hoard
```

The command reads SQLite and rebuilds the snapshot without starting the daemon, connecting to
Telegram or requiring internet access. Open `<archive>/telegram_archive.html` directly in Firefox,
Chromium or another local browser. CSS, JavaScript and the SQLite snapshot are embedded in that
single HTML file; it has no companion site assets, web server, `fetch`, CDN, analytics or external
font dependency. Search and sorting work locally across posts, IDs, current and observed
usernames, display names and channels. Media uses lazy loading, pagination and
`<video controls preload="none">`.

The TUI Archive screen can rebuild the snapshot and shows its path together with downloads,
profiles and jobs. Open the resulting single HTML file in a local browser when needed.

For maintainability, source HTML, CSS and JavaScript remain separate and are packaged at
`src/hoard/templates/telegram/`; the generator assembles them only at report time. Each report
atomically replaces `telegram_archive.html` while preserving `hoard_objects/`. SQLite remains the
source of truth. Media from the short-lived split layout `<archive>/telegram/objects/` is safely
moved into `hoard_objects/` and its SQLite paths are updated; unrelated legacy archive layouts
remain in place. Conflicting or unexpected files are preserved instead of overwritten.

Optional enrichment obtains post text/media and up to 30 latest profile photos visible to the
authorized account:

```bash
hoard telegram enrich
```

Enrichment is queued separately from video-note scans, respects Telegram privacy and FloodWait,
deduplicates media IDs and does not stop the primary scan when a profile photo is unavailable.
The collector automatically queues an initial photo check for profiles already stored in SQLite
and durable retries when a temporary failure becomes due; it does not rewind source checkpoints.
Start the collector first if it is stopped. Stories, hidden data and unrelated message history
are not requested. The offline report remains usable without enrichment.

## Hoard AI workspace — 0.5.0

`hoard.ai` is an independent subsystem; it does not import Telegram or change Telegram archives.
DeepSeek is its first provider through a generic provider interface. The default model is
`deepseek-flash`. The core accepts text and explicitly selected JPEG, PNG, GIF or WebP images, and
supports normal responses and JSON Output with local Draft 2020-12 JSON Schema validation.

Set `DEEPSEEK_API_KEY` for the daemon environment, or open **AI → Настроить ключ** to store it in
the private `~/.config/hoard/ai.json` file. The key field is masked and the daemon never returns
the saved secret. The key is never placed in job payloads, responses, HTML or public logs. AI
requests have no automatic provider retry: an error, truncated response, empty response or
interrupted paid job requires an explicit user decision.

The single Hoard daemon owns two independent job pools. `hoard start`, `stop` and `pause` control
the Telegram pool; the AI pool follows the daemon lifecycle and keeps working when the Telegram
collector is stopped. Their concurrency limits are separate: `HOARD_MAX_JOBS` for the existing
pool and `HOARD_AI_MAX_JOBS` (default `2`) for AI. A long DeepSeek request therefore cannot occupy
a Telegram worker or Telegram lock.

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
ai_telegram_profile_get/photos/photo_get
ai_telegram_psl_start  ai_telegram_analysis_get/list
```

Lists and history are paginated, and both IPC directions retain the 64 KiB message limit. New chat
image jobs store only attachment IDs; the explicitly selected image is copied once into a private
local directory instead of being duplicated as Base64 in SQLite jobs. Old serialized `ai.request`
payloads remain supported and now receive accounting records without a payload migration.

Prompts, chosen memory and message history are sent to the configured external provider only after
the explicit send action. Memory is never inferred from Telegram profiles, never shared between
unrelated scopes and is presented to the model as user-selected data rather than system
instructions. Provider behavior and orchestration are covered with fakes; the test suite makes no
paid DeepSeek requests.

## Telegram AI and subjective PSL

In **Архив → Профили**, select a saved profile and choose **AI-анализ профиля**. Hoard shows only
photos already stored by explicit Telegram enrichment; it never downloads a new photo just for
AI. Choose one to four available photos, a Telegram prompt/version and a model. Before queueing,
you must explicitly confirm both that the person is known to be at least 18 from information
outside the image and that the selected photos/text may be sent to DeepSeek.

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
currency-separated estimated costs and selected photo IDs remain in SQLite. The offline HTML is
intentionally unchanged until round 6.

## Background operation

The UI communicates with one user-owned daemon over a mode-`0600` Unix socket on Linux (Windows uses authenticated loopback IPC). Selecting
**Выход**, pressing Ctrl+C at a menu, or closing the terminal closes only the interface.
The collector continues until it is paused, stopped or the daemon is shut down.

- **Pause** cancels active work safely and leaves it queued for **Continue**.
- **Stop** stops the collector while keeping the management daemon available.
- `hoard shutdown` closes both the collector and daemon cleanly.
- Reopening `hoard` reconnects to the existing daemon.

The legacy interface is inline, does not enter the alternate screen and refreshes only while the
progress view is open. The terminal supplies its black background; Hoard uses orange
`#F79646`, white primary text and gray secondary text. Its centered workspace expands up to
112 columns, collapses cleanly on narrow terminals and replaces the previous view after every
completed prompt, so navigation does not leave duplicate menus on screen.
Lists support both arrow keys and Vim navigation: `j`/`k` move, `l` or Enter selects, and `h`
returns to the parent menu. The same `h` shortcut leaves the live progress view.

`hoard tui` is intentionally full-screen and restores the original terminal on exit. Its
Overview and management screens are idle; only the visible Activity screen refreshes status.
Both interfaces leave
the same daemon and its Telegram and AI queues running after exit.

## Non-interactive commands

```bash
hoard status --json
hoard start
hoard stop
hoard scan
hoard add https://t.me/examplechannel/123 --output ~/Videos/hoard --interval 120
hoard telegram report
hoard telegram enrich
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
account and content you are permitted to archive. Automated browser verification is attempted
when a compatible browser is available; generation and security behavior are covered without one.
GitHub Actions is manual-dispatch only and is not required for release verification.

See [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md) for implementation details.
