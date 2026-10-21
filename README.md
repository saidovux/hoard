# Hoard 0.4.0.2

Hoard is a local-first Linux and Windows archive for Telegram posts, profiles and video notes
found in a channel post's comments. It uses Telethon, SQLite, an autonomous offline HTML catalog
and a small inline Rich/InquirerPy interface. A private background daemon performs all Telegram
work; closing the interface does not stop downloads.

This release exposes only the working Telegram archiver. The project has no web server,
Redis, Docker, Electron or fullscreen dashboard.

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
installation.

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

Run `hoard` and choose **Подключить Telegram**. Hoard performs the complete login as one
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

Choose **Источники и посты** → **Добавить пост**. The wizard:

1. accepts a public post URL such as `https://t.me/examplechannel/123`;
2. connects Telegram if needed and verifies that the account can access the post;
3. asks for the archive folder;
4. uses a 120-second interval or lets you enter 30–86400 seconds;
5. saves the source and offers to start an immediate scan.

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

The TUI catalog menu can rebuild the snapshot, show its path or open the existing single HTML
file in the default browser.

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

## Background operation

The UI communicates with one user-owned daemon over a mode-`0600` Unix socket on Linux (Windows uses authenticated loopback IPC). Selecting
**Выход**, pressing Ctrl+C at a menu, or closing the terminal closes only the interface.
The collector continues until it is paused, stopped or the daemon is shut down.

- **Pause** cancels active work safely and leaves it queued for **Continue**.
- **Stop** stops the collector while keeping the management daemon available.
- `hoard shutdown` closes both the collector and daemon cleanly.
- Reopening `hoard` reconnects to the existing daemon.

The interface is inline, does not enter the alternate screen and refreshes only while the
progress view is open. The terminal supplies the black background; Hoard uses orange
`#F79646`, white primary text and gray secondary text. Its centered workspace expands up to
112 columns, collapses cleanly on narrow terminals and replaces the previous view after every
completed prompt, so navigation does not leave duplicate menus on screen.
Lists support both arrow keys and Vim navigation: `j`/`k` move, `l` or Enter selects, and `h`
returns to the parent menu. The same `h` shortcut leaves the live progress view.

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

- `~/.config/hoard/config.json` — API credentials, mode `0600`
- `~/.local/share/hoard/telegram.session` — Telethon session
- `~/.local/share/hoard/hoard.sqlite3` — sources, Telegram metadata, downloads, jobs and events
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

See [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md) for implementation details.
