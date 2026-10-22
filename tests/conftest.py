"""Cross-platform pytest configuration."""

import asyncio
import sys

import pytest

if sys.platform == "win32" and sys.version_info < (3, 12):

    @pytest.fixture(scope="session", autouse=True)
    def event_loop_policy():
        """Prevent Python 3.11 Proactor stalls between async test modules."""
        return asyncio.WindowsSelectorEventLoopPolicy()
