"""Cross-platform pytest configuration."""

import asyncio
import contextlib
import inspect
import sys

import pytest

if sys.platform == "win32" and sys.version_info < (3, 12):

    @pytest.fixture(scope="session", autouse=True)
    def event_loop_policy():
        """Prevent Python 3.11 Proactor stalls between async test modules."""
        return asyncio.WindowsSelectorEventLoopPolicy()

    @pytest.fixture(autouse=True)
    async def keep_windows_async_tests_responsive(request):
        """Wake the Windows 3.11 test loop while an async test is active."""
        if not inspect.iscoroutinefunction(getattr(request.node, "obj", None)):
            yield
            return

        async def heartbeat() -> None:
            while True:
                await asyncio.sleep(0)

        worker = asyncio.create_task(heartbeat(), name="pytest-win311-loop-heartbeat")
        try:
            yield
        finally:
            worker.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await worker
