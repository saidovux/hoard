"""Synchronous SQLite store; operations are short and run in daemon event loop."""
from __future__ import annotations

import json
import sqlite3
import stat
import time
from collections.abc import Collection
from pathlib import Path
from typing import Any

from .ai.psl import psl_failure_category
from .ai.storage import AIStorage
from .archive_paths import path_is_within
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
        self.db.execute("PRAGMA foreign_keys=ON")
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
              collection_mode TEXT NOT NULL DEFAULT 'full',
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
            "collection_mode": "TEXT NOT NULL DEFAULT 'full'",
        }
        for name, declaration in additions.items():
            if name not in columns:
                self.db.execute(f"ALTER TABLE sources ADD COLUMN {name} {declaration}")
        self.db.commit()
        self.telegram = TelegramStorage(self.db, migrate_legacy=migrate_legacy)
        self.ai = AIStorage(self.db, data_root=filename.parent)

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
        collection_mode: str = "full",
    ) -> int:
        source_id, _created = self.add_source_result(
            post,
            output,
            interval,
            title=title,
            collection_mode=collection_mode,
        )
        return source_id

    def add_source_result(
        self,
        post: Post,
        output: str,
        interval: int = 120,
        *,
        title: str = "",
        collection_mode: str = "full",
    ) -> tuple[int, bool]:
        """Add one source and report whether this call created it.

        ``add_source`` intentionally keeps its historical integer return value.
        Creation-only side effects stay behind the INSERT result so a duplicate
        cannot change archive roots or schedule a catalog publication.
        """
        directory = self._validate_source_settings(output, interval)
        mode = self._collection_mode(collection_mode)
        # No directory is created on add, so typos don't create unused directories.
        with self.db:
            cursor = self.db.execute("""INSERT INTO sources(
              username,post_id,url,output,interval,title,collection_mode)
              VALUES(?,?,?,?,?,?,?) ON CONFLICT(username,post_id)
              DO NOTHING""", (
                post.username,
                post.message_id,
                post.link,
                str(directory),
                interval,
                title.strip()[:200],
                mode,
            ))
        row = self.db.execute(
            "SELECT id FROM sources WHERE username=? AND post_id=?",
            (post.username, post.message_id),
        ).fetchone()
        assert row is not None
        created = cursor.rowcount == 1
        if created:
            self.telegram.ensure_archive_root(directory)
            self.telegram.set_catalog_dirty()
        return int(row["id"]), created

    @staticmethod
    def _collection_mode(value: object) -> str:
        mode = str(value or "").strip().lower()
        if mode not in {"full", "minimal"}:
            raise ValueError("Режим сбора профилей должен быть full или minimal")
        return mode

    @staticmethod
    def _validate_source_settings(output: str, interval: int) -> Path:
        if not 30 <= int(interval) <= 86400:
            raise ValueError("Интервал должен быть от 30 секунд до 24 часов")
        if not str(output).strip():
            raise ValueError("Укажи каталог для архива")
        directory = Path(output).expanduser().resolve()
        if directory.exists() and not directory.is_dir():
            raise ValueError("Путь архива должен указывать на каталог")
        return directory

    def update_source(
        self,
        source_id: int,
        *,
        title: str,
        output: str,
        interval: int,
        collection_mode: str,
    ) -> dict:
        """Update local source settings without changing identity or scheduling state."""
        directory = self._validate_source_settings(output, interval)
        mode = self._collection_mode(collection_mode)
        with self.db:
            cursor = self.db.execute(
                """UPDATE sources SET title=?,output=?,interval=?,collection_mode=?
                WHERE id=?""",
                (title.strip()[:200], str(directory), int(interval), mode, int(source_id)),
            )
            if cursor.rowcount == 0:
                raise ValueError("Источник не найден")
        self.telegram.ensure_archive_root(directory)
        self.telegram.set_catalog_dirty()
        source = self.source(int(source_id))
        assert source is not None
        return source

    def source_settings(self, source_id: int) -> dict | None:
        """Return one editable source record without archive/media details."""
        row = self.db.execute(
            """SELECT id,username,post_id,url,title,output,interval,enabled,
            collection_mode,last_id,next_at FROM sources WHERE id=?""",
            (int(source_id),),
        ).fetchone()
        return dict(row) if row is not None else None

    @staticmethod
    def _overview_text(value: object, max_bytes: int = 56) -> str:
        """Keep a UTF-8-safe presentation snippet inside the bounded IPC page."""
        text = str(value or "")
        encoded = text.encode("utf-8")
        if len(encoded) <= max_bytes:
            return text
        return encoded[: max_bytes - 3].decode("utf-8", errors="ignore") + "…"

    def telegram_sources_overview(
        self,
        *,
        limit: int = 50,
        offset: int = 0,
        query: str = "",
        enabled: bool | None = None,
        sort: str = "id_desc",
    ) -> dict:
        """Return one bounded source page plus batch-computed publication statistics."""
        page_limit = int(limit)
        page_offset = int(offset)
        if not 1 <= page_limit <= 100:
            raise ValueError("limit должен быть от 1 до 100")
        if page_offset < 0:
            raise ValueError("offset не может быть отрицательным")
        needle = str(query or "").strip()
        if len(needle) > 200:
            raise ValueError("Поисковый запрос не должен превышать 200 символов")
        order_by = {
            "id_desc": "s.id DESC",
            "id_asc": "s.id ASC",
            "last_checked_desc": "s.last_checked_at DESC,s.id DESC",
            "last_checked_asc": "s.last_checked_at ASC,s.id ASC",
            "downloads_desc": "download_count DESC,s.id DESC",
            "downloads_asc": "download_count ASC,s.id ASC",
        }.get(str(sort))
        if order_by is None:
            raise ValueError("Неизвестный порядок сортировки источников")

        clauses = ["1=1"]
        parameters: list[object] = []
        if enabled is not None:
            clauses.append("s.enabled=?")
            parameters.append(int(enabled))
        if needle:
            folded = needle.casefold()
            pattern = f"%{folded}%"
            username_pattern = f"%{folded.lstrip('@')}%"
            clauses.append(
                """(lower(s.username) LIKE ? OR lower(s.title) LIKE ? OR
                CAST(s.post_id AS TEXT) LIKE ? OR lower(s.url) LIKE ?)"""
            )
            parameters.extend((username_pattern, pattern, pattern, pattern))
        where = " AND ".join(clauses)
        total = int(
            self.db.execute(
                f"SELECT COUNT(*) FROM sources s WHERE {where}", parameters
            ).fetchone()[0]
        )
        source_fields = """s.id,s.username,s.post_id,s.url,s.title,s.interval,s.enabled,
            s.collection_mode,s.last_checked_at,s.last_result,s.last_error"""
        if str(sort).startswith("downloads_"):
            page_query = f"""SELECT {source_fields},COUNT(d.message_id) AS download_count
                FROM sources s LEFT JOIN downloads d ON d.source_id=s.id
                WHERE {where} GROUP BY s.id ORDER BY {order_by} LIMIT ? OFFSET ?"""
        else:
            page_query = f"""SELECT {source_fields},0 AS download_count FROM sources s
                WHERE {where} ORDER BY {order_by} LIMIT ? OFFSET ?"""
        page = [
            dict(row)
            for row in self.db.execute(
                page_query,
                (*parameters, page_limit, page_offset),
            )
        ]
        for item in page:
            for field in (
                "username",
                "url",
                "title",
                "last_checked_at",
                "last_result",
                "last_error",
            ):
                item[field] = self._overview_text(item.get(field))
        source_ids = [int(item["id"]) for item in page]
        if source_ids:
            placeholders = ",".join("?" for _ in source_ids)
            download_stats = {
                int(row["source_id"]): int(row["download_count"])
                for row in self.db.execute(
                    f"""SELECT source_id,COUNT(*) AS download_count FROM downloads
                    WHERE source_id IN ({placeholders}) GROUP BY source_id""",
                    source_ids,
                )
            }
            profile_stats = {
                int(row["source_id"]): dict(row)
                for row in self.db.execute(
                    f"""SELECT links.source_id,COUNT(DISTINCT links.author_id) AS profile_count,
                    COUNT(DISTINCT CASE WHEN photos.status='saved' THEN
                        links.author_id || ':' || photos.photo_id END) AS profile_photo_count
                    FROM tg_profile_sources links
                    LEFT JOIN tg_profile_photos photos ON photos.author_id=links.author_id
                    WHERE links.source_id IN ({placeholders}) GROUP BY links.source_id""",
                    source_ids,
                )
            }
            frame_stats = {
                int(row["source_id"]): dict(row)
                for row in self.db.execute(
                    f"""SELECT sets.source_id,
                    COUNT(DISTINCT CASE WHEN sets.status='saved'
                        THEN sets.message_id END) AS frame_set_count,
                    COUNT(CASE WHEN sets.status='saved'
                        THEN frames.position END) AS frame_count,
                    COUNT(DISTINCT CASE WHEN sets.status='failed'
                        THEN sets.message_id END) AS frame_failed_count,
                    COUNT(DISTINCT CASE WHEN sets.status='stale'
                        THEN sets.message_id END) AS frame_stale_count
                    FROM tg_video_frame_sets sets
                    LEFT JOIN tg_video_frames frames
                      ON frames.source_id=sets.source_id
                     AND frames.message_id=sets.message_id
                    WHERE sets.source_id IN ({placeholders}) GROUP BY sets.source_id""",
                    source_ids,
                )
            }
            psl_stats = {
                int(row["source_id"]): int(row["psl_count"])
                for row in self.db.execute(
                    f"""WITH source_links(source_id,analysis_id) AS (
                      SELECT batches.source_id,items.analysis_id
                      FROM ai_telegram_batches batches
                      JOIN ai_telegram_batch_items items ON items.batch_id=batches.id
                      JOIN ai_telegram_analyses batch_analyses
                        ON batch_analyses.id=items.analysis_id
                       AND batch_analyses.author_id=items.author_id
                      WHERE batches.source_id IN ({placeholders})
                        AND items.analysis_id IS NOT NULL
                      UNION
                      SELECT video_source_id,id FROM ai_telegram_analyses
                      WHERE video_source_id IN ({placeholders})
                      UNION
                      SELECT source_id,analysis_id FROM ai_telegram_analysis_photos
                      WHERE source_id IN ({placeholders})
                    )
                    SELECT links.source_id,COUNT(DISTINCT links.analysis_id) AS psl_count
                    FROM source_links links
                    JOIN ai_telegram_analyses analyses ON analyses.id=links.analysis_id
                    JOIN ai_attempts attempts ON attempts.id=analyses.attempt_id
                    WHERE attempts.state='completed' AND analyses.result_status='completed'
                    GROUP BY links.source_id""",
                    (*source_ids, *source_ids, *source_ids),
                )
            }
            for item in page:
                source_id = int(item["id"])
                profiles = profile_stats.get(source_id, {})
                frames = frame_stats.get(source_id, {})
                item.update(
                    download_count=int(download_stats.get(source_id, 0)),
                    profile_count=int(profiles.get("profile_count") or 0),
                    profile_photo_count=int(profiles.get("profile_photo_count") or 0),
                    frame_set_count=int(frames.get("frame_set_count") or 0),
                    frame_count=int(frames.get("frame_count") or 0),
                    frame_failed_count=int(frames.get("frame_failed_count") or 0),
                    frame_stale_count=int(frames.get("frame_stale_count") or 0),
                    psl_count=int(psl_stats.get(source_id, 0)),
                )
        return {
            "items": page,
            "total": total,
            "limit": page_limit,
            "offset": page_offset,
            "has_more": page_offset + len(page) < total,
        }

    @staticmethod
    def _publication_page(limit: int = 50, offset: int = 0) -> tuple[int, int]:
        page_limit = int(limit)
        page_offset = int(offset)
        if not 1 <= page_limit <= 100:
            raise ValueError("limit должен быть от 1 до 100")
        if page_offset < 0:
            raise ValueError("offset не может быть отрицательным")
        return page_limit, page_offset

    def _require_publication(self, source_id: int) -> int:
        identifier = int(source_id)
        if identifier <= 0 or self.db.execute(
            "SELECT 1 FROM sources WHERE id=?", (identifier,)
        ).fetchone() is None:
            raise ValueError("Публикация не найдена в локальном архиве")
        return identifier

    @staticmethod
    def _publication_links_cte() -> str:
        """Confirmed source attribution shared by publication PSL readers."""
        return """WITH source_links(source_id,analysis_id,attribution) AS (
          SELECT batches.source_id,items.analysis_id,'batch'
          FROM ai_telegram_batches batches
          JOIN ai_telegram_batch_items items ON items.batch_id=batches.id
          JOIN ai_telegram_analyses batch_analyses
            ON batch_analyses.id=items.analysis_id
           AND batch_analyses.author_id=items.author_id
          WHERE items.analysis_id IS NOT NULL
          UNION
          SELECT video_source_id,id,'video' FROM ai_telegram_analyses
          WHERE video_source_id IS NOT NULL
          UNION
          SELECT source_id,analysis_id,'photo' FROM ai_telegram_analysis_photos
          WHERE source_id IS NOT NULL
        ), scoped_links AS (
          SELECT analysis_id,GROUP_CONCAT(attribution) AS attribution
          FROM source_links WHERE source_id=? GROUP BY analysis_id
        )"""

    def telegram_publication(self, source_id: int) -> dict[str, Any]:
        """Return one source and its source-scoped, read-only aggregates."""
        identifier = self._require_publication(source_id)
        row = self.db.execute(
            self._publication_links_cte()
            + """ SELECT s.id,s.username,s.post_id,s.url,s.title,s.interval,s.enabled,
            s.collection_mode,s.last_checked_at,s.last_result,s.last_error,
            (SELECT COUNT(*) FROM downloads d WHERE d.source_id=s.id) AS download_count,
            (SELECT COUNT(DISTINCT author_id) FROM tg_profile_sources links
              WHERE links.source_id=s.id) AS profile_count,
            (SELECT COUNT(*) FROM tg_video_frames frames
              JOIN tg_video_frame_sets sets
                ON sets.source_id=frames.source_id AND sets.message_id=frames.message_id
              WHERE frames.source_id=s.id AND sets.status='saved') AS frame_count,
            (SELECT COUNT(*) FROM scoped_links links
              JOIN ai_telegram_analyses analyses ON analyses.id=links.analysis_id
              JOIN ai_attempts attempts ON attempts.id=analyses.attempt_id
              WHERE attempts.state='completed'
                AND analyses.result_status='completed') AS psl_count
            FROM sources s WHERE s.id=?""",
            (identifier, identifier),
        ).fetchone()
        assert row is not None
        return dict(row)

    def telegram_publication_downloads(
        self, source_id: int, *, limit: int = 50, offset: int = 0
    ) -> dict[str, Any]:
        """Return one stable source-scoped page without touching media files."""
        identifier = self._require_publication(source_id)
        page_limit, page_offset = self._publication_page(limit, offset)
        total = int(
            self.db.execute(
                "SELECT COUNT(*) FROM downloads WHERE source_id=?", (identifier,)
            ).fetchone()[0]
        )
        items = [
            dict(row)
            for row in self.db.execute(
                """SELECT d.message_id,d.author_id,d.author_username,d.author_name,
                d.date,d.media_url,d.size,d.state,
                COALESCE(sets.status,'missing') AS frame_status,
                COALESCE(sets.frame_count,0) AS frame_count,
                sets.error_code AS frame_error_code
                FROM downloads d LEFT JOIN tg_video_frame_sets sets
                  ON sets.source_id=d.source_id AND sets.message_id=d.message_id
                WHERE d.source_id=? ORDER BY d.message_id DESC LIMIT ? OFFSET ?""",
                (identifier, page_limit, page_offset),
            )
        ]
        return {
            "items": items,
            "total": total,
            "limit": page_limit,
            "offset": page_offset,
            "has_more": page_offset + len(items) < total,
        }

    def telegram_publication_download_details(
        self, source_id: int, message_id: int
    ) -> dict[str, Any]:
        """Inspect one selected MP4 without exposing its absolute path."""
        identifier = self._require_publication(source_id)
        row = self.db.execute(
            """SELECT d.message_id,d.author_id,d.author_username,d.author_name,
            d.date,d.media_url,d.file_path,d.size,d.state,
            COALESCE(sets.status,'missing') AS frame_status,
            COALESCE(sets.frame_count,0) AS frame_count,
            sets.error AS frame_error,sets.error_code AS frame_error_code
            FROM downloads d LEFT JOIN tg_video_frame_sets sets
              ON sets.source_id=d.source_id AND sets.message_id=d.message_id
            WHERE d.source_id=? AND d.message_id=?""",
            (identifier, int(message_id)),
        ).fetchone()
        if row is None:
            raise ValueError("Видеокружок не найден в выбранной публикации")
        item = dict(row)
        try:
            self._validated_media_path(str(item.pop("file_path")), int(item["size"] or 0))
        except ValueError as exc:
            item["available"] = False
            item["availability_error"] = str(exc)
        else:
            item["available"] = True
            item["availability_error"] = ""
        return item

    def telegram_publication_frames(
        self, source_id: int, *, limit: int = 50, offset: int = 0
    ) -> dict[str, Any]:
        """Return lightweight stored frame state; never hash or stat page rows."""
        identifier = self._require_publication(source_id)
        page_limit, page_offset = self._publication_page(limit, offset)
        total = int(
            self.db.execute(
                "SELECT COUNT(*) FROM downloads WHERE source_id=?", (identifier,)
            ).fetchone()[0]
        )
        items = [
            dict(row)
            for row in self.db.execute(
                """SELECT d.message_id,d.author_id,d.author_username,d.author_name,d.date,
                COALESCE(sets.status,'missing') AS frame_status,
                COALESCE(sets.frame_count,0) AS frame_count,sets.duration,
                sets.error,sets.error_code,sets.updated_at,
                COALESCE((SELECT jobs.state FROM jobs
                  WHERE jobs.unique_key=('video-frames:' || d.source_id || ':' || d.message_id)
                    AND jobs.state IN ('waiting','queued','running')
                  ORDER BY jobs.id DESC LIMIT 1),'') AS job_state
                FROM downloads d LEFT JOIN tg_video_frame_sets sets
                  ON sets.source_id=d.source_id AND sets.message_id=d.message_id
                WHERE d.source_id=? ORDER BY d.message_id DESC LIMIT ? OFFSET ?""",
                (identifier, page_limit, page_offset),
            )
        ]
        return {
            "items": items,
            "total": total,
            "limit": page_limit,
            "offset": page_offset,
            "has_more": page_offset + len(items) < total,
        }

    @staticmethod
    def _safe_psl_row(row: Any) -> dict[str, Any]:
        item = dict(row)
        item["failure_category"] = psl_failure_category(
            item.get("attempt_state"), item.pop("error_code", None), item.get("result_status")
        )
        successful = (
            item.get("attempt_state") == "completed"
            and item.get("result_status") == "completed"
            and item.get("presentation") == "female"
            and isinstance(item.get("estimated_age"), int)
            and int(item["estimated_age"]) >= 18
        )
        if not successful:
            item["estimated_age"] = None
            for key in ("raw_psl_min", "raw_psl_max", "boosted_psl"):
                item[key] = None
        return item

    def telegram_publication_psl(
        self, source_id: int, *, limit: int = 50, offset: int = 0
    ) -> dict[str, Any]:
        identifier = self._require_publication(source_id)
        page_limit, page_offset = self._publication_page(limit, offset)
        cte = self._publication_links_cte()
        total = int(
            self.db.execute(
                cte + " SELECT COUNT(*) FROM scoped_links", (identifier,)
            ).fetchone()[0]
        )
        rows = self.db.execute(
            cte
            + """ SELECT analyses.id AS analysis_id,analyses.author_id,
            profiles.current_username,profiles.display_name,analyses.created_at,
            analyses.analyzed_at,analyses.image_mode,attempts.state AS attempt_state,
            attempts.error_code,analyses.result_status,analyses.presentation,
            analyses.estimated_age,analyses.raw_psl_min,analyses.raw_psl_max,
            analyses.boosted_psl,links.attribution
            FROM scoped_links links
            JOIN ai_telegram_analyses analyses ON analyses.id=links.analysis_id
            JOIN ai_attempts attempts ON attempts.id=analyses.attempt_id
            LEFT JOIN tg_profiles profiles ON profiles.author_id=analyses.author_id
            ORDER BY COALESCE(analyses.analyzed_at,analyses.created_at) DESC,
              analyses.id DESC LIMIT ? OFFSET ?""",
            (identifier, page_limit, page_offset),
        )
        items = [self._safe_psl_row(row) for row in rows]
        return {
            "items": items,
            "total": total,
            "limit": page_limit,
            "offset": page_offset,
            "has_more": page_offset + len(items) < total,
        }

    def telegram_publication_psl_details(
        self, source_id: int, analysis_id: str
    ) -> dict[str, Any]:
        identifier = self._require_publication(source_id)
        analysis = str(analysis_id or "").strip()
        if not analysis:
            raise ValueError("Нужен ID анализа")
        row = self.db.execute(
            self._publication_links_cte()
            + """ SELECT analyses.id AS analysis_id,analyses.author_id,
            profiles.current_username,profiles.display_name,analyses.created_at,
            analyses.analyzed_at,analyses.image_mode,attempts.state AS attempt_state,
            attempts.error_code,analyses.result_status,analyses.presentation,
            analyses.estimated_age,analyses.raw_psl_min,analyses.raw_psl_max,
            analyses.boosted_psl,links.attribution
            FROM scoped_links links
            JOIN ai_telegram_analyses analyses ON analyses.id=links.analysis_id
            JOIN ai_attempts attempts ON attempts.id=analyses.attempt_id
            LEFT JOIN tg_profiles profiles ON profiles.author_id=analyses.author_id
            WHERE analyses.id=?""",
            (identifier, analysis),
        ).fetchone()
        if row is None:
            raise ValueError("Анализ не относится к выбранной публикации")
        result = self._safe_psl_row(row)
        result["image_types"] = [
            str(item["source_kind"])
            for item in self.db.execute(
                """SELECT DISTINCT source_kind FROM ai_telegram_analysis_photos
                WHERE analysis_id=? ORDER BY source_kind""",
                (analysis,),
            )
        ]
        return result

    def telegram_publication_profile(
        self, source_id: int, author_id: int
    ) -> dict[str, Any]:
        identifier = self._require_publication(source_id)
        author = int(author_id)
        linked = self.db.execute(
            "SELECT 1 FROM tg_profile_sources WHERE source_id=? AND author_id=?",
            (identifier, author),
        ).fetchone()
        if linked is None:
            raise ValueError("Профиль не относится к выбранной публикации")
        profile = self.telegram.get_profile_identity(author)
        profile["username_history"] = [
            dict(row)
            for row in self.db.execute(
                """SELECT username,first_seen,last_seen FROM tg_username_history
                WHERE author_id=? ORDER BY last_seen DESC,username LIMIT 100""",
                (author,),
            )
        ]
        profile["photo_total"] = int(
            self.db.execute(
                """SELECT COUNT(*) FROM tg_profile_photos
                WHERE author_id=? AND status='saved'""",
                (author,),
            ).fetchone()[0]
        )
        profile["photos"] = self.telegram.profile_photos(author, limit=100)
        profile["source_video_count"] = int(
            self.db.execute(
                "SELECT COUNT(*) FROM downloads WHERE source_id=? AND author_id=?",
                (identifier, author),
            ).fetchone()[0]
        )
        latest = self.db.execute(
            self._publication_links_cte()
            + """ SELECT analyses.id AS analysis_id,analyses.author_id,
            analyses.created_at,analyses.analyzed_at,analyses.image_mode,
            attempts.state AS attempt_state,attempts.error_code,analyses.result_status,
            analyses.presentation,analyses.estimated_age,analyses.raw_psl_min,
            analyses.raw_psl_max,analyses.boosted_psl,links.attribution
            FROM scoped_links links
            JOIN ai_telegram_analyses analyses ON analyses.id=links.analysis_id
            JOIN ai_attempts attempts ON attempts.id=analyses.attempt_id
            WHERE analyses.author_id=?
            ORDER BY COALESCE(analyses.analyzed_at,analyses.created_at) DESC,
              analyses.id DESC LIMIT 1""",
            (identifier, author),
        ).fetchone()
        profile["psl"] = self._safe_psl_row(latest) if latest is not None else None
        return profile

    def _validated_media_path(self, filename: str, expected_size: int) -> Path:
        if not filename or "\x00" in filename:
            raise ValueError("Локальный файл недоступен")
        try:
            path = Path(filename).expanduser().resolve(strict=True)
            file_stat = path.stat()
        except (OSError, RuntimeError, ValueError) as exc:
            raise ValueError("Локальный файл отсутствует") from exc
        roots = self.telegram.allowed_media_roots(self.telegram.archive_root())
        if not any(path_is_within(path, root) for root in roots):
            raise ValueError("Путь к локальному файлу запрещён")
        if not stat.S_ISREG(file_stat.st_mode):
            raise ValueError("Локальный объект не является обычным файлом")
        if int(expected_size or 0) > 0 and file_stat.st_size != int(expected_size):
            raise ValueError("Локальный файл повреждён или изменён")
        return path

    def resolve_publication_media(
        self,
        source_id: int,
        kind: str,
        *,
        message_id: int | None = None,
        author_id: int | None = None,
        photo_id: str | None = None,
        position: int | None = None,
    ) -> Path:
        """Resolve a stable publication object identifier to one verified local file."""
        identifier = self._require_publication(source_id)
        media_kind = str(kind or "")
        if media_kind == "video":
            row = self.db.execute(
                """SELECT file_path,size FROM downloads
                WHERE source_id=? AND message_id=?""",
                (identifier, int(message_id or 0)),
            ).fetchone()
        elif media_kind == "photo":
            author = int(author_id or 0)
            row = self.db.execute(
                """SELECT photos.file_path,photos.size FROM tg_profile_photos photos
                JOIN tg_profile_sources links ON links.author_id=photos.author_id
                WHERE links.source_id=? AND photos.author_id=? AND photos.photo_id=?
                  AND photos.status='saved'""",
                (identifier, author, str(photo_id or "")),
            ).fetchone()
        elif media_kind == "frame":
            message = int(message_id or 0)
            frame_position = int(position or 0)
            details = self.telegram.frame_details(identifier, message)
            if details is None or not details.get("valid"):
                raise ValueError("Набор кадров недоступен или повреждён")
            selected = next(
                (
                    frame
                    for frame in details.get("frames") or []
                    if int(frame.get("position") or 0) == frame_position
                ),
                None,
            )
            row = selected
        else:
            raise ValueError("Неизвестный тип локального медиа")
        if row is None:
            raise ValueError("Локальное медиа не найдено в выбранной публикации")
        return self._validated_media_path(str(row["file_path"]), int(row["size"] or 0))

    def resolve_global_media(
        self,
        kind: str,
        *,
        source_id: int | None = None,
        message_id: int | None = None,
        author_id: int | None = None,
        photo_id: str | None = None,
        position: int | None = None,
    ) -> Path:
        """Resolve one stable archived object while retaining legacy/unlinked access."""
        media_kind = str(kind or "")
        row: Any = None
        if media_kind == "video":
            row = self.db.execute(
                """SELECT file_path,size FROM downloads
                WHERE source_id=? AND message_id=?""",
                (int(source_id or 0), int(message_id or 0)),
            ).fetchone()
        elif media_kind == "photo":
            row = self.db.execute(
                """SELECT file_path,size FROM tg_profile_photos
                WHERE author_id=? AND photo_id=? AND status='saved'""",
                (int(author_id or 0), str(photo_id or "")),
            ).fetchone()
        elif media_kind == "frame":
            source = int(source_id or 0)
            message = int(message_id or 0)
            frame_position = int(position or 0)
            details = self.telegram.frame_details(source, message)
            if details is None or not details.get("valid"):
                raise ValueError("Набор кадров недоступен или повреждён")
            row = next(
                (
                    frame
                    for frame in details.get("frames") or []
                    if int(frame.get("position") or 0) == frame_position
                ),
                None,
            )
        else:
            raise ValueError("Неизвестный тип локального медиа")
        if row is None:
            raise ValueError("Локальное медиа не найдено в архиве")
        return self._validated_media_path(str(row["file_path"]), int(row["size"] or 0))

    def sources(self) -> list[dict]:
        return [
            dict(row)
            for row in self.db.execute(
                """SELECT sources.*, COUNT(downloads.message_id) AS download_count
                FROM sources LEFT JOIN downloads ON downloads.source_id=sources.id
                GROUP BY sources.id ORDER BY sources.id"""
            )
        ]

    def source(self, source_id: int) -> dict | None:
        """Return one source without aggregating every configured source."""
        row = self.db.execute(
            """SELECT sources.*,
            (SELECT COUNT(*) FROM downloads WHERE downloads.source_id=sources.id)
            AS download_count FROM sources WHERE sources.id=?""",
            (int(source_id),),
        ).fetchone()
        return dict(row) if row is not None else None

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
                file_stat = path.stat()
                available = stat.S_ISREG(file_stat.st_mode) and file_stat.st_size == row["size"]
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
            file_stat = file.stat()
            return (
                stat.S_ISREG(file_stat.st_mode)
                and file_stat.st_size == row["size"]
                and row["size"] > 0
            )
        except OSError:
            return False

    def checkpoint(
        self,
        source_id: int,
        message_id: int,
        video: dict | None = None,
        *,
        advance: bool = True,
    ) -> None:
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
                if payload.get("author_id") is not None:
                    self.telegram._observe_profile(
                        author_id=int(payload["author_id"]),
                        username=str(payload.get("author_username") or ""),
                        display_name=str(payload.get("author_name") or "")[:300],
                        observed_at=str(payload["observed_at"]),
                        source_id=source_id,
                    )
                video_path = Path(str(payload["file_path"]))
                try:
                    video_stat = video_path.stat()
                    video_size = int(video_stat.st_size)
                    video_mtime_ns = int(video_stat.st_mtime_ns)
                    video_exists = video_path.is_file() and video_size > 0
                except OSError:
                    video_size = int(payload.get("size") or 0)
                    video_mtime_ns = 0
                    video_exists = False
                if video_exists:
                    needs_frames = self.telegram.register_frame_set_sql(
                        source_id=int(payload["source_id"]),
                        message_id=int(payload["message_id"]),
                        author_id=(
                            int(payload["author_id"])
                            if payload.get("author_id") is not None
                            else None
                        ),
                        file_path=str(video_path.resolve(strict=False)),
                        size=video_size,
                        mtime_ns=video_mtime_ns,
                        mark_dirty=False,
                    )
                    if needs_frames:
                        self.telegram._enqueue_frame_job_sql(
                            int(payload["source_id"]),
                            int(payload["message_id"]),
                            authorized=False,
                        )
                self.telegram._set_dirty_sql()
            if advance:
                self.db.execute(
                    "UPDATE sources SET last_id=MAX(last_id,?) WHERE id=?",
                    (message_id, source_id),
                )

    def recent_downloads(self, count: int = 40) -> list[dict]:
        return [dict(r) for r in self.db.execute("""SELECT downloads.*,
            sources.username AS source_name, sources.post_id,
            frames.status AS frame_status, frames.frame_count AS frame_count,
            frames.error AS frame_error, frames.error_code AS frame_error_code
            FROM downloads LEFT JOIN sources ON downloads.source_id=sources.id
            LEFT JOIN tg_video_frame_sets frames
              ON frames.source_id=downloads.source_id
             AND frames.message_id=downloads.message_id
            ORDER BY downloads.rowid DESC LIMIT ?""", (count,))]

    def telegram_media_list(
        self,
        *,
        limit: int = 50,
        offset: int = 0,
        query: str = "",
        sort: str = "newest",
    ) -> dict[str, Any]:
        """Return a bounded global video-note page without filesystem work."""
        page_limit, page_offset = self._catalog_page(limit, offset)
        needle = str(query or "").strip().casefold()
        if len(needle) > 200:
            raise ValueError("Поиск медиа не должен превышать 200 символов")
        order_by = {
            "newest": "d.rowid DESC",
            "oldest": "d.rowid ASC",
            "date_desc": "d.date DESC,d.rowid DESC",
            "size_desc": "d.size DESC,d.rowid DESC",
        }.get(str(sort or "newest"))
        if order_by is None:
            raise ValueError("Неизвестный порядок сортировки медиа")
        clauses = ["1=1"]
        parameters: list[Any] = []
        if needle:
            pattern = f"%{needle.lstrip('@')}%"
            clauses.append(
                """(CAST(d.message_id AS TEXT) LIKE ? OR CAST(d.author_id AS TEXT) LIKE ? OR
                lower(d.author_username) LIKE ? OR lower(d.author_name) LIKE ? OR
                lower(COALESCE(s.username,'')) LIKE ? OR lower(COALESCE(s.title,'')) LIKE ? OR
                CAST(COALESCE(s.post_id,'') AS TEXT) LIKE ?)"""
            )
            parameters.extend([pattern] * 7)
        where = " AND ".join(clauses)
        total = int(
            self.db.execute(
                f"""SELECT COUNT(*) FROM downloads d
                LEFT JOIN sources s ON s.id=d.source_id WHERE {where}""",
                parameters,
            ).fetchone()[0]
        )
        items = [
            dict(row)
            for row in self.db.execute(
                f"""SELECT d.source_id,d.message_id,d.author_id,d.author_username,
                d.author_name,d.date,d.size,d.state,
                s.username AS source_username,s.title AS source_title,s.post_id,
                CASE WHEN s.id IS NULL THEN 0 ELSE 1 END AS source_available,
                COALESCE(frames.status,'missing') AS frame_status,
                COALESCE(frames.frame_count,0) AS frame_count,
                frames.error_code AS frame_error_code
                FROM downloads d LEFT JOIN sources s ON s.id=d.source_id
                LEFT JOIN tg_video_frame_sets frames
                  ON frames.source_id=d.source_id AND frames.message_id=d.message_id
                WHERE {where} ORDER BY {order_by} LIMIT ? OFFSET ?""",
                (*parameters, page_limit, page_offset),
            )
        ]
        return {
            "items": items,
            "total": total,
            "limit": page_limit,
            "offset": page_offset,
            "has_more": page_offset + len(items) < total,
        }

    def telegram_media_details(self, source_id: int, message_id: int) -> dict[str, Any]:
        """Inspect one globally selected video without requiring a live source relation."""
        row = self.db.execute(
            """SELECT d.source_id,d.message_id,d.author_id,d.author_username,d.author_name,
            d.date,d.media_url,d.file_path,d.size,d.state,
            s.username AS source_username,s.title AS source_title,s.post_id,
            CASE WHEN s.id IS NULL THEN 0 ELSE 1 END AS source_available,
            COALESCE(frames.status,'missing') AS frame_status,
            COALESCE(frames.frame_count,0) AS frame_count,
            frames.error AS frame_error,frames.error_code AS frame_error_code
            FROM downloads d LEFT JOIN sources s ON s.id=d.source_id
            LEFT JOIN tg_video_frame_sets frames
              ON frames.source_id=d.source_id AND frames.message_id=d.message_id
            WHERE d.source_id=? AND d.message_id=?""",
            (int(source_id), int(message_id)),
        ).fetchone()
        if row is None:
            raise ValueError("Видеокружок не найден в локальном архиве")
        item = dict(row)
        try:
            self._validated_media_path(str(item.pop("file_path")), int(item["size"] or 0))
        except ValueError as exc:
            item["available"] = False
            item["availability_error"] = str(exc)
        else:
            item["available"] = True
            item["availability_error"] = ""
        return item

    def recent_events(self, count: int = 30) -> list[dict]:
        return [dict(r) for r in self.db.execute(
            "SELECT created_at,kind,text FROM events ORDER BY id DESC LIMIT ?", (count,))]

    def stats(self) -> dict:
        source_count = self.db.execute("SELECT COUNT(*) FROM sources WHERE enabled=1").fetchone()[0]
        downloaded = self.db.execute("SELECT COUNT(*) FROM downloads").fetchone()[0]
        authors = self.db.execute("SELECT COUNT(*) FROM tg_profiles").fetchone()[0]
        profile_photos = self.db.execute(
            "SELECT COUNT(*) FROM tg_profile_photos WHERE status='saved'"
        ).fetchone()[0]
        return {
            "sources": source_count,
            "downloads": downloaded,
            "authors": authors,
            "profile_photos": profile_photos,
        }

    def telegram_statistics(self) -> dict:
        """Return bounded archive aggregates for Telegram Status."""
        sources = self.db.execute(
            """SELECT COUNT(*) AS total,
            COALESCE(SUM(enabled=1),0) AS enabled FROM sources"""
        ).fetchone()
        media = self.db.execute(
            """SELECT
              (SELECT COUNT(*) FROM downloads) AS video_notes,
              (SELECT COUNT(*) FROM tg_profiles) AS unique_profiles,
              (SELECT COUNT(*) FROM tg_profile_photos WHERE status='saved') AS profile_photos,
              COALESCE(SUM(status='saved' AND kind='image'),0) AS post_images,
              COALESCE(SUM(status='saved' AND kind='video'),0) AS post_videos,
              COALESCE(SUM(status='saved' AND kind='file'),0) AS post_files
            FROM tg_post_media"""
        ).fetchone()
        assert sources is not None and media is not None
        source_total = int(sources["total"] or 0)
        source_enabled = int(sources["enabled"] or 0)
        return {
            "sources": {
                "total": source_total,
                "enabled": source_enabled,
                "disabled": source_total - source_enabled,
            },
            "media": {
                "video_notes": int(media["video_notes"] or 0),
                "post_images": int(media["post_images"] or 0),
                "post_videos": int(media["post_videos"] or 0),
                "post_files": int(media["post_files"] or 0),
                "profile_photos": int(media["profile_photos"] or 0),
            },
            "profiles": {"unique": int(media["unique_profiles"] or 0)},
            "frames": self.telegram.archive_frame_statistics(),
            "psl": self.ai.telegram_statistics(),
        }

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

    def queued_jobs(
        self,
        limit: int = 3,
        *,
        kinds: Collection[str] | None = None,
        job_ids: Collection[int] | None = None,
    ) -> list[dict]:
        clauses = ["state='queued'"]
        parameters: list[Any] = []
        if kinds is not None:
            selected = tuple(kinds)
            if not selected:
                return []
            placeholders = ",".join("?" for _ in selected)
            clauses.append(f"kind IN ({placeholders})")
            parameters.extend(selected)
        if job_ids is not None:
            identifiers = tuple(dict.fromkeys(int(value) for value in job_ids))
            if not identifiers:
                return []
            placeholders = ",".join("?" for _ in identifiers)
            clauses.append(f"id IN ({placeholders})")
            parameters.extend(identifiers)
        query = f"SELECT * FROM jobs WHERE {' AND '.join(clauses)} ORDER BY id LIMIT ?"
        parameters.append(max(0, int(limit)))
        return [dict(row) for row in self.db.execute(query, parameters)]

    def recent_jobs(self, count: int = 80) -> list[dict]:
        return [dict(r) for r in self.db.execute(
            "SELECT id,kind,state,attempts,result,created_at,updated_at "
            "FROM jobs ORDER BY id DESC LIMIT ?",
            (count,))]

    @staticmethod
    def _catalog_page(limit: int = 50, offset: int = 0) -> tuple[int, int]:
        page_limit = int(limit)
        page_offset = int(offset)
        if not 1 <= page_limit <= 100:
            raise ValueError("limit должен быть от 1 до 100")
        if page_offset < 0:
            raise ValueError("offset не может быть отрицательным")
        return page_limit, page_offset

    @staticmethod
    def _job_summary(row: Any) -> dict[str, Any]:
        item = dict(row)
        item["result"] = Store._overview_text(item.get("result"), 300)
        item["retryable"] = (
            item.get("state") == "failed"
            and not str(item.get("kind") or "").startswith("ai.")
        )
        item["manual_permission_required"] = (
            item.get("kind") == "telegram.video_frames"
            and item.get("state") == "waiting"
        )
        return item

    def jobs_list(
        self,
        *,
        limit: int = 50,
        offset: int = 0,
        state: str = "",
        kind: str = "",
    ) -> dict[str, Any]:
        """Return one compact, parameterized page of the global durable queue."""
        page_limit, page_offset = self._catalog_page(limit, offset)
        selected_state = str(state or "").strip()
        if selected_state and selected_state not in {
            "queued", "running", "waiting", "done", "failed"
        }:
            raise ValueError("Неизвестное состояние задания")
        kind_query = str(kind or "").strip().casefold()
        if len(kind_query) > 100:
            raise ValueError("Фильтр типа задания не должен превышать 100 символов")
        clauses = ["1=1"]
        parameters: list[Any] = []
        if selected_state:
            clauses.append("state=?")
            parameters.append(selected_state)
        if kind_query:
            clauses.append("lower(kind) LIKE ?")
            parameters.append(f"%{kind_query}%")
        where = " AND ".join(clauses)
        total = int(
            self.db.execute(
                f"SELECT COUNT(*) FROM jobs WHERE {where}", parameters
            ).fetchone()[0]
        )
        items = [
            self._job_summary(row)
            for row in self.db.execute(
                f"""SELECT id,kind,state,attempts,result,created_at,updated_at
                FROM jobs WHERE {where} ORDER BY id DESC LIMIT ? OFFSET ?""",
                (*parameters, page_limit, page_offset),
            )
        ]
        return {
            "items": items,
            "total": total,
            "limit": page_limit,
            "offset": page_offset,
            "has_more": page_offset + len(items) < total,
        }

    def job_details(self, job_id: int) -> dict[str, Any]:
        """Return safe queue metadata without payloads, paths or credentials."""
        identifier = int(job_id)
        if identifier <= 0:
            raise ValueError("Нужен корректный ID задания")
        row = self.db.execute(
            """SELECT id,kind,state,attempts,result,created_at,updated_at
            FROM jobs WHERE id=?""",
            (identifier,),
        ).fetchone()
        if row is None:
            raise ValueError("Задание не найдено")
        return self._job_summary(row)

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

    def recover_interrupted_jobs(
        self,
        kinds: Collection[str] | None = None,
        *,
        retry: bool = True,
    ) -> None:
        state = "queued" if retry else "failed"
        result = (
            "Восстановлено после остановки"
            if retry
            else "Прервано остановкой; нужен явный повтор"
        )
        parameters: list = [state, result]
        clause = ""
        if kinds is not None:
            selected = tuple(kinds)
            if not selected:
                return
            placeholders = ",".join("?" for _ in selected)
            clause = f" AND kind IN ({placeholders})"
            parameters.extend(selected)
        with self.db:
            self.db.execute(
                f"UPDATE jobs SET state=?,result=? WHERE state='running'{clause}",
                parameters,
            )

    def retry_job(self, jid: int, *, target_state: str = "queued") -> int:
        """Requeue a failed job without violating the active unique key.

        Returns the id of the job that is now responsible for the logical work.
        If another queued/running job already owns the same ``unique_key``, that
        job is returned instead of creating a duplicate or raising
        ``IntegrityError`` from the partial unique index.
        """
        if target_state not in {"queued", "waiting"}:
            raise ValueError("Invalid retry target state")
        with self.db:
            row = self.db.execute(
                "SELECT unique_key,state FROM jobs WHERE id=?",
                (int(jid),),
            ).fetchone()
            if row is None:
                raise ValueError("Задание не найдено")
            current_state = str(row["state"] or "")
            active_states = (
                {"queued", "running", "waiting"}
                if target_state == "waiting"
                else {"queued", "running"}
            )
            if current_state in active_states:
                return int(jid)
            if current_state != "failed":
                raise ValueError("Повторить можно только failed-задание")
            unique_key = row["unique_key"]
            if unique_key is not None:
                states = ("queued", "running", "waiting") if target_state == "waiting" else (
                    "queued",
                    "running",
                )
                placeholders = ",".join("?" for _ in states)
                active = self.db.execute(
                    f"""SELECT id FROM jobs WHERE unique_key=?
                    AND state IN ({placeholders}) LIMIT 1""",
                    (str(unique_key), *states),
                ).fetchone()
                if active is not None:
                    return int(active["id"])
            try:
                cur = self.db.execute(
                    """UPDATE jobs SET state=?,result=?,updated_at=datetime('now')
                    WHERE id=? AND state='failed'""",
                    (
                        target_state,
                        "Ожидает явного запуска обработки кадров"
                        if target_state == "waiting"
                        else "",
                        int(jid),
                    ),
                )
            except sqlite3.IntegrityError:
                # A concurrent enqueue created the active owner between the
                # check and the update.  Defer to it rather than surfacing the
                # index violation as an IPC error.
                active = self.db.execute(
                    """SELECT id FROM jobs WHERE unique_key=?
                    AND state IN ('queued','running') LIMIT 1""",
                    (str(unique_key),),
                ).fetchone()
                if active is not None:
                    return int(active["id"])
                raise
            if cur.rowcount != 1:
                raise ValueError("Нужна существующая задача со статусом failed")
            return int(jid)
