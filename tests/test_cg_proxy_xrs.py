"""
Tests for cg_proxy_xrs.py - Core service functionality.

Focused on:
- LRUCache correctness (concurrency, TTL, eviction)
- Coin chunking logic (boundary conditions)
- API handlers (happy path, error cases)
- Fetch retry logic (network resilience)
"""

import asyncio
import time
from unittest.mock import AsyncMock, MagicMock

import pytest

from cg_proxy_xrs import (
    LRUCache,
    _build_coin_chunks,
    cg_coins_data_handler,
    cg_coins_list_handler,
    coins_list_cache,
    coins_list_lock,
    fetch_json,
    price_cache,
)

# === LRUCache Tests ===


@pytest.mark.asyncio
async def test_lru_cache_basic_set_get():
    """Test basic set and get operations."""
    cache = LRUCache(maxsize=3, ttl=3600)
    await cache.set("key1", "value1")
    assert await cache.get("key1") == "value1"
    assert await cache.get("nonexistent") is None


@pytest.mark.asyncio
async def test_lru_cache_ttl_expiry():
    """Test that entries expire after TTL."""
    cache = LRUCache(maxsize=10, ttl=1)  # 1 second TTL
    await cache.set("key1", "value1")
    assert await cache.get("key1") == "value1"
    time.sleep(1.1)  # Wait for expiry
    assert await cache.get("key1") is None


@pytest.mark.asyncio
async def test_lru_cache_size_limit_eviction():
    """Test LRU eviction when cache exceeds maxsize."""
    cache = LRUCache(maxsize=3, ttl=3600)
    await cache.set("a", 1)
    await cache.set("b", 2)
    await cache.set("c", 3)
    await cache.set("d", 4)  # Should evict 'a' (least recently used)
    assert await cache.get("a") is None
    assert await cache.get("d") == 4
    # Check size is capped
    assert len(cache) <= 3


@pytest.mark.asyncio
async def test_lru_cache_lru_ordering():
    """Test that accessed items move to end (most recently used)."""
    cache = LRUCache(maxsize=3, ttl=3600)
    await cache.set("a", 1)
    await cache.set("b", 2)
    await cache.set("c", 3)
    # Access 'a' to make it recently used
    await cache.get("a")
    # Add 'd' - should evict 'b', not 'a'
    await cache.set("d", 4)
    assert await cache.get("b") is None
    assert await cache.get("a") == 1
    assert await cache.get("d") == 4


@pytest.mark.asyncio
async def test_lru_cache_concurrent_access():
    """Test that concurrent operations don't corrupt cache."""
    cache = LRUCache(maxsize=100, ttl=3600)

    async def writer(prefix: str, count: int):
        for i in range(count):
            await cache.set(f"{prefix}_{i}", f"value_{i}")

    async def reader(prefix: str, count: int):
        for i in range(count):
            await cache.get(f"{prefix}_{i}")

    # Run multiple writers and readers concurrently
    await asyncio.gather(
        writer("w1", 50),
        writer("w2", 50),
        reader("w1", 50),
        reader("w2", 50),
    )
    # Verify some data is retrievable (no crashes, no corruption)
    assert await cache.get("w1_0") == "value_0"
    assert len(cache) > 0


# === Coin Chunking Tests ===


def test_build_coin_chunks_normal():
    """Test chunking with many coins splits into multiple chunks."""
    base_url = "https://api.coingecko.com/api/v3/simple/price?ids={ids}&vs_currencies=usd"
    # 100 coin IDs, should create multiple chunks
    coin_ids = [f"coin{i}" for i in range(100)]
    chunks = _build_coin_chunks(coin_ids, base_url, 200)  # small limit for test
    assert len(chunks) > 1
    # All coins should appear exactly once
    all_in_chunks = [cid for chunk in chunks for cid in chunk]
    assert len(all_in_chunks) == len(coin_ids)
    assert set(all_in_chunks) == set(coin_ids)


def test_build_coin_chunks_empty():
    """Test chunking with empty list returns empty result."""
    chunks = _build_coin_chunks([], "url", 1000)
    assert chunks == []


def test_build_coin_chunks_oversized_single_id():
    """Test that a single ID exceeding URL limit is skipped."""
    base_url = "https://api.coingecko.com/api/v3/simple/price?ids={ids}&vs_currencies=usd"
    # Normal IDs
    coin_ids = ["bitcoin", "ethereum"]
    # Add an extremely long ID that will exceed even by itself
    long_id = "x" * 1000
    coin_ids.append(long_id)
    chunks = _build_coin_chunks(coin_ids, base_url, 100)  # very small limit
    # The oversized ID should be skipped, not cause infinite loop
    all_in_chunks = [cid for chunk in chunks for cid in chunk]
    assert long_id not in all_in_chunks
    assert "bitcoin" in all_in_chunks or "ethereum" in all_in_chunks


# === API Handler Tests ===


@pytest.mark.asyncio
async def test_cg_coins_data_handler_empty_params():
    """Test handler with no coin IDs returns error."""
    result = await cg_coins_data_handler([])
    assert result["success"] is False
    assert "No coin IDs provided" in result["reply"]


