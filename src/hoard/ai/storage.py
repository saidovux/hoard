"""Durable AI data stored in Hoard's existing daemon-owned SQLite database."""
from __future__ import annotations

import hashlib
import json
import os
import re
import uuid
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from .models import AIImage, AIUsage
from .pricing import CostEstimate, decimal_text
from .psl import DEFAULT_PSL_INSTRUCTIONS, PSL_JSON_SCHEMA

AI_SCHEMA_VERSION = 2
MAX_TEXT_BYTES = 32 * 1024
MAX_NAME_BYTES = 300
MAX_PAGE = 100
DEFAULT_CONTEXT_CHARS = 60_000
MAX_CONTEXT_CHARS = 500_000
MAX_ATTACHMENT_BYTES = 12 * 1024 * 1024
MAX_MESSAGE_ATTACHMENTS = 4
MAX_MESSAGE_ATTACHMENT_BYTES = 24 * 1024 * 1024
_SCOPE = re.compile(r"^[a-z][a-z0-9_.-]{0,39}$")
_STATES = {"queued", "running", "completed", "failed", "outcome_unknown"}


def now_utc() -> str:
    return datetime.now(UTC).isoformat()


def new_id() -> str:
    return uuid.uuid4().hex


def validate_scope(value: Any, *, memory: bool = False) -> str:
    scope = str(value or "").strip().lower()
    if not _SCOPE.fullmatch(scope):
        raise ValueError("Область должна быть коротким латинским идентификатором")
    if memory and scope not in {"general", "telegram", "chat"}:
        raise ValueError("Память поддерживает области general, telegram и chat")
    return scope


def bounded_text(value: Any, *, field: str, allow_empty: bool = False) -> str:
    text = str(value or "").strip()
    if not text and not allow_empty:
        raise ValueError(f"{field} не может быть пустым")
    if len(text.encode("utf-8")) > MAX_TEXT_BYTES:
        raise ValueError(f"{field} превышает лимит {MAX_TEXT_BYTES} байт")
    return text


def bounded_name(value: Any, *, field: str) -> str:
    text = str(value or "").strip()
    if not text:
        raise ValueError(f"{field} не может быть пустым")
    if len(text.encode("utf-8")) > MAX_NAME_BYTES:
        raise ValueError(f"{field} слишком длинное")
    return text


def paging(limit: Any = 20, offset: Any = 0) -> tuple[int, int]:
    try:
        page_limit = int(limit)
        page_offset = int(offset)
    except (TypeError, ValueError) as exc:
        raise ValueError("limit и offset должны быть целыми числами") from exc
    if not 1 <= page_limit <= MAX_PAGE or page_offset < 0:
        raise ValueError(f"limit должен быть 1–{MAX_PAGE}, offset — неотрицательным")
    return page_limit, page_offset


def timestamp_text(value: Any, *, field: str) -> str:
    text = str(value or "").strip()
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError as exc:
        raise ValueError(f"{field} должно быть датой ISO 8601") from exc
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=UTC)
    return parsed.astimezone(UTC).isoformat()


