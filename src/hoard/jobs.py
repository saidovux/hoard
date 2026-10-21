"""Small local durable job runner. Handlers are independent of Telegram and TUI."""
from __future__ import annotations

import asyncio
import contextlib
import json
from collections.abc import Awaitable, Callable

Handler = Callable[[dict], Awaitable[str]]


class JobRunner:
    def __init__(
        self,
        store,
        handlers: dict[str, Handler],
        *,
        parallel: int = 3,
        retry_interrupted: bool = True,
        name: str = "jobs",
    ):
        if not 1 <= parallel <= 100:
            raise ValueError("Допустимо 1–100 параллельных задач; по умолчанию 3")
        self.store = store
        self.handlers = handlers
        self.parallel = parallel
        self.retry_interrupted = retry_interrupted
        self.name = name
        self._loop: asyncio.Task | None = None
        self._active: dict[int, asyncio.Task] = {}
        self._wakeup = asyncio.Event()

    @property
    def running(self) -> bool:
        return self._loop is not None and not self._loop.done()

    def wakeup(self) -> None:
        self._wakeup.set()

    async def start(self) -> None:
        if self.running:
            return
        self.store.recover_interrupted_jobs(
            self.handlers.keys(),
            retry=self.retry_interrupted,
        )
        self._loop = asyncio.create_task(self._run(), name=f"hoard-{self.name}-runner")

    async def stop(self) -> None:
        if self._loop is not None:
            self._loop.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._loop
            self._loop = None

    async def _run_one(self, job: dict) -> None:
        jid = job["id"]
        kind = job["kind"]
        try:
            handler = self.handlers[kind]
            result = await handler(json.loads(job["payload"]))
        except asyncio.CancelledError:
            if self.retry_interrupted:
                self.store.finish_job(jid, "queued", "Прервано; продолжится после запуска")
            else:
                self.store.finish_job(jid, "failed", "Прервано; нужен явный повтор")
            raise
        except Exception as exc:
            self.store.finish_job(jid, "failed", f"{type(exc).__name__}: {exc}"[:500])
        else:
            self.store.finish_job(jid, "done", result[:500])
        finally:
            self._active.pop(jid, None)
            self._wakeup.set()

    def _enqueue_due_jobs(self) -> None:
        # Keep schedulers small and synchronous; all work remains an ordinary job.
        if "telegram.scan" in self.handlers:
            for source in self.store.due_sources():
                self.store.enqueue(
                    "telegram.scan",
                    {"source_id": source["id"]},
                    unique_key=f"telegram:{source['id']}",
                )
        # Profiles discovered before this version are backfilled from their stable
        # Telegram IDs. New profiles are also enqueued by the scan; this provides
        # durable retries without rewinding a source.
        if "telegram.profile_photos" in self.handlers:
            for author_id in self.store.telegram.due_profile_photo_ids():
                self.store.enqueue(
                    "telegram.profile_photos",
                    {"author_id": author_id},
                    unique_key=f"telegram-profile-photos:{author_id}",
                )

    async def _run(self) -> None:
        try:
            while True:
                self._enqueue_due_jobs()
                slots = max(0, self.parallel - len(self._active))
                for job in self.store.queued_jobs(
                    limit=slots,
                    kinds=self.handlers.keys(),
                ):
                    if not self.store.mark_running(job["id"]):
                        continue
                    self._active[job["id"]] = asyncio.create_task(self._run_one(job))
                self._wakeup.clear()
                try:
                    await asyncio.wait_for(self._wakeup.wait(), timeout=2)
                except TimeoutError:
                    pass
        finally:
            for task in self._active.values():
                task.cancel()
            if self._active:
                await asyncio.gather(*self._active.values(), return_exceptions=True)
            self._active.clear()
