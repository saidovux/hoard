"""Cross-platform pytest configuration."""

import asyncio
import sys

import pytest

if sys.platform == "win32" and sys.version_info < (3, 12):

    class ResponsiveWindowsSelectorEventLoopPolicy(
        asyncio.WindowsSelectorEventLoopPolicy
    ):
        """Bound selector sleeps in pytest's short-lived Python 3.11 loops."""

        def new_event_loop(self):
            loop = super().new_event_loop()

            def wake_selector() -> None:
                if not loop.is_closed():
                    loop.call_later(0.01, wake_selector)

            loop.call_soon(wake_selector)
            return loop

    @pytest.fixture(scope="session", autouse=True)
    def event_loop_policy():
        """Prevent Python 3.11 Proactor stalls between async test modules."""
        return ResponsiveWindowsSelectorEventLoopPolicy()
