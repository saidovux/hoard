"""Small local durable job runner. Handlers are independent of Telegram and TUI."""
from __future__ import annotations

import asyncio
import contextlib
import json
from collections.abc import Awaitable, Callable

Handler = Callable[[dict], Awaitable[str]]


class JobRunner:
    def __init__(self, store, handlers: dict[str, Handler], *, parallel: int = 3):
        if not 1 <= parallel <= 100:
            raise ValueError("Допустимо 1–100 параллельных задач; по умолчанию 3")
        self.store = store
        self.handlers = handlers
        self.parallel = parallel
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
        self.store.recover_interrupted_jobs()
        self._loop = asyncio.create_task(self._run(), name="hoard-job-runner")

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
            self.store.finish_job(jid, "queued", "Прервано; продолжится после запуска")
            raise
        except Exception as exc:
            self.store.finish_job(jid, "failed", f"{type(exc).__name__}: {exc}"[:500])
        else:
            self.store.finish_job(jid, "done", result[:500])
        finally:
            self._active.pop(jid, None)
            self._wakeup.set()

    async def _run(self) -> None:
        try:
            while True:
                # Keep the Telegram scheduler simple; sources become ordinary jobs.
                for source in self.store.due_sources():
                    self.store.enqueue("telegram.scan", {"source_id": source["id"]},
                                       unique_key=f"telegram:{source['id']}")
                slots = max(0, self.parallel - len(self._active))
                for job in self.store.queued_jobs(limit=slots):
                    if job["kind"] not in self.handlers:
                        self.store.finish_job(
                            job["id"], "failed", "Нет обработчика для типа задачи"
                        )
                        continue
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
