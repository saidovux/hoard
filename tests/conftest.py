"""Cross-platform pytest configuration."""

import asyncio
import contextlib
import sys

import pytest

if sys.platform == "win32" and sys.version_info < (3, 12):

    @pytest.fixture(scope="session", autouse=True)
    def event_loop_policy():
        """Prevent Python 3.11 Proactor stalls between async test modules."""
        return asyncio.WindowsSelectorEventLoopPolicy()


if sys.version_info < (3, 12):

    @pytest.fixture(autouse=True)
    async def keep_python311_async_tests_responsive():
        """Keep Python 3.11 test loops from sleeping with pending async work."""

        async def heartbeat() -> None:
            while True:
                await asyncio.sleep(0)

        worker = asyncio.create_task(heartbeat(), name="pytest-py311-loop-heartbeat")
        try:
            yield
        finally:
            worker.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await worker
