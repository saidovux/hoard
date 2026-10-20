"""Telegram-specific SQLite schema and non-destructive legacy migration."""
from __future__ import annotations

import json
import time
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from .archive_paths import default_archive_root, path_is_within

TELEGRAM_SCHEMA_VERSION = 1


def utc_now() -> str:
    return datetime.now(UTC).isoformat()


def _first(values: list[str]) -> str:
    present = [value for value in values if value]
    return min(present) if present else ""


def _last(values: list[str]) -> str:
    present = [value for value in values if value]
    return max(present) if present else ""


class TelegramStorage:
    """Telegram data layer sharing Hoard's existing SQLite connection."""

    def __init__(self, db, *, migrate_legacy: bool = True):
        self.db = db
        self._migrate_schema()
        if migrate_legacy:
            self.migrate_legacy_downloads()
        self._adopt_legacy_archive_root()

    def _migrate_schema(self) -> None:
        self.db.executescript(
            """
            CREATE TABLE IF NOT EXISTS schema_versions (
                module TEXT PRIMARY KEY,
                version INTEGER NOT NULL,
                updated_at TEXT NOT NULL DEFAULT (datetime('now'))
            );
            CREATE TABLE IF NOT EXISTS tg_settings (
                key TEXT PRIMARY KEY,
                value TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS tg_archive_roots (
                path TEXT PRIMARY KEY,
                first_used_at TEXT NOT NULL DEFAULT (datetime('now')),
                last_used_at TEXT NOT NULL DEFAULT (datetime('now'))
            );
            CREATE TABLE IF NOT EXISTS tg_profiles (
                author_id INTEGER PRIMARY KEY,
                current_username TEXT NOT NULL DEFAULT '',
                display_name TEXT NOT NULL DEFAULT '',
                first_seen TEXT NOT NULL DEFAULT '',
                last_seen TEXT NOT NULL DEFAULT '',
                photos_checked_at TEXT NOT NULL DEFAULT '',
                photo_error TEXT NOT NULL DEFAULT '',
                photos_retry_at REAL NOT NULL DEFAULT 0
            );
            CREATE TABLE IF NOT EXISTS tg_username_history (
                author_id INTEGER NOT NULL,
                username TEXT NOT NULL,
                first_seen TEXT NOT NULL DEFAULT '',
                last_seen TEXT NOT NULL DEFAULT '',
                PRIMARY KEY(author_id, username)
            );
            CREATE TABLE IF NOT EXISTS tg_profile_sources (
                author_id INTEGER NOT NULL,
                source_id INTEGER NOT NULL,
                first_seen TEXT NOT NULL DEFAULT '',
                last_seen TEXT NOT NULL DEFAULT '',
                PRIMARY KEY(author_id, source_id)
            );
            CREATE TABLE IF NOT EXISTS tg_profile_photos (
                author_id INTEGER NOT NULL,
                photo_id TEXT NOT NULL,
                file_path TEXT NOT NULL,
                size INTEGER NOT NULL DEFAULT 0,
                photo_date TEXT NOT NULL DEFAULT '',
                status TEXT NOT NULL DEFAULT 'saved',
                error TEXT NOT NULL DEFAULT '',
                PRIMARY KEY(author_id, photo_id)
            );
            CREATE TABLE IF NOT EXISTS tg_post_metadata (
                source_id INTEGER PRIMARY KEY,
                channel_title TEXT NOT NULL DEFAULT '',
                post_text TEXT NOT NULL DEFAULT '',
                published_at TEXT NOT NULL DEFAULT '',
                fetched_at TEXT NOT NULL DEFAULT '',
                status TEXT NOT NULL DEFAULT 'partial',
                error TEXT NOT NULL DEFAULT ''
            );
            CREATE TABLE IF NOT EXISTS tg_post_media (
                source_id INTEGER NOT NULL,
                media_id TEXT NOT NULL,
                kind TEXT NOT NULL DEFAULT 'file',
                file_path TEXT NOT NULL,
                size INTEGER NOT NULL DEFAULT 0,
                status TEXT NOT NULL DEFAULT 'saved',
                error TEXT NOT NULL DEFAULT '',
                PRIMARY KEY(source_id, media_id)
            );
            CREATE INDEX IF NOT EXISTS tg_profiles_username_idx
                ON tg_profiles(current_username COLLATE NOCASE);
            CREATE INDEX IF NOT EXISTS tg_profiles_name_idx
                ON tg_profiles(display_name COLLATE NOCASE);
            CREATE INDEX IF NOT EXISTS tg_profiles_seen_idx
                ON tg_profiles(last_seen);
            CREATE INDEX IF NOT EXISTS tg_username_history_username_idx
                ON tg_username_history(username COLLATE NOCASE);
            CREATE INDEX IF NOT EXISTS tg_profile_sources_source_idx
                ON tg_profile_sources(source_id, author_id);
            CREATE INDEX IF NOT EXISTS tg_profile_photos_author_idx
                ON tg_profile_photos(author_id, photo_date DESC);
            """
        )
        download_columns = {
            str(row["name"]) for row in self.db.execute("PRAGMA table_info(downloads)")
        }
        additions = {
            "state": "TEXT NOT NULL DEFAULT 'saved'",
            "observed_at": "TEXT NOT NULL DEFAULT ''",
        }
        for name, declaration in additions.items():
            if name not in download_columns:
                self.db.execute(f"ALTER TABLE downloads ADD COLUMN {name} {declaration}")
        self.db.execute(
            """INSERT INTO schema_versions(module,version) VALUES('telegram',?)
            ON CONFLICT(module) DO UPDATE SET
            version=MAX(schema_versions.version,excluded.version),
            updated_at=datetime('now')""",
            (TELEGRAM_SCHEMA_VERSION,),
        )
        self.db.commit()

    def _adopt_legacy_archive_root(self) -> None:
        configured = self.get_setting("archive_root")
        if configured:
            self._remember_archive_root(Path(configured))
            return
        row = self.db.execute("SELECT output FROM sources ORDER BY id LIMIT 1").fetchone()
        if row is not None and str(row["output"] or "").strip():
            self._persist_archive_root(
                Path(str(row["output"])).expanduser().resolve()
            )

    def _remember_archive_root(self, root: Path) -> None:
        with self.db:
            self.db.execute(
                """INSERT INTO tg_archive_roots(path) VALUES(?)
                ON CONFLICT(path) DO UPDATE SET last_used_at=datetime('now')""",
                (str(root.expanduser().resolve()),),
            )

    def _persist_archive_root(self, root: Path) -> None:
        resolved = root.expanduser().resolve()
        with self.db:
            self.db.execute(
                """INSERT INTO tg_settings(key,value) VALUES('archive_root',?)
                ON CONFLICT(key) DO UPDATE SET value=excluded.value""",
                (str(resolved),),
            )
            self.db.execute(
                """INSERT INTO tg_archive_roots(path) VALUES(?)
                ON CONFLICT(path) DO UPDATE SET last_used_at=datetime('now')""",
                (str(resolved),),
            )

    def get_setting(self, key: str, default: str = "") -> str:
        row = self.db.execute("SELECT value FROM tg_settings WHERE key=?", (key,)).fetchone()
        return str(row["value"]) if row is not None else default

    def set_setting(self, key: str, value: str) -> None:
        with self.db:
            self.db.execute(
                """INSERT INTO tg_settings(key,value) VALUES(?,?)
                ON CONFLICT(key) DO UPDATE SET value=excluded.value""",
                (key, value),
            )

    def set_catalog_dirty(self, dirty: bool = True) -> None:
        self.set_setting("catalog_dirty", "1" if dirty else "0")

    def catalog_dirty(self) -> bool:
        return self.get_setting("catalog_dirty", "0") == "1"

    def archive_root(self, preferred: str | Path | None = None, *, persist: bool = False) -> Path:
        if preferred is not None:
            root = Path(preferred).expanduser().resolve()
            if root.exists() and not root.is_dir():
                raise ValueError("Корень архива должен быть каталогом")
            if persist:
                self._persist_archive_root(root)
                self.set_catalog_dirty()
            return root
        configured = self.get_setting("archive_root")
        if configured:
            return Path(configured).expanduser().resolve()
        row = self.db.execute("SELECT output FROM sources ORDER BY id LIMIT 1").fetchone()
        if row is not None and str(row["output"]).strip():
            root = Path(str(row["output"])).expanduser().resolve()
        else:
            root = default_archive_root()
        if persist:
            self._persist_archive_root(root)
        return root

    def ensure_archive_root(self, preferred: str | Path) -> Path:
        configured = self.get_setting("archive_root")
        return self.archive_root(configured or preferred, persist=not bool(configured))

    def allowed_media_roots(self, archive_root: Path) -> list[Path]:
        roots = [archive_root.expanduser().resolve()]
        for row in self.db.execute("SELECT path FROM tg_archive_roots"):
            value = str(row["path"] or "").strip()
            if value:
                candidate = Path(value).expanduser().resolve()
                if candidate not in roots:
                    roots.append(candidate)
        for row in self.db.execute("SELECT DISTINCT output FROM sources"):
            value = str(row["output"] or "").strip()
            if value:
                candidate = Path(value).expanduser().resolve()
                if candidate not in roots:
                    roots.append(candidate)
        return roots

    def _observe_profile(
        self,
        *,
        author_id: int,
        username: str | None,
        display_name: str,
        observed_at: str,
        source_id: int | None,
    ) -> None:
        current = self.db.execute(
            "SELECT * FROM tg_profiles WHERE author_id=?", (author_id,)
        ).fetchone()
        if current is None:
            self.db.execute(
                """INSERT INTO tg_profiles(author_id,current_username,display_name,
                first_seen,last_seen) VALUES(?,?,?,?,?)""",
                (author_id, username or "", display_name, observed_at, observed_at),
            )
        else:
            first_seen = _first([str(current["first_seen"]), observed_at])
            last_seen = _last([str(current["last_seen"]), observed_at])
            is_newest = (
                not current["last_seen"]
                or not observed_at
                or observed_at >= current["last_seen"]
            )
            next_username = str(current["current_username"])
            next_name = str(current["display_name"])
            if is_newest:
                if username is not None:
                    next_username = username
                if display_name:
                    next_name = display_name
            self.db.execute(
                """UPDATE tg_profiles SET current_username=?,display_name=?,
                first_seen=?,last_seen=? WHERE author_id=?""",
                (next_username, next_name, first_seen, last_seen, author_id),
            )
        if username:
            row = self.db.execute(
                """SELECT first_seen,last_seen FROM tg_username_history
                WHERE author_id=? AND username=?""",
                (author_id, username),
            ).fetchone()
            if row is None:
                self.db.execute(
                    """INSERT INTO tg_username_history(author_id,username,first_seen,last_seen)
                    VALUES(?,?,?,?)""",
                    (author_id, username, observed_at, observed_at),
                )
            else:
                self.db.execute(
                    """UPDATE tg_username_history SET first_seen=?,last_seen=?
                    WHERE author_id=? AND username=?""",
                    (
                        _first([str(row["first_seen"]), observed_at]),
                        _last([str(row["last_seen"]), observed_at]),
                        author_id,
                        username,
                    ),
                )
        if source_id is not None:
            row = self.db.execute(
                """SELECT first_seen,last_seen FROM tg_profile_sources
                WHERE author_id=? AND source_id=?""",
                (author_id, source_id),
            ).fetchone()
            if row is None:
                self.db.execute(
                    """INSERT INTO tg_profile_sources(author_id,source_id,first_seen,last_seen)
                    VALUES(?,?,?,?)""",
                    (author_id, source_id, observed_at, observed_at),
                )
            else:
                self.db.execute(
                    """UPDATE tg_profile_sources SET first_seen=?,last_seen=?
                    WHERE author_id=? AND source_id=?""",
                    (
                        _first([str(row["first_seen"]), observed_at]),
                        _last([str(row["last_seen"]), observed_at]),
                        author_id,
                        source_id,
                    ),
                )

    def observe_profile(
        self,
        *,
        author_id: int,
        username: str | None,
        display_name: str,
        observed_at: str | None = None,
        source_id: int | None = None,
    ) -> None:
        with self.db:
            self._observe_profile(
                author_id=int(author_id),
                username=username,
                display_name=display_name[:300],
                observed_at=observed_at or utc_now(),
                source_id=source_id,
            )
            self._set_dirty_sql()

    def _set_dirty_sql(self) -> None:
        self.db.execute(
            """INSERT INTO tg_settings(key,value) VALUES('catalog_dirty','1')
            ON CONFLICT(key) DO UPDATE SET value='1'"""
        )

    def migrate_legacy_downloads(self) -> None:
        if self.get_setting("legacy_downloads_v1") == "done":
            return
        rows = list(
            self.db.execute(
                """SELECT downloads.*, sources.output, sources.last_checked_at
                FROM downloads JOIN sources ON sources.id=downloads.source_id
                ORDER BY downloads.date,downloads.source_id,downloads.message_id"""
            )
        )
        with self.db:
            for raw in rows:
                item = dict(raw)
                sidecar = self._legacy_sidecar(item)
                author = sidecar.get("author") if isinstance(sidecar.get("author"), dict) else {}
                author_id = item.get("author_id")
                if author_id is None and isinstance(author.get("id"), int):
                    author_id = int(author["id"])
                username = str(item.get("author_username") or author.get("username") or "")
                name = str(item.get("author_name") or author.get("name") or "")
                observed_at = str(item.get("observed_at") or item.get("last_checked_at") or "")
                if not observed_at:
                    observed_at = self._sidecar_mtime(item)
                state = self._legacy_file_state(item)
                self.db.execute(
                    """UPDATE downloads SET author_id=COALESCE(author_id,?),
                    author_username=CASE WHEN author_username='' THEN ? ELSE author_username END,
                    author_name=CASE WHEN author_name='' THEN ? ELSE author_name END,
                    observed_at=CASE WHEN observed_at='' THEN ? ELSE observed_at END,
                    state=? WHERE source_id=? AND message_id=?""",
                    (
                        author_id,
                        username,
                        name,
                        observed_at,
                        state,
                        item["source_id"],
                        item["message_id"],
                    ),
                )
                if author_id is not None:
                    self._observe_profile(
                        author_id=int(author_id),
                        username=username or None,
                        display_name=name,
                        observed_at=observed_at,
                        source_id=int(item["source_id"]),
                    )
            if rows:
                self._set_dirty_sql()
            self.db.execute(
                """INSERT INTO tg_settings(key,value) VALUES('legacy_downloads_v1','done')
                ON CONFLICT(key) DO UPDATE SET value='done'"""
            )

    @staticmethod
    def _sidecar_mtime(item: dict[str, Any]) -> str:
        try:
            stamp = Path(str(item["file_path"])).with_suffix(".json").stat().st_mtime
        except (OSError, ValueError):
            return ""
        return datetime.fromtimestamp(stamp, UTC).isoformat()

    @staticmethod
    def _legacy_file_state(item: dict[str, Any]) -> str:
        try:
            path = Path(str(item["file_path"]))
            expected = int(item.get("size") or 0)
            if path.is_file() and path.stat().st_size > 0:
                return "saved" if not expected or path.stat().st_size == expected else "corrupt"
        except (OSError, TypeError, ValueError):
            pass
        return "missing"

    @staticmethod
    def _legacy_sidecar(item: dict[str, Any]) -> dict[str, Any]:
        try:
            source_root = Path(str(item["output"])).expanduser().resolve()
            media = Path(str(item["file_path"])).expanduser().resolve()
            if not path_is_within(media, source_root):
                return {}
            sidecar = media.with_suffix(".json")
            if not sidecar.is_file() or sidecar.stat().st_size > 1024 * 1024:
                return {}
            data = json.loads(sidecar.read_text(encoding="utf-8"))
        except (OSError, ValueError, TypeError, json.JSONDecodeError):
            return {}
        return data if isinstance(data, dict) else {}

    def record_post_metadata(
        self,
        source_id: int,
        *,
        channel_title: str,
        post_text: str,
        published_at: str,
        status: str = "ready",
        error: str = "",
    ) -> None:
        with self.db:
            self.db.execute(
                """INSERT INTO tg_post_metadata(source_id,channel_title,post_text,
                published_at,fetched_at,status,error) VALUES(?,?,?,?,?,?,?)
                ON CONFLICT(source_id) DO UPDATE SET
                channel_title=excluded.channel_title,post_text=excluded.post_text,
                published_at=excluded.published_at,fetched_at=excluded.fetched_at,
                status=excluded.status,error=excluded.error""",
                (
                    int(source_id),
                    channel_title[:300],
                    post_text[:20000],
                    published_at,
                    utc_now(),
                    status,
                    error[:500],
                ),
            )
            self._set_dirty_sql()

    def post_media(self, source_id: int, media_id: str) -> dict[str, Any] | None:
        row = self.db.execute(
            "SELECT * FROM tg_post_media WHERE source_id=? AND media_id=?",
            (int(source_id), str(media_id)),
        ).fetchone()
        return dict(row) if row is not None else None

    def record_post_media(
        self,
        source_id: int,
        media_id: str,
        *,
        kind: str,
        file_path: str,
        size: int,
        status: str = "saved",
        error: str = "",
    ) -> None:
        with self.db:
            self.db.execute(
                """INSERT INTO tg_post_media(source_id,media_id,kind,file_path,size,status,error)
                VALUES(?,?,?,?,?,?,?) ON CONFLICT(source_id,media_id) DO UPDATE SET
                kind=excluded.kind,file_path=excluded.file_path,size=excluded.size,
                status=excluded.status,error=excluded.error""",
                (int(source_id), str(media_id), kind, file_path, int(size), status, error[:500]),
            )
            self._set_dirty_sql()

    def profile_ids(self) -> list[int]:
        return [int(row[0]) for row in self.db.execute("SELECT author_id FROM tg_profiles")]

    def profile_photo_due(self, author_id: int) -> bool:
        row = self.db.execute(
            "SELECT photos_retry_at FROM tg_profiles WHERE author_id=?", (int(author_id),)
        ).fetchone()
        return row is not None and float(row["photos_retry_at"] or 0) <= time.time()

    def profile_photo(self, author_id: int, photo_id: str) -> dict[str, Any] | None:
        row = self.db.execute(
            "SELECT * FROM tg_profile_photos WHERE author_id=? AND photo_id=?",
            (int(author_id), str(photo_id)),
        ).fetchone()
        return dict(row) if row is not None else None

    def record_profile_photo(
        self,
        author_id: int,
        photo_id: str,
        *,
        file_path: str,
        size: int,
        photo_date: str,
        status: str = "saved",
        error: str = "",
    ) -> None:
        with self.db:
            self.db.execute(
                """INSERT INTO tg_profile_photos(author_id,photo_id,file_path,size,
                photo_date,status,error) VALUES(?,?,?,?,?,?,?)
                ON CONFLICT(author_id,photo_id) DO UPDATE SET
                file_path=excluded.file_path,size=excluded.size,
                photo_date=excluded.photo_date,status=excluded.status,error=excluded.error""",
                (
                    int(author_id),
                    str(photo_id),
                    file_path,
                    int(size),
                    photo_date,
                    status,
                    error[:500],
                ),
            )
            self._set_dirty_sql()

    def finish_profile_photo_check(
        self,
        author_id: int,
        *,
        error: str = "",
        retry_after: int = 6 * 60 * 60,
    ) -> None:
        retry_at = time.time() + retry_after if error else time.time() + 24 * 60 * 60
        with self.db:
            self.db.execute(
                """UPDATE tg_profiles SET photos_checked_at=?,photo_error=?,photos_retry_at=?
                WHERE author_id=?""",
                (utc_now(), error[:500], retry_at, int(author_id)),
            )
            self._set_dirty_sql()

    def catalog_rows(self) -> dict[str, list[dict[str, Any]]]:
        queries = {
            "sources": "SELECT * FROM sources ORDER BY id",
            "downloads": "SELECT * FROM downloads ORDER BY source_id,message_id",
            "profiles": "SELECT * FROM tg_profiles ORDER BY author_id",
            "username_history": (
                "SELECT * FROM tg_username_history ORDER BY author_id,first_seen,username"
            ),
            "profile_sources": (
                "SELECT * FROM tg_profile_sources ORDER BY source_id,author_id"
            ),
            "profile_photos": (
                "SELECT * FROM tg_profile_photos ORDER BY author_id,photo_date DESC,photo_id"
            ),
            "post_metadata": "SELECT * FROM tg_post_metadata ORDER BY source_id",
            "post_media": "SELECT * FROM tg_post_media ORDER BY source_id,media_id",
        }
        return {
            name: [dict(row) for row in self.db.execute(query)]
            for name, query in queries.items()
        }
