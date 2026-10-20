"""Telegram MTProto operations owned exclusively by the background service."""
from __future__ import annotations

import json
import os
import time
from enum import StrEnum
from pathlib import Path
from typing import Any

from .archive_paths import post_media_path, profile_photo_path, video_note_path
from .config import read_credentials
from .models import Post, iso_date, post_comment_link
from .paths import ensure_private, session_path
from .telegram_storage import utc_now


class AuthState(StrEnum):
    """Stable states exposed to the terminal client over IPC."""

    API_REQUIRED = "api_required"
    DISCONNECTED = "disconnected"
    PHONE_REQUIRED = "phone_required"
    CODE_REQUIRED = "code_required"
    PASSWORD_REQUIRED = "password_required"
    AUTHORIZED = "authorized"


class AuthFlowError(RuntimeError):
    """An expected, user-actionable Telegram authorization failure."""

    def __init__(
        self,
        message: str,
        state: AuthState,
        *,
        retryable: bool = True,
        code: str = "auth_error",
    ):
        super().__init__(message)
        self.state = state
        self.retryable = retryable
        self.code = code


class TelegramEngine:
    def __init__(self, store, report_progress=None):
        self.store = store
        self.report_progress = report_progress or (lambda _: None)
        self.progress: dict = {}
        self.client = None
        self.phone = ""
        self.phone_code_hash = ""
        self.authorization_step = AuthState.DISCONNECTED

    def auth_result(self, message: str = "") -> dict[str, Any]:
        return {
            "state": str(self.authorization_step),
            "authorized": self.authorization_step == AuthState.AUTHORIZED,
            "message": message,
        }

    def _set_auth_state(self, state: AuthState) -> None:
        self.authorization_step = state

    async def _connect_for_auth(self, state: AuthState):
        try:
            return await self.connect()
        except AuthFlowError:
            raise
        except (OSError, TimeoutError) as exc:
            raise AuthFlowError(
                "Не удалось связаться с Telegram. Проверь интернет и повтори попытку.",
                state,
                code="network",
            ) from exc

    async def connect(self):
        if self.client is None:
            from telethon import TelegramClient
            creds = read_credentials()
            if creds is None:
                self._set_auth_state(AuthState.API_REQUIRED)
                raise AuthFlowError(
                    "Для подключения нужны API ID и API Hash с my.telegram.org/apps.",
                    AuthState.API_REQUIRED,
                    retryable=False,
                    code="api_required",
                )
            ensure_private(session_path().parent)
            self.client = TelegramClient(str(session_path()), *creds, flood_sleep_threshold=10)
            await self.client.connect()
        elif not self.client.is_connected():
            await self.client.connect()
        return self.client

    async def disconnect(self) -> None:
        if self.client is not None:
            await self.client.disconnect()
            self.client = None
        self._set_auth_state(
            AuthState.API_REQUIRED if read_credentials() is None else AuthState.DISCONNECTED
        )

    async def authorized(self) -> bool:
        client = await self.connect()
        ok = await client.is_user_authorized()
        self._set_auth_state(AuthState.AUTHORIZED if ok else AuthState.PHONE_REQUIRED)
        return ok

    async def begin_authorization(self) -> dict[str, Any]:
        """Inspect an existing session before asking the user for anything."""
        if read_credentials() is None:
            self._set_auth_state(AuthState.API_REQUIRED)
            return self.auth_result()
        pending_state = self.authorization_step
        try:
            if await self.authorized():
                return self.auth_result("Telegram подключён; сохранённая сессия действительна.")
        except (OSError, TimeoutError) as exc:
            raise AuthFlowError(
                "Не удалось связаться с Telegram. Проверь интернет и повтори попытку.",
                self.authorization_step,
                code="network",
            ) from exc
        if pending_state == AuthState.PASSWORD_REQUIRED:
            self._set_auth_state(AuthState.PASSWORD_REQUIRED)
            return self.auth_result("Требуется пароль двухэтапной проверки.")
        if self.phone and self.phone_code_hash:
            self._set_auth_state(AuthState.CODE_REQUIRED)
        else:
            self._set_auth_state(AuthState.PHONE_REQUIRED)
        return self.auth_result()

    async def send_code(self, phone: str, *, force: bool = False) -> dict[str, Any]:
        from telethon.errors import (
            ApiIdInvalidError,
            FloodWaitError,
            PhoneNumberBannedError,
            PhoneNumberFloodError,
            PhoneNumberInvalidError,
            RPCError,
        )

        client = await self._connect_for_auth(AuthState.PHONE_REQUIRED)
        try:
            if await client.is_user_authorized():
                self._set_auth_state(AuthState.AUTHORIZED)
                return self.auth_result("Telegram уже подключён.")
        except (OSError, TimeoutError) as exc:
            raise AuthFlowError(
                "Не удалось проверить сессию Telegram. Повтори попытку.",
                AuthState.PHONE_REQUIRED,
                code="network",
            ) from exc
        phone = phone.strip()
        if not phone.startswith("+") or not phone[1:].isdigit():
            raise AuthFlowError(
                "Введи номер в международном формате, например +79990000000.",
                AuthState.PHONE_REQUIRED,
                code="phone_format",
            )
        if not force and phone == self.phone and self.phone_code_hash:
            self._set_auth_state(AuthState.CODE_REQUIRED)
            return self.auth_result("Код уже отправлен; новый запрос не потребовался.")
        try:
            sent = await client.send_code_request(phone)
        except PhoneNumberInvalidError as exc:
            self._set_auth_state(AuthState.PHONE_REQUIRED)
            raise AuthFlowError(
                "Telegram не распознал номер. Проверь международный формат.",
                AuthState.PHONE_REQUIRED,
                code="phone_invalid",
            ) from exc
        except PhoneNumberBannedError as exc:
            raise AuthFlowError(
                "Telegram заблокировал этот номер. "
                "Используй другой аккаунт или обратись в поддержку.",
                AuthState.PHONE_REQUIRED,
                retryable=False,
                code="phone_banned",
            ) from exc
        except PhoneNumberFloodError as exc:
            raise AuthFlowError(
                "Telegram временно ограничил запросы к этому номеру. Попробуй позже.",
                AuthState.PHONE_REQUIRED,
                retryable=False,
                code="phone_flood",
            ) from exc
        except ApiIdInvalidError as exc:
            self._set_auth_state(AuthState.API_REQUIRED)
            raise AuthFlowError(
                "Telegram отклонил API ID / API Hash. Проверь ключи на my.telegram.org/apps.",
                AuthState.API_REQUIRED,
                code="api_invalid",
            ) from exc
        except FloodWaitError as exc:
            raise AuthFlowError(
                f"Telegram просит подождать {exc.seconds} с перед новым запросом кода.",
                AuthState.PHONE_REQUIRED,
                retryable=False,
                code="flood_wait",
            ) from exc
        except (OSError, TimeoutError) as exc:
            raise AuthFlowError(
                "Не удалось запросить код. Проверь интернет и повтори попытку.",
                AuthState.PHONE_REQUIRED,
                code="network",
            ) from exc
        except RPCError as exc:
            raise AuthFlowError(
                "Telegram отклонил запрос кода. Проверь номер и попробуй позже.",
                AuthState.PHONE_REQUIRED,
                code="rpc_error",
            ) from exc
        self.phone, self.phone_code_hash = phone, sent.phone_code_hash
        self._set_auth_state(AuthState.CODE_REQUIRED)
        return self.auth_result("Код отправлен в Telegram.")

    async def sign_in(self, code: str) -> dict[str, Any]:
        from telethon.errors import (
            FloodWaitError,
            PhoneCodeEmptyError,
            PhoneCodeExpiredError,
            PhoneCodeHashEmptyError,
            PhoneCodeInvalidError,
            RPCError,
            SessionPasswordNeededError,
        )

        if not self.phone or not self.phone_code_hash:
            self._set_auth_state(AuthState.PHONE_REQUIRED)
            raise AuthFlowError(
                "Сначала запроси новый код Telegram.",
                AuthState.PHONE_REQUIRED,
                code="code_required",
            )
        if not code.strip():
            raise AuthFlowError(
                "Код не должен быть пустым.",
                AuthState.CODE_REQUIRED,
                code="code_empty",
            )
        client = await self._connect_for_auth(AuthState.CODE_REQUIRED)
        try:
            await client.sign_in(
                phone=self.phone,
                code=code.strip(),
                phone_code_hash=self.phone_code_hash,
            )
        except SessionPasswordNeededError:
            self._set_auth_state(AuthState.PASSWORD_REQUIRED)
            return self.auth_result("Аккаунт защищён паролем двухэтапной проверки.")
        except (PhoneCodeInvalidError, PhoneCodeEmptyError) as exc:
            raise AuthFlowError(
                "Неверный код. Проверь сообщение Telegram и введи код ещё раз.",
                AuthState.CODE_REQUIRED,
                code="code_invalid",
            ) from exc
        except (PhoneCodeExpiredError, PhoneCodeHashEmptyError) as exc:
            self.phone_code_hash = ""
            self._set_auth_state(AuthState.PHONE_REQUIRED)
            raise AuthFlowError(
                "Срок действия кода истёк. Запроси новый код.",
                AuthState.PHONE_REQUIRED,
                code="code_expired",
            ) from exc
        except FloodWaitError as exc:
            raise AuthFlowError(
                f"Слишком много попыток. Telegram просит подождать {exc.seconds} с.",
                AuthState.CODE_REQUIRED,
                retryable=False,
                code="flood_wait",
            ) from exc
        except (OSError, TimeoutError) as exc:
            raise AuthFlowError(
                "Связь с Telegram прервалась. Код не потерян; повтори отправку.",
                AuthState.CODE_REQUIRED,
                code="network",
            ) from exc
        except RPCError as exc:
            raise AuthFlowError(
                "Telegram не принял код. Проверь его или запроси новый.",
                AuthState.CODE_REQUIRED,
                code="rpc_error",
            ) from exc
        self._set_auth_state(AuthState.AUTHORIZED)
        self.phone = self.phone_code_hash = ""
        return self.auth_result("Telegram успешно подключён.")

    async def sign_in_password(self, password: str) -> dict[str, Any]:
        from telethon.errors import FloodWaitError, PasswordHashInvalidError, RPCError

        if self.authorization_step != AuthState.PASSWORD_REQUIRED:
            raise AuthFlowError(
                "Telegram пока не запрашивал пароль двухэтапной проверки.",
                AuthState.PHONE_REQUIRED,
                code="password_not_required",
            )
        if not password:
            raise AuthFlowError(
                "Пароль не должен быть пустым.",
                AuthState.PASSWORD_REQUIRED,
                code="password_empty",
            )
        client = await self._connect_for_auth(AuthState.PASSWORD_REQUIRED)
        try:
            await client.sign_in(password=password)
        except PasswordHashInvalidError as exc:
            raise AuthFlowError(
                "Неверный пароль двухэтапной проверки. Попробуй ещё раз.",
                AuthState.PASSWORD_REQUIRED,
                code="password_invalid",
            ) from exc
        except FloodWaitError as exc:
            raise AuthFlowError(
                f"Слишком много попыток. Telegram просит подождать {exc.seconds} с.",
                AuthState.PASSWORD_REQUIRED,
                retryable=False,
                code="flood_wait",
            ) from exc
        except (OSError, TimeoutError) as exc:
            raise AuthFlowError(
                "Связь с Telegram прервалась. Повтори ввод пароля.",
                AuthState.PASSWORD_REQUIRED,
                code="network",
            ) from exc
        except RPCError as exc:
            raise AuthFlowError(
                "Telegram не принял пароль. Попробуй ещё раз позже.",
                AuthState.PASSWORD_REQUIRED,
                code="rpc_error",
            ) from exc
        self._set_auth_state(AuthState.AUTHORIZED)
        self.phone = self.phone_code_hash = ""
        return self.auth_result("Telegram успешно подключён.")

    async def cancel_authorization(self) -> dict[str, Any]:
        """Forget only the pending login challenge, never the saved session."""
        self.phone = self.phone_code_hash = ""
        authorized = False
        if self.client is not None:
            try:
                authorized = await self.client.is_user_authorized()
            except (OSError, TimeoutError):
                authorized = False
        if self.client is not None and not authorized:
            try:
                await self.disconnect()
            except (OSError, TimeoutError):
                self.client = None
        if read_credentials() is None:
            self._set_auth_state(AuthState.API_REQUIRED)
        elif self.authorization_step != AuthState.AUTHORIZED:
            self._set_auth_state(AuthState.PHONE_REQUIRED)
        return self.auth_result("Подключение отменено; служба продолжает работу.")

    async def validate_post(self, post: Post) -> dict[str, str | int]:
        """Resolve a post before it is persisted as a source."""
        from telethon.errors import FloodWaitError, RPCError

        client = await self._connect_for_auth(AuthState.PHONE_REQUIRED)
        try:
            authorized = await client.is_user_authorized()
        except (OSError, TimeoutError) as exc:
            raise ValueError(
                "Не удалось проверить сессию Telegram. Повтори попытку."
            ) from exc
        if not authorized:
            self._set_auth_state(AuthState.PHONE_REQUIRED)
            raise AuthFlowError(
                "Сначала подключи Telegram.",
                AuthState.PHONE_REQUIRED,
                code="phone_required",
            )
        try:
            entity = await client.get_entity(post.username)
            if not getattr(entity, "broadcast", False):
                raise ValueError("Ссылка должна вести на пост Telegram-канала.")
            original = await client.get_messages(entity, ids=post.message_id)
        except FloodWaitError as exc:
            raise ValueError(
                f"Telegram просит подождать {exc.seconds} с перед проверкой ссылки."
            ) from exc
        except (OSError, TimeoutError) as exc:
            raise ValueError(
                "Не удалось проверить пост. Проверь интернет и повтори попытку."
            ) from exc
        except RPCError as exc:
            raise ValueError(
                "Telegram не дал доступ к каналу. Проверь ссылку и доступ аккаунта."
            ) from exc
        if original is None:
            raise ValueError("Пост не найден или у подключённого аккаунта нет доступа.")
        return {
            "url": post.link,
            "username": post.username,
            "post_id": post.message_id,
            "title": str(getattr(entity, "title", "") or post.username),
        }

    @staticmethod
    def _sender_identity(sender, sender_id: int | None) -> dict[str, Any]:
        name = " ".join(
            filter(
                None,
                [getattr(sender, "first_name", ""), getattr(sender, "last_name", "")],
            )
        ) or getattr(sender, "title", "")
        return {
            "author_id": int(sender_id) if sender_id is not None else None,
            "author_name": str(name or ""),
            "author_username": str(getattr(sender, "username", "") or ""),
        }

    async def _message_identity(self, msg) -> dict[str, Any]:
        sender = getattr(msg, "sender", None)
        if sender is None:
            try:
                sender = await msg.get_sender()
            except Exception:
                sender = None
        return self._sender_identity(sender, getattr(msg, "sender_id", None))

    @staticmethod
    def _post_media_descriptor(message) -> tuple[str, str, str] | None:
        photo = getattr(message, "photo", None)
        if photo is not None and getattr(photo, "id", None) is not None:
            return str(photo.id), "image", ".jpg"
        document = getattr(message, "document", None)
        if document is None or getattr(document, "id", None) is None:
            return None
        mime = str(getattr(document, "mime_type", "") or "").lower()
        if mime.startswith("video/"):
            kind, suffix = "video", ".mp4"
        elif mime in {"image/jpeg", "image/jpg"}:
            kind, suffix = "image", ".jpg"
        elif mime == "image/png":
            kind, suffix = "image", ".png"
        elif mime == "image/webp":
            kind, suffix = "image", ".webp"
        else:
            kind, suffix = "file", ".bin"
        return str(document.id), kind, suffix

    def _record_post(self, source: dict, entity, original) -> None:
        self.store.telegram.record_post_metadata(
            int(source["id"]),
            channel_title=str(getattr(entity, "title", "") or source.get("title") or ""),
            post_text=str(
                getattr(original, "message", "") or getattr(original, "text", "") or ""
            ),
            published_at=iso_date(getattr(original, "date", None)),
        )
        descriptor = self._post_media_descriptor(original)
        if descriptor is not None:
            media_id, _, _ = descriptor
            current = self.store.telegram.post_media(int(source["id"]), media_id)
            valid = False
            if current is not None:
                try:
                    path = Path(str(current["file_path"]))
                    valid = (
                        path.is_file()
                        and int(current["size"]) > 0
                        and path.stat().st_size == int(current["size"])
                    )
                except (OSError, TypeError, ValueError):
                    valid = False
            if not valid:
                self.store.enqueue(
                    "telegram.post_media",
                    {"source_id": int(source["id"])},
                    unique_key=f"telegram-post-media:{int(source['id'])}",
                )

    async def scan(self, source: dict) -> tuple[int, int]:
        """Walk linked-post comments from the last checkpoint, oldest first.

        Checkpoints are written after each completed media. On exception, do not
        advance the cursor beyond unprocessed messages.
        """
        client = await self.connect()
        if not await client.is_user_authorized():
            self._set_auth_state(AuthState.PHONE_REQUIRED)
            raise RuntimeError("Telegram не подключён. Выполни подключение в интерфейсе.")
        post = Post(source["username"], int(source["post_id"]))
        entity = await client.get_entity(post.username)
        if not getattr(entity, "broadcast", False):
            raise ValueError("Ссылка должна указывать на пост канала с комментариями")
        original = await client.get_messages(entity, ids=post.message_id)
        if original is None:
            raise ValueError("Пост не найден или нет доступа")
        self._record_post(source, entity, original)
        cursor = int(source["last_id"])
        missing = self.store.earliest_missing_download(source["id"])
        if missing is not None:
            cursor = min(cursor, missing - 1)
        checked = downloaded = 0
        # Telethon docs: channel/reply_to returns the linked discussion thread.
        async for msg in client.iter_messages(entity, reply_to=post.message_id,
                                               min_id=cursor, reverse=True):
            checked += 1
            self.progress = {"source": post.name, "message": msg.id,
                                     "checked": checked, "downloaded": downloaded,
                                     "percent": 0, "file": ""}
            self.report_progress(self.progress.copy())
            if getattr(msg, "video_note", None) is not None:
                identity = await self._message_identity(msg)
                if identity["author_id"] is not None:
                    author_id = int(identity["author_id"])
                    self.store.telegram.observe_profile(
                        author_id=author_id,
                        username=identity["author_username"],
                        display_name=identity["author_name"],
                        observed_at=iso_date(getattr(msg, "date", None)),
                        source_id=int(source["id"]),
                    )
                    if self.store.telegram.profile_photo_due(author_id):
                        self.store.enqueue(
                            "telegram.profile_photos",
                            {"author_id": author_id},
                            unique_key=f"telegram-profile-photos:{author_id}",
                        )
                if not self.store.seen(source["id"], msg.id):
                    video = await self.download_one(client, post, source, msg, identity)
                    self.store.checkpoint(source["id"], msg.id, video)
                    downloaded += 1
                    self.store.event("ok", f"Сохранён кружок {post.name} / {msg.id}")
                else:
                    self.store.checkpoint(source["id"], msg.id)
            else:
                self.store.checkpoint(source["id"], msg.id)
        self.progress = {}
        self.report_progress({})
        return checked, downloaded

    async def download_one(
        self,
        client,
        post: Post,
        source: dict,
        msg,
        identity: dict[str, Any] | None = None,
    ) -> dict:
        identity = identity or await self._message_identity(msg)
        ident = identity["author_id"]
        name = identity["author_name"]
        username = identity["author_username"]
        output = video_note_path(
            self.store.telegram.archive_root(),
            int(source["id"]),
            ident,
            int(msg.id),
        )
        ensure_private(output.parent)
        partial = output.with_suffix(".mp4.part")
        video = {
            "source_id": source["id"], "message_id": msg.id,
            "author_id": ident, "author_name": name, "author_username": username,
            "date": iso_date(msg.date), "media_url": post_comment_link(post, msg.id),
            "file_path": str(output), "size": 0,
        }
        old_size = self.store.recorded_download_size(source["id"], msg.id)
        needs_download = (not output.is_file() or output.stat().st_size == 0 or
                          (old_size is not None and output.stat().st_size != old_size))
        if needs_download:
            partial.unlink(missing_ok=True)
            try:
                with partial.open("wb") as f:
                    last_at = time.monotonic()
                    last_size = 0
                    last_speed = None
                    def callback(current: int, total: int) -> None:
                        nonlocal last_at, last_size, last_speed
                        now = time.monotonic()
                        elapsed = now - last_at
                        if elapsed >= 0.5:
                            last_speed = max(0.0, (current - last_size) / elapsed)
                            last_at, last_size = now, current
                        self.progress.update({
                            "speed_bps": last_speed,
                            "file": output.name,
                            "bytes": current, "total": total,
                            "percent": round(100 * current / total) if total else 0,
                        })
                        self.report_progress(self.progress.copy())
                    # File object avoids Telethon appending a file extension to .part.
                    result = await client.download_media(msg, file=f, progress_callback=callback)
                    f.flush()
                    os.fsync(f.fileno())
                if result is None or not partial.exists() or partial.stat().st_size == 0:
                    raise OSError("Telegram не вернул медиаданные")
                os.replace(partial, output)
            except BaseException:
                partial.unlink(missing_ok=True)
                raise
        video["size"] = output.stat().st_size
        # The sidecar remains a portable record alongside every video note.
        metadata = {
            "post_url": post.link, "comment_url": video["media_url"],
            "comment_id": msg.id, "date": video["date"],
            "author": {"id": ident, "username": username, "name": name},
            "file": output.name, "size": video["size"],
        }
        sidecar = output.with_suffix(".json")
        tmp = output.with_suffix(".json.tmp")
        try:
            with tmp.open("w", encoding="utf-8") as f:
                json.dump(metadata, f, ensure_ascii=False, indent=2)
                f.flush()
                os.fsync(f.fileno())
            os.replace(tmp, sidecar)
        finally:
            tmp.unlink(missing_ok=True)
        return video

    async def enrich_source(self, source: dict) -> str:
        """Fetch optional post metadata without being required by report generation."""
        client = await self.connect()
        if not await client.is_user_authorized():
            raise RuntimeError("Telegram не подключён")
        entity = await client.get_entity(str(source["username"]))
        original = await client.get_messages(entity, ids=int(source["post_id"]))
        if original is None:
            self.store.telegram.record_post_metadata(
                int(source["id"]),
                channel_title=str(source.get("title") or source["username"]),
                post_text="",
                published_at="",
                status="unavailable",
                error="Пост недоступен",
            )
            return "Пост недоступен; сохранённые данные оставлены без изменений"
        self._record_post(source, entity, original)
        return f"Метаданные поста {source['username']}/{source['post_id']} обновлены"

    async def download_post_media(self, source: dict) -> str:
        client = await self.connect()
        if not await client.is_user_authorized():
            raise RuntimeError("Telegram не подключён")
        entity = await client.get_entity(str(source["username"]))
        original = await client.get_messages(entity, ids=int(source["post_id"]))
        if original is None:
            return "Медиа поста недоступно"
        self._record_post(source, entity, original)
        descriptor = self._post_media_descriptor(original)
        if descriptor is None:
            return "У поста нет доступного медиа"
        media_id, kind, suffix = descriptor
        current = self.store.telegram.post_media(int(source["id"]), media_id)
        if current is not None:
            current_path = Path(str(current["file_path"]))
            try:
                if (
                    current_path.is_file()
                    and int(current["size"]) > 0
                    and current_path.stat().st_size == int(current["size"])
                ):
                    return "Медиа поста уже сохранено"
            except (OSError, TypeError, ValueError):
                pass
        output = post_media_path(
            self.store.telegram.archive_root(), int(source["id"]), media_id, suffix
        )
        ensure_private(output.parent)
        partial = output.with_name(f"{output.name}.part")
        partial.unlink(missing_ok=True)
        try:
            with partial.open("wb") as stream:
                result = await client.download_media(original, file=stream)
                stream.flush()
                os.fsync(stream.fileno())
            if result is None or not partial.is_file() or partial.stat().st_size == 0:
                raise OSError("Telegram не вернул медиаданные поста")
            os.replace(partial, output)
        except BaseException as exc:
            partial.unlink(missing_ok=True)
            self.store.telegram.record_post_media(
                int(source["id"]),
                media_id,
                kind=kind,
                file_path=str(output),
                size=0,
                status="failed",
                error=str(exc),
            )
            raise
        self.store.telegram.record_post_media(
            int(source["id"]),
            media_id,
            kind=kind,
            file_path=str(output),
            size=output.stat().st_size,
        )
        return f"Медиа поста сохранено: {output.name}"

    async def refresh_profile_photos(self, author_id: int) -> str:
        """Save at most 30 visible photos; privacy and limits are final authority."""
        if not self.store.telegram.profile_photo_due(author_id):
            return "Фотографии профиля недавно проверялись"
        try:
            client = await self.connect()
            if not await client.is_user_authorized():
                raise RuntimeError("Telegram не подключён")
            entity = await client.get_entity(int(author_id))
            identity = self._sender_identity(entity, int(author_id))
            self.store.telegram.observe_profile(
                author_id=int(author_id),
                username=identity["author_username"],
                display_name=identity["author_name"],
                observed_at=utc_now(),
            )
            saved = 0
            async for photo in client.iter_profile_photos(entity, limit=30):
                photo_id = str(getattr(photo, "id", ""))
                if not photo_id:
                    continue
                existing = self.store.telegram.profile_photo(author_id, photo_id)
                if existing is not None:
                    path = Path(str(existing["file_path"]))
                    try:
                        if (
                            path.is_file()
                            and int(existing["size"]) > 0
                            and path.stat().st_size == int(existing["size"])
                        ):
                            continue
                    except (OSError, TypeError, ValueError):
                        pass
                output = profile_photo_path(
                    self.store.telegram.archive_root(), int(author_id), photo_id
                )
                ensure_private(output.parent)
                partial = output.with_name(f"{output.name}.part")
                partial.unlink(missing_ok=True)
                try:
                    with partial.open("wb") as stream:
                        result = await client.download_media(photo, file=stream)
                        stream.flush()
                        os.fsync(stream.fileno())
                    if result is None or not partial.is_file() or partial.stat().st_size == 0:
                        raise OSError("Telegram не вернул фотографию профиля")
                    os.replace(partial, output)
                except BaseException:
                    partial.unlink(missing_ok=True)
                    raise
                self.store.telegram.record_profile_photo(
                    int(author_id),
                    photo_id,
                    file_path=str(output),
                    size=output.stat().st_size,
                    photo_date=iso_date(getattr(photo, "date", None)),
                )
                saved += 1
        except Exception as exc:
            retry = 6 * 60 * 60
            try:
                from telethon.errors import FloodWaitError

                if isinstance(exc, FloodWaitError):
                    retry = max(60, int(exc.seconds) + 3)
                    message = f"Telegram ограничил фотографии; повтор через {exc.seconds} с."
                else:
                    message = "Фотографии профиля недоступны или скрыты настройками приватности"
            except ImportError:
                message = "Фотографии профиля сейчас недоступны"
            self.store.telegram.finish_profile_photo_check(
                int(author_id), error=message, retry_after=retry
            )
            return message
        self.store.telegram.finish_profile_photo_check(int(author_id))
        return f"Фотографии профиля обновлены; новых: {saved}"
