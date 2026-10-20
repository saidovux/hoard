"""Synchronous SQLite store; operations are short and run in daemon event loop."""
from __future__ import annotations

import json
import sqlite3
import time
from pathlib import Path

from .models import Post
from .paths import ensure_private
from .telegram_storage import TelegramStorage, utc_now


class Store:
    def __init__(self, filename: Path, *, migrate_legacy: bool = True):
        self.filename = filename
        ensure_private(filename.parent)
        self.db = sqlite3.connect(filename)
        # SQLite creates files using the process umask; explicitly restrict data.
        filename.chmod(0o600)
        self.db.row_factory = sqlite3.Row
        self.db.execute("PRAGMA journal_mode=WAL")
        self.db.execute("PRAGMA busy_timeout=5000")
        self.db.executescript("""
          CREATE TABLE IF NOT EXISTS sources (
              id INTEGER PRIMARY KEY, username TEXT NOT NULL, post_id INTEGER NOT NULL,
              url TEXT NOT NULL, output TEXT NOT NULL, interval INTEGER NOT NULL DEFAULT 120,
              enabled INTEGER NOT NULL DEFAULT 1, last_id INTEGER NOT NULL DEFAULT 0,
              next_at REAL NOT NULL DEFAULT 0, last_error TEXT NOT NULL DEFAULT '',
              title TEXT NOT NULL DEFAULT '', last_checked_at TEXT NOT NULL DEFAULT '',
              last_result TEXT NOT NULL DEFAULT '',
              UNIQUE(username,post_id)
          );
          CREATE TABLE IF NOT EXISTS downloads (
              source_id INTEGER NOT NULL, message_id INTEGER NOT NULL,
              author_id INTEGER, author_username TEXT NOT NULL DEFAULT '',
              author_name TEXT NOT NULL DEFAULT '', date TEXT NOT NULL,
              media_url TEXT NOT NULL, file_path TEXT NOT NULL, size INTEGER NOT NULL,
              PRIMARY KEY(source_id,message_id)
          );
          CREATE TABLE IF NOT EXISTS jobs (
              id INTEGER PRIMARY KEY,
              kind TEXT NOT NULL,
              payload TEXT NOT NULL,
              unique_key TEXT,
              state TEXT NOT NULL DEFAULT 'queued',
              attempts INTEGER NOT NULL DEFAULT 0,
              result TEXT NOT NULL DEFAULT '',
              created_at TEXT NOT NULL DEFAULT (datetime('now')),
              updated_at TEXT NOT NULL DEFAULT (datetime('now'))
          );
          CREATE UNIQUE INDEX IF NOT EXISTS jobs_active_key
              ON jobs(unique_key) WHERE unique_key IS NOT NULL AND state IN ('queued','running');
          CREATE TABLE IF NOT EXISTS events (
              id INTEGER PRIMARY KEY, created_at TEXT NOT NULL DEFAULT (datetime('now')),
              kind TEXT NOT NULL, text TEXT NOT NULL
          );
        """)
        # Existing archives are migrated only by adding nullable/defaulted metadata.
        columns = {
            str(row["name"])
            for row in self.db.execute("PRAGMA table_info(sources)")
        }
        additions = {
            "title": "TEXT NOT NULL DEFAULT ''",
            "last_checked_at": "TEXT NOT NULL DEFAULT ''",
            "last_result": "TEXT NOT NULL DEFAULT ''",
        }
        for name, declaration in additions.items():
            if name not in columns:
                self.db.execute(f"ALTER TABLE sources ADD COLUMN {name} {declaration}")
        self.db.commit()
        self.telegram = TelegramStorage(self.db, migrate_legacy=migrate_legacy)

    def close(self) -> None:
        self.db.close()

    def event(self, kind: str, text: str) -> None:
        self.db.execute("INSERT INTO events(kind,text) VALUES(?,?)", (kind, text[:500]))
        self.db.execute(
            "DELETE FROM events WHERE id NOT IN "
            "(SELECT id FROM events ORDER BY id DESC LIMIT 300)"
        )
        self.db.commit()

    def add_source(
        self,
        post: Post,
        output: str,
        interval: int = 120,
        *,
        title: str = "",
    ) -> int:
        if not 30 <= interval <= 86400:
            raise ValueError("Интервал должен быть от 30 секунд до 24 часов")
        if not output.strip():
            raise ValueError("Укажи каталог для архива")
        directory = Path(output).expanduser().resolve()
        if directory.exists() and not directory.is_dir():
            raise ValueError("Путь архива должен указывать на каталог")
        # No directory is created on add, so typos don't create unused directories.
        with self.db:
            self.db.execute("""INSERT INTO sources(username,post_id,url,output,interval,title)
              VALUES(?,?,?,?,?,?) ON CONFLICT(username,post_id)
              DO UPDATE SET output=excluded.output, interval=excluded.interval,
              title=CASE WHEN excluded.title='' THEN sources.title ELSE excluded.title END,
              enabled=1, next_at=0""", (
                post.username,
                post.message_id,
                post.link,
                str(directory),
                interval,
                title.strip()[:200],
            ))
        row = self.db.execute(
            "SELECT id FROM sources WHERE username=? AND post_id=?",
            (post.username, post.message_id),
        ).fetchone()
        self.telegram.ensure_archive_root(directory)
        self.telegram.set_catalog_dirty()
        return int(row["id"])

    def sources(self) -> list[dict]:
        return [
            dict(row)
            for row in self.db.execute(
                """SELECT sources.*, COUNT(downloads.message_id) AS download_count
                FROM sources LEFT JOIN downloads ON downloads.source_id=sources.id
                GROUP BY sources.id ORDER BY sources.id"""
            )
        ]

    def due_sources(self) -> list[dict]:
        return [
            dict(row)
            for row in self.db.execute(
                "SELECT * FROM sources WHERE enabled=1 AND next_at<=? ORDER BY next_at,id",
                (time.time(),),
            )
        ]

    def set_enabled(self, source_id: int, enabled: bool) -> None:
        with self.db:
            c = self.db.execute("UPDATE sources SET enabled=?,next_at=0 WHERE id=?",
                                (int(enabled), source_id))
            if c.rowcount == 0:
                raise ValueError("Источник не найден")
        self.telegram.set_catalog_dirty()

    def remove_source(self, source_id: int) -> None:
        # Explicitly avoid deleting downloaded files or historic DB records.
        with self.db:
            c = self.db.execute("UPDATE sources SET enabled=0 WHERE id=?", (source_id,))
            if c.rowcount == 0:
                raise ValueError("Источник не найден")
        self.telegram.set_catalog_dirty()

    def schedule(
        self,
        source_id: int,
        seconds: int,
        error: str = "",
        result: str = "",
    ) -> None:
        with self.db:
            self.db.execute(
                """UPDATE sources SET next_at=?,last_error=?,last_checked_at=datetime('now'),
                last_result=? WHERE id=?""",
                (time.time() + seconds, error[:500], result[:500], source_id),
            )
        self.telegram.set_catalog_dirty()

    def recorded_download_size(self, source_id: int, message_id: int) -> int | None:
        row = self.db.execute("SELECT size FROM downloads WHERE source_id=? AND message_id=?",
                              (source_id, message_id)).fetchone()
        return int(row["size"]) if row is not None else None

    def earliest_missing_download(self, source_id: int) -> int | None:
        """Find missing/corrupt archive files even if the message cursor passed them."""
        for row in self.db.execute(
            "SELECT message_id,file_path,size FROM downloads WHERE source_id=? ORDER BY message_id",
            (source_id,),
        ):
            path = Path(row["file_path"])
            try:
                available = path.is_file() and path.stat().st_size == row["size"]
            except OSError:
                available = False
            if not available:
                return int(row["message_id"])
        return None

    def seen(self, source_id: int, message_id: int) -> bool:
        row = self.db.execute(
            "SELECT file_path,size FROM downloads WHERE source_id=? AND message_id=?",
            (source_id, message_id),
        ).fetchone()
        if row is None:
            return False
        file = Path(row["file_path"])
        try:
            return file.is_file() and file.stat().st_size == row["size"] and row["size"] > 0
        except OSError:
            return False

    def checkpoint(self, source_id: int, message_id: int, video: dict | None = None) -> None:
        # Store media record and checkpoint atomically, only AFTER successful write.
        payload = dict(video or {})
        if payload:
            payload.setdefault("observed_at", utc_now())
            payload.setdefault("state", "saved")
        with self.db:
            if payload:
                self.db.execute("""INSERT INTO downloads(source_id,message_id,author_id,
                    author_username,author_name,date,media_url,file_path,size,observed_at,state)
                    VALUES(:source_id,:message_id,:author_id,:author_username,:author_name,
                    :date,:media_url,:file_path,:size,:observed_at,:state)
                    ON CONFLICT(source_id,message_id) DO UPDATE SET
                      author_id=excluded.author_id, author_username=excluded.author_username,
                      author_name=excluded.author_name, file_path=excluded.file_path,
                      size=excluded.size,observed_at=excluded.observed_at,
                      state=excluded.state""", payload)
            self.db.execute("""UPDATE sources SET last_id=MAX(last_id,?) WHERE id=?""",
                            (message_id, source_id))
        if payload:
            self.telegram.set_catalog_dirty()
            if payload.get("author_id") is not None:
                self.telegram.observe_profile(
                    author_id=int(payload["author_id"]),
                    username=str(payload.get("author_username") or ""),
                    display_name=str(payload.get("author_name") or ""),
                    observed_at=str(payload["observed_at"]),
                    source_id=source_id,
                )

    def recent_downloads(self, count: int = 40) -> list[dict]:
        return [dict(r) for r in self.db.execute("""SELECT downloads.*,
            sources.username AS source_name, sources.post_id
            FROM downloads LEFT JOIN sources ON downloads.source_id=sources.id
            ORDER BY downloads.rowid DESC LIMIT ?""", (count,))]

    def recent_events(self, count: int = 30) -> list[dict]:
        return [dict(r) for r in self.db.execute(
            "SELECT created_at,kind,text FROM events ORDER BY id DESC LIMIT ?", (count,))]

    def stats(self) -> dict:
        source_count = self.db.execute("SELECT COUNT(*) FROM sources WHERE enabled=1").fetchone()[0]
        downloaded = self.db.execute("SELECT COUNT(*) FROM downloads").fetchone()[0]
        authors = self.db.execute("SELECT COUNT(*) FROM tg_profiles").fetchone()[0]
        return {"sources": source_count, "downloads": downloaded, "authors": authors}

    def queue_stats(self) -> dict[str, int]:
        result = {"queued": 0, "running": 0, "failed": 0}
        for row in self.db.execute(
            "SELECT state,COUNT(*) AS amount FROM jobs "
            "WHERE state IN ('queued','running','failed') GROUP BY state"
        ):
            result[str(row["state"])] = int(row["amount"])
        return result

    # General queue: not tied to Telegram. Old databases migrate additively.
    def enqueue(self, kind: str, payload: dict, unique_key: str | None = None) -> int:
        with self.db:
            if unique_key is not None:
                row = self.db.execute("""SELECT id FROM jobs WHERE unique_key=?
                    AND state IN ('queued','running') LIMIT 1""", (unique_key,)).fetchone()
                if row:
                    return int(row["id"])
            cur = self.db.execute("INSERT INTO jobs(kind,payload,unique_key) VALUES(?,?,?)",
                                  (kind, json.dumps(payload, ensure_ascii=False), unique_key))
            return int(cur.lastrowid)

    def queued_jobs(self, limit: int = 3) -> list[dict]:
        return [dict(r) for r in self.db.execute(
            "SELECT * FROM jobs WHERE state='queued' ORDER BY id LIMIT ?", (limit,))]

    def recent_jobs(self, count: int = 80) -> list[dict]:
        return [dict(r) for r in self.db.execute(
            "SELECT id,kind,state,attempts,result,created_at,updated_at "
            "FROM jobs ORDER BY id DESC LIMIT ?",
            (count,))]

    def mark_running(self, jid: int) -> bool:
        with self.db:
            cur = self.db.execute("""UPDATE jobs SET state='running', attempts=attempts+1,
                updated_at=datetime('now') WHERE id=? AND state='queued'""", (jid,))
            return cur.rowcount == 1

    def finish_job(self, jid: int, state: str, result: str) -> None:
        if state not in {"done", "failed", "queued"}:
            raise ValueError("Invalid job state")
        with self.db:
            self.db.execute("""UPDATE jobs SET state=?,result=?,updated_at=datetime('now')
                WHERE id=?""", (state, result[:500], jid))

    def recover_interrupted_jobs(self) -> None:
        with self.db:
            self.db.execute("""UPDATE jobs SET state='queued',result='Восстановлено после остановки'
                WHERE state='running'""")

    def retry_job(self, jid: int) -> None:
        with self.db:
            cur = self.db.execute(
                "UPDATE jobs SET state='queued',result='' WHERE id=? AND state='failed'",
                (jid,),
            )
            if cur.rowcount != 1:
                raise ValueError("Нужна существующая задача со статусом failed")
