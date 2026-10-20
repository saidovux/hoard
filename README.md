# Hoard 0.3.0

Hoard is a local-first Linux archiver for Telegram video notes posted in a channel post's
comments. It uses Telethon, SQLite and a small inline Rich/InquirerPy interface. A private
background daemon performs all Telegram work; closing the interface does not stop downloads.

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

Only the daemon opens the Telethon session, so the interface never creates a competing
Telegram client.

## Add a post and archive video notes

Choose **Источники и посты** → **Добавить пост**. The wizard:

1. accepts a public post URL such as `https://t.me/examplechannel/123`;
2. connects Telegram if needed and verifies that the account can access the post;
3. asks for the archive folder;
4. uses a 120-second interval or lets you enter 30–86400 seconds;
5. saves the source and offers to start an immediate scan.

Hoard reads the linked comment thread and stores only Telegram round video messages. Each
MP4 gets a JSON sidecar containing the source post, comment link, message ID, date and the
public sender name/username and ID when available. It does not collect profile photos,
stories, hidden account data or unrelated message history.

Downloads are stored under:

```text
<archive>/<channel>/<post>/<author-id>/msg_<comment-id>.mp4
<archive>/<channel>/<post>/<author-id>/msg_<comment-id>.json
```

SQLite checkpoints and file-size checks prevent duplicate downloads and recover missing or
truncated archive files. The progress view reports the current file, real byte count, real
percentage when Telegram supplies a total, measured speed, queue state and recent errors.

## Background operation

The UI communicates with one user-owned daemon over a mode-`0600` Unix socket. Selecting
**Выход**, pressing Ctrl+C at a menu, or closing the terminal closes only the interface.
The collector continues until it is paused, stopped or the daemon is shut down.

- **Pause** cancels active work safely and leaves it queued for **Continue**.
- **Stop** stops the collector while keeping the management daemon available.
- `hoard shutdown` closes both the collector and daemon cleanly.
- Reopening `hoard` reconnects to the existing daemon.

The interface is inline, does not enter the alternate screen and refreshes only while the
progress view is open. The terminal supplies the black background; Hoard uses orange
`#F79646`, white primary text and gray secondary text.

## Non-interactive commands

```bash
hoard status --json
hoard start
hoard stop
hoard scan
hoard add https://t.me/examplechannel/123 --output ~/Videos/hoard --interval 120
hoard shutdown
```

`hoard add` preserves the script-friendly behavior and saves a syntactically valid source.
The interactive wizard additionally performs the live access check.

## Local files and privacy

- `~/.config/hoard/config.json` — API credentials, mode `0600`
- `~/.local/share/hoard/telegram.session` — Telethon session
- `~/.local/share/hoard/hoard.sqlite3` — sources, downloads, jobs and events
- `~/.local/state/hoard/hoard.sock` — private IPC socket
- `~/.local/state/hoard/daemon.log` — daemon diagnostics

The standard `XDG_CONFIG_HOME`, `XDG_DATA_HOME` and `XDG_STATE_HOME` variables override
these roots. Never publish the files above or a personal archive.

## Diagnostics and verification

```bash
hoard status --json
tail -n 100 ~/.local/state/hoard/daemon.log
python -m compileall -q src tests
ruff check .
pytest -q
```

Set `HOARD_DEBUG=1` only while diagnosing a local daemon problem; normal IPC errors omit
exception class details. Automated tests use fake Telegram clients and require no account,
code or network access. Live login and downloads still require your own authorized Telegram
account and content you are permitted to archive.

See [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md) for implementation details.
