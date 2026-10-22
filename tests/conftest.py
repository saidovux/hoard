"""Cross-platform pytest configuration."""

import asyncio
import inspect
import sys
import threading

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

        loop = asyncio.get_running_loop()
        stopped = threading.Event()

        def wake_loop() -> None:
            while not stopped.wait(0.01):
                try:
                    loop.call_soon_threadsafe(lambda: None)
                except RuntimeError:
                    return

        worker = threading.Thread(
            target=wake_loop,
            name="pytest-win311-loop-wakeup",
            daemon=True,
        )
        worker.start()
        try:
            yield
        finally:
            stopped.set()
            worker.join(timeout=1)