@pytest.mark.asyncio
async def test_cg_coins_data_handler_cache_hits():
    """Test handler returns cached data when available."""
    # Pre-populate cache
    await price_cache.set("bitcoin", {"usd": 50000.0, "timestamp": time.time()})
    result = await cg_coins_data_handler(["bitcoin"])
    assert result["success"] is True
    assert result["reply"]["bitcoin"]["usd"] == 50000.0


@pytest.mark.asyncio
async def test_cg_coins_data_handler_cache_misses():
    """Test handler returns 404 errors for missing coins."""
    result = await cg_coins_data_handler(["unknown_coin"])
    assert result["success"] is False  # All misses -> success=False
    assert "unknown_coin" in result["reply"]
    assert result["reply"]["unknown_coin"]["code"] == 404


@pytest.mark.asyncio
async def test_cg_coins_data_handler_mixed_hits_misses():
    """Test handler with mix of cached and missing coins."""
    await price_cache.set("bitcoin", {"usd": 50000.0, "timestamp": time.time()})
    result = await cg_coins_data_handler(["bitcoin", "ethereum", "unknown"])
    assert result["success"] is True  # At least one hit -> success=True
    assert result["reply"]["bitcoin"]["usd"] == 50000.0
    assert result["reply"]["ethereum"]["code"] == 404
    assert result["reply"]["unknown"]["code"] == 404


@pytest.mark.asyncio
async def test_cg_coins_data_handler_case_insensitive():
    """Test that coin IDs are lowercased before cache lookup."""
    await price_cache.set("bitcoin", {"usd": 50000.0, "timestamp": time.time()})
    result = await cg_coins_data_handler(["BITCOIN", "Bitcoin"])
    # Both should hit the same cache entry
    assert result["success"] is True
    assert result["reply"]["BITCOIN"]["usd"] == 50000.0
    assert result["reply"]["Bitcoin"]["usd"] == 50000.0


@pytest.mark.asyncio
async def test_cg_coins_list_handler_returns_data():
    """Test coins list handler returns cached data."""
    test_data = [{"id": "bitcoin", "symbol": "btc", "name": "Bitcoin"}]
    async with coins_list_lock:
        coins_list_cache["data"] = test_data
        coins_list_cache["timestamp"] = time.time()

    result = await cg_coins_list_handler()
    assert result["success"] is True
    assert result["reply"]["data"] == test_data
    assert "timestamp" in result["reply"]


@pytest.mark.asyncio
async def test_cg_coins_list_handler_no_data_yet():
    """Test coins list handler returns error when not ready."""
    async with coins_list_lock:
        coins_list_cache["data"] = None
        coins_list_cache["timestamp"] = 0

    result = await cg_coins_list_handler()
    assert result["success"] is False
    assert "not available yet" in result["reply"]


# === Fetch Retry Tests ===


@pytest.mark.asyncio
async def test_fetch_json_success(mock_session):
    """Test fetch_json returns JSON on successful response."""
    mock_resp = AsyncMock()
    mock_resp.status = 200
    mock_resp.json = AsyncMock(return_value={"data": "test"})
    mock_session.get.return_value.__aenter__ = AsyncMock(return_value=mock_resp)
    mock_session.get.return_value.__aexit__ = AsyncMock(return_value=False)

    result = await fetch_json(mock_session, "http://test.url")
    assert result == {"data": "test"}
    assert mock_session.get.call_count == 1


@pytest.mark.asyncio
@pytest.mark.usefixtures("fast_sleep")
async def test_fetch_json_retry_on_timeout(mock_session):
    """Test fetch_json retries on timeout."""
    # First two calls raise timeout, third succeeds
    call_count = 0

    def make_resp():
        nonlocal call_count
        call_count += 1
        if call_count <= 2:
            raise asyncio.TimeoutError()
        resp = AsyncMock()
        resp.status = 200
        resp.json = AsyncMock(return_value={"ok": True})
        return resp

    mock_session.get.return_value = MagicMock()
    mock_session.get.return_value.__aenter__ = AsyncMock(side_effect=make_resp)
    mock_session.get.return_value.__aexit__ = AsyncMock(return_value=False)

    result = await fetch_json(mock_session, "http://test.url")
    assert result == {"ok": True}
    assert call_count == 3


@pytest.mark.asyncio
@pytest.mark.usefixtures("fast_sleep")
async def test_fetch_json_max_retries_exceeded(mock_session):
    """Test fetch_json gives up after 5 failed attempts."""
    # Always timeout
    mock_session.get.side_effect = asyncio.TimeoutError

    result = await fetch_json(mock_session, "http://test.url")
    assert result is None
    assert mock_session.get.call_count == 5


@pytest.mark.asyncio
@pytest.mark.usefixtures("fast_sleep")
async def test_fetch_json_handles_http_errors(mock_session):
    """Test fetch_json retries on HTTP errors (not 200)."""
    # First returns 500, second returns 200
    call_count = 0

    def make_resp():
        nonlocal call_count
        call_count += 1
        resp = AsyncMock()
        if call_count == 1:
            resp.status = 500
        else:
            resp.status = 200
            resp.json = AsyncMock(return_value={"recovered": True})
        return resp

    mock_session.get.return_value = MagicMock()
    mock_session.get.return_value.__aenter__ = AsyncMock(side_effect=make_resp)
    mock_session.get.return_value.__aexit__ = AsyncMock(return_value=False)

    result = await fetch_json(mock_session, "http://test.url")
    assert result == {"recovered": True}
    assert call_count == 2
