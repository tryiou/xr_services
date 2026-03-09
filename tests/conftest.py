"""
Conftest - shared fixtures for all tests.
"""

import asyncio
import sys
from pathlib import Path
from unittest.mock import MagicMock

import pytest

# Add repo root to sys.path to allow importing local modules
repo_root = Path(__file__).parent.parent
if str(repo_root) not in sys.path:
    sys.path.insert(0, str(repo_root))

from cg_proxy_xrs import LRUCache, shutdown_flag  # noqa: E402


@pytest.fixture
def event_loop():
    """Create an asyncio event loop for async tests."""
    loop = asyncio.new_event_loop()
    yield loop
    loop.close()


@pytest.fixture
def lru_cache():
    """Fresh LRUCache instance for each test."""
    return LRUCache(maxsize=10, ttl=1)


@pytest.fixture
def mock_session():
    """Mock aiohttp ClientSession for testing."""
    session = MagicMock()
    return session


@pytest.fixture(autouse=True)
def reset_shutdown_flag():
    """Reset the global shutdown flag before each test."""
    shutdown_flag.clear()
    yield
    shutdown_flag.clear()


@pytest.fixture
def fast_sleep(monkeypatch):
    """Patch sleep_interruptible to be instant for faster tests."""

    async def instant_sleep(duration):
        pass

    monkeypatch.setattr("cg_proxy_xrs.sleep_interruptible", instant_sleep)
    return instant_sleep
