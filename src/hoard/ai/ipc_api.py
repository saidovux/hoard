"""Validated, paginated daemon IPC operations for AI infrastructure."""
from __future__ import annotations

from typing import Any

from .chat_service import AIChatService
from .storage import AIStorage


class AIIPC:
    def __init__(self, storage: AIStorage, chats: AIChatService, runner):
        self.storage = storage
        self.chats = chats
        self.runner = runner

    @staticmethod
    def _id(args: dict[str, Any]) -> str:
        value = str(args.get("id") or "").strip()
        if not value:
            raise ValueError("Нужен id")
        return value

    async def handle(self, method: str, args: dict[str, Any]) -> dict:
        if method == "ai_prompt_create":
            return {"prompt": self.storage.create_prompt(args)}
        if method == "ai_prompt_get":
            return {"prompt": self.storage.get_prompt(self._id(args))}
        if method == "ai_prompt_list":
            return self.storage.list_prompts(
                scope=args.get("scope"), limit=args.get("limit", 20), offset=args.get("offset", 0)
            )
        if method == "ai_prompt_update":
            return {"prompt": self.storage.update_prompt(self._id(args), args)}
        if method == "ai_prompt_delete":
            self.storage.delete_prompt(self._id(args))
            return {"message": "Промпт удалён; исторические версии сохранены для аудита"}
        if method == "ai_prompt_versions":
            return self.storage.prompt_versions(
                self._id(args), limit=args.get("limit", 20), offset=args.get("offset", 0)
            )
        if method == "ai_prompt_activate":
            return {
                "prompt": self.storage.activate_prompt(str(args.get("scope") or ""), self._id(args))
            }

        if method == "ai_chat_create":
            return {"chat": self.storage.create_chat(args)}
        if method == "ai_chat_get":
            return {"chat": self.storage.get_chat(self._id(args))}
        if method == "ai_chat_list":
            archived = args.get("archived")
            return self.storage.list_chats(
                scope=args.get("scope"),
                archived=bool(archived) if archived is not None else None,
                limit=args.get("limit", 20),
                offset=args.get("offset", 0),
            )
        if method == "ai_chat_update":
            return {"chat": self.storage.update_chat(self._id(args), args)}
        if method == "ai_chat_delete":
            self.storage.delete_chat(self._id(args))
            return {"message": "Диалог удалён из активных списков"}
        if method == "ai_chat_history":
            return self.storage.chat_history(
                self._id(args), limit=args.get("limit", 20), offset=args.get("offset", 0)
            )
        if method == "ai_chat_send":
            attempt = self.chats.queue_message(
                chat_id=str(args.get("chat_id") or ""),
                content=args.get("content"),
                operation_id=args.get("operation_id"),
                memory_ids=(
                    args.get("memory_ids")
                    if isinstance(args.get("memory_ids"), list)
                    else []
                ),
                attachment_ids=(
                    args.get("attachment_ids")
                    if isinstance(args.get("attachment_ids"), list)
                    else []
                ),
            )
            self.runner.wakeup()
            return {"attempt": attempt, "message": "AI-запрос поставлен в очередь"}

        if method == "ai_memory_create":
            return {"memory": self.storage.create_memory(args)}
        if method == "ai_memory_get":
            return {"memory": self.storage.get_memory(self._id(args))}
        if method == "ai_memory_list":
            return self.storage.list_memory(
                scope=args.get("scope"),
                chat_id=str(args["chat_id"]) if args.get("chat_id") else None,
                limit=args.get("limit", 20),
                offset=args.get("offset", 0),
            )
        if method == "ai_memory_update":
            return {"memory": self.storage.update_memory(self._id(args), args)}
        if method == "ai_memory_delete":
            self.storage.delete_memory(self._id(args))
            return {"message": "Запись памяти удалена"}

        if method == "ai_attachment_import":
            return {"attachment": self.storage.import_attachment(str(args.get("path") or ""))}
        if method == "ai_attachment_get":
            return {"attachment": self.storage.get_attachment(self._id(args))}
        if method == "ai_attachment_delete":
            self.storage.delete_attachment(self._id(args))
            return {"message": "Вложение удалено"}

        if method == "ai_price_create":
            return {"price": self.storage.create_price(args)}
        if method == "ai_price_list":
            return self.storage.list_prices(
                provider=str(args["provider"]) if args.get("provider") else None,
                model=str(args["model"]) if args.get("model") else None,
                limit=args.get("limit", 20),
                offset=args.get("offset", 0),
            )
        if method == "ai_price_delete":
            self.storage.delete_price(self._id(args))
            return {"message": "Тариф отключён; исторические снимки не изменены"}

        if method == "ai_attempt_get":
            return {"attempt": self.storage.attempt(self._id(args))}
        if method == "ai_job_state":
            if args.get("job_id") is not None:
                job_id = int(args["job_id"])
                attempt = self.storage.attempt_by_job(job_id)
            else:
                identifier = self._id(args)
                try:
                    attempt = self.storage.attempt(identifier)
                except ValueError:
                    job_id = int(identifier)
                    attempt = self.storage.attempt_by_job(job_id)
                else:
                    job_id = int(attempt["job_id"])
            return {"attempt": attempt, "job": self.storage.job_state(job_id)}
        if method == "ai_attempt_retry":
            attempt = self.chats.retry_attempt(self._id(args), args.get("operation_id"))
            self.runner.wakeup()
            return {"attempt": attempt, "message": "Создана новая AI-попытка"}
        if method == "ai_statistics":
            return self.storage.statistics(args)
        raise ValueError(f"Неизвестная AI-команда: {method}")