class AIStorage:
    """AI repositories sharing one short-transaction SQLite connection."""

    def __init__(self, db, *, data_root: Path):
        self.db = db
        self.data_root = data_root
        self.attachments_root = data_root / "ai_attachments"
        self._migrate()

    def _migrate(self) -> None:
        self.db.execute("PRAGMA foreign_keys=ON")
        self.db.executescript(
            """
            CREATE TABLE IF NOT EXISTS ai_prompts (
                id TEXT PRIMARY KEY,
                name TEXT NOT NULL,
                scope TEXT NOT NULL,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                deleted_at TEXT
            );
            CREATE TABLE IF NOT EXISTS ai_prompt_versions (
                id TEXT PRIMARY KEY,
                prompt_id TEXT NOT NULL REFERENCES ai_prompts(id),
                version INTEGER NOT NULL,
                instructions TEXT NOT NULL,
                provider TEXT,
                model TEXT,
                response_format TEXT NOT NULL,
                json_schema TEXT,
                max_tokens INTEGER,
                temperature REAL,
                context_limit INTEGER NOT NULL,
                created_at TEXT NOT NULL,
                UNIQUE(prompt_id, version)
            );
            CREATE TABLE IF NOT EXISTS ai_active_prompts (
                scope TEXT PRIMARY KEY,
                prompt_id TEXT NOT NULL REFERENCES ai_prompts(id),
                updated_at TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS ai_chats (
                id TEXT PRIMARY KEY,
                title TEXT NOT NULL,
                scope TEXT NOT NULL,
                provider TEXT NOT NULL,
                model TEXT NOT NULL,
                prompt_id TEXT REFERENCES ai_prompts(id),
                archived INTEGER NOT NULL DEFAULT 0,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                deleted_at TEXT
            );
            CREATE TABLE IF NOT EXISTS ai_messages (
                id TEXT PRIMARY KEY,
                chat_id TEXT NOT NULL REFERENCES ai_chats(id),
                sequence INTEGER NOT NULL,
                role TEXT NOT NULL,
                content TEXT NOT NULL,
                state TEXT NOT NULL,
                attempt_id TEXT,
                created_at TEXT NOT NULL,
                UNIQUE(chat_id, sequence),
                UNIQUE(attempt_id, role)
            );
            CREATE TABLE IF NOT EXISTS ai_memory (
                id TEXT PRIMARY KEY,
                scope TEXT NOT NULL,
                chat_id TEXT REFERENCES ai_chats(id),
                title TEXT NOT NULL,
                enabled INTEGER NOT NULL DEFAULT 1,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                deleted_at TEXT
            );
            CREATE TABLE IF NOT EXISTS ai_memory_versions (
                id TEXT PRIMARY KEY,
                memory_id TEXT NOT NULL REFERENCES ai_memory(id),
                version INTEGER NOT NULL,
                content TEXT NOT NULL,
                created_at TEXT NOT NULL,
                UNIQUE(memory_id, version)
            );
            CREATE TABLE IF NOT EXISTS ai_attachments (
                id TEXT PRIMARY KEY,
                media_type TEXT NOT NULL,
                size INTEGER NOT NULL,
                sha256 TEXT NOT NULL,
                file_path TEXT NOT NULL UNIQUE,
                created_at TEXT NOT NULL,
                deleted_at TEXT
            );
            CREATE TABLE IF NOT EXISTS ai_message_attachments (
                message_id TEXT NOT NULL REFERENCES ai_messages(id) ON DELETE CASCADE,
                attachment_id TEXT NOT NULL REFERENCES ai_attachments(id),
                position INTEGER NOT NULL,
                PRIMARY KEY(message_id, attachment_id),
                UNIQUE(message_id, position)
            );
            CREATE TABLE IF NOT EXISTS ai_prices (
                id TEXT PRIMARY KEY,
                provider TEXT NOT NULL,
                model TEXT NOT NULL,
                effective_from TEXT NOT NULL,
                effective_to TEXT,
                currency TEXT NOT NULL,
                input_cache_hit_per_million TEXT,
                input_cache_miss_per_million TEXT,
                output_per_million TEXT,
                source TEXT NOT NULL,
                created_at TEXT NOT NULL,
                deleted_at TEXT
            );
            CREATE TABLE IF NOT EXISTS ai_attempts (
                id TEXT PRIMARY KEY,
                job_id INTEGER UNIQUE,
                operation_key TEXT UNIQUE,
                chat_id TEXT,
                user_message_id TEXT,
                scope TEXT NOT NULL,
                entity_kind TEXT,
                entity_id TEXT,
                provider TEXT NOT NULL,
                requested_model TEXT,
                actual_model TEXT,
                provider_request_id TEXT,
                prompt_version_id TEXT,
                request_config TEXT NOT NULL DEFAULT '{}',
                context_metadata TEXT NOT NULL DEFAULT '{}',
                state TEXT NOT NULL,
                error_code TEXT,
                prompt_tokens INTEGER,
                completion_tokens INTEGER,
                total_tokens INTEGER,
                prompt_cache_hit_tokens INTEGER,
                prompt_cache_miss_tokens INTEGER,
                reasoning_tokens INTEGER,
                estimated_cost TEXT,
                cost_currency TEXT,
                cost_status TEXT NOT NULL DEFAULT 'unknown',
                price_id TEXT,
                price_snapshot TEXT,
                created_at TEXT NOT NULL,
                sent_at TEXT,
                completed_at TEXT
            );
            CREATE TABLE IF NOT EXISTS ai_attempt_messages (
                attempt_id TEXT NOT NULL REFERENCES ai_attempts(id) ON DELETE CASCADE,
                message_id TEXT NOT NULL REFERENCES ai_messages(id),
                position INTEGER NOT NULL,
                PRIMARY KEY(attempt_id, message_id),
                UNIQUE(attempt_id, position)
            );
            CREATE TABLE IF NOT EXISTS ai_attempt_memory (
                attempt_id TEXT NOT NULL REFERENCES ai_attempts(id) ON DELETE CASCADE,
                memory_id TEXT NOT NULL,
                memory_version_id TEXT NOT NULL REFERENCES ai_memory_versions(id),
                position INTEGER NOT NULL,
                PRIMARY KEY(attempt_id, memory_id),
                UNIQUE(attempt_id, position)
            );
            CREATE TABLE IF NOT EXISTS ai_telegram_analyses (
                id TEXT PRIMARY KEY,
                operation_key TEXT NOT NULL UNIQUE,
                root_analysis_id TEXT REFERENCES ai_telegram_analyses(id),
                author_id INTEGER NOT NULL,
                attempt_id TEXT NOT NULL UNIQUE REFERENCES ai_attempts(id),
                prompt_version_id TEXT NOT NULL REFERENCES ai_prompt_versions(id),
                schema_version INTEGER NOT NULL,
                adult_confirmed INTEGER NOT NULL CHECK(adult_confirmed=1),
                result_status TEXT CHECK(result_status IS NULL OR result_status IN
                    ('completed','insufficient_data','ineligible','invalid_result')),
                presentation TEXT CHECK(presentation IS NULL OR presentation IN
                    ('female','male','unknown')),
                estimated_age INTEGER,
                raw_psl_min REAL,
                raw_psl_max REAL,
                boosted_psl REAL,
                actual_model TEXT,
                analyzed_at TEXT,
                created_at TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS ai_telegram_analysis_photos (
                analysis_id TEXT NOT NULL REFERENCES ai_telegram_analyses(id) ON DELETE CASCADE,
                photo_id TEXT NOT NULL,
                attachment_id TEXT NOT NULL REFERENCES ai_attachments(id),
                position INTEGER NOT NULL,
                source_path TEXT NOT NULL,
                source_sha256 TEXT NOT NULL,
                size INTEGER NOT NULL,
                PRIMARY KEY(analysis_id, photo_id),
                UNIQUE(analysis_id, position)
            );
            CREATE INDEX IF NOT EXISTS ai_prompts_scope_idx
                ON ai_prompts(scope, updated_at DESC);
            CREATE INDEX IF NOT EXISTS ai_prompt_versions_prompt_idx
                ON ai_prompt_versions(prompt_id, version DESC);
            CREATE INDEX IF NOT EXISTS ai_chats_scope_idx
                ON ai_chats(scope, updated_at DESC);
            CREATE INDEX IF NOT EXISTS ai_messages_chat_idx
                ON ai_messages(chat_id, sequence);
            CREATE INDEX IF NOT EXISTS ai_memory_scope_idx
                ON ai_memory(scope, chat_id, updated_at DESC);
            CREATE INDEX IF NOT EXISTS ai_prices_lookup_idx
                ON ai_prices(provider, model, effective_from DESC);
            CREATE INDEX IF NOT EXISTS ai_attempts_created_idx
                ON ai_attempts(created_at DESC);
            CREATE INDEX IF NOT EXISTS ai_attempts_filters_idx
                ON ai_attempts(provider, actual_model, scope, chat_id, created_at);
            CREATE INDEX IF NOT EXISTS ai_telegram_analyses_author_idx
                ON ai_telegram_analyses(author_id, created_at DESC);
            CREATE UNIQUE INDEX IF NOT EXISTS ai_attempts_chat_active_idx
                ON ai_attempts(chat_id)
                WHERE chat_id IS NOT NULL AND state IN ('queued','running');
            """
        )
        chat_columns = {
            str(row["name"]) for row in self.db.execute("PRAGMA table_info(ai_chats)")
        }
        if "internal" not in chat_columns:
            self.db.execute(
                "ALTER TABLE ai_chats ADD COLUMN internal INTEGER NOT NULL DEFAULT 0"
            )
        self.db.execute(
            """INSERT INTO schema_versions(module,version) VALUES('ai',?)
            ON CONFLICT(module) DO UPDATE SET
            version=MAX(schema_versions.version,excluded.version),
            updated_at=datetime('now')""",
            (AI_SCHEMA_VERSION,),
        )
        self.db.commit()
        self.ensure_psl_prompt()

    @staticmethod
    def _json(value: Any) -> str:
        return json.dumps(value, ensure_ascii=False, separators=(",", ":"), sort_keys=True)

    @staticmethod
    def _decode(value: str | None) -> Any:
        return json.loads(value) if value else None

    def _page(self, query: str, parameters: tuple, *, limit: int, offset: int) -> dict:
        rows = [dict(row) for row in self.db.execute(query, (*parameters, limit + 1, offset))]
        return {
            "items": rows[:limit],
            "limit": limit,
            "offset": offset,
            "has_more": len(rows) > limit,
        }

    def _latest_prompt_version(self, prompt_id: str) -> dict:
        row = self.db.execute(
            "SELECT * FROM ai_prompt_versions WHERE prompt_id=? ORDER BY version DESC LIMIT 1",
            (prompt_id,),
        ).fetchone()
        if row is None:
            raise ValueError("Версия промпта не найдена")
        result = dict(row)
        result["json_schema"] = self._decode(result["json_schema"])
        return result

    def get_prompt_version(self, version_id: str) -> dict:
        row = self.db.execute(
            """SELECT v.*,p.name,p.scope FROM ai_prompt_versions v
            JOIN ai_prompts p ON p.id=v.prompt_id
            WHERE v.id=? AND p.deleted_at IS NULL""",
            (str(version_id),),
        ).fetchone()
        if row is None:
            raise ValueError("Версия промпта не найдена")
        result = dict(row)
        result["json_schema"] = self._decode(result["json_schema"])
        return result

    def ensure_psl_prompt(self) -> dict:
        """Install one editable Telegram prompt while retaining every historical version."""
        row = self.db.execute(
            """SELECT id FROM ai_prompts WHERE scope='telegram' AND name='Telegram PSL'
            AND deleted_at IS NULL ORDER BY created_at LIMIT 1"""
        ).fetchone()
        if row is None:
            prompt = self.create_prompt(
                {
                    "name": "Telegram PSL",
                    "scope": "telegram",
                    "instructions": DEFAULT_PSL_INSTRUCTIONS,
                    "provider": "deepseek",
                    "model": "deepseek-chat",
                    "response_format": "json",
                    "json_schema": PSL_JSON_SCHEMA,
                    "temperature": 0,
                    "context_limit": DEFAULT_CONTEXT_CHARS,
                }
            )
        else:
            prompt = self.get_prompt(str(row["id"]))
        return prompt

    def create_prompt(self, values: dict[str, Any]) -> dict:
        prompt_id = new_id()
        timestamp = now_utc()
        scope = validate_scope(values.get("scope", "general"))
        name = bounded_name(values.get("name"), field="Название промпта")
        with self.db:
            self.db.execute(
                "INSERT INTO ai_prompts(id,name,scope,created_at,updated_at) VALUES(?,?,?,?,?)",
                (prompt_id, name, scope, timestamp, timestamp),
            )
            self._insert_prompt_version(prompt_id, 1, values, timestamp)
        return self.get_prompt(prompt_id)

    def _insert_prompt_version(
        self, prompt_id: str, version: int, values: dict[str, Any], timestamp: str
    ) -> str:
        instructions = bounded_text(values.get("instructions"), field="Инструкции")
        response_format = str(values.get("response_format", "text"))
        if response_format not in {"text", "json"}:
            raise ValueError("response_format должен быть text или json")
        schema = values.get("json_schema")
        if schema is not None and not isinstance(schema, dict):
            raise ValueError("json_schema должна быть объектом")
        if schema is not None and response_format != "json":
            raise ValueError("JSON Schema доступна только для JSON-режима")
        max_tokens = values.get("max_tokens")
        if max_tokens is not None:
            max_tokens = int(max_tokens)
            if not 1 <= max_tokens <= 393_216:
                raise ValueError("max_tokens должен быть от 1 до 393216")
        temperature = values.get("temperature")
        if temperature is not None:
            temperature = float(temperature)
            if not 0 <= temperature <= 2:
                raise ValueError("temperature должна быть от 0 до 2")
        context_limit = int(values.get("context_limit", DEFAULT_CONTEXT_CHARS))
        if not 1_000 <= context_limit <= MAX_CONTEXT_CHARS:
            raise ValueError(f"context_limit должен быть 1000–{MAX_CONTEXT_CHARS} символов")
        version_id = new_id()
        self.db.execute(
            """INSERT INTO ai_prompt_versions(id,prompt_id,version,instructions,provider,model,
            response_format,json_schema,max_tokens,temperature,context_limit,created_at)
            VALUES(?,?,?,?,?,?,?,?,?,?,?,?)""",
            (
                version_id,
                prompt_id,
                version,
                instructions,
                str(values.get("provider") or "").strip() or None,
                str(values.get("model") or "").strip() or None,
                response_format,
                self._json(schema) if schema is not None else None,
                max_tokens,
                temperature,
                context_limit,
                timestamp,
            ),
        )
        return version_id

    def get_prompt(self, prompt_id: str) -> dict:
        row = self.db.execute(
            "SELECT * FROM ai_prompts WHERE id=? AND deleted_at IS NULL", (prompt_id,)
        ).fetchone()
        if row is None:
            raise ValueError("Промпт не найден")
        result = dict(row)
        result["current_version"] = self._latest_prompt_version(prompt_id)
        active = self.db.execute(
            "SELECT 1 FROM ai_active_prompts WHERE scope=? AND prompt_id=?",
            (result["scope"], prompt_id),
        ).fetchone()
        result["active"] = active is not None
        return result

    def list_prompts(self, *, scope: str | None, limit: Any, offset: Any) -> dict:
        page_limit, page_offset = paging(limit, offset)
        parameters: tuple = ()
        where = "p.deleted_at IS NULL"
        if scope is not None:
            where += " AND p.scope=?"
            parameters = (validate_scope(scope),)
        return self._page(
            f"""SELECT p.id,p.name,p.scope,p.created_at,p.updated_at,
            v.id AS version_id,v.version,v.provider,v.model,v.response_format,
            CASE WHEN a.prompt_id IS NULL THEN 0 ELSE 1 END AS active
            FROM ai_prompts p
            JOIN ai_prompt_versions v ON v.id=(SELECT id FROM ai_prompt_versions
                WHERE prompt_id=p.id ORDER BY version DESC LIMIT 1)
            LEFT JOIN ai_active_prompts a ON a.scope=p.scope AND a.prompt_id=p.id
            WHERE {where} ORDER BY p.updated_at DESC,p.id LIMIT ? OFFSET ?""",
            parameters,
            limit=page_limit,
            offset=page_offset,
        )

    def update_prompt(self, prompt_id: str, changes: dict[str, Any]) -> dict:
        current = self.get_prompt(prompt_id)
        version = dict(current["current_version"])
        version.update({key: value for key, value in changes.items() if value is not None})
        timestamp = now_utc()
        name = bounded_name(changes.get("name", current["name"]), field="Название промпта")
        with self.db:
            self.db.execute(
                "UPDATE ai_prompts SET name=?,updated_at=? WHERE id=?",
                (name, timestamp, prompt_id),
            )
            self._insert_prompt_version(
                prompt_id, int(current["current_version"]["version"]) + 1, version, timestamp
            )
        return self.get_prompt(prompt_id)

    def delete_prompt(self, prompt_id: str) -> None:
        timestamp = now_utc()
        with self.db:
            cursor = self.db.execute(
                "UPDATE ai_prompts SET deleted_at=?,updated_at=? WHERE id=? AND deleted_at IS NULL",
                (timestamp, timestamp, prompt_id),
            )
            if cursor.rowcount != 1:
                raise ValueError("Промпт не найден")
            self.db.execute("DELETE FROM ai_active_prompts WHERE prompt_id=?", (prompt_id,))

    def prompt_versions(self, prompt_id: str, *, limit: Any, offset: Any) -> dict:
        self.get_prompt(prompt_id)
        page_limit, page_offset = paging(limit, offset)
        result = self._page(
            """SELECT * FROM ai_prompt_versions WHERE prompt_id=?
            ORDER BY version DESC LIMIT ? OFFSET ?""",
            (prompt_id,),
            limit=page_limit,
            offset=page_offset,
        )
        for item in result["items"]:
            item["json_schema"] = self._decode(item["json_schema"])
        return result

    def activate_prompt(self, scope: str, prompt_id: str) -> dict:
        prompt = self.get_prompt(prompt_id)
        selected = validate_scope(scope)
        if prompt["scope"] != selected:
            raise ValueError("Область промпта не совпадает")
        with self.db:
            self.db.execute(
                """INSERT INTO ai_active_prompts(scope,prompt_id,updated_at) VALUES(?,?,?)
                ON CONFLICT(scope) DO UPDATE SET prompt_id=excluded.prompt_id,
                updated_at=excluded.updated_at""",
                (selected, prompt_id, now_utc()),
            )
        return self.get_prompt(prompt_id)

    def create_chat(self, values: dict[str, Any]) -> dict:
        chat_id = new_id()
        timestamp = now_utc()
        scope = validate_scope(values.get("scope", "general"))
        prompt_id = str(values.get("prompt_id") or "") or None
        if prompt_id is None:
            active = self.db.execute(
                "SELECT prompt_id FROM ai_active_prompts WHERE scope=?", (scope,)
            ).fetchone()
            prompt_id = str(active["prompt_id"]) if active else None
        if prompt_id is not None and self.get_prompt(prompt_id)["scope"] != scope:
            raise ValueError("Промпт и чат должны иметь одну область")
        with self.db:
            self.db.execute(
                """INSERT INTO ai_chats(
                id,title,scope,provider,model,prompt_id,created_at,updated_at)
                VALUES(?,?,?,?,?,?,?,?)""",
                (
                    chat_id,
                    bounded_name(values.get("title", "Новый диалог"), field="Название диалога"),
                    scope,
                    bounded_name(values.get("provider", "deepseek"), field="Провайдер"),
                    bounded_name(values.get("model", "deepseek-flash"), field="Модель"),
                    prompt_id,
                    timestamp,
                    timestamp,
                ),
            )
        return self.get_chat(chat_id)

    def get_chat(self, chat_id: str) -> dict:
        row = self.db.execute(
            """SELECT c.*,(SELECT COUNT(*) FROM ai_messages m WHERE m.chat_id=c.id) AS message_count
            FROM ai_chats c WHERE c.id=? AND c.deleted_at IS NULL""",
            (chat_id,),
        ).fetchone()
        if row is None:
            raise ValueError("Диалог не найден")
        return dict(row)

    def list_chats(
        self, *, scope: str | None, archived: bool | None, limit: Any, offset: Any
    ) -> dict:
        page_limit, page_offset = paging(limit, offset)
        clauses = ["c.deleted_at IS NULL", "c.internal=0"]
        parameters: list[Any] = []
        if scope is not None:
            clauses.append("c.scope=?")
            parameters.append(validate_scope(scope))
        if archived is not None:
            clauses.append("c.archived=?")
            parameters.append(int(archived))
        return self._page(
            f"""SELECT c.*,(SELECT COUNT(*) FROM ai_messages m WHERE m.chat_id=c.id)
            AS message_count FROM ai_chats c WHERE {' AND '.join(clauses)}
            ORDER BY c.updated_at DESC,c.id LIMIT ? OFFSET ?""",
            tuple(parameters),
            limit=page_limit,
            offset=page_offset,
        )

    def update_chat(self, chat_id: str, changes: dict[str, Any]) -> dict:
        current = self.get_chat(chat_id)
        prompt_id = changes.get("prompt_id", current["prompt_id"])
        if prompt_id == "":
            prompt_id = None
        if prompt_id is not None and self.get_prompt(str(prompt_id))["scope"] != current["scope"]:
            raise ValueError("Промпт и чат должны иметь одну область")
        with self.db:
            self.db.execute(
                """UPDATE ai_chats SET title=?,provider=?,model=?,prompt_id=?,archived=?,
                updated_at=? WHERE id=?""",
                (
                    bounded_name(changes.get("title", current["title"]), field="Название диалога"),
                    bounded_name(changes.get("provider", current["provider"]), field="Провайдер"),
                    bounded_name(changes.get("model", current["model"]), field="Модель"),
                    prompt_id,
                    int(bool(changes.get("archived", current["archived"]))),
                    now_utc(),
                    chat_id,
                ),
            )
        return self.get_chat(chat_id)

    def delete_chat(self, chat_id: str) -> None:
        timestamp = now_utc()
        with self.db:
            cursor = self.db.execute(
                """UPDATE ai_chats SET deleted_at=?,archived=1,updated_at=?
                WHERE id=? AND deleted_at IS NULL""",
                (timestamp, timestamp, chat_id),
            )
            if cursor.rowcount != 1:
                raise ValueError("Диалог не найден")
            self.db.execute(
                """UPDATE ai_memory SET deleted_at=?,updated_at=?
                WHERE chat_id=? AND deleted_at IS NULL""",
                (timestamp, timestamp, chat_id),
            )

    def chat_history(self, chat_id: str, *, limit: Any, offset: Any) -> dict:
        self.get_chat(chat_id)
        page_limit, page_offset = paging(limit, offset)
        rows = [
            dict(row)
            for row in self.db.execute(
                """SELECT * FROM ai_messages WHERE chat_id=? ORDER BY sequence DESC
                LIMIT ? OFFSET ?""",
                (chat_id, page_limit + 1, page_offset),
            )
        ]
        items = rows[:page_limit]
        items.reverse()
        for item in items:
            item["attachments"] = [
                dict(row)
                for row in self.db.execute(
                    """SELECT a.id,a.media_type,a.size,a.sha256 FROM ai_attachments a
                    JOIN ai_message_attachments ma ON ma.attachment_id=a.id
                    WHERE ma.message_id=? ORDER BY ma.position""",
                    (item["id"],),
                )
            ]
        return {
            "items": items,
            "limit": page_limit,
            "offset": page_offset,
            "has_more": len(rows) > page_limit,
        }

    def create_memory(self, values: dict[str, Any]) -> dict:
        memory_id = new_id()
        timestamp = now_utc()
        scope = validate_scope(values.get("scope", "general"), memory=True)
        chat_id = str(values.get("chat_id") or "") or None
        if scope == "chat":
            if chat_id is None:
                raise ValueError("Для памяти chat нужен chat_id")
            self.get_chat(chat_id)
        elif chat_id is not None:
            raise ValueError("chat_id допустим только для памяти уровня chat")
        with self.db:
            self.db.execute(
                """INSERT INTO ai_memory(id,scope,chat_id,title,enabled,created_at,updated_at)
                VALUES(?,?,?,?,?,?,?)""",
                (
                    memory_id,
                    scope,
                    chat_id,
                    bounded_name(values.get("title"), field="Название памяти"),
                    int(bool(values.get("enabled", True))),
                    timestamp,
                    timestamp,
                ),
            )
            self._insert_memory_version(memory_id, 1, values.get("content"), timestamp)
        return self.get_memory(memory_id)

    def _insert_memory_version(
        self, memory_id: str, version: int, content: Any, timestamp: str
    ) -> str:
        version_id = new_id()
        self.db.execute(
            """INSERT INTO ai_memory_versions(id,memory_id,version,content,created_at)
            VALUES(?,?,?,?,?)""",
            (version_id, memory_id, version, bounded_text(content, field="Память"), timestamp),
        )
        return version_id

    def get_memory(self, memory_id: str) -> dict:
        row = self.db.execute(
            """SELECT m.*,v.id AS version_id,v.version,v.content,v.created_at AS version_created_at
            FROM ai_memory m JOIN ai_memory_versions v ON v.id=(SELECT id FROM ai_memory_versions
            WHERE memory_id=m.id ORDER BY version DESC LIMIT 1)
            WHERE m.id=? AND m.deleted_at IS NULL""",
            (memory_id,),
        ).fetchone()
        if row is None:
            raise ValueError("Запись памяти не найдена")
        return dict(row)

    def list_memory(
        self, *, scope: str | None, chat_id: str | None, limit: Any, offset: Any
    ) -> dict:
        page_limit, page_offset = paging(limit, offset)
        clauses = ["m.deleted_at IS NULL"]
        parameters: list[Any] = []
        if scope is not None:
            clauses.append("m.scope=?")
            parameters.append(validate_scope(scope, memory=True))
        if chat_id is not None:
            clauses.append("m.chat_id=?")
            parameters.append(chat_id)
        return self._page(
            f"""SELECT m.*,v.id AS version_id,v.version,v.content
            FROM ai_memory m JOIN ai_memory_versions v ON v.id=(SELECT id FROM ai_memory_versions
            WHERE memory_id=m.id ORDER BY version DESC LIMIT 1)
            WHERE {' AND '.join(clauses)} ORDER BY m.updated_at DESC,m.id LIMIT ? OFFSET ?""",
            tuple(parameters),
            limit=page_limit,
            offset=page_offset,
        )

    def update_memory(self, memory_id: str, changes: dict[str, Any]) -> dict:
        current = self.get_memory(memory_id)
        timestamp = now_utc()
        with self.db:
            self.db.execute(
                "UPDATE ai_memory SET title=?,enabled=?,updated_at=? WHERE id=?",
                (
                    bounded_name(changes.get("title", current["title"]), field="Название памяти"),
                    int(bool(changes.get("enabled", current["enabled"]))),
                    timestamp,
                    memory_id,
                ),
            )
            if "content" in changes and changes["content"] is not None:
                self._insert_memory_version(
                    memory_id, int(current["version"]) + 1, changes["content"], timestamp
                )
        return self.get_memory(memory_id)

    def delete_memory(self, memory_id: str) -> None:
        timestamp = now_utc()
        with self.db:
            cursor = self.db.execute(
                "UPDATE ai_memory SET deleted_at=?,updated_at=? WHERE id=? AND deleted_at IS NULL",
                (timestamp, timestamp, memory_id),
            )
            if cursor.rowcount != 1:
                raise ValueError("Запись памяти не найдена")

    def import_attachment(self, selected_path: str) -> dict:
        try:
            source = Path(selected_path).expanduser().resolve(strict=True)
        except OSError as exc:
            raise ValueError("Не удалось прочитать явно выбранное вложение") from exc
        if not source.is_file():
            raise ValueError("Вложение должно быть обычным файлом")
        if source.stat().st_size > MAX_ATTACHMENT_BYTES:
            raise ValueError("Вложение превышает локальный лимит 12 MiB")
        image = AIImage.from_file(source)
        digest = hashlib.sha256(image.data).hexdigest()
        attachment_id = new_id()
        self.attachments_root.mkdir(mode=0o700, parents=True, exist_ok=True)
        self.attachments_root.chmod(0o700)
        suffix = {
            "image/jpeg": ".jpg",
            "image/png": ".png",
            "image/gif": ".gif",
            "image/webp": ".webp",
        }[image.media_type]
        destination = self.attachments_root / f"{attachment_id}{suffix}"
        temporary = self.attachments_root / f".{attachment_id}.tmp"
        fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        installed = False
        try:
            with os.fdopen(fd, "wb") as output:
                output.write(image.data)
                output.flush()
                os.fsync(output.fileno())
            os.replace(temporary, destination)
            destination.chmod(0o600)
            with self.db:
                self.db.execute(
                    """INSERT INTO ai_attachments(id,media_type,size,sha256,file_path,created_at)
                    VALUES(?,?,?,?,?,?)""",
                    (
                        attachment_id,
                        image.media_type,
                        len(image.data),
                        digest,
                        str(destination),
                        now_utc(),
                    ),
                )
            installed = True
        finally:
            temporary.unlink(missing_ok=True)
            if not installed:
                destination.unlink(missing_ok=True)
        return self.get_attachment(attachment_id)

    def get_attachment(self, attachment_id: str, *, include_path: bool = False) -> dict:
        row = self.db.execute(
            "SELECT * FROM ai_attachments WHERE id=? AND deleted_at IS NULL", (attachment_id,)
        ).fetchone()
        if row is None:
            raise ValueError("Вложение не найдено")
        result = dict(row)
        if not include_path:
            result.pop("file_path", None)
        return result

    def delete_attachment(self, attachment_id: str) -> None:
        row = self.db.execute(
            "SELECT * FROM ai_attachments WHERE id=? AND deleted_at IS NULL", (attachment_id,)
        ).fetchone()
        if row is None:
            raise ValueError("Вложение не найдено")
        used = self.db.execute(
            "SELECT 1 FROM ai_message_attachments WHERE attachment_id=? LIMIT 1", (attachment_id,)
        ).fetchone()
        if used is not None:
            raise ValueError("Вложение уже связано с историей диалога")
        Path(str(row["file_path"])).unlink(missing_ok=True)
        with self.db:
            self.db.execute(
                "UPDATE ai_attachments SET deleted_at=? WHERE id=?", (now_utc(), attachment_id)
            )

    def create_price(self, values: dict[str, Any]) -> dict:
        price_id = new_id()
        currency = str(values.get("currency") or "").strip().upper()
        if not re.fullmatch(r"[A-Z]{3,8}", currency):
            raise ValueError("Валюта должна быть кодом из 3–8 латинских букв")
        effective_from = timestamp_text(
            values.get("effective_from") or now_utc(), field="Начало тарифа"
        )
        raw_effective_to = values.get("effective_to")
        effective_to = (
            timestamp_text(raw_effective_to, field="Окончание тарифа")
            if raw_effective_to
            else None
        )
        if effective_to is not None and effective_to <= effective_from:
            raise ValueError("Окончание тарифа должно быть позже начала")
        with self.db:
            self.db.execute(
                """INSERT INTO ai_prices(id,provider,model,effective_from,effective_to,currency,
                input_cache_hit_per_million,input_cache_miss_per_million,output_per_million,
                source,created_at) VALUES(?,?,?,?,?,?,?,?,?,?,?)""",
                (
                    price_id,
                    bounded_name(values.get("provider"), field="Провайдер"),
                    bounded_name(values.get("model"), field="Модель"),
                    effective_from,
                    effective_to,
                    currency,
                    decimal_text(
                        values.get("input_cache_hit_per_million"), field="cache-hit тариф"
                    ),
                    decimal_text(
                        values.get("input_cache_miss_per_million"), field="cache-miss тариф"
                    ),
                    decimal_text(values.get("output_per_million"), field="output тариф"),
                    bounded_name(values.get("source"), field="Источник тарифа"),
                    now_utc(),
                ),
            )
        return self.get_price(price_id)

    def get_price(self, price_id: str) -> dict:
        row = self.db.execute(
            "SELECT * FROM ai_prices WHERE id=? AND deleted_at IS NULL", (price_id,)
        ).fetchone()
        if row is None:
            raise ValueError("Тариф не найден")
        return dict(row)

    def list_prices(
        self,
        *,
        provider: str | None,
        model: str | None,
        limit: Any,
        offset: Any,
    ) -> dict:
        page_limit, page_offset = paging(limit, offset)
        clauses = ["deleted_at IS NULL"]
        parameters: list[Any] = []
        if provider is not None:
            clauses.append("provider=?")
            parameters.append(provider)
        if model is not None:
            clauses.append("model=?")
            parameters.append(model)
        return self._page(
            f"SELECT * FROM ai_prices WHERE {' AND '.join(clauses)} "
            "ORDER BY effective_from DESC,id LIMIT ? OFFSET ?",
            tuple(parameters),
            limit=page_limit,
            offset=page_offset,
        )

    def delete_price(self, price_id: str) -> None:
        with self.db:
            cursor = self.db.execute(
                "UPDATE ai_prices SET deleted_at=? WHERE id=? AND deleted_at IS NULL",
                (now_utc(), price_id),
            )
            if cursor.rowcount != 1:
                raise ValueError("Тариф не найден")

    def price_for(self, provider: str, model: str, at: str) -> dict | None:
        row = self.db.execute(
            """SELECT * FROM ai_prices WHERE provider=? AND model=? AND deleted_at IS NULL
            AND effective_from<=? AND (effective_to IS NULL OR effective_to>?)
            ORDER BY effective_from DESC,id DESC LIMIT 1""",
            (provider, model, at, at),
        ).fetchone()
        return dict(row) if row is not None else None

    def attempt(self, attempt_id: str) -> dict:
        row = self.db.execute("SELECT * FROM ai_attempts WHERE id=?", (attempt_id,)).fetchone()
        if row is None:
            raise ValueError("AI-попытка не найдена")
        result = dict(row)
        result["request_config"] = self._decode(result["request_config"]) or {}
        result["context_metadata"] = self._decode(result["context_metadata"]) or {}
        result["price_snapshot"] = self._decode(result["price_snapshot"])
        return result

    def attempt_by_operation(self, operation_key: str) -> dict | None:
        row = self.db.execute(
            "SELECT id FROM ai_attempts WHERE operation_key=?", (operation_key,)
        ).fetchone()
        return self.attempt(str(row["id"])) if row else None

    def attempt_by_job(self, job_id: int) -> dict | None:
        row = self.db.execute(
            "SELECT id FROM ai_attempts WHERE job_id=?", (job_id,)
        ).fetchone()
        return self.attempt(str(row["id"])) if row else None

    def list_attempts(
        self,
        *,
        scope: str | None,
        chat_id: str | None,
        state: str | None,
        limit: Any,
        offset: Any,
    ) -> dict:
        page_limit, page_offset = paging(limit, offset)
        clauses = ["1=1"]
        parameters: list[Any] = []
        if scope is not None:
            clauses.append("scope=?")
            parameters.append(validate_scope(scope))
        if chat_id is not None:
            clauses.append("chat_id=?")
            parameters.append(str(chat_id))
        if state is not None:
            selected_state = str(state)
            if selected_state not in _STATES:
                raise ValueError("Неизвестное состояние AI-попытки")
            clauses.append("state=?")
            parameters.append(selected_state)
        result = self._page(
            f"""SELECT id,job_id,chat_id,scope,entity_kind,entity_id,provider,
            requested_model,actual_model,state,error_code,estimated_cost,cost_currency,
            cost_status,created_at,sent_at,completed_at FROM ai_attempts
            WHERE {' AND '.join(clauses)} ORDER BY created_at DESC,id LIMIT ? OFFSET ?""",
            tuple(parameters),
            limit=page_limit,
            offset=page_offset,
        )
        return result

    def telegram_analysis(self, analysis_id: str) -> dict:
        row = self.db.execute(
            "SELECT * FROM ai_telegram_analyses WHERE id=?", (str(analysis_id),)
        ).fetchone()
        if row is None:
            raise ValueError("Telegram AI-анализ не найден")
        result = dict(row)
        result["attempt"] = self.attempt(str(result["attempt_id"]))
        result["photos"] = [
            dict(photo)
            for photo in self.db.execute(
                """SELECT photo_id,attachment_id,position,source_sha256,size
                FROM ai_telegram_analysis_photos WHERE analysis_id=? ORDER BY position""",
                (str(analysis_id),),
            )
        ]
        return result

    def telegram_analysis_by_operation(self, operation_key: str) -> dict | None:
        row = self.db.execute(
            "SELECT id FROM ai_telegram_analyses WHERE operation_key=?", (operation_key,)
        ).fetchone()
        return self.telegram_analysis(str(row["id"])) if row else None

    def list_telegram_analyses(
        self,
        *,
        author_id: int | None,
        successful_only: bool,
        limit: Any,
        offset: Any,
    ) -> dict:
        page_limit, page_offset = paging(limit, offset)
        clauses = ["1=1"]
        parameters: list[Any] = []
        if author_id is not None:
            clauses.append("a.author_id=?")
            parameters.append(int(author_id))
        if successful_only:
            clauses.append("a.result_status='completed'")
        rows = [
            dict(row)
            for row in self.db.execute(
                f"""SELECT a.*,t.state AS attempt_state,t.error_code,t.estimated_cost,
                t.cost_currency,t.cost_status,t.requested_model
                FROM ai_telegram_analyses a JOIN ai_attempts t ON t.id=a.attempt_id
                WHERE {' AND '.join(clauses)} ORDER BY a.created_at DESC,a.id
                LIMIT ? OFFSET ?""",
                (*parameters, page_limit + 1, page_offset),
            )
        ]
        return {
            "items": rows[:page_limit],
            "limit": page_limit,
            "offset": page_offset,
            "has_more": len(rows) > page_limit,
        }

    def catalog_telegram_analyses(self) -> list[dict[str, Any]]:
        """Read the complete compact PSL journal in one ordered query for HTML export."""
        return [
            dict(row)
            for row in self.db.execute(
                """SELECT a.id,a.author_id,a.attempt_id,a.prompt_version_id,
                a.schema_version,a.result_status,a.presentation,a.estimated_age,
                a.raw_psl_min,a.raw_psl_max,a.boosted_psl,
                COALESCE(a.actual_model,t.actual_model) AS actual_model,
                a.analyzed_at,a.created_at,t.state AS attempt_state,t.completed_at
                FROM ai_telegram_analyses a
                JOIN ai_attempts t ON t.id=a.attempt_id
                ORDER BY a.author_id,
                COALESCE(t.completed_at,a.analyzed_at,a.created_at) DESC,
                COALESCE(a.analyzed_at,a.created_at) DESC,a.id DESC"""
            )
        ]

    def _mark_catalog_changed_sql(self) -> None:
        """Mark a catalog-visible AI mutation inside the caller's transaction."""
        self.db.execute(
            """INSERT INTO tg_settings(key,value) VALUES('catalog_dirty','1')
            ON CONFLICT(key) DO UPDATE SET value='1'"""
        )
        self.db.execute(
            """INSERT INTO tg_settings(key,value) VALUES('catalog_revision','1')
            ON CONFLICT(key) DO UPDATE SET
            value=CAST(CAST(value AS INTEGER)+1 AS TEXT)"""
        )

    def job_state(self, job_id: int) -> dict:
        row = self.db.execute(
            """SELECT id,kind,state,attempts,result,created_at,updated_at
            FROM jobs WHERE id=? AND kind IN ('ai.request','ai.chat')""",
            (job_id,),
        ).fetchone()
        if row is None:
            raise ValueError("AI-задание не найдено")
        return dict(row)

    def active_attempt(self, chat_id: str) -> dict | None:
        row = self.db.execute(
            """SELECT id FROM ai_attempts WHERE chat_id=? AND state IN ('queued','running')
            ORDER BY created_at LIMIT 1""",
            (chat_id,),
        ).fetchone()
        return self.attempt(str(row["id"])) if row else None

    def mark_attempt_running(self, attempt_id: str) -> dict:
        timestamp = now_utc()
        with self.db:
            self.db.execute(
                """UPDATE ai_attempts SET state='running',sent_at=COALESCE(sent_at,?)
                WHERE id=? AND state='queued'""",
                (timestamp, attempt_id),
            )
        return self.attempt(attempt_id)

    def recover_interrupted_attempts(self) -> int:
        """Make a crash-interrupted paid request require an explicit retry."""
        timestamp = now_utc()
        with self.db:
            rows = list(
                self.db.execute(
                    """SELECT a.id,a.user_message_id,a.entity_kind FROM ai_attempts a
                    LEFT JOIN jobs j ON j.id=a.job_id
                    WHERE a.state='running' OR (a.state='queued' AND j.state='running')"""
                )
            )
            attempt_ids = [str(row["id"]) for row in rows]
            if attempt_ids:
                placeholders = ",".join("?" for _ in attempt_ids)
                self.db.execute(
                    f"""UPDATE ai_attempts SET state='outcome_unknown',
                    error_code='daemon_interrupted',cost_status='unknown',completed_at=?
                    WHERE id IN ({placeholders})""",
                    (timestamp, *attempt_ids),
                )
            message_ids = [str(row["user_message_id"]) for row in rows if row["user_message_id"]]
            if message_ids:
                placeholders = ",".join("?" for _ in message_ids)
                self.db.execute(
                    f"UPDATE ai_messages SET state='failed' WHERE id IN ({placeholders})",
                    tuple(message_ids),
                )
            if any(row["entity_kind"] == "telegram.psl" for row in rows):
                self._mark_catalog_changed_sql()
        return len(rows)

    def finish_attempt(
        self,
        attempt_id: str,
        *,
        state: str,
        usage: AIUsage | None,
        response: Any | None,
        error_code: str | None,
        cost: CostEstimate,
        price: dict[str, Any] | None,
        analysis_result: dict[str, Any] | None = None,
    ) -> dict:
        if state not in _STATES - {"queued", "running"}:
            raise ValueError("Invalid terminal attempt state")
        current = self.attempt(attempt_id)
        if current["state"] in {"completed", "failed", "outcome_unknown"}:
            return current
        values = usage.to_dict() if usage else {}
        timestamp = now_utc()
        with self.db:
            self.db.execute(
                """UPDATE ai_attempts SET state=?,error_code=?,actual_model=?,provider_request_id=?,
                prompt_tokens=?,completion_tokens=?,total_tokens=?,prompt_cache_hit_tokens=?,
                prompt_cache_miss_tokens=?,reasoning_tokens=?,estimated_cost=?,cost_currency=?,
                cost_status=?,price_id=?,price_snapshot=?,completed_at=? WHERE id=?""",
                (
                    state,
                    error_code,
                    getattr(response, "model", None),
                    getattr(response, "request_id", None),
                    values.get("prompt_tokens"),
                    values.get("completion_tokens"),
                    values.get("total_tokens"),
                    values.get("prompt_cache_hit_tokens"),
                    values.get("prompt_cache_miss_tokens"),
                    values.get("reasoning_tokens"),
                    cost.amount,
                    cost.currency,
                    cost.status,
                    price.get("id") if price else None,
                    self._json(price) if price else None,
                    timestamp,
                    attempt_id,
                ),
            )
            if state == "completed" and current.get("chat_id"):
                sequence = self.db.execute(
                    "SELECT COALESCE(MAX(sequence),0)+1 FROM ai_messages WHERE chat_id=?",
                    (current["chat_id"],),
                ).fetchone()[0]
                self.db.execute(
                    """INSERT OR IGNORE INTO ai_messages(id,chat_id,sequence,role,content,state,
                    attempt_id,created_at) VALUES(?,?,?,?,?,'completed',?,?)""",
                    (
                        new_id(),
                        current["chat_id"],
                        sequence,
                        "assistant",
                        str(getattr(response, "content", "")),
                        attempt_id,
                        timestamp,
                    ),
                )
            if current.get("user_message_id"):
                message_state = "completed" if state == "completed" else "failed"
                self.db.execute(
                    "UPDATE ai_messages SET state=? WHERE id=?",
                    (message_state, current["user_message_id"]),
                )
            if current.get("chat_id"):
                self.db.execute(
                    "UPDATE ai_chats SET updated_at=? WHERE id=?", (timestamp, current["chat_id"])
                )
            if current.get("entity_kind") == "telegram.psl":
                if state == "completed" and analysis_result is not None:
                    self.db.execute(
                        """UPDATE ai_telegram_analyses SET result_status=?,presentation=?,
                        estimated_age=?,raw_psl_min=?,raw_psl_max=?,boosted_psl=?,
                        actual_model=?,analyzed_at=? WHERE id=? AND analyzed_at IS NULL""",
                        (
                            analysis_result["status"],
                            analysis_result["presentation"],
                            analysis_result["estimated_age"],
                            analysis_result["raw_psl_min"],
                            analysis_result["raw_psl_max"],
                            analysis_result["boosted_psl"],
                            getattr(response, "model", None),
                            timestamp,
                            current["entity_id"],
                        ),
                    )
                elif error_code in {"invalid_json", "json_schema_mismatch", "invalid_psl_result"}:
                    self.db.execute(
                        """UPDATE ai_telegram_analyses SET result_status='invalid_result',
                        actual_model=?,analyzed_at=? WHERE id=? AND analyzed_at IS NULL""",
                        (getattr(response, "model", None), timestamp, current["entity_id"]),
                    )
                self._mark_catalog_changed_sql()
        return self.attempt(attempt_id)

    def statistics(self, filters: dict[str, Any]) -> dict:
        clauses = ["1=1"]
        parameters: list[Any] = []
        mapping = {
            "provider": "provider",
            "model": "COALESCE(actual_model,requested_model)",
            "scope": "scope",
            "chat_id": "chat_id",
        }
        for key, column in mapping.items():
            if filters.get(key) is not None:
                clauses.append(f"{column}=?")
                parameters.append(str(filters[key]))
        if filters.get("date_from"):
            clauses.append("created_at>=?")
            parameters.append(str(filters["date_from"]))
        if filters.get("date_to"):
            clauses.append("created_at<?")
            parameters.append(str(filters["date_to"]))
        where = " AND ".join(clauses)
        row = self.db.execute(
            f"""SELECT COUNT(*) AS attempts,
            SUM(CASE WHEN state='completed' THEN 1 ELSE 0 END) AS completed,
            SUM(CASE WHEN state='failed' THEN 1 ELSE 0 END) AS failed,
            SUM(CASE WHEN state='outcome_unknown' THEN 1 ELSE 0 END) AS outcome_unknown,
            SUM(prompt_tokens) AS prompt_tokens,SUM(completion_tokens) AS completion_tokens,
            SUM(total_tokens) AS total_tokens,SUM(prompt_cache_hit_tokens) AS cache_hit_tokens,
            SUM(prompt_cache_miss_tokens) AS cache_miss_tokens,
            SUM(reasoning_tokens) AS reasoning_tokens,
            SUM(CASE WHEN cost_status!='complete' THEN 1 ELSE 0 END) AS unknown_cost
            FROM ai_attempts WHERE {where}""",
            parameters,
        ).fetchone()
        result = dict(row)
        costs: dict[str, str] = {}
        for cost_row in self.db.execute(
            f"""SELECT cost_currency,estimated_cost FROM ai_attempts WHERE {where}
            AND estimated_cost IS NOT NULL""",
            parameters,
        ):
            currency = str(cost_row["cost_currency"] or "unknown")
            from decimal import Decimal

            total = Decimal(costs.get(currency, "0")) + Decimal(cost_row["estimated_cost"])
            costs[currency] = format(total, "f")
        result["estimated_costs"] = costs
        return result
