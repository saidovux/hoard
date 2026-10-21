import asyncio
import os
import subprocess
import sys
import tempfile
from pathlib import Path

from hoard.ai import AIMessage, AIRequest
from hoard.ai.executor import AICore
from hoard.ai.models import AIResponse
from hoard.ai.provider import AIProvider
from hoard.service import Service
from hoard.storage import Store
from hoard.telegram import AuthState


async def wait_for(predicate, seconds=3):
    async with asyncio.timeout(seconds):
        while not predicate():
            await asyncio.sleep(0.02)


class FakeEngine:
    authorization_step = AuthState.DISCONNECTED

    def __init__(self):
        self.disconnects = 0

    async def disconnect(self):
        self.disconnects += 1


class BlockingProvider(AIProvider):
    name = "deepseek"

    def __init__(self):
        self.started = asyncio.Event()
        self.release = asyncio.Event()
        self.calls = 0

    @property
    def configured(self):
        return True

    async def complete(self, request):
        self.calls += 1
        self.started.set()
        await self.release.wait()
        return AIResponse(
            provider=self.name,
            model="deepseek-flash",
            content="AI complete",
            finish_reason="stop",
        )


def job_state(store, jid):
    return store.db.execute("SELECT state FROM jobs WHERE id=?", (jid,)).fetchone()[0]


async def ai_pool_scenario(tmp_path: Path) -> None:
    store = Store(tmp_path / "db.sqlite")
    engine = FakeEngine()
    provider = BlockingProvider()
    service = Service(
        store=store,
        engine=engine,
        ai_core=AICore({"deepseek": provider}),
    )
    jid = service.enqueue_ai(AIRequest((AIMessage("user", "test"),)))
    unrelated = store.enqueue("telegram.unknown", {})

    try:
        await service.start_background()
        await asyncio.wait_for(provider.started.wait(), 1)
        assert service.running is False
        assert service.ai_running is True

        # A long AI request owns neither the Telegram lock nor its lifecycle.
        await asyncio.wait_for(service.start(), 1)
        await asyncio.wait_for(service.stop(), 1)
        assert service.running is False
        assert provider.calls == 1

        provider.release.set()
        await wait_for(lambda: job_state(store, jid) == "done")
        assert job_state(store, unrelated) == "queued"
    finally:
        await service.stop_background()
        store.close()


async def cancelled_paid_job_scenario(tmp_path: Path) -> None:
    store = Store(tmp_path / "db.sqlite")
    provider = BlockingProvider()
    service = Service(
        store=store,
        engine=FakeEngine(),
        ai_core=AICore({"deepseek": provider}),
    )
    jid = service.enqueue_ai(AIRequest((AIMessage("user", "test"),)))

    try:
        await service.start_background()
        await asyncio.wait_for(provider.started.wait(), 1)
        await service.stop_background()

        assert job_state(store, jid) == "failed"
        await service.start_background()
        await asyncio.sleep(0.1)
        assert provider.calls == 1
        assert job_state(store, jid) == "failed"
    finally:
        await service.stop_background()
        store.close()


WIN311 = sys.platform == "win32" and sys.version_info < (3, 12)


def run_isolated(scenario: str) -> None:
    completed = subprocess.run(
        [sys.executable, str(Path(__file__).resolve()), scenario],
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )
    assert completed.returncode == 0, completed.stdout + completed.stderr


if WIN311:

    def test_ai_pool_runs_while_telegram_is_stopped_and_does_not_block_it():
        run_isolated("pool")

    def test_cancelled_paid_job_requires_explicit_retry():
        run_isolated("cancel")

else:

    async def test_ai_pool_runs_while_telegram_is_stopped_and_does_not_block_it(
        monkeypatch, tmp_path
    ):
        monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "config"))
        await ai_pool_scenario(tmp_path)

    async def test_cancelled_paid_job_requires_explicit_retry(monkeypatch, tmp_path):
        monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "config"))
        await cancelled_paid_job_scenario(tmp_path)


async def isolated_main(scenario: str) -> None:
    with tempfile.TemporaryDirectory(prefix="hoard-ai-jobs-") as directory:
        tmp_path = Path(directory)
        os.environ["XDG_CONFIG_HOME"] = str(tmp_path / "config")
        if scenario == "pool":
            await ai_pool_scenario(tmp_path)
        elif scenario == "cancel":
            await cancelled_paid_job_scenario(tmp_path)
        else:
            raise ValueError(f"Unknown scenario: {scenario}")


if __name__ == "__main__":
    if WIN311:
        asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())
    asyncio.run(isolated_main(sys.argv[1]))
