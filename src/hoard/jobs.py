"""Small local durable job runner. Handlers are independent of Telegram and TUI."""
from __future__ import annotations

import asyncio
import json
from collections.abc import Awaitable, Callable, Collection

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
        include_job_id: bool = False,
        redact_errors: bool = False,
        scheduler: Callable[[], None] | None = None,
        allowed_job_ids: Callable[[], Collection[int] | None] | None = None,
    ):
        if not 1 <= parallel <= 100:
            raise ValueError("Допустимо 1–100 параллельных задач; по умолчанию 3")
        self.store = store
        self.handlers = handlers
        self.parallel = parallel
        self.retry_interrupted = retry_interrupted
        self.name = name
        self.include_job_id = include_job_id
        self.redact_errors = redact_errors
        self.scheduler = scheduler
        self.allowed_job_ids = allowed_job_ids
        self._loop: asyncio.Task | None = None
        self._active: dict[int, asyncio.Task] = {}
        self._wakeup = asyncio.Event()
        self._lifecycle_lock = asyncio.Lock()
        self._scheduler_error_delay = 1.0
        self._scheduler_error_delay_max = 30.0
        self._idle_timeout = 2.0

    @property
    def running(self) -> bool:
        return self._loop is not None and not self._loop.done()

    def wakeup(self) -> None:
        self._wakeup.set()

    async def start(self) -> None:
        async with self._lifecycle_lock:
            if self.running:
                return
            self.store.recover_interrupted_jobs(
                self.handlers.keys(),
                retry=self.retry_interrupted,
            )
            self._loop = asyncio.create_task(
                self._run(), name=f"hoard-{self.name}-runner"
            )

    async def stop(self) -> None:
        # Two concurrent pause/shutdown requests must not cancel the runner
        # loop twice: a second cancel can interrupt its cleanup and destroy the
        # active-task bookkeeping.  The first stop owns the transition.
        async with self._lifecycle_lock:
            loop = self._loop
            if loop is None:
                return
            self._loop = None
            if not loop.done():
                loop.cancel()
            result = await asyncio.gather(loop, return_exceptions=True)
            error = result[0] if result else None
            if isinstance(error, Exception) and not isinstance(
                error, asyncio.CancelledError
            ):
                self._record_error("runner", error)

    def _record_error(self, scope: str, exc: BaseException) -> None:
        try:
            self.store.event(
                "error", f"{self.name}: {scope}: {type(exc).__name__}: {exc}"
            )
        except Exception:
            # Diagnostics must never break the runner or shutdown.
            pass

    async def _run_one(self, job: dict) -> None:
        jid = job["id"]
        kind = job["kind"]
        try:
            handler = self.handlers[kind]
            payload = json.loads(job["payload"])
            if self.include_job_id:
                payload = dict(payload)
                payload["_job_id"] = jid
            result = await handler(payload)
        except asyncio.CancelledError:
            if self.retry_interrupted:
                self.store.finish_job(jid, "queued", "Прервано; продолжится после запуска")
            else:
                self.store.finish_job(jid, "failed", "Прервано; нужен явный повтор")
            raise
        except Exception as exc:
            result = (
                "AI-задание завершилось ошибкой; подробности доступны в журнале попыток"
                if self.redact_errors
                else f"{type(exc).__name__}: {exc}"[:500]
            )
            self.store.finish_job(jid, "failed", result)
        else:
            self.store.finish_job(jid, "done", result[:500])
        finally:
            self._active.pop(jid, None)
            self._wakeup.set()

    def _enqueue_due_jobs(self) -> None:
        # Keep schedulers small and synchronous; all work remains an ordinary job.
        if (
            "telegram.report" in self.handlers
            and self.store.telegram.catalog_dirty()
            and self.store.telegram.catalog_report_due()
        ):
            self.store.enqueue(
                "telegram.report", {}, unique_key="telegram-report"
            )
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
        if self.scheduler is not None:
            self.scheduler()

    async def _run(self) -> None:
        try:
            while True:
                slots = max(0, self.parallel - len(self._active))
                try:
                    self._enqueue_due_jobs()
                    jobs = self.store.queued_jobs(
                        # One deferred report must not hide otherwise runnable work.
                        limit=slots + int("telegram.report" in self.handlers),
                        kinds=self.handlers.keys(),
                        job_ids=(
                            self.allowed_job_ids()
                            if self.allowed_job_ids is not None
                            else None
                        ),
                    )
                except Exception as exc:
                    # A temporary SQLite/filesystem error must not silently kill
                    # the only loop that drains durable queued work.  Pause with
                    # bounded backoff, keep active jobs running, and try again.
                    self._record_error("scheduler", exc)
                    await asyncio.sleep(self._scheduler_error_delay)
                    self._scheduler_error_delay = min(
                        self._scheduler_error_delay * 2,
                        self._scheduler_error_delay_max,
                    )
                    continue
                self._scheduler_error_delay = 1.0

                for job in jobs:
                    if slots <= 0:
                        break
                    try:
                        if (
                            job["kind"] == "telegram.report"
                            and not self.store.telegram.catalog_report_due()
                        ):
                            continue
                    except Exception as exc:
                        self._record_error("scheduler", exc)
                        break
                    try:
                        marked = self.store.mark_running(job["id"])
                    except Exception as exc:
                        self._record_error("mark_running", exc)
                        break
                    if not marked:
                        continue
                    self._active[job["id"]] = asyncio.create_task(self._run_one(job))
                    slots -= 1
                self._wakeup.clear()
                try:
                    # ``wait_for(Event.wait())`` creates a helper task whose
                    # cancellation can stall on Python 3.11's Windows loops.
                    # Timeout the runner task itself so shutdown cancellation
                    # remains direct and deterministic.
                    async with asyncio.timeout(self._idle_timeout):
                        await self._wakeup.wait()
                except TimeoutError:
                    pass
        finally:
            tasks = list(self._active.values())
            for task in tasks:
                task.cancel()
            if tasks:
                try:
                    await asyncio.gather(*tasks, return_exceptions=True)
                finally:
                    self._active.clear()
